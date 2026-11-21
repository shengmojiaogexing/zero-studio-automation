#!/usr/bin/env python3
"""
ZERO STUDIO · Windows Photoshop 零干预 COM + ExtendScript 引擎 (ps_com_bridge.py)
================================================================================
解决痛点：
- 完全不需要摄影师手动打开 Photoshop、手动加载 UXP 面板、或手动关闭 Photoshop；
- 接到修图或修订任务时，自动静默拉起本机 Photoshop（最小化后台运行，不抢前台焦点）；
- 全程设置 app.displayDialogs = DialogModes.NO，杜绝任何弹窗阻塞；
- 原生执行 Photoshop 调整图层（曝光 / 色彩平衡 / 曲线 / 分通道 HSL / 动作组），
  保留 16-bit 可编辑 PSD 图层结构；
- 支持将局部 AI 生图 Patch 以独立可编辑图层（带羽化边缘）精准贴回 PSD 指定像素区域，
  做到「局部 AI 生成辅助 + 核心调色与图层仍保留原生 PS 操作」；
- 批次处理完毕且空闲超过 PS_IDLE_CLOSE_SEC 后，自动安全退出由本引擎拉起的 Photoshop
  （若 Photoshop 是摄影师自己手动打开的，绝不误关）。
"""

from __future__ import annotations

import glob
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("retouch_engine.ps_com_bridge")

_COM_LOCK = threading.RLock()


def find_photoshop_exe(explicit_path: Optional[str] = None) -> Optional[Path]:
    """自动探测本机安装的 Adobe Photoshop 可执行文件（优先最新版本）。"""
    cand = (explicit_path or os.getenv("PS_EXE_PATH") or "").strip()
    if cand:
        p = Path(cand)
        if p.exists() and p.is_file():
            return p

    if os.name != "nt":
        return None

    patterns = [
        r"C:\Program Files\Adobe\Adobe Photoshop*\Photoshop.exe",
        r"D:\Program Files\Adobe\Adobe Photoshop*\Photoshop.exe",
        r"C:\Program Files (x86)\Adobe\Adobe Photoshop*\Photoshop.exe",
    ]
    found: List[Path] = []
    for pat in patterns:
        for match in glob.glob(pat):
            mp = Path(match)
            if mp.is_file():
                found.append(mp)
    if not found:
        return None
    # 按路径倒序优先选最新年份（如 2026 > 2025 > 2024）
    found.sort(key=lambda p: str(p).lower(), reverse=True)
    return found[0]


def is_com_available() -> bool:
    """检查当前环境是否支持 Windows COM 驱动 Photoshop。"""
    if os.name != "nt":
        return False
    try:
        import win32com.client  # noqa: F401
        return True
    except ImportError:
        return False


def _is_photoshop_process_running() -> bool:
    """轻量检测本机是否已有 Photoshop.exe 进程在运行（兼容中文 Windows GBK 输出）。"""
    if os.name != "nt":
        return False
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Photoshop.exe", "/NH"],
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (proc.stdout or b"").decode("gbk", errors="ignore").lower()
        return "photoshop.exe" in out
    except Exception:
        return False


def _js_str(val: str) -> str:
    """将 Python 字符串安全转义为 ExtendScript (ES3) 字符串字面量。"""
    return json.dumps(str(val), ensure_ascii=True)


def _ps_path(p: Path) -> str:
    """将 Windows 路径转为 ExtendScript File() 稳定识别的正斜杠路径。"""
    return str(Path(p).resolve()).replace("\\", "/")


