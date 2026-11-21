#!/usr/bin/env python3
"""
ZERO STUDIO · 反馈修订引擎
============================================================
把「摄影师看图后写的一句话」变成「在现有 PSD 之上继续叠加的修改」。

完整闭环：

    摄影师在预览图下方写意见
        ↓ (POST /api/internal/retouch/jobs/:id/feedback)
    本引擎：
        1. 取当前版本的预览图 + 上一版摘要
        2. 交给视觉模型 -> 生成「增量调色参数」或「一段安全的 ExtendScript」
        3. 在上一版 PSD 基础上继续叠加调整层 / 执行 jsx
        4. 导出新预览图，版本号 +1
        ↓
    摄影师继续看图、继续写意见……

设计要点
--------
- **绝不重做**：始终在上一版 PSD 上叠加，可编辑图层全部保留
- **双重安全**：调色参数走钳位，jsx 走白名单审查（见 gemini_colorist._sanitize_revision_jsx）
- **版本链**：每次修订都留下 (psd, preview, feedback, summary) 记录，可追溯可回滚
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from gemini_colorist import GeminiColorist
from ps_bridge import PSUXPBridgeClient

logger = logging.getLogger("retouch_engine.revision")


@dataclass
class RevisionRequest:
    """一次修订请求（来自摄影师的意见）。"""
    job_id: int
    booking_id: int
    asset_id: int
    feedback_text: str
    current_psd_path: Path
    current_preview_path: Path
    version: int = 1
    previous_summary: str = ""
    preset_code: str = ""


@dataclass
class RevisionResult:
    """一次修订的结果。"""
    job_id: int
    version: int
    summary: str
    confidence: float
    deltas: Dict[str, Any] = field(default_factory=dict)
    jsx_used: bool = False
    output_psd_path: Optional[Path] = None
    output_preview_path: Optional[Path] = None
    warnings: List[str] = field(default_factory=list)


class RevisionEngine:
    """把自然语言意见翻译成 Photoshop 里的增量修改。"""

    def __init__(
        self,
        colorist: Optional[GeminiColorist] = None,
        ps_bridge: Optional[PSUXPBridgeClient] = None,
        booking_api_url: Optional[str] = None,
        internal_api_key: Optional[str] = None,
        sync_client: Any = None,
    ):
        self.colorist = colorist or GeminiColorist()
        self.ps_bridge = ps_bridge or PSUXPBridgeClient()
        self.booking_api_url = (
            booking_api_url or os.getenv("BOOKING_API_URL") or "http://127.0.0.1:3000"
        ).rstrip("/")
        self.internal_api_key = (
            internal_api_key if internal_api_key is not None else os.getenv("INTERNAL_API_KEY", "")
        ).strip()
        self.sync_client = sync_client

    # ---------- 主流程 ----------

    def revise(self, req: RevisionRequest) -> RevisionResult:
        """执行一次修订：AI 出方案 -> PS 叠加 -> 导出新版本。"""
        if not req.feedback_text.strip():
            raise ValueError("意见不能为空，无法修订")

        current_psd = Path(req.current_psd_path)
        current_preview = Path(req.current_preview_path)
        if not current_psd.exists():
            raise FileNotFoundError(f"上一版 PSD 不存在: {current_psd}")
        if not current_preview.exists():
            raise FileNotFoundError(f"上一版预览图不存在: {current_preview}")

        new_version = int(req.version) + 1
        out_dir = current_psd.parent
        stem = current_psd.stem
        # 去掉旧版本后缀，统一成 xxx_v3.psd 这种形式
        base_stem = stem.rsplit("_v", 1)[0] if "_v" in stem and stem.rsplit("_v", 1)[1].isdigit() else stem
        new_psd = out_dir / f"{base_stem}_v{new_version}.psd"
        new_preview = out_dir / f"{base_stem}_v{new_version}.jpg"

        logger.info(
            "开始修订 job=#%d v%d -> v%d，意见：%s",
            req.job_id, req.version, new_version, req.feedback_text[:80],
        )

        # 1) AI 看图 + 读意见 -> 增量方案
        revision = self.colorist.generate_revision(
            preview_image_path=current_preview,
            feedback_text=req.feedback_text,
            version=new_version,
            previous_summary=req.previous_summary,
            preset_code=req.preset_code,
        )

        result = RevisionResult(
            job_id=req.job_id,
            version=new_version,
            summary=revision.get("summary") or "",
            confidence=float(revision.get("confidence") or 0.0),
            deltas=revision.get("deltas") or {},
        )
        if revision.get("jsx_warning"):
            result.warnings.append(revision["jsx_warning"])

        # 2) 在上一版 PSD 基础上叠加
        #    先复制成新版本文件，避免中途失败把上一版写坏
        shutil.copy2(current_psd, new_psd)

        jsx = str(revision.get("jsx") or "").strip()
        if jsx:
            self._apply_jsx(new_psd, jsx, result)
        else:
            self._apply_deltas(new_psd, revision.get("deltas") or {}, result)

        # 3) 导出新预览图（PI 复核用）
        self.ps_bridge.export_srgb_preview(new_psd, new_preview, max_edge=1024)
        result.output_psd_path = new_psd
        result.output_preview_path = new_preview

        logger.info(
            "修订完成 job=#%d v%d -> %s（置信度 %.2f，%s）",
            req.job_id,
            new_version,
            new_psd.name,
            result.confidence,
            "jsx" if result.jsx_used else "调整层",
        )

        # 4) 回写服务端版本记录
        self._report_back(req, result, revision)

        return result

    # ---------- 应用修改 ----------

    def _apply_deltas(self, psd_path: Path, deltas: Dict[str, Any], result: RevisionResult) -> None:
        """把增量调色参数作为新调整层叠加到 PSD 上。"""
        plan = _deltas_to_plan(deltas)
        if not plan.get("_has_changes"):
            result.warnings.append("AI 没有给出任何可执行的调色增量")
            return

        clean_plan = {k: v for k, v in plan.items() if not k.startswith("_")}
        out = self.ps_bridge.apply_retouch_and_export_psd(
            tiff_path=psd_path,          # 直接以 PSD 为输入，在它之上继续叠加
            plan=clean_plan,
            out_psd=psd_path,            # 原地保存（已复制成新版本）
            out_preview_jpg=None,
        )
        for w in (out.get("warnings") or []):
            result.warnings.append(str(w))

    def _apply_jsx(self, psd_path: Path, jsx: str, result: RevisionResult) -> None:
        """参数表达不了时，执行 AI 生成的安全 ExtendScript。"""
        kwargs: Dict[str, Any] = {"jsx": jsx, "save_as_psd": str(psd_path)}
        if getattr(self.ps_bridge, "backend", "") == "com":
            kwargs["psd_path"] = psd_path
        out = self.ps_bridge.run_jsx(**kwargs)
        result.jsx_used = True
        if out.get("reported") is False:
            result.warnings.append("脚本执行完成但未回报结果（可能未产生可见修改）")

    # ---------- 回写服务端 ----------

    def _report_back(self, req: RevisionRequest, result: RevisionResult, revision: Dict[str, Any]) -> None:
        """把本次修订记录（含公网实时上传的新版预览与 PSD）写回主站，供中控台展示版本链。"""
        preview_url = ""
        psd_url = ""
        if self.sync_client is not None:
            try:
                if result.output_preview_path and Path(result.output_preview_path).exists():
                    up_prev = self.sync_client.upload_file(
                        Path(result.output_preview_path),
                        remote_name=f"b{req.booking_id}_j{req.job_id}_v{result.version}.jpg",
                    )
                    preview_url = str(up_prev.get("url") or "")
                if result.output_psd_path and Path(result.output_psd_path).exists():
                    up_psd = self.sync_client.upload_file(
                        Path(result.output_psd_path),
                        remote_name=f"b{req.booking_id}_j{req.job_id}_v{result.version}.psd",
                    )
                    psd_url = str(up_psd.get("url") or "")
                payload = {
                    "job_id": req.job_id,
                    "version": result.version,
                    "feedback_text": req.feedback_text,
                    "summary": result.summary,
                    "confidence": result.confidence,
                    "deltas": result.deltas,
                    "jsx_used": result.jsx_used,
                    "output_psd_path": str(result.output_psd_path) if result.output_psd_path else "",
                    "output_preview_path": str(result.output_preview_path) if result.output_preview_path else "",
                    "preview_url": preview_url,
                    "psd_url": psd_url,
                    "warnings": result.warnings,
                }
                self.sync_client.report_revision(payload)
                logger.info("已公网实时回传修订 v%d 至 Mac 主站: %s", result.version, preview_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("公网回传修订结果失败: %s", exc)
            return

        if not self.internal_api_key:
            logger.warning("缺少 INTERNAL_API_KEY，跳过版本回写")
            return
        payload = {
            "job_id": req.job_id,
            "version": result.version,
            "feedback_text": req.feedback_text,
            "summary": result.summary,
            "confidence": result.confidence,
            "deltas": result.deltas,
            "jsx_used": result.jsx_used,
            "output_psd_path": str(result.output_psd_path) if result.output_psd_path else "",
            "output_preview_path": str(result.output_preview_path) if result.output_preview_path else "",
            "warnings": result.warnings,
        }
        url = f"{self.booking_api_url}/api/internal/retouch/jobs/{req.job_id}/revisions"
        try:
            with httpx.Client(timeout=20.0, trust_env=False) as client:
                resp = client.post(
                    url,
                    headers={"X-API-Key": self.internal_api_key, "Content-Type": "application/json"},
                    json=payload,
                )
                if resp.status_code >= 400:
                    logger.warning("版本回写失败 (HTTP %s): %s", resp.status_code, resp.text[:200])
        except Exception as exc:  # noqa: BLE001
            logger.warning("版本回写异常（不影响本次修订）: %s", exc)


def _deltas_to_plan(deltas: Dict[str, Any]) -> Dict[str, Any]:
    """把修订增量转成 ps_bridge 能消费的 plan 结构，并标记是否真的有变化。"""
    plan: Dict[str, Any] = {
        "exposure_delta": float(deltas.get("exposure_delta") or 0.0),
        "temp_delta": float(deltas.get("temp_delta") or 0.0),
        "tint_delta": float(deltas.get("tint_delta") or 0.0),
        "skin_luminance": float(deltas.get("skin_luminance") or 0.0),
    }

    hsl = deltas.get("hsl_adjustments") or {}
    changed_hsl = {
        k: v for k, v in hsl.items()
        if isinstance(v, dict) and any(abs(float(v.get(x) or 0)) > 0.01 for x in ("hue", "saturation", "luminance"))
    }
    if changed_hsl:
        plan["hsl"] = changed_hsl

    curves = deltas.get("curves_points")
    if isinstance(curves, list) and len(curves) >= 2:
        plan["curves_points"] = curves

    has_changes = any(
        abs(plan[k]) > 0.001 for k in ("exposure_delta", "temp_delta", "tint_delta", "skin_luminance")
    ) or bool(changed_hsl) or bool(curves)
    plan["_has_changes"] = has_changes
    return plan
