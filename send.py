#!/usr/bin/env python3
"""Send a text to the bound WeChat owner. This is what the agent calls to reply.
Usage: python3 send.py "text" [--to USER_ID] [--no-context] [--app] [--reply-to-ms MS]

Direct replies (default): REQUIRE --reply-to-ms matching the current owner inbound batch.
  - missing/foreign/absent owner context → exit 5 (REFUSED)
  - owner sent something newer → exit 4 (SUPERSEDED)
--app: proactive push; refused unless ENABLE_OUTBOX=1. Prefixed with APP_PREFIX.
--to: must be omitted or equal the verified bind owner; otherwise exit 5.

If WeChat refuses and ENABLE_OUTBOX=1, text is queued (exit 3). With ENABLE_OUTBOX=0, failure is not queued.
Exit codes: 0 sent, 3 queued, 4 superseded, 5 refused."""
import argparse, sys
import outbox
import config
from config import APP_PREFIX, OWNER_NAME
from owner import OwnerError, require_owner
from ilink import load_state

p = argparse.ArgumentParser()
p.add_argument("text")
p.add_argument("--to")
p.add_argument("--no-context", action="store_true")
p.add_argument("--app", action="store_true", help="proactive push; requires ENABLE_OUTBOX=1")
p.add_argument("--reply-to-ms", type=int)
a = p.parse_args()

try:
    owner = require_owner()
except OwnerError as e:
    print("REFUSED:", e); sys.exit(5)

if a.to is not None and (a.to or "").strip() != owner:
    print("REFUSED: --to is not the verified bind owner"); sys.exit(5)

if a.app:
    if not config.ENABLE_OUTBOX:
        print("REFUSED: proactive --app sends disabled (ENABLE_OUTBOX=0)"); sys.exit(5)
else:
    # Direct reply: require current owner inbound context + --reply-to-ms
    if a.reply_to_ms is None:
        print("REFUSED: direct reply requires --reply-to-ms <trigger_ms> from the owner webhook batch"); sys.exit(5)
    st = load_state()
    last_ms = st.get("last_inbound_ms")
    last_uid = st.get("last_from_user_id")
    if not last_ms or last_uid != owner:
        print("REFUSED: no current owner inbound context; not sending"); sys.exit(5)
    if last_ms > a.reply_to_ms + 500:
        print("SUPERSEDED: %s sent a newer message; not sending. Stop this run without filing anything." % OWNER_NAME)
        sys.exit(4)
    # Stale/foreign trigger: reply-to far ahead of known owner inbound, or unrelated
    if a.reply_to_ms > last_ms + 500:
        print("REFUSED: --reply-to-ms does not match current owner inbound context"); sys.exit(5)

text = (APP_PREFIX + a.text) if a.app else a.text
try:
    sent, resp = outbox.send_or_queue(text, to=owner, context_token="" if a.no_context else None)
except OwnerError as e:
    print("REFUSED:", e); sys.exit(5)
print("sent:" if sent else ("QUEUED (will auto-send when the user next writes):" if config.ENABLE_OUTBOX else "NOT SENT:"), resp)
sys.exit(0 if sent else (3 if config.ENABLE_OUTBOX else 5))
