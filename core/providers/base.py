from abc import ABC, abstractmethod


class ProviderAdapter(ABC):
    """Abstract interface for web UI providers (Gemini, DeepSeek, etc.)"""

    BASE_URL: str = ""

    def __init__(self, engine):
        self._e = engine

    @property
    def _page(self):
        return self._e._page

    def _log(self, msg, event_type=None):
        self._e._log_debug(msg, event_type)

    @abstractmethod
    async def send_prompt(self, text: str): ...

    @abstractmethod
    async def attach_files(self, file_paths: list): ...

    @abstractmethod
    async def submit_response(self, text=None, expect_attachments=False): ...

    @abstractmethod
    async def redo_response(self): ...

    @abstractmethod
    async def send_chat(self, prompt: str) -> dict: ...

    @abstractmethod
    async def apply_settings(self, model_name=None, tool_name=None): ...

    @abstractmethod
    async def discover_capabilities(self): ...

    @abstractmethod
    async def new_chat(self, target_url=None): ...

    @abstractmethod
    async def dismiss_agreement_popups(self): ...

    @abstractmethod
    async def stop_response(self): ...

    @abstractmethod
    async def clear_attachments(self): ...

    @abstractmethod
    async def download_images(self, save_dir, naming_cfg, extra_meta=None): ...

    @abstractmethod
    async def delete_activity_history(self, range_name="Last hour"): ...

    @abstractmethod
    async def get_gem_title(self) -> dict: ...

    @abstractmethod
    async def get_gem_info(self) -> dict: ...

    @abstractmethod
    async def get_account_info(self): ...

    @abstractmethod
    async def test_connection(self): ...
