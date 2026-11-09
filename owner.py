"""Trusted WeChat owner identity from the official ClawBot bind exchange only.

Fail closed: never invent an owner from the bot id, from last inbound, or from env labels.
OWNER_NAME is display-only and is not an allowlist.
"""
import json, os
from config import DATA_DIR

CRED = os.path.join(DATA_DIR, "credentials.json")


class OwnerError(Exception):
    """No verified bind owner available (or send target rejected)."""


def validate_bind_payload(d):
    """Check a bind confirmation dict. Returns (ok, reason). reason is safe to log (no secrets)."""
    if not isinstance(d, dict):
        return False, "credentials not a dict"
    if d.get("status") != "confirmed":
        return False, "bind status is not confirmed"
    uid = (d.get("ilink_user_id") or "").strip()
    bot = (d.get("ilink_bot_id") or "").strip()
    token = (d.get("bot_token") or "").strip()
    if not uid:
        return False, "missing ilink_user_id from bind confirmation"
    if not uid.endswith("@im.wechat"):
        return False, "ilink_user_id is not a WeChat user identity (@im.wechat)"
    if "@im.bot" in uid:
        return False, "ilink_user_id looks like a bot identity"
    if bot and uid == bot:
        return False, "ilink_user_id equals ilink_bot_id"
    if not token:
        return False, "missing bot_token from bind confirmation"
    bot_from_token = token.split(":", 1)[0].strip()
    if bot_from_token and (uid == bot_from_token or uid.startswith(bot_from_token + ":")):
        return False, "ilink_user_id collides with bot token identity"
    if bot and bot_from_token and bot != bot_from_token:
        # Ambiguous bot identity fields — refuse rather than guess.
        return False, "ilink_bot_id disagrees with bot_token prefix"
    return True, ""


def load_credentials(path=None):
    path = path or CRED
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def trusted_owner_id(c=None):
    """Return verified owner WeChat user id, or None (fail closed)."""
    if c is None:
        c = load_credentials()
    if c is None:
        return None
    ok, _ = validate_bind_payload(c)
    if not ok:
        return None
    return (c.get("ilink_user_id") or "").strip() or None


def require_owner(c=None):
    owner = trusted_owner_id(c)
    if not owner:
        raise OwnerError("no verified bind owner (run bind.py; need status=confirmed and ilink_user_id@im.wechat)")
    return owner


def is_owner(user_id, c=None):
    owner = trusted_owner_id(c)
    return bool(owner) and (user_id or "").strip() == owner
