import os
from pathlib import Path


def load_dotenv(dotenv_path=None):
    """Load simple KEY=VALUE pairs from .env without adding a dependency."""
    path = Path(dotenv_path or Path(__file__).resolve().parents[2] / ".env")
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv()


def get_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_int(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def get_csv(name, default=None):
    value = os.getenv(name)
    if not value:
        return default or []
    return [item.strip() for item in value.split(",") if item.strip()]


API_URL = os.getenv("TEXT2STATA_API_URL", "https://globalai.vip/v1/chat/completions")
DEFAULT_MODEL = os.getenv("TEXT2STATA_DEFAULT_MODEL", "gpt-3.5-turbo")
STATA_PATH = os.getenv("TEXT2STATA_STATA_PATH")
MAX_UPLOAD_MB = get_int("TEXT2STATA_MAX_UPLOAD_MB", 100)
CORS_ORIGINS = get_csv("TEXT2STATA_CORS_ORIGINS", ["http://127.0.0.1:5000", "http://localhost:5000", "null"])
FLASK_DEBUG = get_bool("TEXT2STATA_FLASK_DEBUG", False)

API_LINES = {
    "line1": os.getenv("TEXT2STATA_API_KEY_LINE1", ""),
    "line2": os.getenv("TEXT2STATA_API_KEY_LINE2", ""),
    "line3": os.getenv("TEXT2STATA_API_KEY_LINE3", ""),
}
