#!/usr/bin/env python3
"""
正式修图前置阶段：只读读取 NAS 原始 .ARW/.CR3，套用 presets_map.json 指定的正式修图 .xmp，
静默导出 16-bit TIFF 至本地高速工作区（防止直接向 Photoshop 喂 RAW 触发 ACR 弹窗死锁）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Optional

import cv2
import numpy as np
from dotenv import load_dotenv
from PIL import Image, ImageOps

try:
    import rawpy  # type: ignore
except ImportError:
    rawpy = None

ENGINE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = ENGINE_DIR.parents[1]
load_dotenv(WORKSPACE_ROOT / ".env", override=False)

logger = logging.getLogger("retouch_engine.xmp_raw_stage")

RAW_EXTENSIONS = {".arw", ".cr3"}
DEFAULT_PRESETS_MAP = WORKSPACE_ROOT / "configs" / "presets_map.json"


def load_presets_map(presets_map_path: Optional[Path] = None) -> Dict[str, Any]:
    path = Path(presets_map_path or DEFAULT_PRESETS_MAP)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_preset_entry(
    preset_code: str,
    presets_map_path: Optional[Path] = None,
    retouch_xmp_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """根据 preset_code 解析正式修图 XMP 路径、参考图路径与 PS Action 配置。"""
    pmap = load_presets_map(presets_map_path)
    entry = dict(pmap.get(preset_code) or pmap.get("cyan_air_event") or {})
    xmp_dir = Path(
        retouch_xmp_dir
        or os.getenv("RETOUCH_XMP_DIR")
        or str(WORKSPACE_ROOT / "configs" / "retouch_xmp")
    )

    xmp_file = entry.get("xmp_file") or ""
    xmp_path: Optional[Path] = None
    if xmp_file:
        cand = Path(xmp_file)
        if not cand.is_absolute():
            cand = xmp_dir / xmp_file
        if cand.exists():
            xmp_path = cand

    ref_img = entry.get("reference_image") or ""
    ref_path: Optional[Path] = None
    if ref_img:
        rc = Path(ref_img)
        if not rc.is_absolute():
            rc = WORKSPACE_ROOT / ref_img
        if rc.exists():
            ref_path = rc

    entry["resolved_xmp_path"] = xmp_path
    entry["resolved_reference_path"] = ref_path
    return entry


def parse_xmp_params(xmp_path: Path) -> Dict[str, float]:
    """解析正式修图 .xmp 的核心曝光、阴影、高光、色温、色调、对比度、饱和度标签。"""
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

    def _strip(tag: str) -> str:
        return tag.split("}", 1)[1] if "}" in tag else tag

    for elem in root.iter():
        for k, v in elem.attrib.items():
            key = _strip(k)
            if key in target_keys:
                try:
                    params[key] = float(str(v).strip())
                except ValueError:
                    pass
        tname = _strip(elem.tag)
        if tname in target_keys and elem.text and elem.text.strip():
            try:
                params[tname] = float(elem.text.strip())
            except ValueError:
                pass
    return params


def apply_xmp_to_float_rgb(img_float01: np.ndarray, params: Dict[str, float]) -> np.ndarray:
    """在高精度 float32 [0, 1] 空间执行正式 XMP 基础调色，避免 8-bit 断层。"""
    if not params:
        return img_float01

    img = img_float01.astype(np.float32, copy=True)

    exposure = float(params.get("Exposure2012", 0.0))
    if abs(exposure) > 1e-4:
        img *= float(2.0 ** np.clip(exposure, -4.0, 4.0))

    temp = params.get("Temperature")
    if temp is not None:
        shift = np.clip((temp - 5500.0) / 5500.0, -0.35, 0.35) if temp > 1000.0 else np.clip(temp / 100.0, -0.35, 0.35)
        img[:, :, 0] *= float(1.0 + shift * 0.25)
        img[:, :, 2] *= float(1.0 - shift * 0.25)

    tint = float(params.get("Tint", 0.0))
    if abs(tint) > 1e-4:
        t_shift = float(np.clip(tint / 150.0, -0.25, 0.25))
        img[:, :, 1] *= (1.0 - t_shift * 0.18)

    np.clip(img, 0.0, 1.0, out=img)

    lum = 0.2126 * img[:, :, 0] + 0.7152 * img[:, :, 1] + 0.0722 * img[:, :, 2]
    shadows = float(params.get("Shadows2012", 0.0))
    if abs(shadows) > 1e-4:
        s_strength = float(np.clip(shadows / 100.0, -1.0, 1.0))
        img += np.power(1.0 - lum, 2.0)[..., None] * (s_strength * 0.42) * (0.25 + 0.75 * img)

    highlights = float(params.get("Highlights2012", 0.0))
    if abs(highlights) > 1e-4:
        h_strength = float(np.clip(highlights / 100.0, -1.0, 1.0))
        img += np.power(lum, 2.0)[..., None] * (h_strength * 0.38)

    contrast = float(params.get("Contrast2012", 0.0))
    if abs(contrast) > 1e-4:
        c_factor = 1.0 + float(np.clip(contrast / 100.0, -0.6, 0.8)) * 0.5
        img = (img - 0.5) * c_factor + 0.5

    sat = float(params.get("Saturation", 0.0)) + float(params.get("Vibrance", 0.0)) * 0.5
    if abs(sat) > 1e-4:
        s_factor = 1.0 + float(np.clip(sat / 100.0, -0.8, 0.8)) * 0.5
        gray = (0.2126 * img[:, :, 0] + 0.7152 * img[:, :, 1] + 0.0722 * img[:, :, 2])[..., None]
        img = gray + (img - gray) * s_factor

    return np.clip(img, 0.0, 1.0)


def write_16bit_tiff_unicode(rgb_uint16: np.ndarray, out_tiff_path: Path) -> None:
    """兼容 Windows 中文路径写入真 16-bit RGB TIFF。"""
    out_tiff_path.parent.mkdir(parents=True, exist_ok=True)
    bgr_uint16 = cv2.cvtColor(rgb_uint16, cv2.COLOR_RGB2BGR)
    ok, encoded = cv2.imencode(".tiff", bgr_uint16)
    if not ok:
        raise IOError(f"编码 16-bit TIFF 失败: {out_tiff_path}")
    tmp_path = out_tiff_path.with_name(out_tiff_path.name + ".tmp")
    encoded.tofile(str(tmp_path))
    os.replace(tmp_path, out_tiff_path)


def convert_raw_with_xmp_to_tiff(
    nas_raw_path: Path,
    out_tiff_path: Path,
    preset_code: str = "cyan_air_event",
    xmp_path: Optional[Path] = None,
    presets_map_path: Optional[Path] = None,
    retouch_xmp_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    只读读取 NAS 原始 .ARW/.CR3（或测试 JPG），套用正式修图 .xmp，静默导出 16-bit TIFF 至本地高速工作区。
    NAS 原片绝不写入或修改。
    """
    nas_raw_path = Path(nas_raw_path)
    if not nas_raw_path.exists():
        raise FileNotFoundError(f"NAS 原片不存在: {nas_raw_path}")

    preset_entry = resolve_preset_entry(
        preset_code=preset_code,
        presets_map_path=presets_map_path,
        retouch_xmp_dir=retouch_xmp_dir,
    )
    resolved_xmp: Optional[Path] = xmp_path if (xmp_path and Path(xmp_path).exists()) else preset_entry.get("resolved_xmp_path")
    out_tiff_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. 优先尝试 darktable-cli 16-bit TIFF 静默导出
    darktable_bin = shutil.which("darktable-cli")
    if darktable_bin and resolved_xmp is not None:
        cmd = [
            darktable_bin,
            str(nas_raw_path),
            str(resolved_xmp),
            str(out_tiff_path),
            "--core",
            "--conf",
            "plugins/imageio/format/tiff/bpp=16",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        if proc.returncode == 0 and out_tiff_path.exists() and out_tiff_path.stat().st_size > 1024:
            logger.info("已通过 darktable-cli 导出 16-bit TIFF: %s", out_tiff_path)
            return {
                "tiff_path": str(out_tiff_path),
                "backend": "darktable-cli:16bit-tiff",
                "xmp_used": str(resolved_xmp),
                "bit_depth": 16,
            }

    # 2. 自定义 Adobe DNG/ACR 静默批处理命令（若配置 RETOUCH_ACR_CMD）
    acr_cmd_tpl = os.getenv("RETOUCH_ACR_CMD", "").strip()
    if acr_cmd_tpl and resolved_xmp is not None:
        cmd = acr_cmd_tpl.format(raw=str(nas_raw_path), xmp=str(resolved_xmp), out=str(out_tiff_path))
        proc = subprocess.run(cmd, shell=True, capture_output=True, timeout=180, check=False)
        if proc.returncode == 0 and out_tiff_path.exists() and out_tiff_path.stat().st_size > 1024:
            return {
                "tiff_path": str(out_tiff_path),
                "backend": "acr-cli:16bit-tiff",
                "xmp_used": str(resolved_xmp),
                "bit_depth": 16,
            }

    # 3. 高精度 Python rawpy (16-bit) / Pillow -> float32 XMP -> 真 16-bit TIFF
    xmp_params = parse_xmp_params(resolved_xmp) if resolved_xmp is not None else {}
    img_float01: Optional[np.ndarray] = None
    backend = "python:pillow-16bit-tiff"

    if nas_raw_path.suffix.lower() in RAW_EXTENSIONS and rawpy is not None:
        try:
            with rawpy.imread(str(nas_raw_path)) as raw:
                rgb16 = raw.postprocess(
                    use_camera_wb=True,
                    no_auto_bright=True,
                    output_bps=16,
                    output_color=rawpy.ColorSpace.sRGB,
                )
            img_float01 = rgb16.astype(np.float32) / 65535.0
            backend = "python:rawpy-16bit-tiff"
        except Exception as exc:
            logger.warning("rawpy 16-bit 解码失败，回退 Pillow: %s", exc)

    if img_float01 is None:
        with Image.open(nas_raw_path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            img_float01 = np.asarray(im, dtype=np.float32) / 255.0

    adjusted = apply_xmp_to_float_rgb(img_float01, xmp_params)
    rgb_uint16 = np.clip(np.round(adjusted * 65535.0), 0, 65535).astype(np.uint16)
    write_16bit_tiff_unicode(rgb_uint16, out_tiff_path)

    logger.info(
        "已静默生成 16-bit TIFF: %s -> %s (backend=%s, xmp=%s)",
        nas_raw_path.name,
        out_tiff_path.name,
        backend,
        resolved_xmp.name if resolved_xmp else "none",
    )
    return {
        "tiff_path": str(out_tiff_path),
        "backend": backend,
        "xmp_used": str(resolved_xmp) if resolved_xmp else None,
        "bit_depth": 16,
        "shape": list(rgb_uint16.shape),
    }