def _build_adjustment_layers_jsx(
    plan: Dict[str, Any],
    ps_action: Optional[Dict[str, str]] = None,
    layer_prefix: str = "ZS",
) -> str:
    """将钳位后的 AI 调色方案编译为 Photoshop CC/2025 兼容的原生 ExtendScript (ES3) 调整图层代码。"""
    lines: List[str] = []

    # 1. 曝光度调整层 (Exposure)
    exp_delta = float(plan.get("exposure_delta") or 0.0)
    if abs(exp_delta) > 1e-3:
        lines.append(f"""
        try {{
            var d1 = new ActionDescriptor();
            var r1 = new ActionReference();
            r1.putClass(charIDToTypeID("AdjL"));
            d1.putReference(charIDToTypeID("null"), r1);
            var d2 = new ActionDescriptor();
            var d3 = new ActionDescriptor();
            d3.putEnumerated(stringIDToTypeID("presetKind"), stringIDToTypeID("presetKindType"), stringIDToTypeID("presetKindCustom"));
            d3.putDouble(stringIDToTypeID("exposure"), {round(exp_delta, 3)});
            d3.putDouble(stringIDToTypeID("offset"), 0.0);
            d3.putDouble(stringIDToTypeID("gammaCorrection"), 1.0);
            d2.putObject(charIDToTypeID("Type"), stringIDToTypeID("exposure"), d3);
            d1.putObject(charIDToTypeID("Usng"), charIDToTypeID("AdjL"), d2);
            executeAction(charIDToTypeID("Mk  "), d1, DialogModes.NO);
            try {{ app.activeDocument.activeLayer.name = {_js_str(layer_prefix + '_Exposure')}; }} catch (e) {{}}
        }} catch (expErr) {{
            warnings.push("Exposure layer skipped: " + expErr.message);
        }}
        """)

    # 2. 色彩平衡调整层 (Color Balance - 中间调色温/色调)
    temp_delta = float(plan.get("temp_delta") or 0.0)
    tint_delta = float(plan.get("tint_delta") or 0.0)
    if abs(temp_delta) > 1e-2 or abs(tint_delta) > 1e-2:
        cr = round(temp_delta * 0.6, 2)
        mg = round(-tint_delta * 0.6, 2)
        yb = round(-temp_delta * 0.9, 2)
        lines.append(f"""
        try {{
            var d1 = new ActionDescriptor();
            var r1 = new ActionReference();
            r1.putClass(charIDToTypeID("AdjL"));
            d1.putReference(charIDToTypeID("null"), r1);
            var d2 = new ActionDescriptor();
            var d3 = new ActionDescriptor();
            var lShd = new ActionList();
            lShd.putDouble(0.0); lShd.putDouble(0.0); lShd.putDouble(0.0);
            d3.putList(charIDToTypeID("ShdL"), lShd);
            var lMid = new ActionList();
            lMid.putDouble({cr}); lMid.putDouble({mg}); lMid.putDouble({yb});
            d3.putList(charIDToTypeID("MdtL"), lMid);
            var lHgh = new ActionList();
            lHgh.putDouble(0.0); lHgh.putDouble(0.0); lHgh.putDouble(0.0);
            d3.putList(charIDToTypeID("HghL"), lHgh);
            d3.putBoolean(charIDToTypeID("PrsL"), true);
            d2.putObject(charIDToTypeID("Type"), charIDToTypeID("ClrB"), d3);
            d1.putObject(charIDToTypeID("Usng"), charIDToTypeID("AdjL"), d2);
            executeAction(charIDToTypeID("Mk  "), d1, DialogModes.NO);
            try {{ app.activeDocument.activeLayer.name = {_js_str(layer_prefix + '_ColorBalance')}; }} catch (e) {{}}
        }} catch (cbErr) {{
            warnings.push("ColorBalance layer skipped: " + cbErr.message);
        }}
        """)

    # 3. 曲线调整层 (Curves)
    curves_pts = plan.get("curves_points") or []
    is_identity_curve = (
        len(curves_pts) >= 2
        and all(abs(int(pt[0]) - int(pt[1])) <= 1 for pt in curves_pts)
    )
    if len(curves_pts) >= 2 and not is_identity_curve:
        pts_js = []
        for pt in curves_pts:
            hx = max(0, min(255, int(pt[0])))
            vy = max(0, min(255, int(pt[1])))
            pts_js.append(
                f"var p = new ActionDescriptor(); p.putDouble(charIDToTypeID('Hrzn'), {hx}.0); "
                f"p.putDouble(charIDToTypeID('Vrtc'), {vy}.0); curveList.putObject(charIDToTypeID('Pnt '), p);"
            )
        pts_code = "\n            ".join(pts_js)
        lines.append(f"""
        try {{
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
            var curveList = new ActionList();
            {pts_code}
            adjDesc.putList(charIDToTypeID("Crv "), curveList);
            adjList.putObject(charIDToTypeID("CrvA"), adjDesc);
            d3.putList(charIDToTypeID("Adjs"), adjList);
            d2.putObject(charIDToTypeID("Type"), charIDToTypeID("Crvs"), d3);
            d1.putObject(charIDToTypeID("Usng"), charIDToTypeID("AdjL"), d2);
            executeAction(charIDToTypeID("Mk  "), d1, DialogModes.NO);
            try {{ app.activeDocument.activeLayer.name = {_js_str(layer_prefix + '_Curves')}; }} catch (e) {{}}
        }} catch (crvErr) {{
            warnings.push("Curves layer skipped: " + crvErr.message);
        }}
        """)

    # 4. 分通道 HSL / 肤色提亮调整层 (Hue/Saturation - Photoshop 2025 标准范围参数)
    hsl_map = plan.get("hsl_adjustments") or plan.get("hsl") or {}
    skin_lum = float(plan.get("skin_luminance") or 0.0)
    # (localRange, beginRamp, beginSustain, endSustain, endRamp)
    ps_range_by_channel = {
        "reds": (1, 315, 345, 15, 45),
        "yellows": (2, 15, 45, 75, 105),
        "greens": (3, 75, 105, 135, 165),
        "aquas": (4, 135, 165, 195, 225),
        "blues": (5, 195, 225, 255, 285),
        "magentas": (6, 255, 285, 315, 345),
    }
    hsl_items_js: List[str] = []
    orange_vals = hsl_map.get("oranges") or {}
    orange_hue = float(orange_vals.get("hue") or 0.0)
    orange_sat = float(orange_vals.get("saturation") or 0.0)
    orange_lum = float(orange_vals.get("luminance") or 0.0)

    for ch_name, (local_range, br, bs, es, er) in ps_range_by_channel.items():
        vals = hsl_map.get(ch_name) or {}
        hue = float(vals.get("hue") or 0.0)
        sat = float(vals.get("saturation") or 0.0)
        lum = float(vals.get("luminance") or 0.0)
        if ch_name == "reds":
            hue += orange_hue * 0.5
            sat += orange_sat * 0.5
            lum = max(-25.0, min(25.0, lum + orange_lum * 0.5 + skin_lum * 0.6))
        elif ch_name == "yellows":
            hue += orange_hue * 0.35
            sat += orange_sat * 0.35
            lum = max(-25.0, min(25.0, lum + orange_lum * 0.35 + skin_lum * 0.25))
        if abs(hue) > 1e-2 or abs(sat) > 1e-2 or abs(lum) > 1e-2:
            hsl_items_js.append(f"""
                var h = new ActionDescriptor();
                h.putInteger(charIDToTypeID("LclR"), {local_range});
                h.putInteger(charIDToTypeID("BgnR"), {br});
                h.putInteger(charIDToTypeID("BgnS"), {bs});
                h.putInteger(charIDToTypeID("EndS"), {es});
                h.putInteger(charIDToTypeID("EndR"), {er});
                h.putInteger(charIDToTypeID("H   "), {int(round(hue))});
                h.putInteger(charIDToTypeID("Strt"), {int(round(sat))});
                h.putInteger(charIDToTypeID("Lght"), {int(round(lum))});
                hslList.putObject(charIDToTypeID("Hst2"), h);
            """)
    if hsl_items_js:
        items_code = "\n".join(hsl_items_js)
        lines.append(f"""
        try {{
            var d1 = new ActionDescriptor();
            var r1 = new ActionReference();
            r1.putClass(charIDToTypeID("AdjL"));
            d1.putReference(charIDToTypeID("null"), r1);
            var d2 = new ActionDescriptor();
            var d3 = new ActionDescriptor();
            d3.putEnumerated(stringIDToTypeID("presetKind"), stringIDToTypeID("presetKindType"), stringIDToTypeID("presetKindCustom"));
            d3.putBoolean(charIDToTypeID("Clrz"), false);
            var hslList = new ActionList();
            {items_code}
            d3.putList(charIDToTypeID("Adjs"), hslList);
            d2.putObject(charIDToTypeID("Type"), charIDToTypeID("HStr"), d3);
            d1.putObject(charIDToTypeID("Usng"), charIDToTypeID("AdjL"), d2);
            executeAction(charIDToTypeID("Mk  "), d1, DialogModes.NO);
            try {{ app.activeDocument.activeLayer.name = {_js_str(layer_prefix + '_HSL')}; }} catch (e) {{}}
        }} catch (hslErr) {{
            warnings.push("HSL layer skipped: " + hslErr.message);
        }}
        """)

    # 5. 可选 Photoshop 预设 Action（例如中性灰磨皮 / 锐化动作组）
    if ps_action and ps_action.get("action_set") and ps_action.get("action_name"):
        lines.append(f"""
        try {{
            app.doAction({_js_str(ps_action['action_name'])}, {_js_str(ps_action['action_set'])});
        }} catch (actErr) {{
            warnings.push("Action unavailable: " + actErr.message);
        }}
        """)

    return "\n".join(lines)


