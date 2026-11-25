#!/usr/bin/env python3
"""
Gemini 多模态仿色大脑：
- 热加载 configs/retouch_skills/*.md 规则库（新增 .md 笔记无需改代码自动注入）；
- 输入 PS 导出的纯画面 1024px sRGB 预览图 + 目标仿色参考图（如有）；
- 输出严格 JSON Schema 调色方案，并在代码层实施安全边界钳位（防止极端参数修崩画面与越界重绘）。
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

ENGINE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = ENGINE_DIR.parents[1]
load_dotenv(WORKSPACE_ROOT / ".env", override=False)

logger = logging.getLogger("retouch_engine.gemini_colorist")

DEFAULT_SKILLS_DIR = WORKSPACE_ROOT / "configs" / "retouch_skills"

# 代码层安全边界钳位表（硬红线）
SAFETY_BOUNDS = {
    "exposure_delta": (-1.5, 1.5),
    "temp_delta": (-15.0, 15.0),
    "tint_delta": (-15.0, 15.0),
    "skin_luminance": (-10.0, 15.0),
}
HSL_CHANNELS = ("reds", "oranges", "yellows", "greens", "aquas", "blues", "purples", "magentas")
HSL_DEFAULT_BOUNDS = {
    "hue": (-20.0, 20.0),
    "saturation": (-30.0, 30.0),
    "luminance": (-25.0, 25.0),
}
# 亚洲人肤色主通道 oranges 实行更严格保护边界
HSL_SKIN_ORANGE_BOUNDS = {
    "hue": (-8.0, 8.0),
    "saturation": (-22.0, 15.0),
    "luminance": (-12.0, 28.0),
}
MAX_CURVE_DEVIATION = 35
MAX_INPAINT_AREA_RATIO = 0.35

GEMINI_JSON_CONTRACT_PROMPT = """你必须严格仅输出一个合法的 JSON 对象（不要包含 Markdown 代码块或任何额外解释文字），Schema 如下：
{
  "exposure_delta": 0.0,
  "temp_delta": 0.0,
  "tint_delta": 0.0,
  "hsl_adjustments": {
    "reds": {"hue": 0, "saturation": 0, "luminance": 0},
    "oranges": {"hue": 0, "saturation": 0, "luminance": 0},
    "yellows": {"hue": 0, "saturation": 0, "luminance": 0},
    "greens": {"hue": 0, "saturation": 0, "luminance": 0},
    "aquas": {"hue": 0, "saturation": 0, "luminance": 0},
    "blues": {"hue": 0, "saturation": 0, "luminance": 0},
    "purples": {"hue": 0, "saturation": 0, "luminance": 0},
    "magentas": {"hue": 0, "saturation": 0, "luminance": 0}
  },
  "skin_luminance": 0.0,
  "curves_points": [[0, 0], [64, 64], [128, 128], [192, 192], [255, 255]],
  "selective_color": [
    {"color_range": "reds", "cyan": -4, "magenta": 2, "yellow": -8, "black": -14},
    {"color_range": "whites", "cyan": -4, "magenta": 0, "yellow": -4, "black": -4}
  ],
  "subject_mask_adjust": {
    "enabled": true,
    "exposure_delta": 0.0,
    "temp_delta": 0.0,
    "feather_px": 8
  },
  "background_mask_adjust": {
    "enabled": true,
    "exposure_delta": 0.0,
    "temp_delta": 0.0,
    "saturation_delta": 0,
    "feather_px": 12
  },
  "skin_protect_mask": {
    "enabled": true,
    "fuzziness": 38,
    "exposure_delta": 0.0,
    "temp_delta": 0.0,
    "feather_px": 6
  },
  "finishing": {
    "high_pass_sharpen": true,
    "high_pass_radius": 1.8,
    "high_pass_opacity": 45,
    "neutral_gray_layer": true,
    "frequency_separation": false,
    "freq_sep_radius": 6.0
  },
  "distraction_boxes": [
    {"label": "边缘穿帮元素说明（如左侧路人/右下角柔光箱），无则输出空数组", "box": [0.0, 0.0, 0.1, 0.1]}
  ],
  "needs_local_inpaint": false,
  "inpaint_region": null,
  "inpaint_prompt": "",
  "summary": "中文简明调色摘要（<=80字）"
}
强制分区调色原则：
1. 暗场馆/多灯场照绝不允许靠全局大幅拉 exposure_delta (+0.6以上) 或抬黑场把暗背景拉灰！全局曝光保持克制（[-0.3, +0.4]），把人物提亮放进 subject_mask_adjust，把杂乱背景压暗压冷放进 background_mask_adjust！
2. 当环境降温（temp_delta < 0）时，必须开启 skin_protect_mask 给肤色轻微提亮微暖，防止人脸与四肢发灰发青！
3. 必须仔细检查画面四周边缘是否存在穿帮路人、柔光箱、灯架、垃圾，如有请全部标注到 distraction_boxes（归一化坐标 [x1,y1,x2,y2]），并对最抢镜的一处背景杂物（严禁框选 Coser 本人且面积<=35%）设置 needs_local_inpaint=true。"""


# ==================== 反馈修订契约 ====================
# 用户看完预览图后写一句自然语言意见，模型据此输出「在现有 PSD 之上的增量修改」。
# 关键约束：这是**增量**，不是重做；只描述相对上一版的改变量。
GEMINI_REVISION_CONTRACT_PROMPT = """你必须严格仅输出一个合法的 JSON 对象（不要包含 Markdown 代码块或任何额外解释文字），Schema 如下：
{
  "deltas": {
    "exposure_delta": 0.0,
    "temp_delta": 0.0,
    "tint_delta": 0.0,
    "hsl_adjustments": {
      "reds": {"hue": 0, "saturation": 0, "luminance": 0},
      "oranges": {"hue": 0, "saturation": 0, "luminance": 0},
      "yellows": {"hue": 0, "saturation": 0, "luminance": 0},
      "greens": {"hue": 0, "saturation": 0, "luminance": 0},
      "aquas": {"hue": 0, "saturation": 0, "luminance": 0},
      "blues": {"hue": 0, "saturation": 0, "luminance": 0},
      "purples": {"hue": 0, "saturation": 0, "luminance": 0},
      "magentas": {"hue": 0, "saturation": 0, "luminance": 0}
    },
    "skin_luminance": 0.0,
    "curves_points": [[0, 0], [64, 64], [128, 128], [192, 192], [255, 255]]
  },
  "jsx": "",
  "summary": "中文说明这次改了什么（<=80字）",
  "confidence": 0.0
}
规则：
1. deltas 里的数值是**相对上一版的变化量**，不是绝对值。只想局部微调时，其余字段全部填 0。
2. 如果用户的意见无法用调色参数表达（例如「把背景换掉」「去掉画面外那个人」「加个光晕」），
   才填写 jsx 字段，里面放可直接在 Photoshop 里执行的 ExtendScript 代码。
   - jsx 必须使用 ES3 语法（var、无箭头函数、无 let/const、无 JSON 对象）
   - 操作对象为 app.activeDocument（当前已打开的是上一版 PSD）
   - 严禁关闭文档、严禁保存到新路径、严禁弹出对话框
