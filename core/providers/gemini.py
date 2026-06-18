import asyncio
import os
import re
import time
from datetime import datetime

from config_utils import load_config, save_config
from providers.base import ProviderAdapter


class GeminiProvider(ProviderAdapter):
    """Gemini web UI provider — all Gemini-specific selectors and page logic live here."""

    BASE_URL = "https://gemini.google.com/app"

    # ──────────────────────────────────────────────────────────────────────────
    # send_prompt
    # ──────────────────────────────────────────────────────────────────────────
    async def send_prompt(self, text):
        """Types text into Gemini's prompt area and sends it."""
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        # Target Gemini's prompt input (common selectors)
        prompt_selectors = [
            "div[aria-label='Enter a prompt for Gemini']",
            "div[aria-label='Enter a prompt here']",
            "div.ql-editor[contenteditable='true']",
            "textarea[aria-label='Enter a prompt for Gemini']",
            "textarea[aria-label='Enter a prompt here']",
            # NOTE: "[contenteditable='true']" removed — too broad, causes Playwright
            # strict=True violation when multiple contenteditable elements exist (e.g.
            # after a popup is dismissed and Gemini re-renders its UI).
        ]

        target = None
        retry_waits = [0, 3, 5, 8]  # Progressive waits in seconds (first attempt is instant)
        for attempt, wait_sec in enumerate(retry_waits):
            if wait_sec > 0:
                self._log(f"Prompt input not found. Retrying in {wait_sec}s (attempt {attempt + 1}/{len(retry_waits)})...")
                await asyncio.sleep(wait_sec)
            for sel in prompt_selectors:
                try:
                    elem = self._page.locator(sel).first
                    if await elem.is_visible(timeout=2000):
                        target = elem
                        break
                except:
                    continue
            if target:
                break

        if not target:
            raise Exception("Could not find prompt input area on current page.")

        # Clear existing text if any (Gemini uses contenteditable often)
        await target.click()
        # For contenteditable, sometimes we need to select all and delete
        await self._page.keyboard.press("Control+A")
        await self._page.keyboard.press("Backspace")

        # Type the new prompt
        await target.fill(text) if await target.is_editable() else await target.type(text)

        return {"status": "filled", "prompt": text}

    # ──────────────────────────────────────────────────────────────────────────
    # attach_files
    # ──────────────────────────────────────────────────────────────────────────
    async def attach_files(self, file_paths):
        """
        Smart Incremental Sync (Stem-Based):
        1. Scans Gemini DOM for existing attached filenames.
        2. Compares filenames by STEM (name without extension) to handle Gemini's auto-conversion/renaming.
        3. Only Adds/Removes the differences.
        """
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        LOG_FILE = os.path.join(self._e._data_dir, "engine.log")

        def log_debug(msg):
            timestamp = datetime.now().strftime("[%H:%M:%S]")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} [SYNC] {msg}\n")

        # 1. Detection Phase: Get filenames currently in Gemini
        raw_labels = await self._page.evaluate('''() => {
            const buttons = Array.from(document.querySelectorAll('button[data-test-id="cancel-button"]'));
            return buttons.map(btn => btn.getAttribute('aria-label') || "").filter(l => l.length > 0);
        }''')

        attached_filenames = []
        for label in raw_labels:
            parts = label.split()
            low_parts = [p.lower() for p in parts]
            if "file" in low_parts:
                idx = low_parts.index("file")
                name = " ".join(parts[idx+1:]).strip()
            else:
                name = parts[-1].strip()
            if name.endswith('.'): name = name[:-1]
            attached_filenames.append(name.strip())

        # CRITICAL FIX: Match by STEM (filename without extension)
        # Because Gemini often renames .png to .jpg in the label.
        def get_stem(filename):
            return os.path.splitext(filename)[0].lower()

        attached_stems = [get_stem(n) for n in attached_filenames]

        # Build local target mapping by stem
        target_map = {}  # stem -> (original_name, full_path)
        for p in file_paths:
            base = os.path.basename(p)
            target_map[get_stem(base)] = (base, p)

        target_stems = list(target_map.keys())

        log_debug(f"Browser has (Raw): {attached_filenames}")
        log_debug(f"Target has (Raw): {[v[0] for v in target_map.values()]}")
        log_debug(f"Matching via stems: {attached_stems} vs {target_stems}")

        added_count = 0
        removed_count = 0

        # 2. Remove Phase: Delete files from browser whose STEM is NOT in target
        for i, stem in enumerate(attached_stems):
            if stem not in target_stems:
                real_name = attached_filenames[i]
                try:
                    log_debug(f"Removing (Stem mismatch): {real_name}")
                    selector = f'button[data-test-id="cancel-button"][aria-label*="{real_name}"]'
                    btn = self._page.locator(selector).first
                    if await btn.is_visible():
                        await btn.click()
                        removed_count += 1
                        await asyncio.sleep(0.8)
                except Exception as e:
                    log_debug(f"Remove failed: {real_name} -> {e}")

        # 3. Add Phase: Upload local files whose STEM is NOT in browser
        for stem, (orig_name, full_path) in target_map.items():
            if stem not in attached_stems:
                if not os.path.exists(full_path):
                    continue

                try:
                    log_debug(f"Adding (New stem): {orig_name}")
                    async with self._page.expect_file_chooser(timeout=20000) as fc_info:
                        await self._page.evaluate('''() => {
                            const plusBtn = document.querySelector('button[aria-label="Upload & tools"]') ||
                                            document.querySelector('button[aria-label="Open upload file menu"]') ||
                                            document.querySelector('button[aria-label*="upload" i]') ||
                                            document.querySelector('button[aria-label*="Upload" i]');
                            if (plusBtn) {
                                plusBtn.click();
                            } else {
                                const gemsIcon = document.querySelector('mat-icon[data-mat-icon-name="add_2"]') ||
                                               document.querySelector('mat-icon[fonticon="add"]');
                                if (gemsIcon) { gemsIcon.closest('button').click(); }
                            }
                        }''')
                        await asyncio.sleep(1.2)
                        await self._page.evaluate('''() => {
                            const explicitIcon = document.querySelector('[data-test-id="local-images-files-uploader-icon"]');
                            if (explicitIcon) {
                                const menuItem = explicitIcon.closest('.mat-mdc-menu-item, [role="menuitem"], button');
                                if (menuItem) {
                                    menuItem.click();
                                    return;
                                }
                            }

                            const opt = Array.from(document.querySelectorAll('.menu-text, span, .mdc-list-item__primary-text'))
                                             .find(i => {
                                                 const txt = i.innerText.toLowerCase();
                                                 return txt.includes("upload") || txt.includes("attach");
                                             });
                            if (opt) opt.click();
                        }''')
                        file_chooser = await fc_info.value
                        await file_chooser.set_files(full_path)

                    # PROACTIVE: Immediately check for the MMGen disclaimer after upload
                    await self.dismiss_agreement_popups()

                    added_count += 1
                    await asyncio.sleep(2.5)
                except Exception as e:
                    log_debug(f"Add failed: {orig_name} -> {e}")
            else:
                log_debug(f"Skipping (Stem already present): {orig_name}")

        return {
            "status": "success",
            "added": added_count,
            "removed": removed_count,
            "total_now": len(file_paths)
        }

    # ──────────────────────────────────────────────────────────────────────────
    # discover_capabilities
    # ──────────────────────────────────────────────────────────────────────────
    async def discover_capabilities(self):
        """
        Scans Gemini DOM to find available models, thinking levels, and tools.
        All options are read dynamically from the live DOM — nothing is hardcoded.
        Updates config.json with current selections only (not available options).
        """
        if not self._e.is_running:
            return {"status": "error", "message": "Browser not started"}

        self._log("Starting discovery scan...")
        results = {
            "models": [],
            "thinking_levels": [],
            "current_model": "Unknown",
            "current_thinking_level": None,
            "main_tools": [],   # ordered list matching Gemini UI (includes "More uploads" / "More tools" markers)
            "sub_tools": {},    # {"More uploads": [...], "More tools": [...]}
            # legacy aliases kept for any callers that still read these
            "tools": [],
            "upload_tools": [],
        }

        try:
            # 1. Discover Models + Thinking Levels
            # Read current model from pill header (.picker-primary-text confirmed in DOM)
            current_model_el = await self._page.query_selector(
                'button[data-test-id="bard-mode-menu-button"] .picker-primary-text'
            )
            if current_model_el:
                results["current_model"] = (await current_model_el.inner_text()).strip()

            # Open model menu
            await self._page.click('button[data-test-id="bard-mode-menu-button"]')
            # Wait until at least one model option is in the DOM
            try:
                await self._page.wait_for_selector(
                    'gem-menu-item[data-test-id^="bard-mode-option-"]', timeout=5000
                )
            except Exception:
                self._log("Model menu did not appear.")

            await asyncio.sleep(0.5)

            # Extract model names — text lives in span.label inside gem-menu-item-content
            results["models"] = await self._page.evaluate('''() => {
                return Array.from(
                    document.querySelectorAll('gem-menu-item[data-test-id^="bard-mode-option-"]')
                ).map(item => {
                    const label = item.querySelector('span.label');
                    return label ? label.innerText.trim() : '';
                }).filter(t => t.length > 0);
            }''')

            # Read current thinking level from the "Thinking level" menu item sublabel
            results["current_thinking_level"] = await self._page.evaluate('''() => {
                const item = document.querySelector('gem-menu-item[value="thinking_level"]');
                if (!item) return null;
                const sub = item.querySelector('.sublabel');
                return sub ? sub.innerText.trim() : null;
            }''')

            # Expand the "Thinking level" sub-menu via JS mouseenter (Playwright hover()
            # fires mouseleave on the parent panel and collapses the whole menu)
            expanded = await self._page.evaluate('''() => {
                const item = document.querySelector('gem-menu-item[value="thinking_level"]');
                if (!item) return false;
                item.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
                item.dispatchEvent(new MouseEvent('mouseover',  {bubbles: true}));
                return true;
            }''')

            if expanded:
                await asyncio.sleep(0.8)
                # The sub-menu panel is a sibling div[popover] rendered after the thinking_level item
                results["thinking_levels"] = await self._page.evaluate('''() => {
                    const parent = document.querySelector('gem-menu-item[value="thinking_level"]');
                    if (!parent) return [];
                    let el = parent.nextElementSibling;
                    while (el) {
                        if (el.hasAttribute('popover') || el.classList.contains('cdk-overlay-popover')) {
                            return Array.from(el.querySelectorAll('gem-menu-item span.label'))
                                .map(s => s.innerText.trim())
                                .filter(t => t.length > 0);
                        }
                        el = el.nextElementSibling;
                    }
                    return [];
                }''')

            # Close menu
            await self._page.keyboard.press("Escape")
            await asyncio.sleep(0.5)

            # 2. Discover Tools (in Gemini UI order)
            # Order: upload items → [More uploads] → ai tools → [More tools]
            self._log("Attempting to open Tools drawer...")
            opened = False
            for locator in [
                self._page.locator('button[aria-label="Upload & tools"]').first,
                self._page.locator('button.toolbox-drawer-button').first,
            ]:
                try:
                    if await locator.is_visible(timeout=1000):
                        await locator.click()
                        opened = True
                        break
                except Exception:
                    pass
            if not opened:
                await self._page.evaluate('''() => {
                    const btn = Array.from(document.querySelectorAll('button'))
                                     .find(b => (b.getAttribute("aria-label") || "").toLowerCase().includes("tools")
                                             || b.innerText.includes("Tools"));
                    if (btn) btn.click();
                }''')

            try:
                await self._page.wait_for_selector('toolbox-drawer-item', timeout=5000)
            except Exception:
                self._log("Tools drawer did not appear.")
            await asyncio.sleep(0.5)

            main_tools: list = []
            sub_tools: dict  = {}

            # ── Step A: upload section base items (Upload files, Add from Drive) ──
            # Photos/Notebooks use .gem-menu-item-label (no .menu-text), so use broad selector
            _UPLOAD_LABEL_JS = '''Array.from(document.querySelectorAll(
                    'mat-action-list button[role="menuitem"]'
                )).map(btn => {
                    const el = btn.querySelector('.menu-text.gem-menu-item-label')
                             || btn.querySelector('.menu-text')
                             || btn.querySelector('.gem-menu-item-label')
                             || btn.querySelector('.mdc-list-item__primary-text');
                    return el ? el.innerText.split("\\n")[0].trim() : '';
                }).filter(t => t.length > 0)'''

            upload_base: list = await self._page.evaluate(f'() => {{{_UPLOAD_LABEL_JS}}}')
            main_tools.extend(upload_base)

            # ── Step B: "More uploads" marker + sub-items (Photos, Notebooks) ──
            # After clicking more-upload-button, Photos and Notebooks are added directly
            # to the same mat-action-list — there is no separate .more-uploads-list container.
            has_more_uploads = await self._page.evaluate('''() => {
                const btn = document.querySelector('button.more-upload-button');
                return !!(btn && btn.offsetParent !== null);
            }''')
            if has_more_uploads:
                main_tools.append("More uploads")
                try:
                    await self._page.click('button.more-upload-button')
                    await asyncio.sleep(0.6)
                    all_upload: list = await self._page.evaluate(f'() => {{{_UPLOAD_LABEL_JS}}}')
                    base_set = set(upload_base)
                    sub_tools["More uploads"] = [t for t in all_upload if t not in base_set]
                except Exception as exc:
                    self._log(f"More-uploads sub-scan failed: {exc}")

            # ── Step C: base AI tools (Create image, Canvas) — before expanding ──
            ai_base: list = await self._page.evaluate('''() => {
                return Array.from(document.querySelectorAll('toolbox-drawer-item'))
                    .map(i => {
                        const el = i.querySelector('.label.gem-menu-item-label')
                                || i.querySelector('.label.gds-label-l')
                                || i.querySelector('.mdc-list-item__primary-text');
                        return el ? el.innerText.split('\\n')[0].trim() : '';
                    }).filter(t => t.length > 0);
            }''')
            main_tools.extend(ai_base)

            # ── Step D: "More tools" marker + sub-items (Create music, Guided learning) ──
            has_more_tools = await self._page.evaluate('''() => {
                const btn = document.querySelector('button.more-tools-button');
                return !!(btn && btn.offsetParent !== null);
            }''')
            if has_more_tools:
                main_tools.append("More tools")
                try:
                    await self._page.click('button.more-tools-button')
                    await asyncio.sleep(0.5)
                    ai_all: list = await self._page.evaluate('''() => {
                        return Array.from(document.querySelectorAll('toolbox-drawer-item'))
                            .map(i => {
                                const el = i.querySelector('.label.gem-menu-item-label')
                                        || i.querySelector('.label.gds-label-l')
                                        || i.querySelector('.mdc-list-item__primary-text');
                                return el ? el.innerText.split('\\n')[0].trim() : '';
                            }).filter(t => t.length > 0);
                    }''')
                    ai_base_set = set(ai_base)
                    sub_tools["More tools"] = [t for t in ai_all if t not in ai_base_set]
                except Exception as exc:
                    self._log(f"More-tools sub-scan failed: {exc}")

            results["main_tools"]   = main_tools
            results["sub_tools"]    = sub_tools
            # legacy aliases
            results["tools"]        = ai_base
            results["upload_tools"] = upload_base

            # Close drawer
            await self._page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            await self._page.keyboard.press("Escape")

            # Only persist current selections — available options are always live-scanned
            try:
                save_config({
                    "discovery": {
                        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "current_model": results["current_model"],
                        "current_thinking_level": results["current_thinking_level"],
                    }
                })
                self._log(
                    f"Discovery complete. "
                    f"Models: {results['models']}, "
                    f"Thinking: {results['thinking_levels']}, "
                    f"Main tools ({len(results['main_tools'])}): {results['main_tools']}, "
                    f"Sub-menus: {results['sub_tools']}"
                )
            except Exception as e:
                self._log(f"Failed to save discovery: {e}")

            return {"status": "success", "data": results}

        except Exception as e:
            self._log(f"Discovery failed: {e}")
            return {"status": "error", "message": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # apply_settings
    # ──────────────────────────────────────────────────────────────────────────
    async def apply_settings(self, model_name=None, tool_name=None, thinking_level=None):
        """Applies model, thinking level, and/or tool to the Gemini UI."""
        if not self._e.is_running:
            return {"status": "error", "message": "Browser not started"}

        applied = []
        try:
            # 1. Apply Model
            if model_name:
                self._log(f"Applying model: {model_name}")
                await self._page.click('button[data-test-id="bard-mode-menu-button"]')
                await asyncio.sleep(0.8)
                clicked = await self._page.evaluate('''(name) => {
                    const items = Array.from(document.querySelectorAll(
                        'gem-menu-item[data-test-id^="bard-mode-option-"]'
                    ));
                    const target = items.find(i => {
                        const raw = (i.querySelector("span.label") || i).innerText
                                        .split("\\n")[0].trim().toLowerCase();
                        return raw.startsWith(name.toLowerCase()) || name.toLowerCase().startsWith(raw);
                    });
                    if (target) { target.click(); return true; }
                    return false;
                }''', model_name)
                await asyncio.sleep(1.5)
                applied.append(f"model={'ok' if clicked else 'not found'}")

            # 2. Apply Thinking Level (requires model menu to be open — re-open after model click)
            if thinking_level:
                self._log(f"Applying thinking level: {thinking_level}")
                await self._page.click('button[data-test-id="bard-mode-menu-button"]')
                await asyncio.sleep(0.8)

                # Trigger sub-menu via JS mouseenter (Playwright hover() collapses the menu)
                expanded = await self._page.evaluate('''() => {
                    const item = document.querySelector('gem-menu-item[value="thinking_level"]');
                    if (!item) return false;
                    item.dispatchEvent(new MouseEvent("mouseenter", {bubbles: true}));
                    item.dispatchEvent(new MouseEvent("mouseover",  {bubbles: true}));
                    return true;
                }''')

                if expanded:
                    await asyncio.sleep(0.8)
                    clicked = await self._page.evaluate('''(level) => {
                        const parent = document.querySelector('gem-menu-item[value="thinking_level"]');
                        if (!parent) return false;
                        let el = parent.nextElementSibling;
                        while (el) {
                            if (el.hasAttribute("popover") || el.classList.contains("cdk-overlay-popover")) {
                                const items = Array.from(el.querySelectorAll("gem-menu-item"));
                                const target = items.find(i => {
                                    const lbl = i.querySelector("span.label");
                                    return lbl && lbl.innerText.trim().toLowerCase() === level.toLowerCase();
                                });
                                if (target) { target.click(); return true; }
                                return false;
                            }
                            el = el.nextElementSibling;
                        }
                        return false;
                    }''', thinking_level)
                    await asyncio.sleep(1.0)
                    applied.append(f"thinking={'ok' if clicked else 'not found'}")
                else:
                    applied.append("thinking=no submenu item")

                await self._page.keyboard.press("Escape")
                await asyncio.sleep(0.3)

            # 3. Apply Tool
            if tool_name:
                self._log(f"Applying tool: {tool_name}")
                # Open Tools drawer
                btn = self._page.locator('button.toolbox-drawer-button').first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                else:
                    await self._page.evaluate('''() => {
                        const b = Array.from(document.querySelectorAll("button"))
                                       .find(b => b.innerText.includes("Tools"));
                        if (b) b.click();
                    }''')
                await asyncio.sleep(1.0)

                def _js_match_upload(name: str) -> str:
                    # Photos uses .menu-text.gem-menu-item-label; Notebooks uses .gem-menu-item-label only
                    return f'''(name) => {{
                        const btns = Array.from(document.querySelectorAll(
                            'mat-action-list button[role="menuitem"]'
                        ));
                        const target = btns.find(b => {{
                            const el = b.querySelector(".menu-text.gem-menu-item-label")
                                    || b.querySelector(".menu-text")
                                    || b.querySelector(".gem-menu-item-label")
                                    || b.querySelector(".mdc-list-item__primary-text");
                            const txt = el ? el.innerText.split("\\n")[0].trim() : b.innerText.split("\\n")[0].trim();
                            return txt.toLowerCase() === name.toLowerCase();
                        }});
                        if (target) {{ target.click(); return true; }}
                        return false;
                    }}'''

                def _js_match_ai(name: str) -> str:
                    return f'''(name) => {{
                        const menu = document.getElementById("toolbox-drawer-menu");
                        if (!menu) return false;
                        const items = Array.from(menu.querySelectorAll("toolbox-drawer-item"));
                        const target = items.find(i => {{
                            const lbl = i.querySelector(".label.gds-label-l, .label.gem-menu-item-label, .mdc-list-item__primary-text");
                            return lbl && lbl.innerText.toLowerCase().includes(name.toLowerCase());
                        }});
                        if (target) {{ const b = target.querySelector("button"); if (b) b.click(); return !!b; }}
                        return false;
                    }}'''

                # Pass 1: visible upload items (Upload files, Add from Drive)
                clicked = await self._page.evaluate(_js_match_upload(tool_name), tool_name)

                if not clicked:
                    # Pass 2: expand "More uploads" then retry (Photos, Notebooks)
                    more_up = self._page.locator('button.more-upload-button').first
                    if await more_up.is_visible(timeout=1000):
                        await more_up.click()
                        await asyncio.sleep(0.5)
                        clicked = await self._page.evaluate(_js_match_upload(tool_name), tool_name)
                        if not clicked:
                            await self._page.keyboard.press("Escape")
                            await asyncio.sleep(0.3)

                if not clicked:
                    # Pass 3: visible AI tools (Create image, Canvas)
                    clicked = await self._page.evaluate(_js_match_ai(tool_name), tool_name)

                if not clicked:
                    # Pass 4: expand "More tools" then retry (Create music, Guided learning)
                    more_tools = self._page.locator('button.more-tools-button').first
                    if await more_tools.is_visible(timeout=1000):
                        await more_tools.click()
                        await asyncio.sleep(0.5)
                        clicked = await self._page.evaluate(_js_match_ai(tool_name), tool_name)

                await asyncio.sleep(1.0)
                applied.append(f"tool={'ok' if clicked else 'not found'}")

            summary = ", ".join(applied) if applied else "nothing to apply"
            self._log(f"apply_settings done: {summary}")
            return {"status": "success", "message": summary}
        except Exception as e:
            self._log(f"Apply settings failed: {e}")
            return {"status": "error", "message": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # clear_attachments
    # ──────────────────────────────────────────────────────────────────────────
    async def clear_attachments(self):
        """
        Forcefully removes all file attachments from the Gemini UI.
        Matches all elements with data-test-id="cancel-button".
        """
        if not self._e.is_running:
            return {"status": "error", "message": "Browser not started"}

        try:
            # Locate all cancel buttons
            buttons = await self._page.query_selector_all('button[data-test-id="cancel-button"]')
            removed = 0
            for btn in buttons:
                try:
                    await btn.click()
                    removed += 1
                    await asyncio.sleep(0.5)
                except:
                    continue
            return {"status": "success", "removed": removed}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # dismiss_agreement_popups
    # ──────────────────────────────────────────────────────────────────────────
    async def dismiss_agreement_popups(self):
        """
        Detects and clicks 'Agree' or 'Got it' buttons in modal dialogs.
        Specifically handles the 'Creating content from images and files' popup.
        """
        if not self._e.is_running or not self._page:
            return

        # Target buttons with specific text patterns and data-test-ids
        popup_selectors = [
            'button[data-test-id="upload-image-agree-button"]',  # Precise MMGen Agree
            "button:has-text('Agree')",
            "button:has-text('I agree')",
            "button:has-text('Got it')",
            "button:has-text('Confirm')",
            "button:has-text('同意')"  # Support for Chinese UI
        ]

        try:
            # We use a very short timeout - if it's there, we kill it; if not, we move on.
            for selector in popup_selectors:
                btn = self._page.locator(selector).first
                if await btn.is_visible(timeout=1500):
                    self._log(f"Popup detected. Clicking: {selector}")
                    await btn.click()
                    await asyncio.sleep(1.0)  # Grace period for animation
                    return True
        except Exception:
            # Silence timeouts - if button isn't found/visible, it's not a failure
            pass
        return False

    # ──────────────────────────────────────────────────────────────────────────
    # get_gem_title
    # ──────────────────────────────────────────────────────────────────────────
    async def get_gem_title(self) -> dict:
        """Extracts the Custom Gem Title from the active Gemini page."""
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        try:
            # We look for the main heading element that typically holds the Gem name
            # in gemini.google.com/gem/*
            title_text = await self._page.evaluate('''() => {
                const clean = (t) => t ? t.trim().replace(/\\n/g, ' ') : "";

                // Try to find Name using the reliable legacy selector
                const nameContainer = document.querySelector('.bot-name-container');
                let name = "";
                if (nameContainer) {
                    const temp = nameContainer.cloneNode(true);
                    const badge = temp.querySelector('bot-experiment-badge, .bot-name-container-animation-box');
                    if (badge) badge.remove();
                    name = clean(temp.innerText);
                    if (name) return name;
                }

                // Fallback to document title, stripped of generic "Gemini"
                const docTitle = document.title;
                if (docTitle.includes(" - Gemini") || docTitle === "Gemini") {
                    return docTitle.replace(" - Gemini", "").trim();
                }
                return docTitle;
            }''')

            return {"status": "success", "title": title_text or "Unknown"}
        except Exception as e:
            self._log(f"Error extracting gem title: {e}")
            return {"status": "error", "message": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # get_gem_info
    # ──────────────────────────────────────────────────────────────────────────
    async def get_gem_info(self) -> dict:
        """Extracts the Custom Gem Title AND Description from the active Gemini Gem page."""
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        try:
            result = await self._page.evaluate('''() => {
                const clean = (t) => t ? t.trim().replace(/\\n/g, ' ') : "";

                // --- Extract Name (Exact logic from get_gem_title) ---
                const nameContainer = document.querySelector('.bot-name-container');
                let name = "";
                if (nameContainer) {
                    const temp = nameContainer.cloneNode(true);
                    const badge = temp.querySelector('bot-experiment-badge, .bot-name-container-animation-box');
                    if (badge) badge.remove();
                    name = clean(temp.innerText);
                }
                if (!name) {
                    // Fallback to document title, stripped of generic "Gemini"
                    const docTitle = document.title;
                    if (docTitle.includes(" - Gemini") || docTitle === "Gemini") {
                        name = docTitle.replace(" - Gemini", "").trim();
                    } else {
                        name = docTitle;
                    }
                }

                // --- Extract Description ---
                let description = "";
                // Primary: dedicated description container
                const descContainer = document.querySelector('.bot-description-container');
                if (descContainer) {
                    description = clean(descContainer.innerText);
                }
                // Fallback: look for the subtitle/instruction text near the gem header
                if (!description) {
                    const subtitle = document.querySelector('.bot-subtitle, .bot-instruction-text, .gem-description');
                    if (subtitle) {
                        description = clean(subtitle.innerText);
                    }
                }
                // Fallback: aria-label on the main gem card
                if (!description) {
                    const card = document.querySelector('[data-test-id="gem-card"]');
                    if (card) {
                        const label = card.getAttribute('aria-label') || "";
                        if (label && label !== name) {
                            description = clean(label);
                        }
                    }
                }

                return { name: name || "Unknown Gem", description: description || "" };
            }''')

            return {
                "status": "success",
                "name": result.get("name", "Unknown Gem"),
                "description": result.get("description", "")
            }
        except Exception as e:
            self._log(f"Error extracting gem info: {e}")
            return {"status": "error", "message": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # submit_response
    # ──────────────────────────────────────────────────────────────────────────
    async def submit_response(self, text=None, expect_attachments=False):
        """
        1. Injects prompt if provided.
        2. Presses Enter to submit.
        3. Monitors DOM for: Success (image), Quota Exceeded, or Policy Refusal.
        """
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        # Get the src of the last image on the page before we submit or monitor
        self._e._last_seen_src = await self._page.evaluate('''() => {
            const allResps = Array.from(document.querySelectorAll('model-response, structured-content-container.model-response-text, message-content'));
            const responses = allResps.filter(el => !allResps.some(parent => parent !== el && parent.contains(el)));
            if (responses.length === 0) return null;
            const lastResp = responses[responses.length - 1];
            const img = lastResp.querySelector('single-image img, img.generated-image, .generated-image img, .image-container img, img[alt*="generated" i], img[src^="blob:"]');
            return img ? img.src : null;
        }''')
        self._log(f"Success monitor: last seen image src = {self._e._last_seen_src[:60] if self._e._last_seen_src else 'None'}")

        if text:
            await self.send_prompt(text)
            # Submit only if we injected text
            await self._page.keyboard.press("Enter")
            self._log("Prompt submitted via Enter. Verifying submission...")

            # Brief pause then verify the prompt was actually submitted.
            # In the new Gemini UI (2026-05), Enter may not always trigger submit
            # if the input loses focus or the UI intercepts the keystroke.
            await asyncio.sleep(0.8)
            still_has_text = await self._page.evaluate('''() => {
                const editor = document.querySelector(".ql-editor, div[aria-label='Enter a prompt for Gemini'], div[aria-label='Enter a prompt here']");
                return !!(editor && editor.innerText && editor.innerText.trim().length > 0);
            }''')
            if still_has_text:
                self._log("Text still in input after Enter — falling back to button click.")
                try:
                    # New UI: button[aria-label="Send message"] inside gem-icon-button.submit
                    btn = self._page.locator(
                        'gem-icon-button.submit button[aria-label="Send message"], '
                        'gem-icon-button.send-button button[aria-label="Send message"], '
                        'button[aria-label="Send message"]'
                    ).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        self._log("Fallback button click submitted.")
                    else:
                        self._log("Fallback button not visible — relying on Enter.")
                except Exception as _e:
                    self._log(f"Fallback click failed: {_e}")

            # CRITICAL: Dismiss "Creating content from images/files" popup
            await self.dismiss_agreement_popups()

            self._log("Monitoring for response...")
        else:
            self._log("Monitoring existing response (no prompt injected)...")

        # Load quota and refusal keywords from external JSON files
        quota_kws = ["quota exceeded", "daily limit", "reached your limit"]
        refused_kws = []
        try:
            from config_utils import load_quota_keywords, load_refused_keywords
            quota_kws = [k.lower() for k in load_quota_keywords()]
            refused_kws = load_refused_keywords()
        except Exception as e:
            self._log(f"Failed to load keyword files: {e}")

        self._log("Waiting for Gemini response...")

        has_started_generating = False
        start_gen_time = None
        idle_start_time = None
        last_logged_text = ""

        for _ in range(90):  # 180 seconds max
            if self._e._stop_automation_event.is_set():
                self._log("Stop signal received during monitoring. Bailing out.")
                # Also try to click the browser's stop button to halt generation
                try:
                    await self.stop_response()
                except:
                    pass
                return {"status": "stopped", "message": "Monitoring interrupted by stop signal."}

            data = await self._page.evaluate('''(args) => {
                const bodyText = document.body.innerText.toLowerCase();

                // 1. Quota check (text-based - these are standard system phrases)
                for (const kw of args.quota) {
                    if (bodyText.includes(kw)) return { status: "quota_exceeded", text: kw };
                }

                // Utility to check real visibility.
                // Uses getComputedStyle instead of offsetWidth/offsetHeight:
                // offsetWidth/offsetHeight return 0 in minimized windows even for visible
                // elements. getComputedStyle reads the CSS cascade and is always accurate
                // regardless of window state (minimized, headless, off-screen).
                const isVisible = (el) => {
                    if (!el) return false;
                    const s = window.getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                };

                // 2. Active generation signals (highest priority)
                // stopIcon: truly appears/disappears — DOM presence check is sufficient.
                // progressBar + activeLoadingContainer: Angular Material keeps these elements
                // in the DOM at all times (hidden via CSS). Must use isVisible() to avoid
                // permanently treating every state as "generating".
                // Stop/generating icon detection — covers both old and new Gemini UI.
                // In the new UI (2026-05), icons may use the "lumi-symbols" namespace
                // and may use stop_circle instead of plain stop.
                const stopIcon =
                    document.querySelector('mat-icon[data-mat-icon-name="stop"]') ||
                    document.querySelector('mat-icon[fonticon="stop"]') ||
                    document.querySelector('mat-icon[data-mat-icon-name="stop_circle"]') ||
                    document.querySelector('mat-icon[fonticon="stop_circle"]') ||
                    document.querySelector('mat-icon[data-mat-icon-namespace="lumi-symbols"][data-mat-icon-name="stop"]') ||
                    document.querySelector('mat-icon[data-mat-icon-namespace="lumi-symbols"][data-mat-icon-name="stop_circle"]') ||
                    // Fallback: any mat-icon inside the send-button area whose name is NOT arrow_upward/send
                    // (i.e., it has switched to a stop variant)
                    (() => {
                        const sendBtn = document.querySelector('gem-icon-button.submit mat-icon, gem-icon-button.send-button mat-icon');
                        if (!sendBtn) return null;
                        const n = sendBtn.getAttribute('data-mat-icon-name') || sendBtn.getAttribute('fonticon') || '';
                        // If the icon name is NOT a send variant, assume it's a stop variant
                        return (n && n !== 'arrow_upward' && n !== 'send' && n !== 'send_spark') ? sendBtn : null;
                    })();
                const progressBar = document.querySelector('mat-progress-bar');
                const activeLoadingContainer = document.querySelector('section.processing-state_container--processing');

                if (stopIcon || isVisible(progressBar) || isVisible(activeLoadingContainer)) {
                    // Refusal keywords loaded from refused_keywords.json via args.refused
                    const refusalKws = args.refused || [];
                    let genText = "";
                    if (activeLoadingContainer) {
                        // 2026-06 fix: Gemini can simultaneously show the processing spinner
                        // AND a refusal message in structured-content-container.processing-state-visible.
                        // Check that element first — if it contains refusal text the response is
                        // already decided even though the spinner is still running.
                        const processingVisible = document.querySelector('structured-content-container.processing-state-visible');
                        if (processingVisible) {
                            const pvText = (processingVisible.querySelector('.model-response-text') || processingVisible.querySelector('message-content') || processingVisible).innerText.trim();
                            if (pvText.length > 0 && refusalKws.some(kw => pvText.toLowerCase().includes(kw.toLowerCase()))) {
                                return { status: "refused", text: pvText };
                            }
                        }
                        // Extract text from the active loading container's specific spans
                        const labelSpan = activeLoadingContainer.querySelector('.processing-state_ext-name_label span');
                        const placeholderSpan = activeLoadingContainer.querySelector('.processing-state_ext-name_placeholder span');

                        if (labelSpan && labelSpan.textContent) {
                            genText = labelSpan.textContent.trim();
                        } else if (placeholderSpan && placeholderSpan.textContent) {
                            genText = placeholderSpan.textContent.trim();
                        } else {
                            const _cl = activeLoadingContainer.cloneNode(true);
                            _cl.querySelectorAll('.cdk-visually-hidden,[aria-hidden="true"]').forEach(function(e){e.remove();});
                            genText = _cl.textContent.trim();
                        }
                    } else {
                        // Extract regular streaming generation text
                        let allResps = Array.from(document.querySelectorAll('model-response, structured-content-container.model-response-text, message-content'));
                        const responses = allResps.filter(el => !allResps.some(parent => parent !== el && parent.contains(el)));
                        const lastResp = responses.length > 0 ? responses[responses.length - 1] : null;
                        if (lastResp) {
                            const contentNode = lastResp.querySelector('.model-response-text') || lastResp.querySelector('.message-content') || lastResp;
                            const _cl2 = contentNode.cloneNode(true);
                            _cl2.querySelectorAll('.cdk-visually-hidden,[aria-hidden="true"]').forEach(function(e){e.remove();});
                            genText = _cl2.textContent.trim();
                            // Detect refusal keywords in the current streaming response immediately,
                            // without requiring .response-footer.complete. Gemini streams the refusal
                            // text while the stop-icon is still visible; by the time sendReady fires
                            // the DOM may have restructured and respText becomes empty, causing the
                            // refusal to be misclassified as idle/error.
                            if (genText.length > 0 && refusalKws.some(kw => genText.toLowerCase().includes(kw.toLowerCase()))) {
                                return { status: "refused", text: genText };
                            }
                        }
                    }
                    return { status: "generating", text: genText };
                }

                // 3. Idle state (send button ready)
                // ROOT CAUSE: isVisible() uses offsetWidth/offsetHeight which can be 0
                // in a minimized window even when the element is logically "visible".
                //
                // NEW STRATEGY: Check DOM presence + semantic attributes instead.
                //
                // The outer container has data-test-id="send-button-container" and
                // receives the class "visible" when the send button is active/ready.
                // gem-icon-button has aria-disabled="false" when interactive.
                // Neither check requires layout dimensions — works in minimized windows.
                //
                // 2026-05 UPDATE: Gemini redesigned the submit button icon from "send"
                // to "arrow_upward" under the "lumi-symbols" icon namespace.
                //
                // CRITICAL FIX: In the new Gemini UI, the send-button-container div
                // keeps the "visible" class at ALL times (even while Gemini is generating).
                // We MUST require the "arrow_upward" (send-mode) icon to be present in
                // the button, to distinguish idle-ready from active-generating state.
                // Without this check, sendReady fires immediately after submission,
                // before Gemini even starts generating, causing false reset detections.
                const _sendModeSelectors = [
                    'mat-icon[data-mat-icon-name="arrow_upward"]',
                    'mat-icon[fonticon="arrow_upward"]',
                    'mat-icon[data-mat-icon-name="send"]',
                    'mat-icon[fonticon="send"]',
                    'mat-icon[data-mat-icon-name="send_spark"]',
                    'mat-icon[fonticon="send_spark"]',
                ];
                const _inSendMode = _sendModeSelectors.some(s => !!document.querySelector(s));
                const sendReady = _inSendMode && !!(
                    document.querySelector('[data-test-id="send-button-container"].visible') ||
                    document.querySelector('gem-icon-button.send-button[aria-disabled="false"]') ||
                    document.querySelector('gem-icon-button.submit[aria-disabled="false"]') ||
                    document.querySelector('button[aria-label="Send message"]:not([disabled])') ||
                    document.querySelector('button[aria-label*="Send" i]:not([disabled])')
                );

                if (sendReady) {
                    let allResps = Array.from(document.querySelectorAll('model-response, structured-content-container.model-response-text, message-content'));
                    const responses = allResps.filter(el => !allResps.some(parent => parent !== el && parent.contains(el)));

                    // Metadata for reset detection
                    const editor = document.querySelector('.ql-editor');
                    const inputEmpty = !editor || !editor.innerText.trim();
                    const attachmentCount = document.querySelectorAll('button[data-test-id="cancel-button"]').length;

                    // No conversation history visible - Gemini was reset or is a fresh session.
                    if (responses.length === 0) {
                        return {
                            status: "reset",
                            text: "",
                            inputEmpty: inputEmpty,
                            attachmentCount: attachmentCount
                        };
                    }

                    const lastResp = responses[responses.length - 1];
                    const imgEl = lastResp.querySelector('single-image img, img.generated-image, .generated-image img, .image-container img, img[alt*="generated" i], img[src^="blob:"]');
                    const hasImg = !!imgEl && imgEl.src && imgEl.src !== 'about:blank' && (!args.last_seen_src || imgEl.src !== args.last_seen_src);

                    // Filter out the 'XXX said' header portion
                    const contentNode = lastResp.querySelector('.model-response-text') || lastResp.querySelector('.message-content') || lastResp;
                    const respText = contentNode.innerText.trim();

                    if (hasImg) return { status: "success", text: respText };

                    // Structural and Textual refusal detection:
                    // Gemini refused if the response is "complete" (has the complete footer class)
                    // and it has text content but NO image, OR if it matches known refusal text.
                    const completeFooter = lastResp.querySelector('.response-footer.complete');
                    // Refusal keywords loaded from refused_keywords.json via args.refused
                    const refusalKws = args.refused || [];
                    const isTextRefusal = refusalKws.some(kw => respText.toLowerCase().includes(kw.toLowerCase()));

                    if ((completeFooter || isTextRefusal) && respText.length > 0) {
                        return { status: "refused", text: respText };
                    }

                    // Fallback: respText may be empty due to DOM restructuring after generation.
                    // Check body text so a refusal is never silently misclassified as idle/error.
                    if (respText.length === 0 && refusalKws.length > 0) {
                        const bodyText = document.body.innerText;
                        const bodyRefusal = refusalKws.some(kw => bodyText.toLowerCase().includes(kw.toLowerCase()));
                        if (bodyRefusal) {
                            const fallbackText = bodyText.slice(0, 500);
                            return { status: "refused", text: fallbackText };
                        }
                    }

                    // Otherwise treat as stopped or transitional
                    return { status: "idle_no_img", text: respText };
                }

                return { status: "loading", text: "" };
            }''', {"quota": quota_kws, "refused": refused_kws, "last_seen_src": self._e._last_seen_src})

            status = data['status']
            resp_text = data.get('text', '') or ''
            current_time = time.time()

            # If generating but JS returned no text, try to read the loading label
            # via Playwright's native locator (more reliable than evaluate for this)
            if status == "generating" and not resp_text:
                try:
                    locator = self._page.locator('section.processing-state_container--processing').first
                    if await locator.count() > 0:
                        jslog_attr = await locator.get_attribute('jslog') or ""
                        m = re.search(r'\["([^"]+)",0\]', jslog_attr)
                        if m:
                            resp_text = m.group(1)
                except Exception:
                    pass

            # Log any new status text (throttled to avoid noise)
            if resp_text and resp_text != last_logged_text and len(resp_text) > 2:
                flat = " ".join(resp_text.replace('\n', ' ').split())
                self._log(f"Gemini: \"{flat[:200]}\"")
                last_logged_text = resp_text

            if status == "generating":
                if not has_started_generating:
                    has_started_generating = True
                    start_gen_time = current_time

            if status == "success":
                self._log("Response successful: Image detected.")
                return {"status": "success", "message": "Image generated successfully."}
            elif status == "quota_exceeded":
                self._log(f"Response failed: Quota exceeded.")
                return {"status": "error", "message": "Quota exceeded. Please wait before retrying."}
            elif status == "refused":
                flat_text = " ".join(resp_text.replace('\n', ' ').split())
                self._log(f"Response failed (Refused): {flat_text[:300]}")
                return {"status": "refused", "message": f"Gemini refused: {flat_text[:300]}"}
            elif status == "idle_no_img":
                if not idle_start_time:
                    idle_start_time = current_time

                # Only report 'stopped' after sustained generation (4s grace period)
                if has_started_generating and start_gen_time and (current_time - start_gen_time > 4.0):
                    self._log(f"Idle detected after {current_time - start_gen_time:.1f}s of generation.")
                    return {"status": "error", "message": "Stopped or failed to generate image."}
                elif (current_time - idle_start_time) > 8.0:
                    self._log("Sustained idle detected without image. Treating as refusal.")
                    flat_text = " ".join(resp_text.replace('\n', ' ').split())
                    return {"status": "refused", "message": f"Gemini refused (Sustained Idle): {flat_text[:300]}"}
                else:
                    self._log("Idle detected - in grace period, continuing to monitor...")
            elif status == "reset":
                # Gemini reset to initial state (no conversation history)
                if has_started_generating:
                    # Was generating but now page is empty - definitely an unexpected reset
                    self._log("Gemini page was unexpectedly reset during generation.")
                    return {"status": "error", "message": "Gemini was reset unexpectedly."}

                # If we are NOT injecting a new prompt (monitoring a Redo) and we see a reset,
                # it means the Redo triggered a soft-reset. Return immediately to trigger recovery.
                if not text:
                    self._log("Gemini reset detected during Redo monitoring. Triggering recovery...")
                    return {"status": "reset", "message": "Gemini reset during Redo."}

                # Case: Initial Prompt Submission (text has value)
                input_empty = data.get('inputEmpty', True)
                attachment_count = data.get('attachmentCount', 0)

                # Signal 1: Prompt is still in the input box (Submission failed or page reset)
                # Note: We give it a few seconds grace period to clear
                if not input_empty:
                    if not hasattr(self, '_reset_watchdog_start') or self._reset_watchdog_start is None:
                        self._reset_watchdog_start = current_time
                    elif (current_time - self._reset_watchdog_start) > 6.0:
                        self._log("Prompt still remains in input box after 6s. Submission likely failed.")
                        self._reset_watchdog_start = None
                        return {"status": "reset", "message": "Prompt remains in input box."}

                # Signal 2: Missing attachments (Env reset)
                # If we expected attachments but they are gone, it's a reset.
                if expect_attachments and attachment_count == 0:
                    self._log("Expected attachments disappeared. Env reset detected.")
                    return {"status": "reset", "message": "Attachments missing during monitoring."}

                self._log("Waiting for conversation to appear...")
            else:
                # Any other status (generating, success, etc.) clears the watchdog
                self._reset_watchdog_start = None

            await asyncio.sleep(2)

        return {"status": "timeout", "message": "Timed out waiting for image response."}

    # ──────────────────────────────────────────────────────────────────────────
    # send_chat
    # ──────────────────────────────────────────────────────────────────────────
    async def send_chat(self, prompt: str) -> dict:
        """Sends a text prompt to Gemini and waits for the text reply."""
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        # Record how many model responses already exist before we submit
        existing_count = await self._page.evaluate('''() => {
            const all = Array.from(document.querySelectorAll('model-response, structured-content-container.model-response-text, message-content'));
            return all.filter(el => !all.some(p => p !== el && p.contains(el))).length;
        }''')
        self._log(f"send_chat: existing responses before submit = {existing_count}")

        await self.send_prompt(prompt)
        await self._page.keyboard.press("Enter")
        self._log("send_chat: prompt submitted.")

        await asyncio.sleep(0.8)
        still_has_text = await self._page.evaluate('''() => {
            const editor = document.querySelector(".ql-editor, div[aria-label='Enter a prompt for Gemini'], div[aria-label='Enter a prompt here']");
            return !!(editor && editor.innerText && editor.innerText.trim().length > 0);
        }''')
        if still_has_text:
            try:
                btn = self._page.locator(
                    'gem-icon-button.submit button[aria-label="Send message"], '
                    'gem-icon-button.send-button button[aria-label="Send message"], '
                    'button[aria-label="Send message"]'
                ).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    self._log("send_chat: fallback button click sent.")
            except Exception:
                pass

        # Wait for a NEW response to appear and its content to stabilize.
        # Strategy: look for response count > existing_count, then wait until
        # .response-footer.complete is present on the last response OR the
        # text has been unchanged for 3 consecutive 2-second polls (6s stable).
        last_text = ""
        stable_count = 0
        STABLE_NEEDED = 3

        for i in range(120):  # 240s max
            data = await self._page.evaluate(f'''() => {{
                const all = Array.from(document.querySelectorAll('model-response, structured-content-container.model-response-text, message-content'));
                const responses = all.filter(el => !all.some(p => p !== el && p.contains(el)));

                if (responses.length <= {existing_count}) {{
                    return {{ status: 'waiting', text: '' }};
                }}

                const lastResp = responses[responses.length - 1];
                const contentNode = lastResp.querySelector('.model-response-text') ||
                                    lastResp.querySelector('.message-content') || lastResp;
                const cl = contentNode.cloneNode(true);
                cl.querySelectorAll('.cdk-visually-hidden,[aria-hidden="true"]').forEach(function(e){{ e.remove(); }});
                const text = cl.innerText.trim();

                const isComplete = !!lastResp.querySelector('.response-footer.complete');
                return {{ status: 'has_response', text: text, complete: isComplete }};
            }}''')

            status = data.get("status")
            if status == "waiting":
                if i % 5 == 0:
                    self._log(f"send_chat: waiting for new response... (iter {i})")
            elif status == "has_response":
                text = data.get("text", "")
                is_complete = data.get("complete", False)

                if is_complete and text:
                    self._log(f"send_chat: complete footer detected ({len(text)} chars).")
                    return {"status": "success", "text": text}

                if text and text == last_text:
                    stable_count += 1
                    self._log(f"send_chat: text stable {stable_count}/{STABLE_NEEDED} ({len(text)} chars)")
                    if stable_count >= STABLE_NEEDED:
                        return {"status": "success", "text": text}
                else:
                    stable_count = 0

                last_text = text

            await asyncio.sleep(2)

        return {"status": "timeout", "message": "Timed out waiting for text response."}

    # ──────────────────────────────────────────────────────────────────────────
    # redo_response
    # ──────────────────────────────────────────────────────────────────────────
    async def redo_response(self):
        """
        Triggers Gemini's redo (regenerate) action.
        Handles both the single button redo and the menu-based 'Try again' redo.
        """
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        # 1. Scroll to reveal Redo if hidden
        await self._page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
        await asyncio.sleep(0.5)

        # 2. Click redo/refresh button (target the latest turn's button)
        result = await self._page.evaluate('''async () => {
            const findLastBtn = (sel) => {
                const btns = Array.from(document.querySelectorAll(sel));
                return btns.length > 0 ? btns[btns.length - 1] : null;
            };
            const findByText = (txt) => Array.from(document.querySelectorAll('.menu-text, span, button'))
                                            .reverse()
                                            .find(b => b.innerText.toLowerCase().includes(txt));

            const allIcons = Array.from(document.querySelectorAll('mat-icon[data-mat-icon-name="refresh"], mat-icon[fonticon="refresh"]'));
            const refreshIcon = allIcons.length > 0 ? allIcons[allIcons.length - 1] : null;

            let redoBtn = findLastBtn('regenerate-button button') ||
                          findLastBtn('button[aria-label="Redo"]');

            if (!redoBtn && refreshIcon) {
                redoBtn = refreshIcon.closest('button') || refreshIcon.closest('[role="button"]') || refreshIcon.parentElement;
            }

            if (redoBtn) {
                redoBtn.scrollIntoView({behavior: "instant", block: "center"});
                redoBtn.click();

                // Wait briefly for sub-menu if it exists
                await new Promise(r => setTimeout(r, 1000));

                let tryAgain = findByText("try again");
                if (tryAgain) {
                    tryAgain.click();
                    return "REDO_WITH_TRY_AGAIN";
                }
                return "REDO_CLICKED";
            }
            return "NOT_FOUND";
        }''')

        if result != "NOT_FOUND":
            self._log(f"Redo triggered: {result}")
            # Ensure the UI has transitioned to 'generating' before returning
            for _ in range(15):
                await asyncio.sleep(0.5)
                is_gen = await self._page.evaluate('''() => {
                    return !!document.querySelector('mat-progress-bar') ||
                           !!document.querySelector('mat-icon[data-mat-icon-name="stop"]') ||
                           !!document.querySelector('section.processing-state_container--processing');
                }''')
                if is_gen:
                    break
            return {"status": "success", "message": f"Redo action sent ({result})."}
        else:
            self._log("Redo button not found.")
            return {"status": "error", "message": "Redo button not found on page."}

    # ──────────────────────────────────────────────────────────────────────────
    # download_images
    # ──────────────────────────────────────────────────────────────────────────
    async def download_images(self, save_dir, naming_cfg, extra_meta=None):
        """
        Downloads images from the last response and enriches metadata.
        naming_cfg: {prefix, padding, start}
        extra_meta: {prompt, url, upload_path}
        """
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        os.makedirs(save_dir, exist_ok=True)

        # 1. Identify images in last response using top-level filtering (matches success monitor)
        last_resp_handle = await self._page.evaluate_handle('''() => {
            const allResps = Array.from(document.querySelectorAll('model-response, structured-content-container.model-response-text, message-content'));
            const responses = allResps.filter(el => !allResps.some(parent => parent !== el && parent.contains(el)));
            return responses.length > 0 ? responses[responses.length - 1] : null;
        }''')
        last_response = last_resp_handle.as_element()
        if not last_response:
            return {"status": "error", "message": "No response found to download from."}

        valid_imgs = []
        for retry in range(20):  # 20 * 0.5s = 10s max
            imgs = await last_response.query_selector_all('single-image img, img.generated-image, .generated-image img, .image-container img, img[alt*="generated" i], img[src^="blob:"]')
            if not imgs:
                imgs = await last_response.query_selector_all('img')
            valid_imgs = []
            seen_positions = []
            seen_srcs = set()
            for img in imgs:
                # Use evaluate to get complete, naturalWidth and boundingClientRect info
                # so it works robustly in minimized/background/headless states
                img_info = await img.evaluate('''el => {
                    const rect = el.getBoundingClientRect();
                    return {
                        width: rect.width, height: rect.height,
                        x: rect.left + window.scrollX, y: rect.top + window.scrollY,
                        complete: el.complete, naturalW: el.naturalWidth,
                        visible: el.offsetWidth > 0 || el.offsetHeight > 0 || (el.complete && el.naturalWidth > 50)
                    };
                }''')

                if not img_info.get("visible"):
                    continue

                # Check dimensions
                w = img_info.get("width", 0)
                natural_w = img_info.get("naturalW", 0)

                src = await img.get_attribute('src')
                src = src.strip() if src else ""

                # We want large images (e.g. width > 250 or naturalWidth > 250)
                # and must ensure a valid src is present and the image has loaded (naturalWidth > 50)
                if src and (natural_w > 250 or (w > 250 and natural_w > 50)):
                    cx = img_info.get("x", 0) + w / 2
                    cy = img_info.get("y", 0) + img_info.get("height", 0) / 2

                    norm_src = src
                    if "googleusercontent.com" in src and "=" in src:
                        norm_src = src.split("=")[0]

                    is_dup = False
                    if norm_src and not norm_src.startswith("data:"):
                        if norm_src in seen_srcs:
                            is_dup = True

                    if not is_dup:
                        for sx, sy, scx, scy in seen_positions:
                            if (abs(img_info.get("x", 0) - sx) < 15 and abs(img_info.get("y", 0) - sy) < 15) or \
                               (abs(cx - scx) < 15 and abs(cy - scy) < 15):
                                is_dup = True
                                break

                    self._log(f"IMG-DETECTION: src={src[:60]}, w={w:.1f}, natural_w={natural_w}, dup={is_dup}")

                    if not is_dup:
                        valid_imgs.append(img)
                        seen_positions.append((img_info.get("x", 0), img_info.get("y", 0), cx, cy))
                        if norm_src and not norm_src.startswith("data:"):
                            seen_srcs.add(norm_src)

            if valid_imgs:
                break
            await asyncio.sleep(0.5)

        if not valid_imgs:
            return {"status": "ignored", "message": "No valid large images found."}

        self._log(f"Downloading {len(valid_imgs)} images...")

        prefix = naming_cfg.get("prefix", "")
        padding = naming_cfg.get("padding", 2)
        start_idx = naming_cfg.get("start", 1)

        cfg = load_config()
        if cfg.get("track_last_file_num", False):
            max_num = -1
            prefix_escaped = re.escape(prefix)
            pattern = re.compile(rf"^{prefix_escaped}(\d+)\.[a-zA-Z0-9]+$", re.IGNORECASE)
            try:
                for filename in os.listdir(save_dir):
                    match = pattern.match(filename)
                    if match:
                        try:
                            num = int(match.group(1))
                            if num > max_num:
                                max_num = num
                        except ValueError:
                            pass
            except Exception as scan_err:
                self._log(f"Error scanning save_dir: {scan_err}")

            if max_num != -1:
                self._log(f"Auto-track: found max number {max_num} in folder. Next number: {max_num + 1}")
                start_idx = max_num + 1
            else:
                self._log(f"Auto-track: no existing files found. Using start number: {start_idx}")

        from PIL import Image
        import io
        import hashlib
        from processing_utils import save_with_metadata
        seen_hashes = set()

        def get_image_ahash(path):
            try:
                with Image.open(path) as img:
                    small = img.resize((8, 8), Image.Resampling.BILINEAR).convert('L')
                    try:
                        pixels = list(small.getdata())
                    except Exception:
                        pixels = list(small.get_flattened_data())
                    avg = sum(pixels) / 64.0
                    bits = ''.join(['1' if p >= avg else '0' for p in pixels])
                    return int(bits, 2)
            except Exception as e:
                self._log(f"Failed to calculate aHash: {e}")
                return None

        dl_count = 0
        saved_paths = []

        for idx in range(len(valid_imgs)):
            try:
                # ── STEP 1: Wait for image to fully load & render ──
                img_ready = False
                img = None
                img_info = {}
                for _load_wait in range(20):  # 20 * 0.5s = 10s max
                    # Dynamically re-locate the image to avoid stale element (Element is not attached to the DOM)
                    try:
                        last_resp_handle = await self._page.evaluate_handle('''() => {
                            const allResps = Array.from(document.querySelectorAll('model-response, structured-content-container.model-response-text, message-content'));
                            const responses = allResps.filter(el => !allResps.some(parent => parent !== el && parent.contains(el)));
                            return responses.length > 0 ? responses[responses.length - 1] : null;
                        }''')
                        last_response = last_resp_handle.as_element()
                        if last_response:
                            imgs = await last_response.query_selector_all('single-image img, img.generated-image, .generated-image img, .image-container img, img[alt*="generated" i], img[src^="blob:"]')
                            if not imgs:
                                imgs = await last_response.query_selector_all('img')
                            if idx < len(imgs):
                                img = imgs[idx]
                    except Exception as re_locate_err:
                        self._log(f"DL-DIAG: Error re-locating image at index {idx}: {re_locate_err}")

                    if img is None:
                        await asyncio.sleep(0.5)
                        continue

                    img_info = await img.evaluate('''el => {
                        const rect = el.getBoundingClientRect();
                        return {
                            width: rect.width, height: rect.height,
                            complete: el.complete, naturalW: el.naturalWidth
                        };
                    }''')
                    # Support minimized/background windows where getBoundingClientRect() returns 0, but complete and naturalW are valid
                    if (img_info.get("height", 0) > 50 and img_info.get("width", 0) > 50) or (img_info.get("complete") and img_info.get("naturalW", 0) > 50):
                        img_ready = True
                        break
                    await asyncio.sleep(0.5)

                if not img_ready or img is None:
                    h_val = img_info.get('height') if img else None
                    w_val = img_info.get('width') if img else None
                    comp_val = img_info.get('complete') if img else None
                    nat_val = img_info.get('naturalW') if img else None
                    self._log(f"DL-DIAG: Image not ready after 10s (h={h_val}, w={w_val}, complete={comp_val}, naturalW={nat_val}). Skipping.")
                    continue

                # ── STEP 1b: Scroll & Viewport Verification ──
                await img.evaluate('el => el.scrollIntoView({behavior: "instant", block: "center"})')
                await asyncio.sleep(0.5)

                viewport_info = await img.evaluate('''el => {
                    const rect = el.getBoundingClientRect();
                    return {
                        inViewport: rect.width > 0 && rect.height > 0 &&
                                    rect.top >= 0 && rect.left >= 0 &&
                                    rect.bottom <= window.innerHeight && rect.right <= window.innerWidth,
                        imgRect: {t: Math.round(rect.top), l: Math.round(rect.left),
                                  b: Math.round(rect.bottom), r: Math.round(rect.right)},
                        windowSize: {w: window.innerWidth, h: window.innerHeight}
                    };
                }''')
                self._log(f"DL-DIAG: viewport={viewport_info}")

                if not viewport_info.get("inViewport"):
                    await self._page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                    await asyncio.sleep(0.3)
                    await img.evaluate('el => el.scrollIntoView({behavior: "instant", block: "center"})')
                    await asyncio.sleep(0.5)

                # ── STEP 2: Click image to open lightbox/dialog ──
                await img.click(force=True)
                await asyncio.sleep(1.5)

                # ── STEP 3: Diagnose what dialog opened ──
                dialog_diag = await self._page.evaluate('''(imgEl) => {
                    const result = {dialogFound: false, dialogType: "none", dlBtnFound: false,
                                    dlBtnInfo: null, allButtons: [], dialogClasses: ""};

                    // Check for mat-dialog (Pro editing dialog or standard preview)
                    const dialog = document.querySelector('mat-dialog-container');
                    if (dialog) {
                        result.dialogFound = true;
                        const content = dialog.querySelector('mat-dialog-content');
                        result.dialogClasses = content ? content.className : dialog.className;

                        if (content && content.className.includes('trusted-image-dialog')) {
                            result.dialogType = "pro_editing";
                        } else {
                            result.dialogType = "standard";
                        }

                        // Check for dialog image (high-res)
                        const dialogImg = dialog.querySelector('img[data-test-id="trusted-image"], img.generated-image');
                        if (dialogImg) {
                            result.dialogImgSrc = dialogImg.src ? dialogImg.src.substring(0, 80) : "none";
                        }
                    }

                    // Check for cdk-overlay (Angular Material overlay)
                    const overlay = document.querySelector('.cdk-overlay-container .cdk-overlay-pane');
                    if (overlay && !result.dialogFound) {
                        result.dialogFound = true;
                        result.dialogType = "cdk_overlay";
                        result.dialogClasses = overlay.className;
                    }

                    // Scope search for download buttons
                    const searchScope = dialog || overlay || (imgEl ? imgEl.closest('single-image') || imgEl.closest('.image-container') || imgEl.parentElement : document);

                    // Scan mat-icon elements inside the scoped container
                    const matIcons = Array.from(searchScope.querySelectorAll('mat-icon'));
                    const dlIcons = matIcons.filter(i => {
                        const name = i.getAttribute('data-mat-icon-name') || '';
                        const font = i.getAttribute('fonticon') || '';
                        return name === 'download' || font === 'download';
                    });

                    if (dlIcons.length > 0) {
                        result.dlBtnFound = true;
                        const icon = dlIcons[0];
                        const parentBtn = icon.closest('button');
                        result.dlBtnInfo = {
                            tagName: parentBtn ? 'button' : icon.tagName,
                            visible: parentBtn ? (parentBtn.offsetParent !== null) : (icon.offsetParent !== null),
                            disabled: parentBtn ? parentBtn.disabled : false,
                            ariaLabel: parentBtn ? (parentBtn.ariaLabel || '') : '',
                            iconName: icon.getAttribute('data-mat-icon-name') || '',
                            fonticon: icon.getAttribute('fonticon') || '',
                            classes: parentBtn ? parentBtn.className.substring(0, 120) : ''
                        };
                    }

                    // Fallback: scan buttons for Download text in scoped container
                    if (!result.dlBtnFound) {
                        const textBtn = Array.from(searchScope.querySelectorAll('button'))
                            .find(x => (x.ariaLabel || '').toLowerCase().includes('download') ||
                                       (x.title || '').toLowerCase().includes('download') ||
                                       x.innerText.toLowerCase().includes('download'));
                        if (textBtn) {
                            result.dlBtnFound = true;
                            result.dlBtnInfo = {
                                tagName: 'button', visible: textBtn.offsetParent !== null,
                                disabled: textBtn.disabled,
                                ariaLabel: textBtn.ariaLabel || '',
                                innerText: textBtn.innerText.substring(0, 50),
                                classes: textBtn.className.substring(0, 120)
                            };
                        }
                    }

                    // Collect summary of ALL buttons in search scope for debugging
                    const btns = Array.from(searchScope.querySelectorAll('button'));
                    result.allButtons = btns.slice(0, 15).map(b => ({
                        ariaLabel: (b.ariaLabel || '').substring(0, 40),
                        text: b.innerText.substring(0, 30).replace(/\\n/g, ' '),
                        icon: (() => {
                            const mi = b.querySelector('mat-icon');
                            if (!mi) return '';
                            return mi.getAttribute('data-mat-icon-name') ||
                                   mi.getAttribute('fonticon') ||
                                   mi.innerText.substring(0, 20);
                        })(),
                        visible: b.offsetParent !== null,
                        disabled: b.disabled
                    }));

                    return result;
                }''', img)
                self._log(f"DL-DIAG: dialog={dialog_diag}")

                # ── STEP 4: Wait for Download button if not yet visible ──
                dl_btn_found = dialog_diag.get("dlBtnFound", False)

                if not dl_btn_found:
                    # Poll for the button (dialog might still be animating)
                    for _wait in range(12):  # 12 * 0.5s = 6s max
                        dl_btn_found = await self._page.evaluate('''(imgEl) => {
                            const dialog = document.querySelector('mat-dialog-container');
                            const overlay = document.querySelector('.cdk-overlay-container .cdk-overlay-pane');
                            const searchScope = dialog || overlay || (imgEl ? imgEl.closest('single-image') || imgEl.closest('.image-container') || imgEl.parentElement : document);

                            const matIcon = searchScope.querySelector('mat-icon[data-mat-icon-name="download"]');
                            if (matIcon) return true;
                            const fontIcon = searchScope.querySelector('mat-icon[fonticon="download"]');
                            if (fontIcon) return true;
                            const btn = Array.from(searchScope.querySelectorAll('button'))
                                         .find(x => (x.ariaLabel || '').toLowerCase().includes('download') ||
                                                   (x.title || '').toLowerCase().includes('download') ||
                                                   x.innerText.toLowerCase().includes('download'));
                            if (btn) return true;
                            return false;
                        }''', img)
                        if dl_btn_found:
                            self._log(f"DL-DIAG: button appeared after {(_wait+1)*0.5:.1f}s polling")
                            break
                        await asyncio.sleep(0.5)

                # ── STEP 5: Execute download (button path or blob fallback) ──
                if not dl_btn_found:
                    self._log("DL-DIAG: No download button found after polling. Using canvas extraction.")
                    # Wait for the dialog's high-res image to fully load before drawing to canvas.
                    # The dialog img (trusted-image) loads asynchronously after the dialog opens.
                    # We poll until naturalWidth > 512 (full-res), up to 10s.
                    for _hires_wait in range(20):  # 20 * 0.5s = 10s max
                        hires_nw = await self._page.evaluate('''() => {
                            const dialogImg = document.querySelector('img[data-test-id="trusted-image"]') ||
                                              document.querySelector('mat-dialog-container img.generated-image') ||
                                              document.querySelector('mat-dialog-container img');
                            return dialogImg ? dialogImg.naturalWidth : 0;
                        }''')
                        if hires_nw > 512:
                            self._log(f"DL-DIAG: Dialog hi-res image ready (naturalWidth={hires_nw}) after {_hires_wait * 0.5:.1f}s.")
                            break
                        self._log(f"DL-DIAG: Waiting for dialog hi-res image... (naturalWidth={hires_nw}, attempt {_hires_wait+1}/20)")
                        await asyncio.sleep(0.5)
                    else:
                        self._log("DL-DIAG: Dialog hi-res image did not load after 10s. Aborting canvas extraction.")
                        await self._page.keyboard.press("Escape")
                        await asyncio.sleep(0.5)
                        continue

                    # Extract image pixels directly via canvas while dialog is still open
                    img_bytes = await self._page.evaluate('''async (imgEl) => {
                        try {
                            const dialogImg = document.querySelector('img[data-test-id="trusted-image"]') ||
                                              document.querySelector('mat-dialog-container img.generated-image') ||
                                              document.querySelector('mat-dialog-container img') ||
                                              imgEl;
                            if (!dialogImg || !dialogImg.naturalWidth) return null;

                            const canvas = document.createElement('canvas');
                            canvas.width = dialogImg.naturalWidth;
                            canvas.height = dialogImg.naturalHeight;
                            const ctx = canvas.getContext('2d');
                            ctx.drawImage(dialogImg, 0, 0);

                            const blob = await new Promise(r => canvas.toBlob(r, 'image/png'));
                            if (!blob) return null;
                            const buf = await blob.arrayBuffer();
                            return Array.from(new Uint8Array(buf));
                        } catch(e) {
                            return null;
                        }
                    }''', img)

                    await self._page.keyboard.press("Escape")
                    await asyncio.sleep(0.5)

                    if img_bytes:
                        while True:
                            save_name = f"{prefix}{str(start_idx).zfill(padding)}.png"
                            save_path = os.path.join(save_dir, save_name)
                            if not os.path.exists(save_path):
                                break
                            start_idx += 1

                        raw = bytes(img_bytes)
                        self._log(f"DL-DIAG: Canvas extracted {len(raw)} bytes ({len(raw)/1024:.0f}KB)")
                        with Image.open(io.BytesIO(raw)) as pil_img:
                            save_with_metadata(pil_img, pil_img, save_path, extra_meta=extra_meta)

                        # Size validation: file must be >= 1MB, else the dialog hi-res img wasn't ready.
                        file_sz = os.path.getsize(save_path)
                        if file_sz < 1024 * 1024:
                            self._log(f"DL-DIAG: Canvas result still too small ({file_sz/1024:.1f}KB < 1MB). Low-res placeholder captured. Discarding.")
                            try:
                                os.remove(save_path)
                            except Exception as rm_err:
                                self._log(f"Failed to delete small file: {rm_err}")
                            continue

                        # Check PIL average hash to prevent duplicate saving (robust to scaling/canvas rescue)
                        is_pixel_dup = False
                        new_ahash = get_image_ahash(save_path)
                        if new_ahash is not None:
                            for old_ahash in seen_hashes:
                                distance = bin(new_ahash ^ old_ahash).count('1')
                                if distance <= 3:
                                    self._log(f"Duplicate image content detected (aHash={hex(new_ahash)}, distance={distance}). Deleting duplicate: {save_name}")
                                    try:
                                        os.remove(save_path)
                                    except Exception as rm_err:
                                        self._log(f"Failed to remove duplicate file: {rm_err}")
                                    is_pixel_dup = True
                                    break
                            if not is_pixel_dup:
                                seen_hashes.add(new_ahash)

                        if is_pixel_dup:
                            continue

                        saved_paths.append(save_path)
                        start_idx += 1
                        dl_count += 1
                        self._log(f"Saved (canvas fallback): {save_name}")
                        continue
                    else:
                        self._log("DL-DIAG: Canvas extraction returned null. Skipping.")
                        await self._page.keyboard.press("Escape")
                        continue

                # ── Button-based download path using Playwright expect_download ──────────────
                try:
                    self._log("DL-DIAG: Triggering download and waiting for Playwright download event...")
                    async with self._page.expect_download(timeout=90000) as download_info:
                        btn_clicked = await self._page.evaluate('''() => {
                            const dialog = document.querySelector('mat-dialog-container');
                            const overlay = document.querySelector('.cdk-overlay-container .cdk-overlay-pane');
                            const scope = dialog || overlay || document;

                            // Most specific: aria-label="Download full-sized image"
                            let btn = scope.querySelector('button[aria-label="Download full-sized image"]');
                            if (!btn) {
                                // Fallback: closest button to a download mat-icon
                                const icon = scope.querySelector(
                                    'mat-icon[data-mat-icon-name="download"], mat-icon[fonticon="download"]');
                                if (icon) btn = icon.closest('button') || icon;
                            }
                            if (!btn) {
                                btn = Array.from(scope.querySelectorAll('button'))
                                    .find(x => (x.ariaLabel || '').toLowerCase().includes('download') ||
                                               x.innerText.toLowerCase().includes('download'));
                            }
                            if (btn) { btn.click(); return true; }
                            return false;
                        }''')

                        if not btn_clicked:
                            self._log("DL-DIAG: click_result=not_found")
                            raise Exception("Download button vanished between detection and click")

                    download = await download_info.value

                    # Determine save filename
                    while True:
                        save_name = f"{prefix}{str(start_idx).zfill(padding)}.png"
                        save_path = os.path.join(save_dir, save_name)
                        if not os.path.exists(save_path):
                            break
                        start_idx += 1

                    await download.save_as(save_path)
                    self._log(f"DL-DIAG: Native download completed and saved directly: {save_path}")

                    # Read original image into memory to avoid Windows file lock issues when saving metadata
                    with open(save_path, "rb") as f:
                        img_data = f.read()

                    with Image.open(io.BytesIO(img_data)) as original_img:
                        save_with_metadata(original_img, original_img, save_path, extra_meta=extra_meta)

                except Exception as dl_err:
                    self._log(f"DL-DIAG: Native download failed: {dl_err}")
                    raise dl_err

                # Size validation: saved file must be >= 1MB
                file_sz = os.path.getsize(save_path)
                if file_sz < 1024 * 1024:
                    self._log(f"DL-DIAG: Downloaded file too small ({file_sz/1024:.1f}KB < 1MB). Discarding.")
                    try:
                        os.remove(save_path)
                    except Exception:
                        pass
                    await self._page.keyboard.press("Escape")
                    await asyncio.sleep(1.0)
                    continue

                # Check PIL average hash to prevent duplicate saving
                is_pixel_dup = False
                new_ahash = get_image_ahash(save_path)
                if new_ahash is not None:
                    for old_ahash in seen_hashes:
                        distance = bin(new_ahash ^ old_ahash).count('1')
                        if distance <= 3:
                            self._log(f"Duplicate detected (aHash={hex(new_ahash)}, d={distance}). Deleting: {save_name}")
                            try:
                                os.remove(save_path)
                            except Exception:
                                pass
                            is_pixel_dup = True
                            break
                    if not is_pixel_dup:
                        seen_hashes.add(new_ahash)

                if is_pixel_dup:
                    await self._page.keyboard.press("Escape")
                    await asyncio.sleep(1.0)
                    continue

                saved_paths.append(save_path)
                start_idx += 1
                dl_count += 1
                self._log(f"Saved: {save_name}")

                await self._page.keyboard.press("Escape")
                await asyncio.sleep(1.0)
            except Exception as e:
                self._log(f"Download skip: {e}")
                # Blob rescue: the dialog image src is a blob: URL containing the full-res image.
                # fetch(blobUrl) gives us the ORIGINAL bytes without canvas re-encoding loss.
                # Blob URLs are revoked when the dialog closes — must extract before pressing Escape.
                try:
                    self._log("DL-DIAG: Attempting blob fetch rescue (dialog still open)...")
                    img_data = await self._page.evaluate('''async (imgEl) => {
                        try {
                            const dialogImg = document.querySelector('img[data-test-id="trusted-image"]') ||
                                              document.querySelector('mat-dialog-container img.generated-image') ||
                                              document.querySelector('mat-dialog-container img') ||
                                              imgEl;
                            if (!dialogImg) return {error: 'no_img'};
                            const src = dialogImg.src || '';
                            const nw = dialogImg.naturalWidth || 0;
                            const nh = dialogImg.naturalHeight || 0;
                            if (!src) return {error: 'no_src', nw, nh};

                            let fetchError = null;

                            // Strategy A: fetch the blob URL directly (lossless, original bytes)
                            if (src.startsWith('blob:')) {
                                try {
                                    const resp = await fetch(src);
                                    const buf = await resp.arrayBuffer();
                                    return {bytes: Array.from(new Uint8Array(buf)), nw, nh, method: 'blob_fetch'};
                                } catch (fetchErr) {
                                    fetchError = String(fetchErr);
                                    // Blob may have been revoked — fall through to canvas
                                }
                            }

                            // Strategy B: canvas fallback (only if blob fetch failed)
                            if (!nw) return {error: `not_loaded (fetch_err: ${fetchError})`, nw, nh};
                            const canvas = document.createElement('canvas');
                            canvas.width = nw;
                            canvas.height = nh;
                            const ctx = canvas.getContext('2d');
                            ctx.drawImage(dialogImg, 0, 0);
                            const blob = await new Promise(r => canvas.toBlob(r, 'image/png'));
                            if (!blob) return {error: `canvas_null (fetch_err: ${fetchError})`, nw, nh};
                            const buf2 = await blob.arrayBuffer();
                            return {bytes: Array.from(new Uint8Array(buf2)), nw, nh, method: 'canvas', fetch_error: fetchError};
                        } catch(e) {
                            return {error: String(e)};
                        }
                    }''', img)

                    # Close dialog after extraction
                    await self._page.keyboard.press("Escape")
                    await asyncio.sleep(0.5)

                    if img_data and img_data.get('bytes'):
                        nw = img_data.get('nw', 0)
                        method = img_data.get('method', 'unknown')
                        raw = bytes(img_data['bytes'])
                        fetch_err = img_data.get('fetch_error')
                        if fetch_err:
                            self._log(f"DL-DIAG: Blob fetch failed with error: {fetch_err}")
                        self._log(f"DL-DIAG: Rescued {len(raw)} bytes ({len(raw)/1024:.0f}KB) via {method}, naturalWidth={nw}")

                        # Reject if the image dimensions are too small (indicates a thumbnail)
                        if nw < 512:
                            self._log(f"DL-DIAG: Image too small (naturalWidth={nw} < 512). Likely a thumbnail. Skipping.")
                            continue

                        while True:
                            save_name = f"{prefix}{str(start_idx).zfill(padding)}.png"
                            save_path = os.path.join(save_dir, save_name)
                            if not os.path.exists(save_path):
                                break
                            start_idx += 1

                        with Image.open(io.BytesIO(raw)) as pil_img:
                            save_with_metadata(pil_img, pil_img, save_path, extra_meta=extra_meta)

                        # Duplicate check via perceptual hash
                        is_pixel_dup = False
                        new_ahash = get_image_ahash(save_path)
                        if new_ahash is not None:
                            for old_ahash in seen_hashes:
                                distance = bin(new_ahash ^ old_ahash).count('1')
                                if distance <= 3:
                                    self._log(f"Duplicate detected (aHash={hex(new_ahash)}, d={distance}). Deleting: {save_name}")
                                    try:
                                        os.remove(save_path)
                                    except Exception:
                                        pass
                                    is_pixel_dup = True
                                    break
                            if not is_pixel_dup:
                                seen_hashes.add(new_ahash)

                        if is_pixel_dup:
                            continue

                        saved_paths.append(save_path)
                        start_idx += 1
                        dl_count += 1
                        self._log(f"Saved (blob rescue): {save_name}")
                    else:
                        err = img_data.get('error', 'unknown') if img_data else 'null_response'
                        self._log(f"DL-DIAG: Blob rescue failed: {err}")
                except Exception as fallback_err:
                    self._log(f"Blob rescue exception: {fallback_err}")
                    try:
                        await self._page.keyboard.press("Escape")
                    except:
                        pass

        if dl_count == 0:
            return {"status": "ignored", "message": "Images detected but all downloads failed.", "saved_paths": []}

        return {
            "status": "success",
            "count": dl_count,
            "next_start": start_idx,
            "saved_paths": saved_paths
        }

    # ──────────────────────────────────────────────────────────────────────────
    # stop_response
    # ──────────────────────────────────────────────────────────────────────────
    async def stop_response(self):
        """
        Clicks the 'Stop' button (square icon) if it exists.
        Returns immediately without waiting for confirmation to keep the UI responsive.
        """
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        self._log("Attempting to stop response via 'stop' icon...")

        stopped = await self._page.evaluate('''() => {
            const stopIcon = document.querySelector('mat-icon[data-mat-icon-name="stop"]');
            if (stopIcon) {
                const btn = stopIcon.closest('button');
                if (btn) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }''')

        if stopped:
            self._log("Stop command sent successfully.")
            return {"status": "success", "message": "Response stop command triggered."}
        else:
            self._log("Stop icon not found.")
            return {"status": "ignored", "message": "No active 'stop' icon found to click."}

    # ──────────────────────────────────────────────────────────────────────────
    # new_chat
    # ──────────────────────────────────────────────────────────────────────────
    async def new_chat(self, target_url: str = None):
        """
        Clicks the 'New chat' button in the Gemini sidebar.
        If target_url is a Gem URL, it navigates directly instead.
        """
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        # Mark the start of the new session logs (for debug_dump tracking)
        engine_log_path = os.path.join(self._e._data_dir, "engine.log")
        if os.path.exists(engine_log_path):
            self._e._engine_log_last_pos = os.path.getsize(engine_log_path)
        else:
            self._e._engine_log_last_pos = 0

        # 1. Smarter Navigation for Gems
        current_target = target_url
        if not current_target:
            # Try to read from config if not provided using standard utility
            try:
                cfg = load_config()
                current_target = cfg.get("browser_url")
            except:
                pass

        if current_target and "gemini.google.com/gem/" in current_target:
            self._log(f"Gem URL detected: {current_target}")
            await self._e.navigate(current_target)
            await asyncio.sleep(2.0)
            return {"status": "success", "message": "Navigated to Gem URL directly."}

        self._log("Attempting to trigger New Chat via UI...")

        # Try finding the element using a robust set of selectors (handling wrapper tag changes)
        result = await self._page.evaluate('''() => {
            const btn = document.querySelector('[data-test-id="new-chat-button"]') ||
                        document.querySelector('side-nav-action-button[data-test-id="new-chat-button"]') ||
                        document.querySelector('gem-nav-list-item[data-test-id="new-chat-button"]');
            if (btn) {
                // The actual clickable element might be the anchor or button inside
                const link = btn.querySelector('a[aria-label="New chat"]') ||
                             btn.querySelector('a') ||
                             btn.querySelector('button');
                if (link) {
                    link.click();
                    return "CLICKED_INNER_ELEMENT";
                }
                btn.click();
                return "CLICKED_CONTAINER";
            }
            // Fallback: search globally for any anchor or button with "New chat" aria-label
            const fallbackLink = document.querySelector('a[aria-label="New chat"]') ||
                                 document.querySelector('button[aria-label="New chat"]');
            if (fallbackLink) {
                fallbackLink.click();
                return "CLICKED_FALLBACK_LINK";
            }
            return "NOT_FOUND";
        }''')

        if result != "NOT_FOUND":
            self._log(f"New Chat triggered: {result}")
            # Wait for navigation/reset
            await asyncio.sleep(1.0)
            return {"status": "success", "message": f"New Chat triggered ({result})."}
        else:
            self._log("New Chat button not found. Falling back to default URL.")
            await self._e.navigate("https://gemini.google.com/app")
            return {"status": "success", "message": "Navigated to default app as fallback."}

    # ──────────────────────────────────────────────────────────────────────────
    # delete_activity_history
    # ──────────────────────────────────────────────────────────────────────────
    async def delete_activity_history(self, range_name: str = "Last hour"):
        """
        Navigates to the Gemini Activity page and deletes activity based on the specified range.
        range_name: 'Last hour', 'Last day', 'Always'
        """
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        self._log(f"Initiating history deletion: {range_name}")

        try:
            # 1. Direct navigation to the Gemini activity page
            await self._e.navigate("https://myactivity.google.com/product/gemini?utm_source=gemini")
            await asyncio.sleep(2.0)

            # --- [NEW] Pre-deletion: Handle initial warnings, tours, or banners ---
            # These can block the 'Delete' button or other interactions
            pre_dismiss_selectors = [
                'button:has-text("Dismiss")',
                'button:has-text("Got it")',
                'button[aria-label="Dismiss"]',
                '.xPkBGb:has-text("Dismiss")',  # Specific selector for "Safer with Google" banner
                'div[role="dialog"] button:has-text("OK")'
            ]

            # Attempt to clear up to 2 distinct banners/popups
            for _ in range(2):
                dismiss_found = False
                for selector in pre_dismiss_selectors:
                    btn = self._page.locator(selector).first
                    if await btn.is_visible():
                        btn_text = await btn.inner_text() or selector
                        self._log(f"Pre-deletion: Dismissing blocker ({btn_text})...")
                        await btn.click()
                        await asyncio.sleep(1.0)
                        dismiss_found = True
                        break  # Check for next banner if any
                if not dismiss_found:
                    break

            # 2. Find and click the 'Delete' button
            delete_btn = self._page.locator('button[aria-label="Delete"]').first
            if not await delete_btn.is_visible():
                self._log("Delete button not visible. Trying to scroll or force dismiss any overlays...")
                await self._page.mouse.click(10, 10)  # Click corner to lose focus/dismiss lightboxes
                await self._page.keyboard.press("PageDown")
                await asyncio.sleep(1.0)
                if not await delete_btn.is_visible():
                    self._log("Delete button still not visible on activity page.")
                    # Final attempt: click by coordinates if possible or log failure
                    return {"status": "error", "message": "Delete button not visible"}

            await delete_btn.click()
            await asyncio.sleep(1.0)

            # 3. Select the range option
            # Map user-friendly names to selectors/text
            range_map = {
                "Last hour": "Last hour",
                "Last day": "Last day",
                "All time": "Always"
            }
            target_text = range_map.get(range_name, "Last hour")

            # Use a more flexible locator to handle 'Always' vs 'All time' variants
            if range_name == "All time":
                self._log("Searching for 'Always' or 'All time' option...")
                option = self._page.locator('li[role="menuitem"]').filter(has_text=re.compile(r"^(Always|All time)$", re.I)).first
            else:
                option = self._page.locator(f'li[role="menuitem"]:has-text("{target_text}")')

            if not await option.is_visible():
                return {"status": "error", "message": f"Option '{target_text}' not found"}

            await option.click()
            await asyncio.sleep(2.0)

            # 4. Handle Confirmation or "Got it" dialogs
            # These can appear for "Always" range or as a one-time warning/info
            # The USER reported: "Confirm that you would like to delete the following activity -> delete or close"
            dialog_selectors = [
                'button:has-text("Delete")',
                'button:has-text("Got it")',
                'button:has-text("Confirm")',
                'button.VfPpkd-LgbsSe:has-text("Delete")',
                'button.VfPpkd-LgbsSe:has-text("Got it")',
                'button:has-text("Close")'
            ]

            self._log("Checking for post-selection dialogs...")
            for _ in range(4):
                dialog_handled = False

                modal = self._page.locator('div.llhEMd, div.VfPpkd-Sx9N0d').first
                if await modal.is_visible():
                    # Case 1: Detect "No activity" text inside modal
                    no_activity_text = modal.locator('text="You have no selected activity"').first
                    if await no_activity_text.is_visible():
                        close_btn = modal.locator('button:has-text("Close"), button:has-text("Got it")').first
                        if await close_btn.is_visible():
                            self._log("Gemini Activity: No activity found to delete. Closing...")
                            await close_btn.click(force=True)
                            await asyncio.sleep(1.0)
                            return {"status": "success", "message": "No activity to delete"}

                    # Case 2: Detect "Delete" button inside modal
                    modal_delete_btn = modal.locator('button:has-text("Delete"), button[jsname="nUV0Pd"]').first
                    if await modal_delete_btn.is_visible():
                        self._log("Gemini Activity: Deleting confirmed items...")
                        await modal_delete_btn.click(force=True)
                        await asyncio.sleep(2.0)
                        dialog_handled = True
                        continue

                    # Generic "Got it" or "OK" inside modal
                    modal_got_it_btn = modal.locator('button:has-text("Got it"), button:has-text("OK")').first
                    if await modal_got_it_btn.is_visible():
                        await modal_got_it_btn.click(force=True)
                        await asyncio.sleep(1.0)
                        dialog_handled = True
                        continue

                # Fallback to general selectors if modal check didn't catch it
                for selector in dialog_selectors:
                    btn = self._page.locator(selector).first
                    if await btn.is_visible():
                        btn_text = await btn.inner_text() or selector
                        await btn.click(force=True)
                        await asyncio.sleep(1.5)
                        dialog_handled = True
                        break

                if not dialog_handled:
                    break

            # 5. Monitor Snackbar Feedback
            self._log("Monitoring for snackbar feedback...")
            # Locator for snackbar/alert
            snackbar = self._page.locator('[role="alert"], [role="status"]').first

            # Monitoring loop for snackbar messages
            for _ in range(10):  # 10 seconds timeout
                if await snackbar.is_visible():
                    msg = await snackbar.inner_text()
                    if msg:
                        flat_msg = " ".join(msg.strip().split())
                        self._log(f"Gemini Activity: {flat_msg}")

                        # Stop if we see a completion message
                        if any(x in flat_msg.lower() for x in ["deleted", "complete", "removed"]):
                            break
                await asyncio.sleep(1.0)

            return {"status": "success", "message": f"History deletion ({range_name}) completed."}

        except Exception as e:
            self._log(f"Error during history deletion: {e}")
            return {"status": "error", "message": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # get_account_info
    # ──────────────────────────────────────────────────────────────────────────
    async def get_account_info(self):
        """Checks the browser's top-right login status via DOM selectors.
        Returns a dict: {logged_in: bool, account_id: str|None, status: str}
        Based on the proven check_signin.py reference pattern.
        """
        if not self._e.is_running:
            raise Exception("Browser Engine not started")

        # Brief stability wait, then try network idle
        await self._page.wait_for_timeout(200)
        try:
            await self._page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass  # Proceed even if network idle times out

        # Selector 1: Google Account button (logged-in indicator)
        avatar_selectors = 'a[href*="accounts.google.com/SignOut"], [aria-label*="Google Account"], img.mavatar-image, img.gb_n, img[src*="googleusercontent.com/a/"]'
        avatar_locators = self._page.locator(avatar_selectors)

        # Selector 2: Sign-in button (not-logged-in indicator)
        signin_selectors = 'a[href*="accounts.google.com/ServiceLogin"], button:has-text("Sign in"), a:has-text("Sign in")'
        signin_locators = self._page.locator(signin_selectors)

        # Find the first VISIBLE element for avatar
        is_logged_in = False
        target_avatar = None
        count_avatar = await avatar_locators.count()
        for i in range(count_avatar):
            if await avatar_locators.nth(i).is_visible():
                is_logged_in = True
                target_avatar = avatar_locators.nth(i)
                break

        # Find the first VISIBLE element for sign in
        is_not_logged_in = False
        target_signin = None
        count_signin = await signin_locators.count()
        for i in range(count_signin):
            if await signin_locators.nth(i).is_visible():
                is_not_logged_in = True
                target_signin = signin_locators.nth(i)
                break

        if is_logged_in and target_avatar:
            account_id = "Unknown Account"
            try:
                # Traverse up from the element to find an aria-label or title with the email
                aria_label = await target_avatar.evaluate('''el => {
                    let current = el;
                    for (let i = 0; i < 5; i++) {
                        if (!current) break;
                        let label = current.getAttribute('aria-label') || current.getAttribute('title') || current.getAttribute('data-tooltip');
                        if (label && label.includes('@')) return label;
                        if (label && label.toLowerCase().includes('google account')) return label;
                        current = current.parentElement;
                    }
                    return null;
                }''')
                if aria_label:
                    match_email = re.search(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,})\b", aria_label)
                    match_name = re.search(r"Google Account:\s*(.*?)\s*\(", aria_label, re.I)
                    if match_email:
                        account_id = match_email.group(1)
                    elif match_name:
                        account_id = match_name.group(1)
                    else:
                        account_id = aria_label.split(':')[-1].strip()
            except Exception:
                pass

            self._e.automation_status["current_account_id"] = account_id
            return {"logged_in": True, "account_id": account_id, "status": "logged_in"}

        elif is_not_logged_in:
            self._e.automation_status["current_account_id"] = None
            return {"logged_in": False, "account_id": None, "status": "not_logged_in"}

        else:
            # Fallback: check Gemini sidebar conversations list
            chat_list = self._page.locator('div[data-test-id="conversations-list"]').first
            if await chat_list.is_visible():
                account_id = "Unknown (sidebar detected)"
                self._e.automation_status["current_account_id"] = account_id
                return {"logged_in": True, "account_id": account_id, "status": "logged_in"}

            self._e.automation_status["current_account_id"] = None
            return {"logged_in": False, "account_id": None, "status": "unknown"}

    # ──────────────────────────────────────────────────────────────────────────
    # test_connection
    # ──────────────────────────────────────────────────────────────────────────
    async def test_connection(self):
        """Simple test to verify Playwright installation and Gemini connectivity."""
        try:
            await self._e.start()
            status = await self._e.navigate("https://www.google.com")
            print(f"Connection Test: Google returned {status}")
            await self._e.get_screenshot("browser_screen_capture/test_google.png")
            await self._e.stop()
            return True
        except Exception as e:
            print(f"Connection Test Failed: {e}")
            return False