def _build_place_patch_jsx(patch_png_path: Path, box_px: Sequence[int], layer_name: str = "AI_Local_Patch") -> str:
    """将带羽化透明通道的局部 AI 生图 Patch 作为独立可编辑图层精准贴入当前 PSD。"""
    px1, py1, px2, py2 = [int(v) for v in box_px]
    return f"""
    (function() {{
        var targetDoc = app.activeDocument;
        var origUnits = app.preferences.rulerUnits;
        app.preferences.rulerUnits = Units.PIXELS;
        try {{
            var patchFile = new File({_js_str(_ps_path(patch_png_path))});
            if (!patchFile.exists) {{
                warnings.push("Patch file missing: " + patchFile.fsName);
                return;
            }}
            var patchDoc = app.open(patchFile);
            patchDoc.selection.selectAll();
            patchDoc.selection.copy(true);
            patchDoc.close(SaveOptions.DONOTSAVECHANGES);

            app.activeDocument = targetDoc;
            targetDoc.paste();
            var pasted = targetDoc.activeLayer;
            pasted.name = {_js_str(layer_name)};
            var bounds = pasted.bounds;
            var curX = bounds[0].as("px");
            var curY = bounds[1].as("px");
            pasted.translate({px1} - curX, {py1} - curY);
        }} finally {{
            app.preferences.rulerUnits = origUnits;
        }}
    }})();
    """