3. 用户没有提到的方面一律不要改（保持 0 / 空字符串）。
4. confidence 是 0.0~1.0 的置信度；低于 0.3 时说明你没把握，仍要返回最保守的修改。"""


def _sanitize_revision_jsx(jsx: str) -> Tuple[bool, str]:
    """对 AI 给出的修订 jsx 做安全审查。

    修订脚本只应「改当前文档」，绝不允许：
      - 关闭/保存覆盖文档（会毁掉 PSD 版本链）
      - 打开任意文件、执行系统命令、调用外部程序
      - 弹出对话框卡住无人值守流程
    """
    code = str(jsx or "")
    if not code.strip():
        return False, "脚本为空"
    if len(code) > 20000:
        return False, "脚本过长（>20000 字符）"

    lowered = code.lower()

    # 绝对不能碰的文档生命周期操作
    forbidden = [
        ("app.activedocument.close", "关闭文档"),
        ("documents.close", "关闭文档"),
        ("doc.close", "关闭文档"),
        ("saved", "保存会覆盖版本链"),
        ("saveas", "另存会污染交付目录"),
        ("app.open", "打开文件"),
        ("app.system", "系统调用"),
        ("system.call", "系统调用"),
        ("system(" , "系统调用"),
        ("execute(", "执行外部程序"),
        ("$.evalfile", "执行外部脚本"),
        ("new file(", "文件系统写入"),
        (".remove()", "删除对象"),
        ("alert(", "弹窗会阻塞无人值守"),
        ("confirm(", "弹窗会阻塞无人值守"),
    ]
    for token, desc in forbidden:
        if token in lowered:
            return False, f"包含禁用操作（{desc}）"

    # 必须作用于当前文档
    if "app.activedocument" not in lowered:
        return False, "未引用 app.activeDocument，可能误操作其它文档"

    return True, ""


def _strip_code_fence(text: str) -> str:
    """去掉模型可能包裹的 ```json ... ``` 代码块或前后说明，只保留 JSON 主体。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
        if t.lower().startswith("json"):
            t = t[4:].lstrip()
    # 兜底：截取第一个 { 到最后一个 }
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        t = t[start:end + 1]
    return t.strip()


def load_retouch_skills(
    skills_dir: Optional[Path] = None,
    selected_skills: Optional[List[str]] = None,
) -> Tuple[str, List[str]]:
    """热加载 configs/retouch_skills/*.md 下的规则文档；支持传入 selected_skills 仅加载勾选的 Skill。"""
    sdir = Path(skills_dir or DEFAULT_SKILLS_DIR)
    if not sdir.exists():
        return "", []
    md_files = sorted(sdir.glob("*.md"), key=lambda p: p.name.lower())
    allow_set = (
        {
            item
            for raw in selected_skills
            for s in (str(raw).strip().lower(),)
            if s
            for item in (s, s if s.endswith(".md") else f"{s}.md")
        }
        if selected_skills is not None
        else None
    )
    chunks: List[str] = []
    loaded_names: List[str] = []
    for f in md_files:
        if allow_set is not None and f.name.lower() not in allow_set:
            continue
        try:
            content = f.read_text(encoding="utf-8").strip()
            if content:
                chunks.append(f"=== Skill: {f.name} ===\n{content}")
                loaded_names.append(f.name)
        except OSError as exc:
            logger.warning("读取修图 Skill 文件失败 (%s): %s", f, exc)
    return "\n\n".join(chunks), loaded_names


