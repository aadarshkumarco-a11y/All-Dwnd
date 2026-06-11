"""
Shared subscription manager for both bots.
Stores data in subscriptions.json in the workspace root.
"""
import json
import time
from pathlib import Path

SUBS_FILE = Path(__file__).parent / "subscriptions.json"
ADMIN_IDS = {7552634255}


def _load() -> dict:
    if SUBS_FILE.exists():
        try:
            return json.loads(SUBS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save(data: dict):
    SUBS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def add_user(user_id: int, days: int | None = None, name: str = "") -> dict:
    """
    Add / renew subscription.
    days=None → permanent access.
    Returns the subscription record.
    """
    data = _load()
    key = str(user_id)
    expires = None if days is None else int(time.time() + days * 86400)
    record = {
        "name": name,
        "added_at": int(time.time()),
        "expires": expires,
    }
    data[key] = record
    _save(data)
    return record


def remove_user(user_id: int) -> bool:
    data = _load()
    key = str(user_id)
    if key in data:
        del data[key]
        _save(data)
        return True
    return False


def is_authorized(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    data = _load()
    sub = data.get(str(user_id))
    if not sub:
        return False
    expires = sub.get("expires")
    if expires is None:
        return True
    return time.time() < expires


def get_user(user_id: int) -> dict | None:
    return _load().get(str(user_id))


def get_all() -> dict:
    return _load()


def remaining_str(sub: dict) -> str:
    """Return human-readable remaining time for a subscription."""
    expires = sub.get("expires")
    if expires is None:
        return "♾️ Permanent"
    left = expires - time.time()
    if left <= 0:
        return "❌ Expired"
    days = int(left // 86400)
    hours = int((left % 86400) // 3600)
    if days > 0:
        return f"⏳ {days}d {hours}h remaining"
    minutes = int((left % 3600) // 60)
    return f"⏳ {hours}h {minutes}m remaining"
