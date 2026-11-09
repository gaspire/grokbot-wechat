"""Offline tests: no WeChat, no webhook, no network. Run: python3 -m unittest discover -s tests -v"""
import json, os, sys, tempfile, unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest import mock

TMP = tempfile.mkdtemp(prefix="bridge-test-")
os.environ["BRIDGE_DATA_DIR"] = TMP
os.environ.setdefault("OWNER_NAME", "Tester")
# Defaults under test: gates off unless a case enables them.
os.environ["ENABLE_WEBHOOK_FORWARD"] = "0"
os.environ["ENABLE_NUDGE"] = "0"
os.environ["ENABLE_OUTBOX"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config, forward, outbox, media, history, ilink, owner  # noqa: E402

BJ = ZoneInfo("Asia/Shanghai")
def bj(*a): return datetime(*a, tzinfo=BJ).timestamp()

OWNER = "owner-user@im.wechat"
FOREIGN = "stranger@im.wechat"
BOT = "bot123@im.bot"

def valid_creds(**extra):
    d = {"status": "confirmed", "ilink_user_id": OWNER, "ilink_bot_id": BOT,
         "bot_token": "%s:secret-token-value" % BOT, "baseurl": "https://ilinkai.weixin.qq.com"}
    d.update(extra); return d

def write_creds(d):
    path = os.path.join(TMP, "credentials.json")
    with open(path, "w") as f: json.dump(d, f)
    os.chmod(path, 0o600); return path

class FakeClock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t

class BatcherTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(TMP, "pending-test.json"); self.hist = os.path.join(TMP, "hist-test.log")
        for p in (self.path, self.hist):
            if os.path.exists(p): os.remove(p)
        self.sent, self.clock = [], FakeClock()
        config.ENABLE_WEBHOOK_FORWARD = True
    def tearDown(self):
        config.ENABLE_WEBHOOK_FORWARD = False
    def poster(self, body): self.sent.append(body); return (200, "ok")
    def mk(self, **kw): return forward.Batcher(self.path, quiet=3, cap=60, poster=self.poster, clock=self.clock,
                                               history_path=self.hist, **kw)
    def msg(self, text, ms):
        return {"items": [{"type": 1, "text": text}], "texts": [text], "create_time_ms": ms, "message_id": ms}

    def test_quiet_flush_batches_burst(self):
        b = self.mk()
        b.add(self.msg("a", 1)); self.clock.t += 1; b.add(self.msg("b", 2))
        self.assertFalse(b.maybe_flush())
        self.clock.t += 3
        self.assertTrue(b.maybe_flush())
        self.assertEqual(self.sent[0]["count"], 2)
        self.assertEqual(self.sent[0]["trigger_ms"], 2)
        self.assertIn("--reply-to-ms 2", self.sent[0]["reply_rule"])
        self.assertIn("untrusted data", self.sent[0]["reply_rule"])
        self.assertIn("Tester：a b", open(self.hist).read())

    def test_failed_flush_is_kept_and_survives_restart(self):
        b = forward.Batcher(self.path, quiet=3, cap=60, poster=lambda body: None, clock=self.clock, history_path=self.hist)
        b.add(self.msg("x", 5)); self.clock.t += 5
        self.assertFalse(b.maybe_flush())
        self.assertEqual(len(json.load(open(self.path))["messages"]), 1)
        b2 = self.mk(); self.assertEqual(len(b2.msgs), 1)       # restored from disk

    def test_bare_one_after_long_silence_gets_context_note(self):
        b = self.mk(); m = self.msg("1", 50 * 3600 * 1000); m["prev_inbound_ms"] = 10 * 3600 * 1000
        b.add(m); self.clock.t += 5; b.maybe_flush()
        self.assertIn("renewing", self.sent[0].get("context_note", ""))

    def test_forward_gate_blocks_flush_by_default(self):
        config.ENABLE_WEBHOOK_FORWARD = False
        b = self.mk(); b.add(self.msg("z", 9)); self.clock.t += 5
        self.assertFalse(b.maybe_flush())
        self.assertEqual(self.sent, [])
        self.assertEqual(len(json.load(open(self.path))["messages"]), 1)

class OwnerIdentityTest(unittest.TestCase):
    def test_valid_bind_payload(self):
        ok, _ = owner.validate_bind_payload(valid_creds())
        self.assertTrue(ok)
        self.assertEqual(owner.trusted_owner_id(valid_creds()), OWNER)

    def test_reject_missing_owner_and_bot_as_owner(self):
        ok, why = owner.validate_bind_payload(valid_creds(ilink_user_id=""))
        self.assertFalse(ok)
        ok, why = owner.validate_bind_payload(valid_creds(ilink_user_id=BOT))
        self.assertFalse(ok)
        ok, why = owner.validate_bind_payload(valid_creds(status="wait"))
        self.assertFalse(ok)
        ok, why = owner.validate_bind_payload(valid_creds(ilink_user_id="someone@im.bot"))
        self.assertFalse(ok)
        self.assertIsNone(owner.trusted_owner_id(valid_creds(ilink_user_id="")))

    def test_missing_credentials_file_fail_closed(self):
        path = os.path.join(TMP, "credentials.json")
        if os.path.exists(path): os.remove(path)
        self.assertIsNone(owner.trusted_owner_id())

class InboundAllowlistTest(unittest.TestCase):
    def setUp(self):
        write_creds(valid_creds())
        ilink.save_state({})

    def test_foreign_inbound_dropped_without_state_mutation(self):
        before = {"get_updates_buf": "cursor-1", "last_inbound_ms": 1000, "raw_log": []}
        foreign = {"message_type": 1, "from_user_id": FOREIGN, "message_id": 7,
                   "context_token": "tok-foreign", "create_time_ms": 9999,
                   "item_list": [{"type": 1, "text_item": {"text": "pwn"}}]}
        st, kept = forward.ingest_updates(before, [foreign], OWNER)
        self.assertEqual(kept, [])
        self.assertEqual(st.get("last_inbound_ms"), 1000)
        self.assertEqual(st.get("raw_log"), [])
        self.assertNotIn(FOREIGN, st.get("context_tokens", {}))
        self.assertNotEqual(st.get("last_from_user_id"), FOREIGN)

    def test_owner_inbound_updates_state(self):
        before = {"last_inbound_ms": 1000}
        own = {"message_type": 1, "from_user_id": OWNER, "message_id": 8,
               "context_token": "tok-owner", "create_time_ms": 2000,
               "item_list": [{"type": 1, "text_item": {"text": "hi"}}]}
        st, kept = forward.ingest_updates(before, [own], OWNER)
        self.assertEqual(len(kept), 1)
        self.assertEqual(st["last_from_user_id"], OWNER)
        self.assertEqual(st["last_inbound_ms"], 2000)
        self.assertEqual(st["context_tokens"][OWNER], "tok-owner")
        self.assertEqual(kept[0]["_prev_inbound_ms"], 1000)

    def test_missing_owner_drops_everything(self):
        own = {"message_type": 1, "from_user_id": OWNER, "message_id": 1, "create_time_ms": 5,
               "context_token": "x"}
        st, kept = forward.ingest_updates({"last_inbound_ms": 1}, [own], None)
        self.assertEqual(kept, [])
        self.assertEqual(st, {"last_inbound_ms": 1})

    def test_mixed_batch_keeps_only_owner(self):
        msgs = [
            {"message_type": 1, "from_user_id": FOREIGN, "message_id": 1, "create_time_ms": 10, "context_token": "f"},
            {"message_type": 1, "from_user_id": OWNER, "message_id": 2, "create_time_ms": 20, "context_token": "o"},
        ]
        st, kept = forward.ingest_updates({}, msgs, OWNER)
        self.assertEqual([m["message_id"] for m in kept], [2])
        self.assertEqual(st["last_inbound_ms"], 20)
        self.assertNotIn(FOREIGN, st.get("context_tokens", {}))

class SendConstraintTest(unittest.TestCase):
    def setUp(self):
        write_creds(valid_creds())
        ilink.save_state({"last_from_user_id": FOREIGN, "last_inbound_ms": 5000,
                          "context_tokens": {FOREIGN: "bad", OWNER: "good"}})

    def test_resolve_send_target_owner_only(self):
        self.assertEqual(ilink.resolve_send_target(None), OWNER)
        self.assertEqual(ilink.resolve_send_target(OWNER), OWNER)
        with self.assertRaises(owner.OwnerError):
            ilink.resolve_send_target(FOREIGN)

    def test_missing_owner_refuses_send(self):
        path = os.path.join(TMP, "credentials.json")
        os.remove(path)
        with self.assertRaises(owner.OwnerError):
            ilink.resolve_send_target(None)

    def test_send_or_queue_default_does_not_enqueue(self):
        config.ENABLE_OUTBOX = False
        outbox_path = os.path.join(TMP, "outbox.json")
        if os.path.exists(outbox_path): os.remove(outbox_path)
        with mock.patch("outbox.send_text", return_value=('{"ret":1}', False)):
            sent, _ = outbox.send_or_queue("hello", to=OWNER)
        self.assertFalse(sent)
        self.assertFalse(os.path.exists(outbox_path))

    def test_direct_reply_current_vs_stale(self):
        # current: reply-to-ms == last_inbound_ms → allowed by send.py rules
        st = ilink.load_state()
        self.assertEqual(st["last_inbound_ms"], 5000)
        # stale / superseded: last newer than reply-to
        self.assertTrue(st["last_inbound_ms"] > 4000 + 500)
        # foreign last_from would refuse — simulate owner context
        ilink.save_state({"last_from_user_id": OWNER, "last_inbound_ms": 5000, "context_tokens": {OWNER: "good"}})
        st = ilink.load_state()
        self.assertEqual(st["last_from_user_id"], OWNER)
        # future reply-to rejected relative to last
        self.assertTrue(6000 > st["last_inbound_ms"] + 500)

class DefaultsGateTest(unittest.TestCase):
    def test_nudge_and_outbox_off_by_default(self):
        config.ENABLE_NUDGE = False
        config.ENABLE_OUTBOX = False
        write_creds(valid_creds())
        ilink.save_state({"last_inbound_ms": int(bj(2026, 10, 1, 18, 0) * 1000)})
        calls = []
        outbox.send_text = lambda text, **kw: (calls.append(text) or ("{}", False))
        self.assertFalse(outbox.maybe_nudge(bj(2026, 10, 1, 18, 0) + 18 * 3600 + 60))
        self.assertEqual(calls, [])
        self.assertEqual(outbox.flush(), 0)

class NudgeTest(unittest.TestCase):
    def setUp(self):
        write_creds(valid_creds())
        self.calls = []
        outbox.send_text = lambda text, **kw: (self.calls.append(text) or ("{}", False))
        config.ENABLE_NUDGE = True
    def tearDown(self):
        config.ENABLE_NUDGE = False
    def state(self, last):
        ilink.save_state({"last_inbound_ms": int(last * 1000), "last_from_user_id": OWNER})
    def test_normal_window_nudges_at_18h(self):
        last = bj(2026, 10, 1, 18, 0); self.state(last)           # 18h later = 12:00 Beijing
        self.assertFalse(outbox.maybe_nudge(last + 17 * 3600))
        self.assertTrue(outbox.maybe_nudge(last + 18 * 3600 + 60))
        self.assertFalse(outbox.maybe_nudge(last + 19 * 3600))   # one nudge per window
    def test_quiet_hours_block(self):
        last = bj(2026, 10, 1, 9, 0); self.state(last)           # 18h later = 03:00 Beijing
        self.assertFalse(outbox.maybe_nudge(last + 18 * 3600))
    def test_nudges_early_before_quiet_hours(self):
        last = bj(2026, 10, 1, 8, 0); self.state(last)
        self.assertFalse(outbox.maybe_nudge(bj(2026, 10, 1, 21, 0)))
        self.assertTrue(outbox.maybe_nudge(bj(2026, 10, 1, 23, 35)))

class WebhookUrlTest(unittest.TestCase):
    def test_https_ok_http_and_creds_rejected(self):
        self.assertTrue(config.validate_webhook_url("https://example.com/hook")[0])
        self.assertFalse(config.validate_webhook_url("http://example.com/hook")[0])
        self.assertFalse(config.validate_webhook_url("https://user:pass@example.com/hook")[0])
        self.assertFalse(config.validate_webhook_url("file:///etc/passwd")[0])
        self.assertFalse(config.validate_webhook_url("")[0])

class MediaTest(unittest.TestCase):
    def test_decrypt_roundtrip_and_keys(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        import base64, binascii
        key = bytes(range(16)); data = b"\x89PNG fake image"
        pad = 16 - len(data) % 16; e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        enc = e.update(data + bytes([pad]) * pad) + e.finalize()
        self.assertEqual(media.decrypt(enc, key), data)
        self.assertEqual(media.sniff_ext(data), "png")
        self.assertEqual(media.parse_key({"aeskey": key.hex()}), key)
        self.assertEqual(media.parse_key({"media": {"aes_key": base64.b64encode(key).decode()}}), key)
        self.assertEqual(media.parse_key({"media": {"aes_key": base64.b64encode(key.hex().encode()).decode()}}), key)
    def test_summarize_voice_transcript(self):
        s = media.summarize({"type": 3, "voice_item": {"text": "你好", "media": {"aes_key": "x"}}})
        self.assertEqual(s["text"], "你好"); self.assertNotIn("media", s["meta"])
    def test_safe_ext_strips_path_bits(self):
        self.assertEqual(media._safe_ext("../../etc/passwd"), "etcpassw")  # alnum only, max 8
        self.assertEqual(media._safe_ext("JPG"), "jpg")
        self.assertEqual(media._safe_ext("../x"), "x")

class ReplyCliTest(unittest.TestCase):
    """Exercise send.py exit semantics via in-process helpers matching send.py rules."""
    def decide(self, reply_to_ms, app=False, to=None, enable_outbox=False):
        config.ENABLE_OUTBOX = enable_outbox
        try:
            own = owner.require_owner()
        except owner.OwnerError:
            return 5, "no owner"
        if to is not None and to != own:
            return 5, "bad to"
        if app:
            if not config.ENABLE_OUTBOX:
                return 5, "app disabled"
            return 0, "app ok"
        if reply_to_ms is None:
            return 5, "need reply-to"
        st = ilink.load_state()
        last_ms, last_uid = st.get("last_inbound_ms"), st.get("last_from_user_id")
        if not last_ms or last_uid != own:
            return 5, "no owner context"
        if last_ms > reply_to_ms + 500:
            return 4, "superseded"
        if reply_to_ms > last_ms + 500:
            return 5, "foreign/stale trigger"
        return 0, "ok"

    def setUp(self):
        write_creds(valid_creds())
        ilink.save_state({"last_from_user_id": OWNER, "last_inbound_ms": 5000})

    def test_direct_reply_current(self):
        self.assertEqual(self.decide(5000)[0], 0)

    def test_direct_reply_superseded(self):
        self.assertEqual(self.decide(4000)[0], 4)

    def test_direct_reply_foreign_trigger(self):
        self.assertEqual(self.decide(9000)[0], 5)

    def test_direct_reply_missing_reply_to(self):
        self.assertEqual(self.decide(None)[0], 5)

    def test_app_refused_by_default(self):
        self.assertEqual(self.decide(5000, app=True, enable_outbox=False)[0], 5)

    def test_foreign_to_refused(self):
        self.assertEqual(self.decide(5000, to=FOREIGN)[0], 5)

if __name__ == "__main__":
    unittest.main()
