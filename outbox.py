"""Outbox for app-initiated WeChat sends + keep-alive nudge.

Both queue/flush and nudge are gated by config flags (default OFF):
  ENABLE_OUTBOX — queue failed sends and flush after owner inbound
  ENABLE_NUDGE  — keep-alive renew prompt near the ~24h window end

WeChat iLink only lets the bot message the user for a while (~24h, observed) after the user's
last message. Failed sends are queued only when ENABLE_OUTBOX=1.
"""
import json, os, time, logging
from datetime import datetime
from zoneinfo import ZoneInfo
import config
from config import QUIET_TZ, NUDGE_TEXT, quiet_hours
from ilink import DIR, load_state, save_state, send_text
from owner import OwnerError, trusted_owner_id
BJ = ZoneInfo(QUIET_TZ)

OUTBOX = os.path.join(DIR, "outbox.json")   # queued texts; may contain private content, NEVER commit
NUDGE_AFTER_S = 18 * 3600   # observed: 11h ok, 35h failed; stay well inside
NUDGE_BEFORE_S = 23 * 3600  # past this the nudge itself would likely fail
NUDGE_EARLIEST_S = 12 * 3600  # if the 18-23h window hits quiet hours, nudge early (11h observed ok)
NUDGE_MARGIN_S = 30 * 60      # send this long before quiet hours begin
QUIET_HOURS = quiet_hours()   # fixed zone (QUIET_TZ), independent of the machine TZ
log = logging.getLogger("forward")

def _load():
    try: return json.load(open(OUTBOX))
    except Exception: return []

def _save(q):
    tmp = OUTBOX + ".tmp"
    with open(tmp, "w") as f: json.dump(q, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUTBOX)

def is_ok(resp):
    try:
        r = json.loads(resp) if resp.strip() else {}
        return not (isinstance(r, dict) and (r.get("ret") or r.get("errcode")))
    except ValueError:
        return False

def enqueue(text, to=None):
    if not config.ENABLE_OUTBOX:
        return
    q = _load(); q.append({"text": text, "to": to, "queued_at": time.time()}); _save(q)

def send_or_queue(text, to=None, context_token=None):
    """Try to send to owner; on failure queue only if ENABLE_OUTBOX. Returns (sent, resp)."""
    try:
        resp, _ = send_text(text, to=to, context_token=context_token)
    except OwnerError:
        raise
    except Exception as e:
        resp = "%s: %s" % (type(e).__name__, e)
    if is_ok(resp): return True, resp
    if config.ENABLE_OUTBOX:
        enqueue(text, to); return False, resp
    return False, resp

def flush():
    """Send queued messages in order; stop at first failure. Returns number sent. No-op if disabled."""
    if not config.ENABLE_OUTBOX:
        return 0
    if not trusted_owner_id():
        log.warning("outbox flush skipped: no verified owner"); return 0
    q = _load(); sent = 0
    while q:
        item = q[0]
        try: resp, _ = send_text(item["text"], to=item.get("to"), context_token="")
        except OwnerError as e:
            log.warning("outbox flush dropped item: %s", e); q.pop(0); _save(q); continue
        except Exception as e: resp = "%s: %s" % (type(e).__name__, e)
        if not is_ok(resp):
            log.warning("outbox flush stopped: %s", str(resp)[:200]); break
        q.pop(0); sent += 1; _save(q); time.sleep(1.5)
    if sent: log.info("outbox flushed %d message(s), %d left", sent, len(q))
    return sent

def last_inbound_s(st=None):
    st = st or load_state()
    if st.get("last_inbound_ms"): return st["last_inbound_ms"] / 1000
    ts = [m.get("create_time_ms") for m in st.get("raw_log", []) if m.get("message_type") == 1 and m.get("create_time_ms")]
    return max(ts) / 1000 if ts else None

def latest_ok_s(last):
    """Latest non-quiet moment before the window closes (last + NUDGE_BEFORE_S)."""
    t = last + NUDGE_BEFORE_S
    for _ in range(48 * 4):
        if datetime.fromtimestamp(t, BJ).hour not in QUIET_HOURS: return t
        t -= 15 * 60
    return t

def maybe_nudge(now=None):
    if not config.ENABLE_NUDGE:
        return False
    if not trusted_owner_id():
        return False
    now = now or time.time()
    st = load_state(); last = last_inbound_s(st)
    if not last: return False
    age = now - last
    if st.get("nudged_for") == last: return False
    if not (NUDGE_EARLIEST_S <= age < NUDGE_BEFORE_S): return False
    if datetime.fromtimestamp(now, BJ).hour in QUIET_HOURS: return False
    if age < NUDGE_AFTER_S and now < latest_ok_s(last) - NUDGE_MARGIN_S: return False
    try: resp, _ = send_text(NUDGE_TEXT, context_token="")
    except OwnerError as e:
        log.warning("keepalive nudge refused: %s", e); return False
    except Exception as e: resp = "%s: %s" % (type(e).__name__, e)
    st = load_state(); st["nudged_for"] = last; save_state(st)  # one attempt per window
    log.info("keepalive nudge sent=%s age=%.1fh", is_ok(resp), age / 3600)
    return is_ok(resp)
