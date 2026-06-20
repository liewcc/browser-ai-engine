import asyncio
import os
import sys
import time
import json
import traceback
from config_utils import load_config, save_config
from playwright.async_api import async_playwright
from datetime import datetime
from providers.gemini import GeminiProvider

# Fix for Windows asyncio NotImplementedError with subprocesses
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

class BrowserEngine:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.is_running = False
        # Data dir: where browser_user_data/, logs, and state files live.
        # Defaults to the directory of this file; override with BROWSER_ENGINE_DATA_DIR
        # so code and data can live in separate locations (e.g. when code is a git submodule).
        self._data_dir = os.environ.get("BROWSER_ENGINE_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
        self._project_root = os.environ.get("BROWSER_ENGINE_PROJECT_ROOT") or os.path.dirname(self._data_dir)
        self._state_file = os.path.join(self._data_dir, "browser_state.json")
        self._sandbox_dir = None
        self._log_queue = []
        self._log_history = []
        self._stop_automation_event = asyncio.Event()
        self.automation_status = {
            "is_running": False,
            "mode": "rounds",
            "goal": 0,
            "cycles": 0,
            "successes": 0,
            "refusals": 0,
            "resets": 0,
            "pending_refused": 0,
            "pending_resets": 0,
            "start_time": None,
            "initial_user": None
        }
        # Per-image reject rate tracking
        self._reject_log_path = os.path.join(self._data_dir, "reject_stat_log.json")
        self._cycle_start_time = None   # float: time.time() at start of current cycle
        self._pending_refused = 0       # refused count waiting to be attributed to next successful image
        self._pending_resets = 0        # reset count waiting to be attributed to next successful image
        self._automation_needs_new_chat = True # Flag to force New Chat on next cycle
        self._session_lost = False      # Watchdog flag for engine_service to detect logout
        self._watchdog_task = None      # Handle for the background watchdog task
        self._watchdog_log_path = os.path.join(self._data_dir, "watchdog.log")
        # Per-account session stats snapshot: captured when an account becomes active.
        # Stores {"successes": N, "refusals": N, "resets": N} so that per-account deltas
        # can be computed when that account is later switched away.
        self._acct_snapshot = None
        # Registration browser handles (separate from main browser)
        self._reg_playwright = None
        self._reg_context = None
        self._engine_log_last_pos = None
        # Last image src seen before submit (used by submit_response for change detection)
        self._last_seen_src = None
        # Gemini provider delegate
        self._provider = GeminiProvider(self)



    @property
    def current_url(self):
        """Returns the current page URL."""
        if self._page:
            return self._page.url
        return None

    @property
    def browser_pids(self):
        """Returns a list of all browser-related PIDs."""
        pids = []
        try:
            import psutil
            current_proc = psutil.Process(os.getpid())
            for child in current_proc.children(recursive=True):
                try:
                    name = child.name().lower()
                    if "chrome" in name or "chromium" in name:
                        if child.pid not in pids:
                            pids.append(child.pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass
        return pids

    @property
    def browser_pid(self):
        """Returns the main browser PID (first in list)."""
        pids = self.browser_pids
        return pids[0] if pids else None

    async def inject_session_state(self):
        """Inject saved session state from browser_state.json."""
        if not os.path.exists(self._state_file) or not self._context:
            return
        try:
            import json
            with open(self._state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
            if 'cookies' in state:
                await self._context.add_cookies(state['cookies'])
        except Exception as e:
            print(f"Session injection failed: {e}")

    async def save_session_state(self):
        """Safely export current state."""
        if self._context:
            try:
                await self._context.storage_state(path=self._state_file)
            except Exception as e:
                print(f"Session save failed: {e}")

    async def apply_hardcore_stealth(self, page):
        """Manual JS injection for anti-detection and auto-interruption handling."""
        try:
            await page.add_init_script("""
                // Anti-detection
                Object.defineProperty(navigator, 'webdriver', {get: () => False});
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

                // Proactive Dialog Dismissal (MutationObserver)
                const observer = new MutationObserver((mutations) => {
                    for (const mutation of mutations) {
                        for (const node of mutation.addedNodes) {
                            if (node.nodeType === 1) { // Element node
                                // Target the "Agree" button in the MMGen disclaimer dialog
                                const agreeBtn = node.querySelector('button[data-test-id="upload-image-agree-button"]');
                                if (agreeBtn) {
                                    console.log("[GemiPersona] Disclaimer detected. Auto-clicking Agree...");
                                    agreeBtn.click();
                                }
                            }
                        }
                    }
                });
                observer.observe(document.documentElement, { childList: true, subtree: true });
            """)
        except Exception as e:
            print(f"Stealth injection failed: {e}")

    async def _cleanup_sandbox(self):
        """Cleanup junction and sandbox directory."""
        if self._sandbox_dir and os.path.exists(self._sandbox_dir):
            try:
                # Remove junction first (Windows 'rmdir' on junction doesn't delete source)
                junction_path = os.path.join(self._sandbox_dir, "Default")
                if os.path.exists(junction_path):
                    import subprocess
                    subprocess.run(['rmdir', junction_path], shell=True, capture_output=True)
                
                # Small delay to release file handles
                import shutil
                shutil.rmtree(self._sandbox_dir, ignore_errors=True)
            except Exception as e:
                pass  # Sandbox cleanup failed silently
            self._sandbox_dir = None

    async def start(self, headless=True, url="https://gemini.google.com/app", profile_name="Default"):
        """
        Scheme A: Dynamic Sandbox - Creates a junction to the target profile
        and launches Playwright with a unique temporary user data dir.
        """
        self.last_headless = headless
        
        if self.is_running:
            return
        
        # Guard: close any lingering registration browser before starting the main one
        await self.stop_registration()
        
        # 1. Prepare Sandbox
        base_dir = self._data_dir
        source_user_data = os.path.join(base_dir, "browser_user_data")
        
        # Note: _cleanup_sandbox() sets self._sandbox_dir to None, so we must 
        # initialize/re-initialize it AFTER cleanup.
        temp_sandbox_path = os.path.join(base_dir, "browser_session_sandbox")
        if os.path.exists(temp_sandbox_path):
            # Temporarily set it so cleanup knows what to delete
            self._sandbox_dir = temp_sandbox_path
            await self._cleanup_sandbox()
        
        self._sandbox_dir = temp_sandbox_path
        os.makedirs(self._sandbox_dir, exist_ok=True)
        
        # 2. Map Profile
        if profile_name:
            target_profile_path = os.path.join(source_user_data, profile_name)
            sandbox_default = os.path.join(self._sandbox_dir, "Default")
            
            if os.path.exists(target_profile_path):
                import subprocess
                # Create Junction: Playwright will look for 'Default' inside the sandbox
                cmd = f'mklink /J "{sandbox_default}" "{target_profile_path}"'
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                if res.returncode != 0:
                    print(f"[ERROR] Junction failed (Code {res.returncode}): {res.stderr.strip()}")
                else:
                    if os.path.exists(sandbox_default):
                        pass  # Junction verified
                    else:
                        print(f"[ERROR] Junction reported success but path does not exist!")
            else:
                print(f"[ERROR] Source profile path not found: {target_profile_path}")
            
            # Copy root config files
            import shutil
            for f_name in ["Local State", "Variations"]:
                src = os.path.join(source_user_data, f_name)
                if os.path.exists(src):
                    dest = os.path.join(self._sandbox_dir, f_name)
                    shutil.copy2(src, dest)
                    
                    # Force "Default" profile in Local State to match our junction
                    if f_name == "Local State":
                        try:
                            import json
                            with open(dest, "r", encoding="utf-8") as f:
                                state = json.load(f)
                            if "profile" in state:
                                state["profile"]["last_used"] = "Default"
                                state["profile"]["last_active_profiles"] = ["Default"]
                            with open(dest, "w", encoding="utf-8") as f:
                                json.dump(state, f)
                            pass  # Local State patched
                        except Exception as e:
                            print(f"[ERROR] Failed to patch Local State: {e}")
            
            # Explicitly force "Last Profile" file in sandbox root
            try:
                last_profile_path = os.path.join(self._sandbox_dir, "Last Profile")
                with open(last_profile_path, "w", encoding="utf-8") as f:
                    f.write("Default")
                pass  # 'Last Profile' file created
            except Exception as e:
                print(f"[ERROR] Failed to create 'Last Profile': {e}")

        # 3. Launch Playwright
        self._playwright = await async_playwright().start()
        
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        
        target_viewport = {'width': 2560, 'height': 1440} if headless else None
        
        launch_args = [
            "--start-minimized",
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--safebrowsing-disable-download-protection"
        ]
        
        # Use our sandbox as the persistent context root
        launch_dir = self._sandbox_dir if profile_name else source_user_data
        self._user_data_dir = launch_dir  # Stored so download_images() can locate Chrome's download dir
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=launch_dir,
            headless=headless,
            user_agent=user_agent,
            viewport=target_viewport,
            ignore_default_args=["--enable-automation", "--use-mock-keychain"],
            args=launch_args,
            ignore_https_errors=True,
            java_script_enabled=True,
            device_scale_factor=1,
            accept_downloads=True,
            bypass_csp=True
        )
        
        # Removed manual state injection - Playwright Persistent Context 
        # handles this more reliably via the profile folder itself.
        # if headless:
        #    await self.inject_session_state()

        self._page = await self._context.new_page()
        await self.apply_hardcore_stealth(self._page)
        
        # Force minimize for non-headless mode.
        # --start-minimized gets overridden by Playwright's new_page(), so we
        # re-minimize via CDP to keep the headed fallback window invisible.
        if not headless:
            await self._force_minimize_window()
        
        self.is_running = True

    async def _force_minimize_window(self):
        """Use CDP to force the browser window into minimized state.
        
        Called after new_page() in non-headless mode because Playwright's
        page creation overrides Chrome's --start-minimized flag.
        """
        if not self._page:
            return
        try:
            cdp = await self._page.context.new_cdp_session(self._page)
            window_info = await cdp.send("Browser.getWindowForTarget")
            await cdp.send("Browser.setWindowBounds", {
                "windowId": window_info["windowId"],
                "bounds": {"windowState": "minimized"}
            })
        except Exception as e:
            pass  # CDP minimize failed silently

    async def stop(self):
        """Stops the browser session and cleans up sandbox."""
        if not self.is_running:
            return
        
        # Removed manual state save 
        # await self.save_session_state()
        
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
            
        self.is_running = False
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        
        # Final cleanup
        await self._cleanup_sandbox()

    async def start_registration(self):
        """
        Opens a headed browser directly against browser_user_data/ (no sandbox).
        Allows the user to add new Google accounts / Chrome profiles.
        Data is written directly to disk and will be visible to the engine on next start.
        """
        # Close any previous registration browser first
        await self.stop_registration()
        
        base_dir = self._data_dir
        user_data_dir = os.path.join(base_dir, "browser_user_data")
        
        self._reg_playwright = await async_playwright().start()
        self._reg_context = await self._reg_playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            ignore_default_args=["--enable-automation", "--use-mock-keychain"],
            args=["--start-minimized", "--disable-blink-features=AutomationControlled", "--no-sandbox"],
            ignore_https_errors=True,
            bypass_csp=True
        )
        print("[REG] Registration browser started. user_data_dir:", user_data_dir)

    async def stop_registration(self):
        """Closes the registration browser if it is open."""
        if self._reg_context:
            try:
                await self._reg_context.close()
            except Exception as e:
                print(f"[REG] Error closing registration context: {e}")
            self._reg_context = None
        if self._reg_playwright:
            try:
                await self._reg_playwright.stop()
            except Exception as e:
                print(f"[REG] Error stopping registration playwright: {e}")
            self._reg_playwright = None
        print("[REG] Registration browser stopped.")


    async def navigate(self, url):
        """Navigates to a URL using reference-aligned wait state."""
        if not self.is_running:
            raise Exception("Browser Engine not started")
        
        try:
            # Use domcontentloaded as per reference watcher.py (more stable for SPAs)
            response = await self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # PROACTIVE: Check for agreement popups immediately after navigation
            await self.dismiss_agreement_popups()
            await asyncio.sleep(0.5)  # Grace period: let DOM stabilize after popup dismissal
            # Re-minimize after navigation if running in non-headless mode,
            # since goto() can restore a minimized window.
            if not self.last_headless:
                await self._force_minimize_window()
            return response.status if response else 0
        except Exception as e:
            print(f"Navigation warning: {e}")
            return 200 

    async def send_prompt(self, text):
        return await self._provider.send_prompt(text)

    async def attach_files(self, file_paths):
        return await self._provider.attach_files(file_paths)

    def _log_debug(self, msg, event_type=None):
        """Helper to log debug info to engine.log and internal queue."""
        LOG_FILE = os.path.join(self._data_dir, "engine.log")
        timestamp = datetime.now().strftime("[%H:%M:%S]")
        # Standardize prefix for the UI backend logs
        log_msg = f"{timestamp} API>> {msg}"

        # Mirror to stdout so the console / TUI SERVICE LOG shows these too,
        # not just the engine.log file and the in-memory queue.
        try:
            print(log_msg, flush=True)
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, 'encoding', None) or 'utf-8'
            try:
                print(log_msg.encode(enc, errors='backslashreplace').decode(enc), flush=True)
            except Exception:
                pass

        # Add to internal queue for API consumption
        if not hasattr(self, '_log_queue'):
             self._log_queue = []
        self._log_queue.append(log_msg)
        # Keep queue somewhat bounded
        if len(self._log_queue) > 500:
             self._log_queue = self._log_queue[-500:]

        # Add to history buffer for cross-page persistence
        if not hasattr(self, '_log_history'):
             self._log_history = []
        self._log_history.append(log_msg)
        if len(self._log_history) > 500:
             self._log_history = self._log_history[-500:]

        import json
        msg_lower = msg.lower()
        
        if event_type is None:
            event_type = "DEBUG"
            if "--- [auto] running round" in msg_lower:
                event_type = "START"
            elif "response successful" in msg_lower or ("saved:" in msg_lower and ".png" in msg_lower):
                event_type = "SUCCESS"
            elif "response failed (refused)" in msg_lower or "treating as refusal" in msg_lower or "gemini refused" in msg_lower:
                event_type = "REJECT"
            elif (
                "gemini page was unexpectedly reset" in msg_lower 
                or "automation loop encountered an issue" in msg_lower
                or "env reset detected" in msg_lower
                or "reset detected during redo" in msg_lower
                or "reset detected in cycle" in msg_lower
                or "submission likely failed" in msg_lower
                or "reset during redo" in msg_lower
                or "reset unexpectedly" in msg_lower
                or "automation error in cycle" in msg_lower
                or "automation error (recovered)" in msg_lower
            ):
                event_type = "RESET"
            elif "automation manager started" in msg_lower or "automation finished" in msg_lower:
                event_type = "BOUNDARY"
            elif "switched to" in msg_lower:
                event_type = "ACCOUNT_SWITCH"
        
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "round": self.automation_status.get("cycles", 0) + 1,
            "account": (self.automation_status.get("current_account_id") or self.automation_status.get("initial_user") or "unknown").split('@')[0].lower(),
            "event": event_type,
            "message": msg
        }
        
        if "rejectstat: wrote record for" in msg_lower:
            entry["event"] = "REJECT_STAT"
            import re
            stat_match = re.search(r"dur=([\d.]+)s, ref=(\d+), rst=(\d+)", msg)
            if stat_match:
                entry["duration"] = float(stat_match.group(1))
                entry["reject"] = int(stat_match.group(2))
                entry["reset"] = int(stat_match.group(3))
            fname_match = re.search(r"for\s+([^ ]+)\s+\(", msg)
            if fname_match:
                entry["filename"] = fname_match.group(1).strip()
                
        if "saved: " in msg_lower:
            try:
                entry["filename"] = msg.split("Saved: ")[1].strip()
            except:
                pass

        json_line = json.dumps(entry, ensure_ascii=False) + "\n"

        # Write to engine.log (always)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json_line)
        except:
            pass

    def clear_physical_logs(self):
        """Truncates the engine.log file."""
        LOG_FILE = os.path.join(self._data_dir, "engine.log")
        try:
            import json
            entry = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "round": 0,
                "account": "system",
                "event": "LOG_CLEARED",
                "message": "Engine log cleared by user."
            }
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except Exception as e:
            print(f"Failed to clear log: {e}")
            return False

    def _log_watchdog(self, msg, to_ui=False):
        """Helper to log anomalies to watchdog.log and optionally to the UI."""
        timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
        log_entry = f"{timestamp} {msg}\n"
        try:
            with open(self._watchdog_log_path, "a", encoding="utf-8") as f:
                f.write(log_entry)
        except:
            pass
        
        if to_ui:
            self._log_debug(f"WATCHDOG>> {msg}")
            # Ensure the critical record is also printed to console as per "æ­£å¼log" request
            timestamp_now = datetime.now().strftime("[%H:%M:%S]")
            print(f"{timestamp_now} WATCHDOG>> {msg}")

    def get_and_clear_logs(self):
        """Returns all queued logs and clears the queue."""
        if not hasattr(self, '_log_queue'):
             self._log_queue = []
        logs = list(self._log_queue)
        self._log_queue.clear()
        return logs

    def get_log_history(self):
        """Returns the full log history buffer without clearing it."""
        if not hasattr(self, '_log_history'):
             self._log_history = []
        return list(self._log_history)


    def _write_reject_stat(self, filename, duration_sec, refused_count, reset_count):
        """Appends a per-image stat record to reject_stat_log.json."""
        try:
            records = []
            if os.path.exists(self._reject_log_path):
                with open(self._reject_log_path, "r", encoding="utf-8") as f:
                    records = json.load(f)
            image_count = sum(1 for r in records if not str(r.get("filename", "")).startswith("["))
            idx_val = image_count + 1 if not filename.startswith("[") else "-"
            
            records.append({
                "index": idx_val,
                "filename": filename,
                "duration_sec": round(duration_sec, 2),
                "refused_count": refused_count,
                "reset_count": reset_count
            })
            with open(self._reject_log_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2, ensure_ascii=False)
            self._log_debug(f"RejectStat: Wrote record for {filename} (dur={duration_sec:.1f}s, ref={refused_count}, rst={reset_count})")
        except Exception as e:
            self._log_debug(f"RejectStat: Failed to write stat for {filename}: {e}")

    async def discover_capabilities(self):
        return await self._provider.discover_capabilities()

    async def apply_settings(self, model_name=None, tool_name=None, thinking_level=None):
        return await self._provider.apply_settings(model_name, tool_name, thinking_level)

    async def clear_attachments(self):
        return await self._provider.clear_attachments()

    async def dismiss_agreement_popups(self):
        return await self._provider.dismiss_agreement_popups()

    async def get_screenshot(self, output_path=None):
        """Captures a screenshot with reference-aligned stability."""
        if not self.is_running:
            raise Exception("Browser Engine not started")
        
        # Stability waits
        await self._page.wait_for_load_state("load")
        await self._page.wait_for_timeout(2000) 
        
        # Fix for white screen: ensure body is present and visible
        try:
            body_visible = await self._page.is_visible("body")
            if not body_visible:
                await self._page.wait_for_selector("body", state="visible", timeout=10000)
        except Exception:
            pass
        
        if not output_path:
            out_dir = "browser_screen_capture"
            output_path = f"{out_dir}/screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            os.makedirs(out_dir, exist_ok=True)
            
        # Using full_page=True as seen in reference check_signin.py
        await self._page.screenshot(path=output_path, full_page=True)
        return output_path

    async def get_gem_title(self) -> dict:
        return await self._provider.get_gem_title()

    async def get_gem_info(self) -> dict:
        return await self._provider.get_gem_info()

    async def submit_response(self, text=None, expect_attachments=False):
        return await self._provider.submit_response(text, expect_attachments)

    async def send_chat(self, prompt: str, new_conversation: bool = True) -> dict:
        return await self._provider.send_chat(prompt, new_conversation=new_conversation)

    async def get_last_response(self) -> dict:
        return await self._provider.get_last_response()

    async def redo_response(self):
        return await self._provider.redo_response()

    async def download_images(self, save_dir, naming_cfg, extra_meta=None):
        return await self._provider.download_images(save_dir, naming_cfg, extra_meta)

    async def stop_response(self):
        return await self._provider.stop_response()

    async def new_chat(self, target_url: str = None):
        return await self._provider.new_chat(target_url)

    async def delete_activity_history(self, range_name: str = "Last hour"):
        return await self._provider.delete_activity_history(range_name)

    async def stop_automation(self):
        """Signals the automation loop to stop and attempts to stop current page activity."""
        self._stop_automation_event.set()
        self._log_debug("Automation stop signaled. Attempting browser halt...")
        try:
            # Propagate stop to the actual browser button
            await self.stop_response()
        except:
            pass

    async def run_automation_loop(self, settings: dict):
        """
        Main automation loop.
        settings: {mode: 'rounds'|'images', goal: int, config: dict}
        """
        if not self.is_running:
            raise Exception("Browser Engine not started")

        self._stop_automation_event.clear()
        self.automation_status["is_running"] = True
        
        # We NO LONGER reset cycles/successes here because this function is called per-round.
        # Initialization happens in engine_service or via a dedicated reset call.
        if self.automation_status.get("start_time") is None:
            from datetime import datetime
            self.automation_status["start_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            # Reset session lost flag for this run
            self._session_lost = False
            
            # Start Watchdog Task
            cfg = settings.get("config", {})
            if not cfg:
                self._log_debug("ERROR: Missing config in settings.")
                return {"status": "error", "message": "Missing config"}
                
            target_user = cfg.get("active_user")
            if self._watchdog_task is None:
                self._watchdog_task = asyncio.create_task(self._run_account_watchdog(target_user=target_user))

            self.automation_status["is_running"] = True
            self._log_debug(f"--- [AUTO] RUNNING ROUND: {self.automation_status.get('cycles', 0) + 1} ---")
            
            while self.automation_status.get("is_running", False):
                # Proactive Watchdog Check: if previous iteration (or watchdog) flagged session loss
                if getattr(self, "_session_lost", False):
                    self._log_debug("Watchdog: Critical session loss detected. Aborting loop.")
                    return {"status": "quota", "message": "Session lost or account mismatch."}

                if self._stop_automation_event.is_set():
                    break


                # Refresh cycles and stats from status in each iteration
                mode = self.automation_status.get("mode", "rounds")
                goal = self.automation_status.get("goal", 0)
                cycles = self.automation_status.get("cycles", 0)
                successes = self.automation_status.get("successes", 0)

                if mode == "rounds":
                    if cycles >= goal: break
                else: # images
                    if successes >= goal: break

                # 2. Cycle Strategy â€” record start time for this cycle
                if getattr(self, '_cycle_start_time', None) is None:
                    self._cycle_start_time = time.time()
                    self.automation_status["current_cycle_start_ts"] = self._cycle_start_time
                    self.automation_status["inter_cycle_start_ts"] = None  # exit watermark phase
                if getattr(self, '_lc_cycle_start_time', None) is None:
                    self._lc_cycle_start_time = time.time()
                is_initial = (cycles == 0) or getattr(self, "_automation_needs_new_chat", True)
                
                try:
                    # 3. Execution
                    if is_initial:
                        target_url = cfg.get("browser_url")
                        self._log_debug(f"Cycle #{cycles + 1}: Starting Fresh Setup (Navigating to: {target_url or 'New Chat'})...")
                        await self.new_chat(target_url=target_url)
                        if self._stop_automation_event.is_set(): break
                        await asyncio.sleep(2.0)
                        
                        # Live scan capabilities to validate settings on new chat
                        self._log_debug("Performing live discovery scan at new chat...")
                        discovery_res = await self.discover_capabilities()
                        
                        # Reload config to get latest user settings, but merge with settings config to preserve dynamically injected values (e.g. aspect ratio)
                        cfg = {**load_config(), **settings.get("config", {})}
                        
                        sel_model = cfg.get("selected_model")
                        sel_tool = cfg.get("selected_tool")
                        sel_sub_tool = cfg.get("selected_sub_tool")
                        sel_thinking = cfg.get("selected_thinking_level")
                        
                        model_to_apply = sel_model
                        tool_to_apply = sel_sub_tool or sel_tool
                        thinking_to_apply = sel_thinking
                        
                        if discovery_res.get("status") == "success":
                            discovered = discovery_res.get("data", {})
                            models = discovered.get("models", [])
                            main_tools = discovered.get("main_tools", [])
                            sub_tools = discovered.get("sub_tools", {})
                            thinking_levels = discovered.get("thinking_levels", [])
                            
                            # Validate model (only invalidate if list is non-empty, otherwise assume transient error)
                            if sel_model and models:
                                if sel_model not in models:
                                    self._log_debug(f"Selected model '{sel_model}' not found in live scan. Leaving empty.")
                                    model_to_apply = None
                                
                            # Validate thinking level (only invalidate if list is non-empty)
                            if sel_thinking and thinking_levels:
                                if sel_thinking not in thinking_levels:
                                    self._log_debug(f"Selected thinking level '{sel_thinking}' not found in live scan. Leaving empty.")
                                    thinking_to_apply = None
                                
                            # Validate tool & sub-tool
                            if sel_tool and main_tools:
                                if sel_tool in main_tools:
                                    if sel_tool in sub_tools:
                                        # It has a sub-menu
                                        if sel_sub_tool and sub_tools[sel_tool]:
                                            if sel_sub_tool in sub_tools[sel_tool]:
                                                tool_to_apply = sel_sub_tool
                                            else:
                                                self._log_debug(f"Selected sub-tool '{sel_sub_tool}' not found under '{sel_tool}'. Leaving empty.")
                                                tool_to_apply = None
                                        else:
                                            # Sub-tools list is empty, but category is in main_tools. Fallback to category if no sub-tool is valid
                                            tool_to_apply = sel_sub_tool
                                    else:
                                        # Standard tool
                                        tool_to_apply = sel_tool
                                else:
                                    self._log_debug(f"Selected tool '{sel_tool}' not found in live scan. Leaving empty.")
                                    tool_to_apply = None
                        else:
                            self._log_debug(f"Discovery scan failed: {discovery_res.get('message')}. Applying settings directly from config.")
                        
                        await self.apply_settings(
                            model_name=model_to_apply,
                            tool_name=tool_to_apply,
                            thinking_level=thinking_to_apply
                        )
                        if self._stop_automation_event.is_set(): break
                        
                        has_files = bool(cfg.get("selected_files"))
                        if has_files:
                            await self.attach_files(cfg.get("selected_files"))
                        if self._stop_automation_event.is_set(): break
                        
                        resp = await self.submit_response(text=cfg.get("prompt"), expect_attachments=has_files)
                        if self._stop_automation_event.is_set(): break
                        self._automation_needs_new_chat = False
                    else:
                        self._log_debug(f"Cycle #{cycles + 1}: Triggering Redo...")
                        resp = await self.redo_response()
                        if resp and resp.get("status") == "success":
                            resp = await self.submit_response(text=None) 
                        else:
                            # If Redo button not found, check if it's because of a reset
                            self._log_debug(f"Redo trigger failed: {resp.get('message') if resp else 'No response'}")
                            snapshot_data = await self._page.evaluate('''(args) => {
                                const responses = Array.from(document.querySelectorAll('model-response'));
                                if (responses.length === 0) return "reset";
                                return "error";
                            }''')
                            if snapshot_data == "reset":
                                resp = {"status": "reset", "message": "Reset detected during Redo attempt."}

                    # 4. Analyze Final Cycle Result
                    if not resp:
                        self._log_debug("ERROR: No response object after execution.")
                        return {"status": "error", "message": "Empty response"}

                    status = resp.get("status")
                    
                    if status == "success":
                        self.automation_status["cycles"] += 1
                        # NOTE: successes is NOT incremented here yet.
                        # It is only counted AFTER download_images confirms files are on disk.
                        # This prevents the count from inflating when a Reset occurs mid-download.
                        
                        naming = {
                            "prefix": cfg.get("name_prefix", ""), 
                            "padding": cfg.get("name_padding", 2), 
                            "start": cfg.get("name_start", 1)
                        }
                        
                        # Safety for join
                        selected_files = cfg.get("selected_files") or []
                        meta = {
                            "aspect_ratio": cfg.get("aspect_ratio", ""),
                            # Use clean prompt (without "Aspect Ratio: ..." prefix) for metadata
                            "prompt": cfg.get("prompt_clean", cfg.get("prompt", "")), 
                            "url": self.current_url or "", 
                            "upload_path": ", ".join(selected_files) if isinstance(selected_files, list) else str(selected_files)
                        }

                        # In "modify image" mode the UI stores the reference image's original
                        # PNG metadata in image_ref_source_meta.  Use those values so the newly
                        # downloaded image inherits the correct provenance (prompt/url/upload_path
                        # from the original reference image, not the ephemeral prefix prompt).
                        ref_source_meta = cfg.get("image_ref_source_meta")
                        if ref_source_meta and isinstance(ref_source_meta, dict):
                            for _k in ("aspect_ratio", "prompt", "url", "upload_path"):
                                if ref_source_meta.get(_k):
                                    if _k == "prompt":
                                        import re
                                        meta[_k] = re.sub(r"^Aspect Ratio:.*?\n\n", "", ref_source_meta[_k], flags=re.DOTALL)
                                    else:
                                        meta[_k] = ref_source_meta[_k]
                        
                        dl_resp = await self.download_images(cfg.get("save_dir"), naming, meta)
                        saved_paths = []
                        if dl_resp and dl_resp.get("status") == "success":
                            saved_paths = dl_resp.get("saved_paths", [])
                            
                        if saved_paths:
                            new_start = dl_resp.get("next_start", cfg.get("name_start"))
                            cfg["name_start"] = new_start
                            self._update_config_start(new_start)
                            
                            self.automation_status["successes"] += 1
                            self.automation_status["cycles"] += 1
                            
                            # Write per-image reject stat record
                            cycle_end = time.time()
                            cycle_dur = cycle_end - self._cycle_start_time if self._cycle_start_time else 0
                            for i, sp in enumerate(saved_paths):
                                self._write_reject_stat(
                                    filename=os.path.basename(sp),
                                    duration_sec=cycle_dur / max(len(saved_paths), 1),
                                    refused_count=self._pending_refused if i == 0 else 0,
                                    reset_count=self._pending_resets if i == 0 else 0
                                )
                            # Snapshot pending counters BEFORE zeroing
                            cycle_refused_snap = getattr(self, '_pending_refused', 0)
                            cycle_resets_snap  = getattr(self, '_pending_resets', 0)
                            lc_cycle_refused_snap = getattr(self, '_lc_pending_refused', 0)
                            lc_cycle_resets_snap = getattr(self, '_lc_pending_resets', 0)
                            
                            lc_cycle_end = time.time()
                            lc_cycle_dur = lc_cycle_end - self._lc_cycle_start_time if getattr(self, '_lc_cycle_start_time', None) else 0

                            # Reset global pending counters and mark cycle end cleanly.
                            self._pending_refused = 0
                            self._pending_resets = 0
                            self.automation_status["pending_refused"] = 0
                            self.automation_status["pending_resets"] = 0
                            self._cycle_start_time = None
                            self.automation_status["current_cycle_start_ts"] = None
                            # Mark the inter-cycle phase start (watermark / post-processing period)
                            self.automation_status["inter_cycle_start_ts"] = time.time()
                            
                            # Reset loop control pending counters.
                            self._lc_pending_refused = 0
                            self._lc_pending_resets = 0
                            self._lc_cycle_start_time = None
                        else:
                            # Download failed (e.g. Reset mid-download). Do NOT count as success.
                            self._log_debug("Download failed after image detected. Success NOT counted. Forcing New Chat.")
                            self.automation_status["resets"] += 1
                            self._pending_resets = getattr(self, '_pending_resets', 0) + 1
                            self.automation_status["pending_resets"] = self._pending_resets
                            self._lc_pending_resets = getattr(self, '_lc_pending_resets', 0) + 1
                            self._automation_needs_new_chat = True
                            
                            cycle_refused_snap = 0
                            cycle_resets_snap  = 0
                            lc_cycle_refused_snap = 0
                            lc_cycle_resets_snap = 0
                            cycle_dur          = 0
                            lc_cycle_dur       = 0
                        
                        # Cycle complete â€” expose cycle stats for loop-control threshold check
                        return {
                            "status": "success",
                            "saved_paths": saved_paths,
                            "cycle_duration_sec": cycle_dur,
                            "cycle_refused": cycle_refused_snap,
                            "cycle_resets":  cycle_resets_snap,
                            "lc_cycle_duration_sec": lc_cycle_dur,
                            "lc_cycle_refused": lc_cycle_refused_snap,
                            "lc_cycle_resets": lc_cycle_resets_snap,
                        }
                        
                    elif status == "refused":
                        self.automation_status["cycles"] += 1
                        self.automation_status["refusals"] += 1
                        self._pending_refused = getattr(self, '_pending_refused', 0) + 1
                        self.automation_status["pending_refused"] = self._pending_refused
                        self._lc_pending_refused = getattr(self, '_lc_pending_refused', 0) + 1
                        return {"status": "refused"}
                        
                    elif status == "reset":
                        self.automation_status["resets"] += 1
                        self.automation_status["cycles"] += 1
                        self._pending_resets = getattr(self, '_pending_resets', 0) + 1
                        self.automation_status["pending_resets"] = self._pending_resets
                        self._lc_pending_resets = getattr(self, '_lc_pending_resets', 0) + 1
                        self._log_debug(f"Reset detected in Cycle #{self.automation_status['cycles']}. Counting and forcing New Chat.")
                        self._automation_needs_new_chat = True
                        return {"status": "reset"}
                        
                    elif status in ["error", "timeout"]:
                        if status == "error" and "quota" in str(resp.get("message", "")).lower():
                            self._log_debug("QUOTA EXCEEDED.")
                            await self.stop()
                            # NOTE: do NOT set automation_status["is_running"] = False here.
                            # automation_manager is still alive doing account-switching; setting
                            # is_running=False while the browser restarts creates a race where
                            # the UI's auto-continue fires and spawns a second manager.
                            self._automation_needs_new_chat = True
                            return {"status": "quota", "message": "Quota reached."}
                        else:
                            self._log_debug(f"Automation loop encountered an issue: {resp.get('message')}")
                            self.automation_status["cycles"] += 1
                            self.automation_status["resets"] += 1
                            self._pending_resets += 1
                            self.automation_status["pending_resets"] = self._pending_resets
                            self._lc_pending_resets = getattr(self, '_lc_pending_resets', 0) + 1
                            self._automation_needs_new_chat = True
                            return {"status": status, "message": resp.get("message", "Unknown issue occurred")}

                    elif status == "stopped":
                        # Stop signal received during submit_response.
                        # Break immediately to avoid re-iterating the while loop with is_initial=True,
                        # which would duplicate setup steps (new_chat, apply_settings) and
                        # potentially re-download the same image from the previous response.
                        break

                    await asyncio.sleep(2)
                    

                except Exception as e:
                    import traceback
                    tb = traceback.format_exc()
                    self._log_debug(f"Automation Error in Cycle #{self.automation_status['cycles']+1}:\n{tb}")
                    # Treat as a recoverable reset instead of breaking the entire loop.
                    # This allows engine_service to continue with the next round or switch accounts.
                    self.automation_status["cycles"] += 1
                    self.automation_status["resets"] += 1
                    self._pending_resets = getattr(self, '_pending_resets', 0) + 1
                    self.automation_status["pending_resets"] = self._pending_resets
                    self._lc_pending_resets = getattr(self, '_lc_pending_resets', 0) + 1
                    self._automation_needs_new_chat = True
                    self._log_debug("Recoverable error â€” will retry with New Chat on next round.")
                    return {"status": "reset", "message": f"Automation error (recovered): {e}"}

            # NOTE: We NO LONGER clear _cycle_start_time here...
            self.automation_status["is_running"] = False
            self._log_debug(f"Automation Finished. Final Stats: {self.automation_status}")
            
            # --- FINAL EXIT RECORDING REMOVED FROM HERE ---
            # Now handled by engine_service.py's finally block for better session-wide accuracy.
            
            final_status = "finished"
            if getattr(self, "_session_lost", False):
                final_status = "quota"
            elif self._stop_automation_event.is_set():
                final_status = "stopped"
                
            return {"status": final_status, "stats": self.automation_status}
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self._log_debug(f"CRITICAL CRASH in run_automation_loop:\n{tb}")
            self.automation_status["is_running"] = False
            return {"status": "error", "message": str(e)}
        finally:
            # Lifecycle: Ensure watchdog is killed when automation loop ends
            if self._watchdog_task:
                # Silently cancel the watchdog - no log needed for routine teardown
                self._watchdog_task.cancel()
                try:
                    await self._watchdog_task
                except asyncio.CancelledError:
                    pass
                self._watchdog_task = None

    async def _run_account_watchdog(self, target_user: str = None):
        """
        Independent background task to periodically verify login status.
        Runs until _stop_automation_event is set.
        """
        # Fully silent start - anomalies only are logged
        try:
            # Initial cooldown to let first-page navigation settle.
            # Read from config; default 20s to cover Gem URL load + model/tool apply.
            try:
                _cfg = load_config()
                initial_delay = _cfg.get("watchdog_initial_delay", 20)
            except Exception:
                initial_delay = 20
            await asyncio.sleep(initial_delay)
            
            while not self._stop_automation_event.is_set():
                if not self.is_running or not self._page:
                    break
                
                try:
                    # Non-invasive account check
                    acc = await self.get_account_info()
                    
                    # 1. Detection: Not Logged In
                    if not acc.get("logged_in"):
                        self._log_watchdog("CRITICAL - Session lost (Guest detected).", to_ui=True)
                        self._session_lost = True
                        self._stop_automation_event.set()
                        break
                    
                    # 2. Detection: Account Mismatch (if target_user provided as email)
                    current_acc = acc.get("account_id")
                    if target_user and "@" in target_user and current_acc:
                        if target_user.lower() != current_acc.lower() and current_acc != "Unknown Account":
                            self._log_watchdog(f"CRITICAL - Account mismatch! Expected {target_user}, found {current_acc}.", to_ui=True)
                            self._session_lost = True
                            self._stop_automation_event.set()
                            break

                except Exception as e:
                    self._log_watchdog(f"Anomaly: Check failed ({e}). Retrying in 30s...")

                # Periodic check interval
                await asyncio.sleep(45)
                
        except asyncio.CancelledError:
            pass # Clean exit
        except Exception as e:
            self._log_watchdog(f"Critical Watchdog Internal Error: {e}")
        # No finally log - fully silent on normal end

    def _update_config_start(self, next_start):
        """Helper to persist the next available start number using config_utils."""
        try:
            save_config({"name_start": next_start})
            self._log_debug(f"Persistence: next start number updated to {next_start}.")
        except Exception as e:
            self._log_debug(f"Persistence Error: Failed to update config: {e}")

    async def get_account_info(self):
        return await self._provider.get_account_info()

    async def debug_dump(self, action_name):
        """Dumps a window of engine.log + current page HTML into data/debug.log.

        Each call captures only the engine.log lines written SINCE the previous
        debug_dump call, so every section in debug.log corresponds exactly to
        what the engine was doing during that action (new chat / submit / redo).

        On 'new chat': overwrites debug.log (new session) and resets the read
        position to the current end of engine.log, so only future entries are
        captured.
        """
        try:
            import os
            import json
            from datetime import datetime

            # 1. Check if debug logging is enabled in config
            config_path = os.path.join(self._project_root, "data", "config.json")
            if not os.path.exists(config_path):
                return
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not cfg.get("debug_logging_enabled", False):
                return

            engine_log_path = os.path.join(self._data_dir, "engine.log")
            debug_log_path  = os.path.join(self._project_root, "data", "debug.log")
            os.makedirs(os.path.dirname(debug_log_path), exist_ok=True)

            is_new_chat = (action_name == "new chat")
            write_mode  = "w" if is_new_chat else "a"

            # 2. Read ONLY the new engine.log lines since the last dump.
            #    _engine_log_last_pos tracks the byte offset after the previous read.
            if is_new_chat:
                # On new chat: read logs generated since self._engine_log_last_pos was set in new_chat()
                last_pos = getattr(self, '_engine_log_last_pos', None)
                if last_pos is None:
                    if os.path.exists(engine_log_path):
                        last_pos = os.path.getsize(engine_log_path)
                    else:
                        last_pos = 0
                new_log_lines = ""
                if os.path.exists(engine_log_path):
                    try:
                        with open(engine_log_path, "r", encoding="utf-8", errors="replace") as lf:
                            lf.seek(last_pos)
                            new_log_lines = lf.read()
                            self._engine_log_last_pos = lf.tell()
                    except Exception as le:
                        new_log_lines = f"Error reading engine log: {le}"

            else:
                last_pos = getattr(self, '_engine_log_last_pos', 0)
                new_log_lines = ""
                if os.path.exists(engine_log_path):
                    try:
                        with open(engine_log_path, "r", encoding="utf-8", errors="replace") as lf:
                            lf.seek(last_pos)
                            new_log_lines = lf.read()
                            self._engine_log_last_pos = lf.tell()
                    except Exception as le:
                        new_log_lines = f"Error reading engine log: {le}"

            # 3. Get the page HTML
            page_content = ""
            if self._page:
                try:
                    page_content = await self._page.content()
                except Exception as pe:
                    page_content = f"Error retrieving page content: {pe}"
            else:
                page_content = "Browser not started or page not available."

            # 4. Write to debug.log
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            header = (
                "\n" + "="*80 +
                f"\nDEBUG DUMP: Action '{action_name}' at {timestamp}\n" +
                "="*80 + "\n"
            )

            with open(debug_log_path, write_mode, encoding="utf-8") as df:
                df.write(header)
                if new_log_lines:
                    df.write("--- ENGINE LOG (this action) ---\n")
                    df.write(new_log_lines)
                    df.write("\n")
                df.write(f"--- BROWSER PAGE HTML ({action_name}) ---\n")
                df.write(page_content)
                df.write("\n")

            self._log_debug(f"Debug logging: Dumped DOM and logs for '{action_name}' to data/debug.log (mode={write_mode})")
        except Exception as e:
            if hasattr(self, '_log_debug'):
                self._log_debug(f"Debug logging failed: {e}")



    async def test_connection(self):
        return await self._provider.test_connection()

if __name__ == "__main__":
    # Test script
    engine = BrowserEngine()
    asyncio.run(engine.test_connection())