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
    build_ambient_glow_jsx,
    build_clipping_mask_jsx,
    build_convert_smart_object_jsx,
    build_exposure_gamma_jsx,
    build_frequency_separation_jsx,
    build_high_pass_sharpen_jsx,
    build_levels_layer_jsx,
    build_liquify_jsx,
    build_modify_selection_jsx,
    build_neutral_gray_jsx,
    build_select_color_range_jsx,
    build_select_subject_jsx,
    build_set_layer_blend_opacity_jsx,
    build_vibrance_layer_jsx,
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
        gamma: float = 1.0,
        offset: float = 0.0,
        layer_name: str = "ZS_Exposure",
    ) -> Dict[str, Any]:
        lo, hi = SAFETY_BOUNDS["exposure_delta"]
        safe_exp = _clamp(exposure_delta, lo, hi)
        safe_gamma = _clamp(gamma, 0.70, 1.40)
        safe_offset = _clamp(offset, -0.1, 0.1)
        if abs(safe_exp) <= 1e-3 and abs(safe_gamma - 1.0) <= 1e-3 and abs(safe_offset) <= 1e-4:
            res = {"status": "skipped", "reason": "exposure/gamma delta is ~0"}
            return self._record("ps_add_exposure_layer", {"exposure_delta": safe_exp, "gamma": safe_gamma}, res)
        jsx = build_exposure_gamma_jsx(
            exposure_delta=safe_exp,
            offset=safe_offset,
            gamma=safe_gamma,
            layer_name=layer_name,
        )
        res = self._eval(jsx)
        res["applied_exposure_delta"] = safe_exp
        res["applied_gamma"] = safe_gamma
        return self._record(
            "ps_add_exposure_layer",
            {"exposure_delta": safe_exp, "gamma": safe_gamma, "offset": safe_offset, "layer_name": layer_name},
            res,
        )

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
        """导出 sRGB JPEG；当 max_edge <= 0 时保持原图全分辨率导出（不缩放）。"""
        out = Path(output_jpg_path)
        if not self.dry_run:
            out.parent.mkdir(parents=True, exist_ok=True)
        q_ps = max(1, min(12, int(round(int(quality) / 100.0 * 12))))
        edge = 0 if int(max_edge) <= 0 else max(256, min(16384, int(max_edge)))
        resize_js = ""
        if edge > 0:
            resize_js = f"""
            var w = dup.width.as("px");
            var h = dup.height.as("px");
            if (Math.max(w, h) > {edge}) {{
                if (w >= h) {{
                    dup.resizeImage(UnitValue({edge}, "px"), null, null, ResampleMethod.BICUBICSHARPER);
                }} else {{
                    dup.resizeImage(null, UnitValue({edge}, "px"), null, ResampleMethod.BICUBICSHARPER);
                }}
            }}
            """
        jsx = f"""
        var origDoc = app.activeDocument;
        var dup = origDoc.duplicate("zs_preview_tmp", true);
        try {{
            dup.convertProfile("sRGB IEC61966-2.1", Intent.PERCEPTUAL, true, false);
            if (dup.bitsPerChannel !== BitsPerChannelType.EIGHT) {{
                dup.bitsPerChannel = BitsPerChannelType.EIGHT;
            }}
            {resize_js}
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

    def ps_add_vibrance_layer(
        self,
        vibrance: int = 0,
        saturation: int = 0,
        layer_name: str = "ZS_Vibrance",
    ) -> Dict[str, Any]:
        jsx = build_vibrance_layer_jsx(vibrance=vibrance, saturation=saturation, layer_name=layer_name)
        res = self._eval(jsx)
        return self._record(
            "ps_add_vibrance_layer",
            {"vibrance": int(_clamp(vibrance, -100, 60)), "saturation": int(_clamp(saturation, -80, 40)), "layer_name": layer_name},
            res,
        )

    def ps_set_clipping_mask(self, enabled: bool = True) -> Dict[str, Any]:
        jsx = build_clipping_mask_jsx(enabled=enabled)
        res = self._eval(jsx)
        return self._record("ps_set_clipping_mask", {"enabled": bool(enabled)}, res)

    def ps_add_ambient_glow_layer(
        self,
        opacity: float = 18.0,
        blend_mode: str = "screen",
        layer_name: str = "ZS_AmbientGlow",
    ) -> Dict[str, Any]:
        jsx = build_ambient_glow_jsx(opacity=opacity, blend_mode=blend_mode, layer_name=layer_name)
        res = self._eval(jsx)
        return self._record(
            "ps_add_ambient_glow_layer",
            {"opacity": _clamp(opacity, 5.0, 40.0), "blend_mode": blend_mode, "layer_name": layer_name},
            res,
        )

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

    def _apply_patch_mask(
        self,
        mask_mode: str = "skin",
        bbox: Optional[Sequence[float]] = None,
        feather_px: float = 12.0,
        fuzziness: int = 40,
    ) -> str:
        """为当前顶层 AI 贴片图层强制建立精准蒙版（绝不整图无蒙版覆盖）。"""
        mode = str(mask_mode or "skin").strip().lower()
        if mode not in ("skin", "subject", "background", "bbox", "hidden"):
            mode = "skin"
        f_px = _clamp(feather_px, 0.0, 80.0)

        if mode == "hidden":
            jsx = """
            var doc = app.activeDocument;
            doc.selection.selectAll();
            var d = new ActionDescriptor();
            d.putClass(charIDToTypeID("Nw  "), charIDToTypeID("Chnl"));
            var r = new ActionReference();
            r.putEnumerated(charIDToTypeID("Chnl"), charIDToTypeID("Chnl"), charIDToTypeID("Msk "));
            d.putReference(charIDToTypeID("At  "), r);
            d.putEnumerated(charIDToTypeID("Usng"), charIDToTypeID("UsrM"), charIDToTypeID("HdSl"));
            executeAction(charIDToTypeID("Mk  "), d, DialogModes.NO);
            doc.selection.deselect();
            return "mask:hidden";
            """
            self._eval(jsx)
            return "hidden"

        if mode == "bbox" and bbox and len(bbox) == 4:
            x1, y1, x2, y2 = [max(0.0, min(1.0, float(v))) for v in bbox]
            jsx = f"""
            var doc = app.activeDocument;
            var w = doc.width.as("px");
            var h = doc.height.as("px");
            var region = [[w * {x1}, h * {y1}], [w * {x2}, h * {y1}], [w * {x2}, h * {y2}], [w * {x1}, h * {y2}]];
            doc.selection.select(region);
            try {{ doc.selection.feather(UnitValue({f_px}, "px")); }} catch (e) {{}}
            return "bbox_selected";
            """
            self._eval(jsx)
            self.ps_add_layer_mask_from_selection(invert=False)
            self.ps_deselect()
            return f"bbox:{x1:.2f},{y1:.2f},{x2:.2f},{y2:.2f}"

        if mode == "skin":
            self.ps_select_color_range(preset="skinTones", fuzziness=int(_clamp(fuzziness, 15, 80)))
            if f_px > 0:
                self.ps_modify_selection(feather_px=f_px, invert=False)
            self.ps_add_layer_mask_from_selection(invert=False)
            self.ps_deselect()
            return "skin"

        if mode == "subject":
            self.ps_select_subject()
            if f_px > 0:
                self.ps_modify_selection(feather_px=f_px, invert=False)
            self.ps_add_layer_mask_from_selection(invert=False)
            self.ps_deselect()
            return "subject"

        # background
        self.ps_select_subject()
        self.ps_modify_selection(feather_px=f_px, invert=True)
        self.ps_add_layer_mask_from_selection(invert=False)
        self.ps_deselect()
        return "background"

    def ps_place_image_with_mask(
        self,
        image_path: str,
        mask_mode: str = "skin",
        bbox: Optional[Sequence[float]] = None,
        opacity: float = 65.0,
        blend_mode: str = "normal",
        feather_px: float = 12.0,
        layer_name: str = "ZS_AI_MaskedPatch",
    ) -> Dict[str, Any]:
        """将外部/AI 生成的精修参考图置入当前画布独立图层，并强制套用局部蒙版（skin/subject/background/bbox/hidden），绝不整图覆盖原图。"""
        img_p = Path(image_path)
        if not self.dry_run and not img_p.exists():
            raise FileNotFoundError(f"待置入图片不存在: {img_p}")
        op = _clamp(opacity, 10.0, 90.0)
        place_jsx = f"""
        var doc = app.activeDocument;
        var f = new File({_js_str(_ps_path(img_p))});
        var idPlc = charIDToTypeID("Plc ");
        var desc = new ActionDescriptor();
        desc.putPath(charIDToTypeID("null"), f);
        desc.putEnumerated(charIDToTypeID("FTcs"), charIDToTypeID("QCSt"), charIDToTypeID("Qcsa"));
        executeAction(idPlc, desc, DialogModes.NO);
        var lyr = doc.activeLayer;
        lyr.name = {_js_str(layer_name)};
        // 对齐撑满原画布，确保与底层像素 1:1 严丝合缝
        var b = lyr.bounds;
        var lw = b[2].as("px") - b[0].as("px");
        var lh = b[3].as("px") - b[1].as("px");
        var dw = doc.width.as("px");
        var dh = doc.height.as("px");
        if (lw > 0 && lh > 0) {{
            lyr.resize((dw / lw) * 100.0, (dh / lh) * 100.0, AnchorPosition.MIDDLECENTER);
        }}
        var b2 = lyr.bounds;
        lyr.translate(-b2[0].as("px"), -b2[1].as("px"));
        return "placed:" + lyr.name;
        """
        place_res = self._eval(place_jsx)
        applied_mask = self._apply_patch_mask(mask_mode=mask_mode, bbox=bbox, feather_px=feather_px)
        self.ps_set_layer_blend_opacity(blend_mode=blend_mode, opacity=op, layer_name=layer_name)
        return self._record(
            "ps_place_image_with_mask",
            {"image_path": str(img_p), "mask_mode": applied_mask, "opacity": op, "blend_mode": blend_mode, "layer_name": layer_name},
            {"status": "ok", "place": place_res, "mask": applied_mask},
        )

    def ps_ai_generate_masked_patch(
        self,
        prompt: str,
        mask_mode: str = "skin",
        bbox: Optional[Sequence[float]] = None,
        opacity: float = 60.0,
        blend_mode: str = "normal",
        feather_px: float = 12.0,
        layer_name: str = "ZS_AI_Skin_Patch",
    ) -> Dict[str, Any]:
        """调用本机 PSAI 插件（ZERO-STUDIO-PS-Plugin）或 .env 图像编辑 API 生成局部精修素材，再通过蒙版仅保留所需区域（绝不整图 AI 覆盖）。"""
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise ValueError("ps_ai_generate_masked_patch 必须提供 prompt")
        op = _clamp(opacity, 10.0, 90.0)
        if self.dry_run:
            return self._record(
                "ps_ai_generate_masked_patch",
                {"prompt": clean_prompt, "mask_mode": mask_mode, "opacity": op, "blend_mode": blend_mode, "layer_name": layer_name},
                {"status": "ok", "backend": "dry_run", "mask": mask_mode},
            )

        # 导出当前画布 1024px 预览图作为图生图参考输入
        tmp_dir = Path(tempfile.gettempdir()) / "zs_ai_patches"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        src_jpg = tmp_dir / f"src_{os.getpid()}.jpg"
        prev = self.ps_get_canvas_preview(max_edge=1024, return_base64=True, output_path=str(src_jpg))
        src_data_url = prev.get("image_data_url", "")

        # 通道 A：优先调用本机已打开的 ZERO-STUDIO-PS-Plugin (Socket.IO MCP 桥)
        psai_err = ""
        try:
            from adb_mcp_client import AdbMCPPhotoshopClient

            adb = AdbMCPPhotoshopClient(dry_run=False)
            if adb.is_available():
                res = adb.send_command(
                    "psaiGenerateAndPlace",
                    {
                        "prompt": clean_prompt,
                        "sourceImageDataUrl": src_data_url,
                        "bbox": list(bbox) if bbox else None,
                        "layerName": layer_name,
                        "colorMatch": True,
                    },
                    timeout=120.0,
                )
                if res.get("status") == "ok":
                    applied_mask = self._apply_patch_mask(mask_mode=mask_mode, bbox=bbox, feather_px=feather_px)
                    self.ps_set_layer_blend_opacity(blend_mode=blend_mode, opacity=op, layer_name=layer_name)
                    return self._record(
                        "ps_ai_generate_masked_patch",
                        {"prompt": clean_prompt, "mask_mode": applied_mask, "opacity": op, "layer_name": layer_name},
                        {"status": "ok", "backend": "psai-uxp-plugin", "mask": applied_mask, "plugin_res": res},
                    )
        except Exception as exc:  # noqa: BLE001
            psai_err = str(exc)

        # 通道 B：回退到 .env 配置的 OpenAI / GRS 图像编辑接口（IMAGE_GEN_API_URL 或 GRS_BASE_URL）
        import httpx

        api_base = (os.getenv("IMAGE_GEN_API_URL") or os.getenv("GRS_BASE_URL") or "").strip().rstrip("/")
        api_key = (os.getenv("IMAGE_GEN_API_KEY") or os.getenv("GRS_API_KEY") or "").strip()
        model = (os.getenv("IMAGE_EDIT_MODEL") or "nano-banana-2.1").strip()
        if not api_base or not api_key:
            raise RuntimeError(
                f"PSAI 插件未连接({psai_err or '未开启'})且未配置 IMAGE_GEN_API_URL/IMAGE_GEN_API_KEY，无法生成局部贴片"
            )

        endpoint = api_base if api_base.endswith("/edits") or api_base.endswith("/generations") else f"{api_base}/images/edits"
        with httpx.Client(timeout=120.0) as client:
            with open(src_jpg, "rb") as fh:
                resp = client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {api_key}"},
                    data={"model": model, "prompt": clean_prompt, "response_format": "b64_json"},
                    files={"image": ("canvas.jpg", fh, "image/jpeg")},
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"图像编辑 API 失败 (HTTP {resp.status_code}): {resp.text[:300]}")
            payload = resp.json()
            item = (payload.get("data") or [{}])[0]
            out_img = tmp_dir / f"patch_{os.getpid()}.png"
            if item.get("b64_json"):
                out_img.write_bytes(base64.b64decode(item["b64_json"]))
            elif item.get("url"):
                r_img = client.get(item["url"])
                r_img.raise_for_status()
                out_img.write_bytes(r_img.content)
            else:
                raise RuntimeError(f"图像编辑 API 未返回图片数据: {str(payload)[:200]}")

        return self.ps_place_image_with_mask(
            image_path=str(out_img),
            mask_mode=mask_mode,
            bbox=bbox,
            opacity=op,
            blend_mode=blend_mode,
            feather_px=feather_px,
            layer_name=layer_name,
        )

    # ==================== OpenAI Tool Calling Schema & Dispatch ====================

    @staticmethod
    def _normalize_tool_args(tool_name: str, raw_args: Dict[str, Any], fn: Callable[..., Any]) -> Dict[str, Any]:
        """兼容大模型 Tool Calling / 终审 fix_calls 常见的参数别名，并过滤签名外字段防止 TypeError。"""
        import inspect

        args = dict(raw_args)
        # 通用别名: name -> layer_name
        if "name" in args and "layer_name" not in args:
            args["layer_name"] = args.pop("name")

        if tool_name == "ps_add_exposure_layer":
            if "exposure" in args and "exposure_delta" not in args:
                args["exposure_delta"] = args.pop("exposure")
            if "exposure_delta" not in args:
                args["exposure_delta"] = 0.0

        elif tool_name == "ps_add_color_balance_layer":
            if "temp" in args and "temp_delta" not in args:
                args["temp_delta"] = args.pop("temp")
            if "tint" in args and "tint_delta" not in args:
                args["tint_delta"] = args.pop("tint")

        elif tool_name == "ps_add_curves_layer":
            if "points" in args and "curves_points" not in args:
                args["curves_points"] = args.pop("points")
            if "curves_points" not in args:
                args["curves_points"] = [[0, 0], [128, 128], [255, 255]]

        elif tool_name == "ps_add_hsl_layer":
            if "channels" in args and "hsl_adjustments" not in args:
                args["hsl_adjustments"] = args.pop("channels")
            if "hsl_adjustments" not in args or not isinstance(args.get("hsl_adjustments"), dict):
                # 兼容 AI 传 target="skin"/"oranges"/"master" + saturation_delta/lightness_delta/hue_delta
                target = str(args.pop("target", "") or args.pop("channel", "") or "oranges").strip().lower()
                ch_map = {
                    "skin": "oranges", "orange": "oranges", "oranges": "oranges",
                    "red": "reds", "reds": "reds",
                    "yellow": "yellows", "yellows": "yellows",
                    "green": "greens", "greens": "greens",
                    "cyan": "cyans", "cyans": "cyans", "aqua": "cyans", "aquas": "cyans",
                    "blue": "blues", "blues": "blues",
                    "purple": "magentas", "magenta": "magentas", "magentas": "magentas",
                }
                ch_key = ch_map.get(target, "oranges")
                h_val = float(args.pop("hue_delta", args.pop("hue", 0.0)) or 0.0)
                s_val = float(args.pop("saturation_delta", args.pop("saturation", 0.0)) or 0.0)
                l_val = float(args.pop("lightness_delta", args.pop("luminance_delta", args.pop("lightness", args.pop("luminance", 0.0)))) or 0.0)
                if target in ("all", "master", "background", "bg"):
                    args["hsl_adjustments"] = {
                        c: {"hue": h_val, "saturation": s_val, "lightness": l_val}
                        for c in ("reds", "yellows", "greens", "cyans", "blues")
                    }
                else:
                    args["hsl_adjustments"] = {ch_key: {"hue": h_val, "saturation": s_val, "lightness": l_val}}

        elif tool_name == "ps_add_vibrance_layer":
            if "vibrance_delta" in args and "vibrance" not in args:
                args["vibrance"] = int(round(float(args.pop("vibrance_delta"))))
            if "saturation_delta" in args and "saturation" not in args:
                args["saturation"] = int(round(float(args.pop("saturation_delta"))))

        elif tool_name == "ps_modify_selection":
            action = str(args.pop("action", "") or "").strip().lower()
            if action == "invert":
                args["invert"] = True
            if "radius" in args and "feather_px" not in args:
                args["feather_px"] = args.pop("radius")
            if "feather" in args and "feather_px" not in args:
                args["feather_px"] = args.pop("feather")
            if "expand" in args and "expand_px" not in args:
                args["expand_px"] = args.pop("expand")
            if "contract" in args and "contract_px" not in args:
                args["contract_px"] = args.pop("contract")

        elif tool_name == "ps_set_layer_blend_opacity":
            if "blend" in args and "blend_mode" not in args:
                args["blend_mode"] = args.pop("blend")

        # 终极防爆：按目标方法签名过滤未知参数
        sig = inspect.signature(fn)
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if not has_varkw:
            valid_keys = set(sig.parameters.keys())
            args = {k: v for k, v in args.items() if k in valid_keys}
        return args

    def dispatch_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """供本地 8B 模型 Tool Calling 或编排器统一分发执行。支持 AI 便捷传入 mask='subject'|'background'|'skin'。"""
        args = dict(arguments) if isinstance(arguments, dict) else {}
        auto_mask = str(args.pop("mask", "") or "").strip().lower()
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
            "ps_add_vibrance_layer": self.ps_add_vibrance_layer,
            "ps_set_clipping_mask": self.ps_set_clipping_mask,
            "ps_add_ambient_glow_layer": self.ps_add_ambient_glow_layer,
            "ps_apply_liquify": self.ps_apply_liquify,
            "ps_execute_extendscript": self.ps_execute_extendscript,
            "ps_execute_batchplay": self.ps_execute_batchplay,
            "ps_get_canvas_preview": self.ps_get_canvas_preview,
            "ps_place_image_with_mask": self.ps_place_image_with_mask,
            "ps_ai_generate_masked_patch": self.ps_ai_generate_masked_patch,
        }
        fn = handlers.get(tool_name)
        if fn is None:
            raise ValueError(f"未知 Photoshop MCP 工具: {tool_name}")

        # 若 AI 附带 mask="subject" / "background" / "skin"，自动前置建立羽化选区并在图层创建后取消选区
        mask_applied = False
        if auto_mask in ("subject", "person", "foreground"):
            self.ps_select_subject()
            self.ps_modify_selection(feather_px=4.0, invert=False)
            mask_applied = True
        elif auto_mask in ("background", "bg"):
            self.ps_select_subject()
            self.ps_modify_selection(feather_px=8.0, invert=True)
            mask_applied = True
        elif auto_mask in ("skin", "skintones", "skin_tones"):
            self.ps_select_color_range(preset="skinTones", fuzziness=38)
            self.ps_modify_selection(feather_px=5.0, invert=False)
            mask_applied = True

        try:
            clean_args = self._normalize_tool_args(tool_name, args, fn)
            return fn(**clean_args)
        finally:
            if mask_applied:
                try:
                    self.ps_deselect()
                except Exception:
                    pass

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
    def ps_add_vibrance_layer(vibrance: int = 0, saturation: int = 0, layer_name: str = "ZS_Vibrance") -> str:
        """新建非破坏性自然饱和度调整图层（支持背景降 -70 或主体提鲜活 +15）。"""
        return json.dumps(kit.ps_add_vibrance_layer(vibrance=vibrance, saturation=saturation, layer_name=layer_name), ensure_ascii=False)

    @mcp.tool()
    def ps_set_clipping_mask(enabled: bool = True) -> str:
        """设置或取消当前图层的剪贴蒙版。"""
        return json.dumps(kit.ps_set_clipping_mask(enabled=enabled), ensure_ascii=False)

    @mcp.tool()
    def ps_add_ambient_glow_layer(opacity: float = 18.0, blend_mode: str = "screen", layer_name: str = "ZS_AmbientGlow") -> str:
        """在光源方向新建柔和空气感高光辉光层（滤色/线性减淡）。"""
        return json.dumps(kit.ps_add_ambient_glow_layer(opacity=opacity, blend_mode=blend_mode, layer_name=layer_name), ensure_ascii=False)

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
