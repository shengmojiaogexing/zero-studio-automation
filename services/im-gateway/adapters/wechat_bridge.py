"""对外微信 4.x HTTP/Webhook 桥接适配器 (WECHAT_MODE=bridge_http)。

兼容 `openclaw-wechat-channel` / `wechatbot-new` / `gewechat` 等新一代微信 4.x 桥接服务：
- 出站：通过 RESTful HTTP API (`WECHAT_BRIDGE_URL`) 发送文本、图片、位置与好友申请；
- 入站：提供 `parse_webhook_event()`，把外部桥接服务回调的 JSON 标准化为 `IncomingMessage`，
  直接送入 `im-gateway` 的本地 2B/4B 守门员 + DeepSeek 接单大脑。
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import httpx

from .base import IMAdapter, IncomingMessage

logger = logging.getLogger("im_gateway.adapters.wechat_bridge")


class WeChatHTTPBridgeAdapter(IMAdapter):
    """通用微信 4.x HTTP/Webhook 桥接适配器（彻底解耦旧版 wxauto UIA 限制）。"""

    platform = "wechat"

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 20.0,
        mock_http_fn: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self.base_url = (
            base_url if base_url is not None else os.getenv("WECHAT_BRIDGE_URL", "http://127.0.0.1:8095")
        ).strip().rstrip("/")
        self.token = (token if token is not None else os.getenv("WECHAT_BRIDGE_TOKEN", "")).strip()
        self.timeout = float(timeout)
        self.mock_http_fn = mock_http_fn

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
            h["X-Bridge-Token"] = self.token
        return h

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.mock_http_fn is not None:
            return self.mock_http_fn(path, payload)
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError:
                return {"status": "ok", "raw": resp.text[:300]}

    async def send_text(
        self,
        target_id: str,
        text: str,
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        return await self._post(
            "/api/send/text",
            {"to": str(target_id), "chat_type": chat_type, "text": str(text)},
        )

    async def send_image(
        self,
        target_id: str,
        image: Union[str, Path, bytes],
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"to": str(target_id), "chat_type": chat_type}
        if isinstance(image, bytes):
            payload["image_base64"] = base64.b64encode(image).decode("ascii")
        elif isinstance(image, Path) or (isinstance(image, str) and not image.startswith(("http://", "https://"))):
            p = Path(image)
            payload["image_path"] = str(p)
            if p.exists() and p.is_file():
                payload["image_base64"] = base64.b64encode(p.read_bytes()).decode("ascii")
        else:
            payload["image_url"] = str(image)
        return await self._post("/api/send/image", payload)

    async def send_location(
        self,
        target_id: str,
        lat: float,
        lng: float,
        title: str,
        address: str,
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        text = f"📍 {title}\n地址：{address}\n坐标：{lat:.5f}, {lng:.5f}"
        return await self.send_text(target_id, text, chat_type=chat_type)

    async def request_add_friend(
        self,
        user_id: str,
        remark: str,
        group_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._post(
            "/api/friend/add",
            {"user_id": str(user_id), "remark": str(remark), "group_id": group_id or ""},
        )

    @staticmethod
    def parse_webhook_event(data: Dict[str, Any]) -> Optional[IncomingMessage]:
        """将外部微信 4.x 桥接 Webhook 负载统一解析为 IncomingMessage。"""
        if not isinstance(data, dict):
            return None
        user_id = str(data.get("user_id") or data.get("sender_id") or data.get("from") or "").strip()
        if not user_id:
            return None
        group_id = str(data.get("group_id") or data.get("room_id") or "").strip() or None
        chat_type = str(data.get("chat_type") or ("group" if group_id else "private")).strip().lower()
        if chat_type not in ("group", "private"):
            chat_type = "group" if group_id else "private"
        nickname = str(data.get("nickname") or data.get("sender_name") or user_id).strip()
        text = str(data.get("text") or data.get("content") or "").strip()
        raw_imgs = data.get("images") or ([] if not data.get("image_url") else [data.get("image_url")])
        images: List[str] = [str(x).strip() for x in raw_imgs if str(x).strip()]
        return IncomingMessage(
            platform="wechat",
            chat_type=chat_type,
            group_id=group_id,
            user_id=user_id,
            nickname=nickname,
            text=text,
            images=images,
            raw=data,
        )
