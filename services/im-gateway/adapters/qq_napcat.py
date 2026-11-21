"""QQ NapCatQQ (OneBot v11 WebSocket / HTTP) 适配器。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

import httpx
import websockets

from .base import IMAdapter, IncomingMessage

logger = logging.getLogger("im_gateway.qq_napcat")

CQ_IMAGE_RE = re.compile(r"\[CQ:image,[^\]]*(?:url|file)=([^,\]]+)[^\]]*\]")
CQ_ANY_RE = re.compile(r"\[CQ:[^\]]+\]")


class QQNapCatAdapter(IMAdapter):
    """对接 NapCatQQ OneBot v11：支持普通 QQ 群全量监听、私聊收发图文/位置与申请好友。"""

    platform = "qq"

    def __init__(
        self,
        ws_url: Optional[str] = None,
        http_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 15.0,
    ):
        self.ws_url = (ws_url or os.getenv("NAPCAT_WS_URL") or "ws://127.0.0.1:3001").strip()
        self.http_url = (http_url or os.getenv("NAPCAT_HTTP_URL") or "http://127.0.0.1:3002").rstrip("/")
        self.token = (token if token is not None else os.getenv("NAPCAT_TOKEN", "")).strip()
        self.timeout = timeout
        self.ws_connected: bool = False
        self.reverse_ws_connected: bool = False
        self.last_heartbeat_at: float = 0.0
        self.last_webhook_at: float = 0.0
        self.self_id: str = ""
        self.nickname: str = ""

    def record_event_heartbeat(self, event: Dict[str, Any], source: str = "webhook") -> None:
        now = time.time()
        self.last_heartbeat_at = now
        if source == "webhook":
            self.last_webhook_at = now
        if event.get("self_id"):
            self.self_id = str(event["self_id"])

    async def get_status(self) -> Dict[str, Any]:
        """探测 NapCatQQ HTTP API 与 WS/Webhook 双模在线状态。"""
        http_online = False
        err_msg = ""
        try:
            url = f"{self.http_url}/get_login_info"
            async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
                resp = await client.post(url, json={}, headers=self._headers())
                if resp.status_code < 400:
                    body = resp.json()
                    data = body.get("data") if isinstance(body, dict) else {}
                    if isinstance(data, dict):
                        if data.get("user_id"):
                            self.self_id = str(data["user_id"])
                        if data.get("nickname"):
                            self.nickname = str(data["nickname"])
                    http_online = True
                else:
                    err_msg = f"HTTP {resp.status_code}"
        except Exception as exc:
            err_msg = str(exc)

        recent_heartbeat = (time.time() - self.last_heartbeat_at) < 120 if self.last_heartbeat_at else False
        online = http_online or self.ws_connected or self.reverse_ws_connected or recent_heartbeat
        return {
            "online": online,
            "http_online": http_online,
            "ws_connected": self.ws_connected,
            "reverse_ws_connected": self.reverse_ws_connected,
            "recent_webhook": recent_heartbeat,
            "http_url": self.http_url,
            "ws_url": self.ws_url,
            "self_id": self.self_id,
            "nickname": self.nickname,
            "last_heartbeat_at": self.last_heartbeat_at or None,
            "error": "" if online else err_msg,
        }

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _call_api(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.http_url}/{action.lstrip('/')}"
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def parse_onebot_event(event: Dict[str, Any]) -> Optional[IncomingMessage]:
        """将 OneBot v11 消息事件（支持 JSON 数组消息段与 CQ 码字符串）标准化为 IncomingMessage。"""
        if event.get("post_type") != "message":
            return None

        msg_type = event.get("message_type")
        if msg_type not in ("group", "private"):
            return None

        user_id = str(event.get("user_id") or "")
        if not user_id:
            return None

        sender = event.get("sender") or {}
        nickname = str(sender.get("card") or sender.get("nickname") or user_id)
        group_id = str(event["group_id"]) if msg_type == "group" and event.get("group_id") is not None else None

        text_parts: List[str] = []
        images: List[str] = []

        message = event.get("message")
        if isinstance(message, list):
            for seg in message:
                if not isinstance(seg, dict):
                    continue
                seg_type = seg.get("type")
                seg_data = seg.get("data") or {}
                if seg_type == "text":
                    t = str(seg_data.get("text") or "")
                    if t:
                        text_parts.append(t)
                elif seg_type == "image":
                    img_ref = seg_data.get("url") or seg_data.get("file")
                    if img_ref:
                        images.append(str(img_ref))
        else:
            raw_str = str(message if message is not None else event.get("raw_message") or "")
            for match in CQ_IMAGE_RE.finditer(raw_str):
                images.append(match.group(1))
            clean_text = CQ_ANY_RE.sub("", raw_str).strip()
            if clean_text:
                text_parts.append(clean_text)

        return IncomingMessage(
            platform="qq",
            chat_type=msg_type,
            group_id=group_id,
            user_id=user_id,
            nickname=nickname,
            text="".join(text_parts).strip(),
            images=images,
            raw=event,
        )

    async def send_text(
        self,
        target_id: str,
        text: str,
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        segments = [{"type": "text", "data": {"text": text}}]
        if chat_type == "group":
            return await self._call_api(
                "send_group_msg",
                {"group_id": int(target_id), "message": segments},
            )
        return await self._call_api(
            "send_private_msg",
            {"user_id": int(target_id), "message": segments},
        )

    async def send_image(
        self,
        target_id: str,
        image: Union[str, Path, bytes],
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        if isinstance(image, bytes):
            file_spec = f"base64://{base64.b64encode(image).decode('ascii')}"
        elif isinstance(image, Path):
            file_spec = f"base64://{base64.b64encode(image.read_bytes()).decode('ascii')}"
        else:
            s = str(image)
            if s.startswith(("http://", "https://", "base64://", "file://")):
                file_spec = s
            else:
                p = Path(s)
                file_spec = f"base64://{base64.b64encode(p.read_bytes()).decode('ascii')}" if p.exists() else s

        segments = [{"type": "image", "data": {"file": file_spec}}]
        if chat_type == "group":
            return await self._call_api(
                "send_group_msg",
                {"group_id": int(target_id), "message": segments},
            )
        return await self._call_api(
            "send_private_msg",
            {"user_id": int(target_id), "message": segments},
        )

    async def send_location(
        self,
        target_id: str,
        lat: float,
        lng: float,
        title: str,
        address: str,
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        segments = [
            {
                "type": "location",
                "data": {
                    "lat": str(lat),
                    "lon": str(lng),
                    "title": title,
                    "content": address,
                },
            }
        ]
        if chat_type == "group":
            return await self._call_api(
                "send_group_msg",
                {"group_id": int(target_id), "message": segments},
            )
        return await self._call_api(
            "send_private_msg",
            {"user_id": int(target_id), "message": segments},
        )

    async def request_add_friend(
        self,
        user_id: str,
        remark: str,
        group_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """调用 NapCatQQ 扩展好友申请接口（支持携带来源群号与定制验证话术）。"""
        payload: Dict[str, Any] = {
            "user_id": int(user_id),
            "comment": remark,
            "remark": remark,
        }
        if group_id:
            payload["group_id"] = int(group_id)
        return await self._call_api("send_friend_add_request", payload)

    async def listen_ws(
        self,
        on_message: Callable[[IncomingMessage], Awaitable[Any]],
        stop_event: Optional[asyncio.Event] = None,
        reconnect_delay: float = 5.0,
    ) -> None:
        """持续监听 NapCatQQ OneBot v11 正向 WebSocket 事件流，支持心跳与断线重连。"""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        while stop_event is None or not stop_event.is_set():
            try:
                logger.info("连接 NapCatQQ WebSocket: %s", self.ws_url)
                async with websockets.connect(
                    self.ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    self.ws_connected = True
                    self.last_heartbeat_at = time.time()
                    async for raw_msg in ws:
                        if stop_event is not None and stop_event.is_set():
                            break
                        try:
                            event = json.loads(raw_msg)
                            self.record_event_heartbeat(event, source="ws")
                            if event.get("post_type") == "meta_event":
                                continue
                            incoming = self.parse_onebot_event(event)
                            if incoming is not None:
                                await on_message(incoming)
                        except Exception as exc:
                            logger.error("处理 NapCatQQ 消息异常: %s", exc)
            except asyncio.CancelledError:
                self.ws_connected = False
                break
            except Exception as exc:
                self.ws_connected = False
                logger.warning("NapCatQQ WS 连接中断，%.1fs 后重连: %s", reconnect_delay, exc)
                await asyncio.sleep(reconnect_delay)
        self.ws_connected = False
