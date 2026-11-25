#!/usr/bin/env python3
"""
ZERO STUDIO · Photoshop 高级精修原子工具 ExtendScript 构建器 (ps_mcp_advanced.py)
================================================================================
覆盖商业人像与二次元 Cosplay 高频精修全套非破坏性操作：
- 中性灰图层 (Neutral Gray 50% SoftLight/Overlay)
- 高低频磨皮图层组 (Frequency Separation: Low Gaussian + High ApplyImage LinearLight)
- 色阶调整图层 (Levels)
- 色彩范围选区 (Color Range: 肤色/高光/中间调/阴影/自定义 RGB)
- 选区羽化 / 收缩 / 扩展 / 反选 (Modify Selection)
- 选区转图层蒙版 (Layer Mask from Selection)
- 图层混合模式与不透明度 / 填充度 (Blend Mode & Opacity)
- 转智能对象 (Convert to Smart Object)
- 智能高反差保留锐化 (Stamped -> Smart Object -> High Pass -> Overlay/SoftLight)
- 人脸感知液化 / 液化网格 (Face-Aware Liquify / Liquify Mesh，零弹窗)
- 选择主体 (Select Subject)
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence


def _js_str(val: str) -> str:
    return json.dumps(str(val), ensure_ascii=True)


def _clamp(val: Any, lo: float, hi: float) -> float:
    try:
        num = float(val)
    except (TypeError, ValueError):
        num = 0.0
    return round(max(lo, min(hi, num)), 3)


BLEND_MODES_MAP = {
    "normal": "BlendMode.NORMAL",
    "multiply": "BlendMode.MULTIPLY",
    "screen": "BlendMode.SCREEN",
    "overlay": "BlendMode.OVERLAY",
    "softlight": "BlendMode.SOFTLIGHT",
    "hardlight": "BlendMode.HARDLIGHT",
    "linearlight": "BlendMode.LINEARLIGHT",
    "vividlight": "BlendMode.VIVIDLIGHT",
    "darken": "BlendMode.DARKEN",
    "lighten": "BlendMode.LIGHTEN",
    "color": "BlendMode.COLORBLEND",
    "luminosity": "BlendMode.LUMINOSITY",
    "hue": "BlendMode.HUE",
    "saturation": "BlendMode.SATURATION",
}


def build_neutral_gray_jsx(
    layer_name: str = "ZS_NeutralGray",
    blend_mode: str = "softlight",
    opacity: float = 100.0,
) -> str:
    """新建 50% 中性灰柔光/叠加图层（非破坏性中性灰修瑕与光影重塑起手式）。"""
    bm = BLEND_MODES_MAP.get(str(blend_mode or "softlight").strip().lower(), "BlendMode.SOFTLIGHT")
    op = _clamp(opacity, 0.0, 100.0)
    return f"""
    var doc = app.activeDocument;
    var ng = doc.artLayers.add();
    ng.name = {_js_str(layer_name)};
    var grayColor = new SolidColor();
    grayColor.rgb.red = 128;
    grayColor.rgb.green = 128;
    grayColor.rgb.blue = 128;
    doc.selection.selectAll();
    doc.selection.fill(grayColor, ColorBlendMode.NORMAL, 100, false);
    doc.selection.deselect();
    ng.blendMode = {bm};
    ng.opacity = {op};
    return "neutral_gray_created:" + ng.name;
    """


def build_frequency_separation_jsx(
    gaussian_radius: float = 6.0,
    group_name: str = "ZS_FreqSep",
) -> str:
    """一键建立标准 8/16-bit 高低频磨皮图层组（低频高斯模糊 + 高频 Apply Image 线性光）。"""
    rad = _clamp(gaussian_radius, 1.0, 25.0)
    return f"""
    var doc = app.activeDocument;
    // 1. 盖印可见图层作为低频层
    var lowLayer = doc.artLayers.add();
    lowLayer.name = "Low_Frequency";
    var dMerge = new ActionDescriptor();
    dMerge.putBoolean(charIDToTypeID("Dplc"), true);
    executeAction(charIDToTypeID("MrgV"), dMerge, DialogModes.NO);
    lowLayer = doc.activeLayer;
    lowLayer.name = "Low_Frequency";

    // 2. 复制一份作为高频层
    var highLayer = lowLayer.duplicate();
    highLayer.name = "High_Frequency";

    // 3. 低频层执行高斯模糊
    doc.activeLayer = lowLayer;
    lowLayer.applyGaussianBlur({rad});

    // 4. 高频层执行应用图像 (Apply Image: Subtract Low_Frequency, scale=2, offset=128)
    doc.activeLayer = highLayer;
    var dApp = new ActionDescriptor();
    var dWith = new ActionDescriptor();
    var rLow = new ActionReference();
    rLow.putEnumerated(charIDToTypeID("Chnl"), charIDToTypeID("Chnl"), charIDToTypeID("RGB "));
    rLow.putName(charIDToTypeID("Lyr "), "Low_Frequency");
    dWith.putReference(charIDToTypeID("T   "), rLow);
    dWith.putEnumerated(charIDToTypeID("Clcl"), charIDToTypeID("Clcn"), charIDToTypeID("Sbtr"));
    dWith.putDouble(charIDToTypeID("Scl "), 2.0);
    dWith.putInteger(charIDToTypeID("Ofst"), 128);
    dApp.putObject(charIDToTypeID("With"), charIDToTypeID("Clcl"), dWith);
    executeAction(charIDToTypeID("AppI"), dApp, DialogModes.NO);
    highLayer.blendMode = BlendMode.LINEARLIGHT;

    // 5. 编组整理
    var grp = doc.layerSets.add();
    grp.name = {_js_str(group_name)};
    lowLayer.move(grp, ElementPlacement.INSIDE);
    highLayer.move(grp, ElementPlacement.INSIDE);
    return "freq_sep_created:radius={rad}";
    """


def build_levels_layer_jsx(
    black_point: int = 0,
    gamma: float = 1.0,
    white_point: int = 255,
    output_black: int = 0,
    output_white: int = 255,
    layer_name: str = "ZS_Levels",
) -> str:
    """新建非破坏性「色阶 (Levels)」调整图层（带安全范围钳位）。"""
    bp = int(_clamp(black_point, 0, 120))
    wp = int(_clamp(white_point, max(bp + 20, 135), 255))
    gm = _clamp(gamma, 0.5, 2.0)
    ob = int(_clamp(output_black, 0, 80))
    ow = int(_clamp(output_white, max(ob + 40, 175), 255))
    return f"""
    var d1 = new ActionDescriptor();
    var r1 = new ActionReference();
    r1.putClass(charIDToTypeID("AdjL"));
    d1.putReference(charIDToTypeID("null"), r1);
    var d2 = new ActionDescriptor();
    var d3 = new ActionDescriptor();
    d3.putEnumerated(stringIDToTypeID("presetKind"), stringIDToTypeID("presetKindType"), stringIDToTypeID("presetKindCustom"));
    var adjList = new ActionList();
    var adjDesc = new ActionDescriptor();
    var chRef = new ActionReference();
    chRef.putEnumerated(charIDToTypeID("Chnl"), charIDToTypeID("Chnl"), charIDToTypeID("Cmps"));
    adjDesc.putReference(charIDToTypeID("Chnl"), chRef);
    var inList = new ActionList();
    inList.putInteger({bp});
    inList.putInteger({wp});
    adjDesc.putList(charIDToTypeID("Inpt"), inList);
    adjDesc.putDouble(charIDToTypeID("Gmm "), {gm});
    var outList = new ActionList();
    outList.putInteger({ob});
    outList.putInteger({ow});
    adjDesc.putList(charIDToTypeID("Otpt"), outList);
    adjList.putObject(charIDToTypeID("LvlA"), adjDesc);
    d3.putList(charIDToTypeID("Adjs"), adjList);
    d2.putObject(charIDToTypeID("Type"), charIDToTypeID("Lvls"), d3);
    d1.putObject(charIDToTypeID("Usng"), charIDToTypeID("AdjL"), d2);
    executeAction(charIDToTypeID("Mk  "), d1, DialogModes.NO);
    try {{ app.activeDocument.activeLayer.name = {_js_str(layer_name)}; }} catch (e) {{}}
    return "levels_added";
    """


def build_select_color_range_jsx(
    preset: str = "skinTones",
    fuzziness: int = 40,
    target_color_rgb: Optional[Sequence[int]] = None,
) -> str:
    """执行 Photoshop「选择 -> 色彩范围 (Color Range)」，支持肤色/高光/中间调/阴影或自定义 RGB。"""
    fuzz = int(_clamp(fuzziness, 0, 200))
    if target_color_rgb and len(target_color_rgb) == 3:
        r = int(_clamp(target_color_rgb[0], 0, 255))
        g = int(_clamp(target_color_rgb[1], 0, 255))
        b = int(_clamp(target_color_rgb[2], 0, 255))
        return f"""
        var d = new ActionDescriptor();
        d.putInteger(charIDToTypeID("Fzns"), {fuzz});
        var minC = new ActionDescriptor();
        minC.putDouble(charIDToTypeID("Rd  "), {r});
        minC.putDouble(charIDToTypeID("Grn "), {g});
        minC.putDouble(charIDToTypeID("Bl  "), {b});
        d.putObject(charIDToTypeID("Mnm "), charIDToTypeID("RGBC"), minC);
        var maxC = new ActionDescriptor();
        maxC.putDouble(charIDToTypeID("Rd  "), {r});
        maxC.putDouble(charIDToTypeID("Grn "), {g});
        maxC.putDouble(charIDToTypeID("Bl  "), {b});
        d.putObject(charIDToTypeID("Mxm "), charIDToTypeID("RGBC"), maxC);
        d.putInteger(stringIDToTypeID("colorModel"), 0);
        executeAction(charIDToTypeID("ClrR"), d, DialogModes.NO);
        return "color_range_rgb:{r},{g},{b}";
        """

    preset_map = {
        "skintones": "skinTones",
        "highlights": "highlights",
        "midtones": "midtones",
        "shadows": "shadows",
        "reds": "reds",
        "yellows": "yellows",
        "greens": "greens",
        "cyans": "cyans",
        "blues": "blues",
        "magentas": "magentas",
    }
    p_key = preset_map.get(str(preset or "skintones").strip().lower(), "skinTones")
    return f"""
    var d = new ActionDescriptor();
    d.putEnumerated(charIDToTypeID("Clrs"), charIDToTypeID("Clrs"), stringIDToTypeID({_js_str(p_key)}));
    d.putInteger(charIDToTypeID("Fzns"), {fuzz});
    executeAction(charIDToTypeID("ClrR"), d, DialogModes.NO);
    return "color_range_preset:{p_key}";
    """


def build_modify_selection_jsx(
    feather_px: float = 0.0,
    contract_px: int = 0,
    expand_px: int = 0,
    invert: bool = False,
) -> str:
    """对当前选区执行羽化、收缩、扩展或反选。"""
    f_px = _clamp(feather_px, 0.0, 250.0)
    c_px = int(_clamp(contract_px, 0, 200))
    e_px = int(_clamp(expand_px, 0, 200))
    lines = ["var sel = app.activeDocument.selection;"]
    if c_px > 0:
        lines.append(f"sel.contract(UnitValue({c_px}, 'px'));")
    if e_px > 0:
        lines.append(f"sel.expand(UnitValue({e_px}, 'px'));")
    if f_px > 0:
        lines.append(f"sel.feather(UnitValue({f_px}, 'px'));")
    if invert:
        lines.append("sel.invert();")
    lines.append('return "selection_modified";')
    return "\n".join(lines)


def build_add_layer_mask_jsx(invert: bool = False) -> str:
    """将当前选区转为激活图层的蒙版（支持显示选区或隐藏选区）。"""
    mode = "HdSl" if invert else "RvlS"
    return f"""
    var d = new ActionDescriptor();
    d.putClass(charIDToTypeID("Nw  "), charIDToTypeID("Chnl"));
    var r = new ActionReference();
    r.putEnumerated(charIDToTypeID("Chnl"), charIDToTypeID("Chnl"), charIDToTypeID("Msk "));
    d.putReference(charIDToTypeID("At  "), r);
    d.putEnumerated(charIDToTypeID("Usng"), charIDToTypeID("UsrM"), charIDToTypeID("{mode}"));
    executeAction(charIDToTypeID("Mk  "), d, DialogModes.NO);
    return "layer_mask_added:{mode}";
    """


def build_set_layer_blend_opacity_jsx(
    blend_mode: str = "",
    opacity: Optional[float] = None,
    fill_opacity: Optional[float] = None,
    layer_name: str = "",
) -> str:
    """修改指定图层（或当前激活图层）的混合模式、不透明度与填充度。"""
    lines: List[str] = ["var doc = app.activeDocument;", "var lyr = doc.activeLayer;"]
    if layer_name.strip():
        lines.append(f"try {{ lyr = doc.artLayers.getByName({_js_str(layer_name.strip())}); doc.activeLayer = lyr; }} catch (e) {{}}")
    if blend_mode.strip():
        bm = BLEND_MODES_MAP.get(blend_mode.strip().lower())
        if bm:
            lines.append(f"lyr.blendMode = {bm};")
    if opacity is not None:
        lines.append(f"lyr.opacity = {_clamp(opacity, 0.0, 100.0)};")
    if fill_opacity is not None:
        lines.append(f"lyr.fillOpacity = {_clamp(fill_opacity, 0.0, 100.0)};")
    lines.append('return "layer_style_updated:" + lyr.name;')
    return "\n".join(lines)


def build_convert_smart_object_jsx(layer_name: str = "") -> str:
    """将目标图层转为智能对象（后续滤镜/液化/ACR 均非破坏性可二次编辑）。"""
    prefix = ""
    if layer_name.strip():
        prefix = f"try {{ app.activeDocument.activeLayer = app.activeDocument.artLayers.getByName({_js_str(layer_name.strip())}); }} catch (e) {{}}"
    return f"""
    {prefix}
    executeAction(stringIDToTypeID("newPlacedLayer"), new ActionDescriptor(), DialogModes.NO);
    return "converted_to_smart_object:" + app.activeDocument.activeLayer.name;
    """


def build_high_pass_sharpen_jsx(
    radius: float = 2.0,
    blend_mode: str = "overlay",
    opacity: float = 60.0,
    layer_name: str = "ZS_HighPass",
) -> str:
    """一键盖印可见图层 -> 转智能对象 -> 高反差保留 (High Pass) -> 叠加/柔光锐化。"""
    rad = _clamp(radius, 0.3, 15.0)
    bm = BLEND_MODES_MAP.get(str(blend_mode or "overlay").strip().lower(), "BlendMode.OVERLAY")
    op = _clamp(opacity, 0.0, 100.0)
    return f"""
    var doc = app.activeDocument;
    var hp = doc.artLayers.add();
    var dMerge = new ActionDescriptor();
    dMerge.putBoolean(charIDToTypeID("Dplc"), true);
    executeAction(charIDToTypeID("MrgV"), dMerge, DialogModes.NO);
    hp = doc.activeLayer;
    hp.name = {_js_str(layer_name)};
    executeAction(stringIDToTypeID("newPlacedLayer"), new ActionDescriptor(), DialogModes.NO);
    var dHp = new ActionDescriptor();
    dHp.putUnitDouble(charIDToTypeID("Rds "), charIDToTypeID("#Pxl"), {rad});
    executeAction(charIDToTypeID("HghP"), dHp, DialogModes.NO);
    doc.activeLayer.blendMode = {bm};
    doc.activeLayer.opacity = {op};
    return "high_pass_applied:radius={rad}";
    """


def build_select_subject_jsx() -> str:
    """一键调用 Photoshop 原生「选择主体 (Select Subject)」。"""
    return """
    var d = new ActionDescriptor();
    d.putBoolean(stringIDToTypeID("sampleAllLayers"), false);
    executeAction(stringIDToTypeID("autoCutout"), d, DialogModes.NO);
    return "subject_selected";
    """


def build_liquify_jsx(
    face_slim: int = 0,
    eye_size: int = 0,
    jawline: int = 0,
    convert_smart_first: bool = True,
) -> str:
    """非破坏性人脸微调液化（先转智能对象，再应用安全范围钳位后的 Face-Aware Liquify）。"""
    slim = int(_clamp(face_slim, -40, 40))
    eye = int(_clamp(eye_size, -30, 30))
    jaw = int(_clamp(jawline, -40, 40))
    smart_step = (
        'executeAction(stringIDToTypeID("newPlacedLayer"), new ActionDescriptor(), DialogModes.NO);'
        if convert_smart_first
        else ""
    )
    return f"""
    {smart_step}
    var d = new ActionDescriptor();
    var faceDesc = new ActionDescriptor();
    faceDesc.putInteger(stringIDToTypeID("faceWidth"), {slim});
    faceDesc.putInteger(stringIDToTypeID("eyeSize"), {eye});
    faceDesc.putInteger(stringIDToTypeID("jawline"), {jaw});
    d.putObject(stringIDToTypeID("faceMesh"), stringIDToTypeID("faceMesh"), faceDesc);
    try {{
        executeAction(charIDToTypeID("LqFy"), d, DialogModes.NO);
        return "liquify_applied:slim={slim},eye={eye},jaw={jaw}";
    }} catch (lqErr) {{
        warnings.push("Liquify skipped (requires detectable face or interactive mesh): " + lqErr.message);
        return "liquify_skipped";
    }}
    """
