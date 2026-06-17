import os
import json
import copy
import logging

def get_project_root():
    return os.path.dirname(os.path.abspath(os.path.dirname(__file__)))

def get_config_path():
    return os.path.join(get_project_root(), "data", "config.json")

def get_login_lookup_path():
    return os.path.join(get_project_root(), "data", "user_login_lookup.json")

DEFAULT_CONFIG = {
    "show_engine_console": False,
    "heartbeat_timeout": 3600,
    "headless": True,
    "auto_start_browser": True,
    "auto_continue_loop": False,
    "browser_url": "https://gemini.google.com/app",
    "prompt": "",
    "selected_tool": "",
    "selected_model": "",
    "discovery": {
        "available_tools": [],
        "available_models": []
    },
    "automation": {
        "auto_looping": False,
        "mode": "rounds",
        "goal": 1,
        "remove_watermark": True,
        "use_gpu": True,
        "continue_clear_pending": True
    },
    "active_user": None,
    "save_dir": os.path.join(get_project_root(), "gemini_outputs"),
    "name_prefix": "",
    "name_padding": 2,
    "name_start": 1,
    "track_last_file_num": False,
    "startup_redirect": "gemini_setup",
    "quota_full": [],
    "selected_files": [],
    "quota_cooldown_hours": 24,
    "bypass_quota_full": False,
    "fixed_aspect_ratio": "None",
    "prompt_matrix": {
        "enabled": False,
        "items": [
            {"ratio": "16:9 (Landscape)", "target": 5, "current": 0},
            {"ratio": "9:16 (Portrait)", "target": 5, "current": 0},
            {"ratio": "1:1 (Square)", "target": 5, "current": 0},
            {"ratio": "4:3 (Landscape)", "target": 5, "current": 0},
            {"ratio": "3:4 (Portrait)", "target": 5, "current": 0},
            {"ratio": "21:9 (Ultrawide)", "target": 5, "current": 0},
            {"ratio": "3:2 (Landscape)", "target": 5, "current": 0},
            {"ratio": "2:3 (Portrait)", "target": 5, "current": 0}
        ]
    },
    "gemini_api": {
        "enabled": False,
        "api_key": "",
        "model": "gemini-2.5-flash-image"
    },
    "health_view_mode": "Full Loading History (All Events)",
    "system_navigation": "Engine Settings",
    "notifications_enabled": True,
    "notification_sound_enabled": True,
    "close_to_tray": True
}

def load_config():
    """Reads config.json with fallback to defaults and protection against 0-byte reads."""
    config_path = get_config_path()
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    
    if os.path.exists(config_path):
        try:
            # Check size first to avoid race-condition empty reads
            if os.path.getsize(config_path) > 0:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        # Deep merge the 'automation' block to preserve defaults for missing sub-keys
                        if "automation" in data and isinstance(data["automation"], dict):
                            if "automation" not in cfg:
                                cfg["automation"] = {}
                            cfg["automation"].update(data["automation"])
                            # Remove it from data so the top-level update doesn't overwrite the merged version
                            data_copy = copy.deepcopy(data)
                            del data_copy["automation"]
                            cfg.update(data_copy)
                        else:
                            cfg.update(data)
        except Exception as e:
            print(f"[CONFIG] Read Error: {e}")
            # Fallback to DEFAULT_CONFIG is already in cfg
    return cfg

def save_config(updates):
    """Merges updates into current config and writes atomically."""
    current = load_config()
    current.update(updates)
    
    config_path = get_config_path()
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=4, ensure_ascii=False)
        return current
    except Exception as e:
        print(f"[CONFIG] Write Error: {e}")
        return current

def load_login_lookup():
    """Reads user_login_lookup.json with protection against 0-byte reads."""
    lookup_path = get_login_lookup_path()
    if os.path.exists(lookup_path):
        try:
            if os.path.getsize(lookup_path) > 0:
                with open(lookup_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
        except Exception as e:
            print(f"[CONFIG] Lookup Read Error: {e}")
    return []

def get_refused_keywords_path():
    return os.path.join(get_project_root(), "data", "refused_keywords.json")

def get_quota_keywords_path():
    return os.path.join(get_project_root(), "data", "quota_full_keywords.json")

_DEFAULT_REFUSED_KEYWORDS = [
    "但是不能生成那样的图片", "不能生成那样",
    "不能以那种方式", "描绘未成年人",
    "需要我帮你生成这个人的其他图片吗", "需要我帮你生成其他图片吗",
    "我可以为许多内容", "但是这个不行", "要不要试试别的",
    "无法生成", "不能生成", "无法处理这个请求",
    "i can't", "i cannot", "sorry", "apologize", "unable to",
    "language model", "can't help with that", "i'm not able to"
]

_DEFAULT_QUOTA_KEYWORDS = [
    "quota exceeded", "daily limit", "reached your limit",
    "我今天无法为您创建更多图像", "十分抱歉", "一旦您的额度重置"
]

def load_refused_keywords():
    path = get_refused_keywords_path()
    if os.path.exists(path):
        try:
            if os.path.getsize(path) > 0:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
        except Exception as e:
            print(f"[CONFIG] refused_keywords read error: {e}")
    return list(_DEFAULT_REFUSED_KEYWORDS)

def save_refused_keywords(keywords: list):
    path = get_refused_keywords_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(keywords, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[CONFIG] refused_keywords write error: {e}")
        return False

def load_quota_keywords():
    path = get_quota_keywords_path()
    if os.path.exists(path):
        try:
            if os.path.getsize(path) > 0:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
        except Exception as e:
            print(f"[CONFIG] quota_full_keywords read error: {e}")
    return list(_DEFAULT_QUOTA_KEYWORDS)

def save_quota_keywords(keywords: list):
    path = get_quota_keywords_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(keywords, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[CONFIG] quota_full_keywords write error: {e}")
        return False

def save_login_lookup(data):
    """Writes user_login_lookup.json atomically via temp-file + os.replace.
    
    Prevents the engine from reading a partially-written file during
    concurrent UI saves (os.replace is atomic on Windows within the same FS).
    """
    lookup_path = get_login_lookup_path()
    tmp_path = lookup_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(tmp_path, lookup_path)
        return True
    except Exception as e:
        print(f"[CONFIG] Lookup Write Error: {e}")
        # Clean up orphaned tmp file if present
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False
