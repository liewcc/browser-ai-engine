import httpx
import asyncio

class EngineClient:
    def __init__(self, base_url="http://127.0.0.1:8000"):
        self.base_url = base_url

    async def check_health(self):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self.base_url}/health", timeout=0.2)
                return resp.json()
        except:
            return None

    async def get_status(self):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self.base_url}/browser/status", timeout=10.0)
                return resp.json()
        except:
            return {"engine_running": False}

    async def get_engine_logs(self):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self.base_url}/engine/logs", timeout=2.0)
                return resp.json()
        except:
            return {"logs": []}

    async def start_engine(self, persona=None, headless=None):
        payload = persona if persona else {}
        if headless is not None:
            payload["headless"] = headless
            
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/engine/start", json=payload, timeout=60.0)
            return resp.json()

    async def stop_engine(self):
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/engine/stop", timeout=10.0)
            return resp.json()

    async def navigate(self, url):
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/browser/navigate", json={"url": url}, timeout=60.0)
            return resp.json()

    async def get_snapshot(self):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/browser/snapshot", timeout=30.0)
            return resp.json()

    async def get_account_info(self):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/browser/account", timeout=15.0)
            return resp.json()

    async def send_heartbeat(self):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(f"{self.base_url}/engine/heartbeat", timeout=1.0)
                return resp.json()
        except:
            return None

    async def start_registration_mode(self):
        """Opens a headed browser directly against browser_user_data/ for profile registration."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/engine/start_registration", timeout=30.0)
            return resp.json()

    async def stop_registration(self):
        """Closes the registration browser if it is open."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(f"{self.base_url}/engine/stop_registration", timeout=10.0)
                return resp.json()
        except:
            return {"status": "skipped"}

    async def switch_profile(self, use_backup=False):
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/engine/switch_profile", 
                params={"use_backup": use_backup},
                timeout=65.0
            )
            return resp.json()

    async def switch_profile_previous(self):
        """Switches to the previous profile in the lookup list."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/engine/switch_profile_previous",
                timeout=65.0
            )
            return resp.json()

    async def switch_to_profile(self, username: str):
        """Switches directly to the specified username."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/engine/switch_to_profile",
                params={"username": username},
                timeout=65.0
            )
            return resp.json()

    async def send_prompt(self, text):
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/browser/prompt", json={"text": text}, timeout=60.0)
            return resp.json()

    async def attach_files(self, file_paths):
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/browser/attach_files", json=file_paths, timeout=30.0)
            return resp.json()

    async def clear_attachments(self):
        """Clears all attachments from the browser."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/browser/clear_attachments", timeout=30.0)
            return resp.json()

    async def discover_capabilities(self):
        """Discovers available models and tools."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/browser/discover", timeout=60.0)
            return resp.json()

    async def apply_settings(self, model=None, tool=None):
        """Applies model and/or tool settings."""
        async with httpx.AsyncClient() as client:
            payload = {"model": model, "tool": tool}
            resp = await client.post(f"{self.base_url}/browser/apply_settings", json=payload, timeout=60.0)
            return resp.json()

    async def submit_response(self, text=None):
        async with httpx.AsyncClient() as client:
            payload = {"text": text} if text else None
            resp = await client.post(f"{self.base_url}/browser/submit", json=payload, timeout=150.0)
            return resp.json()

    async def stop_response(self):
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/browser/stop", timeout=45.0)
            return resp.json()

    async def redo_response(self):
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/browser/redo", timeout=60.0)
            return resp.json()

    async def new_chat(self):
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/browser/new_chat", timeout=30.0)
            return resp.json()

    async def download_images(self, save_dir, naming, meta):
        async with httpx.AsyncClient() as client:
            payload = {
                "save_dir": save_dir,
                "naming": naming,
                "meta": meta
            }
            resp = await client.post(f"{self.base_url}/browser/download", json=payload, timeout=300.0)
            return resp.json()

    async def process_images(self, paths, save_dir):
        async with httpx.AsyncClient() as client:
            payload = {
                "paths": paths,
                "save_dir": save_dir
            }
            resp = await client.post(f"{self.base_url}/browser/process", json=payload, timeout=300.0)
            return resp.json()

    async def get_gem_title(self):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/browser/gem_title", timeout=15.0)
            return resp.json()

    async def get_gem_info(self):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/browser/gem_info", timeout=15.0)
            return resp.json()

    async def start_automation(self, mode, goal, config):
        async with httpx.AsyncClient() as client:
            payload = {"mode": mode, "goal": goal, "config": config}
            resp = await client.post(f"{self.base_url}/browser/automation/start", json=payload, timeout=30.0)
            return resp.json()

    async def continue_automation(self, mode, goal, config, clear_pending=False):
        async with httpx.AsyncClient() as client:
            payload = {"mode": mode, "goal": goal, "config": config, "clear_pending": clear_pending}
            resp = await client.post(f"{self.base_url}/browser/automation/continue", json=payload, timeout=30.0)
            return resp.json()

    async def stop_automation(self):
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/browser/automation/stop", timeout=30.0)
            return resp.json()

    async def request_new_chat(self):
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/browser/automation/request_new_chat", timeout=10.0)
            return resp.json()

    async def get_automation_stats(self):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self.base_url}/browser/automation/stats", timeout=5.0)
                return resp.json()
        except:
            return {}

    async def clear_engine_logs(self):
        """Clears the physical engine.log file via API."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/engine/clear_logs", timeout=10.0)
            return resp.json()

    async def reset_time_timer(self):
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/engine/reset_time_timer", timeout=5.0)
            return resp.json()
