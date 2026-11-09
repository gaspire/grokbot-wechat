"""Minimal WeChat ClawBot iLink client.
Protocol notes: https://chihao.li/blog/en/wechat-clawbot-ilink-protocol and the
@tencent-weixin/openclaw-weixin 2.4.9 npm package (headers / base_info mimic that client).

Outbound sends are constrained to the verified bind owner (credentials.json ilink_user_id).
"""
import base64, json, os, random, secrets, urllib.request
from config import DATA_DIR
from owner import OwnerError, require_owner
DIR = DATA_DIR
CRED = os.path.join(DIR, "credentials.json")   # written by bind.py; NEVER commit
STATE = os.path.join(DIR, "state.json")        # cursor + context tokens + recent raw msgs; NEVER commit
BASE_INFO = {"channel_version": "2.4.9", "bot_agent": "OpenClaw"}

def creds():
    return json.load(open(CRED))

def load_state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {}

def save_state(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f: json.dump(s, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600); os.replace(tmp, STATE)

def post(endpoint, body, timeout=15):
    c = creds()
    hdrs = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(str(random.getrandbits(32)).encode()).decode(),
            "iLink-App-Id": "bot", "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 9),
            "Authorization": "Bearer " + c["bot_token"].strip()}
    body = dict(body, base_info=BASE_INFO)
    url = (c.get("baseurl") or "https://ilinkai.weixin.qq.com").rstrip("/") + "/" + endpoint
    r = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), headers=hdrs, method="POST")
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return f.read().decode()

def resolve_send_target(to=None):
    """Return the only allowed recipient (bind owner). Raises OwnerError otherwise."""
    owner = require_owner()
    if to is None:
        return owner
    if (to or "").strip() != owner:
        raise OwnerError("refusing send to non-owner recipient")
    return owner

def send_text(text, to=None, context_token=None):
    """Send one text to the verified bind owner only.

    to must be omitted or equal the bind owner; any other recipient raises OwnerError.
    context_token=None -> use the latest stored token for the owner; "" -> send without one.
    Does not fall back to last_from_user_id (that could be poisoned before hardening).
    """
    owner = resolve_send_target(to)
    st = load_state()
    if context_token is None:
        context_token = st.get("context_tokens", {}).get(owner)
    msg = {"from_user_id": "", "to_user_id": owner, "client_id": "openclaw-weixin-" + secrets.token_hex(8),
           "message_type": 2, "message_state": 2, "item_list": [{"type": 1, "text_item": {"text": text}}]}
    if context_token: msg["context_token"] = context_token
    resp = post("ilink/bot/sendmessage", {"msg": msg})
    # iLink answers HTTP 200 even on refusal; success = no non-zero ret / errcode in the JSON body.
    try:
        r = json.loads(resp) if resp.strip() else {}
        ok = not (isinstance(r, dict) and (r.get("ret") or r.get("errcode")))
    except ValueError:
        ok = False
    if ok:
        try:
            import history; history.append(history.grok_line(text))
        except Exception:
            pass
    return resp, bool(context_token)
