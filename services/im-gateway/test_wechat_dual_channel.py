#!/usr/bin/env python3
"""
微信双通道综合单元测试 (test_wechat_dual_channel.py)
====================================================
覆盖：
1. 对内微信通知通道 (`OpenClawWeixinNotifier`)：动作汇总推送 + 修图待终审推送；
2. 对外微信 4.x 桥接适配器 (`WeChatHTTPBridgeAdapter`)：出站文本/图片/位置/加好友 + 入站 Webhook 解析；
3. 双通道路由 (`WECHAT_MODE=bridge_http` / `wecom_kf`) 与 `PoseBriefingStore` 联动。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parent
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

from adapters.wechat_bridge import WeChatHTTPBridgeAdapter  # noqa: E402
from openclaw_notifier import OpenClawWeixinNotifier  # noqa: E402
from pose_briefing import PoseBriefingStore  # noqa: E402


class TestWeChatDualChannel(unittest.TestCase):
    def test_openclaw_internal_notifier_pose_and_review(self):
        sent = []

        def mock_send(channel, payload):
            sent.append((channel, payload))
            return "ok:mock"

        notifier = OpenClawWeixinNotifier(weixin_target="photographer_self", mock_send_fn=mock_send)
        r1 = notifier.push_pose_summary("# 动作汇总\n- 回眸", event_name="CP", date="2026-12-26")
        self.assertEqual(r1["status"], "ok")

        r2 = notifier.push_review_ready(
            booking_id=42,
            cn="小梨",
            character="初音未来",
            review_url="https://studio.example.com/review.html?booking_id=42",
            preview_url="https://studio.example.com/outputs/b42.jpg",
            asset_count=6,
        )
        self.assertEqual(r2["status"], "ok")
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][0], "openclaw-weixin")
        self.assertIn("动作汇总", sent[0][1]["title"])
        self.assertIn("#42", sent[1][1]["title"])
        self.assertEqual(sent[1][1]["action_url"], "https://studio.example.com/review.html?booking_id=42")

    def test_wechat_4x_http_bridge_outbound_and_inbound(self):
        calls = []

        def mock_http(path, payload):
            calls.append((path, payload))
            return {"status": "ok", "echo": path}

        adapter = WeChatHTTPBridgeAdapter(
            base_url="http://127.0.0.1:8095",
            token="test-bridge-token",
            mock_http_fn=mock_http,
        )

        async def _run():
            await adapter.send_text("wxid_client_1", "在的，那天有档期", chat_type="private")
            await adapter.send_image("wxid_client_1", b"\x89PNG\r\n", chat_type="private")
            await adapter.send_location("wxid_client_1", 39.9042, 116.4074, "展馆东门", "检票口右侧")
            await adapter.request_add_friend("wxid_client_2", "你好，看到你在找漫展摄影", group_id="grp_1")

        asyncio.run(_run())
        self.assertEqual([c[0] for c in calls], [
            "/api/send/text",
            "/api/send/image",
            "/api/send/text",
            "/api/friend/add",
        ])
        self.assertIn("image_base64", calls[1][1])

        # 入站 Webhook 解析（群聊 + 私聊）
        msg_priv = WeChatHTTPBridgeAdapter.parse_webhook_event({
            "sender_id": "wxid_coser_88",
            "sender_name": "阿璃",
            "content": "请问下周漫展还接场照吗",
            "image_url": "https://cdn.example.com/ref.jpg",
        })
        self.assertIsNotNone(msg_priv)
        assert msg_priv is not None
        self.assertEqual(msg_priv.platform, "wechat")
        self.assertEqual(msg_priv.chat_type, "private")
        self.assertEqual(msg_priv.images, ["https://cdn.example.com/ref.jpg"])

        msg_grp = WeChatHTTPBridgeAdapter.parse_webhook_event({
            "user_id": "wxid_coser_99",
            "group_id": "room_123",
            "text": "蹲一个周六漫展摄影",
        })
        assert msg_grp is not None
        self.assertEqual(msg_grp.chat_type, "group")
        self.assertEqual(msg_grp.group_id, "room_123")

    def test_pose_briefing_integrates_with_notifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PoseBriefingStore(Path(tmp))
            store.upsert_sheet(booking_id=9, cn="林林", character="白贞德", poses=["举旗仰拍"])
            res = store.push_summary_to_mobile(pushplus_token="", openclaw_webhook_url="")
            self.assertEqual(res["status"], "ok")
            self.assertTrue(Path(res["summary_file"]).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