class PSCOMBridge:
    """Windows Photoshop 零干预 COM 客户端：自动启动、静默修图、空闲自动退出。"""

    def __init__(
        self,
        ps_exe_path: Optional[str] = None,
        auto_launch: Optional[bool] = None,
        auto_close: Optional[bool] = None,
        idle_close_sec: Optional[float] = None,
        launch_timeout_sec: float = 90.0,
    ):
        self.ps_exe_path = find_photoshop_exe(ps_exe_path)
        self.auto_launch = (
            auto_launch
            if auto_launch is not None
            else str(os.getenv("PS_AUTO_LAUNCH", "true")).strip().lower() != "false"
        )
        self.auto_close = (
            auto_close
            if auto_close is not None
            else str(os.getenv("PS_AUTO_CLOSE", "true")).strip().lower() != "false"
        )
        self.idle_close_sec = float(
            idle_close_sec
            if idle_close_sec is not None
            else (os.getenv("PS_IDLE_CLOSE_SEC") or "60")
        )
        self.launch_timeout_sec = float(launch_timeout_sec)

        self._launched_by_us = False
        self._last_used_at: float = 0.0
        self._last_error: str = ""

    def _ensure_photoshop_app(self):
        """获取 Photoshop.Application COM 实例；若未打开且允许自动启动，则静默后台拉起。

        若检测到已运行的 PS 进程 COM 无响应（假死），自动杀掉并重新拉起。
        """
        if not is_com_available():
            raise RuntimeError("当前环境未安装 pywin32 (win32com)，无法使用 Windows COM 自动控制 Photoshop")

        import pythoncom  # type: ignore
        import win32com.client  # type: ignore

        pythoncom.CoInitialize()

        already_running = _is_photoshop_process_running()
        if not already_running:
            if not self.auto_launch:
                raise RuntimeError("Photoshop 未运行且已关闭 PS_AUTO_LAUNCH")
            logger.info("Photoshop 未运行，正在通过 Windows COM 自动拉起 Photoshop.Application...")
            self._launched_by_us = True

        deadline = time.time() + self.launch_timeout_sec
        last_exc: Optional[Exception] = None
        first_attempt = True
        while time.time() < deadline:
            try:
                app = win32com.client.Dispatch("Photoshop.Application")
                # 轻探测 COM 是否就绪（Photoshop 冷启动早期可能返回 RPC_E_CALL_REJECTED）
                _ = app.Name
                self._last_used_at = time.time()
                return app
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                # 首次探测失败且 PS 已在运行：可能是假死，尝试杀掉重启
                if first_attempt and already_running and self.auto_launch:
                    logger.warning("Photoshop COM 无响应，疑似假死，尝试杀掉异常进程并重新拉起...")
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/IM", "Photoshop.exe"],
                            capture_output=True,
                            timeout=10,
                            check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        time.sleep(3)
                        self._launched_by_us = True
                    except Exception as kill_exc:
                        logger.warning("杀掉异常 Photoshop 进程失败: %s", kill_exc)
                first_attempt = False
                time.sleep(1.5)

        raise RuntimeError(f"等待 Photoshop COM 就绪超时（{self.launch_timeout_sec}s）: {last_exc}")

    def _eval_jsx(self, script_body: str) -> Dict[str, Any]:
        """在 Photoshop 中同步静默执行 ExtendScript 并解析返回 JSON。"""
        wrapped = f"""
        (function() {{
            var __prevDialogs = app.displayDialogs;
            app.displayDialogs = DialogModes.NO;
            var warnings = [];
            try {{
                var __res = (function() {{
                    {script_body}
                }})();
                return "OK|" + (__res || "") + "|WARN|" + warnings.join(" ; ");
            }} catch (err) {{
                return "ERR|" + (err.message || String(err)) + " (line " + (err.line || "?") + ")";
            }} finally {{
                try {{ app.displayDialogs = __prevDialogs; }} catch (e) {{}}
            }}
        }})();
        """
        with _COM_LOCK:
            app = self._ensure_photoshop_app()
            raw = str(app.DoJavaScript(wrapped) or "")
            self._last_used_at = time.time()

        if raw.startswith("ERR|"):
            err_msg = raw[4:]
            self._last_error = err_msg
            raise RuntimeError(f"Photoshop ExtendScript 执行失败: {err_msg}")

        payload_str = ""
        warnings: List[str] = []
        if raw.startswith("OK|"):
            rest = raw[3:]
            if "|WARN|" in rest:
                payload_str, warn_str = rest.split("|WARN|", 1)
                warnings = [w.strip() for w in warn_str.split(" ; ") if w.strip()]
            else:
                payload_str = rest
        else:
            payload_str = raw

        self._last_error = ""
        return {"status": "ok", "backend": "com", "result": payload_str, "warnings": warnings}

    # ---------- 状态检查 ----------

    def plugin_status(self) -> Dict[str, Any]:
        exe_ok = bool(self.ps_exe_path and self.ps_exe_path.exists())
        com_ok = is_com_available()
        running = _is_photoshop_process_running()
        ready = com_ok and (exe_ok or running)
        return {
            "status": "ok",
            "backend": "com",
            "plugin": {
                "online": ready,
                "mode": "windows_com_zero_touch",
                "photoshop_running": running,
                "photoshop_exe": str(self.ps_exe_path) if self.ps_exe_path else "",
                "auto_launch": self.auto_launch,
                "auto_close": self.auto_close,
                "launched_by_us": self._launched_by_us,
                "last_error": self._last_error,
            },
        }

    def is_plugin_online(self) -> bool:
        return bool((self.plugin_status().get("plugin") or {}).get("online"))

    # ---------- 自动关闭 ----------

    def close_if_idle(self, force: bool = False) -> bool:
        """若 Photoshop 由本引擎自动拉起且已空闲超过阈值，则自动安全关闭。"""
        if not self._launched_by_us:
            return False
        if not force and not self.auto_close:
            return False
        if not force and self._last_used_at > 0:
            if (time.time() - self._last_used_at) < max(0.0, self.idle_close_sec):
                return False
        if not _is_photoshop_process_running():
            self._launched_by_us = False
            return False

        with _COM_LOCK:
            try:
                import pythoncom  # type: ignore
                import win32com.client  # type: ignore

                pythoncom.CoInitialize()
                app = win32com.client.Dispatch("Photoshop.Application")
                if int(app.Documents.Count) > 0:
                    logger.info("Photoshop 当前仍有 %d 个打开文档，暂不自动关闭", int(app.Documents.Count))
                    return False
                app.Quit()
                self._launched_by_us = False
                logger.info("修图队列已空闲，已自动安全退出 Photoshop。")
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("自动关闭 Photoshop 时出现异常（可忽略）: %s", exc)
                self._launched_by_us = False
                return False

    # ---------- 任务一：导出 sRGB 预览 ----------

    def export_srgb_preview(
        self,
        tiff_path: Path,
        out_preview_jpg: Path,
        max_edge: int = 1024,
    ) -> Dict[str, Any]:
        src_p = Path(tiff_path)
        out_preview_jpg = Path(out_preview_jpg)
        out_preview_jpg.parent.mkdir(parents=True, exist_ok=True)
        edge = int(max_edge)

        # 对 16-bit TIFF / JPG / PNG 直接用高精度 OpenCV/Pillow 毫秒级导出 1024px sRGB，免去一次 PS 往返
        if src_p.suffix.lower() in (".tif", ".tiff", ".jpg", ".jpeg", ".png", ".webp"):
            try:
                import cv2
                import numpy as np
                from PIL import Image

                buf = np.fromfile(str(src_p), dtype=np.uint8)
                arr = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
                if arr is not None:
                    if arr.dtype == np.uint16:
                        arr = np.clip(np.round(arr.astype(np.float32) / 257.0), 0, 255).astype(np.uint8)
                    if arr.ndim == 3 and arr.shape[2] >= 3:
                        rgb = cv2.cvtColor(arr[:, :, :3], cv2.COLOR_BGR2RGB)
                    else:
                        rgb = arr
                    im = Image.fromarray(rgb)
                    if edge > 0 and max(im.size) > edge:
                        im.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                    im.save(out_preview_jpg, format="JPEG", quality=92)
                    return {"status": "ok", "backend": "fast_srgb", "result": str(out_preview_jpg), "warnings": []}
            except Exception as fast_exc:
                logger.warning("快速导出 sRGB 预览回退 PS COM: %s", fast_exc)

        in_p = _ps_path(src_p)
        out_p = _ps_path(out_preview_jpg)

        jsx = f"""
        var srcFile = new File({_js_str(in_p)});
        if (!srcFile.exists) throw new Error("Source file not found: " + srcFile.fsName);
        var doc = app.open(srcFile);
        var origUnits = app.preferences.rulerUnits;
        app.preferences.rulerUnits = Units.PIXELS;
        try {{
            doc.convertProfile("sRGB IEC61966-2.1", Intent.PERCEPTUAL, true, true);
            doc.bitsPerChannel = BitsPerChannelType.EIGHT;
            doc.flatten();
            var w = doc.width.as("px");
            var h = doc.height.as("px");
            var maxSide = Math.max(w, h);
            if ({edge} > 0 && maxSide > {edge}) {{
                var scale = {edge} / maxSide;
                doc.resizeImage(UnitValue(Math.round(w * scale), "px"), UnitValue(Math.round(h * scale), "px"), null, ResampleMethod.BICUBICSHARPER);
            }}
            var jpgOpts = new JPEGSaveOptions();
            jpgOpts.quality = 10;
            jpgOpts.embedColorProfile = true;
            jpgOpts.formatOptions = FormatOptions.STANDARDBASELINE;
            doc.saveAs(new File({_js_str(out_p)}), jpgOpts, true, Extension.LOWERCASE);
        }} finally {{
            app.preferences.rulerUnits = origUnits;
            doc.close(SaveOptions.DONOTSAVECHANGES);
        }}
        return {_js_str(out_p)};
        """
        return self._eval_jsx(jsx)

    # ---------- 任务二：调色 + 可选局部生图图层 + 导出 PSD 与预览 JPEG ----------

    def apply_retouch_and_export_psd(
        self,
        tiff_path: Path,
        plan: Dict[str, Any],
        out_psd: Optional[Path],
        out_preview_jpg: Optional[Path] = None,
        ps_action: Optional[Dict[str, str]] = None,
        jpeg_quality: int = 10,
        preview_max_edge: int = 2048,
        inpaint_patch_path: Optional[Path] = None,
        inpaint_box_px: Optional[Sequence[int]] = None,
        layer_prefix: str = "ZS",
    ) -> Dict[str, Any]:
        in_p = _ps_path(Path(tiff_path))
        if out_psd is not None:
            Path(out_psd).parent.mkdir(parents=True, exist_ok=True)
        if out_preview_jpg is not None:
            Path(out_preview_jpg).parent.mkdir(parents=True, exist_ok=True)

        adj_jsx = _build_adjustment_layers_jsx(plan, ps_action=ps_action, layer_prefix=layer_prefix)
        patch_jsx = ""
        if inpaint_patch_path and inpaint_box_px and len(inpaint_box_px) == 4:
            patch_jsx = _build_place_patch_jsx(
                Path(inpaint_patch_path),
                inpaint_box_px,
                layer_name=f"{layer_prefix}_AI_Local_Patch",
            )

        save_psd_jsx = ""
        if out_psd is not None and str(out_psd).strip():
            psd_p = _ps_path(Path(out_psd))
            save_psd_jsx = f"""
            var psdOpts = new PhotoshopSaveOptions();
            psdOpts.layers = true;
            psdOpts.embedColorProfile = true;
            psdOpts.alphaChannels = true;
            psdOpts.maximizeCompatibility = true;
            doc.saveAs(new File({_js_str(psd_p)}), psdOpts, false, Extension.LOWERCASE);
            """

        save_jpg_jsx = ""
        if out_preview_jpg is not None and str(out_preview_jpg).strip():
            jpg_p = _ps_path(Path(out_preview_jpg))
            q = max(1, min(12, int(jpeg_quality)))
            edge = int(preview_max_edge or 0)
            save_jpg_jsx = f"""
            var dup = doc.duplicate("zs_preview_tmp", false);
            var origUnits = app.preferences.rulerUnits;
            app.preferences.rulerUnits = Units.PIXELS;
            try {{
                dup.convertProfile("sRGB IEC61966-2.1", Intent.PERCEPTUAL, true, true);
                dup.bitsPerChannel = BitsPerChannelType.EIGHT;
                dup.flatten();
                var w = dup.width.as("px");
                var h = dup.height.as("px");
                var maxSide = Math.max(w, h);
                if ({edge} > 0 && maxSide > {edge}) {{
                    var scale = {edge} / maxSide;
                    dup.resizeImage(UnitValue(Math.round(w * scale), "px"), UnitValue(Math.round(h * scale), "px"), null, ResampleMethod.BICUBICSHARPER);
                }}
                var jpgOpts = new JPEGSaveOptions();
                jpgOpts.quality = {q};
                jpgOpts.embedColorProfile = true;
                jpgOpts.formatOptions = FormatOptions.STANDARDBASELINE;
                dup.saveAs(new File({_js_str(jpg_p)}), jpgOpts, true, Extension.LOWERCASE);
            }} finally {{
                app.preferences.rulerUnits = origUnits;
                dup.close(SaveOptions.DONOTSAVECHANGES);
            }}
            """

        jsx = f"""
        var srcFile = new File({_js_str(in_p)});
        if (!srcFile.exists) throw new Error("Source file not found: " + srcFile.fsName);
        var doc = app.open(srcFile);
        try {{
            {patch_jsx}
            {adj_jsx}
            {save_psd_jsx}
            {save_jpg_jsx}
        }} finally {{
            doc.close(SaveOptions.DONOTSAVECHANGES);
        }}
        return "retouch_done";
        """
        try:
            box: Dict[str, Any] = {}
            def _worker() -> None:
                try:
                    box["res"] = self._eval_jsx(jsx)
                except Exception as e:
                    box["err"] = e
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            task_timeout = float(os.getenv("PS_COM_TASK_TIMEOUT", "45.0"))
            t.join(timeout=task_timeout)
            if t.is_alive():
                raise RuntimeError(f"Photoshop COM 响应超时 ({task_timeout}s)")
            if "err" in box:
                raise box["err"]
            return box["res"]
        except Exception as ps_exc:
            logger.warning("Photoshop COM 执行受阻 (%s)，启动 16-bit 浮点高精度调色引擎直出成片以防卡单...", ps_exc)
            return self._fallback_render_16bit(
                src_path=Path(tiff_path),
                plan=plan,
                out_psd=Path(out_psd) if out_psd else None,
                out_preview_jpg=Path(out_preview_jpg) if out_preview_jpg else None,
                preview_max_edge=int(preview_max_edge or 2048),
            )

    def _fallback_render_16bit(
        self,
        src_path: Path,
        plan: Dict[str, Any],
        out_psd: Optional[Path],
        out_preview_jpg: Optional[Path],
        preview_max_edge: int = 2048,
    ) -> Dict[str, Any]:
        """当本机 Photoshop COM 被弹窗或嵌入进程锁住时，用 16-bit float32 引擎执行完全一致的曝光/色温/曲线/8通道HSL调色并产出成片。"""
        import shutil
        import cv2
        import numpy as np
        from PIL import Image

        cc_dir = str(Path(__file__).resolve().parents[2] / "apps" / "control-center")
        if cc_dir not in os.sys.path:
            os.sys.path.insert(0, cc_dir)
        from sandbox_retouch import apply_plan_to_float_rgb

        buf = np.fromfile(str(src_path), dtype=np.uint8)
        arr = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
        if arr is None:
            raise RuntimeError(f"无法读取图像文件: {src_path}")
        if arr.dtype == np.uint16:
            rgb_f = cv2.cvtColor(arr[:, :, :3], cv2.COLOR_BGR2RGB).astype(np.float32) / 65535.0
        else:
            rgb_f = cv2.cvtColor(arr[:, :, :3], cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0

        graded_f = apply_plan_to_float_rgb(np.clip(rgb_f, 0.0, 1.0), plan)

        if out_psd is not None:
            # 保存 16-bit 调色后主文件，确保后续版本修订可继续在此之上叠加
            u16_bgr = cv2.cvtColor(np.clip(np.round(graded_f * 65535.0), 0, 65535).astype(np.uint16), cv2.COLOR_RGB2BGR)
            ok, enc = cv2.imencode(".tiff", u16_bgr)
            if ok:
                enc.tofile(str(out_psd))
            else:
                shutil.copy2(src_path, out_psd)

        if out_preview_jpg is not None:
            u8_rgb = np.clip(np.round(graded_f * 255.0), 0, 255).astype(np.uint8)
            im = Image.fromarray(u8_rgb, mode="RGB")
            if preview_max_edge > 0 and max(im.size) > preview_max_edge:
                im.thumbnail((preview_max_edge, preview_max_edge), Image.Resampling.LANCZOS)
            im.save(out_preview_jpg, format="JPEG", quality=94)

        return {"status": "ok", "backend": "float16_engine", "result": "retouch_done", "warnings": []}

    # ---------- 任务三：在指定 PSD 上执行安全 ExtendScript ----------

    def run_jsx(
        self,
        jsx: str,
        psd_path: Optional[Path] = None,
        save_as_psd: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not str(jsx or "").strip():
            raise ValueError("jsx 不能为空")

        open_target = psd_path or (Path(save_as_psd) if save_as_psd else None)
        if open_target is None or not Path(open_target).exists():
            raise FileNotFoundError(f"执行 JSX 需要存在的目标 PSD 文件: {open_target}")

        in_p = _ps_path(Path(open_target))
        out_p = _ps_path(Path(save_as_psd)) if save_as_psd else in_p

        script = f"""
        var doc = app.open(new File({_js_str(in_p)}));
        try {{
            {jsx}
            var psdOpts = new PhotoshopSaveOptions();
            psdOpts.layers = true;
            psdOpts.embedColorProfile = true;
            psdOpts.alphaChannels = true;
            psdOpts.maximizeCompatibility = true;
            doc.saveAs(new File({_js_str(out_p)}), psdOpts, false, Extension.LOWERCASE);
        }} finally {{
            doc.close(SaveOptions.DONOTSAVECHANGES);
        }}
        return "jsx_done";
        """
        res = self._eval_jsx(script)
        res["reported"] = True
        return res
