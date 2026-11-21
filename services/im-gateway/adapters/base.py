"""统一 IM 通道抽象层：定义标准消息模型与 IMAdapter 抽象基类。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


@dataclass
class IncomingMessage:
    """跨平台标准化入站消息事件。"""

    platform: str                   # "qq" | "wechat" | "wecom_kf"
    chat_type: str                  # "group" | "private"
    group_id: Optional[str]         # 群聊 ID/群名（私聊为 None）
    user_id: str                    # 平台内用户唯一标识（QQ号/微信昵称微信号/企微external_userid）
    nickname: str = ""
    text: str = ""
    images: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


class IMAdapter(ABC):
    """统一 IM 通道抽象接口，屏蔽 QQ / 微信 / 企业微信底层协议差异。"""

    platform: str = "unknown"

    @abstractmethod
    async def send_text(
        self,
        target_id: str,
        text: str,
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        """发送纯文本消息。"""

    @abstractmethod
    async def send_image(
        self,
        target_id: str,
        image: Union[str, Path, bytes],
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        """发送图片（支持本地路径、URL 或原始二进制 bytes）。"""

    @abstractmethod
    async def send_location(
        self,
        target_id: str,
        lat: float,
        lng: float,
        title: str,
        address: str,
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        """发送位置卡片或位置引导文本。"""

    @abstractmethod
    async def request_add_friend(
        self,
        user_id: str,
        remark: str,
        group_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """发起好友申请（附带定制化验证话术）。"""

    async def send_gallery_link(
        self,
        target_id: str,
        title: str,
        desc: str,
        gallery_url: str,
        thumb_media_id: str = "",
    ) -> Dict[str, Any]:
        """默认以图文链接文本下发选图短链；企业微信客服等支持卡片的通道可覆写为结构化卡片。"""
        msg = f"✨ {title}\n{desc}\n👉 点击在线选图：{gallery_url}"
        return await self.send_text(target_id, msg, chat_type="private")

    # ---- 同步便捷封装（供非 async 脚本或 CLI 调用） ----
    def send_text_sync(self, target_id: str, text: str, chat_type: str = "private") -> Dict[str, Any]:
        return asyncio.run(self.send_text(target_id, text, chat_type=chat_type))

    def send_image_sync(
        self,
        target_id: str,
        image: Union[str, Path, bytes],
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        return asyncio.run(self.send_image(target_id, image, chat_type=chat_type))

    def send_location_sync(
        self,
        target_id: str,
        lat: float,
        lng: float,
        title: str,
        address: str,
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        return asyncio.run(self.send_location(target_id, lat, lng, title, address, chat_type=chat_type))

    def request_add_friend_sync(
        self,
        user_id: str,
        remark: str,
        group_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return asyncio.run(self.request_add_friend(user_id, remark, group_id=group_id))
