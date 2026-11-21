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
    "saturation": (-18.0, 15.0),
    "luminance": (-12.0, 15.0),
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
  "needs_local_inpaint": false,
  "inpaint_region": null,
  "inpaint_prompt": "",
  "summary": "中文简明调色摘要（<=80字）"
}
注：inpaint_region 若启用局部去杂物，必须为归一化坐标 [x1, y1, x2, y2] (0.0~1.0)，严禁框选人脸与服设。"""


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
    """去掉模型可能包裹的 ```json ... ``` 代码块，只保留 JSON 主体。"""
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
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


def load_retouch_skills(skills_dir: Optional[Path] = None) -> Tuple[str, List[str]]:
    """热加载 configs/retouch_skills/*.md 下的全部规则文档，无需重启或改代码。"""
    sdir = Path(skills_dir or DEFAULT_SKILLS_DIR)
    if not sdir.exists():
        return "", []
    md_files = sorted(sdir.glob("*.md"), key=lambda p: p.name.lower())
    chunks: List[str] = []
    loaded_names: List[str] = []
    for f in md_files:
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

    return {
        "exposure_delta": exposure_delta,
        "temp_delta": temp_delta,
        "tint_delta": tint_delta,
        "hsl_adjustments": safe_hsl,
        "skin_luminance": skin_luminance,
        "curves_points": safe_curves,
        "needs_local_inpaint": needs_inpaint,
        "inpaint_region": inpaint_region,
        "inpaint_prompt": inpaint_prompt,
        "summary": str(plan.get("summary") or "按 Cosplay 仿色规则生成精修微调方案").strip()[:120],
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
    ) -> Dict[str, Any]:
        """输入纯画面 1024px sRGB 预览图 + 可选参考图 + 热加载 Skill 规则，输出安全钳位后的 JSON 方案。"""
        skills_text, loaded_skills = load_retouch_skills(self.skills_dir)

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
            "max_tokens": 2500,
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
            "max_tokens": 2000,
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
    def _data_url(image_path: Path) -> str:
        suffix = image_path.suffix.lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix, "image/jpeg")
        b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
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
