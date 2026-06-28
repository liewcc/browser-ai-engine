import asyncio
import time
from providers.base import ProviderAdapter


class CopilotProvider(ProviderAdapter):
    """Microsoft Copilot web UI provider — copilot.microsoft.com."""

    BASE_URL = "https://copilot.microsoft.com"

    # Selectors confirmed from live DOM capture 2026-06-28
    _SEL_INPUT        = '[data-testid="composer"] textarea'
    _SEL_SEND         = '[data-testid="composer"] button[title*="Send"]'
    _SEL_STOP         = 'button[aria-label="Stop"]'
    _SEL_NEW_CHAT     = '[data-testid="sidebar-new-chat-button"]'
    _SEL_UPLOAD       = '[data-testid="composer-create-button"]'
    _SEL_ACCOUNT_BTN  = '[data-testid="sidebar-settings-button"]'
    _SEL_RESPONSE     = '[data-testid="ai-message-body"]'
    _SEL_REGEN        = 'button[aria-label="Regenerate"]'
    _SEL_MODEL        = 'button[aria-label*="Response mode"]'

    def __init__(self, engine):
        super().__init__(engine)

    # ── Login helpers ──────────────────────────────────────────────────────────

    async def _check_login(self) -> dict:
        """Detect login state via the sidebar account button.
        Logged in  → button contains an img (avatar).
        Logged out → page has a visible Sign-in button.
        """
        try:
            result = await self._page.evaluate(f"""
                () => {{
                    const acct = document.querySelector('{self._SEL_ACCOUNT_BTN}');
                    if (acct) {{
                        const avatar = acct.querySelector('img');
                        if (avatar) {{
                            return {{ logged_in: true, account_id: acct.title || 'unknown' }};
                        }}
                    }}
                    // Check for any visible Sign-in button
                    const signIn = Array.from(document.querySelectorAll('button, a')).find(el => {{
                        const t = (el.innerText || el.textContent || '').toLowerCase().trim();
                        return t === 'sign in' || t === 'log in';
                    }});
                    if (signIn) return {{ logged_in: false, reason: 'sign_in_button' }};
                    return {{ logged_in: false, reason: 'unknown' }};
                }}
            """)
            return result
        except Exception as e:
            self._log(f"Copilot: _check_login error: {e}")
            return {"logged_in": False}

    async def _relaunch_headed(self) -> None:
        """Stop headless browser, relaunch headed so user can log in."""
        url = self.BASE_URL
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
            self._log(f"Copilot: Could not restore window (non-fatal): {e}")

    # ── Core chat methods ──────────────────────────────────────────────────────

    async def new_chat(self, target_url: str = None):
        """Navigate to Copilot, verify login, return login_required if not authenticated."""
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        self._log("Copilot: Checking current page...")
        current_url = self._page.url if self._page else ""
        if "copilot.microsoft.com" not in current_url:
            self._log("Copilot: Navigating to Copilot home...")
            await self._e.navigate(self.BASE_URL)
        await self.dismiss_agreement_popups()

        # Wait for input box — its presence means page is ready
        input_ready = False
        try:
            await self._page.wait_for_selector(
                self._SEL_INPUT, state="visible", timeout=15000
            )
            input_ready = True
            self._log("Copilot: Chat input ready.")
        except Exception as e:
            self._log(f"Copilot: Chat input not visible — page needs interaction: {e}")

        if not input_ready:
            if getattr(self._e, 'last_headless', True):
                self._log("Copilot: Switching to headed browser for user interaction.")
                await self._relaunch_headed()
            return {
                "status": "login_required",
                "message": (
                    "Copilot requires your attention (verification or login). "
                    "Please interact with the browser window, then call new_chat() again."
                )
            }

        # Click New chat button to reset conversation state
        try:
            new_chat_btn = self._page.locator(self._SEL_NEW_CHAT).first
            if await new_chat_btn.is_visible(timeout=3000):
                await new_chat_btn.click()
                await asyncio.sleep(0.5)
                self._log("Copilot: Clicked New chat button.")
        except Exception:
            pass  # Not fatal — page may already be on a fresh chat

        # If the input box is visible the user is on the chat page → logged in.
        # No separate avatar check needed; _check_login() selector is unreliable.
        self._log("Copilot: Chat ready.")
        return {"status": "success"}

    async def send_chat(self, prompt: str, **kwargs) -> dict:
        """Type prompt, submit, wait for response to stabilize, return text."""
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        new_conversation = kwargs.get("new_conversation", True)
        if new_conversation:
            nav_result = await self.new_chat()
            if nav_result.get("status") == "login_required":
                return nav_result

        # Fill input via keyboard.type() — mimics human typing to avoid bot detection.
        # fill() sets the DOM value directly and is detectable; type() sends real key events.
        try:
            inp = self._page.locator(self._SEL_INPUT).first
            await inp.click()
            await asyncio.sleep(0.3)
            await self._page.keyboard.type(prompt, delay=25)
            await asyncio.sleep(0.3)
            self._log("Copilot: Prompt typed.")
        except Exception as e:
            raise Exception(f"Copilot: Could not type chat input: {e}")

        # Submit via Enter (more reliable than clicking the Send button)
        await self._page.keyboard.press("Enter")
        self._log("Copilot: Pressed Enter to submit.")

        # Wait for generation to complete: Stop button disappears → done
        await asyncio.sleep(2.0)
        start_time = time.time()
        while time.time() - start_time < 180:
            try:
                stop_visible = await self._page.locator(self._SEL_STOP).is_visible()
            except Exception:
                stop_visible = False
            if not stop_visible:
                self._log("Copilot: Stop button gone — generation complete.")
                break
            await asyncio.sleep(1.0)

        # Content-stabilization: poll last ai-message-body until text is stable
        prev_text = ""
        stable_count = 0
        for _ in range(15):
            cur_text = await self._page.evaluate(f"""
                () => {{
                    const items = document.querySelectorAll('{self._SEL_RESPONSE}');
                    if (!items.length) return '';
                    return items[items.length - 1].innerText || '';
                }}
            """)
            if cur_text and cur_text == prev_text:
                stable_count += 1
                if stable_count >= 2:
                    break
            else:
                stable_count = 0
            prev_text = cur_text
            await asyncio.sleep(0.8)

        text = prev_text.strip()
        self._log(f"Copilot: Response length = {len(text)}")
        return {"status": "success", "text": text}

    async def get_last_response(self) -> dict:
        """Read the last ai-message-body and whether generation is still running."""
        try:
            text = await self._page.evaluate(f"""
                () => {{
                    const items = document.querySelectorAll('{self._SEL_RESPONSE}');
                    if (!items.length) return '';
                    return items[items.length - 1].innerText || '';
                }}
            """)
            try:
                still_generating = await self._page.locator(self._SEL_STOP).is_visible()
            except Exception:
                still_generating = False
            return {"text": text.strip(), "done": not still_generating}
        except Exception as e:
            return {"text": "", "done": False, "error": str(e)}

    async def stop_response(self):
        """Click the Stop generating button if visible."""
        if not self._e.is_running:
            raise Exception("Browser Engine not started")
        try:
            stop_btn = self._page.locator(self._SEL_STOP).first
            if await stop_btn.is_visible(timeout=2000):
                await stop_btn.click()
                self._log("Copilot: Clicked Stop button.")
                return {"status": "success", "message": "Stopped."}
        except Exception as e:
            self._log(f"Copilot: stop_response warning: {e}")
        return {"status": "ignored", "message": "No active stop button found."}

    async def redo_response(self):
        """Click the Regenerate button on the last message."""
        if not self._e.is_running:
            raise Exception("Browser Engine not started")
        try:
            regen = self._page.locator(self._SEL_REGEN).first
            await regen.wait_for(state="visible", timeout=5000)
            await regen.click()
            self._log("Copilot: Clicked Regenerate button.")
        except Exception as e:
            raise Exception(f"Copilot: Regenerate button not found: {e}")

        # Wait for new response to stabilize
        await asyncio.sleep(2.0)
        prev_text, stable_count = "", 0
        start = time.time()
        while time.time() - start < 120:
            try:
                stop_visible = await self._page.locator(self._SEL_STOP).is_visible()
            except Exception:
                stop_visible = False
            if not stop_visible:
                break
            await asyncio.sleep(1.0)

        return {"status": "success", "message": "Regenerated."}

    async def dismiss_agreement_popups(self):
        """Dismiss cookie banners and modal overlays."""
        if not self._e.is_running:
            return
        try:
            clicked = await self._page.evaluate("""
                () => {
                    // Microsoft cookie/ToS dismiss buttons
                    const selectors = [
                        'dialog[role="dialog"] button[aria-label*="Dismiss"]',
                        '#cookie-banner button',
                        'button[id*="accept"]',
                        'button[id*="cookie"]',
                    ];
                    for (const sel of selectors) {
                        const btn = document.querySelector(sel);
                        if (btn && btn.offsetParent !== null) {
                            btn.click();
                            return sel;
                        }
                    }
                    // Generic keyword fallback
                    const keywords = ["accept all", "accept", "agree", "got it", "dismiss"];
                    for (const btn of document.querySelectorAll('button')) {
                        const t = (btn.innerText || btn.textContent || '').toLowerCase().trim();
                        if (keywords.some(kw => t === kw) && btn.offsetParent !== null) {
                            btn.click();
                            return t;
                        }
                    }
                    return null;
                }
            """)
            if clicked:
                self._log(f"Copilot: Dismissed popup ({clicked}).")
        except Exception as e:
            self._log(f"Copilot: dismiss_agreement_popups warning: {e}")

    # ── Settings / capabilities ────────────────────────────────────────────────

    async def apply_settings(self, model_name=None, tool_name=None, thinking_level=None):
        """Switch Copilot response mode (Smart / Creative / Precise / etc.)."""
        if not model_name:
            return {"status": "success", "message": "No changes requested"}
        try:
            btn = self._page.locator(self._SEL_MODEL).first
            await btn.click()
            await asyncio.sleep(0.4)
            # Click the menu item matching model_name
            await self._page.locator(f'[role="menuitem"]:has-text("{model_name}")').first.click()
            await asyncio.sleep(0.3)
            self._log(f"Copilot: Response mode set to {model_name}.")
            return {"status": "success", "message": f"Mode set to {model_name}"}
        except Exception as e:
            self._log(f"Copilot: apply_settings error: {e}")
            return {"status": "error", "message": str(e)}

    async def discover_capabilities(self):
        """Return available response modes from the model dropdown."""
        try:
            btn = self._page.locator(self._SEL_MODEL).first
            await btn.click()
            await asyncio.sleep(0.5)
            modes = await self._page.evaluate("""
                () => Array.from(document.querySelectorAll('[role="menuitem"]'))
                          .map(el => el.innerText.trim()).filter(Boolean)
            """)
            # Close the menu
            await self._page.keyboard.press("Escape")
            return {"status": "success", "models": modes, "main_tools": [], "sub_tools": {}, "thinking_levels": []}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── File handling ──────────────────────────────────────────────────────────

    async def attach_files(self, file_paths: list) -> dict:
        """Click the create/attach button then set files via the hidden input."""
        if not self._e.is_running:
            raise Exception("Browser Engine not started")
        try:
            if not file_paths:
                return {"status": "success", "added": 0, "removed": 0, "total_now": 0}
            # Open the attach menu
            create_btn = self._page.locator(self._SEL_UPLOAD).first
            await create_btn.click()
            await asyncio.sleep(0.5)
            # Use the hidden file input
            file_input = self._page.locator('input[type="file"]').first
            await file_input.set_input_files(file_paths)
            self._log(f"Copilot: Attached {len(file_paths)} file(s).")
            return {"status": "success", "added": len(file_paths), "removed": 0, "total_now": len(file_paths)}
        except Exception as e:
            self._log(f"Copilot: attach_files error: {e}")
            return {"status": "error", "message": str(e)}

    async def clear_attachments(self):
        raise NotImplementedError("Copilot: clear_attachments not yet implemented")

    async def download_images(self, save_dir, naming_cfg, extra_meta=None):
        raise NotImplementedError("Copilot: download_images not yet implemented")

    # ── Account / history ──────────────────────────────────────────────────────

    async def get_account_info(self) -> dict:
        login_state = await self._check_login()
        return {
            "status": "success",
            "logged_in": login_state.get("logged_in", False),
            "account_id": login_state.get("account_id", ""),
        }

    async def delete_activity_history(self, range_name="Last hour"):
        raise NotImplementedError("Copilot: delete_activity_history not yet implemented")

    # ── Gem methods (not applicable to Copilot) ────────────────────────────────

    async def get_gem_title(self) -> dict:
        return {"status": "unsupported", "message": "Copilot has no Gem concept"}

    async def get_gem_info(self) -> dict:
        return {"status": "unsupported", "message": "Copilot has no Gem concept"}

    # ── Not used in direct-chat mode ───────────────────────────────────────────

    async def send_prompt(self, text: str):
        raise NotImplementedError("Copilot: use send_chat() instead")

    async def submit_response(self, text=None, expect_attachments=False):
        raise NotImplementedError("Copilot: use send_chat() instead")