def _clamp_float(val: Any, lo: float, hi: float, field_name: str, clamped_fields: List[str]) -> float:
    try:
        num = float(val)
    except (TypeError, ValueError):
        num = 0.0
    if num < lo:
        clamped_fields.append(field_name)
        return lo
    if num > hi:
        clamped_fields.append(field_name)
        return hi
    return round(num, 3)


def clamp_retouch_plan(raw_plan: Dict[str, Any]) -> Dict[str, Any]:
    """
    对 Gemini 输出的修图方案实施代码层硬安全边界钳位：
    - 防止极端曝光/色温/HSL/曲线修崩画面或破坏肤色；
    - 防止局部生图蒙版越界超过整图 35%（强制执行严禁整图重绘红线）；
    - 记录 clamped_fields 便于摄影师终审台核验。
    """
    clamped_fields: List[str] = []
    plan = raw_plan if isinstance(raw_plan, dict) else {}

    exposure_delta = _clamp_float(
        plan.get("exposure_delta", 0.0), *SAFETY_BOUNDS["exposure_delta"], "exposure_delta", clamped_fields
    )
    temp_delta = _clamp_float(
        plan.get("temp_delta", 0.0), *SAFETY_BOUNDS["temp_delta"], "temp_delta", clamped_fields
    )
    tint_delta = _clamp_float(
        plan.get("tint_delta", 0.0), *SAFETY_BOUNDS["tint_delta"], "tint_delta", clamped_fields
    )
    skin_luminance = _clamp_float(
        plan.get("skin_luminance", 0.0), *SAFETY_BOUNDS["skin_luminance"], "skin_luminance", clamped_fields
    )

    # HSL 8 通道安全钳位（oranges 肤色通道实行特严保护）
    raw_hsl = plan.get("hsl_adjustments") if isinstance(plan.get("hsl_adjustments"), dict) else {}
    safe_hsl: Dict[str, Dict[str, float]] = {}
    for ch in HSL_CHANNELS:
        ch_raw = raw_hsl.get(ch) if isinstance(raw_hsl.get(ch), dict) else {}
        bounds_map = HSL_SKIN_ORANGE_BOUNDS if ch == "oranges" else HSL_DEFAULT_BOUNDS
        safe_hsl[ch] = {
            metric: _clamp_float(ch_raw.get(metric, 0.0), lo, hi, f"hsl.{ch}.{metric}", clamped_fields)
            for metric, (lo, hi) in bounds_map.items()
        }

    # 曲线控制点单调性与最大偏移钳位
    raw_curves = plan.get("curves_points")
    safe_curves: List[List[int]] = []
    if isinstance(raw_curves, list) and len(raw_curves) >= 2:
        parsed_pts: List[Tuple[int, int]] = []
        for pt in raw_curves:
            if isinstance(pt, (list, tuple)) and len(pt) == 2:
                try:
                    x = int(round(float(pt[0])))
                    y = int(round(float(pt[1])))
                    cx = max(0, min(255, x))
                    lo_y = max(0, cx - MAX_CURVE_DEVIATION)
                    hi_y = min(255, cx + MAX_CURVE_DEVIATION)
                    cy = max(lo_y, min(hi_y, y))
                    if cx != x or cy != y:
                        clamped_fields.append(f"curves_points[{cx}]")
                    parsed_pts.append((cx, cy))
                except (TypeError, ValueError):
                    continue
        parsed_pts.sort(key=lambda p: p[0])
        # 去重相同 x 并保证 y 单调不下降（防反相太阳化）
        dedup_pts: List[List[int]] = []
        prev_y = 0
        for cx, cy in parsed_pts:
            if dedup_pts and dedup_pts[-1][0] == cx:
                continue
            if cy < prev_y:
                cy = prev_y
                clamped_fields.append(f"curves_monotonic[{cx}]")
            dedup_pts.append([cx, cy])
            prev_y = cy
        safe_curves = dedup_pts

    if len(safe_curves) < 2:
        safe_curves = [[0, 0], [64, 64], [128, 128], [192, 192], [255, 255]]

    # 局部生图区域校验：禁止面积超过 35% 的大面积重绘
    needs_inpaint = bool(plan.get("needs_local_inpaint", False))
    inpaint_region: Optional[List[float]] = None
    inpaint_prompt = str(plan.get("inpaint_prompt") or "").strip()[:200]

    raw_region = plan.get("inpaint_region")
    if needs_inpaint and isinstance(raw_region, (list, tuple)) and len(raw_region) == 4:
        try:
            x1, y1, x2, y2 = [max(0.0, min(1.0, float(v))) for v in raw_region]
            if x2 > x1 and y2 > y1:
                area = (x2 - x1) * (y2 - y1)
                if area > MAX_INPAINT_AREA_RATIO:
                    # 超过 35% 红线直接禁用局部生图并记录钳位
                    clamped_fields.append("inpaint_region_area_exceeded")
                    needs_inpaint = False
                else:
                    inpaint_region = [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]
            else:
                needs_inpaint = False
        except (TypeError, ValueError):
            needs_inpaint = False
    elif needs_inpaint:
        needs_inpaint = False

    if not needs_inpaint:
        inpaint_region = None
        inpaint_prompt = ""

    # 分区蒙版调色与收尾参数安全钳位（主体蒙版 / 背景反选蒙版 / 肤色保护蒙版 / 收尾 / 可选颜色 / 穿帮框）
    raw_subj = plan.get("subject_mask_adjust") if isinstance(plan.get("subject_mask_adjust"), dict) else {}
    subject_mask_adjust = {
        "enabled": bool(raw_subj.get("enabled", False)),
        "exposure_delta": _clamp_float(raw_subj.get("exposure_delta", 0.0), -1.0, 1.2, "subject_mask.exposure_delta", clamped_fields),
        "temp_delta": _clamp_float(raw_subj.get("temp_delta", 0.0), -12.0, 12.0, "subject_mask.temp_delta", clamped_fields),
        "feather_px": _clamp_float(raw_subj.get("feather_px", 8.0), 0.0, 60.0, "subject_mask.feather_px", clamped_fields),
    }

    raw_bg = plan.get("background_mask_adjust") if isinstance(plan.get("background_mask_adjust"), dict) else {}
    background_mask_adjust = {
        "enabled": bool(raw_bg.get("enabled", False)),
        "exposure_delta": _clamp_float(raw_bg.get("exposure_delta", 0.0), -1.5, 0.8, "background_mask.exposure_delta", clamped_fields),
        "temp_delta": _clamp_float(raw_bg.get("temp_delta", 0.0), -15.0, 15.0, "background_mask.temp_delta", clamped_fields),
        "saturation_delta": _clamp_float(raw_bg.get("saturation_delta", 0.0), -40.0, 20.0, "background_mask.saturation_delta", clamped_fields),
        "feather_px": _clamp_float(raw_bg.get("feather_px", 12.0), 0.0, 80.0, "background_mask.feather_px", clamped_fields),
    }

    raw_skin = plan.get("skin_protect_mask") if isinstance(plan.get("skin_protect_mask"), dict) else {}
    skin_protect_mask = {
        "enabled": bool(raw_skin.get("enabled", False)),
        "fuzziness": int(round(_clamp_float(raw_skin.get("fuzziness", 38), 10.0, 90.0, "skin_mask.fuzziness", clamped_fields))),
        "exposure_delta": _clamp_float(raw_skin.get("exposure_delta", 0.0), -0.5, 0.8, "skin_mask.exposure_delta", clamped_fields),
        "temp_delta": _clamp_float(raw_skin.get("temp_delta", 0.0), -8.0, 10.0, "skin_mask.temp_delta", clamped_fields),
        "feather_px": _clamp_float(raw_skin.get("feather_px", 6.0), 0.0, 40.0, "skin_mask.feather_px", clamped_fields),
    }

    raw_fin = plan.get("finishing") if isinstance(plan.get("finishing"), dict) else {}
    finishing = {
        "high_pass_sharpen": bool(raw_fin.get("high_pass_sharpen", False)),
        "high_pass_radius": _clamp_float(raw_fin.get("high_pass_radius", 1.8), 0.5, 6.0, "finishing.high_pass_radius", clamped_fields),
        "high_pass_opacity": _clamp_float(raw_fin.get("high_pass_opacity", 45.0), 10.0, 85.0, "finishing.high_pass_opacity", clamped_fields),
        "neutral_gray_layer": bool(raw_fin.get("neutral_gray_layer", False)),
        "frequency_separation": bool(raw_fin.get("frequency_separation", False)),
        "freq_sep_radius": _clamp_float(raw_fin.get("freq_sep_radius", 6.0), 2.0, 16.0, "finishing.freq_sep_radius", clamped_fields),
    }

    safe_selective: List[Dict[str, Any]] = []
    raw_sc = plan.get("selective_color")
    if isinstance(raw_sc, list):
        for sc in raw_sc[:4]:
            if isinstance(sc, dict) and sc.get("color_range"):
                safe_selective.append({
                    "color_range": str(sc["color_range"]).strip().lower(),
                    "cyan": _clamp_float(sc.get("cyan", 0.0), -30.0, 30.0, "selective_color.cyan", clamped_fields),
                    "magenta": _clamp_float(sc.get("magenta", 0.0), -30.0, 30.0, "selective_color.magenta", clamped_fields),
                    "yellow": _clamp_float(sc.get("yellow", 0.0), -30.0, 30.0, "selective_color.yellow", clamped_fields),
                    "black": _clamp_float(sc.get("black", 0.0), -30.0, 30.0, "selective_color.black", clamped_fields),
                })

    safe_distractions: List[Dict[str, Any]] = []
    raw_dist = plan.get("distraction_boxes")
    if isinstance(raw_dist, list):
        for item in raw_dist[:8]:
            if isinstance(item, dict) and isinstance(item.get("box"), (list, tuple)) and len(item["box"]) == 4:
                try:
                    bx = [round(max(0.0, min(1.0, float(v))), 4) for v in item["box"]]
                    if bx[2] > bx[0] and bx[3] > bx[1]:
                        safe_distractions.append({
                            "label": str(item.get("label") or "边缘穿帮").strip()[:60],
                            "box": bx,
                        })
                except (TypeError, ValueError):
                    continue

    return {
        "exposure_delta": exposure_delta,
        "temp_delta": temp_delta,
        "tint_delta": tint_delta,
        "hsl_adjustments": safe_hsl,
        "skin_luminance": skin_luminance,
        "curves_points": safe_curves,
        "selective_color": safe_selective,
        "subject_mask_adjust": subject_mask_adjust,
        "background_mask_adjust": background_mask_adjust,
        "skin_protect_mask": skin_protect_mask,
        "finishing": finishing,
        "distraction_boxes": safe_distractions,
        "needs_local_inpaint": needs_inpaint,
        "inpaint_region": inpaint_region,
        "inpaint_prompt": inpaint_prompt,
        "summary": str(plan.get("summary") or "按 Cosplay 分区蒙版仿色规则生成精修方案").strip()[:120],
        "clamped_fields": sorted(set(clamped_fields)),
    }


