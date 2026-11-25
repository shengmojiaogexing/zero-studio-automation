#!/usr/bin/env python3
"""
Windows Photoshop 队列桥接 Python 客户端与局部 Patch 生图叠回模块：
- 通过 booking-site 的内部队列接口 (POST /api/internal/ps-bridge/run) 驱动 Photoshop；
  PS 插件主动轮询取任务，因此**插件不需要监听任何端口**（UXP 平台限制）；
- 阶段 A：打开 16-bit TIFF 静默导出纯画面 1024px sRGB JPEG（杜绝带 UI 与色域偏差的系统截屏）；
- 阶段 B：将安全钳位后的 AI JSON 方案翻译为确定性调整层并导出 PSD + 预览 JPEG；
- 阶段 C：当 needs_local_inpaint=true 时，仅裁切局部蒙版 Patch 调用 IMAGE_GEN_API_URL 并羽化叠回原图。
- 扩展口：可执行任意 ExtendScript（run_jsx），用于插件未封装的能力。
"""

from __future__ import annotations

import base64
import io
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

import cv2
import httpx
import numpy as np
from dotenv import load_dotenv
from PIL import Image

ENGINE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = ENGINE_DIR.parents[1]
load_dotenv(WORKSPACE_ROOT / ".env", override=False)

logger = logging.getLogger("retouch_engine.ps_bridge")


