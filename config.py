"""Central configuration. Everything personal/secret comes from environment variables or a local `.env`
file (see .env.example). Nothing secret is hard-coded in this repo.

Safety defaults (fail closed / off until operator enables):
  ENABLE_WEBHOOK_FORWARD=0  — do not POST inbound batches to the Grok Bot webhook
  ENABLE_NUDGE=0            — do not send keep-alive nudge
  ENABLE_OUTBOX=0           — do not queue/flush proactive sends; --app pushes refused
"""
import json, os
from urllib.parse import urlparse

CODE_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_dotenv(path):
    """Tiny .env loader (KEY=VALUE per line, # comments). Never overrides variables already in the env."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"": v = v[1:-1]
                os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass

_load_dotenv(os.path.join(CODE_DIR, ".env"))

def _env(name, default=""):
    return os.environ.get(name, default).strip()

def _env_bool(name, default=False):
    raw = _env(name, "1" if default else "0").lower()
    if raw in ("1", "true", "yes", "on"): return True
    if raw in ("0", "false", "no", "off", ""): return False
    return default

# Where runtime files live: credentials.json, state.json, pending.json, outbox.json, history.log, logs.
DATA_DIR = os.path.abspath(os.path.expanduser(_env("BRIDGE_DATA_DIR") or CODE_DIR))
os.makedirs(DATA_DIR, exist_ok=True)

# --- feature gates (default OFF for safe install) ---
ENABLE_WEBHOOK_FORWARD = _env_bool("ENABLE_WEBHOOK_FORWARD", False)
ENABLE_NUDGE = _env_bool("ENABLE_NUDGE", False)
ENABLE_OUTBOX = _env_bool("ENABLE_OUTBOX", False)

# --- inbound: WeChat -> Grok Bot webhook routine ---
WEBHOOK_URL = _env("WECHAT_WEBHOOK_URL")            # the URL of your Grok Bot routine's webhook trigger
SECRETS_FILE = _env("WECHAT_SECRETS_FILE")          # optional JSON file holding WECHAT_WEBHOOK_KEY (any nesting depth 1)

def validate_webhook_url(url):
    """Allow only https webhook destinations without embedded credentials. Returns (ok, reason)."""
    if not url:
        return False, "WECHAT_WEBHOOK_URL is empty"
    try:
        p = urlparse(url)
    except Exception as e:
        return False, "unparseable webhook URL: %s" % e
    if p.scheme != "https":
        return False, "webhook URL must use https"
    if p.username is not None or p.password is not None:
        return False, "webhook URL must not embed credentials"
    if not p.hostname:
        return False, "webhook URL missing host"
    if p.hostname.lower() in ("localhost", "127.0.0.1", "::1") and not _env_bool("WECHAT_WEBHOOK_ALLOW_LOCAL", False):
        return False, "localhost webhook blocked (set WECHAT_WEBHOOK_ALLOW_LOCAL=1 to override)"
    return True, ""

def safe_secrets_path(path):
    """Resolve secrets file path; reject empty / non-files. Does not read contents."""
    if not path:
        return None, "empty path"
    abs_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(abs_path):
        return None, "secrets path is not a file"
    if os.path.islink(abs_path):
        # allow symlink only if the ultimate target is still a regular file
        if not os.path.isfile(os.path.realpath(abs_path)):
            return None, "secrets symlink target is not a file"
    return abs_path, ""

def webhook_key():
    """Bearer key for the webhook. Env WECHAT_WEBHOOK_KEY wins; else look it up in WECHAT_SECRETS_FILE."""
    k = _env("WECHAT_WEBHOOK_KEY")
    if k or not SECRETS_FILE: return k
    path, err = safe_secrets_path(SECRETS_FILE)
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("WECHAT_WEBHOOK_KEY"): return str(d["WECHAT_WEBHOOK_KEY"]).strip()
        for sect in (d.values() if isinstance(d, dict) else []):
            if isinstance(sect, dict) and sect.get("WECHAT_WEBHOOK_KEY"):
                return str(sect["WECHAT_WEBHOOK_KEY"]).strip()
    except Exception:
        pass
    return ""

# Downloaded images / files / voice / video land here; the path is passed to the agent in the webhook payload.
_attach = _env("WECHAT_ATTACH_DIR")
ATTACH_DIR = os.path.abspath(os.path.expanduser(_attach or os.path.join(DATA_DIR, "attachments")))
# Refuse attach dir that escapes via symlink games when the env value itself tries to leave a jail —
# operators set this deliberately; we still normalize and keep writes confined in media.py.

# --- batching (pending.json) ---
BATCH_QUIET_S = float(_env("BATCH_QUIET_S", "3"))    # flush when no new user message for this long
BATCH_CAP_S = float(_env("BATCH_CAP_S", "60"))       # ...or when the oldest pending message is this old

# --- persona / wording ---
OWNER_NAME = _env("OWNER_NAME", "User")              # display label ONLY — not an allowlist
BOT_NAME = _env("BOT_NAME", "grok")                  # speaker label for bot lines in history.log
APP_PREFIX = _env("APP_PREFIX", "[grok bot] ")       # prefix for app-initiated (non-reply) pushes: send.py --app
NUDGE_TEXT = _env("NUDGE_TEXT", APP_PREFIX + "离你上次发消息快一天了，回个 1 续一下，免得之后的提醒发不过来。")

# --- time zones ---
QUIET_TZ = _env("QUIET_TZ", "Asia/Shanghai")         # quiet hours are evaluated in this zone (Beijing by default)
QUIET_START = int(_env("QUIET_START", "0"))          # no keep-alive nudge from QUIET_START:00 ...
QUIET_END = int(_env("QUIET_END", "8"))              # ... until QUIET_END:00 (exclusive)
HISTORY_TZ = _env("HISTORY_TZ", "Asia/Shanghai")     # timestamps written into history.log

def quiet_hours():
    if QUIET_START <= QUIET_END: return set(range(QUIET_START, QUIET_END))
    return set(range(QUIET_START, 24)) | set(range(0, QUIET_END))   # wraps midnight, e.g. 23 -> 7
