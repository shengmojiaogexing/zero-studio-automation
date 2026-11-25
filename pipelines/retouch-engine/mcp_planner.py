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

from gemini_colorist import clamp_retouch_plan  # noqa: E402
from ps_mcp_server import PSMCPToolkit  # noqa: E402

logger = logging.getLogger("retouch_engine.mcp_planner")

ALLOWED_PLANNER_TOOLS = {
    "ps_add_exposure_layer",
    "ps_add_color_balance_layer",
    "ps_add_curves_layer",
    "ps_add_hsl_layer",
    "ps_add_selective_color_layer",
    "ps_run_action",
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
        self.mock_llm_fn = mock_llm_fn

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
            user_msg += "\n请调用对应的 Photoshop MCP 工具完成非破坏性调色。"

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
