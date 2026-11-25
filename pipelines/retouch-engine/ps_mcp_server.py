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
import base64
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

ENGINE_DIR = Path(__file__).resolve().parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from adb_mcp_client import AdbMCPPhotoshopClient  # noqa: E402
from gemini_colorist import (  # noqa: E402
    HSL_CHANNELS,
    HSL_DEFAULT_BOUNDS,
    HSL_SKIN_ORANGE_BOUNDS,
    MAX_CURVE_DEVIATION,
    SAFETY_BOUNDS,
    _sanitize_revision_jsx,
    clamp_retouch_plan,
)
from ps_com_bridge import (  # noqa: E402
    PSCOMBridge,
    _build_adjustment_layers_jsx,
    _js_str,
    _ps_path,
)
from ps_mcp_advanced import (  # noqa: E402
    BLEND_MODES_MAP,
    build_add_layer_mask_jsx,
    build_convert_smart_object_jsx,
    build_frequency_separation_jsx,
    build_high_pass_sharpen_jsx,
    build_levels_layer_jsx,
    build_liquify_jsx,
    build_modify_selection_jsx,
    build_neutral_gray_jsx,
    build_select_color_range_jsx,
    build_select_subject_jsx,
    build_set_layer_blend_opacity_jsx,
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
        adb_client: Optional[AdbMCPPhotoshopClient] = None,
        dry_run: bool = False,
    ):
        self.dry_run = bool(dry_run)
        self.com_bridge = com_bridge if (com_bridge is not None or dry_run) else PSCOMBridge()
        self.adb_client = adb_client if (adb_client is not None or dry_run) else AdbMCPPhotoshopClient()
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

    # ==================== 高频商业精修原子工具 ====================

    def ps_create_neutral_gray_layer(
        self,
        layer_name: str = "ZS_NeutralGray",
        blend_mode: str = "softlight",
        opacity: float = 100.0,
    ) -> Dict[str, Any]:
        jsx = build_neutral_gray_jsx(layer_name=layer_name, blend_mode=blend_mode, opacity=opacity)
        res = self._eval(jsx)
        return self._record(
            "ps_create_neutral_gray_layer",
            {"layer_name": layer_name, "blend_mode": blend_mode, "opacity": opacity},
            res,
        )

    def ps_create_frequency_separation(
        self,
        gaussian_radius: float = 6.0,
        group_name: str = "ZS_FreqSep",
    ) -> Dict[str, Any]:
        jsx = build_frequency_separation_jsx(gaussian_radius=gaussian_radius, group_name=group_name)
        res = self._eval(jsx)
        return self._record(
            "ps_create_frequency_separation",
            {"gaussian_radius": _clamp(gaussian_radius, 1.0, 25.0), "group_name": group_name},
            res,
        )

    def ps_add_levels_layer(
        self,
        black_point: int = 0,
        gamma: float = 1.0,
        white_point: int = 255,
        output_black: int = 0,
        output_white: int = 255,
        layer_name: str = "ZS_Levels",
    ) -> Dict[str, Any]:
        jsx = build_levels_layer_jsx(
            black_point=black_point,
            gamma=gamma,
            white_point=white_point,
            output_black=output_black,
            output_white=output_white,
            layer_name=layer_name,
        )
        res = self._eval(jsx)
        return self._record(
            "ps_add_levels_layer",
            {
                "black_point": black_point,
                "gamma": gamma,
                "white_point": white_point,
                "output_black": output_black,
                "output_white": output_white,
                "layer_name": layer_name,
            },
            res,
        )

    def ps_select_color_range(
        self,
        preset: str = "skinTones",
        fuzziness: int = 40,
        target_color_rgb: Optional[Sequence[int]] = None,
    ) -> Dict[str, Any]:
        jsx = build_select_color_range_jsx(
            preset=preset,
            fuzziness=fuzziness,
            target_color_rgb=target_color_rgb,
        )
        res = self._eval(jsx)
        return self._record(
            "ps_select_color_range",
            {"preset": preset, "fuzziness": fuzziness, "target_color_rgb": list(target_color_rgb) if target_color_rgb else None},
            res,
        )

    def ps_modify_selection(
        self,
        feather_px: float = 0.0,
        contract_px: int = 0,
        expand_px: int = 0,
        invert: bool = False,
    ) -> Dict[str, Any]:
        jsx = build_modify_selection_jsx(
            feather_px=feather_px,
            contract_px=contract_px,
            expand_px=expand_px,
            invert=invert,
        )
        res = self._eval(jsx)
        return self._record(
            "ps_modify_selection",
            {"feather_px": feather_px, "contract_px": contract_px, "expand_px": expand_px, "invert": bool(invert)},
            res,
        )

    def ps_add_layer_mask_from_selection(self, invert: bool = False) -> Dict[str, Any]:
        jsx = build_add_layer_mask_jsx(invert=invert)
        res = self._eval(jsx)
        return self._record("ps_add_layer_mask_from_selection", {"invert": bool(invert)}, res)

    def ps_set_layer_blend_opacity(
        self,
        blend_mode: str = "",
        opacity: Optional[float] = None,
        fill_opacity: Optional[float] = None,
        layer_name: str = "",
    ) -> Dict[str, Any]:
        jsx = build_set_layer_blend_opacity_jsx(
            blend_mode=blend_mode,
            opacity=opacity,
            fill_opacity=fill_opacity,
            layer_name=layer_name,
        )
        res = self._eval(jsx)
        return self._record(
            "ps_set_layer_blend_opacity",
            {"blend_mode": blend_mode, "opacity": opacity, "fill_opacity": fill_opacity, "layer_name": layer_name},
            res,
        )

    def ps_convert_to_smart_object(self, layer_name: str = "") -> Dict[str, Any]:
        jsx = build_convert_smart_object_jsx(layer_name=layer_name)
        res = self._eval(jsx)
        return self._record("ps_convert_to_smart_object", {"layer_name": layer_name}, res)

    def ps_apply_high_pass_sharpen(
        self,
        radius: float = 2.0,
        blend_mode: str = "overlay",
        opacity: float = 60.0,
        layer_name: str = "ZS_HighPass",
    ) -> Dict[str, Any]:
        jsx = build_high_pass_sharpen_jsx(
            radius=radius,
            blend_mode=blend_mode,
            opacity=opacity,
            layer_name=layer_name,
        )
        res = self._eval(jsx)
        return self._record(
            "ps_apply_high_pass_sharpen",
            {"radius": _clamp(radius, 0.3, 15.0), "blend_mode": blend_mode, "opacity": _clamp(opacity, 0.0, 100.0), "layer_name": layer_name},
            res,
        )

    def ps_select_subject(self) -> Dict[str, Any]:
        jsx = build_select_subject_jsx()
        res = self._eval(jsx)
        return self._record("ps_select_subject", {}, res)

    def ps_deselect(self) -> Dict[str, Any]:
        jsx = 'app.activeDocument.selection.deselect(); return "deselected";'
        res = self._eval(jsx)
        return self._record("ps_deselect", {}, res)

    def ps_apply_liquify(
        self,
        face_slim: int = 0,
        eye_size: int = 0,
        jawline: int = 0,
        convert_smart_first: bool = True,
    ) -> Dict[str, Any]:
        jsx = build_liquify_jsx(
            face_slim=face_slim,
            eye_size=eye_size,
            jawline=jawline,
            convert_smart_first=convert_smart_first,
        )
        res = self._eval(jsx)
        return self._record(
            "ps_apply_liquify",
            {
                "face_slim": int(_clamp(face_slim, -40, 40)),
                "eye_size": int(_clamp(eye_size, -30, 30)),
                "jawline": int(_clamp(jawline, -40, 40)),
                "convert_smart_first": bool(convert_smart_first),
            },
            res,
        )

    # ==================== 双万能引擎（100% Photoshop 全功能逃生舱） ====================

    def ps_execute_extendscript(self, jsx_code: str) -> Dict[str, Any]:
        """万能引擎 ①（COM 层）：带安全白名单审查的任意 Photoshop ExtendScript 执行器。"""
        ok, reason = _sanitize_revision_jsx(jsx_code)
        if not ok:
            raise ValueError(f"ExtendScript 安全审查未通过: {reason}")
        res = self._eval(str(jsx_code))
        return self._record("ps_execute_extendscript", {"length": len(jsx_code)}, res)

    def ps_execute_batchplay(self, descriptors: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """万能引擎 ②（UXP / adb-mcp 层）：直接执行任意 Photoshop batchPlay ActionDescriptor 序列。"""
        if not isinstance(descriptors, (list, tuple)) or not descriptors:
            raise ValueError("descriptors 必须为非空 ActionDescriptor 列表")
        for idx, item in enumerate(descriptors):
            if not isinstance(item, dict) or "_obj" not in item:
                raise ValueError(f"descriptors[{idx}] 缺少必需字段 _obj")
        if self.dry_run:
            res = {"status": "ok", "backend": "dry_run", "count": len(descriptors)}
            return self._record("ps_execute_batchplay", {"count": len(descriptors)}, res)
        assert self.adb_client is not None
        res = self.adb_client.send_command("batchPlay", {"descriptors": list(descriptors)})
        return self._record("ps_execute_batchplay", {"count": len(descriptors)}, res)

    # ==================== 视觉自检闭环（AI 边修边看画布） ====================

    def ps_get_canvas_preview(
        self,
        max_edge: int = 768,
        return_base64: bool = False,
        output_path: str = "",
    ) -> Dict[str, Any]:
        """导出当前画布 8-bit sRGB 预览图并计算亮度/高光/暗部统计，供 AI 视觉自检。"""
        out_p = Path(output_path) if output_path else (Path(tempfile.gettempdir()) / "zs_mcp_canvas_preview.jpg")
        export_res = self.ps_export_preview(str(out_p), max_edge=max_edge, quality=88)
        metrics: Dict[str, Any] = {}
        b64_url = ""
        if not self.dry_run and out_p.exists():
            try:
                import numpy as np
                from PIL import Image
                with Image.open(out_p) as im:
                    arr = np.asarray(im.convert("RGB"), dtype=np.float32)
                    luma = 0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]
                    metrics = {
                        "width": im.width,
                        "height": im.height,
                        "mean_luma": round(float(luma.mean()), 2),
                        "highlight_clip_ratio": round(float((luma > 248).mean()), 4),
                        "shadow_clip_ratio": round(float((luma < 8).mean()), 4),
                    }
                if return_base64:
                    b64_url = "data:image/jpeg;base64," + base64.b64encode(out_p.read_bytes()).decode("ascii")
            except Exception as exc:  # noqa: BLE001
                metrics["metrics_error"] = str(exc)
        elif self.dry_run:
            metrics = {"mean_luma": 128.0, "highlight_clip_ratio": 0.01, "shadow_clip_ratio": 0.02, "simulated": True}

        res = {
            "status": "ok",
            "preview_path": str(out_p),
            "metrics": metrics,
            "image_data_url": b64_url if return_base64 else "",
            "export": export_res,
        }
        return self._record("ps_get_canvas_preview", {"max_edge": max_edge, "return_base64": return_base64}, res)

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
            "ps_create_neutral_gray_layer": self.ps_create_neutral_gray_layer,
            "ps_create_frequency_separation": self.ps_create_frequency_separation,
            "ps_add_levels_layer": self.ps_add_levels_layer,
            "ps_select_color_range": self.ps_select_color_range,
            "ps_modify_selection": self.ps_modify_selection,
            "ps_add_layer_mask_from_selection": self.ps_add_layer_mask_from_selection,
            "ps_set_layer_blend_opacity": self.ps_set_layer_blend_opacity,
            "ps_convert_to_smart_object": self.ps_convert_to_smart_object,
            "ps_apply_high_pass_sharpen": self.ps_apply_high_pass_sharpen,
            "ps_select_subject": self.ps_select_subject,
            "ps_deselect": self.ps_deselect,
            "ps_apply_liquify": self.ps_apply_liquify,
            "ps_execute_extendscript": self.ps_execute_extendscript,
            "ps_execute_batchplay": self.ps_execute_batchplay,
            "ps_get_canvas_preview": self.ps_get_canvas_preview,
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
            {
                "type": "function",
                "function": {
                    "name": "ps_create_neutral_gray_layer",
                    "description": "新建 50% 中性灰柔光/叠加图层，用于非破坏性光影重塑与修瑕。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "layer_name": {"type": "string"},
                            "blend_mode": {"type": "string", "enum": ["softlight", "overlay"]},
                            "opacity": {"type": "number"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_create_frequency_separation",
                    "description": "一键建立高低频磨皮图层组（低频高斯模糊 + 高频 Apply Image 线性光）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "gaussian_radius": {"type": "number", "description": "低频模糊半径 1.0~25.0 px，人像常用 4~8"},
                            "group_name": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_add_levels_layer",
                    "description": "新建非破坏性「色阶」调整图层（控制黑场/灰场/白场）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "black_point": {"type": "integer"},
                            "gamma": {"type": "number"},
                            "white_point": {"type": "integer"},
                            "output_black": {"type": "integer"},
                            "output_white": {"type": "integer"},
                            "layer_name": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_select_color_range",
                    "description": "按色彩范围创建选区（肤色/高光/中间调/阴影或自定义 RGB）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "preset": {"type": "string", "enum": ["skinTones", "highlights", "midtones", "shadows", "reds", "yellows", "greens", "cyans", "blues", "magentas"]},
                            "fuzziness": {"type": "integer"},
                            "target_color_rgb": {"type": "array", "items": {"type": "integer"}},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_modify_selection",
                    "description": "对当前选区执行羽化、收缩、扩展或反选。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "feather_px": {"type": "number"},
                            "contract_px": {"type": "integer"},
                            "expand_px": {"type": "integer"},
                            "invert": {"type": "boolean"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_add_layer_mask_from_selection",
                    "description": "将当前选区转为激活图层的蒙版（支持反向隐藏）。",
                    "parameters": {
                        "type": "object",
                        "properties": {"invert": {"type": "boolean"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_set_layer_blend_opacity",
                    "description": "设置图层混合模式、不透明度与填充度。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "blend_mode": {"type": "string", "enum": sorted(BLEND_MODES_MAP.keys())},
                            "opacity": {"type": "number"},
                            "fill_opacity": {"type": "number"},
                            "layer_name": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_convert_to_smart_object",
                    "description": "将目标图层转为智能对象（后续滤镜均可二次修改）。",
                    "parameters": {
                        "type": "object",
                        "properties": {"layer_name": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_apply_high_pass_sharpen",
                    "description": "盖印可见图层 -> 转智能对象 -> 高反差保留锐化（叠加/柔光）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "radius": {"type": "number"},
                            "blend_mode": {"type": "string"},
                            "opacity": {"type": "number"},
                            "layer_name": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_select_subject",
                    "description": "一键调用 Photoshop 原生「选择主体」抠出人物主体选区（之后新建的调整层会自动携带主体蒙版）。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_deselect",
                    "description": "取消当前文档选区（切换分区蒙版前必须调用）。",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_apply_liquify",
                    "description": "非破坏性智能对象人脸感知微调液化（瘦脸/大眼/下颌线，均做安全钳位）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "face_slim": {"type": "integer"},
                            "eye_size": {"type": "integer"},
                            "jawline": {"type": "integer"},
                            "convert_smart_first": {"type": "boolean"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_execute_extendscript",
                    "description": "万能引擎①：执行任意通过安全审查的 Photoshop ExtendScript (ES3) 代码。",
                    "parameters": {
                        "type": "object",
                        "properties": {"jsx_code": {"type": "string"}},
                        "required": ["jsx_code"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_execute_batchplay",
                    "description": "万能引擎②：经由 adb-mcp 执行任意 Photoshop batchPlay ActionDescriptor 列表。",
                    "parameters": {
                        "type": "object",
                        "properties": {"descriptors": {"type": "array", "items": {"type": "object"}}},
                        "required": ["descriptors"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ps_get_canvas_preview",
                    "description": "导出当前画布预览图并返回亮度/高光/暗部统计，供 AI 视觉自检。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "max_edge": {"type": "integer"},
                            "return_base64": {"type": "boolean"},
                        },
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

    @mcp.tool()
    def ps_create_neutral_gray_layer(layer_name: str = "ZS_NeutralGray", blend_mode: str = "softlight", opacity: float = 100.0) -> str:
        """新建 50% 中性灰柔光/叠加图层。"""
        return json.dumps(kit.ps_create_neutral_gray_layer(layer_name=layer_name, blend_mode=blend_mode, opacity=opacity), ensure_ascii=False)

    @mcp.tool()
    def ps_create_frequency_separation(gaussian_radius: float = 6.0, group_name: str = "ZS_FreqSep") -> str:
        """一键建立高低频磨皮图层组（低频高斯模糊 + 高频 Apply Image 线性光）。"""
        return json.dumps(kit.ps_create_frequency_separation(gaussian_radius=gaussian_radius, group_name=group_name), ensure_ascii=False)

    @mcp.tool()
    def ps_add_levels_layer(black_point: int = 0, gamma: float = 1.0, white_point: int = 255, output_black: int = 0, output_white: int = 255, layer_name: str = "ZS_Levels") -> str:
        """新建非破坏性色阶调整图层。"""
        return json.dumps(kit.ps_add_levels_layer(black_point=black_point, gamma=gamma, white_point=white_point, output_black=output_black, output_white=output_white, layer_name=layer_name), ensure_ascii=False)

    @mcp.tool()
    def ps_select_color_range(preset: str = "skinTones", fuzziness: int = 40, target_color_rgb: Optional[List[int]] = None) -> str:
        """按色彩范围创建选区（肤色/高光/中间调/阴影或自定义 RGB）。"""
        return json.dumps(kit.ps_select_color_range(preset=preset, fuzziness=fuzziness, target_color_rgb=target_color_rgb), ensure_ascii=False)

    @mcp.tool()
    def ps_modify_selection(feather_px: float = 0.0, contract_px: int = 0, expand_px: int = 0, invert: bool = False) -> str:
        """对当前选区执行羽化、收缩、扩展或反选。"""
        return json.dumps(kit.ps_modify_selection(feather_px=feather_px, contract_px=contract_px, expand_px=expand_px, invert=invert), ensure_ascii=False)

    @mcp.tool()
    def ps_add_layer_mask_from_selection(invert: bool = False) -> str:
        """将当前选区转为激活图层的蒙版。"""
        return json.dumps(kit.ps_add_layer_mask_from_selection(invert=invert), ensure_ascii=False)

    @mcp.tool()
    def ps_set_layer_blend_opacity(blend_mode: str = "", opacity: Optional[float] = None, fill_opacity: Optional[float] = None, layer_name: str = "") -> str:
        """修改图层混合模式、不透明度与填充度。"""
        return json.dumps(kit.ps_set_layer_blend_opacity(blend_mode=blend_mode, opacity=opacity, fill_opacity=fill_opacity, layer_name=layer_name), ensure_ascii=False)

    @mcp.tool()
    def ps_convert_to_smart_object(layer_name: str = "") -> str:
        """将目标图层转为智能对象。"""
        return json.dumps(kit.ps_convert_to_smart_object(layer_name=layer_name), ensure_ascii=False)

    @mcp.tool()
    def ps_apply_high_pass_sharpen(radius: float = 2.0, blend_mode: str = "overlay", opacity: float = 60.0, layer_name: str = "ZS_HighPass") -> str:
        """盖印可见图层 -> 转智能对象 -> 高反差保留锐化。"""
        return json.dumps(kit.ps_apply_high_pass_sharpen(radius=radius, blend_mode=blend_mode, opacity=opacity, layer_name=layer_name), ensure_ascii=False)

    @mcp.tool()
    def ps_select_subject() -> str:
        """一键调用 Photoshop 原生选择主体。"""
        return json.dumps(kit.ps_select_subject(), ensure_ascii=False)

    @mcp.tool()
    def ps_deselect() -> str:
        """取消当前文档选区。"""
        return json.dumps(kit.ps_deselect(), ensure_ascii=False)

    @mcp.tool()
    def ps_apply_liquify(face_slim: int = 0, eye_size: int = 0, jawline: int = 0, convert_smart_first: bool = True) -> str:
        """非破坏性智能对象人脸感知微调液化（带安全钳位）。"""
        return json.dumps(kit.ps_apply_liquify(face_slim=face_slim, eye_size=eye_size, jawline=jawline, convert_smart_first=convert_smart_first), ensure_ascii=False)

    @mcp.tool()
    def ps_execute_extendscript(jsx_code: str) -> str:
        """万能引擎①（COM 层）：带安全白名单审查的任意 Photoshop ExtendScript 执行器。"""
        return json.dumps(kit.ps_execute_extendscript(jsx_code), ensure_ascii=False)

    @mcp.tool()
    def ps_execute_batchplay(descriptors: List[Dict[str, Any]]) -> str:
        """万能引擎②（UXP / adb-mcp 层）：执行任意 Photoshop batchPlay ActionDescriptor 列表。"""
        return json.dumps(kit.ps_execute_batchplay(descriptors), ensure_ascii=False)

    @mcp.tool()
    def ps_get_canvas_preview(max_edge: int = 768, return_base64: bool = False) -> str:
        """导出当前画布预览图并返回亮度/高光/暗部统计，供 AI 视觉自检。"""
        return json.dumps(kit.ps_get_canvas_preview(max_edge=max_edge, return_base64=return_base64), ensure_ascii=False)

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