def build_batchplay_descriptors(
    plan: Dict[str, Any],
    ps_action: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """将 Gemini 安全钳位后的 JSON 方案转换为确定性 Photoshop UXP action.batchPlay 描述符序列。"""
    descriptors: List[Dict[str, Any]] = []

    # 1. Exposure / Gamma 微调调整层
    exp_delta = float(plan.get("exposure_delta") or 0.0)
    if abs(exp_delta) > 1e-3:
        descriptors.append(
            {
                "_obj": "make",
                "_target": [{"_ref": "adjustmentLayer"}],
                "using": {
                    "_obj": "adjustmentLayer",
                    "type": {
                        "_obj": "exposure",
                        "exposure": round(exp_delta, 3),
                        "offset": 0.0,
                        "gammaCorrection": 1.0,
                    },
                },
            }
        )

    # 2. ColorBalance 色温 / 色调偏移（中间调）
    temp_delta = float(plan.get("temp_delta") or 0.0)
    tint_delta = float(plan.get("tint_delta") or 0.0)
    if abs(temp_delta) > 1e-2 or abs(tint_delta) > 1e-2:
        descriptors.append(
            {
                "_obj": "make",
                "_target": [{"_ref": "adjustmentLayer"}],
                "using": {
                    "_obj": "adjustmentLayer",
                    "type": {
                        "_obj": "colorBalance",
                        "midtoneLevels": [
                            round(temp_delta * 0.6, 2),       # Cyan - Red
                            round(-tint_delta * 0.6, 2),      # Magenta - Green
                            round(-temp_delta * 0.9, 2),      # Yellow - Blue
                        ],
                        "preserveLuminosity": True,
                    },
                },
            }
        )

    # 3. Curves 曲线调整层（含肤色明度柔化参考）
    curves_pts = plan.get("curves_points") or []
    if len(curves_pts) >= 2:
        descriptors.append(
            {
                "_obj": "make",
                "_target": [{"_ref": "adjustmentLayer"}],
                "using": {
                    "_obj": "adjustmentLayer",
                    "type": {
                        "_obj": "curves",
                        "adjustment": [
                            {
                                "_obj": "curvesAdjustment",
                                "channel": {"_ref": "channel", "_enum": "channel", "_value": "composite"},
                                "curve": [
                                    {"_obj": "paint", "horizontal": int(pt[0]), "vertical": int(pt[1])}
                                    for pt in curves_pts
                                ],
                            }
                        ],
                    },
                },
            }
        )

    # 4. Hue/Saturation 分通道 HSL 调整 + skin_luminance（作用于 yellows/reds 肤色范围）
    hsl_map = plan.get("hsl_adjustments") or {}
    skin_lum = float(plan.get("skin_luminance") or 0.0)
    ps_range_by_channel = {
        "reds": 1,
        "yellows": 2,
        "greens": 3,
        "aquas": 4,
        "blues": 5,
        "magentas": 6,
    }
    hsl_adjustments_list: List[Dict[str, Any]] = []
    for ch_name, local_range in ps_range_by_channel.items():
        vals = hsl_map.get(ch_name) or {}
        hue = float(vals.get("hue") or 0.0)
        sat = float(vals.get("saturation") or 0.0)
        lum = float(vals.get("luminance") or 0.0)
        if ch_name == "reds" and abs(skin_lum) > 1e-2:
            lum = max(-25.0, min(25.0, lum + skin_lum * 0.5))
        if abs(hue) > 1e-2 or abs(sat) > 1e-2 or abs(lum) > 1e-2:
            hsl_adjustments_list.append(
                {
                    "_obj": "hueSatAdjustmentV2",
                    "localRange": local_range,
                    "hue": round(hue, 2),
                    "saturation": round(sat, 2),
                    "lightness": round(lum, 2),
                }
            )
    if hsl_adjustments_list:
        descriptors.append(
            {
                "_obj": "make",
                "_target": [{"_ref": "adjustmentLayer"}],
                "using": {
                    "_obj": "adjustmentLayer",
                    "type": {
                        "_obj": "hueSaturation",
                        "adjustment": hsl_adjustments_list,
                    },
                },
            }
        )

    # 5. 可选 Photoshop 预设 Action（如中性灰磨皮/锐化动作组）
    if ps_action and ps_action.get("action_set") and ps_action.get("action_name"):
        descriptors.append(
            {
                "_obj": "play",
                "_target": [
                    {"_ref": "action", "_name": ps_action["action_name"]},
                    {"_ref": "actionSet", "_name": ps_action["action_set"]},
                ],
            }
        )

    return descriptors


class PSUXPBridgeClient:
    """与 Photoshop 通信的客户端（队列轮询模式）。

    架构说明（重要）
    ----------------
    Adobe UXP 平台**不允许插件监听本地端口**，所以旧版「直连 127.0.0.1:8765」
    的做法永远连不上。当前链路的真实形态是：

        retouch-engine ──POST /api/internal/ps-bridge/run──▶ booking-site（队列）
                                                                ▲
                                          PS 插件 ──轮询 GET───┘ 取走任务并执行

    本客户端因此改为「向 booking-site 的内部接口投递任务并等待结果」。
    booking-site 会先检查插件是否在线，离线时立刻返回明确错误，
    不会让调用方空等 15 分钟。

    PS 是否打开由插件状态决定：PS 没开 → 任务无法执行 → 明确告知；
    PS 打开且面板加载后，插件每秒轮询，任务会被自动取走执行。
    """

    def __init__(
        self,
        bridge_url: Optional[str] = None,
        bridge_token: Optional[str] = None,
        timeout: float = 180.0,
        max_busy_retries: int = 2,
        retry_base_delay: float = 1.0,
        backend: Optional[str] = None,
    ):
        # bridge_url 现在指向 booking-site 的内部接口基址
        self._explicit_bridge_url = bridge_url is not None
        self.bridge_url = (
            bridge_url
            or os.getenv("PS_BRIDGE_QUEUE_URL")
            or os.getenv("BOOKING_API_URL")
            or "http://127.0.0.1:3000"
        ).rstrip("/")
        self.bridge_token = (
            bridge_token
            if bridge_token is not None
            else (os.getenv("INTERNAL_API_KEY") or os.getenv("PS_UXP_BRIDGE_TOKEN") or "")
        ).strip()
        self.timeout = float(timeout)
        self.max_busy_retries = int(max_busy_retries)
        self.retry_base_delay = float(retry_base_delay)

        # 后端选择：
        # - 调用方显式传入 bridge_url（如单元测试 mock 服务）时固定走 uxp 队列；
        # - 否则按 PS_BACKEND (auto | com | uxp) 决定；Windows 本机 auto 默认优先零干预 COM。
        self._com_bridge = None
        requested = (backend or os.getenv("PS_BACKEND") or "auto").strip().lower()
        if self._explicit_bridge_url and not backend:
            self.backend = "uxp"
        elif requested == "uxp":
            self.backend = "uxp"
        else:
            from ps_com_bridge import PSCOMBridge, find_photoshop_exe, is_com_available

            if is_com_available() and (requested == "com" or find_photoshop_exe() is not None):
                self._com_bridge = PSCOMBridge()
                self.backend = "com"
                logger.info(
                    "Photoshop 驱动模式：Windows COM 零干预自动启停 (%s)",
                    self._com_bridge.ps_exe_path or "COM Auto-Dispatch",
                )
            elif requested == "com":
                raise RuntimeError("PS_BACKEND=com 但当前环境未安装 pywin32 或非 Windows 系统")
            else:
                self.backend = "uxp"

    # ---------- 基础请求 ----------

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.bridge_token,
        }

    def _run_task(
        self,
        task_type: str,
        payload: Dict[str, Any],
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """投递一个 PS 任务并等待 Photoshop 执行完返回结果。"""
        url = f"{self.bridge_url}/api/internal/ps-bridge/run"
        body = {
            "type": task_type,
            "payload": payload,
            "timeout_ms": int(timeout_ms if timeout_ms is not None else self.timeout * 1000),
        }
        last_err: Optional[str] = None

        with httpx.Client(timeout=self.timeout + 10.0, trust_env=False) as client:
            for attempt in range(1, self.max_busy_retries + 2):
                try:
                    resp = client.post(url, headers=self._headers(), json=body)
                except httpx.HTTPError as exc:
                    raise RuntimeError(
                        f"无法连接 booking-site（{url}）：{exc}。"
                        "请确认预约网站服务已启动。"
                    ) from exc

                try:
                    data: Dict[str, Any] = resp.json()
                except Exception:
                    data = {"raw": resp.text}

                if resp.status_code == 200:
                    return data

                message = str(data.get("message") or data.get("detail") or resp.text)

                # 504 = 等 PS 超时；最常见原因是 PS 没开或插件面板没加载
                if resp.status_code == 504:
                    raise RuntimeError(
                        "等待 Photoshop 执行超时。请确认：\n"
                        "  1) Photoshop 已打开\n"
                        "  2) Photoshop UXP / MCP 插件面板已加载并已连接\n"
                        f"  服务端返回：{message}"
                    )

                # 400/401 属于参数或鉴权问题，重试无意义
                if resp.status_code in (400, 401, 403, 404):
                    raise RuntimeError(f"PS 任务被拒绝 ({task_type}, HTTP {resp.status_code}): {message}")

                if attempt <= self.max_busy_retries:
                    delay = self.retry_base_delay * attempt
                    logger.warning(
                        "PS 任务投递失败 (%s, HTTP %s)，第 %d/%d 次重试 (%.1fs)...",
                        task_type, resp.status_code, attempt, self.max_busy_retries, delay,
                    )
                    time.sleep(delay)
                    last_err = f"HTTP {resp.status_code}: {message}"
                    continue

                raise RuntimeError(f"PS 任务失败 ({task_type}, HTTP {resp.status_code}): {message}")

        raise RuntimeError(f"PS 任务超过最大重试次数: {last_err}")

    # ---------- 插件健康检查 ----------

    def plugin_status(self) -> Dict[str, Any]:
        """查询 PS 插件或 Windows COM 自动引擎是否就绪（不投递任务，只读状态）。"""
        if self._com_bridge is not None:
            return self._com_bridge.plugin_status()
        url = f"{self.bridge_url}/api/ps-bridge/status"
        try:
            with httpx.Client(timeout=8.0, trust_env=False) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": str(exc), "plugin": {"online": False}}

    def close_if_idle(self, force: bool = False) -> bool:
        """若处于 Windows COM 模式且 Photoshop 为本引擎拉起并已空闲，自动关闭 Photoshop。"""
        if self._com_bridge is not None:
            return self._com_bridge.close_if_idle(force=force)
        return False

    def is_plugin_online(self) -> bool:
        st = self.plugin_status()
        return bool((st.get("plugin") or {}).get("online"))

    def wait_for_plugin(self, timeout_sec: float = 60.0, poll_sec: float = 2.0) -> bool:
        """等待 PS 插件上线（用于批量任务开始前确认 PS 已就绪）。"""
        deadline = time.time() + max(1.0, float(timeout_sec))
        while time.time() < deadline:
            if self.is_plugin_online():
                return True
            time.sleep(poll_sec)
        return False

    # ---------- 任务一：导出 sRGB 预览 ----------

    def export_srgb_preview(
        self,
        tiff_path: Path,
        out_preview_jpg: Path,
        max_edge: int = 1024,
    ) -> Dict[str, Any]:
        """让 Photoshop 打开 TIFF 并静默导出纯画面 sRGB JPEG。"""
        out_preview_jpg = Path(out_preview_jpg)
        out_preview_jpg.parent.mkdir(parents=True, exist_ok=True)
        if self._com_bridge is not None:
            return self._com_bridge.export_srgb_preview(
                tiff_path=Path(tiff_path),
                out_preview_jpg=out_preview_jpg,
                max_edge=int(max_edge),
            )
        return self._run_task(
            "export_srgb",
            {
                "tiff_path": str(Path(tiff_path).resolve()),
                "output_jpg_path": str(out_preview_jpg.resolve()),
                "max_edge": int(max_edge),
            },
        )

    # ---------- 任务二：调色并导出 PSD（主交付链路） ----------

    def apply_retouch_and_export_psd(
        self,
        tiff_path: Path,
        plan: Dict[str, Any],
        out_psd: Path,
        out_preview_jpg: Optional[Path] = None,
        ps_action: Optional[Dict[str, str]] = None,
        jpeg_quality: int = 10,
        preview_max_edge: int = 2048,
        inpaint_patch_path: Optional[Path] = None,
        inpaint_box_px: Optional[Sequence[int]] = None,
        layer_prefix: str = "ZS",
    ) -> Dict[str, Any]:
        """在 Photoshop 内应用调色并导出 **PSD 工程文件**。

        PSD 保留可编辑的调整图层，后续用户反馈可以在此之上继续叠加修改，
        不必从原片重做。
        """
        out_psd = Path(out_psd)
        out_psd.parent.mkdir(parents=True, exist_ok=True)
        if out_preview_jpg is not None:
            Path(out_preview_jpg).parent.mkdir(parents=True, exist_ok=True)

        if self._com_bridge is not None:
            return self._com_bridge.apply_retouch_and_export_psd(
                tiff_path=Path(tiff_path),
                plan=plan,
                out_psd=out_psd,
                out_preview_jpg=Path(out_preview_jpg) if out_preview_jpg is not None else None,
                ps_action=ps_action,
                jpeg_quality=int(jpeg_quality),
                preview_max_edge=int(preview_max_edge),
                inpaint_patch_path=Path(inpaint_patch_path) if inpaint_patch_path is not None else None,
                inpaint_box_px=inpaint_box_px,
                layer_prefix=layer_prefix,
            )

        # ps_action（录制动作）与 batch_play 目前由插件侧按 plan 生成调整层；
        # 需要额外驱动动作时通过 jsx 任务补足。
        payload: Dict[str, Any] = {
            "tiff_path": str(Path(tiff_path).resolve()),
            "output_psd_path": str(out_psd.resolve()),
            "plan": plan,
            "jpeg_quality": int(jpeg_quality),
            "preview_max_edge": int(preview_max_edge),
        }
        if ps_action:
            payload["ps_action"] = ps_action
        if out_preview_jpg is not None:
            payload["output_preview_jpg_path"] = str(Path(out_preview_jpg).resolve())

        return self._run_task("retouch", payload)

    def apply_retouch_and_export(
        self,
        tiff_path: Path,
        plan: Dict[str, Any],
        out_final_jpg: Path,
        ps_action: Optional[Dict[str, str]] = None,
        jpeg_quality: int = 11,
    ) -> Dict[str, Any]:
        """调色并只导出 JPEG（兼容旧调用方）。"""
        out_final_jpg = Path(out_final_jpg)
        out_final_jpg.parent.mkdir(parents=True, exist_ok=True)
        if self._com_bridge is not None:
            return self._com_bridge.apply_retouch_and_export_psd(
                tiff_path=Path(tiff_path),
                plan=plan,
                out_psd=None,
                out_preview_jpg=out_final_jpg,
                ps_action=ps_action,
                jpeg_quality=int(jpeg_quality),
                preview_max_edge=0,
            )
        payload: Dict[str, Any] = {
            "tiff_path": str(Path(tiff_path).resolve()),
            "output_psd_path": "",           # 只要 JPG 时不存 PSD
            "output_preview_jpg_path": str(out_final_jpg.resolve()),
            "plan": plan,
            "jpeg_quality": int(jpeg_quality),
        }
        if ps_action:
            payload["ps_action"] = ps_action
        return self._run_task("retouch", payload)

    # ---------- 任务三：抓像素（不落盘） ----------

    def capture_region(
        self,
        document_id: Optional[int] = None,
        selection: Optional[Dict[str, int]] = None,
        max_edge: int = 0,
        fmt: str = "png",
        quality: float = 0.92,
    ) -> Dict[str, Any]:
        """抓取 PS 当前文档/指定区域，返回 base64 图像（可用于 AI 看图判断）。"""
        payload: Dict[str, Any] = {"format": fmt, "quality": float(quality)}
        if document_id is not None:
            payload["document_id"] = int(document_id)
        if selection:
            payload["selection"] = selection
        if max_edge:
            payload["max_edge"] = int(max_edge)
        return self._run_task("capture_region", payload)

    # ---------- 任务四：贴图回选区 ----------

    def place_image(
        self,
        selection: Dict[str, int],
        image_base64: Optional[str] = None,
        image_path: Optional[str] = None,
        document_id: Optional[int] = None,
        anti_mode: int = 0,
        layer_type: str = "smartObject",
        group_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把一张图贴回 PS 文档的指定选区（智能对象优先，保留内部原生分辨率）。"""
        payload: Dict[str, Any] = {
            "selection": selection,
            "anti_mode": int(anti_mode),
            "layer_type": layer_type,
        }
        if image_base64:
            payload["image_base64"] = image_base64
        if image_path:
            payload["image_path"] = image_path
        if document_id is not None:
            payload["document_id"] = int(document_id)
        if group_name:
            payload["group_name"] = group_name
        return self._run_task("place_image", payload)

    # ---------- 任务五：执行任意 ExtendScript ----------

    def run_jsx(
        self,
        jsx: str,
        document_id: Optional[int] = None,
        save_as_psd: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        psd_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """在 Photoshop 里执行任意 ExtendScript（ES3 语法），返回脚本的返回值。

        这是扩展口：插件没封装的能力（滤镜、通道、动作、批处理）都能通过它实现。
        """
        if not str(jsx or "").strip():
            raise ValueError("jsx 不能为空")
        if self._com_bridge is not None:
            return self._com_bridge.run_jsx(
                jsx=jsx,
                psd_path=Path(psd_path) if psd_path is not None else None,
                save_as_psd=save_as_psd,
            )
        payload: Dict[str, Any] = {"jsx": jsx}
        if document_id is not None:
            payload["document_id"] = int(document_id)
        if save_as_psd:
            payload["save_as_psd"] = str(save_as_psd)
        return self._run_task("jsx", payload, timeout_ms=timeout_ms)


def generate_local_inpaint_patch(
    source_image_path: Path,
    region_norm: Sequence[float],
    prompt: str,
    out_patch_png: Path,
    image_gen_api_url: Optional[str] = None,
    image_gen_api_key: Optional[str] = None,
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """
    生成带羽化透明边缘的局部修补 Patch PNG，供 Photoshop 作为独立可编辑图层精准贴入 PSD：
    - 仅裁切 region_norm ([x1, y1, x2, y2]) 对应的局部图像块发给 IMAGE_GEN_API_URL；
    - 严禁整图重绘，保证核心人像、服设与后续 PS 调色图层完全可控。
    """
    api_url = (image_gen_api_url or os.getenv("IMAGE_GEN_API_URL") or "").strip()
    api_key = (image_gen_api_key if image_gen_api_key is not None else os.getenv("IMAGE_GEN_API_KEY", "")).strip()
    if not api_url:
        return {"applied": False, "reason": "IMAGE_GEN_API_URL_NOT_CONFIGURED"}

    buf = np.fromfile(str(source_image_path), dtype=np.uint8)
    full_bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if full_bgr is None:
        raise IOError(f"无法读取待局部修补源图: {source_image_path}")

    h, w = full_bgr.shape[:2]
    x1, y1, x2, y2 = [max(0.0, min(1.0, float(v))) for v in region_norm]
    px1, py1 = max(0, int(round(x1 * w))), max(0, int(round(y1 * h)))
    px2, py2 = min(w, int(round(x2 * w))), min(h, int(round(y2 * h)))
    pw, ph = px2 - px1, py2 - py1
    if pw < 8 or ph < 8:
        return {"applied": False, "reason": "PATCH_TOO_SMALL"}

    patch_bgr = full_bgr[py1:py2, px1:px2]
    patch_rgb = cv2.cvtColor(patch_bgr, cv2.COLOR_BGR2RGB)
    patch_io = io.BytesIO()
    Image.fromarray(patch_rgb).save(patch_io, format="PNG")
    patch_b64 = base64.b64encode(patch_io.getvalue()).decode("ascii")

    parsed = urlparse(api_url)
    use_trust_env = parsed.hostname not in ("127.0.0.1", "localhost", "::1")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    with httpx.Client(timeout=timeout, trust_env=use_trust_env) as client:
        resp = client.post(
            api_url,
            headers=headers,
            json={
                "patch_image_base64": patch_b64,
                "patch_size": [pw, ph],
                "region_norm": [x1, y1, x2, y2],
                "prompt": prompt,
                "mode": "local_patch_only",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    edited_b64 = data.get("edited_patch_base64") or data.get("image_base64")
    if not edited_b64:
        raise RuntimeError(f"局部生图接口未返回 edited_patch_base64: {data}")

    edited_pil = Image.open(io.BytesIO(base64.b64decode(edited_b64))).convert("RGB")
    if edited_pil.size != (pw, ph):
        edited_pil = edited_pil.resize((pw, ph), Image.Resampling.LANCZOS)
    edited_rgb = np.asarray(edited_pil, dtype=np.uint8)

    # 构造边缘羽化 Alpha 通道，贴入 PS 后边缘自动自然融合且可随时用蒙版微调
    alpha = np.ones((ph, pw), dtype=np.float32)
    feather = max(2, min(16, min(pw, ph) // 8))
    if feather > 0 and ph > feather * 2 and pw > feather * 2:
        alpha[:feather, :] = np.linspace(0.0, 1.0, feather, dtype=np.float32)[:, None]
        alpha[-feather:, :] = np.linspace(1.0, 0.0, feather, dtype=np.float32)[:, None]
        alpha[:, :feather] *= np.linspace(0.0, 1.0, feather, dtype=np.float32)[None, :]
        alpha[:, -feather:] *= np.linspace(1.0, 0.0, feather, dtype=np.float32)[None, :]

    rgba = np.dstack([edited_rgb, np.clip(np.round(alpha * 255.0), 0, 255).astype(np.uint8)])
    out_patch_png = Path(out_patch_png)
    out_patch_png.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(out_patch_png, format="PNG")
    return {
        "applied": True,
        "patch_png_path": str(out_patch_png),
        "patch_box_px": [px1, py1, px2, py2],
        "prompt": prompt,
    }



def apply_local_inpaint_patch(
    image_path: Path,
    region_norm: Sequence[float],
    prompt: str,
    image_gen_api_url: Optional[str] = None,
    image_gen_api_key: Optional[str] = None,
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """
    局部蒙版 Patch 生图修补并羽化叠回原图：
    - 仅裁切 region_norm ([x1, y1, x2, y2]) 对应的局部图像块发给 IMAGE_GEN_API_URL；
    - 绝不向生图模型发送或重绘整张人像，保证 Coser 人脸与服设像素 100% 零篡改。
    """
    api_url = (image_gen_api_url or os.getenv("IMAGE_GEN_API_URL") or "").strip()
    api_key = (image_gen_api_key if image_gen_api_key is not None else os.getenv("IMAGE_GEN_API_KEY", "")).strip()
    if not api_url:
        logger.info("未配置 IMAGE_GEN_API_URL，跳过局部蒙版生图步骤。")
        return {"applied": False, "reason": "IMAGE_GEN_API_URL_NOT_CONFIGURED"}

    buf = np.fromfile(str(image_path), dtype=np.uint8)
    full_bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if full_bgr is None:
        raise IOError(f"无法读取待局部修补成片: {image_path}")

    h, w = full_bgr.shape[:2]
    x1, y1, x2, y2 = [max(0.0, min(1.0, float(v))) for v in region_norm]
    px1, py1 = max(0, int(round(x1 * w))), max(0, int(round(y1 * h)))
    px2, py2 = min(w, int(round(x2 * w))), min(h, int(round(y2 * h)))
    pw, ph = px2 - px1, py2 - py1
    if pw < 8 or ph < 8:
        return {"applied": False, "reason": "PATCH_TOO_SMALL"}

    patch_bgr = full_bgr[py1:py2, px1:px2]
    patch_rgb = cv2.cvtColor(patch_bgr, cv2.COLOR_BGR2RGB)
    patch_io = io.BytesIO()
    Image.fromarray(patch_rgb).save(patch_io, format="PNG")
    patch_b64 = base64.b64encode(patch_io.getvalue()).decode("ascii")

    # 本地 127.0.0.1 / localhost 请求禁用系统代理，外部 API 遵循环境配置
    parsed = urlparse(api_url)
    use_trust_env = parsed.hostname not in ("127.0.0.1", "localhost", "::1")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    with httpx.Client(timeout=timeout, trust_env=use_trust_env) as client:
        resp = client.post(
            api_url,
            headers=headers,
            json={
                "patch_image_base64": patch_b64,
                "patch_size": [pw, ph],
                "region_norm": [x1, y1, x2, y2],
                "prompt": prompt,
                "mode": "local_patch_only",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    edited_b64 = data.get("edited_patch_base64") or data.get("image_base64")
    if not edited_b64:
        raise RuntimeError(f"局部生图接口未返回 edited_patch_base64: {data}")

    edited_pil = Image.open(io.BytesIO(base64.b64decode(edited_b64))).convert("RGB")
    if edited_pil.size != (pw, ph):
        edited_pil = edited_pil.resize((pw, ph), Image.Resampling.LANCZOS)
    edited_bgr = cv2.cvtColor(np.asarray(edited_pil, dtype=np.uint8), cv2.COLOR_RGB2BGR)

    # 羽化渐变蒙版叠回（中心 100% 替换为修补后 Patch，边缘平滑过渡，原图外部像素 100% 保持不动）
    mask = np.ones((ph, pw), dtype=np.float32)
    feather = max(2, min(16, min(pw, ph) // 8))
    if feather > 0 and ph > feather * 2 and pw > feather * 2:
        mask[:feather, :] = np.linspace(0.0, 1.0, feather, dtype=np.float32)[:, None]
        mask[-feather:, :] = np.linspace(1.0, 0.0, feather, dtype=np.float32)[:, None]
        mask[:, :feather] *= np.linspace(0.0, 1.0, feather, dtype=np.float32)[None, :]
        mask[:, -feather:] *= np.linspace(1.0, 0.0, feather, dtype=np.float32)[None, :]

    alpha = mask[..., None]
    blended = np.clip(
        edited_bgr.astype(np.float32) * alpha + patch_bgr.astype(np.float32) * (1.0 - alpha),
        0,
        255,
    ).astype(np.uint8)
    full_bgr[py1:py2, px1:px2] = blended

    ok, encoded = cv2.imencode(".jpg", full_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        raise IOError("局部修补叠回后编码 JPEG 失败")
    tmp_out = image_path.with_name(image_path.name + ".tmp")
    encoded.tofile(str(tmp_out))
    os.replace(tmp_out, image_path)

    logger.info("局部 Patch 生图修补叠回完成: %s (region=[%d,%d,%d,%d])", image_path.name, px1, py1, px2, py2)
    return {"applied": True, "patch_box_px": [px1, py1, px2, py2], "prompt": prompt}
