#!/usr/bin/env python3
"""
ZERO STUDIO · Photoshop 标准 MCP Server (ps_mcp_server.py)
===========================================================
基于官方 `mcp.server.fastmcp.FastMCP` + 本机 `PSCOMBridge`（Windows COM 零干预驱动），
向任意 MCP 客户端（Claude Desktop / Cursor / LM Studio / 本地 8B 拆解器）暴露原子化、
非破坏性的 Photoshop 精修工具集。

核心原则：
1. 绝不整图 AI 重绘，全程通过 Photoshop 原生调整图层与动作组操作；
2. 保留 16-bit 可编辑 PSD 图层结构，客户随时可查验 PSD；
3. 所有调色参数均在工具入口执行代码层安全边界钳位（防止 8B 小模型或大模型参数越界）；
4. 同时支持：
   - 标准 MCP Server 运行：`python ps_mcp_server.py` (stdio) 或 `--transport sse`
   - 进程内直接调用：`PSMCPToolkit`（供 `mcp_planner.py` 本地 8B Tool Calling 直接调度，支持 dry_run 离线测试）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

ENGINE_DIR = Path(__file__).resolve().parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from gemini_colorist import (  # noqa: E402
    HSL_CHANNELS,
    HSL_DEFAULT_BOUNDS,
    HSL_SKIN_ORANGE_BOUNDS,
    MAX_CURVE_DEVIATION,
    SAFETY_BOUNDS,
    clamp_retouch_plan,
)
from ps_com_bridge import (  # noqa: E402
    PSCOMBridge,
    _build_adjustment_layers_jsx,
    _js_str,
    _ps_path,
)

logger = logging.getLogger("retouch_engine.ps_mcp")

SELECTIVE_COLOR_RANGES = {
    "reds": "Rds ",
    "yellows": "Ylws",
    "greens": "Grns",
    "cyans": "Cyns",
    "blues": "Bls ",
    "magentas": "Mgnt",
    "whites": "Whts",
    "neutrals": "Ntrl",
    "blacks": "Blks",
}


def _clamp(val: Any, lo: float, hi: float) -> float:
    try:
        num = float(val)
    except (TypeError, ValueError):
        num = 0.0
    return round(max(lo, min(hi, num)), 3)


def _build_selective_color_jsx(
    color_range: str,
    cyan: float = 0.0,
    magenta: float = 0.0,
    yellow: float = 0.0,
    black: float = 0.0,
    relative: bool = True,
    layer_name: str = "ZS_SelectiveColor",
) -> str:
    """生成 Photoshop 原生「可选颜色 (Selective Color)」调整图层 ExtendScript。"""
    key = str(color_range or "neutrals").strip().lower()
    char_id = SELECTIVE_COLOR_RANGES.get(key)
    if not char_id:
        raise ValueError(f"不支持的可选颜色通道: {color_range}，允许值: {sorted(SELECTIVE_COLOR_RANGES)}")

    c = _clamp(cyan, -30.0, 30.0)
    m = _clamp(magenta, -30.0, 30.0)
    y = _clamp(yellow, -30.0, 30.0)
    k = _clamp(black, -30.0, 30.0)
    method = "Rltv" if relative else "Absl"

    return f"""
    var d1 = new ActionDescriptor();
    var r1 = new ActionReference();
    r1.putClass(charIDToTypeID("AdjL"));
    d1.putReference(charIDToTypeID("null"), r1);
    var d2 = new ActionDescriptor();
    var d3 = new ActionDescriptor();
    d3.putEnumerated(stringIDToTypeID("presetKind"), stringIDToTypeID("presetKindType"), stringIDToTypeID("presetKindCustom"));
    d3.putEnumerated(charIDToTypeID("Mthd"), charIDToTypeID("CrcM"), charIDToTypeID("{method}"));
    var cList = new ActionList();
    var cDesc = new ActionDescriptor();
    cDesc.putEnumerated(charIDToTypeID("Clrs"), charIDToTypeID("Clrs"), charIDToTypeID("{char_id}"));
    cDesc.putUnitDouble(charIDToTypeID("Cyn "), charIDToTypeID("#Prc"), {c});
    cDesc.putUnitDouble(charIDToTypeID("Mgnt"), charIDToTypeID("#Prc"), {m});
    cDesc.putUnitDouble(charIDToTypeID("Ylw "), charIDToTypeID("#Prc"), {y});
    cDesc.putUnitDouble(charIDToTypeID("Blck"), charIDToTypeID("#Prc"), {k});
    cList.putObject(charIDToTypeID("ClrC"), cDesc);
    d3.putList(charIDToTypeID("ClrC"), cList);
    d2.putObject(charIDToTypeID("Type"), charIDToTypeID("SlcC"), d3);
    d1.putObject(charIDToTypeID("Usng"), charIDToTypeID("AdjL"), d2);
    executeAction(charIDToTypeID("Mk  "), d1, DialogModes.NO);
    try {{ app.activeDocument.activeLayer.name = {_js_str(layer_name)}; }} catch (e) {{}}
    return "selective_color_added:{key}";
    """


class PSMCPToolkit:
    """
    Photoshop MCP 原子工具执行器：
    - 真实模式：复用 `PSCOMBridge` 通过 Windows COM 静默驱动本机 Photoshop；
    - 演练模式 (`dry_run=True`)：记录每一步安全钳位后的工具调用，供单元测试与离线验证使用。
    """

    def __init__(
        self,
        com_bridge: Optional[PSCOMBridge] = None,
        dry_run: bool = False,
    ):
        self.dry_run = bool(dry_run)
        self.com_bridge = com_bridge if (com_bridge is not None or dry_run) else PSCOMBridge()
        self.call_history: List[Dict[str, Any]] = []

    def _record(self, tool: str, args: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        entry = {"tool": tool, "args": args, "result": result}
        self.call_history.append(entry)
        return result

    def _eval(self, jsx: str) -> Dict[str, Any]:
        if self.dry_run:
            return {"status": "ok", "backend": "dry_run", "result": "simulated", "warnings": []}
        assert self.com_bridge is not None
        return self.com_bridge._eval_jsx(jsx)

    # ==================== 原子工具实现 ====================

    def ps_status(self) -> Dict[str, Any]:
        if self.dry_run:
            res = {"status": "ok", "backend": "dry_run", "plugin": {"online": True, "mode": "dry_run"}}
            return self._record("ps_status", {}, res)
        assert self.com_bridge is not None
        return self._record("ps_status", {}, self.com_bridge.plugin_status())

    def ps_open_document(self, file_path: str) -> Dict[str, Any]:
        p = Path(file_path)
        if not self.dry_run and not p.exists():
            raise FileNotFoundError(f"待打开文件不存在: {p}")
        jsx = f"""
        var f = new File({_js_str(_ps_path(p))});
        if (!f.exists) {{ throw new Error("File not found: " + f.fsName); }}
        var doc = app.open(f);
        return doc.name + "|" + doc.width.as("px") + "x" + doc.height.as("px");
        """
        res = self._eval(jsx)
        return self._record("ps_open_document", {"file_path": str(p)}, res)

    def ps_get_document_info(self) -> Dict[str, Any]:
        jsx = """
        if (app.documents.length === 0) { return "NO_ACTIVE_DOCUMENT"; }
        var doc = app.activeDocument;
        var names = [];
        for (var i = 0; i < doc.layers.length && i < 30; i++) {
            names.push(doc.layers[i].name);
        }
        return doc.name + "|" + Math.round(doc.width.as("px")) + "x" + Math.round(doc.height.as("px"))
            + "|bits=" + String(doc.bitsPerChannel) + "|layers=" + names.join(",");
        """
        res = self._eval(jsx)
        return self._record("ps_get_document_info", {}, res)

    def ps_add_exposure_layer(
        self,
        exposure_delta: float,
        layer_name: str = "ZS_Exposure",
    ) -> Dict[str, Any]:
        lo, hi = SAFETY_BOUNDS["exposure_delta"]
        safe_exp = _clamp(exposure_delta, lo, hi)
        if abs(safe_exp) <= 1e-3:
            res = {"status": "skipped", "reason": "exposure_delta is ~0"}
            return self._record("ps_add_exposure_layer", {"exposure_delta": safe_exp}, res)
        jsx = _build_adjustment_layers_jsx({"exposure_delta": safe_exp}, layer_prefix=layer_name.replace("_Exposure", ""))
        res = self._eval(jsx + '\nreturn "exposure_added";')
        res["applied_exposure_delta"] = safe_exp
        return self._record("ps_add_exposure_layer", {"exposure_delta": safe_exp, "layer_name": layer_name}, res)

    def ps_add_color_balance_layer(
        self,
        temp_delta: float = 0.0,
        tint_delta: float = 0.0,
        layer_name: str = "ZS_ColorBalance",
    ) -> Dict[str, Any]:
        safe_temp = _clamp(temp_delta, *SAFETY_BOUNDS["temp_delta"])
        safe_tint = _clamp(tint_delta, *SAFETY_BOUNDS["tint_delta"])
        if abs(safe_temp) <= 1e-2 and abs(safe_tint) <= 1e-2:
            res = {"status": "skipped", "reason": "temp/tint delta is ~0"}
            return self._record("ps_add_color_balance_layer", {"temp_delta": safe_temp, "tint_delta": safe_tint}, res)
        jsx = _build_adjustment_layers_jsx(
            {"temp_delta": safe_temp, "tint_delta": safe_tint},
            layer_prefix=layer_name.replace("_ColorBalance", ""),
        )
        res = self._eval(jsx + '\nreturn "color_balance_added";')
        res["applied_temp_delta"] = safe_temp
        res["applied_tint_delta"] = safe_tint
        return self._record(
            "ps_add_color_balance_layer",
            {"temp_delta": safe_temp, "tint_delta": safe_tint, "layer_name": layer_name},
            res,
        )

    def ps_add_curves_layer(
        self,
        curves_points: Sequence[Sequence[int]],
        layer_name: str = "ZS_Curves",
    ) -> Dict[str, Any]:
        safe = clamp_retouch_plan({"curves_points": list(curves_points)})
        pts = safe["curves_points"]
        jsx = _build_adjustment_layers_jsx({"curves_points": pts}, layer_prefix=layer_name.replace("_Curves", ""))
        if not jsx.strip():
            res = {"status": "skipped", "reason": "identity curve"}
            return self._record("ps_add_curves_layer", {"curves_points": pts}, res)
        res = self._eval(jsx + '\nreturn "curves_added";')
        res["applied_curves_points"] = pts
        return self._record("ps_add_curves_layer", {"curves_points": pts, "layer_name": layer_name}, res)

    def ps_add_hsl_layer(
        self,
        hsl_adjustments: Dict[str, Dict[str, float]],
        skin_luminance: float = 0.0,
        layer_name: str = "ZS_HSL",
    ) -> Dict[str, Any]:
        safe = clamp_retouch_plan({"hsl_adjustments": hsl_adjustments, "skin_luminance": skin_luminance})
        jsx = _build_adjustment_layers_jsx(
            {"hsl_adjustments": safe["hsl_adjustments"], "skin_luminance": safe["skin_luminance"]},
            layer_prefix=layer_name.replace("_HSL", ""),
        )
        if not jsx.strip():
            res = {"status": "skipped", "reason": "no non-zero hsl channels"}
            return self._record("ps_add_hsl_layer", {"skin_luminance": safe["skin_luminance"]}, res)
        res = self._eval(jsx + '\nreturn "hsl_added";')
        res["clamped_fields"] = safe["clamped_fields"]
        return self._record(
            "ps_add_hsl_layer",
            {"hsl_adjustments": safe["hsl_adjustments"], "skin_luminance": safe["skin_luminance"], "layer_name": layer_name},
            res,
        )

    def ps_add_selective_color_layer(
        self,
        color_range: str = "neutrals",
        cyan: float = 0.0,
        magenta: float = 0.0,
        yellow: float = 0.0,
        black: float = 0.0,
        relative: bool = True,
        layer_name: str = "ZS_SelectiveColor",
    ) -> Dict[str, Any]:
        jsx = _build_selective_color_jsx(
            color_range=color_range,
            cyan=cyan,
            magenta=magenta,
            yellow=yellow,
            black=black,
            relative=relative,
            layer_name=layer_name,
        )
        res = self._eval(jsx)
        return self._record(
            "ps_add_selective_color_layer",
            {
                "color_range": color_range,
                "cyan": _clamp(cyan, -30.0, 30.0),
                "magenta": _clamp(magenta, -30.0, 30.0),
                "yellow": _clamp(yellow, -30.0, 30.0),
                "black": _clamp(black, -30.0, 30.0),
                "relative": bool(relative),
                "layer_name": layer_name,
            },
            res,
        )

    def ps_run_action(self, action_set: str, action_name: str) -> Dict[str, Any]:
        if not str(action_set).strip() or not str(action_name).strip():
            raise ValueError("action_set 和 action_name 均不能为空")
        jsx = _build_adjustment_layers_jsx({}, ps_action={"action_set": action_set, "action_name": action_name})
        res = self._eval(jsx + '\nreturn "action_played";')
        return self._record("ps_run_action", {"action_set": action_set, "action_name": action_name}, res)

    def ps_save_psd(self, output_psd_path: str) -> Dict[str, Any]:
        out = Path(output_psd_path)
        if not self.dry_run:
            out.parent.mkdir(parents=True, exist_ok=True)
        jsx = f"""
        var doc = app.activeDocument;
        var psdFile = new File({_js_str(_ps_path(out))});
        var psdOpts = new PhotoshopSaveOptions();
        psdOpts.alphaChannels = true;
        psdOpts.annotations = true;
        psdOpts.embedColorProfile = true;
        psdOpts.layers = true;
        psdOpts.maximizeCompatibility = true;
        doc.saveAs(psdFile, psdOpts, true, Extension.LOWERCASE);
        return psdFile.fsName;
        """
        res = self._eval(jsx)
        return self._record("ps_save_psd", {"output_psd_path": str(out)}, res)

    def ps_export_preview(self, output_jpg_path: str, max_edge: int = 1024, quality: int = 92) -> Dict[str, Any]:
        out = Path(output_jpg_path)
        if not self.dry_run:
            out.parent.mkdir(parents=True, exist_ok=True)
        q_ps = max(1, min(12, int(round(int(quality) / 100.0 * 12))))
        edge = max(256, min(4096, int(max_edge)))
        jsx = f"""
        var origDoc = app.activeDocument;
        var dup = origDoc.duplicate("zs_preview_tmp", true);
        try {{
            dup.convertProfile("sRGB IEC61966-2.1", Intent.PERCEPTUAL, true, false);
            if (dup.bitsPerChannel !== BitsPerChannelType.EIGHT) {{
                dup.bitsPerChannel = BitsPerChannelType.EIGHT;
            }}
            var w = dup.width.as("px");
            var h = dup.height.as("px");
            if (Math.max(w, h) > {edge}) {{
                if (w >= h) {{
                    dup.resizeImage(UnitValue({edge}, "px"), null, null, ResampleMethod.BICUBICSHARPER);
                }} else {{
                    dup.resizeImage(null, UnitValue({edge}, "px"), null, ResampleMethod.BICUBICSHARPER);
                }}
            }}
            var jpgFile = new File({_js_str(_ps_path(out))});
            var jpgOpts = new JPEGSaveOptions();
            jpgOpts.quality = {q_ps};
            jpgOpts.embedColorProfile = true;
            dup.saveAs(jpgFile, jpgOpts, true, Extension.LOWERCASE);
            return jpgFile.fsName;
        }} finally {{
            dup.close(SaveOptions.DONOTSAVECHANGES);
            app.activeDocument = origDoc;
        }}
        """
        res = self._eval(jsx)
        return self._record("ps_export_preview", {"output_jpg_path": str(out), "max_edge": edge, "quality": quality}, res)

    def ps_close_document(self, save_changes: bool = False) -> Dict[str, Any]:
        opt = "SaveOptions.SAVECHANGES" if save_changes else "SaveOptions.DONOTSAVECHANGES"
        jsx = f"""
        if (app.documents.length > 0) {{
            app.activeDocument.close({opt});
            return "closed";
        }}
        return "no_document";
        """
        res = self._eval(jsx)
        return self._record("ps_close_document", {"save_changes": bool(save_changes)}, res)

    # ==================== OpenAI Tool Calling Schema & Dispatch ====================

    def dispatch_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """供本地 8B 模型 Tool Calling 或编排器统一分发执行。"""
        args = arguments if isinstance(arguments, dict) else {}
        handlers: Dict[str, Callable[..., Dict[str, Any]]] = {
            "ps_status": self.ps_status,
            "ps_open_document": self.ps_open_document,
            "ps_get_document_info": self.ps_get_document_info,
            "ps_add_exposure_layer": self.ps_add_exposure_layer,
            "ps_add_color_balance_layer": self.ps_add_color_balance_layer,
            "ps_add_curves_layer": self.ps_add_curves_layer,
            "ps_add_hsl_layer": self.ps_add_hsl_layer,
            "ps_add_selective_color_layer": self.ps_add_selective_color_layer,
            "ps_run_action": self.ps_run_action,
            "ps_save_psd": self.ps_save_psd,
            "ps_export_preview": self.ps_export_preview,
            "ps_close_document": self.ps_close_document,
        }
        fn = handlers.get(tool_name)
        if fn is None:
            raise ValueError(f"未知 Photoshop MCP 工具: {tool_name}")
        return fn(**args)

    @staticmethod
    def get_openai_tool_schemas() -> List[Dict[str, Any]]:
        """返回可供本地 8B 模型（LM Studio / Ollama OpenAI 兼容接口）直接使用的 tools 定义。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "ps_add_exposure_layer",
                    "description": "在当前 PSD 中新建非破坏性「曝光度」调整图层（安全范围 -1.5 到 +1.5）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "exposure_delta": {"type": "number", "description": "曝光增量，范围 [-1.5, 1.5]"},
                            "layer_name": {"type": "string", "description": "图层名称，默认 ZS_Exposure"},
                        },
                        "required": ["exposure_delta"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_add_color_balance_layer",
                    "description": "新建非破坏性「色彩平衡」中间调调整图层，用于微调色温与色调（范围 -15 到 +15）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "temp_delta": {"type": "number", "description": "冷暖偏移，负偏冷青蓝，正偏暖黄"},
                            "tint_delta": {"type": "number", "description": "绿/洋红偏移，范围 [-15, 15]"},
                            "layer_name": {"type": "string"},
                        },
                        "required": ["temp_delta", "tint_delta"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_add_curves_layer",
                    "description": "新建非破坏性「曲线」调整图层，控制点单调递增且单点偏移不超过 35。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "curves_points": {
                                "type": "array",
                                "items": {"type": "array", "items": {"type": "integer"}},
                                "description": "例如 [[0,8],[64,60],[128,132],[192,196],[255,250]]",
                            },
                            "layer_name": {"type": "string"},
                        },
                        "required": ["curves_points"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_add_hsl_layer",
                    "description": "新建非破坏性「色相/饱和度 (HSL)」分通道调整图层，含亚洲人肤色 oranges 特严保护。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "hsl_adjustments": {
                                "type": "object",
                                "description": "键为 reds/oranges/yellows/greens/aquas/blues/purples/magentas，值为 {hue, saturation, luminance}",
                            },
                            "skin_luminance": {"type": "number", "description": "肤色明度微调 [-10, 15]"},
                            "layer_name": {"type": "string"},
                        },
                        "required": ["hsl_adjustments"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_add_selective_color_layer",
                    "description": "新建非破坏性「可选颜色」调整图层，精细控制某色彩范围的青/洋红/黄/黑比例（[-30, +30]）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "color_range": {
                                "type": "string",
                                "enum": sorted(SELECTIVE_COLOR_RANGES.keys()),
                            },
                            "cyan": {"type": "number"},
                            "magenta": {"type": "number"},
                            "yellow": {"type": "number"},
                            "black": {"type": "number"},
                            "relative": {"type": "boolean"},
                            "layer_name": {"type": "string"},
                        },
                        "required": ["color_range"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_run_action",
                    "description": "播放本机 Photoshop 动作组中的预设 Action（例如中性灰磨皮/锐化）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action_set": {"type": "string"},
                            "action_name": {"type": "string"},
                        },
                        "required": ["action_set", "action_name"],
                    },
                },
            },
        ]


