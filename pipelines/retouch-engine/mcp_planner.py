#!/usr/bin/env python3
"""
ZERO STUDIO · 双模型接力修图编排器 (mcp_planner.py)
====================================================
架构：
    云端视觉大模型（香蕉 / Gemini Pro）
        ↓  只看 1 次 1024px 预览图，低成本输出高层修图方案（JSON / 自然语言要点）
    本地 8B 小模型（LM Studio / Ollama，如 Qwen3-8B / Qwen2.5-7B-Instruct，0 API 费）
        ↓  通过 OpenAI Tool Calling 将方案拆解为一系列 Photoshop MCP 原子工具调用
    PSMCPToolkit (ps_mcp_server.py -> Windows COM / Photoshop 原生调整层)
        ↓  保留 16-bit 可编辑 PSD 图层 + 导出 1024px 预览图

若本地 8B 服务未启动或超时，自动无缝降级为确定性 MCP 工具序列编译（deterministic_fallback），
确保修图产线零中断、参数始终受代码层安全钳位保护。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

ENGINE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = ENGINE_DIR.parents[1]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

load_dotenv(WORKSPACE_ROOT / ".env", override=False)

from adb_mcp_client import AdbMCPPhotoshopClient  # noqa: E402
from gemini_colorist import clamp_retouch_plan  # noqa: E402
from ps_mcp_server import PSMCPToolkit  # noqa: E402

logger = logging.getLogger("retouch_engine.mcp_planner")

ALLOWED_PLANNER_TOOLS = {
    "ps_add_exposure_layer",
    "ps_add_color_balance_layer",
    "ps_add_curves_layer",
    "ps_add_hsl_layer",
    "ps_add_selective_color_layer",
    "ps_add_levels_layer",
    "ps_create_neutral_gray_layer",
    "ps_create_frequency_separation",
    "ps_select_color_range",
    "ps_modify_selection",
    "ps_add_layer_mask_from_selection",
    "ps_set_layer_blend_opacity",
    "ps_convert_to_smart_object",
    "ps_apply_high_pass_sharpen",
    "ps_select_subject",
    "ps_deselect",
    "ps_add_vibrance_layer",
    "ps_set_clipping_mask",
    "ps_add_ambient_glow_layer",
    "ps_apply_liquify",
    "ps_run_action",
    "ps_execute_extendscript",
    "ps_execute_batchplay",
    "ps_get_canvas_preview",
}

LOCAL_8B_PLANNER_SYSTEM_PROMPT = """你是 ZERO STUDIO 的本地 Photoshop MCP 执行拆解器（8B 工具调用模型）。
你的任务：读取上级视觉大模型给出的修图方案（JSON 调色建议或自然语言修图指导），
将其拆解为有序、克制、非破坏性的 Photoshop MCP 工具调用序列。

【铁律】
1. 只调用提供的 Photoshop 调整图层与动作工具，绝不臆造不存在的工具名。
2. 遵循标准后期图层顺序：
   曝光度 (ps_add_exposure_layer) -> 色彩平衡 (ps_add_color_balance_layer) ->
   曲线 (ps_add_curves_layer) -> 分通道 HSL (ps_add_hsl_layer) ->
   可选颜色 (ps_add_selective_color_layer，仅在方案明确需要细调肤色/暗部青蓝时调用) ->
   动作组 (ps_run_action，仅当方案提供 action_set 与 action_name 时调用)。
