import os

# Load from .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    if os.path.exists(".env"):
        with open(".env", "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()

API_BASE = os.getenv("API_BASE", "https://discord.com/api/v9")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "20"))
AUTO_ACCEPT = os.getenv("AUTO_ACCEPT", "True").lower() in ("true", "1", "yes")
LOG_PROGRESS = os.getenv("LOG_PROGRESS", "True").lower() in ("true", "1", "yes")
DEBUG = os.getenv("DEBUG", "True").lower() in ("true", "1", "yes")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
ADMIN_BYPASS_LIMIT = os.getenv("ADMIN_BYPASS_LIMIT", "True").lower() in ("true", "1", "yes")
CHANNEL_ID = int(os.getenv("CHANNEL_ID")) if os.getenv("CHANNEL_ID", "").strip().isdigit() else None

# Global statistics & tracking
ACTIVE_USERS = {}  # maps user_id -> task reference or metadata
TOTAL_COMPLETED = 0
TOTAL_FAILED = 0

# Load from user_limits.json if it exists to preserve stats across restarts
if os.path.exists("user_limits.json"):
    try:
        import json
        with open("user_limits.json", "r", encoding="utf-8") as f:
            limits_data = json.load(f)
            TOTAL_COMPLETED = limits_data.get("total_completed", 0)
            TOTAL_FAILED = limits_data.get("total_failed", 0)
    except Exception:
        pass

# Custom Emojis (configurable via .env, fallback to standard unicode)
EMOJI_LOADING = os.getenv("EMOJI_LOADING", "⚙️")
EMOJI_SUCCESS = os.getenv("EMOJI_SUCCESS", "✅")
EMOJI_FAIL = os.getenv("EMOJI_FAIL", "❌")
EMOJI_ROCKET = os.getenv("EMOJI_ROCKET", "🚀")

# Cấu hình SePay & Thanh Toán MB Bank
SEPAY_API_KEY = os.getenv("SEPAY_API_KEY", "")
BANK_ID = os.getenv("BANK_ID", "MB")
ACCOUNT_NO = os.getenv("ACCOUNT_NO", "2211231106")
ACCOUNT_NAME = os.getenv("ACCOUNT_NAME", "TANG XUAN ANH")

SUPPORTED_TASKS = [
    "WATCH_VIDEO",
    "PLAY_ON_DESKTOP",
    "STREAM_ON_DESKTOP",
    "PLAY_ACTIVITY",
    "WATCH_VIDEO_ON_MOBILE",
]


def update_env_variable(key: str, value: str):
    """Cập nhật biến môi trường trong RAM và lưu vĩnh viễn vào file .env"""
    key = key.strip()
    value = value.strip()
    
    # 1. Cập nhật RAM os.environ
    os.environ[key] = value

    # 2. Cập nhật các biến module tương ứng trong config
    if key == "ADMIN_BYPASS_LIMIT":
        global ADMIN_BYPASS_LIMIT
        ADMIN_BYPASS_LIMIT = value.lower() in ("true", "1", "yes")
    elif key == "POLL_INTERVAL" and value.isdigit():
        global POLL_INTERVAL
        POLL_INTERVAL = int(value)
    elif key == "HEARTBEAT_INTERVAL" and value.isdigit():
        global HEARTBEAT_INTERVAL
        HEARTBEAT_INTERVAL = int(value)
    elif key == "AUTO_ACCEPT":
        global AUTO_ACCEPT
        AUTO_ACCEPT = value.lower() in ("true", "1", "yes")
    elif key == "LOG_PROGRESS":
        global LOG_PROGRESS
        LOG_PROGRESS = value.lower() in ("true", "1", "yes")
    elif key == "DEBUG":
        global DEBUG
        DEBUG = value.lower() in ("true", "1", "yes")

    # 3. Cập nhật file .env
    env_path = ".env"
    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            if k.strip() == key:
                new_lines.append(f"{key}={value}\n")
                found = True
                continue
        new_lines.append(line)

    if not found:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append(f"{key}={value}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

