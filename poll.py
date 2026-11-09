#!/usr/bin/env python3
"""Debug tool: fetch updates once (long-poll ~35s). Saves cursor + owner context_token to state.json.
WARNING: do NOT run this while forward.py is running. getupdates is cursor-based; whatever poll.py
consumes is gone for forward.py (the messages never reach the agent).
Non-owner inbound is dropped before state mutation (same policy as forward.py).
Usage: python3 poll.py [--loop]"""
import json, sys, socket
from ilink import post, load_state, save_state
from owner import trusted_owner_id

def once():
    st = load_state()
    try:
        raw = post("ilink/bot/getupdates", {"get_updates_buf": st.get("get_updates_buf", "")}, timeout=40)
    except (socket.timeout, TimeoutError):
        print("(timeout, no messages)"); return []
    r = json.loads(raw)
    if r.get("ret", 0) != 0: print("ERROR:", raw); return []
    if r.get("get_updates_buf"): st["get_updates_buf"] = r["get_updates_buf"]
    msgs = r.get("msgs") or []
    owner = trusted_owner_id()
    kept = []
    for m in msgs:
        texts = [(i.get("text_item") or i.get("voice_item") or {}).get("text") for i in m.get("item_list") or []]
        print(json.dumps({"from_user_id": m.get("from_user_id"), "message_type": m.get("message_type"),
                          "texts": texts, "context_token": "(redacted)" if m.get("context_token") else None,
                          "create_time_ms": m.get("create_time_ms")}, ensure_ascii=False))
        if m.get("message_type") != 1:
            continue
        uid = (m.get("from_user_id") or "").strip()
        if not owner or uid != owner:
            print("(dropped non-owner or missing owner)", uid or "?")
            continue
        st["last_from_user_id"] = owner
        if m.get("context_token"):
            st.setdefault("context_tokens", {})[owner] = m["context_token"]
        st["last_inbound_ms"] = max(st.get("last_inbound_ms") or 0, m.get("create_time_ms") or 0)
        kept.append(m)
    if kept:
        st.setdefault("raw_log", []); st["raw_log"] = (st["raw_log"] + kept)[-20:]
    save_state(st)
    if not msgs: print("(no messages)")
    return kept

if __name__ == "__main__":
    if "--loop" in sys.argv:
        while True: once()
    else: once()
