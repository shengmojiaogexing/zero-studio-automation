#!/usr/bin/env python3
"""
废片预筛、场记二维码身份锚点、GPU 多模态角色聚类（含合影多标签）与数据库回写模块。

处理管线：
1. 场记二维码识别（OpenCV QRCodeDetector，兼容 pyzbar）：
   解析首图二维码中的 booking_id，按拍摄时间戳自动切段并将后续连拍绑定到对应订单；
   场记二维码图本身标记 is_rejected = 1 (reject_reason = 'SLATE_QR')，避免展示给客户。
2. 废片预筛（OpenCV）：
   - 全图平均亮度 + 暗部/高光占比：过滤测光黑片与严重过曝死白片；
   - Laplacian 方差：过滤严重虚焦/脱焦废片；
   - 自动写入 is_rejected = 1 与具体 reject_reason。
3. 可插拔 GPU 多模态角色聚类与合影多标签：
   - 自动检测 NVIDIA CUDA（如 RTX 5070 Ti 12GB），未装 GPU 依赖时平滑回退 CPU；
   - 时间间隔分段 -> 服饰外观向量（SigLIP/CLIP 或 HSV 空间纹理回退）+ 多人人脸特征（InsightFace 或 OpenCV Haar 回退）
     -> 密度聚类（HDBSCAN 或 NumPy 余弦聚类回退）-> 对照 GET /api/internal/bookings/by-event 订单角色；
   - 单张有效图检测到多个已识别角色时，自动在 booking_ids 中写入多个 ID，回写 asset_booking_links 实现合影多标签。
4. 事务回写至 booking-site (POST /api/internal/batches) 并打印每个订单的选图短链 /gallery/{token}。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
import requests
from dotenv import load_dotenv

try:
    from pyzbar.pyzbar import decode as pyzbar_decode  # type: ignore
except ImportError:
    pyzbar_decode = None

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(WORKSPACE_ROOT / ".env", override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [cull_cluster] %(message)s",
)
logger = logging.getLogger("cull_and_cluster")


# ==================== 基础图像与设备工具 ====================

def cv2_imread_unicode(image_path: Path) -> Optional[np.ndarray]:
    """兼容 Windows 中文路径的 OpenCV 图像读取。"""
    if not image_path.exists():
        return None
    buf = np.fromfile(str(image_path), dtype=np.uint8)
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def detect_compute_device() -> Tuple[str, str]:
    """自动检测本机 NVIDIA CUDA（RTX 5070 Ti），缺省或未装 torch 时平滑回退 CPU。"""
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            return "cuda", f"CUDA ({gpu_name})"
        return "cpu", "CPU (PyTorch installed, CUDA unavailable)"
    except ImportError:
        return "cpu", "CPU fallback (PyTorch/GPU optional deps not installed)"


def parse_iso_timestamp(ts_str: Optional[str]) -> float:
    """将 ISO 时间戳转为 epoch 秒，解析失败返回 0.0。"""
    if not ts_str:
        return 0.0
    s = str(ts_str).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).timestamp()
        except ValueError:
            continue
    return 0.0


# ==================== 阶段 1：场记二维码识别与废片预筛 ====================

def detect_slate_qr(image_bgr: np.ndarray) -> Optional[Dict[str, Any]]:
    """
    识别场记二维码并解析 JSON（提取 booking_id / cn / character / event_name）。
    优先使用 OpenCV QRCodeDetector，兼容 pyzbar 回退。
    """
    detector = cv2.QRCodeDetector()
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)

    candidates: List[np.ndarray] = [image_bgr, gray]
    h, w = gray.shape[:2]
    if max(h, w) > 1024:
        scale = 1024.0 / max(h, w)
        small = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        candidates.append(small)

    # 二值化增强应对反光或弱对比场记屏
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    candidates.append(otsu)

    decoded_texts: List[str] = []
    for cand in candidates:
        try:
            text, _, _ = detector.detectAndDecode(cand)
            if text and text.strip():
                decoded_texts.append(text.strip())
                break
        except cv2.error:
            continue

    if not decoded_texts and pyzbar_decode is not None:
        try:
            for obj in pyzbar_decode(gray):
                txt = obj.data.decode("utf-8", errors="ignore").strip()
                if txt:
                    decoded_texts.append(txt)
                    break
        except Exception:
            pass

    for raw_text in decoded_texts:
        try:
            payload = json.loads(raw_text)
            if isinstance(payload, dict) and "booking_id" in payload:
                bid = int(payload["booking_id"])
                if bid > 0:
                    payload["booking_id"] = bid
                    return payload
        except (ValueError, TypeError):
            continue

    return None


_SUBJECT_HAAR_CASCADE: Optional[cv2.CascadeClassifier] = None


def _get_subject_haar() -> Optional[cv2.CascadeClassifier]:
    global _SUBJECT_HAAR_CASCADE
    if _SUBJECT_HAAR_CASCADE is None:
        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        if cascade_path.exists():
            _SUBJECT_HAAR_CASCADE = cv2.CascadeClassifier(str(cascade_path))
    return _SUBJECT_HAAR_CASCADE


def evaluate_technical_quality(
    image_bgr: np.ndarray,
    dark_mean_thresh: float = 25.0,
    dark_ratio_thresh: float = 0.85,
    subject_dark_mean_thresh: float = 38.0,
    subject_dark_p90_thresh: float = 65.0,
    bright_mean_thresh: float = 235.0,
    highlight_ratio_thresh: float = 0.85,
    blur_laplacian_thresh: float = 35.0,
) -> Dict[str, Any]:
    """
    废片预筛指标计算（Sprint 2.5 大光比/暗背景防误杀升级）：
    - 优先检测人脸区域或中心主体区域亮度，绝不仅凭全图平均亮度误杀“暗背景亮主体”场照；
    - 仅当【全图极暗 且 主体区域同样极暗】（测光黑片）或【主体/全图大面积死白】时才判曝光废片；
    - 清晰度取全图与主体区域 Laplacian 方差最大值，兼顾暗背景大光圈虚化人像。
    """
    h, w = image_bgr.shape[:2]
    max_edge = max(h, w)
    if max_edge > 1024:
        scale = 1024.0 / max_edge
        work_bgr = cv2.resize(image_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    else:
        work_bgr = image_bgr

    gray = cv2.cvtColor(work_bgr, cv2.COLOR_BGR2GRAY)
    wh, ww = gray.shape[:2]

    mean_brightness = float(np.mean(gray))
    dark_ratio = float(np.mean(gray < 15))
    highlight_ratio = float(np.mean(gray > 245))
    laplacian_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    # 优先检测人脸区域作为主体 ROI，未检测到时回退至中心主体区域 (15%~85% H, 20%~80% W)
    has_face_roi = False
    subject_mask = np.zeros((wh, ww), dtype=bool)
    haar = _get_subject_haar()
    if haar is not None:
        faces = haar.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(36, 36))
        if len(faces) > 0:
            has_face_roi = True
            for (fx, fy, fw, fh) in faces:
                pad_x = int(fw * 0.35)
                pad_y = int(fh * 0.35)
                x1, y1 = max(0, fx - pad_x), max(0, fy - pad_y)
                x2, y2 = min(ww, fx + fw + pad_x), min(wh, fy + fh + pad_y)
                subject_mask[y1:y2, x1:x2] = True

    if has_face_roi and np.any(subject_mask):
        subject_pixels = gray[subject_mask]
        ys, xs = np.where(subject_mask)
        subject_crop = gray[int(ys.min()): int(ys.max()) + 1, int(xs.min()): int(xs.max()) + 1]
    else:
        y1, y2 = int(wh * 0.15), int(wh * 0.85)
        x1, x2 = int(ww * 0.20), int(ww * 0.80)
        subject_crop = gray[y1:y2, x1:x2]
        subject_pixels = subject_crop.reshape(-1)

    subject_mean = float(np.mean(subject_pixels))
    subject_p90 = float(np.percentile(subject_pixels, 90))
    subject_highlight_ratio = float(np.mean(subject_pixels > 245))
    subject_laplacian_var = float(cv2.Laplacian(subject_crop, cv2.CV_64F).var()) if subject_crop.size > 0 else laplacian_var
    effective_laplacian = max(laplacian_var, subject_laplacian_var)

    is_rejected = 0
    reject_reason: Optional[str] = None

    full_is_dark = (mean_brightness < dark_mean_thresh) or (dark_ratio > dark_ratio_thresh)
    subject_is_dark = (subject_mean < subject_dark_mean_thresh) and (subject_p90 < subject_dark_p90_thresh)

    subject_is_blown = (subject_highlight_ratio > 0.80) and (subject_mean > bright_mean_thresh)
    full_is_blown = (mean_brightness > bright_mean_thresh) and (highlight_ratio > highlight_ratio_thresh)

    if full_is_dark and subject_is_dark:
        is_rejected = 1
        reject_reason = (
            f"UNDEREXPOSED_BLACK(mean={mean_brightness:.1f},dark={dark_ratio:.1%},"
            f"subj_mean={subject_mean:.1f},subj_p90={subject_p90:.1f})"
        )
    elif subject_is_blown or full_is_blown:
        is_rejected = 1
        reject_reason = (
            f"OVEREXPOSED_WHITE(mean={mean_brightness:.1f},hi={highlight_ratio:.1%},"
            f"subj_mean={subject_mean:.1f},subj_hi={subject_highlight_ratio:.1%})"
        )
    elif effective_laplacian < blur_laplacian_thresh:
        is_rejected = 1
        reject_reason = f"BLURRY_OUT_OF_FOCUS(lap_var={laplacian_var:.1f},subj_lap={subject_laplacian_var:.1f})"

    return {
        "is_rejected": is_rejected,
        "reject_reason": reject_reason,
        "metrics": {
            "mean_brightness": round(mean_brightness, 2),
            "dark_ratio": round(dark_ratio, 4),
            "highlight_ratio": round(highlight_ratio, 4),
            "laplacian_var": round(laplacian_var, 2),
            "subject_mean": round(subject_mean, 2),
            "subject_p90": round(subject_p90, 2),
            "subject_laplacian_var": round(subject_laplacian_var, 2),
            "has_face_roi": has_face_roi,
        },
    }


# ==================== 阶段 2 & 3：可插拔多模态特征提取与聚类管线 ====================

class CostumeFeatureExtractor:
    """
    服饰/角色外观特征提取器（可插拔设计）：
    - 优先：SigLIP (transformers) 或 OpenCLIP（自动运行在 RTX 5070 Ti CUDA 上）
    - 回退：OpenCV HSV 颜色直方图 + 空间分块纹理向量（CPU 毫秒级零 ML 依赖）
    """

    def __init__(self, device: str = "cpu", enable_ml: bool = True):
        self.device = device
        self.backend_name = "hsv_spatial_fallback"
        self._model = None
        self._processor = None
        self._openclip_preprocess = None

        if enable_ml and device == "cuda":
            self._try_load_ml_backend()

    def _try_load_ml_backend(self) -> None:
        # 优先尝试 SigLIP (HuggingFace transformers)
        try:
            import torch  # type: ignore
            from transformers import AutoModel, AutoProcessor  # type: ignore

            model_id = os.getenv("SIGLIP_MODEL_ID", "google/siglip-base-patch16-384")
            self._processor = AutoProcessor.from_pretrained(model_id, local_files_only=True)
            self._model = AutoModel.from_pretrained(model_id, local_files_only=True).to(self.device).eval()
            self.backend_name = f"siglip:{model_id}@{self.device}"
            logger.info("已启用 GPU SigLIP 服饰向量后端: %s", self.backend_name)
            return
        except Exception:
            pass

        # 其次尝试 OpenCLIP
        try:
            import open_clip  # type: ignore

            model_name = os.getenv("OPENCLIP_MODEL", "ViT-B-32")
            model, _, preprocess = open_clip.create_model_and_transforms(model_name, pretrained="laion2b_s34b_b79k")
            self._model = model.to(self.device).eval()
            self._openclip_preprocess = preprocess
            self.backend_name = f"open_clip:{model_name}@{self.device}"
            logger.info("已启用 GPU OpenCLIP 服饰向量后端: %s", self.backend_name)
        except Exception:
            logger.info("未检测到本地缓存的 SigLIP/CLIP 权重或可选包，使用轻量 HSV 空间特征回退。")

    def extract(self, image_bgr: np.ndarray) -> np.ndarray:
        if self.backend_name.startswith("siglip:") and self._model is not None:
            import torch  # type: ignore
            from PIL import Image

            rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
            inputs = self._processor(images=Image.fromarray(rgb), return_tensors="pt").to(self.device)
            with torch.no_grad():
                feats = self._model.get_image_features(**inputs)
                feats = feats / feats.norm(dim=-1, keepdim=True).clamp(min=1e-8)
            return feats[0].detach().cpu().numpy().astype(np.float32)

        if self.backend_name.startswith("open_clip:") and self._model is not None:
            import torch  # type: ignore
            from PIL import Image

            rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
            tensor = self._openclip_preprocess(Image.fromarray(rgb)).unsqueeze(0).to(self.device)
            with torch.no_grad():
                feats = self._model.encode_image(tensor)
                feats = feats / feats.norm(dim=-1, keepdim=True).clamp(min=1e-8)
            return feats[0].detach().cpu().numpy().astype(np.float32)

        # CPU 轻量回退：中心 60% 主体区域 HSV 直方图 + 2x2 空间颜色分布
        resized = cv2.resize(image_bgr, (256, 256), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
        parts: List[np.ndarray] = []

        # 中心主体权重更高（聚焦 Coser 服饰与假发）
        center = hsv[48:208, 48:208]
        h_center = cv2.calcHist([center], [0, 1, 2], None, [12, 4, 4], [0, 180, 0, 256, 0, 256]).flatten()
        parts.append(h_center * 1.5)

        for gy in (0, 128):
            for gx in (0, 128):
                patch = hsv[gy: gy + 128, gx: gx + 128]
                h_patch = cv2.calcHist([patch], [0, 1], None, [8, 4], [0, 180, 0, 256]).flatten()
                parts.append(h_patch)

        vec = np.concatenate(parts).astype(np.float32)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 1e-8 else vec


class FaceFeatureExtractor:
    """
    多人人脸检测与特征提取器（用于未打板角色归属与合影多标签）：
    - 优先：InsightFace (CUDAExecutionProvider / CPUExecutionProvider)
    - 回退：OpenCV Haar Cascade 人脸框检测 + 局部色彩纹理向量
    """

    def __init__(self, device: str = "cpu", enable_ml: bool = True):
        self.device = device
        self.backend_name = "opencv_haar_fallback"
        self._insight_app = None
        self._haar = None

        if enable_ml:
            self._try_load_insightface()

        if self._insight_app is None:
            cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            if cascade_path.exists():
                self._haar = cv2.CascadeClassifier(str(cascade_path))

    def _try_load_insightface(self) -> None:
        try:
            from insightface.app import FaceAnalysis  # type: ignore

            providers = (
                ["CUDAExecutionProvider", "CPUExecutionProvider"]
                if self.device == "cuda"
                else ["CPUExecutionProvider"]
            )
            app = FaceAnalysis(name="buffalo_l", providers=providers)
            app.prepare(ctx_id=0 if self.device == "cuda" else -1, det_size=(640, 640))
            self._insight_app = app
            self.backend_name = f"insightface:buffalo_l@{self.device}"
            logger.info("已启用 InsightFace 人脸特征后端: %s", self.backend_name)
        except Exception:
            pass

    def extract_faces(self, image_bgr: np.ndarray) -> List[Dict[str, Any]]:
        """返回图像中检测到的所有人脸列表，每项含 bbox 与 L2 归一化 embedding。"""
        if self._insight_app is not None:
            faces_out: List[Dict[str, Any]] = []
            try:
                detected = self._insight_app.get(image_bgr)
                for f in detected:
                    emb = np.asarray(f.normed_embedding, dtype=np.float32)
                    bbox = [int(v) for v in f.bbox.tolist()]
                    faces_out.append({"bbox": bbox, "embedding": emb})
            except Exception as exc:
                logger.warning("InsightFace 推理异常，跳过该图人脸提取: %s", exc)
            return faces_out

        if self._haar is None:
            return []

        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        rects = self._haar.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(48, 48))
        results: List[Dict[str, Any]] = []
        for (x, y, w, h) in rects:
            crop = image_bgr[y: y + h, x: x + w]
            if crop.size == 0:
                continue
            hsv_crop = cv2.cvtColor(cv2.resize(crop, (64, 64)), cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv_crop], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256]).flatten().astype(np.float32)
            norm = float(np.linalg.norm(hist))
            if norm > 1e-8:
                hist /= norm
            results.append({"bbox": [int(x), int(y), int(x + w), int(y + h)], "embedding": hist})
        return results


def segment_by_time_gap(indices_with_ts: List[Tuple[int, float]], gap_seconds: float = 300.0) -> List[List[int]]:
    """按拍摄时间间隔将未打板有效图切分为独立拍摄段落。"""
    if not indices_with_ts:
        return []
    ordered = sorted(indices_with_ts, key=lambda x: x[1])
    segments: List[List[int]] = [[ordered[0][0]]]
    prev_ts = ordered[0][1]
    for idx, ts in ordered[1:]:
        if prev_ts > 0 and ts > 0 and (ts - prev_ts) > gap_seconds:
            segments.append([idx])
        else:
            segments[-1].append(idx)
        prev_ts = ts
    return segments


def cluster_embeddings(embeddings: np.ndarray, cosine_threshold: float = 0.78) -> List[int]:
    """
    向量聚类：
    - 优先尝试 hdbscan.HDBSCAN（安装可选依赖且样本数 >= 4 时）
    - 回退：纯 NumPy 余弦相似度连通分量聚类
    """
    n = embeddings.shape[0]
    if n == 0:
        return []
    if n == 1:
        return [0]

    try:
        import hdbscan  # type: ignore

        if n >= 4:
            clusterer = hdbscan.HDBSCAN(min_cluster_size=2, metric="euclidean")
            labels = clusterer.fit_predict(embeddings).tolist()
            # 将噪声点 (-1) 分配独立簇号，保证不丢图
            next_label = max([l for l in labels if l >= 0], default=-1) + 1
            normalized_labels: List[int] = []
            for l in labels:
                if l < 0:
                    normalized_labels.append(next_label)
                    next_label += 1
                else:
                    normalized_labels.append(int(l))
            return normalized_labels
    except ImportError:
        pass
    except Exception:
        pass

    # Pure NumPy 余弦相似度连通分量聚类
    sim_matrix = embeddings @ embeddings.T
    visited = [False] * n
    labels = [-1] * n
    cur_label = 0
    for i in range(n):
        if visited[i]:
            continue
        queue = [i]
        visited[i] = True
        while queue:
            u = queue.pop(0)
            labels[u] = cur_label
            for v in range(n):
                if not visited[v] and sim_matrix[u, v] >= cosine_threshold:
                    visited[v] = True
                    queue.append(v)
        cur_label += 1
    return labels


# ==================== 与 booking-site 内部 API 交互 ====================

def fetch_event_bookings(
    booking_api_url: str,
    api_key: str,
    event_name: str,
    date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """调用 GET /api/internal/bookings/by-event 拉取该场次预约角色对照表。"""
    headers = {"X-API-Key": api_key}
    base = booking_api_url.rstrip("/")
    params: Dict[str, str] = {"event_name": event_name}
    if date:
        params["date"] = date

    try:
        resp = requests.get(f"{base}/api/internal/bookings/by-event", params=params, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            bookings = data.get("bookings") or []
            if bookings or not date:
                return bookings
        # 若按 event_name + date 查不到（如日期格式差异），放宽仅按 event_name 查询
        if date:
            resp2 = requests.get(
                f"{base}/api/internal/bookings/by-event",
                params={"event_name": event_name},
                headers=headers,
                timeout=15,
            )
            if resp2.status_code == 200:
                return resp2.json().get("bookings") or []
    except Exception as exc:
        logger.warning("拉取场次预约单失败（将继续以二维码锚点与局部聚类运行）: %s", exc)
    return []


def post_batch_results(
    booking_api_url: str,
    api_key: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """将完整拷卡、废片预筛、聚类与多标签绑定结果回写至 POST /api/internal/batches。"""
    base = booking_api_url.rstrip("/")
    resp = requests.post(
        f"{base}/api/internal/batches",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"回写 POST /api/internal/batches 失败 (HTTP {resp.status_code}): {resp.text}")
    return resp.json()


# ==================== 主流程：废片预筛 -> 二维码切段 -> 多模态聚类 -> 回写 ====================

def process_batch_manifest(
    manifest_path: Path,
    booking_api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    max_qr_segment_gap_sec: float = 1800.0,
    unslated_time_gap_sec: float = 300.0,
    enable_ml: bool = True,
    auto_notify: Optional[bool] = None,
) -> Dict[str, Any]:
    """执行完整的废片预筛、二维码切段绑定、多模态聚类（含合影多标签）与回写。"""
    manifest_path = manifest_path.resolve()
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    api_url = (booking_api_url or os.getenv("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
    internal_key = api_key or os.getenv("INTERNAL_API_KEY") or ""
    if not internal_key:
        raise RuntimeError("缺少 INTERNAL_API_KEY，无法访问 booking-site 内部接口。")

    device, device_desc = detect_compute_device()
    logger.info("计算设备检测: %s", device_desc)

    costume_extractor = CostumeFeatureExtractor(device=device, enable_ml=enable_ml)
    face_extractor = FaceFeatureExtractor(device=device, enable_ml=enable_ml)
    logger.info(
        "特征提取后端: costume=%s | face=%s",
        costume_extractor.backend_name,
        face_extractor.backend_name,
    )

    event_name = manifest.get("event_name", "")
    shoot_date = manifest.get("shoot_date")
    event_bookings = fetch_event_bookings(api_url, internal_key, event_name, date=shoot_date)
    logger.info("已拉取场次预约对照表: event=%s, bookings=%d", event_name, len(event_bookings))

    raw_assets: List[Dict[str, Any]] = manifest.get("assets") or []

    # 按拍摄时间（兜底文件名）升序排列，确保场记二维码切段顺序准确
    enriched: List[Dict[str, Any]] = []
    for item in raw_assets:
        ts = parse_iso_timestamp(item.get("captured_at"))
        enriched.append(
            {
                **item,
                "_ts": ts,
                "is_rejected": 0,
                "reject_reason": None,
                "cluster_id": None,
                "booking_ids": [],
                "_qr_payload": None,
                "_costume_vec": None,
                "_faces": [],
            }
        )
    enriched.sort(key=lambda x: (x["_ts"], x.get("filename", "").lower()))

    # 1) 逐张读取预览图 -> 优先识别场记二维码 -> 否则执行废片预筛
    for asset in enriched:
        img_path = Path(asset.get("preview_path") or asset.get("nas_raw_path") or "")
        img_bgr = cv2_imread_unicode(img_path)
        if img_bgr is None:
            asset["is_rejected"] = 1
            asset["reject_reason"] = "UNREADABLE_IMAGE"
            continue

        qr_payload = detect_slate_qr(img_bgr)
        if qr_payload is not None:
            asset["_qr_payload"] = qr_payload
            asset["is_rejected"] = 1
            asset["reject_reason"] = "SLATE_QR"
            logger.info(
                "识别场记二维码锚点: %s -> booking_id=%s (CN=%s, 角色=%s)",
                asset["filename"],
                qr_payload["booking_id"],
                qr_payload.get("cn", ""),
                qr_payload.get("character", ""),
            )
            continue

        qc = evaluate_technical_quality(img_bgr)
        asset["is_rejected"] = qc["is_rejected"]
        asset["reject_reason"] = qc["reject_reason"]
        asset["_qc_metrics"] = qc["metrics"]

        if asset["is_rejected"] == 1:
            logger.info("废片预筛过滤: %s -> %s", asset["filename"], asset["reject_reason"])
        else:
            # 仅为有效图提取服饰与人脸特征，供未打板聚类和合影多标签使用
            asset["_costume_vec"] = costume_extractor.extract(img_bgr)
            asset["_faces"] = face_extractor.extract_faces(img_bgr)

    # 2) 按场记二维码时间顺序切段并直接绑定到对应 booking_id
    active_booking_id: Optional[int] = None
    last_anchor_ts: float = 0.0

    for asset in enriched:
        ts = asset["_ts"]
        if asset["_qr_payload"] is not None:
            active_booking_id = int(asset["_qr_payload"]["booking_id"])
            last_anchor_ts = ts
            asset["booking_ids"] = [active_booking_id]
            asset["cluster_id"] = f"qr_booking_{active_booking_id}"
            continue

        if active_booking_id is not None:
            within_gap = (
                last_anchor_ts <= 0
                or ts <= 0
                or (ts - last_anchor_ts) <= max_qr_segment_gap_sec
            )
            if within_gap:
                asset["booking_ids"] = [active_booking_id]
                asset["cluster_id"] = f"qr_booking_{active_booking_id}"
                if ts > 0:
                    last_anchor_ts = ts
            else:
                active_booking_id = None

    # 3) 构建已知角色原型库（来自二维码已锚定有效图 + 预约单 character_ref_img）
    booking_costume_protos: Dict[int, List[np.ndarray]] = {}
    booking_face_protos: Dict[int, List[np.ndarray]] = {}

    for asset in enriched:
        if asset["is_rejected"] == 0 and len(asset["booking_ids"]) == 1:
            bid = asset["booking_ids"][0]
            if asset["_costume_vec"] is not None:
                booking_costume_protos.setdefault(bid, []).append(asset["_costume_vec"])
            if len(asset["_faces"]) == 1:
                booking_face_protos.setdefault(bid, []).append(asset["_faces"][0]["embedding"])

    # 补充预约单中本地可读的 character_ref_img 原型
    for b in event_bookings:
        ref = b.get("character_ref_img")
        bid = int(b["id"])
        if ref and Path(str(ref)).exists():
            ref_img = cv2_imread_unicode(Path(str(ref)))
            if ref_img is not None:
                booking_costume_protos.setdefault(bid, []).append(costume_extractor.extract(ref_img))
                ref_faces = face_extractor.extract_faces(ref_img)
                if len(ref_faces) == 1:
                    booking_face_protos.setdefault(bid, []).append(ref_faces[0]["embedding"])

    def _mean_norm(vecs: List[np.ndarray]) -> np.ndarray:
        avg = np.mean(np.stack(vecs, axis=0), axis=0)
        nrm = float(np.linalg.norm(avg))
        return avg / nrm if nrm > 1e-8 else avg

    costume_centroids: Dict[int, np.ndarray] = {
        bid: _mean_norm(vecs) for bid, vecs in booking_costume_protos.items() if vecs
    }
    face_centroids: Dict[int, np.ndarray] = {
        bid: _mean_norm(vecs) for bid, vecs in booking_face_protos.items() if vecs
    }

    # 4) 对未打板有效图执行时间间隔分段 + 聚类 + 角色原型匹配
    unslated_indices = [
        (i, asset["_ts"])
        for i, asset in enumerate(enriched)
        if asset["is_rejected"] == 0 and not asset["booking_ids"]
    ]
    if unslated_indices:
        segments = segment_by_time_gap(unslated_indices, gap_seconds=unslated_time_gap_sec)
        for seg_idx, seg_member_indices in enumerate(segments, start=1):
            vecs = np.stack([enriched[i]["_costume_vec"] for i in seg_member_indices], axis=0)
            cluster_labels = cluster_embeddings(vecs)

            # 按簇聚合，尝试匹配已知角色原型
            clusters_map: Dict[int, List[int]] = {}
            for member_i, lbl in zip(seg_member_indices, cluster_labels):
                clusters_map.setdefault(lbl, []).append(member_i)

            for lbl, member_list in clusters_map.items():
                cluster_tag = f"unslated_s{seg_idx}_c{lbl}"
                cluster_center = _mean_norm([enriched[i]["_costume_vec"] for i in member_list])

                best_bid: Optional[int] = None
                best_score = 0.0
                for bid, proto in costume_centroids.items():
                    score = float(np.dot(cluster_center, proto))
                    if score > best_score:
                        best_score = score
                        best_bid = bid

                for i in member_list:
                    enriched[i]["cluster_id"] = cluster_tag
                    if best_bid is not None and best_score >= 0.72:
                        enriched[i]["booking_ids"] = [best_bid]

    # 5) 合影多标签增强：若单张有效图检测到 >=2 张人脸且命中多个不同已识别角色，写入多个 booking_id
    face_match_threshold = 0.45 if face_extractor.backend_name.startswith("insightface:") else 0.75
    if len(face_centroids) >= 2:
        for asset in enriched:
            if asset["is_rejected"] != 0 or len(asset["_faces"]) < 2:
                continue
            matched_bids = set(asset["booking_ids"])
            for face_obj in asset["_faces"]:
                f_emb = face_obj["embedding"]
                best_bid: Optional[int] = None
                best_sim = 0.0
                for bid, proto in face_centroids.items():
                    sim = float(np.dot(f_emb, proto))
                    if sim > best_sim:
                        best_sim = sim
                        best_bid = bid
                if best_bid is not None and best_sim >= face_match_threshold:
                    matched_bids.add(best_bid)
            if len(matched_bids) > len(asset["booking_ids"]):
                asset["booking_ids"] = sorted(matched_bids)
                logger.info(
                    "检测到合影多标签: %s -> booking_ids=%s",
                    asset["filename"],
                    asset["booking_ids"],
                )

    # 6) 组装回写 payload 并调用 POST /api/internal/batches
    assets_payload: List[Dict[str, Any]] = []
    for asset in enriched:
        assets_payload.append(
            {
                "filename": asset["filename"],
                "nas_raw_path": asset["nas_raw_path"],
                "preview_url": asset["preview_url"],
                "sha256": asset["sha256"],
                "is_rejected": int(asset["is_rejected"]),
                "reject_reason": asset["reject_reason"],
                "cluster_id": asset["cluster_id"],
                "captured_at": asset["captured_at"],
                "booking_ids": asset["booking_ids"],
            }
        )

    batch_payload = {
        "batch_id": manifest["batch_id"],
        "camera_model": manifest.get("camera_model", ""),
        "event_name": event_name,
        "venue": manifest.get("venue", ""),
        "nas_path": manifest.get("nas_path", ""),
        "total_files": manifest.get("total_files", len(assets_payload)),
        "verified_files": manifest.get("verified_files", len(assets_payload)),
        "assets": assets_payload,
    }

    sync_result = post_batch_results(api_url, internal_key, batch_payload)

    # 7) 输出每个订单生成的选图短链
    logger.info(
        "批次 %s 回写成功：写入资产 %d 张，关联记录 %d 条。",
        sync_result.get("batch_id"),
        sync_result.get("inserted_assets", 0),
        sync_result.get("total_links", 0),
    )
    public_base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/") or api_url
    print("\n================ 客户选图短链清单 ================")
    for b in sync_result.get("bookings") or []:
        short_path = b.get("gallery_path") or f"/gallery/{b.get('gallery_token')}"
        full_url = f"{public_base}{short_path}"
        print(
            f"  [订单 #{b['id']}] CN: {b.get('cn')} | 角色: {b.get('character')} "
            f"| 状态: {b.get('status')} | 有效图: {b.get('valid_asset_count')} 张 "
            f"| 短链: {short_path} ({full_url})"
        )
    print("==================================================\n")

    # 8) 自动推送选图链接给已匹配客户（当 auto_notify=True 或 AUTO_PUSH_GALLERY=true）
    should_notify = (
        auto_notify
        if auto_notify is not None
        else str(os.getenv("AUTO_PUSH_GALLERY", "false")).strip().lower() == "true"
    )
    notify_results: List[Dict[str, Any]] = []
    if should_notify:
        notify_results = auto_notify_clients(
            bookings=sync_result.get("bookings") or [],
            event_bookings=event_bookings,
            public_base=public_base,
            internal_key=internal_key,
        )

    return {
        "device": device,
        "device_desc": device_desc,
        "costume_backend": costume_extractor.backend_name,
        "face_backend": face_extractor.backend_name,
        "batch_payload": batch_payload,
        "sync_result": sync_result,
        "notify_results": notify_results,
    }


def auto_notify_clients(
    bookings: List[Dict[str, Any]],
    event_bookings: List[Dict[str, Any]],
    public_base: str,
    internal_key: str,
    im_gateway_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """自动通过 im-gateway 将每个已匹配订单的选图短链推送至客户微信/QQ。"""
    gateway_url = (im_gateway_url or os.getenv("IM_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/")
    booking_meta = {int(eb["id"]): eb for eb in event_bookings if eb.get("id") is not None}
    results: List[Dict[str, Any]] = []

    for b in bookings:
        bid = b.get("id")
        valid_cnt = int(b.get("valid_asset_count") or 0)
        token = str(b.get("gallery_token") or "").strip()
        if not bid or valid_cnt <= 0 or not token:
            continue

        meta = booking_meta.get(int(bid), {})
        platform = str(b.get("platform") or meta.get("platform") or "").strip().lower()
        im_user_id = str(
            b.get("im_user_id")
            or meta.get("im_user_id")
            or b.get("qq")
            or meta.get("qq")
            or b.get("wechat")
            or meta.get("wechat")
            or ""
        ).strip()
        if not platform or platform == "web" or not im_user_id:
            logger.info("订单 #%s (%s) 未绑定微信/QQ通道，跳过自动推送（可在中控台手动复制短链）", bid, b.get("cn"))
            results.append({"booking_id": bid, "ok": False, "reason": "no_im_channel"})
            continue

        gallery_url = f"{public_base.rstrip('/')}/gallery/{token}"
        try:
            resp = requests.post(
                f"{gateway_url}/api/gateway/notify-gallery",
                headers={"X-API-Key": internal_key, "Content-Type": "application/json"},
                json={
                    "platform": platform,
                    "im_user_id": im_user_id,
                    "cn": b.get("cn") or meta.get("cn") or "",
                    "character": b.get("character") or meta.get("character") or "",
                    "gallery_token": token,
                    "gallery_url": gallery_url,
                    "booking_id": int(bid),
                },
                timeout=10,
                proxies={"http": None, "https": None},
            )
            ok = resp.status_code < 400
            if ok:
                logger.info("已自动推送选图链接给客户: 订单 #%s (%s) -> %s:%s", bid, b.get("cn"), platform, im_user_id)
            else:
                logger.warning("推送选图链接失败: 订单 #%s HTTP %s %s", bid, resp.status_code, resp.text[:160])
            results.append({"booking_id": bid, "ok": ok, "status_code": resp.status_code, "gallery_url": gallery_url})
        except Exception as exc:  # noqa: BLE001
            logger.warning("调用 IM 网关自动推送选图链接异常 (订单 #%s): %s", bid, exc)
            results.append({"booking_id": bid, "ok": False, "error": str(exc)})

    return results


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ZERO STUDIO 废片预筛、二维码切段与多模态聚类模块")
    parser.add_argument("--manifest", type=str, required=True, help="ingest.py 生成的 manifest.json 路径")
    parser.add_argument("--api-url", type=str, default=None, help="覆盖 BOOKING_API_URL")
    parser.add_argument("--api-key", type=str, default=None, help="覆盖 INTERNAL_API_KEY")
    parser.add_argument("--no-ml", action="store_true", help="强制跳过重型 GPU 模型加载，仅用轻量 CPU 特征回退")
    parser.add_argument("--auto-notify", action="store_true", help="分类入库后自动通过 IM 网关推送选图链接给客户")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    process_batch_manifest(
        manifest_path=Path(args.manifest),
        booking_api_url=args.api_url,
        api_key=args.api_key,
        enable_ml=not args.no_ml,
        auto_notify=True if args.auto_notify else None,
    )


if __name__ == "__main__":
    main()
