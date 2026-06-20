import asyncio
import time
from providers.base import ProviderAdapter


class DeepSeekProvider(ProviderAdapter):
    """DeepSeek web UI provider — implementation of ProviderAdapter for chat.deepseek.com."""

    BASE_URL = "https://chat.deepseek.com"

    def __init__(self, engine):
        super().__init__(engine)

    async def new_chat(self, target_url: str = None):
        """Navigate to DeepSeek and wait for the chat input to be ready.
        If the page needs human interaction (verification or login), ensures
        a headed browser is open and returns login_required status.
        """
        if not self._e.is_running:
            raise Exception("Browser Engine not started")
        # ponytail: ignore target_url — it comes from config.json which holds a Gemini URL
        url = self.BASE_URL
        self._log(f"DeepSeek: Navigating to {url}...")
        await self._e.navigate(url)

        # Wait for the chat input — its presence means the page is fully ready
        input_ready = False
        try:
            await self._page.wait_for_selector(
                "textarea[placeholder='Message DeepSeek']",
                state="visible",
                timeout=15000
            )
            input_ready = True
            self._log("DeepSeek: Chat input ready.")
        except Exception as e:
            self._log(f"DeepSeek: Chat input not visible — page needs human interaction: {e}")

        await self.dismiss_agreement_popups()

        if not input_ready:
            # Page requires human attention (verification challenge, login, etc.)
            # Ensure the browser is visible so the user can interact
            if getattr(self._e, 'last_headless', True):
                self._log("DeepSeek: Switching to headed browser for user interaction.")
                profile = getattr(self._e, '_last_profile_name', 'Default')
                await self._e.stop()
                await self._e.start(headless=False, url=url, profile_name=profile)
                await self._e.navigate(url)
                try:
                    cdp = await self._page.context.new_cdp_session(self._page)
                    win_id = (await cdp.send("Browser.getWindowForTarget"))["windowId"]
                    await cdp.send("Browser.setWindowBounds", {
                        "windowId": win_id,
                        "bounds": {"windowState": "normal"}
                    })
                    await cdp.detach()
                except Exception as e:
                    self._log(f"DeepSeek: Could not restore window (non-fatal): {e}")
            return {
                "status": "login_required",
                "message": "DeepSeek requires your attention (verification or login). Please interact with the browser window, then call new_chat() again."
            }

        # Page is ready — log account info if available
        login_state = await self._check_login()
        if login_state.get("logged_in"):
            self._log(f"DeepSeek: Logged in as '{login_state.get('username', 'unknown')}'.")
        else:
            self._log("DeepSeek: Chat ready (account info not visible).")
        return {"status": "success"}

    async def send_chat(self, prompt: str, **kwargs) -> dict:
        """Type prompt, submit, wait for response to finish, and extract reply."""
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        new_conversation = kwargs.get("new_conversation", True)
        if new_conversation:
            self._log("send_chat: initiating a new chat for clean state.")
            nav_result = await self.new_chat()
            if nav_result.get("status") == "login_required":
                return nav_result

        # Target input
        input_selector = "textarea[placeholder='Message DeepSeek']"
        try:
            elem = self._page.locator(input_selector).first
            await elem.click()
            await elem.fill(prompt)
            self._log("DeepSeek: Prompt filled.")
        except Exception as e:
            self._log(f"DeepSeek: Error locating or typing prompt: {e}")
            raise Exception(f"Could not find or interact with chat input: {e}")

        # Submit response
        send_button = self._page.locator("div[role='button'][class*='ds-button--primary']").first
        if await send_button.is_visible():
            await send_button.click()
            self._log("DeepSeek: Clicked send button.")
        else:
            await self._page.keyboard.press("Enter")
            self._log("DeepSeek: Pressed Enter to submit.")

        # Wait for generation to complete by watching for content to stabilize
        await asyncio.sleep(2.0)  # Let generation start
        prev_text = ""
        stable_count = 0
        start_time = time.time()
        while time.time() - start_time < 120:
            cur_text = await self._page.evaluate("""
                () => {
                    const els = document.querySelectorAll('div.ds-markdown');
                    if (!els.length) return '';
                    return els[els.length - 1].innerText || '';
                }
            """)
            if cur_text and cur_text == prev_text:
                stable_count += 1
                if stable_count >= 3:  # Stable for 3 * 0.8s = ~2.4s
                    self._log("DeepSeek: Response stabilized, generation complete.")
                    break
            else:
                stable_count = 0
            prev_text = cur_text
            await asyncio.sleep(0.8)

        # Extract response text
        try:
            response_text = await self._page.evaluate('''() => {
                const els = Array.from(document.querySelectorAll('div[class*="ds-markdown"], div[class*="markdown-body"], div.ds-markdown, div[class*="markdown"]'));
                if (!els.length) return '';
                const last = els[els.length - 1];
                return last.innerText || last.textContent || '';
            }''')
            response_text = response_text.strip()
            self._log(f"DeepSeek: Extracted response text length = {len(response_text)}")
            return {"status": "success", "text": response_text}
        except Exception as e:
            self._log(f"DeepSeek: Error extracting response text: {e}")
            return {"status": "error", "message": f"Failed to extract response text: {e}"}

    async def dismiss_agreement_popups(self):
        """Try to click any agree/accept button; return gracefully if none."""
        if not self._e.is_running:
            return
        try:
            clicked = await self._page.evaluate('''() => {
                const keywords = ["accept", "agree", "got it", "ok", "confirm", "i agree", "consent"];
                const buttons = Array.from(document.querySelectorAll('button'));
                for (const btn of buttons) {
                    const text = (btn.innerText || btn.textContent || "").toLowerCase().trim();
                    if (keywords.some(kw => text === kw || text.includes(kw))) {
                        btn.click();
                        return text;
                    }
                }
                return null;
            }''')
            if clicked:
                self._log(f"DeepSeek: Auto-dismissed agreement popup by clicking button with text: {clicked}")
        except Exception as e:
            self._log(f"DeepSeek: Warning in dismiss_agreement_popups: {e}")

    async def _check_login(self) -> dict:
        """Detect login state by looking for the user avatar image in the sidebar.
        Returns {"logged_in": True, "username": "..."} or {"logged_in": False}.
        """
        try:
            result = await self._page.evaluate("""
                () => {
                    const avatar = document.querySelector('img[src*="user-avatar"]');
                    if (!avatar) return { logged_in: false };
                    // Walk up to find sibling text element (username)
                    const wrapper = avatar.closest('div');
                    const nameEl = wrapper
                        ? wrapper.parentElement
                            ? wrapper.parentElement.querySelector('div:not(:has(img))')
                            : null
                        : null;
                    const username = nameEl ? nameEl.textContent.trim() : null;
                    return { logged_in: true, username: username || 'unknown' };
                }
            """)
            return result
        except Exception as e:
            self._log(f"DeepSeek: _check_login error: {e}")
            return {"logged_in": False}

    async def stop_response(self):
        """Click stop button if visible."""
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        # Check if the primary button is currently in generating/stop state
        is_generating = False
        try:
            is_generating = await self._page.evaluate("""
                () => {
                    const btn = document.querySelector("div[role='button'][class*='ds-button--primary']");
                    if (!btn) return false;
                    if (btn.classList.contains('ds-button--disabled')) return false;
                    const svg = btn.querySelector('svg');
                    if (!svg) return false;
                    return !svg.innerHTML.includes('0.981587') && !svg.innerHTML.includes('M8.3125');
                }
            """)
        except Exception:
            pass

        if is_generating:
            try:
                btn = self._page.locator("div[role='button'][class*='ds-button--primary']").first
                await btn.click()
                self._log("DeepSeek: Clicked primary stop button.")
                return {"status": "success", "message": "Stopped response."}
            except Exception as e:
                self._log(f"DeepSeek: Error clicking primary stop button: {e}")

        # Fallback to other selectors
        stop_selectors = [
            "div[aria-label*='Stop']",
            "button[aria-label*='Stop']",
            "button[title*='Stop']",
            ".ds-icon-stop"
        ]

        for selector in stop_selectors:
            try:
                loc = self._page.locator(selector).first
                if await loc.is_visible(timeout=1000):
                    await loc.click()
                    self._log(f"DeepSeek: Clicked stop button matching '{selector}'.")
                    return {"status": "success", "message": "Stopped response."}
            except Exception as e:
                self._log(f"DeepSeek: Error clicking stop button '{selector}': {e}")

        return {"status": "ignored", "message": "No active stop button found."}

    async def get_last_response(self) -> dict:
        """Read the current text of the last assistant message in the DOM."""
        try:
            text = await self._page.evaluate("""
                () => {
                    const els = document.querySelectorAll('div.ds-markdown');
                    if (!els.length) return '';
                    const last = els[els.length - 1];
                    return last.innerText || last.textContent || '';
                }
            """)
            # Check if still generating — look for primary button not in disabled or send state
            still_generating = False
            try:
                still_generating = await self._page.evaluate("""
                    () => {
                        const btn = document.querySelector("div[role='button'][class*='ds-button--primary']");
                        if (!btn) return false;
                        if (btn.classList.contains('ds-button--disabled')) return false;
                        const svg = btn.querySelector('svg');
                        if (!svg) return false;
                        return !svg.innerHTML.includes('0.981587') && !svg.innerHTML.includes('M8.3125');
                    }
                """)
            except Exception:
                pass
            return {"text": text.strip(), "done": not still_generating}
        except Exception as e:
            return {"text": "", "done": False, "error": str(e)}

    async def test_connection(self):
        """Navigate to BASE_URL, return success or error."""
        if not self._e.is_running:
            return {"status": "error", "message": "Browser Engine not started"}
        try:
            self._log(f"DeepSeek: Testing connection to {self.BASE_URL}...")
            await self._e.navigate(self.BASE_URL)
            return {"status": "success"}
        except Exception as e:
            self._log(f"DeepSeek: Connection test failed: {e}")
            return {"status": "error", "message": str(e)}

    async def attach_files(self, file_paths: list) -> dict:
        """Attach files to the current DeepSeek prompt using the hidden file input."""
        if not self._e.is_running:
            raise Exception("Browser Engine not started")
        try:
            file_input = self._page.locator('input[type="file"]').first
            if not file_paths:
                await file_input.set_input_files([])
                self._log("DeepSeek: Cleared all attachments.")
                return {"status": "success", "added": 0, "removed": 0, "total_now": 0}
            await file_input.set_input_files(file_paths)
            self._log("DeepSeek: Files queued, waiting for submit button to become active...")
            # Upload is done when the primary submit button loses ds-button--disabled class
            start = time.time()
            while time.time() - start < 60:
                is_disabled = await self._page.evaluate(
                    "() => { const btn = document.querySelector('div[role=\"button\"][class*=\"ds-button--primary\"]'); "
                    "return btn ? btn.classList.contains('ds-button--disabled') : true; }"
                )
                if not is_disabled:
                    break
                await asyncio.sleep(0.5)
            self._log("DeepSeek: Submit button active — uploads complete.")
            return {"status": "success", "added": len(file_paths), "removed": 0, "total_now": len(file_paths)}
        except Exception as e:
            self._log(f"DeepSeek: attach_files error: {e}")
            return {"status": "error", "message": str(e)}


    async def submit_response(self, text=None, expect_attachments=False):
        raise NotImplementedError("DeepSeek provider does not support submit_response")

    async def redo_response(self):
        """Click the regenerate button and wait for the response to complete."""
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        self._log("DeepSeek: Attempting to trigger regenerate (redo)...")

        # Locate and click the regenerate button (refresh/circular arrow icon)
        clicked = await self._page.evaluate("""
            () => {
                const path = document.querySelector('svg path[d*="7.92136"]');
                if (path) {
                    const btn = path.closest('[role="button"]');
                    if (btn) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }
        """)

        if not clicked:
            self._log("DeepSeek: Regenerate button with SVG path containing '7.92136' not found.")
            raise Exception("Regenerate button not found in DOM.")

        self._log("DeepSeek: Clicked regenerate button.")

        # Wait for generation to complete by watching for content to stabilize
        await asyncio.sleep(2.0)  # Let generation start
        prev_text = ""
        stable_count = 0
        start_time = time.time()
        while time.time() - start_time < 120:
            cur_text = await self._page.evaluate("""
                () => {
                    const els = document.querySelectorAll('div.ds-markdown');
                    if (!els.length) return '';
                    return els[els.length - 1].innerText || '';
                }
            """)
            if cur_text and cur_text == prev_text:
                stable_count += 1
                if stable_count >= 3:  # Stable for 3 * 0.8s = ~2.4s
                    self._log("DeepSeek: Response stabilized, regenerate complete.")
                    break
            else:
                stable_count = 0
            prev_text = cur_text
            await asyncio.sleep(0.8)

        return {"status": "success", "message": "Regenerated"}

    async def apply_settings(self, model_name=None, tool_name=None, thinking_level=None):
        """Switch DeepSeek mode (Instant / Expert / Vision) via the mode dropdown."""
        if not model_name:
            return {"status": "success", "message": "No changes requested"}
        valid_modes = ["Instant", "Expert", "Vision"]
        target = next((m for m in valid_modes if m.lower() == model_name.lower()), None)
        if not target:
            return {"status": "error", "message": f"Unknown mode '{model_name}'. Available: {valid_modes}"}
        try:
            # Click the currently active mode label to open the dropdown
            mode_selector = ", ".join(f"span:text('{m}')" for m in valid_modes)
            await self._page.locator(mode_selector).first.click()
            await asyncio.sleep(0.5)
            # Click the target mode in the dropdown
            await self._page.locator(f"span:text('{target}')").first.click()
            await asyncio.sleep(0.3)
            self._log(f"DeepSeek: Mode set to {target}")
            return {"status": "success", "message": f"Mode set to {target}"}
        except Exception as e:
            self._log(f"DeepSeek: apply_settings error: {e}")
            return {"status": "error", "message": str(e)}

    async def discover_capabilities(self):
        raise NotImplementedError("DeepSeek provider does not support discover_capabilities")

    async def clear_attachments(self):
        raise NotImplementedError("DeepSeek provider does not support clear_attachments")

    async def download_images(self, save_dir, naming_cfg, extra_meta=None):
        raise NotImplementedError("DeepSeek provider does not support download_images")

    async def delete_activity_history(self, range_name="Last hour"):
        raise NotImplementedError("DeepSeek provider does not support delete_activity_history")

    async def get_gem_title(self) -> dict:
        raise NotImplementedError("DeepSeek provider does not support get_gem_title")

    async def get_gem_info(self) -> dict:
        raise NotImplementedError("DeepSeek provider does not support get_gem_info")

    async def get_account_info(self):
        raise NotImplementedError("DeepSeek provider does not support get_account_info")

    async def send_prompt(self, text: str):
        raise NotImplementedError("DeepSeek provider does not support send_prompt")