def create_fastmcp_server(dry_run: bool = False):
    """构建标准 FastMCP Server 实例（可被 Claude Desktop / Cursor / LM Studio 等 MCP 宿主直接拉起）。"""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("zero-studio-photoshop-mcp")
    kit = PSMCPToolkit(dry_run=dry_run)

    @mcp.tool()
    def ps_status() -> str:
        """检查本机 Photoshop COM 驱动与运行状态。"""
        return json.dumps(kit.ps_status(), ensure_ascii=False)

    @mcp.tool()
    def ps_open_document(file_path: str) -> str:
        """在 Photoshop 中静默打开 16-bit TIFF 或 PSD 文件。"""
        return json.dumps(kit.ps_open_document(file_path), ensure_ascii=False)

    @mcp.tool()
    def ps_get_document_info() -> str:
        """读取当前激活 PSD 文档的尺寸、位深与图层列表。"""
        return json.dumps(kit.ps_get_document_info(), ensure_ascii=False)

    @mcp.tool()
    def ps_add_exposure_layer(exposure_delta: float, layer_name: str = "ZS_Exposure") -> str:
        """新建非破坏性曝光度调整图层（代码层钳位 [-1.5, 1.5]）。"""
        return json.dumps(kit.ps_add_exposure_layer(exposure_delta, layer_name=layer_name), ensure_ascii=False)

    @mcp.tool()
    def ps_add_color_balance_layer(
        temp_delta: float = 0.0,
        tint_delta: float = 0.0,
        layer_name: str = "ZS_ColorBalance",
    ) -> str:
        """新建非破坏性色彩平衡调整图层（色温/色调钳位 [-15, 15]）。"""
        return json.dumps(
            kit.ps_add_color_balance_layer(temp_delta=temp_delta, tint_delta=tint_delta, layer_name=layer_name),
            ensure_ascii=False,
        )

    @mcp.tool()
    def ps_add_curves_layer(curves_points: List[List[int]], layer_name: str = "ZS_Curves") -> str:
        """新建非破坏性曲线调整图层（强制单调递增，单点偏移 <= 35）。"""
        return json.dumps(kit.ps_add_curves_layer(curves_points, layer_name=layer_name), ensure_ascii=False)

    @mcp.tool()
    def ps_add_hsl_layer(
        hsl_adjustments: Dict[str, Dict[str, float]],
        skin_luminance: float = 0.0,
        layer_name: str = "ZS_HSL",
    ) -> str:
        """新建非破坏性分通道 HSL 调整图层（oranges 肤色通道特严保护）。"""
        return json.dumps(
            kit.ps_add_hsl_layer(hsl_adjustments, skin_luminance=skin_luminance, layer_name=layer_name),
            ensure_ascii=False,
        )

    @mcp.tool()
    def ps_add_selective_color_layer(
        color_range: str = "neutrals",
        cyan: float = 0.0,
        magenta: float = 0.0,
        yellow: float = 0.0,
        black: float = 0.0,
        relative: bool = True,
        layer_name: str = "ZS_SelectiveColor",
    ) -> str:
        """新建非破坏性可选颜色调整图层（各分量钳位 [-30, 30]）。"""
        return json.dumps(
            kit.ps_add_selective_color_layer(
                color_range=color_range,
                cyan=cyan,
                magenta=magenta,
                yellow=yellow,
                black=black,
                relative=relative,
                layer_name=layer_name,
            ),
            ensure_ascii=False,
        )

    @mcp.tool()
    def ps_run_action(action_set: str, action_name: str) -> str:
        """运行 Photoshop 本地预设动作（如中性灰磨皮/锐化）。"""
        return json.dumps(kit.ps_run_action(action_set, action_name), ensure_ascii=False)

    @mcp.tool()
    def ps_save_psd(output_psd_path: str) -> str:
        """保存完整 16-bit 可编辑 PSD 文件（绝不合并压平图层）。"""
        return json.dumps(kit.ps_save_psd(output_psd_path), ensure_ascii=False)

    @mcp.tool()
    def ps_export_preview(output_jpg_path: str, max_edge: int = 1024, quality: int = 92) -> str:
        """无损复制临时副本并导出 8-bit sRGB JPEG 预览图（不改动原 PSD 图层）。"""
        return json.dumps(
            kit.ps_export_preview(output_jpg_path, max_edge=max_edge, quality=quality),
            ensure_ascii=False,
        )

    @mcp.tool()
    def ps_close_document(save_changes: bool = False) -> str:
        """关闭当前 Photoshop 文档。"""
        return json.dumps(kit.ps_close_document(save_changes=save_changes), ensure_ascii=False)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="ZERO STUDIO Photoshop MCP Server")
    parser.add_argument("--transport", choices=("stdio", "sse", "streamable-http"), default="stdio")
    parser.add_argument("--dry-run", action="store_true", help="演练模式（不连接本机 Photoshop）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [ps_mcp] %(message)s")
    server = create_fastmcp_server(dry_run=args.dry_run)
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
