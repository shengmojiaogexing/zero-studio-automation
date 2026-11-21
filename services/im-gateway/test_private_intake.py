"""Regression test: private-chat intake must not loop, and must not ignore images."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "services" / "im-gateway"))

from adapters.base import IncomingMessage  # noqa: E402
import brain as brain_mod  # noqa: E402
from conversation_store import ConversationStore  # noqa: E402


class FakeAdapter:
    def __init__(self):
        self.sent_texts = []
        self.sent_images = []

    async def send_text(self, target_id, text, chat_type="private"):
        self.sent_texts.append(text)
        return {"ok": True}

    async def send_image(self, target_id, image, chat_type="private"):
        self.sent_images.append(image)
        return {"ok": True}


def make_brain(llm_replies, db_path=None, context_ttl_sec=86400):
    """llm_replies: dicts returned in order, one per LLM call."""
    br = brain_mod.StudioBrain.__new__(brain_mod.StudioBrain)
    if db_path is None:
        br._test_tmp = tempfile.TemporaryDirectory()
        db_path = Path(br._test_tmp.name) / "test_memory.db"
    br.conversation_store = ConversationStore(
        Path(db_path), context_ttl_sec=context_ttl_sec, cleanup_interval_sec=30
    )
    br._session_locks = {}
    queue = list(llm_replies)

    class LLM:
        async def chat_json(self, system_prompt, user_prompt):
            self.last_prompt = user_prompt
            if queue:
                return queue.pop(0)
            return {"is_photography_related": True, "extracted": {}, "replies": ["ok"]}

    br.llm = LLM()
    br.sessions = {}
    br.adapters = {"wechat": FakeAdapter()}
    br.local_gatekeeper = type("G", (), {"enabled": False})()
    br._override_threshold = None
    br.internal_api_key = "k"
    br.booking_api_url = "http://127.0.0.1:3000"
    br.get_active_rules = lambda: {
        "private_only_reply_related": True,
        "private_system_prompt": "",
        "keywords": [],
    }

    async def no_booking(platform, user_id):
        return None

    br._lookup_booking_by_session = no_booking
    return br


class FakeHTTPResponse:
    def __init__(self, status_code=201, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeHTTPClient:
    """替掉 httpx.AsyncClient：下单/拉二维码都不走真实网络。"""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        return FakeHTTPResponse(201, {"booking_id": 1, "gallery_token": "tok123"})

    async def get(self, url, headers=None, params=None):
        png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489")

        class R(FakeHTTPResponse):
            pass

        r = R(200, {})
        r.content = png
        return r


def msg(text="", images=None):
    return IncomingMessage(
        platform="wechat", user_id="u1", text=text, chat_type="private",
        nickname="client", images=images or [], group_id=None,
    )


async def test_image_not_ignored():
    br = make_brain([
        {"is_photography_related": False, "extracted": {}, "replies": []},
        {"is_photography_related": True, "extracted": {}, "replies": ["ok"]},
    ])
    res = await br.handle_private_message(msg(text="", images=["file:///tmp/qr.png"]))
    assert res["action"] != "IGNORED_UNRELATED_PRIVATE", f"image msg must not be silenced: {res}"
    print("[PASS] 1) image/QR message is not silenced")


async def test_no_loop_after_repeat_ask():
    br = make_brain([{"is_photography_related": True, "extracted": {}, "replies": []}] * 10)
    actions = []
    for _ in range(5):
        res = await br.handle_private_message(msg(text="done"))
        actions.append((res["action"], res.get("asked_field"), res.get("asked_count")))

    # 追问计数不应超过 2 次（第3次起停止索要）
    ask_counts = [a[2] for a in actions if a[0] == "ASK_MISSING_SLOTS"]
    assert max(ask_counts) <= 2, f"field asked too many times: {actions}"

    # 关键：真正「问信息」的句子不应重复出现 3 次以上
    texts = br.adapters["wechat"].sent_texts
    asking = [t for t in texts if "老大说下" in t]
    assert len(asking) <= 2, f"repeated intake questions: {asking}"

    # 超限后应引导客户去网站自助，而不是继续纠缠
    assert any("网站" in t for t in texts), f"should fall back to website guidance: {texts}"
    print(f"[PASS] 2) no infinite loop, ask sequence: {actions}")


async def test_known_fields_not_reasked():
    br = make_brain([{"is_photography_related": True,
                      "extracted": {"cn": "Sakura", "character": "Miku"},
                      "replies": ["ok"]}] * 4)
    await br.handle_private_message(msg(text="cn is Sakura, shooting Miku"))
    captured = {}
    orig = br.llm.chat_json

    async def spy(system_prompt, user_prompt):
        captured["prompt"] = json.loads(user_prompt)
        return await orig(system_prompt, user_prompt)

    br.llm.chat_json = spy
    await br.handle_private_message(msg(text="then?"))
    known = captured["prompt"].get("known_fields", {})
    assert known.get("cn") == "Sakura" and known.get("character") == "Miku", known
    assert "cn" not in captured["prompt"]["missing_fields"]
    print(f"[PASS] 3) known fields passed to model and not re-asked: {known}")


async def test_bubbles_short():
    br = make_brain([{"is_photography_related": True, "extracted": {}, "replies": []}])
    await br.handle_private_message(msg(text="hello"))
    texts = br.adapters["wechat"].sent_texts
    assert len(texts) <= 3, f"too many bubbles: {texts}"
    print(f"[PASS] 4) bubble count bounded: {texts}")


async def test_existing_booking_backfills_slots():
    """客户已在官网约好（发截图/说约好了），必须自动回填，不再从 CN 问起。"""
    br = make_brain([{"is_photography_related": True, "extracted": {}, "replies": ["ok收到"]}])

    async def fake_lookup(platform, user_id):
        return {
            "id": 7, "cn": "小樱", "character": "初音未来",
            "event_name": "11月27日 BACC", "date": "11月27日", "time": "14:00-16:00",
            "quantity": 1, "remarks": "",
        }

    br._lookup_booking_by_session = fake_lookup
    res = await br.handle_private_message(msg(text="老大我约好了"))
    assert res["action"] == "CONFIRMED_EXISTING_BOOKING", f"已有预约应直接确认而非重新下单: {res}"
    texts = br.adapters["wechat"].sent_texts
    assert not any("称谓" in t or "怎么称呼" in t for t in texts), f"不该问称呼: {texts}"
    assert not br.adapters["wechat"].sent_images, "已约好时不应再发新二维码"
    print(f"[PASS] 5) 已有预约自动回填，直接确认不重复下单: {texts}")


async def test_image_triggers_lookup():
    """客户只发图片也要去查已有预约。"""
    br = make_brain([{"is_photography_related": True, "extracted": {}, "replies": ["ok收到"]}])
    called = {"n": 0}

    async def fake_lookup(platform, user_id):
        called["n"] += 1
        return {"id": 9, "cn": "A", "character": "B", "event_name": "C", "date": "D", "time": "E"}

    br._lookup_booking_by_session = fake_lookup
    res = await br.handle_private_message(msg(text="", images=["http://img/qr.png"]))
    assert called["n"] == 1, "图片消息应触发预约查询"
    assert res["action"] != "ASK_MISSING_SLOTS"
    print("[PASS] 6) 图片消息会按会话找回已有预约")


async def test_dedupe_same_question():
    """模型自己重复同一句追问时，必须去重，不能连发两遍「老大怎么称呼」。"""
    br = make_brain([{
        "is_photography_related": True, "extracted": {},
        "replies": ["老大怎么称呼", "老大怎么称呼"],
    }])
    await br.handle_private_message(msg(text="在吗"))
    texts = br.adapters["wechat"].sent_texts
    same = [t for t in texts if "称呼" in t]
    assert len(same) == 1, f"重复追问未去重: {texts}"
    print(f"[PASS] 7) 同一句追问已去重: {texts}")


async def test_persistent_dedupe_and_memory():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "shared.db"
        br = make_brain([{
            "is_photography_related": True,
            "extracted": {"cn": "小樱", "character": "初音未来"},
            "replies": ["在的", "27号接的"],
        }], db_path=db_path)
        incoming = msg("去27号吗")
        incoming.user_id = "客户A"
        incoming.nickname = "微信昵称A"
        incoming.raw = {"message_id": "wx-001"}
        first = await br.handle_message(incoming)
        duplicate = await br.handle_message(incoming)
        assert first["action"] == "ASK_MISSING_SLOTS"
        assert duplicate["action"] == "IGNORED_DUPLICATE"
        assert len(br.adapters["wechat"].sent_texts) == 2, "同一消息 ID 不得二次发送"

        br2 = make_brain([{
            "is_photography_related": True, "extracted": {}, "replies": ["好的"],
        }], db_path=db_path)
        br2.adapters = br.adapters
        follow_up = msg("角色就按这个")
        follow_up.user_id = "客户A"
        follow_up.nickname = "微信昵称A"
        follow_up.raw = {"message_id": "wx-002"}
        await br2.handle_message(follow_up)
        prompt = json.loads(br2.llm.last_prompt)
        assert prompt["current_slots"]["cn"] == "小樱", "重启后槽位丢失"
        assert prompt["current_slots"]["character"] == "初音未来"
        assert any("去27号吗" in h["text"] for h in prompt["recent_history"]), "重启后历史丢失"
        assert prompt["memory_scope"] == "wechat:客户A"
        saved = br2.conversation_store.load_session("wechat", "客户A", "微信昵称A")
        assert "CN:小樱" in saved["auto_remark"]
        assert saved["_version"] >= 2
        print("[PASS] 8) 持久化防重、重启记忆、自动备注与版本号")


async def test_contact_context_isolated():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "isolated.db"
        br = make_brain([
            {"is_photography_related": True, "extracted": {"cn": "Alpha"}, "replies": ["收到"]},
            {"is_photography_related": True, "extracted": {}, "replies": ["好的"]},
        ], db_path=db_path)
        first = msg("我是Alpha")
        first.user_id = "客户A"
        first.raw = {"message_id": "a-001"}
        await br.handle_message(first)
        second = msg("在吗")
        second.user_id = "客户B"
        second.nickname = "微信昵称B"
        second.raw = {"message_id": "b-001"}
        await br.handle_message(second)
        prompt = json.loads(br.llm.last_prompt)
        assert prompt["memory_scope"] == "wechat:客户B"
        assert prompt["current_slots"]["cn"] == ""
        assert all("Alpha" not in h["text"] for h in prompt["recent_history"]), "联系人上下文串扰"
        print("[PASS] 9) 不同微信联系人的上下文完全隔离")


async def test_context_cleanup_and_fallback_dedupe_expiry():
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(Path(tmp) / "cleanup.db", context_ttl_sec=10)
        session = store._base_session()
        session["slots"]["cn"] = "过期客户"
        session["history"] = [{"role": "user", "text": "旧消息"}]
        store.save_session("wechat", "客户C", "昵称C", session, now_ts=100)
        assert store.claim_message("fallback", "wechat", "客户C", ttl_sec=50, now_ts=100)
        assert not store.claim_message("fallback", "wechat", "客户C", ttl_sec=50, now_ts=120)
        assert store.claim_message("fallback", "wechat", "客户C", ttl_sec=50, now_ts=160)
        report = store.cleanup(now_ts=120)
        assert report["expired_sessions"] == 1
        cleaned = store.load_session("wechat", "客户C", "昵称C", now_ts=121)
        assert cleaned["history"] == []
        assert cleaned["slots"]["cn"] == ""
        assert cleaned["_version"] >= 2
        print("[PASS] 10) 过期上下文定期清理，内容防重按期限失效")


def test_message_feed_archive_and_filters():
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(Path(tmp) / "feed.db")
        replied_id = store.record_incoming(
            platform="wechat", chat_type="private", group_id=None,
            user_id="客户A", nickname="小樱昵称", text="27号接吗", images=[],
            dedupe_key="feed-a", now_ts=100,
        )
        store.record_result(replied_id, {
            "action": "ASK_MISSING_SLOTS", "replies": ["接的", "老大拍啥角色"],
        }, now_ts=101)
        ignored_id = store.record_incoming(
            platform="wechat", chat_type="private", group_id=None,
            user_id="客户B", nickname="日常朋友", text="中午吃了什么", images=[],
            dedupe_key="feed-b", now_ts=102,
        )
        store.record_result(ignored_id, {
            "action": "IGNORED_UNRELATED_PRIVATE", "reason": "与摄影无关",
        }, now_ts=103)
        pending_id = store.record_incoming(
            platform="wechat", chat_type="private", group_id=None,
            user_id="客户C", nickname="待人工客户", text="想约拍", images=[],
            dedupe_key="feed-c", now_ts=104,
        )
        store.record_result(pending_id, {"action": "ENQUEUED_ADD_FRIEND"}, now_ts=105)

        assert store.list_conversations(status="replied")["items"][0]["nickname"] == "小樱昵称"
        assert store.list_conversations(status="no_reply")["items"][0]["last_reason"] == "与摄影无关"
        assert store.list_conversations(status="needs_reply")["items"][0]["nickname"] == "待人工客户"
        assert store.list_conversations(query="小樱")["total"] == 1
        detail = store.get_conversation("wechat", "客户A")
        assert [message["direction"] for message in detail["messages"]] == ["in", "out", "out"]
        assert [message["text"] for message in detail["messages"]] == ["27号接吗", "接的", "老大拍啥角色"]

        store.record_outgoing(
            platform="wechat", chat_id="客户C", user_id="客户C", nickname="待人工客户",
            text="我人工回复你", resolve_pending=True, now_ts=106,
        )
        assert store.list_conversations(status="replied", query="待人工")["total"] == 1
        store.clear_session("wechat", "客户A")
        assert len(store.get_conversation("wechat", "客户A")["messages"]) == 3, "清理 AI 上下文不应删除消息档案"
        print("[PASS] 11) 全部消息永久归档、昵称搜索、回复状态筛选与人工回复")


async def main():
    # 单测不碰真实网络：把 httpx 客户端替换为桩实现
    brain_mod.httpx = type("Stub", (), {"AsyncClient": FakeHTTPClient})

    await test_image_not_ignored()
    await test_no_loop_after_repeat_ask()
    await test_known_fields_not_reasked()
    await test_bubbles_short()
    await test_existing_booking_backfills_slots()
    await test_image_triggers_lookup()
    await test_dedupe_same_question()
    await test_persistent_dedupe_and_memory()
    await test_contact_context_isolated()
    await test_context_cleanup_and_fallback_dedupe_expiry()
    test_message_feed_archive_and_filters()
    print("=== ALL PASS (11/11) ===")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    asyncio.run(main())