def _encode_image_part(image_path: Path) -> Dict[str, Any]:
    ext = image_path.suffix.lower()
    mime = "image/jpeg"
    if ext == ".png":
        mime = "image/png"
    elif ext == ".webp":
        mime = "image/webp"
    return {
        "inline_data": {
            "mime_type": mime,
            "data": base64.b64encode(image_path.read_bytes()).decode("ascii"),
        }
    }


class GeminiColorist:
    """调用 Gemini 多模态 API（支持注入 mock_fn 进行离线测试）。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        skills_dir: Optional[Path] = None,
        mock_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        timeout: float = 60.0,
        base_url: Optional[str] = None,
    ):
        # 优先用统一的视觉大脑配置，兼容旧的 GEMINI_API_KEY
        self.api_key = (
            api_key
            if api_key is not None
            else (
                os.getenv("VISION_API_KEY")
                or os.getenv("GEMINI_API_KEY")
                or os.getenv("DEEPSEEK_API_KEY", "")
            )
        ).strip()
        self.model = (
            model
            or os.getenv("VISION_MODEL")
            or os.getenv("GEMINI_RETOUCH_MODEL")
            or "gemini-2.5-pro"
        ).strip()
        # 配置了 VISION_BASE_URL 就按 OpenAI 兼容协议调用（中转站），否则走 Google 原生接口
        self.base_url = (
            base_url if base_url is not None else (os.getenv("VISION_BASE_URL") or "")
        ).strip().rstrip("/")
        self.skills_dir = Path(skills_dir or DEFAULT_SKILLS_DIR)
        self.mock_fn = mock_fn
        self.timeout = timeout

    def generate_plan(
        self,
        srgb_preview_path: Path,
        preset_code: str,
        preset_notes: str = "",
        character: str = "",
        event_name: str = "",
        reference_image_path: Optional[Path] = None,
        selected_skills: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """输入纯画面 1024px sRGB 预览图 + 可选参考图 + 勾选的 Skill 规则，输出安全钳位后的 JSON 方案。"""
        skills_text, loaded_skills = load_retouch_skills(self.skills_dir, selected_skills=selected_skills)

        context = {
            "srgb_preview_path": str(srgb_preview_path),
            "reference_image_path": str(reference_image_path) if reference_image_path else None,
            "preset_code": preset_code,
            "preset_notes": preset_notes,
            "character": character,
            "event_name": event_name,
            "loaded_skills": loaded_skills,
            "skills_text": skills_text,
        }

        if self.mock_fn is not None:
            raw_plan = self.mock_fn(context)
        elif self.base_url:
            raw_plan = self._call_openai_compatible(
                srgb_preview_path=Path(srgb_preview_path),
                reference_image_path=Path(reference_image_path) if reference_image_path else None,
                skills_text=skills_text,
                preset_code=preset_code,
                preset_notes=preset_notes,
                character=character,
                event_name=event_name,
            )
        else:
            raw_plan = self._call_gemini_rest(
                srgb_preview_path=Path(srgb_preview_path),
                reference_image_path=Path(reference_image_path) if reference_image_path else None,
                skills_text=skills_text,
                preset_code=preset_code,
                preset_notes=preset_notes,
                character=character,
                event_name=event_name,
            )

        safe_plan = clamp_retouch_plan(raw_plan)
        safe_plan["model"] = self.model if self.mock_fn is None else f"mock:{self.model}"
        safe_plan["loaded_skills"] = loaded_skills
        if safe_plan["clamped_fields"]:
            logger.warning("Gemini 方案存在越界参数，已安全钳位: %s", safe_plan["clamped_fields"])
        return safe_plan

    # ==================== 终盘视觉整体检验 + 逆推回退裁判 ====================

    def review_final_retouch(
        self,
        original_preview_path: Path,
        final_preview_path: Path,
        checkpoints: List[Dict[str, Any]],
        selected_skills: Optional[List[str]] = None,
        reference_image_path: Optional[Path] = None,
        preset_notes: str = "",
    ) -> Dict[str, Any]:
        """
        保存完最终成片后，由视觉大模型对照「原图 + 最终成片 (+ 参考图) + 检查点清单」进行整体质量检验：
        - 合格返回 passed=True, score(0~100), summary;
        - 不合格返回 passed=False, root_cause, rollback_to_checkpoint, fix_calls (标准 PS MCP 工具调用列表)。
        """
        skills_text, _ = load_retouch_skills(self.skills_dir, selected_skills=selected_skills)
        ckpt_brief = [
            {
                "id": c.get("id"),
                "step_end": c.get("step_end"),
                "tools": [s.get("tool") for s in (c.get("steps") or [])],
            }
            for c in (checkpoints or [])
        ]

        prompt = (
            f"{skills_text}\n\n"
            f"=== 终盘成品质量整体检验任务 ===\n"
            f"- 额外风格要求：{preset_notes or '按所选 Skill 标准验收'}\n"
            f"- 第一张图是修前原图预览；第二张图是 Photoshop 最终保存的成片预览；"
            f"{'第三张图是目标仿色参考图。' if reference_image_path and Path(reference_image_path).exists() else '无额外参考图。'}\n"
            f"- 检查点时间线：{json.dumps(ckpt_brief, ensure_ascii=False)}\n\n"
            "请从【主体曝光与通透感、背景压暗去杂色、肤色白皙自然度、高光是否过曝、暗部是否发灰】五个维度打分并审查。\n"
            "严格仅输出一个合法 JSON 对象：\n"
            "{\n"
            '  "passed": true,\n'
            '  "score": 88,\n'
            '  "summary": "中文检验评语（<=100字）",\n'
            '  "root_cause": "若 passed=false，说明哪一步出了问题；通过则为空字符串",\n'
            '  "rollback_to_checkpoint": "若需回退，填写检查点 id（如 ckpt_01 或 source）；通过则为空字符串",\n'
            '  "fix_calls": [\n'
            '    {"name": "ps_add_exposure_layer", "arguments": {"exposure_delta": 0.2, "layer_name": "ZS_Fix_Exp"}}\n'
            "  ]\n"
            "}\n"
            "注意：只有当画面存在明显缺陷（score < 78）时才将 passed 设为 false 并给出克制的 fix_calls；"
            "若已达到商业交付基线（score >= 78）请设为 passed=true 且 fix_calls=[]。"
        )

        if self.mock_fn is not None:
            return self.mock_fn({"mode": "review", "checkpoints": ckpt_brief, "prompt": prompt})

        if not self.api_key:
            return {
                "passed": True,
                "score": 80,
                "summary": "未配置云端视觉 API Key，跳过 AI 终盘视觉打分，直接交由人工终审。",
                "root_cause": "",
                "rollback_to_checkpoint": "",
                "fix_calls": [],
            }

        try:
            if self.base_url:
                content: List[Dict[str, Any]] = [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": self._data_url(Path(original_preview_path))}},
                    {"type": "image_url", "image_url": {"url": self._data_url(Path(final_preview_path))}},
                ]
                if reference_image_path and Path(reference_image_path).exists():
                    content.append({"type": "image_url", "image_url": {"url": self._data_url(Path(reference_image_path))}})
                body = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": content}],
                    "temperature": 0.15,
                    "max_tokens": 8192,
                }
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                        json=body,
                    )
                    resp.raise_for_status()
                    text_out = str((resp.json().get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
                parsed = json.loads(_strip_code_fence(text_out))
            else:
                parts: List[Dict[str, Any]] = [
                    {"text": prompt},
                    _encode_image_part(Path(original_preview_path)),
                    _encode_image_part(Path(final_preview_path)),
                ]
                if reference_image_path and Path(reference_image_path).exists():
                    parts.append(_encode_image_part(Path(reference_image_path)))
                endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(
                        endpoint,
                        json={"contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0.15, "responseMimeType": "application/json"}},
                    )
                    resp.raise_for_status()
                    text_out = str(resp.json()["candidates"][0]["content"]["parts"][0]["text"]).strip()
                parsed = json.loads(_strip_code_fence(text_out))

            return {
                "passed": bool(parsed.get("passed", True)),
                "score": int(parsed.get("score", 82)),
                "summary": str(parsed.get("summary") or "AI 终盘检验完成").strip()[:160],
                "root_cause": str(parsed.get("root_cause") or "").strip(),
                "rollback_to_checkpoint": str(parsed.get("rollback_to_checkpoint") or "").strip(),
                "fix_calls": parsed.get("fix_calls") if isinstance(parsed.get("fix_calls"), list) else [],
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI 终盘视觉检验调用异常，放行交由人工终审: %s", exc)
            return {
                "passed": True,
                "score": 0,
                "summary": f"AI 终盘检验异常（{exc}），已保留分层 PSD 交由人工终审",
                "root_cause": "",
                "rollback_to_checkpoint": "",
                "fix_calls": [],
            }

    # ==================== 反馈修订（看图 + 用户意见 -> 增量修改） ====================

    def generate_revision(
        self,
        preview_image_path: Path,
        feedback_text: str,
        version: int = 1,
        previous_summary: str = "",
        preset_code: str = "",
    ) -> Dict[str, Any]:
        """用户看完预览图写了一句意见，据此生成「在现有 PSD 之上」的增量修改。

        返回:
            {
              "deltas": {...钳位后的调色增量...},
              "jsx": "可选的 ExtendScript（无法用参数表达时）",
              "summary": "这次改了什么",
              "confidence": 0.0~1.0,
              "version": 目标版本号,
              "model": ...
            }
        """
        feedback = str(feedback_text or "").strip()
        if not feedback:
            raise ValueError("用户意见不能为空")

        skills_text, loaded_skills = load_retouch_skills(self.skills_dir)
        preview_path = Path(preview_image_path)
        if not preview_path.exists():
            raise FileNotFoundError(f"预览图不存在: {preview_path}")

        context = {
            "preview_image_path": str(preview_path),
            "feedback_text": feedback,
            "version": int(version),
            "previous_summary": previous_summary,
            "preset_code": preset_code,
            "skills_text": skills_text,
            "loaded_skills": loaded_skills,
        }

        if self.mock_fn is not None:
            raw = self.mock_fn(context)
        else:
            raw = self._call_revision_model(
                preview_path=preview_path,
                feedback_text=feedback,
                version=int(version),
                previous_summary=previous_summary,
                preset_code=preset_code,
                skills_text=skills_text,
            )

        return self._normalize_revision(raw, version=int(version), loaded_skills=loaded_skills)

    def _call_revision_model(
        self,
        preview_path: Path,
        feedback_text: str,
        version: int,
        previous_summary: str,
        preset_code: str,
        skills_text: str,
    ) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("未配置视觉模型 API Key（VISION_API_KEY）")

        text_prompt = (
            f"{skills_text}\n\n"
            f"=== 反馈修订任务 ===\n"
            f"- 这是第 {version} 版的修订\n"
            f"- 上一版做了什么: {previous_summary or '（未知）'}\n"
            f"- 出片预设: {preset_code or '（未知）'}\n"
            f"- 图：当前预览（也就是上一版修完的样子）\n"
            f"- 摄影师的意见：{feedback_text}\n\n"
            f"请只针对这条意见给出**增量**修改。\n\n"
            f"{GEMINI_REVISION_CONTRACT_PROMPT}"
        )

        content: List[Dict[str, Any]] = [{"type": "text", "text": text_prompt}]
        content.append({"type": "image_url", "image_url": {"url": self._data_url(preview_path)}})

        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.2,
            "max_tokens": 8192,
        }

        if self.base_url:
            endpoint = f"{self.base_url}/chat/completions"
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(endpoint, json=body, headers=headers)
                if resp.status_code >= 400:
                    raise RuntimeError(f"视觉模型调用失败 (HTTP {resp.status_code}): {resp.text[:400]}")
                data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError(f"视觉模型返回空候选结果: {str(data)[:300]}")
            text_out = str(choices[0].get("message", {}).get("content") or "").strip()
            return json.loads(_strip_code_fence(text_out))

        # Google 原生接口
        payload = {
            "contents": [{
                "parts": [
                    {"text": text_prompt},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": base64.b64encode(preview_path.read_bytes()).decode("ascii"),
                        }
                    },
                ]
            }],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2500},
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"Gemini 调用失败 (HTTP {resp.status_code}): {resp.text[:400]}")
            data = resp.json()
        try:
            text_out = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Gemini 返回结构异常: {str(data)[:300]}") from exc
        return json.loads(_strip_code_fence(text_out))

    @staticmethod
    def _normalize_revision(raw: Dict[str, Any], version: int, loaded_skills: List[str]) -> Dict[str, Any]:
        """把模型输出规整成可执行增量：调色参数走钳位，jsx 走安全检查。"""
        raw = raw if isinstance(raw, dict) else {}

        raw_deltas = raw.get("deltas")
        if not isinstance(raw_deltas, dict):
            raw_deltas = raw          # 容忍模型直接把字段写在顶层

        safe_deltas = clamp_retouch_plan(raw_deltas)
        deltas = {
            "exposure_delta": safe_deltas.get("exposure_delta", 0.0),
            "temp_delta": safe_deltas.get("temp_delta", 0.0),
            "tint_delta": safe_deltas.get("tint_delta", 0.0),
            "hsl_adjustments": safe_deltas.get("hsl_adjustments", {}),
            "skin_luminance": safe_deltas.get("skin_luminance", 0.0),
            "curves_points": safe_deltas.get("curves_points"),
        }

        jsx = str(raw.get("jsx") or "").strip()
        jsx_warning = ""
        if jsx:
            ok, reason = _sanitize_revision_jsx(jsx)
            if not ok:
                jsx_warning = f"AI 给出的 jsx 被安全策略拒绝：{reason}（本次只应用调色增量）"
                jsx = ""

        try:
            confidence = float(raw.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        out = {
            "deltas": deltas,
            "jsx": jsx,
            "summary": str(raw.get("summary") or "").strip() or "按摄影师意见做了微调",
            "confidence": confidence,
            "version": int(version),
            "loaded_skills": loaded_skills,
            "clamped_fields": safe_deltas.get("clamped_fields", []),
        }
        if jsx_warning:
            out["jsx_warning"] = jsx_warning
        return out

    def _call_openai_compatible(
        self,
        srgb_preview_path: Path,
        reference_image_path: Optional[Path],
        skills_text: str,
        preset_code: str,
        preset_notes: str,
        character: str,
        event_name: str,
    ) -> Dict[str, Any]:
        """按 OpenAI 兼容协议调用中转站/OpenAI 的多模态模型。

        只有一个职责：看图后输出 Camera Raw 参数 JSON。
        真正的调色在 Photoshop 里完成，AI 不生成任何图片。
        """
        if not self.api_key:
            raise RuntimeError("未配置视觉模型 API Key（VISION_API_KEY）")

        text_prompt = (
            f"{skills_text}\n\n"
            f"=== 当前修图任务 ===\n"
            f"- 目标预设代码: {preset_code}\n"
            f"- 预设说明: {preset_notes or '按规则库执行'}\n"
            f"- 角色: {character or '未知'}\n"
            f"- 活动: {event_name or '未知'}\n"
            f"- 第一张图是 PS 导出的纯画面 1024px sRGB 原图预览；"
            f"{'第二张图是目标仿色参考图。' if reference_image_path and reference_image_path.exists() else '无额外参考图。'}\n\n"
            f"{GEMINI_JSON_CONTRACT_PROMPT}"
        )

        content: List[Dict[str, Any]] = [{"type": "text", "text": text_prompt}]
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": self._data_url(srgb_preview_path)},
            }
        )
        if reference_image_path and reference_image_path.exists():
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._data_url(reference_image_path)},
                }
            )

        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.2,
            "max_tokens": 8192,
        }

        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(endpoint, json=body, headers=headers)
            if resp.status_code >= 400:
                raise RuntimeError(f"视觉模型调用失败 (HTTP {resp.status_code}): {resp.text[:400]}")
            data = resp.json()

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"视觉模型返回空候选结果: {str(data)[:300]}")
        text_out = str(choices[0].get("message", {}).get("content") or "").strip()
        text_out = _strip_code_fence(text_out)
        return json.loads(text_out)

    @staticmethod
    def _data_url(image_path: Path, max_edge: int = 1024, quality: int = 82) -> str:
        """将图片安全缩放到 max_edge 以内并压缩为轻量 JPEG，防止超大图触发网关 HTTP 413。"""
        raw_bytes = image_path.read_bytes()
        try:
            import io
            from PIL import Image, ImageOps

            with Image.open(io.BytesIO(raw_bytes)) as im:
                im = ImageOps.exif_transpose(im)
                w, h = im.size
                needs_resize = max(w, h) > max_edge or len(raw_bytes) > 320 * 1024 or image_path.suffix.lower() not in (".jpg", ".jpeg")
                if needs_resize:
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    elif im.mode == "L":
                        im = im.convert("RGB")
                    if max(w, h) > max_edge:
                        im.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=quality, optimize=True)
                    raw_bytes = buf.getvalue()
                    b64 = base64.b64encode(raw_bytes).decode("ascii")
                    return f"data:image/jpeg;base64,{b64}"
        except Exception:
            pass

        suffix = image_path.suffix.lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix, "image/jpeg")
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        return f"data:{mime};base64,{b64}"

    def _call_gemini_rest(
        self,
        srgb_preview_path: Path,
        reference_image_path: Optional[Path],
        skills_text: str,
        preset_code: str,
        preset_notes: str,
        character: str,
        event_name: str,
    ) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("未配置 GEMINI_API_KEY，无法调用 Gemini 多模态仿色 API")

        parts: List[Dict[str, Any]] = [
            {
                "text": (
                    f"{skills_text}\n\n"
                    f"=== 当前修图任务 ===\n"
                    f"- 目标预设代码: {preset_code}\n"
                    f"- 预设说明: {preset_notes or '按规则库执行'}\n"
                    f"- 角色: {character or '未知'}\n"
                    f"- 活动: {event_name or '未知'}\n"
                    f"- 第一张图是 PS 导出的纯画面 1024px sRGB 原图预览；"
                    f"{'第二张图是目标仿色参考图。' if reference_image_path and reference_image_path.exists() else '无额外参考图。'}\n\n"
                    f"{GEMINI_JSON_CONTRACT_PROMPT}"
                )
            },
            _encode_image_part(srgb_preview_path),
        ]

        if reference_image_path and reference_image_path.exists():
            parts.append(_encode_image_part(reference_image_path))

        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(endpoint, json=body)
            resp.raise_for_status()
            data = resp.json()

        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini 返回空候选结果: {data}")
        text_out = candidates[0]["content"]["parts"][0]["text"].strip()
        if text_out.startswith("```"):
            text_out = text_out.strip("`").lstrip("json").strip()
        return json.loads(text_out)
