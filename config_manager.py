import json
import os

CONFIG_FILE = 'config.json'

DEFAULT_CONFIG = {
    "api_key": "",
    "target_language": "English",
    "shortcut": "ctrl+space",
    "mode": "toggle",  # can be 'toggle' or 'hold'
    "minimize_to_tray": True,
    "start_minimized": False,
    "run_at_startup": False,
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            for key, value in DEFAULT_CONFIG.items():
                if key not in config:
                    config[key] = value
            return config
    except Exception as e:
        print(f"Error loading config: {e}")
        return DEFAULT_CONFIG.copy()

def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        print(f"Error saving config: {e}")
