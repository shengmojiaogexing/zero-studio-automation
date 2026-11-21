#!/usr/bin/env python3
"""
Sprint 2.5 + Sprint 3 离线自动化冒烟测试：
Mock DeepSeek 与 NapCatQQ/wxauto 通道，联动真实 booking-site，验证：
(a) 群聊闲聊被本地正则 0-Token 过滤、约拍需求触发高意向并进入冷却队列（含 DRY_RUN 演练校验）；
(b) 同一用户 30 天去重、连续入队随机间隔 30–120 秒、超出每小时 3 次额度被正确限频拦截；
(c) 私聊多轮对话收齐信息后自动写入真实 booking-site，并拉取真实场记二维码 PNG 下发客户 + 校验 /api/gateway/notify-gallery；
(d) 企业微信客服 Adapter 回调验签（GET/POST + AES-256-CBC 解密）与文本/图片/位置/选图 H5 链接卡片消息体构造通过；
(e) Sprint 2.5 大光比暗背景亮主体防误杀 + 预览 XMP 前置处理（原片零修改）回归通过。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import cv2
import httpx
import numpy as np
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

GATEWAY_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = GATEWAY_DIR.parents[1]
BOOKING_SITE_DIR = WORKSPACE_ROOT / "apps" / "booking-site"
PIPELINE_DIR = WORKSPACE_ROOT / "pipelines" / "ingest-cull-cluster"

sys.path.insert(0, str(GATEWAY_DIR))
sys.path.insert(0, str(PIPELINE_DIR))

from adapters.base import IMAdapter, IncomingMessage  # noqa: E402
from adapters.qq_napcat import QQNapCatAdapter  # noqa: E402
from adapters.wechat_wxauto import WeChatWxAutoAdapter  # noqa: E402
from adapters.wecom_kf import WeComKFAdapter  # noqa: E402
from main import create_app  # noqa: E402
from rate_limiter import FriendRequestRateLimiter  # noqa: E402
import ingest  # noqa: E402
import cull_and_cluster  # noqa: E402

TEST_BOOKING_PORT = 3458
BOOKING_BASE_URL = f"http://127.0.0.1:{TEST_BOOKING_PORT}"
INTERNAL_KEY = "sprint3-e2e-internal-key"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class MockIMAdapter(IMAdapter):
    """记录所有发出的文字、图片、定位与加好友调用，用于断言验证。"""

    def __init__(self, platform: str):
        self.platform = platform
        self.sent_texts: List[Dict[str, Any]] = []
        self.sent_images: List[Dict[str, Any]] = []
        self.sent_locations: List[Dict[str, Any]] = []
        self.friend_requests: List[Dict[str, Any]] = []
        self.fail_next_text = False

    async def send_text(self, target_id: str, text: str, chat_type: str = "private") -> Dict[str, Any]:
        if self.fail_next_text:
            self.fail_next_text = False
            raise RuntimeError("模拟通道发送失败")
        rec = {"target_id": target_id, "text": text, "chat_type": chat_type}
        self.sent_texts.append(rec)
        return {"status": "ok", **rec}

    async def send_image(
        self, target_id: str, image: Union[str, Path, bytes], chat_type: str = "private"
    ) -> Dict[str, Any]:
        raw_bytes = image if isinstance(image, bytes) else Path(image).read_bytes()
        rec = {"target_id": target_id, "size": len(raw_bytes), "bytes": raw_bytes, "chat_type": chat_type}
        self.sent_images.append(rec)
        return {"status": "ok", "target_id": target_id, "size": len(raw_bytes)}

    async def send_location(
        self,
        target_id: str,
        lat: float,
        lng: float,
        title: str,
        address: str,
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        rec = {"target_id": target_id, "lat": lat, "lng": lng, "title": title, "address": address}
        self.sent_locations.append(rec)
        return {"status": "ok", **rec}

    async def request_add_friend(
        self, user_id: str, remark: str, group_id: Optional[str] = None
    ) -> Dict[str, Any]:
        rec = {"user_id": user_id, "remark": remark, "group_id": group_id}
        self.friend_requests.append(rec)
        return {"status": "ok", **rec}


class MockDeepSeekClient:
    """离线确定性 DeepSeek JSON Mode Mock，同时统计调用次数以验证正则快筛 0-Token 效果。"""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    async def chat_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        self.calls.append({"system": system_prompt[:32], "user": user_prompt})
        payload = json.loads(user_prompt)

        # 群聊意向评分模式
        if "message" in payload and "group_id" in payload:
            text = payload["message"]
            if any(k in text for k in ("找摄", "约拍", "场照")):
                return {
                    "intent_score": 92,
                    "is_potential_client": True,
                    "city": "上海",
                    "event_name": "CP31",
                    "character": "流萤",
                    "shoot_time": "2026-05-01",
                    "reason": "明确寻找漫展场照摄影师",
                    "add_friend_remark": "你好呀！群里看到你CP31出流萤找场照摄，扩列看作品约拍吗~",
                }
            return {
                "intent_score": 25,
                "is_potential_client": False,
                "city": "",
                "event_name": "",
                "character": "",
                "shoot_time": "",
                "reason": "非约拍需求",
                "add_friend_remark": "",
            }

        # 私聊多轮接单模式
        latest = payload.get("latest_message", "")
        extracted = {"cn": "", "character": "", "event_name": "", "date": "", "time": "", "remarks": ""}
        if "CP31" in latest:
            extracted["event_name"] = "CP31"
        if "芙宁娜" in latest:
            extracted["character"] = "芙宁娜"
        if "水神小团子" in latest:
            extracted["cn"] = "水神小团子"
        if "5月1日" in latest or "2026-05-01" in latest:
            extracted["date"] = "2026-05-01"
        if "14:00-16:00" in latest:
            extracted["time"] = "14:00-16:00"
        if "冷色调" in latest:
            extracted["remarks"] = "想要冷色调场照"

        return {
            "extracted": extracted,
            "reply": "收到啦！请补充一下你的 CN、拍摄日期和具体时段，我马上帮你锁档~",
        }


def start_booking_server(db_path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "PORT": str(TEST_BOOKING_PORT),
        "DB_PATH": str(db_path),
        "INTERNAL_API_KEY": INTERNAL_KEY,
        "ADMIN_USER": "admin",
        "ADMIN_PASSWORD": "test-password",
        "PUSHPLUS_TOKEN": "",
    }
    proc = subprocess.Popen(
        ["node", str(BOOKING_SITE_DIR / "server.js")],
        cwd=str(BOOKING_SITE_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    deadline = time.monotonic() + 12.0
    with httpx.Client(timeout=1.0, trust_env=False) as probe:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise RuntimeError(f"booking-site 退出异常: {out} {err}")
            try:
                r = probe.get(f"{BOOKING_BASE_URL}/api/settings")
                if r.status_code == 200:
                    return proc
            except httpx.HTTPError:
                time.sleep(0.2)
    proc.kill()
    proc.wait(timeout=5)
    raise RuntimeError("booking-site 启动超时")


def encrypt_wecom_payload(encoding_aes_key: str, corp_id: str, plaintext: str) -> str:
    """构造标准企业微信 AES-256-CBC 加密密文，用于测试 WeComKFAdapter 解密逻辑。"""
    aes_key = base64.b64decode(encoding_aes_key + "=")
    msg_bytes = plaintext.encode("utf-8")
    raw = os.urandom(16) + struct.pack("!I", len(msg_bytes)) + msg_bytes + corp_id.encode("utf-8")
    pad_len = 32 - (len(raw) % 32)
    padded = raw + bytes([pad_len] * pad_len)
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_key[:16]))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


async def run_all_tests() -> None:
    print("=== Sprint 2.5 + Sprint 3 离线冒烟测试开始 ===")

    with tempfile.TemporaryDirectory(prefix="zero_sprint3_") as tmp_str:
        tmp_root = Path(tmp_str)
        booking_db = tmp_root / "booking_sprint3.db"
        gateway_db = tmp_root / "gateway_sprint3.db"

        booking_proc = start_booking_server(booking_db)
        try:
            mock_qq = MockIMAdapter("qq")
            mock_wx = MockIMAdapter("wechat")

            # 生成随机 43 位合法 Base64 AESKey 用于企微加解密测试
            aes_key_b64 = base64.b64encode(os.urandom(32)).decode("ascii").rstrip("=")
            wecom_adapter = WeComKFAdapter(
                corp_id="ww_zero_studio_corp",
                kf_secret="kf_test_secret",
                token="wecom_cb_token_2026",
                encoding_aes_key=aes_key_b64,
                open_kfid="wk_zero_kf_001",
            )

            limiter = FriendRequestRateLimiter(
                db_path=gateway_db,
                max_per_hour=3,
                min_interval_sec=30.0,
                max_interval_sec=120.0,
                dedup_days=30,
                dry_run=True,
            )
            mock_llm = MockDeepSeekClient()

            app = create_app(
                adapters={"qq": mock_qq, "wechat": mock_wx, "wecom_kf": wecom_adapter},
                rate_limiter=limiter,
                llm_client=mock_llm,
                booking_api_url=BOOKING_BASE_URL,
                internal_api_key=INTERNAL_KEY,
                enable_background_workers=False,
            )

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://im-gateway.local", trust_env=False) as client:
                # 校验 OneBot v11 事件解析器与 wxauto 桌面/网页模式开关
                parsed_ob = QQNapCatAdapter.parse_onebot_event(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 888888,
                        "user_id": 10001,
                        "sender": {"card": "小流萤"},
                        "message": [
                            {"type": "text", "data": {"text": "上海CP31 5月1日出流萤找摄影约拍场照"}},
                            {"type": "image", "data": {"url": "https://example.com/ref.jpg"}},
                        ],
                    }
                )
                assert parsed_ob is not None and parsed_ob.group_id == "888888" and len(parsed_ob.images) == 1
                wx_mode_check = WeChatWxAutoAdapter(mode="web")
                assert wx_mode_check.mode == "web"

                # ---------- (a) 群聊闲聊过滤 vs 约拍高意向入冷却队列 ----------
                r_chat = await client.post(
                    "/webhook/napcat",
                    json={
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 888888,
                        "user_id": 10000,
                        "sender": {"nickname": "路人甲"},
                        "message": [{"type": "text", "data": {"text": "今天中午吃啥呀，好饿"}}],
                    },
                )
                assert r_chat.status_code == 200
                assert r_chat.json()["result"]["action"] == "IGNORED_NO_KEYWORD"
                assert len(mock_llm.calls) == 0, "普通闲聊不得消耗 DeepSeek Token！"

                r_lead1 = await client.post(
                    "/webhook/napcat",
                    json={
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 888888,
                        "user_id": 10001,
                        "sender": {"card": "小流萤"},
                        "message": [{"type": "text", "data": {"text": "上海CP31 5月1日出流萤找摄影约拍场照"}}],
                    },
                )
                assert r_lead1.status_code == 200
                lead1_res = r_lead1.json()["result"]
                assert lead1_res["action"] == "ENQUEUED_ADD_FRIEND"
                assert lead1_res["intent_score"] >= 80
                assert 30.0 <= lead1_res["queue"]["delay_sec"] <= 120.5
                assert lead1_res["queue"]["dry_run"] is True

                # 演练模式消费到点队列：应标记为 DRY_RUN 且绝不调用真实 request_add_friend
                future_ts = lead1_res["queue"]["scheduled_at"] + 1.0
                dry_processed = await limiter.process_due_requests({"qq": mock_qq}, now_ts=future_ts)
                assert len(dry_processed) == 1 and dry_processed[0]["status"] == "DRY_RUN"
                assert len(mock_qq.friend_requests) == 0, "DRY_RUN_ADD_FRIEND=true 不得真正触发加好友"
                print("[PASS] (a) 群聊闲聊 0-Token 过滤、高意向入冷却队列与 DRY_RUN 安全演练验证通过")

                # ---------- (b) 同一用户 30 天去重 + 每小时 3 次上限限频 + 30-120s 间隔 ----------
                r_dup = await client.post(
                    "/webhook/napcat",
                    json={
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 888888,
                        "user_id": 10001,  # 同一用户重复触发
                        "sender": {"card": "小流萤"},
                        "message": [{"type": "text", "data": {"text": "再蹲一个CP31场照摄影约拍"}}],
                    },
                )
                assert r_dup.status_code == 200
                assert r_dup.json()["result"]["action"] == "BLOCKED_DEDUP_30D"

                # 第 2、3 个不同高意向用户成功入队，验证随机冷却间隔在 30-120s
                r_lead2 = await client.post(
                    "/webhook/napcat",
                    json={
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 888888,
                        "user_id": 10002,
                        "sender": {"card": "用户2"},
                        "message": [{"type": "text", "data": {"text": "CP31找摄影拍场照"}}],
                    },
                )
                assert r_lead2.json()["result"]["action"] == "ENQUEUED_ADD_FRIEND"
                sched2 = r_lead2.json()["result"]["queue"]["scheduled_at"]

                r_lead3 = await client.post(
                    "/webhook/napcat",
                    json={
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 888888,
                        "user_id": 10003,
                        "sender": {"card": "用户3"},
                        "message": [{"type": "text", "data": {"text": "五一漫展约拍找摄影"}}],
                    },
                )
                assert r_lead3.json()["result"]["action"] == "ENQUEUED_ADD_FRIEND"
                sched3 = r_lead3.json()["result"]["queue"]["scheduled_at"]
                gap_2_3 = sched3 - sched2
                assert 30.0 <= gap_2_3 <= 120.0, f"相邻加好友间隔应在 30-120s，实际 {gap_2_3:.2f}s"

                # 第 4 个用户超出每小时 3 次上限，必须被拦截
                r_lead4 = await client.post(
                    "/webhook/napcat",
                    json={
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 888888,
                        "user_id": 10004,
                        "sender": {"card": "用户4"},
                        "message": [{"type": "text", "data": {"text": "急找摄影约拍场照"}}],
                    },
                )
                assert r_lead4.json()["result"]["action"] == "BLOCKED_HOURLY_QUOTA_EXCEEDED"
                print(
                    f"[PASS] (b) 同用户 30 天去重、随机间隔({gap_2_3:.1f}s ∈ [30,120])与每小时 3 次额度拦截验证通过"
                )

                # ---------- (c) 私聊多轮接单 -> 写库真实 booking-site -> 下发场记二维码 PNG ----------
                dm_turn1 = await client.post(
                    "/api/gateway/incoming",
                    headers={"X-API-Key": INTERNAL_KEY},
                    json={
                        "platform": "wechat",
                        "chat_type": "private",
                        "user_id": "wx_furina_client",
                        "nickname": "小团子",
                        "text": "你好呀，我想约CP31出芙宁娜！",
                    },
                )
                assert dm_turn1.status_code == 200
                t1_res = dm_turn1.json()["result"]
                assert t1_res["action"] == "ASK_MISSING_SLOTS"
                assert set(t1_res["missing"]) == {"cn", "date", "time"}
                assert len(mock_wx.sent_texts) == 1 and len(mock_wx.sent_images) == 0

                dm_turn2 = await client.post(
                    "/api/gateway/incoming",
                    headers={"X-API-Key": INTERNAL_KEY},
                    json={
                        "platform": "wechat",
                        "chat_type": "private",
                        "user_id": "wx_furina_client",
                        "nickname": "小团子",
                        "text": "我CN叫水神小团子，5月1日 14:00-16:00，想要冷色调场照~",
                    },
                )
                assert dm_turn2.status_code == 200
                t2_res = dm_turn2.json()["result"]
                assert t2_res["action"] == "BOOKING_CREATED_AND_QR_SENT"
                created_bid = t2_res["booking_id"]
                created_token = t2_res["gallery_token"]

                # 验证确实下发了有效的场记二维码 PNG 图片给微信客户
                assert len(mock_wx.sent_images) == 1
                sent_qr_bytes = mock_wx.sent_images[0]["bytes"]
                assert len(sent_qr_bytes) > 100 and sent_qr_bytes[:8] == PNG_MAGIC

                # 核验真实 booking-site SQLite 数据库已落盘
                conn = sqlite3.connect(str(booking_db))
                try:
                    row = conn.execute(
                        "SELECT id, platform, im_user_id, cn, character, event_name, date, time, status, gallery_token "
                        "FROM bookings_v3 WHERE id = ?",
                        (created_bid,),
                    ).fetchone()
                    assert row is not None
                    assert row[1] == "wechat" and row[2] == "wx_furina_client"
                    assert row[3] == "水神小团子" and row[4] == "芙宁娜" and row[5] == "CP31"
                    assert row[6] == "2026-05-01" and row[7] == "14:00-16:00" and row[8] == "BOOKED"
                finally:
                    conn.close()

                # 校验选图链接回调推送接口 POST /api/gateway/notify-gallery（鉴权 + 推送）
                no_auth_notify = await client.post(
                    "/api/gateway/notify-gallery",
                    json={
                        "platform": "wechat",
                        "user_id": "wx_furina_client",
                        "booking_id": created_bid,
                        "gallery_token": created_token,
                    },
                )
                assert no_auth_notify.status_code == 401

                ok_notify = await client.post(
                    "/api/gateway/notify-gallery",
                    headers={"X-API-Key": INTERNAL_KEY},
                    json={
                        "platform": "wechat",
                        "user_id": "wx_furina_client",
                        "booking_id": created_bid,
                        "cn": "水神小团子",
                        "character": "芙宁娜",
                        "event_name": "CP31",
                        "gallery_token": created_token,
                    },
                )
                assert ok_notify.status_code == 200
                assert f"/gallery/{created_token}" in mock_wx.sent_texts[-1]["text"]

                gallery_detail = await client.get(
                    "/api/gateway/conversations/detail",
                    headers={"X-API-Key": INTERNAL_KEY},
                    params={"platform": "wechat", "chat_id": "wx_furina_client"},
                )
                assert gallery_detail.status_code == 200
                gallery_archived = [
                    msg for msg in gallery_detail.json()["messages"]
                    if msg["action"] == "GALLERY_NOTIFICATION"
                ]
                assert len(gallery_archived) == 1
                assert gallery_archived[0]["direction"] == "out"
                assert gallery_archived[0]["id"] == ok_notify.json()["message_id"]
                assert f"/gallery/{created_token}" in gallery_archived[0]["text"]
                assert gallery_archived[0]["nickname"] == "水神小团子"

                delivery_url = f"{BOOKING_BASE_URL}/delivery/{created_token}"
                ok_delivery = await client.post(
                    "/api/gateway/notify-delivery",
                    headers={"X-API-Key": INTERNAL_KEY},
                    json={
                        "platform": "wechat",
                        "user_id": "wx_furina_client",
                        "booking_id": created_bid,
                        "cn": "水神小团子",
                        "character": "芙宁娜",
                        "event_name": "CP31",
                        "delivery_url": delivery_url,
                        "output_count": 9,
                    },
                )
                assert ok_delivery.status_code == 200
                assert delivery_url in mock_wx.sent_texts[-1]["text"]

                delivery_detail = await client.get(
                    "/api/gateway/conversations/detail",
                    headers={"X-API-Key": INTERNAL_KEY},
                    params={"platform": "wechat", "chat_id": "wx_furina_client"},
                )
                delivery_archived = [
                    msg for msg in delivery_detail.json()["messages"]
                    if msg["action"] == "DELIVERY_NOTIFICATION"
                ]
                assert len(delivery_archived) == 1
                assert delivery_archived[0]["direction"] == "out"
                assert delivery_archived[0]["id"] == ok_delivery.json()["message_id"]
                assert delivery_url in delivery_archived[0]["text"]
                assert "共 9 张高清精修成片" in delivery_archived[0]["text"]

                mock_wx.fail_next_text = True
                try:
                    failed_delivery = await client.post(
                        "/api/gateway/notify-delivery",
                        headers={"X-API-Key": INTERNAL_KEY},
                        json={
                            "platform": "wechat",
                            "user_id": "wx_failed_notify",
                            "booking_id": created_bid,
                            "delivery_url": delivery_url,
                        },
                    )
                    failed_status = failed_delivery.status_code
                except RuntimeError as exc:
                    assert "模拟通道发送失败" in str(exc)
                    failed_status = 500
                assert failed_status >= 500
                failed_detail = await client.get(
                    "/api/gateway/conversations/detail",
                    headers={"X-API-Key": INTERNAL_KEY},
                    params={"platform": "wechat", "chat_id": "wx_failed_notify"},
                )
                assert failed_detail.status_code == 200
                assert failed_detail.json()["messages"] == []
                print(
                    f"[PASS] (c) 私聊多轮接单写库真实 booking-site(#{created_bid})、"
                    f"下发有效场记二维码 PNG({len(sent_qr_bytes)} bytes)、选图/交付通知永久归档及失败不归档验证通过"
                )

                # ---------- (d) 企业微信客服 Adapter 回调验签与消息体构造 ----------
                ts = "1732100000"
                nonce = "nonce_zero_123"
                plain_echo = "wecom_echo_ok_2026"
                enc_echo = encrypt_wecom_payload(aes_key_b64, "ww_zero_studio_corp", plain_echo)
                valid_sig = wecom_adapter.compute_signature(ts, nonce, enc_echo)

                r_wecom_ok = await client.get(
                    "/webhook/wecom-kf",
                    params={
                        "msg_signature": valid_sig,
                        "timestamp": ts,
                        "nonce": nonce,
                        "echostr": enc_echo,
                    },
                )
                assert r_wecom_ok.status_code == 200 and r_wecom_ok.text == plain_echo

                r_wecom_bad = await client.get(
                    "/webhook/wecom-kf",
                    params={
                        "msg_signature": "deadbeef_invalid_sig",
                        "timestamp": ts,
                        "nonce": nonce,
                        "echostr": enc_echo,
                    },
                )
                assert r_wecom_bad.status_code == 403

                # 构造文本、图片、位置与选图 H5 链接卡片 payload 校验
                txt_p = wecom_adapter.build_text_payload("wm_ext_001", "欢迎约拍 ZERO STUDIO")
                assert txt_p["msgtype"] == "text" and txt_p["open_kfid"] == "wk_zero_kf_001"
                img_p = wecom_adapter.build_image_payload("wm_ext_001", "MEDIA_ID_123")
                assert img_p["msgtype"] == "image" and img_p["image"]["media_id"] == "MEDIA_ID_123"
                loc_p = wecom_adapter.build_location_payload("wm_ext_001", 31.19, 121.30, "国展北厅", "上海青浦")
                assert loc_p["msgtype"] == "location" and loc_p["location"]["name"] == "国展北厅"
                card_p = wecom_adapter.build_gallery_link_card_payload(
                    touser="wm_ext_001",
                    title="你的 CP31 选图相册已就绪",
                    desc="已自动过滤废片，点击挑选精修图与预设",
                    gallery_url=f"{BOOKING_BASE_URL}/gallery/{created_token}",
                    thumb_media_id="THUMB_999",
                )
                assert card_p["msgtype"] == "link"
                assert card_p["link"]["url"].endswith(f"/gallery/{created_token}")
                assert card_p["link"]["thumb_media_id"] == "THUMB_999"
                print("[PASS] (d) 企业微信客服 Adapter 回调验签(AES解密)/拒绝非法签名及图文/定位/H5选图卡片构造验证通过")

                # ---------- (e) Sprint 2.5 大光比暗背景亮主体防误杀 + 预览 XMP 回归 ----------
                hc_img = np.full((900, 1200, 3), 4, dtype=np.uint8)
                cv2.rectangle(hc_img, (480, 260), (720, 680), (175, 160, 210), -1)
                cv2.circle(hc_img, (600, 220), 65, (205, 195, 220), -1)
                for y in range(280, 660, 16):
                    cv2.line(hc_img, (490, y), (710, y), (50, 45, 70), 2)
                for x in range(500, 710, 18):
                    cv2.line(hc_img, (x, 270), (x, 670), (240, 235, 245), 2)

                qc_hc = cull_and_cluster.evaluate_technical_quality(hc_img)
                assert qc_hc["metrics"]["dark_ratio"] > 0.85
                assert qc_hc["is_rejected"] == 0, f"大光比暗背景亮主体不应被误杀: {qc_hc}"

                # 同步确认真正的全黑测光废片仍会被拦截
                pure_black = np.full((900, 1200, 3), 3, dtype=np.uint8)
                qc_black = cull_and_cluster.evaluate_technical_quality(pure_black)
                assert qc_black["is_rejected"] == 1 and "UNDEREXPOSED_BLACK" in (qc_black["reject_reason"] or "")

                raw_origin = tmp_root / "hc_raw_origin.jpg"
                Image.fromarray(cv2.cvtColor(hc_img, cv2.COLOR_BGR2RGB)).save(raw_origin, format="JPEG", quality=92)
                before_sha = hashlib.sha256(raw_origin.read_bytes()).hexdigest()

                xmp_path = tmp_root / "preview_air.xmp"
                xmp_path.write_text(
                    """<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
      crs:Exposure2012="+0.90"
      crs:Shadows2012="+55"
      crs:Highlights2012="-25"
      crs:Temperature="5700" />
  </rdf:RDF>
</x:xmpmeta>
""",
                    encoding="utf-8",
                )
                out_webp = tmp_root / "hc_preview.webp"
                backend = ingest.apply_preview_xmp(raw_origin, xmp_path, out_webp)
                assert out_webp.exists() and "xmp" in backend
                assert hashlib.sha256(raw_origin.read_bytes()).hexdigest() == before_sha, "原片绝不可被修改"
                preview_bgr = cull_and_cluster.cv2_imread_unicode(out_webp)
                assert preview_bgr is not None and float(np.mean(preview_bgr)) > float(np.mean(hc_img))
                print(f"[PASS] (e) Sprint 2.5 大光比暗背景防误杀、全黑片拦截与预览 XMP({backend})原片零修改回归通过")

            print("=== Sprint 2.5 + Sprint 3 全部离线冒烟测试通过 (5/5) ===")
        finally:
            booking_proc.terminate()
            try:
                booking_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                booking_proc.kill()


if __name__ == "__main__":
    asyncio.run(run_all_tests())
