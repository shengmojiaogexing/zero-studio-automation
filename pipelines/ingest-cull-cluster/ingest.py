#!/usr/bin/env python3
"""
Windows 自动拷卡、单次流式双校验与高速 2048px WebP 预览图生成模块。

核心特性：
1. NAS 目标寻址：优先检测 NAS_UNC_PATH，不可达时回退 NAS_DRIVE_PATH，并执行目标磁盘剩余空间校验。
2. 支持格式：Sony .ARW、Canon .CR3、.JPG/.JPEG。
3. 双模式运行：
   - CLI 手动指定目录模式：--source <dir> --event <name> --venue <venue>
   - Windows 热插拔监听模式：--watch（3 秒防抖防 Windows 缩略图/资源管理器句柄占用）
4. EXIF 自动提取：优先调用系统 exiftool，不可用时平滑回退 exifread / Pillow / mtime，
   生成唯一批次 ID：{YYYYMMDD}_{EventCode}_{CameraModel}_{Seq}（绝不在拷卡阶段强行绑定单订单）。
5. 工业级单次流式双校验：4MB 分块单次读源卡写 NAS .tmp 并同步计算源端 SHA-256 ->
   落盘 fsync 后仅回读一次 NAS .tmp 校验哈希一致 -> 原子 os.replace -> 落盘 manifest.json。
   严格只读源卡，绝不自动删除或格式化源卡文件。
6. 高速 2048px WebP 预览图：优先 exiftool -b -PreviewImage 提取 RAW 内嵌预览图再缩放，
   不可用时自动回退 rawpy / Pillow。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from dotenv import load_dotenv
from PIL import Image, ImageOps

try:
    import exifread  # type: ignore
except ImportError:
    exifread = None

try:
    import rawpy  # type: ignore
except ImportError:
    rawpy = None

# 加载根目录 .env（不覆盖外部显式传入的环境变量）
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(WORKSPACE_ROOT / ".env", override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [ingest] %(message)s",
)
logger = logging.getLogger("ingest")

SUPPORTED_EXTENSIONS = {".arw", ".cr3", ".jpg", ".jpeg"}
RAW_EXTENSIONS = {".arw", ".cr3"}
SKIP_DIR_NAMES = {
    "system volume information",
    "$recycle.bin",
    ".trashes",
    ".spotlight-v100",
    "__macosx",
}
CHUNK_SIZE = 4 * 1024 * 1024  # 4MB 流式分块
PREVIEW_MAX_EDGE = 2048
DRIVE_REMOVABLE = 2


def sanitize_token(value: str, fallback: str = "UNK", max_len: int = 24) -> str:
    """将活动名或相机型号清洗为安全的批次 ID 字段。"""
    cleaned = re.sub(r"[^A-Za-z0-9\-]+", "", (value or "").strip())
    if not cleaned:
        # 若全为中文等非 ASCII 字符，保留安全字符或生成简短哈希码
        stripped = re.sub(r"\s+", "", value or "")
        if stripped:
            short_hash = hashlib.md5(stripped.encode("utf-8")).hexdigest()[:6].upper()
            return f"EVT{short_hash}"
        return fallback
    return cleaned[:max_len]


def resolve_nas_root(
    explicit_nas_root: Optional[str] = None,
    unc_path: Optional[str] = None,
    drive_path: Optional[str] = None,
) -> Path:
    """
    优先检测 NAS_UNC_PATH，不可达时回退 NAS_DRIVE_PATH。
    若传入 explicit_nas_root（如 CLI / 测试覆盖），优先使用该路径。
    """
    candidates: List[Tuple[str, Optional[str]]] = []
    if explicit_nas_root:
        candidates.append(("CLI --nas-root", explicit_nas_root))
    else:
        candidates.append(("NAS_UNC_PATH", unc_path or os.getenv("NAS_UNC_PATH")))
        candidates.append(("NAS_DRIVE_PATH", drive_path or os.getenv("NAS_DRIVE_PATH")))

    for label, raw_path in candidates:
        if not raw_path:
            continue
        p = Path(raw_path)
        try:
            if p.exists() and p.is_dir():
                logger.info("NAS 目标就绪 (%s): %s", label, p)
                return p
            # 若父级挂载点存在且可写，自动创建目录
            parent = p.parent
            if parent.exists() and parent.is_dir():
                p.mkdir(parents=True, exist_ok=True)
                logger.info("NAS 目标目录已自动创建 (%s): %s", label, p)
                return p
            logger.warning("NAS 路径不可达 (%s): %s", label, p)
        except OSError as exc:
            logger.warning("检测 NAS 路径失败 (%s=%s): %s", label, p, exc)

    raise RuntimeError(
        "所有 NAS 目标路径均不可达！请检查 NAS_UNC_PATH 或 NAS_DRIVE_PATH 配置及网络连接。"
    )


def check_disk_space(target_dir: Path, required_bytes: int, safety_ratio: float = 1.15) -> Dict[str, int]:
    """校验目标磁盘剩余空间是否足以容纳本批次拷卡文件。"""
    usage = shutil.disk_usage(str(target_dir))
    needed_with_margin = int(required_bytes * safety_ratio) + 16 * 1024 * 1024
    if usage.free < needed_with_margin:
        raise RuntimeError(
            f"目标磁盘空间不足！路径={target_dir}，"
            f"剩余={usage.free / (1024**3):.2f}GB，预计需要={needed_with_margin / (1024**3):.2f}GB"
        )
    logger.info(
        "目标磁盘空间校验通过：剩余 %.2f GB，本批源文件 %.2f MB",
        usage.free / (1024**3),
        required_bytes / (1024**2),
    )
    return {"total": usage.total, "used": usage.used, "free": usage.free}


def scan_media_files(source_dir: Path) -> List[Path]:
    """递归扫描源目录中的 .ARW / .CR3 / .JPG / .JPEG 文件。"""
    if not source_dir.exists() or not source_dir.is_dir():
        raise FileNotFoundError(f"源目录不存在或不是文件夹: {source_dir}")

    found: List[Path] = []
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and d.lower() not in SKIP_DIR_NAMES
        ]
        for fname in sorted(files):
            if fname.startswith("._") or fname.startswith("."):
                continue
            fpath = Path(root) / fname
            if fpath.suffix.lower() in SUPPORTED_EXTENSIONS:
                found.append(fpath)

    found.sort(key=lambda p: str(p).lower())
    return found


def _parse_exif_datetime(raw_dt: Optional[str]) -> Optional[str]:
    """将 EXIF 时间格式 'YYYY:MM:DD HH:MM:SS' 规范化为 ISO 8601 字符串。"""
    if not raw_dt:
        return None
    s = str(raw_dt).strip().split("+")[0].split(".")[0].strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return None


def extract_exif_batch_with_exiftool(files: List[Path]) -> Dict[Path, Dict[str, Optional[str]]]:
    """若系统安装了 exiftool，批量高速读取相机型号与拍摄时间。"""
    exiftool_bin = shutil.which("exiftool")
    if not exiftool_bin or not files:
        return {}

    result: Dict[Path, Dict[str, Optional[str]]] = {}
    try:
        # 避免 Windows 命令行长度限制，按每批 100 个文件调用
        for idx in range(0, len(files), 100):
            chunk = files[idx: idx + 100]
            cmd = [
                exiftool_bin,
                "-j",
                "-q",
                "-q",
                "-Model",
                "-DateTimeOriginal",
                "-CreateDate",
                *[str(p) for p in chunk],
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                continue
            items = json.loads(proc.stdout)
            for item in items:
                src = item.get("SourceFile")
                if not src:
                    continue
                resolved = Path(src).resolve()
                model = str(item.get("Model") or "").strip() or None
                dt_raw = item.get("DateTimeOriginal") or item.get("CreateDate")
                result[resolved] = {
                    "camera_model": model,
                    "captured_at": _parse_exif_datetime(dt_raw),
                }
    except Exception as exc:
        logger.warning("exiftool 批量读取失败，将回退 exifread: %s", exc)
    return result


def extract_exif_fallback(file_path: Path) -> Dict[str, Optional[str]]:
    """回退链路：exifread -> Pillow EXIF -> 文件 mtime。"""
    camera_model: Optional[str] = None
    captured_at: Optional[str] = None

    # 1. exifread
    if exifread is not None:
        try:
            with open(file_path, "rb") as f:
                tags = exifread.process_file(f, details=False, strict=False)
            if tags:
                model_tag = tags.get("Image Model") or tags.get("EXIF LensModel")
                if model_tag:
                    camera_model = str(model_tag).strip() or None
                dt_tag = (
                    tags.get("EXIF DateTimeOriginal")
                    or tags.get("EXIF DateTimeDigitized")
                    or tags.get("Image DateTime")
                )
                if dt_tag:
                    captured_at = _parse_exif_datetime(str(dt_tag))
        except Exception:
            pass

    # 2. Pillow EXIF（对 JPG/JPEG 极快且稳定）
    if (not camera_model or not captured_at) and file_path.suffix.lower() in {".jpg", ".jpeg"}:
        try:
            with Image.open(file_path) as img:
                exif = img.getexif()
                if exif:
                    if not camera_model and 272 in exif:
                        camera_model = str(exif.get(272)).strip() or None
                    if not captured_at:
                        dt_val = exif.get(36867) or exif.get(306)
                        captured_at = _parse_exif_datetime(str(dt_val)) if dt_val else None
        except Exception:
            pass

    # 3. 最终时间兜底：文件修改时间
    if not captured_at:
        mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
        captured_at = mtime.strftime("%Y-%m-%dT%H:%M:%S")

    return {"camera_model": camera_model, "captured_at": captured_at}


def generate_unique_batch_id(
    nas_root: Path,
    event_name: str,
    camera_model: str,
    shoot_date_str: str,
    event_code: Optional[str] = None,
) -> str:
    """生成唯一拍摄批次 ID：{YYYYMMDD}_{EventCode}_{CameraModel}_{Seq}。"""
    date_part = re.sub(r"[^0-9]", "", shoot_date_str)[:8] or datetime.now().strftime("%Y%m%d")
    ev_part = sanitize_token(event_code or event_name, fallback="EVENT", max_len=20)
    cam_part = sanitize_token(camera_model, fallback="CAM", max_len=20)
    prefix = f"{date_part}_{ev_part}_{cam_part}_"

    seq = 1
    while (nas_root / f"{prefix}{seq:03d}").exists():
        seq += 1
    return f"{prefix}{seq:03d}"


def stream_copy_and_verify(src_path: Path, dst_final_path: Path) -> Tuple[str, int]:
    """
    工业级单次流式双校验拷卡：
    1. 4MB 分块单次读取源卡文件 -> 写入 NAS .tmp 文件 -> 同步在内存中计算源端 SHA-256；
    2. flush + os.fsync 落盘；
    3. 仅从 NAS .tmp 回读一次计算目标端 SHA-256，校验与源端完全一致；
    4. 一致后原子 os.replace 为最终文件名，绝不触碰/删除源卡文件。
    """
    dst_final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dst_final_path.with_name(dst_final_path.name + ".tmp")

    src_hasher = hashlib.sha256()
    bytes_written = 0

    try:
        with open(src_path, "rb") as f_src, open(tmp_path, "wb") as f_dst:
            while True:
                chunk = f_src.read(CHUNK_SIZE)
                if not chunk:
                    break
                src_hasher.update(chunk)
                f_dst.write(chunk)
                bytes_written += len(chunk)
            f_dst.flush()
            os.fsync(f_dst.fileno())

        src_sha256 = src_hasher.hexdigest()

        # 仅回读一次 NAS 目标 .tmp 校验落地完整性
        dst_hasher = hashlib.sha256()
        bytes_verified = 0
        with open(tmp_path, "rb") as f_verify:
            while True:
                chunk = f_verify.read(CHUNK_SIZE)
                if not chunk:
                    break
                dst_hasher.update(chunk)
                bytes_verified += len(chunk)

        dst_sha256 = dst_hasher.hexdigest()
        if dst_sha256 != src_sha256 or bytes_verified != bytes_written:
            raise IOError(
                f"SHA-256 双校验失败: {src_path.name} "
                f"(src={src_sha256[:12]}:{bytes_written}, dst={dst_sha256[:12]}:{bytes_verified})"
            )

        os.replace(tmp_path, dst_final_path)
        return src_sha256, bytes_written
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _save_resized_webp(img: Image.Image, output_path: Path, max_edge: int = PREVIEW_MAX_EDGE) -> None:
    """按 EXIF 方向校正后，将长边缩放到 2048px（不放大）并保存为高质量 WebP。"""
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    elif img.mode == "RGBA":
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_webp = output_path.with_name(output_path.name + ".tmp")
    img.save(tmp_webp, format="WEBP", quality=85, method=4)
    os.replace(tmp_webp, output_path)


def parse_xmp_adjustments(xmp_path: Path) -> Dict[str, float]:
    """
    解析 Lightroom / Adobe Camera Raw (.xmp) 中的核心曝光与色温标签。
    兼容属性写法 (<rdf:Description crs:Exposure2012="+0.85" ...>) 与子元素写法 (<crs:Exposure2012>+0.85</crs:Exposure2012>)。
    """
    target_keys = {
        "Exposure2012",
        "Shadows2012",
        "Highlights2012",
        "Contrast2012",
        "Temperature",
        "Tint",
        "Vibrance",
        "Saturation",
    }
    params: Dict[str, float] = {}
    tree = ET.parse(str(xmp_path))
    root = tree.getroot()

    def _strip_ns(tag: str) -> str:
        return tag.split("}", 1)[1] if "}" in tag else tag

    for elem in root.iter():
        for attr_k, attr_v in elem.attrib.items():
            key = _strip_ns(attr_k)
            if key in target_keys:
                try:
                    params[key] = float(str(attr_v).strip())
                except ValueError:
                    pass
        tag_name = _strip_ns(elem.tag)
        if tag_name in target_keys and elem.text and elem.text.strip():
            try:
                params[tag_name] = float(elem.text.strip())
            except ValueError:
                pass

    return params


def apply_xmp_adjustments_to_rgb(rgb_uint8: np.ndarray, params: Dict[str, float]) -> np.ndarray:
    """
    轻量 Python XMP 渲染器：将 Exposure2012 / Shadows2012 / Highlights2012 / Temperature / Tint 等
    核心调色参数近似应用到 RGB 图像（仅用于生成客户选图预览 WebP，绝不修改 NAS 原片）。
    """
    if not params:
        return rgb_uint8

    img = rgb_uint8.astype(np.float32) / 255.0

    # 1. Exposure2012 (EV 档位)
    exposure = float(params.get("Exposure2012", 0.0))
    if abs(exposure) > 1e-4:
        img = img * (2.0 ** np.clip(exposure, -4.0, 4.0))

    # 2. Temperature / Tint 色温色调微调
    temp = params.get("Temperature")
    if temp is not None:
        # 既支持绝对色温 (如 5500K)，也支持相对偏移 (如 -10..+10)
        if temp > 1000.0:
            shift = np.clip((temp - 5500.0) / 5500.0, -0.35, 0.35)
        else:
            shift = np.clip(temp / 100.0, -0.35, 0.35)
        img[:, :, 0] *= (1.0 + shift * 0.25)  # R
        img[:, :, 2] *= (1.0 - shift * 0.25)  # B

    tint = float(params.get("Tint", 0.0))
    if abs(tint) > 1e-4:
        t_shift = np.clip(tint / 150.0, -0.25, 0.25)
        img[:, :, 1] *= (1.0 - t_shift * 0.18)  # G

    img = np.clip(img, 0.0, 1.0)

    # 3. Shadows2012 & Highlights2012 分区亮度调整
    lum = 0.2126 * img[:, :, 0] + 0.7152 * img[:, :, 1] + 0.0722 * img[:, :, 2]
    shadows = float(params.get("Shadows2012", 0.0))
    if abs(shadows) > 1e-4:
        s_strength = np.clip(shadows / 100.0, -1.0, 1.0)
        shadow_mask = np.power(1.0 - lum, 2.0)[..., None]
        img = img + shadow_mask * (s_strength * 0.42) * (0.25 + 0.75 * img)

    highlights = float(params.get("Highlights2012", 0.0))
    if abs(highlights) > 1e-4:
        h_strength = np.clip(highlights / 100.0, -1.0, 1.0)
        hi_mask = np.power(lum, 2.0)[..., None]
        img = img + hi_mask * (h_strength * 0.38)

    # 4. Contrast2012
    contrast = float(params.get("Contrast2012", 0.0))
    if abs(contrast) > 1e-4:
        c_factor = 1.0 + np.clip(contrast / 100.0, -0.6, 0.8) * 0.5
        img = (img - 0.5) * c_factor + 0.5

    # 5. Saturation / Vibrance
    sat = float(params.get("Saturation", 0.0)) + float(params.get("Vibrance", 0.0)) * 0.5
    if abs(sat) > 1e-4:
        s_factor = 1.0 + np.clip(sat / 100.0, -0.8, 0.8) * 0.5
        gray = (0.2126 * img[:, :, 0] + 0.7152 * img[:, :, 1] + 0.0722 * img[:, :, 2])[..., None]
        img = gray + (img - gray) * s_factor

    return np.clip(img * 255.0, 0.0, 255.0).astype(np.uint8)


def apply_preview_xmp(raw_path: Path, xmp_path: Path, out_webp: Path) -> str:
    """
    可插拔的预览 XMP 前置处理步骤（NAS 原片始终只读、零修改）：
    1. 若系统可用 darktable-cli，优先调用 darktable-cli 渲染；
    2. 若配置了 PREVIEW_ACR_CMD（Adobe DNG+ACR 批处理命令模板），调用外部 CLI；
    3. 否则使用内建轻量 Python XMP 参数解析渲染器（支持 rawpy / Pillow 解码 + 核心 crs 标签调色）生成 2048px WebP。
    """
    if not xmp_path.exists():
        raise FileNotFoundError(f"预览 XMP 预设文件不存在: {xmp_path}")

    # 1. darktable-cli 外部引擎优先
    darktable_bin = shutil.which("darktable-cli")
    if darktable_bin:
        with tempfile.TemporaryDirectory(prefix="dt_xmp_") as td:
            tmp_jpg = Path(td) / f"{raw_path.stem}.jpg"
            try:
                proc = subprocess.run(
                    [darktable_bin, str(raw_path), str(xmp_path), str(tmp_jpg)],
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                if proc.returncode == 0 and tmp_jpg.exists() and tmp_jpg.stat().st_size > 1024:
                    with Image.open(tmp_jpg) as img:
                        _save_resized_webp(img, out_webp)
                    return "darktable-cli:xmp"
            except Exception as exc:
                logger.warning("darktable-cli 渲染失败，回退 Python XMP 渲染器: %s", exc)

    # 2. 自定义 Adobe DNG+ACR 批处理 CLI（通过环境变量 PREVIEW_ACR_CMD 配置）
    acr_cmd_tpl = os.getenv("PREVIEW_ACR_CMD", "").strip()
    if acr_cmd_tpl:
        with tempfile.TemporaryDirectory(prefix="acr_xmp_") as td:
            tmp_out = Path(td) / f"{raw_path.stem}.jpg"
            try:
                cmd = acr_cmd_tpl.format(raw=str(raw_path), xmp=str(xmp_path), out=str(tmp_out))
                proc = subprocess.run(cmd, shell=True, capture_output=True, timeout=90, check=False)
                if proc.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 1024:
                    with Image.open(tmp_out) as img:
                        _save_resized_webp(img, out_webp)
                    return "acr-cli:xmp"
            except Exception as exc:
                logger.warning("PREVIEW_ACR_CMD 渲染失败，回退 Python XMP 渲染器: %s", exc)

    # 3. 内建轻量 Python XMP 参数解析渲染器
    params = parse_xmp_adjustments(xmp_path)
    ext = raw_path.suffix.lower()
    decoder_tag = "pillow"

    rgb_arr: Optional[np.ndarray] = None
    if ext in RAW_EXTENSIONS and rawpy is not None:
        try:
            with rawpy.imread(str(raw_path)) as raw:
                rgb_arr = raw.postprocess(half_size=True, use_camera_wb=True, no_auto_bright=True, output_bps=8)
            decoder_tag = "rawpy"
        except Exception as exc:
            logger.warning("rawpy 解码 %s 失败，回退 Pillow XMP 渲染: %s", raw_path.name, exc)

    if rgb_arr is None:
        with Image.open(raw_path) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            if max(img.size) > PREVIEW_MAX_EDGE:
                img.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.Resampling.LANCZOS)
            rgb_arr = np.asarray(img, dtype=np.uint8)
        decoder_tag = "pillow"

    adjusted_rgb = apply_xmp_adjustments_to_rgb(rgb_arr, params)
    with Image.fromarray(adjusted_rgb, mode="RGB") as out_img:
        _save_resized_webp(out_img, out_webp)
    return f"python-xmp:{decoder_tag}"


def generate_webp_preview(
    raw_or_jpg_path: Path,
    output_webp_path: Path,
    preview_xmp_path: Optional[Path] = None,
) -> str:
    """
    高速生成 2048px WebP 预览图：
    - 若配置了 PREVIEW_XMP_PATH（或显式传入 preview_xmp_path），先执行 apply_preview_xmp 生成预览预设图；
    - 否则 RAW (.ARW/.CR3) 优先 exiftool -b -PreviewImage -> 回退 rawpy -> 回退 Pillow；JPG/JPEG 直接 Pillow 缩放。
    """
    resolved_xmp: Optional[Path] = preview_xmp_path
    if resolved_xmp is None:
        env_xmp = os.getenv("PREVIEW_XMP_PATH", "").strip()
        if env_xmp:
            resolved_xmp = Path(env_xmp)

    if resolved_xmp is not None:
        if resolved_xmp.exists():
            return apply_preview_xmp(raw_or_jpg_path, resolved_xmp, output_webp_path)
        logger.warning("配置的 PREVIEW_XMP_PATH 不存在 (%s)，跳过 XMP 预览处理。", resolved_xmp)

    ext = raw_or_jpg_path.suffix.lower()

    if ext in RAW_EXTENSIONS:
        # 1. 优先 exiftool 提取内嵌预览图（速度最快）
        exiftool_bin = shutil.which("exiftool")
        if exiftool_bin:
            for tag in ("-PreviewImage", "-JpgFromRaw"):
                try:
                    proc = subprocess.run(
                        [exiftool_bin, "-b", tag, str(raw_or_jpg_path)],
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
                    if proc.returncode == 0 and len(proc.stdout) > 4096:
                        with Image.open(io.BytesIO(proc.stdout)) as img:
                            _save_resized_webp(img, output_webp_path)
                        return f"exiftool:{tag}"
                except Exception:
                    continue

        # 2. 回退 rawpy（先试内嵌缩略图，不足则半尺寸解拜耳）
        if rawpy is not None:
            try:
                with rawpy.imread(str(raw_or_jpg_path)) as raw:
                    try:
                        thumb = raw.extract_thumb()
                        if thumb.format == rawpy.ThumbFormat.JPEG and len(thumb.data) > 4096:
                            with Image.open(io.BytesIO(thumb.data)) as img:
                                if max(img.size) >= 1024:
                                    _save_resized_webp(img, output_webp_path)
                                    return "rawpy:thumb"
                    except Exception:
                        pass
                    rgb = raw.postprocess(half_size=True, use_camera_wb=True, no_auto_bright=False)
                    with Image.fromarray(rgb) as img:
                        _save_resized_webp(img, output_webp_path)
                    return "rawpy:postprocess"
            except Exception as exc:
                logger.warning("rawpy 解码 %s 失败，尝试 Pillow 回退: %s", raw_or_jpg_path.name, exc)

    # 3. JPG/JPEG 或最终 Pillow 回退
    with Image.open(raw_or_jpg_path) as img:
        _save_resized_webp(img, output_webp_path)
    return "pillow"


def ingest_directory(
    source_dir: Path,
    event_name: str,
    venue: str = "",
    nas_root: Optional[Path] = None,
    preview_root: Optional[Path] = None,
    event_code: Optional[str] = None,
    preview_xmp_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """执行一次完整的目录拷卡 + 单次流式双校验 + WebP 预览图生成 + manifest.json 落盘。"""
    source_dir = source_dir.resolve()
    media_files = scan_media_files(source_dir)
    if not media_files:
        raise ValueError(f"源目录未发现支持的图像文件 (.ARW/.CR3/.JPG/.JPEG): {source_dir}")

    resolved_nas_root = nas_root if nas_root is not None else resolve_nas_root()
    resolved_preview_root = (
        preview_root
        if preview_root is not None
        else Path(
            os.getenv(
                "PREVIEW_OUTPUT_DIR",
                str(WORKSPACE_ROOT / "apps" / "booking-site" / "uploads" / "previews"),
            )
        )
    )
    resolved_preview_root.mkdir(parents=True, exist_ok=True)

    # 1. 计算源文件总大小并校验目标磁盘剩余空间
    total_src_bytes = sum(f.stat().st_size for f in media_files)
    check_disk_space(resolved_nas_root, total_src_bytes)

    # 2. 提取 EXIF（相机型号与拍摄时间）
    exiftool_map = extract_exif_batch_with_exiftool(media_files)
    meta_by_file: Dict[Path, Dict[str, Optional[str]]] = {}
    camera_models: List[str] = []
    capture_dates: List[str] = []

    for fpath in media_files:
        resolved = fpath.resolve()
        info = exiftool_map.get(resolved)
        if not info or not info.get("captured_at") or not info.get("camera_model"):
            fb = extract_exif_fallback(fpath)
            info = {
                "camera_model": (info or {}).get("camera_model") or fb.get("camera_model"),
                "captured_at": (info or {}).get("captured_at") or fb.get("captured_at"),
            }
        meta_by_file[resolved] = info
        if info.get("camera_model"):
            camera_models.append(str(info["camera_model"]))
        if info.get("captured_at"):
            capture_dates.append(str(info["captured_at"])[:10])

    primary_camera = Counter(camera_models).most_common(1)[0][0] if camera_models else "UnknownCam"
    shoot_date = min(capture_dates) if capture_dates else datetime.now().strftime("%Y-%m-%d")

    # 3. 生成唯一拍摄批次 ID（不强行绑定单个订单）
    batch_id = generate_unique_batch_id(
        nas_root=resolved_nas_root,
        event_name=event_name,
        camera_model=primary_camera,
        shoot_date_str=shoot_date,
        event_code=event_code,
    )
    nas_batch_dir = resolved_nas_root / batch_id
    nas_batch_dir.mkdir(parents=True, exist_ok=True)
    preview_batch_dir = resolved_preview_root / batch_id
    preview_batch_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "开始批次拷卡: batch_id=%s | 相机=%s | 文件数=%d | NAS=%s",
        batch_id,
        primary_camera,
        len(media_files),
        nas_batch_dir,
    )

    # 4. 逐文件执行单次流式双校验 + 2048px WebP 预览图生成
    assets_manifest: List[Dict[str, Any]] = []
    verified_count = 0
    used_names: Dict[str, int] = {}

    for idx, src_file in enumerate(media_files, start=1):
        resolved = src_file.resolve()
        info = meta_by_file[resolved]

        # 防止源卡不同子目录下同名文件覆盖
        base_name = src_file.name
        lower_name = base_name.lower()
        if lower_name in used_names:
            used_names[lower_name] += 1
            unique_name = f"{src_file.stem}_{used_names[lower_name]}{src_file.suffix}"
        else:
            used_names[lower_name] = 1
            unique_name = base_name

        dst_raw_path = nas_batch_dir / unique_name
        sha256_hex, size_bytes = stream_copy_and_verify(src_file, dst_raw_path)
        verified_count += 1

        preview_filename = f"{Path(unique_name).stem}.webp"
        preview_disk_path = preview_batch_dir / preview_filename
        preview_backend = generate_webp_preview(
            dst_raw_path,
            preview_disk_path,
            preview_xmp_path=preview_xmp_path,
        )
        preview_url = f"/previews/{batch_id}/{preview_filename}"

        assets_manifest.append(
            {
                "filename": unique_name,
                "source_filename": src_file.name,
                "nas_raw_path": str(dst_raw_path),
                "preview_path": str(preview_disk_path),
                "preview_url": preview_url,
                "preview_backend": preview_backend,
                "size_bytes": size_bytes,
                "sha256": sha256_hex,
                "verified": True,
                "camera_model": info.get("camera_model") or primary_camera,
                "captured_at": info.get("captured_at"),
            }
        )
        logger.info(
            "[%d/%d] 双校验通过: %s | sha256=%s... | preview=%s (%s)",
            idx,
            len(media_files),
            unique_name,
            sha256_hex[:12],
            preview_filename,
            preview_backend,
        )

    manifest: Dict[str, Any] = {
        "batch_id": batch_id,
        "camera_model": primary_camera,
        "event_name": event_name,
        "venue": venue,
        "shoot_date": shoot_date,
        "source_dir": str(source_dir),
        "nas_path": str(nas_batch_dir),
        "preview_dir": str(preview_batch_dir),
        "total_files": len(media_files),
        "verified_files": verified_count,
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "source_deleted": False,
        "assets": assets_manifest,
    }

    manifest_path = nas_batch_dir / "manifest.json"
    tmp_manifest = nas_batch_dir / "manifest.json.tmp"
    with open(tmp_manifest, "w", encoding="utf-8") as mf:
        json.dump(manifest, mf, ensure_ascii=False, indent=2)
        mf.flush()
        os.fsync(mf.fileno())
    os.replace(tmp_manifest, manifest_path)

    manifest["manifest_path"] = str(manifest_path)
    logger.info("批次拷卡与校验完成！manifest 已落盘: %s", manifest_path)
    return manifest


def get_windows_removable_drives() -> List[Path]:
    """检测当前挂载的 Windows 可移动存储盘符（SD/CFexpress 读卡器）。"""
    if os.name != "nt":
        return []
    drives: List[Path] = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for letter_idx in range(26):
        if bitmask & (1 << letter_idx):
            drive_str = f"{chr(65 + letter_idx)}:\\"
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(drive_str)
            if drive_type == DRIVE_REMOVABLE:
                drives.append(Path(drive_str))
    return drives


def wait_for_debounce(drive_root: Path, debounce_sec: float = 3.0) -> bool:
    """
    3 秒防抖等待：
    防止 SD 卡刚插入时 Windows 资源管理器缩略图/索引服务仍在挂载或占用文件句柄。
    连续确认文件集合与大小在 debounce_sec 内稳定且首个文件可读后返回 True。
    """
    logger.info("检测到新可移动介质 %s，启动 %.1f 秒防抖稳定性检测...", drive_root, debounce_sec)
    last_snapshot: Optional[Tuple[int, int]] = None
    stable_since: Optional[float] = None
    deadline = time.monotonic() + max(debounce_sec * 4, 15.0)

    while time.monotonic() < deadline:
        if not drive_root.exists():
            return False
        try:
            scan_target = drive_root / "DCIM" if (drive_root / "DCIM").exists() else drive_root
            files = scan_media_files(scan_target)
            snapshot = (len(files), sum(f.stat().st_size for f in files))
        except OSError:
            snapshot = None

        if snapshot and snapshot[0] > 0:
            if snapshot == last_snapshot:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= debounce_sec:
                    # 再试探打开第一个文件确保句柄无独占锁
                    try:
                        with open(files[0], "rb") as test_f:
                            test_f.read(64)
                        return True
                    except OSError:
                        stable_since = time.monotonic()
            else:
                last_snapshot = snapshot
                stable_since = time.monotonic()
        time.sleep(0.5)

    return False


def watch_removable_cards(
    event_name: str,
    venue: str = "",
    nas_root: Optional[Path] = None,
    preview_root: Optional[Path] = None,
    event_code: Optional[str] = None,
    debounce_sec: float = 3.0,
    poll_interval: float = 1.5,
    auto_cluster: bool = True,
    auto_notify: Optional[bool] = None,
) -> None:
    """Windows 热插拔 SD 卡持续监听模式：插卡 -> 自动拷卡 -> 自动分类入库 -> 自动发给客户。"""
    if os.name != "nt":
        raise RuntimeError("热插拔监听模式仅支持 Windows 平台")

    logger.info(
        "进入 Windows SD 卡热插拔监听模式（防抖=%.1fs，活动=%s，场地=%s，自动分类=%s，自动推客=%s）... 按 Ctrl+C 退出",
        debounce_sec,
        event_name,
        venue or "-",
        auto_cluster,
        auto_notify if auto_notify is not None else os.getenv("AUTO_PUSH_GALLERY", "true"),
    )
    known_drives = set(get_windows_removable_drives())
    if known_drives:
        logger.info("启动时已存在的可移动盘符（将忽略直到重新插拔）: %s", [str(d) for d in known_drives])

    try:
        while True:
            current_drives = set(get_windows_removable_drives())
            newly_inserted = current_drives - known_drives

            for drive in sorted(newly_inserted):
                if wait_for_debounce(drive, debounce_sec=debounce_sec):
                    src = drive / "DCIM" if (drive / "DCIM").exists() else drive
                    try:
                        # 动态探测 NAS，若离线且允许本地兜底则落盘到 work/raw_local_backup，绝不漏拷
                        target_nas = nas_root
                        if target_nas is None:
                            try:
                                target_nas = resolve_nas_root()
                            except RuntimeError:
                                if str(os.getenv("INGEST_LOCAL_FALLBACK", "true")).lower() != "false":
                                    target_nas = WORKSPACE_ROOT / "work" / "raw_local_backup"
                                    target_nas.mkdir(parents=True, exist_ok=True)
                                    logger.warning("NAS 当前不可达，自动启用本地高速缓存备份目录: %s", target_nas)
                                else:
                                    raise

                        effective_event = (
                            event_name
                            or os.getenv("AUTO_WATCH_EVENT_NAME")
                            or f"自动拷卡_{datetime.now().strftime('%Y%m%d')}"
                        )
                        manifest = ingest_directory(
                            source_dir=src,
                            event_name=effective_event,
                            venue=venue,
                            nas_root=target_nas,
                            preview_root=preview_root,
                            event_code=event_code,
                        )
                        if auto_cluster and manifest.get("manifest_path"):
                            logger.info("拷卡完成，自动触发废片预筛、角色分类入库与客户推送...")
                            from cull_and_cluster import process_batch_manifest

                            process_batch_manifest(
                                manifest_path=Path(manifest["manifest_path"]),
                                auto_notify=auto_notify,
                            )
                    except Exception as exc:
                        logger.error("自动拷卡/分类处理失败 (%s): %s", drive, exc)
                else:
                    logger.info("介质 %s 未发现有效图像或未通过防抖检测，已跳过", drive)

            known_drives = current_drives
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info("收到退出信号，已停止热插拔监听。")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ZERO STUDIO 自动拷卡与流式双校验模块")
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--source", type=str, help="手动指定源卡/目录路径")
    mode_group.add_argument("--watch", action="store_true", help="启动 Windows SD 卡热插拔监听模式（3秒防抖）")

    parser.add_argument("--event", type=str, default="", help="活动名称（--watch 留空时自动读取 AUTO_WATCH_EVENT_NAME 或按日期命名）")
    parser.add_argument("--venue", type=str, default="", help="拍摄场馆/地点")
    parser.add_argument("--event-code", type=str, default=None, help="可选批次活动短码（用于 batch_id）")
    parser.add_argument("--nas-root", type=str, default=None, help="显式覆盖 NAS 根目录")
    parser.add_argument("--preview-dir", type=str, default=None, help="显式覆盖 WebP 预览图输出目录")
    parser.add_argument("--preview-xmp", type=str, default=None, help="显式指定预览调色预设 XMP 路径（覆盖 PREVIEW_XMP_PATH）")
    parser.add_argument("--debounce", type=float, default=3.0, help="热插拔防抖秒数（默认 3.0）")
    parser.add_argument("--auto-cluster", action="store_true", help="--watch 拷卡完成后自动执行分类入库")
    parser.add_argument("--auto-notify", action="store_true", help="分类入库完成后自动推送选图链接给客户")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    preview_root = Path(args.preview_dir) if args.preview_dir else None
    preview_xmp = Path(args.preview_xmp) if args.preview_xmp else None

    if args.watch:
        explicit_nas = Path(args.nas_root) if args.nas_root else None
        watch_removable_cards(
            event_name=args.event or (os.getenv("AUTO_WATCH_EVENT_NAME") or ""),
            venue=args.venue,
            nas_root=explicit_nas,
            preview_root=preview_root,
            event_code=args.event_code,
            debounce_sec=args.debounce,
            auto_cluster=True if args.auto_cluster else True,
            auto_notify=True if args.auto_notify else None,
        )
    else:
        if not args.event:
            raise SystemExit("手动 --source 模式必须提供 --event 活动名称")
        nas_root = resolve_nas_root(explicit_nas_root=args.nas_root)
        manifest = ingest_directory(
            source_dir=Path(args.source),
            event_name=args.event,
            venue=args.venue,
            nas_root=nas_root,
            preview_root=preview_root,
            event_code=args.event_code,
            preview_xmp_path=preview_xmp,
        )
        print(json.dumps({"batch_id": manifest["batch_id"], "manifest_path": manifest["manifest_path"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
