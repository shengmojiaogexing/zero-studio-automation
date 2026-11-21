"""企业微信客服 (WeCom KF) 适配器预留实现：完整封装 token 缓存、回调验签解密与 kf/send_msg。"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import struct
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .base import IMAdapter, IncomingMessage

logger = logging.getLogger("im_gateway.wecom_kf")

WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"


class WeComKFAdapter(IMAdapter):
    """
    企业微信客服 Adapter：
    - access_token 内存缓存与提前刷新（提前 300 秒过期）；
    - 标准 SHA-1 回调签名校验 + AES-256-CBC 回调密文解密；
    - kf/send_msg 支持文本、图片、位置、以及选图 H5 链接卡片（msgtype=link）；
    配置 WECOM_CORP_ID、WECOM_KF_SECRET（及可选 WECOM_KF_TOKEN / WECOM_OPEN_KFID）即可无缝切换。
    """

    platform = "wecom_kf"

    def __init__(
        self,
        corp_id: Optional[str] = None,
        kf_secret: Optional[str] = None,
        token: Optional[str] = None,
        encoding_aes_key: Optional[str] = None,
        open_kfid: Optional[str] = None,
        timeout: float = 15.0,
    ):
        self.corp_id = (corp_id if corp_id is not None else os.getenv("WECOM_CORP_ID", "")).strip()
        self.kf_secret = (kf_secret if kf_secret is not None else os.getenv("WECOM_KF_SECRET", "")).strip()
        self.token = (token if token is not None else os.getenv("WECOM_KF_TOKEN", "")).strip()
        self.encoding_aes_key = (
            encoding_aes_key if encoding_aes_key is not None else os.getenv("WECOM_KF_ENCODING_AES_KEY", "")
        ).strip()
        self.open_kfid = (open_kfid if open_kfid is not None else os.getenv("WECOM_OPEN_KFID", "")).strip()
        self.timeout = timeout

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @property
    def is_configured(self) -> bool:
        return bool(self.corp_id and self.kf_secret)

    # ==================== 1. access_token 缓存 ====================

    async def get_access_token(self, force_refresh: bool = False) -> str:
        """获取并缓存企业微信客服 access_token。"""
        now = time.time()
        if not force_refresh and self._access_token and now < self._token_expires_at:
            return self._access_token

        if not self.is_configured:
            raise RuntimeError("企业微信客服未配置 WECOM_CORP_ID 或 WECOM_KF_SECRET")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{WECOM_API_BASE}/gettoken",
                params={"corpid": self.corp_id, "corpsecret": self.kf_secret},
            )
            resp.raise_for_status()
            data = resp.json()

        if data.get("errcode", 0) != 0 or not data.get("access_token"):
            raise RuntimeError(f"获取企微 access_token 失败: {data}")

        self._access_token = str(data["access_token"])
        expires_in = int(data.get("expires_in", 7200))
        self._token_expires_at = now + max(expires_in - 300, 60)
        return self._access_token

    # ==================== 2. 回调签名校验与密文解密 ====================

    def compute_signature(self, timestamp: str, nonce: str, encrypt_or_echostr: str = "") -> str:
        """计算企业微信回调标准 SHA-1 签名。"""
        parts = sorted([self.token, str(timestamp), str(nonce), str(encrypt_or_echostr)])
        return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    def verify_signature(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        encrypt_or_echostr: str = "",
    ) -> bool:
        """校验企微回调请求头/参数中的 msg_signature。"""
        if not self.token or not msg_signature:
            return False
        expected = self.compute_signature(timestamp, nonce, encrypt_or_echostr)
        return expected == str(msg_signature).strip()

    def decrypt_message(self, encrypted_b64: str) -> Tuple[str, str]:
        """
        使用 AES-256-CBC 解密企微回调密文（echostr 或 Encrypt 节点）。
        返回 (plaintext_str, receive_corp_id)。
        """
        if not self.encoding_aes_key:
            raise RuntimeError("未配置 WECOM_KF_ENCODING_AES_KEY，无法解密密文")
        aes_key = base64.b64decode(self.encoding_aes_key + "=")
        if len(aes_key) != 32:
            raise ValueError("Invalid WECOM_KF_ENCODING_AES_KEY length")

        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_key[:16]))
        decryptor = cipher.decryptor()
        raw_padded = decryptor.update(base64.b64decode(encrypted_b64)) + decryptor.finalize()

        # PKCS#7 unpad (32-byte block)
        pad_len = raw_padded[-1]
        if pad_len < 1 or pad_len > 32:
            raise ValueError("Invalid PKCS7 padding in WeCom payload")
        content = raw_padded[:-pad_len]

        msg_len = struct.unpack("!I", content[16:20])[0]
        msg_bytes = content[20: 20 + msg_len]
        recv_corp_id = content[20 + msg_len:].decode("utf-8", errors="ignore")
        if self.corp_id and recv_corp_id != self.corp_id:
            raise ValueError(f"CorpID mismatch: expected {self.corp_id}, got {recv_corp_id}")
        return msg_bytes.decode("utf-8"), recv_corp_id

    def verify_url_callback(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
    ) -> str:
        """处理企微后台配置回调 URL 时的 GET 验签（若配置 AESKey 则自动解密返回明文）。"""
        if not self.verify_signature(msg_signature, timestamp, nonce, echostr):
            raise ValueError("Invalid WeCom callback signature")
        if self.encoding_aes_key:
            plaintext, _ = self.decrypt_message(echostr)
            return plaintext
        return echostr

    @staticmethod
    def parse_kf_sync_message(item: Dict[str, Any]) -> Optional[IncomingMessage]:
        """将企微 kf/sync_msg 拉取到的单条客户消息标准化为 IncomingMessage。"""
        if item.get("origin") != 3:  # 3 = 微信客户发送
            return None
        external_userid = str(item.get("external_userid") or "")
        if not external_userid:
            return None
        msgtype = item.get("msgtype")
        text = ""
        images: list[str] = []
        if msgtype == "text":
            text = str((item.get("text") or {}).get("content") or "").strip()
        elif msgtype == "image":
            media_id = (item.get("image") or {}).get("media_id")
            if media_id:
                images.append(f"wecom_media://{media_id}")
        return IncomingMessage(
            platform="wecom_kf",
            chat_type="private",
            group_id=None,
            user_id=external_userid,
            nickname=external_userid,
            text=text,
            images=images,
            raw=item,
        )

    @staticmethod
    def extract_encrypt_from_xml(xml_text: str) -> str:
        root = ET.fromstring(xml_text)
        node = root.find("Encrypt")
        if node is None or not node.text:
            raise ValueError("Missing <Encrypt> node in WeCom callback XML")
        return node.text.strip()

    # ==================== 3. kf/send_msg 消息体构造与发送 ====================

    def build_kf_send_msg_payload(
        self,
        touser: str,
        msgtype: str,
        body: Dict[str, Any],
        open_kfid: Optional[str] = None,
        msgid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """构造标准企业微信客服 POST /cgi-bin/kf/send_msg 请求体。"""
        kfid = (open_kfid or self.open_kfid).strip()
        if not kfid:
            raise ValueError("Missing open_kfid (configure WECOM_OPEN_KFID or pass explicitly)")
        payload: Dict[str, Any] = {
            "touser": touser,
            "open_kfid": kfid,
            "msgtype": msgtype,
            msgtype: body,
        }
        if msgid:
            payload["msgid"] = msgid
        return payload

    def build_text_payload(self, touser: str, text: str, open_kfid: Optional[str] = None) -> Dict[str, Any]:
        return self.build_kf_send_msg_payload(touser, "text", {"content": text}, open_kfid=open_kfid)

    def build_image_payload(self, touser: str, media_id: str, open_kfid: Optional[str] = None) -> Dict[str, Any]:
        return self.build_kf_send_msg_payload(touser, "image", {"media_id": media_id}, open_kfid=open_kfid)

    def build_gallery_link_card_payload(
        self,
        touser: str,
        title: str,
        desc: str,
        gallery_url: str,
        thumb_media_id: str = "",
        open_kfid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """构造选图 H5 链接卡片（企微客服 msgtype=link）。"""
        link_body: Dict[str, Any] = {
            "title": title,
            "desc": desc,
            "url": gallery_url,
        }
        if thumb_media_id:
            link_body["thumb_media_id"] = thumb_media_id
        return self.build_kf_send_msg_payload(touser, "link", link_body, open_kfid=open_kfid)

    def build_location_payload(
        self,
        touser: str,
        lat: float,
        lng: float,
        title: str,
        address: str,
        open_kfid: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.build_kf_send_msg_payload(
            touser,
            "location",
            {
                "name": title,
                "address": address,
                "latitude": float(lat),
                "longitude": float(lng),
            },
            open_kfid=open_kfid,
        )

    async def upload_temp_image(self, image: Union[str, Path, bytes], filename: str = "upload.png") -> str:
        """上传临时图片素材到企业微信，返回 media_id。"""
        token = await self.get_access_token()
        if isinstance(image, bytes):
            content = image
        else:
            p = Path(image)
            filename = p.name
            content = p.read_bytes()

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{WECOM_API_BASE}/media/upload",
                params={"access_token": token, "type": "image"},
                files={"media": (filename, content, "image/png")},
            )
            resp.raise_for_status()
            data = resp.json()

        if data.get("errcode", 0) != 0 or not data.get("media_id"):
            raise RuntimeError(f"企微上传图片素材失败: {data}")
        return str(data["media_id"])

    async def _post_kf_send_msg(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        token = await self.get_access_token()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{WECOM_API_BASE}/kf/send_msg",
                params={"access_token": token},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"企微 kf/send_msg 发送失败: {data}")
        return data

    async def send_text(
        self,
        target_id: str,
        text: str,
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        payload = self.build_text_payload(target_id, text)
        return await self._post_kf_send_msg(payload)

    async def send_image(
        self,
        target_id: str,
        image: Union[str, Path, bytes],
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        if isinstance(image, str) and image.startswith("media_id:"):
            media_id = image.split("media_id:", 1)[1]
        else:
            media_id = await self.upload_temp_image(image)
        payload = self.build_image_payload(target_id, media_id)
        return await self._post_kf_send_msg(payload)

    async def send_location(
        self,
        target_id: str,
        lat: float,
        lng: float,
        title: str,
        address: str,
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        payload = self.build_location_payload(target_id, lat, lng, title, address)
        return await self._post_kf_send_msg(payload)

    async def send_gallery_link(
        self,
        target_id: str,
        title: str,
        desc: str,
        gallery_url: str,
        thumb_media_id: str = "",
    ) -> Dict[str, Any]:
        """以企业微信客服原生链接卡片（msgtype=link）推送客户选图 H5。"""
        payload = self.build_gallery_link_card_payload(
            touser=target_id,
            title=title,
            desc=desc,
            gallery_url=gallery_url,
            thumb_media_id=thumb_media_id,
        )
        return await self._post_kf_send_msg(payload)

    async def request_add_friend(
        self,
        user_id: str,
        remark: str,
        group_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """企业微信客服无需加好友即可直接会话，返回说明状态。"""
        return {
            "status": "skipped",
            "platform": self.platform,
            "reason": "WeCom KF communicates directly via external_userid without friend requests",
            "user_id": user_id,
        }
