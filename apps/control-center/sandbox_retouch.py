#!/usr/bin/env python3
"""
ZERO STUDIO 单图仿色与修图实验室引擎 (Color-Match & Retouch Sandbox)
流程：
1. 读取测试图（JPG/PNG/WEBP/ARW/CR3），生成 1024px sRGB 原图对照；
2. 若选择已导入的 XMP 预设，解析并套用 XMP 基础参数；
3. 热加载全部已启用修图 Skill (*.md)，调用真实 Gemini 多模态 API（附带可选参考图）生成仿色与修图方案 JSON；
   （若未配置 GEMINI_API_KEY 或网络不可达，自动降级为基于参考图统计的本地安全仿色方案并标注模式）；
4. 本地实时渲染调色结果图（曝光、色温、色调、曲线 LUT、HSL 8 通道、肤色提亮）；
5. 若用户勾选且本机 Photoshop UXP Bridge 在线，同步推送至 Photoshop 执行。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
from PIL import Image, ImageOps

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
RETOUCH_ENGINE_DIR = WORKSPACE_ROOT / "pipelines" / "retouch-engine"
if str(RETOUCH_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(RETOUCH_ENGINE_DIR))

from gemini_colorist import GeminiColorist, clamp_retouch_plan, load_retouch_skills  # noqa: E402
from xmp_raw_stage import (  # noqa: E402
    RAW_EXTENSIONS,
    apply_xmp_to_float_rgb,
    parse_xmp_params,
    resolve_preset_entry,
)

try:
    import rawpy  # type: ignore
except ImportError:
    rawpy = None

logger = logging.getLogger("control_center.sandbox_retouch")


def load_image_as_float01(image_path: Path, max_side: int = 1024) -> np.ndarray:
    """读取 JPG/PNG/WEBP/ARW/CR3 并缩放至 1024px sRGB float32 [0, 1]。"""
    ext = image_path.suffix.lower()
    if ext in RAW_EXTENSIONS:
        if rawpy is None:
            raise RuntimeError("当前环境未安装 rawpy，无法解码 .ARW/.CR3 原始文件")
        with rawpy.imread(str(image_path)) as raw:
            rgb16 = raw.postprocess(
                use_camera_wb=True,
                no_auto_bright=True,
                output_bps=16,
                output_color=rawpy.ColorSpace.sRGB,
            )
        img_f = np.clip(rgb16.astype(np.float32) / 65535.0, 0.0, 1.0)
        h, w = img_f.shape[:2]
        if max(h, w) > max_side:
            scale = max_side / float(max(h, w))
            img_f = cv2.resize(img_f, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        return np.clip(img_f, 0.0, 1.0)

    with Image.open(image_path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        arr = np.asarray(im, dtype=np.float32) / 255.0
        return np.clip(arr, 0.0, 1.0)


def save_float01_as_jpeg(img_f: np.ndarray, out_path: Path, quality: int = 92) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    u8 = np.clip(np.round(img_f * 255.0), 0, 255).astype(np.uint8)
    Image.fromarray(u8, mode="RGB").save(out_path, format="JPEG", quality=quality)


def _build_curve_lut(curves_points: List[List[int]]) -> np.ndarray:
    if not curves_points or len(curves_points) < 2:
        return np.linspace(0.0, 1.0, 256, dtype=np.float32)
    pts = sorted([(int(p[0]), int(p[1])) for p in curves_points], key=lambda x: x[0])
    if pts[0][0] > 0:
        pts.insert(0, (0, 0))
    if pts[-1][0] < 255:
        pts.append((255, 255))
    xs = np.array([p[0] for p in pts], dtype=np.float32)
    ys = np.array([p[1] for p in pts], dtype=np.float32)
    lut = np.interp(np.arange(256, dtype=np.float32), xs, ys) / 255.0
    return np.clip(lut, 0.0, 1.0).astype(np.float32)


def apply_plan_to_float_rgb(img_f: np.ndarray, plan: Dict[str, Any]) -> np.ndarray:
    """将 Gemini 仿色 JSON 方案实时渲染到 float32 RGB 图像上。"""
    out = img_f.astype(np.float32).copy()

    # 1. Exposure delta
    exp = float(plan.get("exposure_delta") or 0.0)
    if abs(exp) > 1e-4:
        out = out * (2.0 ** exp)

    # 2. Temperature & Tint delta (-15..+15)
    temp = float(plan.get("temp_delta") or 0.0)
    tint = float(plan.get("tint_delta") or 0.0)
    if abs(temp) > 1e-4:
        shift = temp * 0.008
        out[:, :, 0] *= (1.0 + shift)
        out[:, :, 2] *= (1.0 - shift)
    if abs(tint) > 1e-4:
        g_shift = tint * 0.006
        out[:, :, 1] *= (1.0 - g_shift)

    out = np.clip(out, 0.0, 1.0)

    # 3. Curves LUT
    curves = plan.get("curves_points")
    if isinstance(curves, list) and len(curves) >= 2:
        lut = _build_curve_lut(curves)
        idx = np.clip(np.round(out * 255.0), 0, 255).astype(np.int32)
        out = lut[idx]

    # 4. HSL 8 通道微调 + 肤色提亮 (HSV 近似实现)
    hsl = plan.get("hsl_adjustments") or {}
    skin_lum = float(plan.get("skin_luminance") or 0.0)
    has_hsl = any(
        isinstance(v, dict) and (abs(float(v.get("hue", 0))) > 0.1 or abs(float(v.get("saturation", 0))) > 0.1 or abs(float(v.get("luminance", 0))) > 0.1)
        for v in hsl.values()
    )
    if has_hsl or abs(skin_lum) > 0.1:
        u8 = np.clip(np.round(out * 255.0), 0, 255).astype(np.uint8)
        hsv = cv2.cvtColor(u8, cv2.COLOR_RGB2HSV).astype(np.float32)
        h_ch = hsv[:, :, 0] * 2.0  # 0..360 deg
        s_ch = hsv[:, :, 1] / 255.0
        v_ch = hsv[:, :, 2] / 255.0

        # OpenCV hue ranges (degrees 0..360)
        channel_centers = {
            "reds": 0.0,
            "oranges": 28.0,
            "yellows": 58.0,
            "greens": 115.0,
            "aquas": 180.0,
            "blues": 225.0,
            "purples": 275.0,
            "magentas": 320.0,
        }
        for ch_name, center in channel_centers.items():
            adj = hsl.get(ch_name)
            if not isinstance(adj, dict):
                continue
            dh = float(adj.get("hue", 0.0))
            ds = float(adj.get("saturation", 0.0))
            dl = float(adj.get("luminance", 0.0))
            if ch_name == "oranges" and abs(skin_lum) > 0.1:
                dl += skin_lum * 0.8
            if abs(dh) < 0.1 and abs(ds) < 0.1 and abs(dl) < 0.1:
                continue
            dist = np.minimum(np.abs(h_ch - center), 360.0 - np.abs(h_ch - center))
            weight = np.clip(1.0 - dist / 35.0, 0.0, 1.0) * np.clip(s_ch * 2.5, 0.0, 1.0)
            h_ch = (h_ch + dh * 0.4 * weight) % 360.0
            s_ch = np.clip(s_ch * (1.0 + (ds / 100.0) * weight), 0.0, 1.0)
            v_ch = np.clip(v_ch * (1.0 + (dl / 120.0) * weight), 0.0, 1.0)

        hsv[:, :, 0] = (h_ch / 2.0) % 180.0
        hsv[:, :, 1] = np.clip(s_ch * 255.0, 0, 255)
        hsv[:, :, 2] = np.clip(v_ch * 255.0, 0, 255)
        rgb_u8 = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)
        out = rgb_u8.astype(np.float32) / 255.0

    return np.clip(out, 0.0, 1.0)


def _compute_fallback_plan(
    src_f: np.ndarray,
    ref_f: Optional[np.ndarray],
    preset_code: str,
    loaded_skills: List[str],
    reason_prefix: str,
) -> Dict[str, Any]:
    """当未配置 GEMINI_API_KEY 或网络不可达时，基于图像/参考图直方图生成安全合理的本地仿色方案。"""
    src_mean = float(src_f.mean())
    exp_delta = 0.0
    temp_delta = 0.0
    tint_delta = 0.0
    skin_lum = 4.0

    if ref_f is not None:
        ref_mean = float(ref_f.mean())
        exp_delta = max(-0.8, min(0.8, (ref_mean - src_mean) * 1.8))
        src_rb = float(src_f[:, :, 0].mean() - src_f[:, :, 2].mean())
        ref_rb = float(ref_f[:, :, 0].mean() - ref_f[:, :, 2].mean())
        temp_delta = max(-10.0, min(10.0, (ref_rb - src_rb) * 45.0))
        src_g = float(src_f[:, :, 1].mean() - 0.5 * (src_f[:, :, 0].mean() + src_f[:, :, 2].mean()))
        ref_g = float(ref_f[:, :, 1].mean() - 0.5 * (ref_f[:, :, 0].mean() + ref_f[:, :, 2].mean()))
        tint_delta = max(-8.0, min(8.0, (src_g - ref_g) * 40.0))
        summary = f"{reason_prefix}已按参考图亮度与冷暖分布完成局部仿色微调（加载 {len(loaded_skills)} 条 Skill）"
    else:
        if preset_code == "warm_film_daily":
            temp_delta, exp_delta, skin_lum = 4.5, 0.15, 5.0
        elif preset_code == "high_contrast_studio":
            temp_delta, exp_delta, skin_lum = -1.5, 0.1, 3.0
        elif preset_code == "cyber_studio":
            temp_delta, tint_delta, skin_lum = -5.0, 3.0, 4.0
        else:
            temp_delta, exp_delta, skin_lum = -2.5, 0.2, 5.5
        summary = f"{reason_prefix}已按「{preset_code or '默认通透'}」基调完成曝光、色温与肤色提亮微调"

    raw_plan = {
        "exposure_delta": round(exp_delta, 3),
        "temp_delta": round(temp_delta, 3),
        "tint_delta": round(tint_delta, 3),
        "hsl_adjustments": {
            "reds": {"hue": 0, "saturation": 3, "luminance": 2},
            "oranges": {"hue": 0, "saturation": -4, "luminance": 5},
            "yellows": {"hue": -4, "saturation": -8, "luminance": 2},
            "greens": {"hue": 4, "saturation": -10, "luminance": 0},
            "aquas": {"hue": 0, "saturation": 6, "luminance": 2},
            "blues": {"hue": -2, "saturation": 8, "luminance": -2},
            "purples": {"hue": 0, "saturation": 2, "luminance": 0},
            "magentas": {"hue": 0, "saturation": 0, "luminance": 0},
        },
        "skin_luminance": skin_lum,
        "curves_points": [[0, 0], [64, 58], [128, 132], [192, 198], [255, 255]],
        "needs_local_inpaint": False,
        "inpaint_region": None,
        "inpaint_prompt": "",
        "summary": summary,
    }
    return clamp_retouch_plan(raw_plan)


def run_sandbox_pipeline(
    image_path: Path,
    run_dir: Path,
    preset_code: str,
    ref_image_path: Optional[Path],
    send_to_ps: bool,
    cfg: Dict[str, str],
    skills_dir: Path,
    presets_map_path: Path,
    retouch_xmp_dir: Path,
) -> Dict[str, Any]:
    """执行单图仿色试修完整管线并返回结构化结果。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    orig_f = load_image_as_float01(image_path, max_side=1024)
    orig_out = run_dir / "original.jpg"
    save_float01_as_jpeg(orig_f, orig_out)

    # Stage 1: Resolve & apply XMP preset
    preset_entry = resolve_preset_entry(
        preset_code=preset_code,
        presets_map_path=presets_map_path,
        retouch_xmp_dir=retouch_xmp_dir,
    ) if preset_code else {}
    xmp_path: Optional[Path] = preset_entry.get("resolved_xmp_path")
    xmp_params: Dict[str, float] = {}
    base_f = orig_f.copy()
    if xmp_path and xmp_path.exists():
        try:
            xmp_params = parse_xmp_params(xmp_path)
            base_f = apply_xmp_to_float_rgb(base_f, xmp_params)
        except Exception as exc:
            logger.warning("沙盒解析 XMP (%s) 失败: %s", xmp_path, exc)

    base_out = run_dir / "xmp_base.jpg"
    save_float01_as_jpeg(base_f, base_out)

    # 若用户未上传参考图，则自动沿用 presets_map.json 绑定的参考图
    effective_ref: Optional[Path] = ref_image_path
    if effective_ref is None and preset_entry.get("resolved_reference_path"):
        cand_ref = Path(preset_entry["resolved_reference_path"])
        if cand_ref.exists():
            effective_ref = cand_ref

    ref_f: Optional[np.ndarray] = None
    ref_saved_name = ""
    if effective_ref and effective_ref.exists():
        ref_f = load_image_as_float01(effective_ref, max_side=768)
        save_float01_as_jpeg(ref_f, run_dir / "reference.jpg")
        ref_saved_name = "reference.jpg"

    # Stage 2: Load enabled skills & invoke vision model (只出调色参数，图片由 PS 处理)
    _, loaded_skills = load_retouch_skills(skills_dir)
    api_key = (
        cfg.get("VISION_API_KEY")
        or cfg.get("GEMINI_API_KEY")
        or ""
    ).strip()
    model = (
        cfg.get("VISION_MODEL")
        or cfg.get("GEMINI_RETOUCH_MODEL")
        or "gemini-2.5-pro"
    ).strip()
    base_url = (cfg.get("VISION_BASE_URL") or "").strip().rstrip("/")
    gemini_mode = "fallback_local"
    gemini_note = ""

    if api_key:
        # 主模型失败时按顺序尝试备用模型，避免单一模型维护导致整条链路降级
        fallback_models = [
            m.strip()
            for m in (cfg.get("VISION_FALLBACK_MODELS") or "").split(",")
            if m.strip()
        ]
        candidates = [model] + [m for m in fallback_models if m != model]
        last_err = None
        plan = None
        for cand in candidates:
            try:
                colorist = GeminiColorist(
                    api_key=api_key,
                    model=cand,
                    skills_dir=skills_dir,
                    timeout=90.0,
                    base_url=base_url or None,
                )
                plan = colorist.generate_plan(
                    srgb_preview_path=base_out,
                    preset_code=preset_code or "default",
                    preset_notes=str(preset_entry.get("default_notes") or ""),
                    reference_image_path=effective_ref,
                )
                gemini_mode = "vision_live"
                gemini_note = f"已调用视觉模型 {cand} 生成 PS 调色参数，注入 {len(loaded_skills)} 条 Skill"
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning("视觉模型 %s 调用失败: %s", cand, exc)

        if plan is None:
            logger.warning("所有视觉模型均失败，回退本地仿色: %s", last_err)
            gemini_mode = "fallback_local"
            gemini_note = f"视觉模型调用失败已安全降级本地仿色：{last_err}"
            plan = _compute_fallback_plan(base_f, ref_f, preset_code, loaded_skills, "[本地降级] ")
    else:
        gemini_note = "未配置 VISION_API_KEY，当前使用本地仿色引擎（配置后自动切换真实 AI 决策）"
        plan = _compute_fallback_plan(base_f, ref_f, preset_code, loaded_skills, "[本地演练] ")

    # Stage 3: Render color-matched result locally
    result_f = apply_plan_to_float_rgb(base_f, plan)
    result_out = run_dir / "result.jpg"
    save_float01_as_jpeg(result_f, result_out)

    # Stage 4: Optional Photoshop UXP Bridge push
    ps_result: Dict[str, Any] = {"sent": False, "skipped": True, "message": "未勾选推送至 Photoshop"}
    if send_to_ps:
        ps_url = (cfg.get("PS_UXP_BRIDGE_URL") or "http://127.0.0.1:8765").rstrip("/")
        ps_token = (cfg.get("PS_UXP_BRIDGE_TOKEN") or "").strip()
        try:
            import httpx
            headers = {"X-PS-Bridge-Token": ps_token} if ps_token else {}
            with httpx.Client(timeout=8.0, trust_env=False) as client:
                r = client.post(
                    f"{ps_url}/api/ps/apply-plan",
                    headers=headers,
                    json={
                        "input_path": str(base_out),
                        "output_path": str(run_dir / "ps_output.jpg"),
                        "plan": plan,
                        "action_set": preset_entry.get("ps_action_set") or "ZERO_STUDIO",
                        "action_name": preset_entry.get("ps_action_name") or "",
                    },
                )
                ps_result = {
                    "sent": r.status_code < 400,
                    "status_code": r.status_code,
                    "response": r.text[:300],
                }
        except Exception as exc:
            ps_result = {"sent": False, "error": f"Photoshop UXP Bridge 未连接或执行失败: {exc}"}

    return {
        "status": "ok",
        "preset_code": preset_code,
        "preset_name": preset_entry.get("name") or preset_code or "无预设(原图)",
        "xmp_applied": str(xmp_path.name) if xmp_path else "",
        "xmp_params": xmp_params,
        "loaded_skills": loaded_skills,
        "gemini_mode": gemini_mode,
        "gemini_note": gemini_note,
        "reasoning": plan.get("summary") or "",
        "plan_json": plan,
        "original_file": "original.jpg",
        "xmp_base_file": "xmp_base.jpg",
        "result_file": "result.jpg",
        "reference_file": ref_saved_name,
        "ps_result": ps_result,
    }
