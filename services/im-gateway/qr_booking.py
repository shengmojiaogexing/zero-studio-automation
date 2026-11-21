"""二维码识别：把客户发来的预约截图里的二维码解出来，直接拿到订单。

客户在官网预约成功后会截图发给客服，截图里那个二维码的内容就是
`ZERO-STUDIO-BOOKING|...` 串（或旧版的 JSON）。解出订单号后即可直接
从预约库取回完整信息，客户完全不用再报一遍。

依赖：opencv-python（cv2）。未安装时自动降级，不影响主流程。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger("im_gateway.qr")

BOOKING_QR_PREFIX = "ZERO-STUDIO-BOOKING"
# 二维码里包含 gallery_token 时，可据此直接查订单
TOKEN_RE = re.compile(r"(?:^|[;|])(?:token|gallery_token)=([0-9a-zA-Z]{8,64})")
BOOKING_URL_RE = re.compile(r"/booking-qr/([0-9a-zA-Z]{8,64})")
# 官网二维码图片路径（客户可能直接发链接而非截图）
QR_URL_HINT_RE = re.compile(r"/booking-qr/", re.IGNORECASE)


def _cv2():
    try:
        import cv2  # noqa: PLC0415

        return cv2
    except Exception:  # noqa: BLE001
        return None


async def download_image_ref(ref: str, timeout: float = 12.0) -> Optional[bytes]:
    """把 QQ/微信的图片引用（http/https URL 或本地路径）读成 bytes。"""
    if not ref:
        return None
    if ref.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=True) as client:
                r = await client.get(ref)
                if r.status_code == 200 and r.content:
                    return r.content
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载图片失败 %s: %s", ref[:80], exc)
        return None
    # 本地路径
    try:
        from pathlib import Path

        p = Path(ref)
        if p.exists() and p.is_file():
            return p.read_bytes()
    except Exception:  # noqa: BLE001
        pass
    return None


def decode_qr_texts(image_bytes: bytes) -> List[str]:
    """从图片里解出所有二维码文本（支持二维码只是截图的一小部分）。"""
    cv2 = _cv2()
    if cv2 is None or not image_bytes:
        return []
    try:
        import numpy as np  # noqa: PLC0415

        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return []

        detector = cv2.QRCodeDetector()
        found: List[str] = []

        def collect(mat) -> bool:
            try:
                result = detector.detectAndDecodeMulti(mat)
            except Exception:  # noqa: BLE001
                return False
            # OpenCV 4.x/5.x 返回 (ok, decoded, points, straight_qrcode)
            decoded = result[1] if len(result) > 1 else ()
            vals = [d for d in decoded if d] if decoded else []
            if vals:
                found.extend(vals)
                return True
            return False

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # 1) 原图 / 灰度
        if collect(img) or collect(gray):
            return list(dict.fromkeys(found))

        # 2) 二值化（截图压缩后灰度会被插值污染）
        for thresh in (127, 160, 200):
            _, binary = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY)
            if collect(cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)):
                return list(dict.fromkeys(found))
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if collect(cv2.cvtColor(otsu, cv2.COLOR_GRAY2BGR)):
            return list(dict.fromkeys(found))

        # 3) 放大（截图里的二维码往往很小）
        h, w = gray.shape[:2]
        for scale in (2, 3):
            big = cv2.resize(gray, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
            if collect(big):
                return list(dict.fromkeys(found))

        # 4) 先定位再裁剪放大（二维码在截图里只占一小块时最有效）
        try:
            ok, pts = detector.detect(gray)
        except Exception:  # noqa: BLE001
            ok, pts = False, None
        if ok and pts is not None:
            p = np.array(pts).reshape(-1, 2)
            x0, y0 = p.min(axis=0).astype(int)
            x1, y1 = p.max(axis=0).astype(int)
            m = max(20, int((x1 - x0) * 0.15))
            y0, x0 = max(0, y0 - m), max(0, x0 - m)
            y1, x1 = min(h, y1 + m), min(w, x1 + m)
            crop = gray[y0:y1, x0:x1]
            if crop.size:
                for fx in (3, 4, 6):
                    big = cv2.resize(crop, None, fx=fx, fy=fx, interpolation=cv2.INTER_NEAREST)
                    if collect(big):
                        return list(dict.fromkeys(found))

        # 5) 滑动裁剪兜底（二维码位置不固定时）
        for fy in (0.10, 0.25, 0.40, 0.55):
            for fx in (0.10, 0.30, 0.50):
                ch, cw = int(h * 0.35), int(w * 0.55)
                y, x = int(h * fy), int(w * fx)
                crop = gray[y:y + ch, x:x + cw]
                if crop.size == 0:
                    continue
                big = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
                if collect(big):
                    return list(dict.fromkeys(found))

        return list(dict.fromkeys(found))
    except Exception as exc:  # noqa: BLE001
        logger.warning("二维码解码异常: %s", exc)
        return []


def extract_booking_token(qr_text: str) -> Optional[str]:
    """从二维码文本里取出能查到订单的标识（gallery_token）。"""
    if not qr_text:
        return None
    text = qr_text.strip()

    # 形如 ZERO-STUDIO-BOOKING|v=2;order=4;...;gallery_token=xxxx;
    if BOOKING_QR_PREFIX in text:
        m = TOKEN_RE.search(text)
        return m.group(1) if m else None

    # 客户可能发的是链接：/booking-qr/<token> 或 /api/booking-info/<token>
    if text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        m = BOOKING_URL_RE.search(parsed.path)
        if m:
            return m.group(1)
        qs = parse_qs(parsed.query)
        for key in ("token", "gallery_token"):
            if qs.get(key):
                return qs[key][0]
        return None

    # 旧版 JSON 场记码：只有 booking_id，没有 token
    if text.lstrip().startswith("{"):
        try:
            import json  # noqa: PLC0415

            data = json.loads(text)
            if isinstance(data, dict):
                for key in ("gallery_token", "token"):
                    if data.get(key):
                        return str(data[key])
        except Exception:  # noqa: BLE001
            return None
    return None


def extract_order_id(qr_text: str) -> Optional[int]:
    """兼容只有订单号、没有 token 的旧二维码。"""
    if not qr_text or BOOKING_QR_PREFIX not in qr_text:
        return None
    m = re.search(r"(?:^|;|\|)order=(\d+)", qr_text)
    return int(m.group(1)) if m else None


async def decode_booking_from_images(image_refs: List[str]) -> Optional[Dict[str, Any]]:
    """从一组图片里找出预约二维码，返回 {token, order_id, qr_text}。"""
    for ref in image_refs or []:
        raw = await download_image_ref(ref)
        if not raw:
            continue
        for text in decode_qr_texts(raw):
            token = extract_booking_token(text)
            order_id = extract_order_id(text)
            if token or order_id:
                logger.info("从图片解出预约二维码: token=%s order=%s", token, order_id)
                return {"token": token, "order_id": order_id, "qr_text": text}
    return None