3. 数值接近 0 的步骤直接跳过，不要创建无意义的空图层。
4. 严格尊重亚洲人肤色保护红线（oranges 色相 [-8,8]、饱和度 [-18,15]），切忌激进拉满。"""


def compile_bright_low_contrast_stack(layer_prefix: str = "BLC") -> List[Dict[str, Any]]:
    """
    严格按 `configs/retouch_skills/bright_low_contrast_cosplay_skill.md` 第二阶段编排的
    「亮调低反差 Cosplay」标准 Photoshop 分层 MCP 工具栈：
      1. 反选背景 -> 色阶压平反差 (明度混合模式) -> 自然饱和度 -65 -> 分通道 HSL 压制红/黄/绿/青蓝 -> 背景压暗
      2. 肤色色彩范围 (skinTones) -> 独立明度提亮 (不偏色)
      3. 主体选区 -> 曝光度+Gamma 恢复立体感 -> 自然饱和度 +16 色彩点睛
      4. 环境高光辉光 (滤色 18%) + 50% 双中性灰柔光图层
      5. 仅针对主体的高反差保留锐化 (2.0px, 线性光 30% + 主体蒙版不锐化背景)
    """
    return [
        # 1a. 背景反选 -> 色阶压平反差（明度混合模式）
        {"name": "ps_select_subject", "arguments": {}},
        {"name": "ps_modify_selection", "arguments": {"feather_px": 12.0, "invert": True}},
        {"name": "ps_add_levels_layer", "arguments": {"black_point": 8, "gamma": 1.0, "white_point": 245, "output_black": 10, "output_white": 240, "layer_name": f"{layer_prefix}_BG_Levels_Luma"}},
        {"name": "ps_set_layer_blend_opacity", "arguments": {"blend_mode": "luminosity", "opacity": 100.0}},
        {"name": "ps_deselect", "arguments": {}},
        # 1b. 背景反选 -> 自然饱和度大幅降低 (-65)
        {"name": "ps_select_subject", "arguments": {}},
        {"name": "ps_modify_selection", "arguments": {"feather_px": 12.0, "invert": True}},
        {"name": "ps_add_vibrance_layer", "arguments": {"vibrance": -65, "saturation": -10, "layer_name": f"{layer_prefix}_BG_Vibrance"}},
        {"name": "ps_deselect", "arguments": {}},
        # 1c. 背景反选 -> 分通道 HSL 压制（消除背景红牌子/绿杂色，保留暖调氛围）
        {"name": "ps_select_subject", "arguments": {}},
        {"name": "ps_modify_selection", "arguments": {"feather_px": 12.0, "invert": True}},
        {
            "name": "ps_add_hsl_layer",
            "arguments": {
                "hsl_adjustments": {
                    "reds": {"hue": 0, "saturation": -30, "luminance": -15},
                    "yellows": {"hue": 0, "saturation": -25, "luminance": -6},
                    "greens": {"hue": 0, "saturation": -30, "luminance": 0},
                    "aquas": {"hue": 0, "saturation": -20, "luminance": 6},
                    "blues": {"hue": 0, "saturation": -20, "luminance": 6},
                },
                "skin_luminance": 0.0,
                "layer_name": f"{layer_prefix}_BG_HSL",
            },
        },
        {"name": "ps_deselect", "arguments": {}},
        # 1d. 背景反选 -> 压暗背景突出主体
        {"name": "ps_select_subject", "arguments": {}},
        {"name": "ps_modify_selection", "arguments": {"feather_px": 14.0, "invert": True}},
        {"name": "ps_add_exposure_layer", "arguments": {"exposure_delta": -0.45, "gamma": 1.0, "layer_name": f"{layer_prefix}_BG_Darken"}},
        {"name": "ps_deselect", "arguments": {}},
        # 2. 主体肤色独立提亮（明度混合模式，白净通透不偏色）
        {"name": "ps_select_color_range", "arguments": {"preset": "skinTones", "fuzziness": 38}},
        {"name": "ps_modify_selection", "arguments": {"feather_px": 6.0, "invert": False}},
        {"name": "ps_add_exposure_layer", "arguments": {"exposure_delta": 0.28, "gamma": 1.0, "layer_name": f"{layer_prefix}_Skin_Bright_Luma"}},
        {"name": "ps_set_layer_blend_opacity", "arguments": {"blend_mode": "luminosity", "opacity": 85.0}},
        {"name": "ps_deselect", "arguments": {}},
        # 3. 主体恢复立体感（曝光 + Gamma 微调增强暗部扎实度）+ 色彩点睛
        {"name": "ps_select_subject", "arguments": {}},
        {"name": "ps_modify_selection", "arguments": {"feather_px": 8.0, "invert": False}},
        {"name": "ps_add_exposure_layer", "arguments": {"exposure_delta": 0.20, "gamma": 0.94, "layer_name": f"{layer_prefix}_Subject_Contrast"}},
        {"name": "ps_deselect", "arguments": {}},
        {"name": "ps_select_subject", "arguments": {}},
        {"name": "ps_modify_selection", "arguments": {"feather_px": 8.0, "invert": False}},
        {"name": "ps_add_vibrance_layer", "arguments": {"vibrance": 16, "saturation": 0, "layer_name": f"{layer_prefix}_Subject_Vibrance"}},
        {"name": "ps_deselect", "arguments": {}},
        # 4. 空气感高光辉光 + 50% 双中性灰柔光层
        {"name": "ps_add_ambient_glow_layer", "arguments": {"opacity": 18.0, "blend_mode": "screen", "layer_name": f"{layer_prefix}_AmbientGlow"}},
        {"name": "ps_create_neutral_gray_layer", "arguments": {"layer_name": f"{layer_prefix}_NeutralGray", "blend_mode": "softlight", "opacity": 100.0}},
        # 5. 仅针对主体的高反差保留锐化（2.0px, 线性光 30% + 主体蒙版不锐化背景）
        {"name": "ps_apply_high_pass_sharpen", "arguments": {"radius": 2.0, "blend_mode": "linearlight", "opacity": 30.0, "layer_name": f"{layer_prefix}_Subject_HighPass"}},
        {"name": "ps_select_subject", "arguments": {}},
        {"name": "ps_add_layer_mask_from_selection", "arguments": {"invert": False}},
        {"name": "ps_deselect", "arguments": {}},
    ]


def compile_plan_to_mcp_calls(
    plan: Dict[str, Any],
    ps_action: Optional[Dict[str, str]] = None,
    layer_prefix: str = "ZS",
) -> List[Dict[str, Any]]:
    """
    确定性回退编译器：将安全钳位后的方案直接编译为标准 PS MCP 工具调用序列。
    当本地 8B 模型未启动或未返回有效 tool_calls 时无缝兜底。
    """
    safe = clamp_retouch_plan(plan)
    calls: List[Dict[str, Any]] = []

    if abs(float(safe.get("exposure_delta") or 0.0)) > 1e-3:
        calls.append({
            "name": "ps_add_exposure_layer",
            "arguments": {
                "exposure_delta": safe["exposure_delta"],
                "layer_name": f"{layer_prefix}_Exposure",
            },
        })

    if abs(float(safe.get("temp_delta") or 0.0)) > 1e-2 or abs(float(safe.get("tint_delta") or 0.0)) > 1e-2:
        calls.append({
            "name": "ps_add_color_balance_layer",
            "arguments": {
                "temp_delta": safe["temp_delta"],
                "tint_delta": safe["tint_delta"],
                "layer_name": f"{layer_prefix}_ColorBalance",
            },
        })

    curves = safe.get("curves_points") or []
    is_identity = len(curves) >= 2 and all(abs(int(p[0]) - int(p[1])) <= 1 for p in curves)
    if len(curves) >= 2 and not is_identity:
        calls.append({
            "name": "ps_add_curves_layer",
            "arguments": {
                "curves_points": curves,
                "layer_name": f"{layer_prefix}_Curves",
            },
        })

    hsl = safe.get("hsl_adjustments") or {}
    skin_lum = float(safe.get("skin_luminance") or 0.0)
    has_hsl = abs(skin_lum) > 1e-2 or any(
        isinstance(v, dict) and any(abs(float(v.get(k) or 0.0)) > 1e-2 for k in ("hue", "saturation", "luminance"))
        for v in hsl.values()
    )
    if has_hsl:
        calls.append({
            "name": "ps_add_hsl_layer",
            "arguments": {
                "hsl_adjustments": hsl,
                "skin_luminance": skin_lum,
                "layer_name": f"{layer_prefix}_HSL",
            },
        })

    # 支持上级方案附带可选颜色建议（selective_color: [{color_range, cyan, magenta, yellow, black}]）
    sc_list = plan.get("selective_color")
    if isinstance(sc_list, list):
        for idx, sc in enumerate(sc_list[:3], start=1):
            if isinstance(sc, dict) and sc.get("color_range"):
                calls.append({
                    "name": "ps_add_selective_color_layer",
                    "arguments": {
                        "color_range": str(sc["color_range"]),
                        "cyan": float(sc.get("cyan") or 0.0),
                        "magenta": float(sc.get("magenta") or 0.0),
                        "yellow": float(sc.get("yellow") or 0.0),
                        "black": float(sc.get("black") or 0.0),
                        "relative": bool(sc.get("relative", True)),
                        "layer_name": f"{layer_prefix}_SelColor_{idx}",
                    },
                })

    # 5. 主体蒙版分区调色（ps_select_subject -> 羽化 -> 调整层自动带主体蒙版 -> 取消选区）
    subj = safe.get("subject_mask_adjust") or {}
    if isinstance(subj, dict) and subj.get("enabled"):
        s_exp = float(subj.get("exposure_delta") or 0.0)
        s_temp = float(subj.get("temp_delta") or 0.0)
        s_feather = float(subj.get("feather_px") or 8.0)
        if abs(s_exp) > 1e-3:
            calls.append({"name": "ps_select_subject", "arguments": {}})
            if s_feather > 0:
                calls.append({"name": "ps_modify_selection", "arguments": {"feather_px": s_feather, "invert": False}})
            calls.append({"name": "ps_add_exposure_layer", "arguments": {"exposure_delta": s_exp, "layer_name": f"{layer_prefix}_Subject_Exposure"}})
            calls.append({"name": "ps_deselect", "arguments": {}})
        if abs(s_temp) > 1e-2:
            calls.append({"name": "ps_select_subject", "arguments": {}})
            if s_feather > 0:
                calls.append({"name": "ps_modify_selection", "arguments": {"feather_px": s_feather, "invert": False}})
            calls.append({"name": "ps_add_color_balance_layer", "arguments": {"temp_delta": s_temp, "tint_delta": 0.0, "layer_name": f"{layer_prefix}_Subject_Color"}})
            calls.append({"name": "ps_deselect", "arguments": {}})

    # 6. 背景反选压暗分区（ps_select_subject -> 反选羽化 -> 调整层自动带背景蒙版 -> 取消选区）
    bg = safe.get("background_mask_adjust") or {}
    if isinstance(bg, dict) and bg.get("enabled"):
        b_exp = float(bg.get("exposure_delta") or 0.0)
        b_temp = float(bg.get("temp_delta") or 0.0)
        b_sat = float(bg.get("saturation_delta") or 0.0)
        b_feather = float(bg.get("feather_px") or 12.0)
        if abs(b_exp) > 1e-3:
            calls.append({"name": "ps_select_subject", "arguments": {}})
            calls.append({"name": "ps_modify_selection", "arguments": {"feather_px": b_feather, "invert": True}})
            calls.append({"name": "ps_add_exposure_layer", "arguments": {"exposure_delta": b_exp, "layer_name": f"{layer_prefix}_BG_Exposure"}})
            calls.append({"name": "ps_deselect", "arguments": {}})
        if abs(b_temp) > 1e-2:
            calls.append({"name": "ps_select_subject", "arguments": {}})
            calls.append({"name": "ps_modify_selection", "arguments": {"feather_px": b_feather, "invert": True}})
            calls.append({"name": "ps_add_color_balance_layer", "arguments": {"temp_delta": b_temp, "tint_delta": 0.0, "layer_name": f"{layer_prefix}_BG_Color"}})
            calls.append({"name": "ps_deselect", "arguments": {}})
        if abs(b_sat) > 1e-1:
            calls.append({"name": "ps_select_subject", "arguments": {}})
            calls.append({"name": "ps_modify_selection", "arguments": {"feather_px": b_feather, "invert": True}})
            calls.append({
                "name": "ps_add_hsl_layer",
                "arguments": {
                    "hsl_adjustments": {
                        "yellows": {"hue": 0, "saturation": b_sat, "luminance": 0},
                        "greens": {"hue": 0, "saturation": b_sat, "luminance": 0},
                    },
                    "skin_luminance": 0.0,
                    "layer_name": f"{layer_prefix}_BG_Desat",
                },
            })
            calls.append({"name": "ps_deselect", "arguments": {}})

    # 7. 肤色独立保护蒙版（色彩范围 skinTones -> 羽化 -> 提亮/微暖防染灰 -> 取消选区）
    skin = safe.get("skin_protect_mask") or {}
    if isinstance(skin, dict) and skin.get("enabled"):
        sk_exp = float(skin.get("exposure_delta") or 0.0)
        sk_temp = float(skin.get("temp_delta") or 0.0)
        sk_fuzz = int(skin.get("fuzziness") or 38)
        sk_feather = float(skin.get("feather_px") or 6.0)
        if abs(sk_exp) > 1e-3:
            calls.append({"name": "ps_select_color_range", "arguments": {"preset": "skinTones", "fuzziness": sk_fuzz}})
            if sk_feather > 0:
                calls.append({"name": "ps_modify_selection", "arguments": {"feather_px": sk_feather, "invert": False}})
            calls.append({"name": "ps_add_exposure_layer", "arguments": {"exposure_delta": sk_exp, "layer_name": f"{layer_prefix}_Skin_Bright"}})
            calls.append({"name": "ps_deselect", "arguments": {}})
        if abs(sk_temp) > 1e-2:
            calls.append({"name": "ps_select_color_range", "arguments": {"preset": "skinTones", "fuzziness": sk_fuzz}})
            if sk_feather > 0:
                calls.append({"name": "ps_modify_selection", "arguments": {"feather_px": sk_feather, "invert": False}})
            calls.append({"name": "ps_add_color_balance_layer", "arguments": {"temp_delta": sk_temp, "tint_delta": 0.0, "layer_name": f"{layer_prefix}_Skin_Warm"}})
            calls.append({"name": "ps_deselect", "arguments": {}})

    # 8. 商业非破坏性收尾（中性灰 / 高低频 / 智能对象高反差保留锐化）
    fin = safe.get("finishing") or {}
    if isinstance(fin, dict):
        if fin.get("frequency_separation"):
            calls.append({
                "name": "ps_create_frequency_separation",
                "arguments": {"gaussian_radius": float(fin.get("freq_sep_radius") or 6.0), "group_name": f"{layer_prefix}_FreqSep"},
            })
        if fin.get("neutral_gray_layer"):
            calls.append({
                "name": "ps_create_neutral_gray_layer",
                "arguments": {"layer_name": f"{layer_prefix}_NeutralGray", "blend_mode": "softlight", "opacity": 100.0},
            })
        if fin.get("high_pass_sharpen"):
            calls.append({
                "name": "ps_apply_high_pass_sharpen",
                "arguments": {
                    "radius": float(fin.get("high_pass_radius") or 1.8),
                    "blend_mode": "overlay",
                    "opacity": float(fin.get("high_pass_opacity") or 45.0),
                    "layer_name": f"{layer_prefix}_HighPass",
                },
            })

    if ps_action and ps_action.get("action_set") and ps_action.get("action_name"):
        calls.append({
            "name": "ps_run_action",
            "arguments": {
                "action_set": str(ps_action["action_set"]),
                "action_name": str(ps_action["action_name"]),
            },
        })

    return calls


class Local8BMCPPlanner:
    """
    本地 8B 模型 MCP 拆解与执行编排器：
    - 优先调用本地 LM Studio / Ollama 8B 模型（如 Qwen3-8B）生成 Photoshop MCP 工具调用；
    - 不可用时无缝回退到 `compile_plan_to_mcp_calls` 确定性编译；
    - 通过 `PSMCPToolkit` 在 Photoshop 内执行并导出 16-bit PSD + 1024px 预览图。
    """

    def __init__(
        self,
        enabled: Optional[bool] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
        toolkit: Optional[PSMCPToolkit] = None,
        adb_client: Optional[AdbMCPPhotoshopClient] = None,
        ps_mcp_backend: Optional[str] = None,
        mock_llm_fn: Optional[Callable[[Dict[str, Any]], List[Dict[str, Any]]]] = None,
    ):
        env_enabled = os.getenv("LOCAL_MCP_PLANNER_ENABLED", "")
        if enabled is not None:
            self.enabled = bool(enabled)
        elif env_enabled.strip():
            self.enabled = env_enabled.strip().lower() not in ("0", "false", "no", "off")
        else:
            self.enabled = bool((os.getenv("LOCAL_MCP_PLANNER_URL") or "").strip())

        self.base_url = (
            base_url
            or os.getenv("LOCAL_MCP_PLANNER_URL")
            or os.getenv("LOCAL_LLM_BASE_URL")
            or "http://127.0.0.1:1234/v1"
        ).strip().rstrip("/")
        self.model = (
            model
            or os.getenv("LOCAL_MCP_PLANNER_MODEL")
            or "auto"
        ).strip()
        self.api_key = (api_key or os.getenv("LOCAL_MCP_PLANNER_API_KEY") or "lm-studio").strip()
        self.timeout = float(timeout)
        self.toolkit = toolkit or PSMCPToolkit()
        self.adb_client = adb_client or AdbMCPPhotoshopClient()
        self.ps_mcp_backend = (
            ps_mcp_backend or os.getenv("PS_MCP_BACKEND") or "auto"
        ).strip().lower()
        self.mock_llm_fn = mock_llm_fn

    def _should_use_adb_mcp(self) -> bool:
        if self.ps_mcp_backend == "adb_mcp":
            return True
        if self.ps_mcp_backend == "com":
            return False
        if getattr(self.toolkit, "dry_run", False):
            return False
        # auto: 若 mikechambers/adb-mcp 代理 (3001) 在线则优先走 adb-mcp，否则回退本地 COM
        return self.adb_client.is_proxy_online()

    def _execute_via_adb_mcp(
        self,
        source_path: Path,
        tool_calls: List[Dict[str, Any]],
        out_psd_path: Path,
        out_preview_jpg: Optional[Path],
        planner_mode: str,
    ) -> Dict[str, Any]:
        """将 8B 拆解出的调色工具调用路由到 mikechambers/adb-mcp (UXP Socket.IO)。"""
        executed: List[Dict[str, Any]] = []
        warnings: List[str] = []

        self.adb_client.open_photoshop_file(str(source_path))
        layers_resp = self.adb_client.get_layers()
        layer_id = 1
        raw_layers = (layers_resp.get("layers") or layers_resp.get("response") or []) if isinstance(layers_resp, dict) else []
        if isinstance(raw_layers, list) and raw_layers and isinstance(raw_layers[0], dict):
            layer_id = int(raw_layers[0].get("id") or 1)

        for step in tool_calls:
            name = step["name"]
            args = step["arguments"]
            try:
                if name == "ps_add_exposure_layer":
                    brightness = int(round(float(args.get("exposure_delta") or 0.0) * 28))
                    self.adb_client.add_brightness_contrast(layer_id=layer_id, brightness=brightness, contrast=0)
                    executed.append({"tool": "adb_mcp:addBrightnessContrast", "args": {"brightness": brightness}, "status": "ok"})
                elif name == "ps_add_color_balance_layer":
                    td = float(args.get("temp_delta") or 0.0)
                    ti = float(args.get("tint_delta") or 0.0)
                    mid = [int(round(td * 0.6)), int(round(-ti * 0.6)), int(round(-td * 0.9))]
                    self.adb_client.add_color_balance(layer_id=layer_id, midtones=mid)
                    executed.append({"tool": "adb_mcp:addColorBalance", "args": {"midtones": mid}, "status": "ok"})
                elif name == "ps_add_hsl_layer":
                    hsl = args.get("hsl_adjustments") or {}
                    sats = [float(v.get("saturation") or 0.0) for v in hsl.values() if isinstance(v, dict)]
                    avg_sat = int(round(sum(sats) / max(len(sats), 1))) if sats else 0
                    self.adb_client.add_vibrance(layer_id=layer_id, vibrance=avg_sat, saturation=int(round(avg_sat * 0.5)))
                    executed.append({"tool": "adb_mcp:addVibrance", "args": {"vibrance": avg_sat}, "status": "ok"})
                else:
                    warnings.append(f"adb-mcp 暂未映射工具 {name}，已安全跳过")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"adb-mcp {name} skipped: {exc}")
                executed.append({"tool": name, "args": args, "status": "error", "error": str(exc)})

        self.adb_client.save_document_as(str(out_psd_path), file_type="PSD")
        if out_preview_jpg is not None:
            tmp_png = out_preview_jpg.with_suffix(".adb_tmp.png")
            self.adb_client.save_preview_png(str(tmp_png))
            if tmp_png.exists():
                from PIL import Image
                with Image.open(tmp_png) as im:
                    im.convert("RGB").save(out_preview_jpg, format="JPEG", quality=92)
                try:
                    tmp_png.unlink()
                except OSError:
                    pass

        return {
            "status": "ok",
            "backend": "adb_mcp",
            "planner_mode": planner_mode,
            "steps_count": len(executed),
            "executed_steps": executed,
            "output_psd_path": str(out_psd_path),
            "output_preview_path": str(out_preview_jpg) if out_preview_jpg else "",
            "warnings": warnings,
        }

    def _resolve_model_name(self) -> str:
        if self.model and self.model.lower() != "auto":
            return self.model
        try:
            with httpx.Client(timeout=5.0, trust_env=False) as client:
                resp = client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                resp.raise_for_status()
                models = resp.json().get("data") or []
                if models:
                    return str(models[0].get("id") or "local-8b")
        except Exception as exc:  # noqa: BLE001
            logger.debug("探测本地 8B 模型列表失败: %s", exc)
        return "local-8b"

    def decompose_to_tool_calls(
        self,
        high_level_plan: Dict[str, Any],
        brief_text: str = "",
        ps_action: Optional[Dict[str, str]] = None,
        layer_prefix: str = "ZS",
    ) -> Tuple[List[Dict[str, Any]], str]:
        """
        将云端视觉模型的高层方案拆解为 Photoshop MCP 工具调用列表。
        返回 `(tool_calls, planner_mode)`，其中 `planner_mode` 为 `local_8b` / `mock_8b` / `deterministic_fallback`。
        """
        if self.mock_llm_fn is not None:
            raw_calls = self.mock_llm_fn({
                "plan": high_level_plan,
                "brief_text": brief_text,
                "ps_action": ps_action,
                "tools": PSMCPToolkit.get_openai_tool_schemas(),
            })
            valid = self._filter_tool_calls(raw_calls)
            if valid:
                return valid, "mock_8b"
            return compile_plan_to_mcp_calls(high_level_plan, ps_action=ps_action, layer_prefix=layer_prefix), "deterministic_fallback"

        if not self.enabled:
            return compile_plan_to_mcp_calls(high_level_plan, ps_action=ps_action, layer_prefix=layer_prefix), "deterministic_fallback"

        try:
            model_id = self._resolve_model_name()
            user_msg = (
                f"以下是云端视觉模型给出的高层修图方案：\n"
                f"{json.dumps(high_level_plan, ensure_ascii=False, indent=2)}\n"
            )
            if brief_text.strip():
                user_msg += f"\n补充自然语言修图要求：{brief_text.strip()}\n"
            if ps_action and ps_action.get("action_set") and ps_action.get("action_name"):
                user_msg += f"\n可用 Photoshop 动作组：{json.dumps(ps_action, ensure_ascii=False)}\n"
            user_msg += "\n请调用对应的 Photoshop MCP 工具完成非破坏性分区蒙版调色与精修。"

            body = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": LOCAL_8B_PLANNER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "tools": PSMCPToolkit.get_openai_tool_schemas(),
                "tool_choice": "auto",
                "temperature": 0.1,
                "max_tokens": 1500,
            }
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()

            msg = ((data.get("choices") or [{}])[0]).get("message") or {}
            raw_tcs = msg.get("tool_calls") or []
            parsed: List[Dict[str, Any]] = []
            for tc in raw_tcs:
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "").strip()
                raw_args = fn.get("arguments")
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        continue
                elif isinstance(raw_args, dict):
                    args = raw_args
                else:
                    args = {}
                parsed.append({"name": name, "arguments": args})

            valid = self._filter_tool_calls(parsed)
            if valid:
                logger.info("本地 8B 模型 (%s) 成功拆解出 %d 步 PS MCP 工具调用", model_id, len(valid))
                return valid, f"local_8b:{model_id}"
            logger.warning("本地 8B 模型未返回有效工具调用，自动降级为确定性 MCP 编译")
        except Exception as exc:  # noqa: BLE001
            logger.warning("本地 8B MCP 拆解器不可达 (%s)，自动降级为确定性 MCP 编译", exc)

        return compile_plan_to_mcp_calls(high_level_plan, ps_action=ps_action, layer_prefix=layer_prefix), "deterministic_fallback"

    @staticmethod
    def _filter_tool_calls(calls: Any) -> List[Dict[str, Any]]:
        if not isinstance(calls, list):
            return []
        valid: List[Dict[str, Any]] = []
        for item in calls:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            args = item.get("arguments")
            if name in ALLOWED_PLANNER_TOOLS and isinstance(args, dict):
                valid.append({"name": name, "arguments": args})
        return valid

    def execute_retouch_session(
        self,
        source_path: Path,
        high_level_plan: Dict[str, Any],
        out_psd_path: Path,
        out_preview_jpg: Optional[Path] = None,
        brief_text: str = "",
        ps_action: Optional[Dict[str, str]] = None,
        layer_prefix: str = "ZS",
        close_after: bool = True,
    ) -> Dict[str, Any]:
        """
        完整执行一次「打开原图/PSD -> 8B 拆解并调用 MCP 调整图层 -> 保存 16-bit PSD -> 导出预览 -> 关闭文档」。
        """
        tool_calls, planner_mode = self.decompose_to_tool_calls(
            high_level_plan=high_level_plan,
            brief_text=brief_text,
            ps_action=ps_action,
            layer_prefix=layer_prefix,
        )

        if self._should_use_adb_mcp():
            return self._execute_via_adb_mcp(
                source_path=source_path,
                tool_calls=tool_calls,
                out_psd_path=out_psd_path,
                out_preview_jpg=out_preview_jpg,
                planner_mode=planner_mode,
            )

        executed: List[Dict[str, Any]] = []
        warnings: List[str] = []

        self.toolkit.ps_open_document(str(source_path))
        try:
            for step in tool_calls:
                name = step["name"]
                args = step["arguments"]
                try:
                    res = self.toolkit.dispatch_tool(name, args)
                    executed.append({"tool": name, "args": args, "status": res.get("status", "ok")})
                    for w in (res.get("warnings") or []):
                        warnings.append(str(w))
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"{name} skipped: {exc}")
                    executed.append({"tool": name, "args": args, "status": "error", "error": str(exc)})

            self.toolkit.ps_save_psd(str(out_psd_path))
            if out_preview_jpg is not None:
                self.toolkit.ps_export_preview(str(out_preview_jpg), max_edge=1024, quality=92)
        finally:
            if close_after:
                self.toolkit.ps_close_document(save_changes=False)

        return {
            "status": "ok",
            "planner_mode": planner_mode,
            "steps_count": len(executed),
            "executed_steps": executed,
            "output_psd_path": str(out_psd_path),
            "output_preview_path": str(out_preview_jpg) if out_preview_jpg else "",
            "warnings": warnings,
        }

    # ==================== 检查点保存 + 终盘审查 + AI 逆推回退 ====================

    def execute_with_checkpoints_and_backtrack(
        self,
        source_path: Path,
        high_level_plan: Dict[str, Any],
        out_psd_path: Path,
        out_preview_jpg: Path,
        brief_text: str = "",
        ps_action: Optional[Dict[str, str]] = None,
        layer_prefix: str = "ZS",
        checkpoint_every: int = 3,
        max_backtrack_rounds: int = 2,
        review_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        生产级「检查点存档 + 终盘审查 + AI 逆推回退修改」工作流：
        1. 连续执行 MCP 工具调用，**不在中途频繁调视觉模型自检**；
        2. 每执行 `checkpoint_every` 步静默保存一次检查点 `(ckpt_XX.psd, ckpt_XX.jpg)` 与步骤映射；
        3. 全部执行并保存最终成片后，再调用 `review_fn` 对保存好的图片做终盘审查；
        4. 若终盘审查判定有问题（`passed=False`）：
           - AI 根据各检查点步骤清单与预览图**逆推**定位出问题前的最后一个干净检查点 (`rollback_to_checkpoint`)；
           - 自动回退打开该检查点 PSD，执行定向修正工具调用 (`fix_calls`)；
           - 重新保存成片并再次审查，直至通过或达到 `max_backtrack_rounds` 上限。
        """
        tool_calls, planner_mode = self.decompose_to_tool_calls(
            high_level_plan=high_level_plan,
            brief_text=brief_text,
            ps_action=ps_action,
            layer_prefix=layer_prefix,
        )

        ckpt_every = max(1, int(checkpoint_every))
        ckpt_dir = Path(out_psd_path).parent / ".checkpoints" / Path(out_psd_path).stem
        if not getattr(self.toolkit, "dry_run", False):
            ckpt_dir.mkdir(parents=True, exist_ok=True)

        checkpoints: List[Dict[str, Any]] = []
        executed: List[Dict[str, Any]] = []
        warnings: List[str] = []

        # ---- 阶段 1：连续执行 + 每 N 步静默保存检查点（不做中途 AI 自检） ----
        self.toolkit.ps_open_document(str(source_path))
        try:
            for idx, step in enumerate(tool_calls, start=1):
                name = step["name"]
                args = step["arguments"]
                try:
                    res = self.toolkit.dispatch_tool(name, args)
                    executed.append({"step": idx, "tool": name, "args": args, "status": res.get("status", "ok")})
                    for w in (res.get("warnings") or []):
                        warnings.append(str(w))
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"step {idx} {name} skipped: {exc}")
                    executed.append({"step": idx, "tool": name, "args": args, "status": "error", "error": str(exc)})

                is_ckpt_step = (idx % ckpt_every == 0) or (idx == len(tool_calls))
                if is_ckpt_step:
                    ckpt_id = f"ckpt_{len(checkpoints) + 1:02d}"
                    ckpt_psd = ckpt_dir / f"{ckpt_id}_step{idx}.psd"
                    ckpt_jpg = ckpt_dir / f"{ckpt_id}_step{idx}.jpg"
                    self.toolkit.ps_save_psd(str(ckpt_psd))
                    self.toolkit.ps_export_preview(str(ckpt_jpg), max_edge=1024, quality=88)
                    checkpoints.append({
                        "id": ckpt_id,
                        "step_end": idx,
                        "psd_path": str(ckpt_psd),
                        "preview_path": str(ckpt_jpg),
                        "steps": executed[max(0, idx - ckpt_every):idx],
                    })

            self.toolkit.ps_save_psd(str(out_psd_path))
            self.toolkit.ps_export_preview(str(out_preview_jpg), max_edge=1024, quality=92)
        finally:
            self.toolkit.ps_close_document(save_changes=False)

        # ---- 阶段 2 & 3：保存完后审查成品 -> 有问题则 AI 逆推回退检查点并定向修改 ----
        backtrack_history: List[Dict[str, Any]] = []
        final_review: Dict[str, Any] = {"passed": True, "reason": "未配置终盘审查器，直接交付人工终审"}

        if review_fn is not None:
            ckpt_by_id = {c["id"]: c for c in checkpoints}
            for round_idx in range(1, max(1, int(max_backtrack_rounds)) + 1):
                review = review_fn({
                    "round": round_idx,
                    "final_preview_path": str(out_preview_jpg),
                    "final_psd_path": str(out_psd_path),
                    "checkpoints": checkpoints,
                    "executed_steps": executed,
                    "plan": high_level_plan,
                })
                final_review = review if isinstance(review, dict) else {"passed": True}
                if final_review.get("passed", True):
                    break

                # AI 逆推定位回退检查点
                target_ckpt_id = str(final_review.get("rollback_to_checkpoint") or "").strip()
                root_cause = str(final_review.get("root_cause") or "终盘审查未通过").strip()
                fix_calls = self._filter_tool_calls(final_review.get("fix_calls") or [])

                if target_ckpt_id in ckpt_by_id:
                    rollback_psd = Path(ckpt_by_id[target_ckpt_id]["psd_path"])
                else:
                    target_ckpt_id = "source"
                    rollback_psd = Path(source_path)

                fix_executed: List[Dict[str, Any]] = []
                self.toolkit.ps_open_document(str(rollback_psd))
                try:
                    for fstep in fix_calls:
                        res = self.toolkit.dispatch_tool(fstep["name"], fstep["arguments"])
                        fix_executed.append({"tool": fstep["name"], "args": fstep["arguments"], "status": res.get("status", "ok")})
                    self.toolkit.ps_save_psd(str(out_psd_path))
                    self.toolkit.ps_export_preview(str(out_preview_jpg), max_edge=1024, quality=92)
                finally:
                    self.toolkit.ps_close_document(save_changes=False)

                backtrack_history.append({
                    "round": round_idx,
                    "root_cause": root_cause,
                    "rollback_to_checkpoint": target_ckpt_id,
                    "rollback_psd": str(rollback_psd),
                    "fix_steps": fix_executed,
                })

            # 若最后一次回退修复后还没做最终复审，再补一次终审确认
            if backtrack_history and not final_review.get("passed", True):
                final_review = review_fn({
                    "round": len(backtrack_history) + 1,
                    "final_preview_path": str(out_preview_jpg),
                    "final_psd_path": str(out_psd_path),
                    "checkpoints": checkpoints,
                    "executed_steps": executed,
                    "backtrack_history": backtrack_history,
                    "plan": high_level_plan,
                })

        return {
            "status": "ok",
            "planner_mode": planner_mode,
            "steps_count": len(executed),
            "executed_steps": executed,
            "checkpoints": checkpoints,
            "backtrack_history": backtrack_history,
            "final_review": final_review,
            "output_psd_path": str(out_psd_path),
            "output_preview_path": str(out_preview_jpg),
            "warnings": warnings,
        }
