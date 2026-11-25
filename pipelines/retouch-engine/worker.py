#!/usr/bin/env python3
"""
ZERO STUDIO 正式修图主调度器 (Retouch Worker)：
串联完整修图管线：
  拉取 SELECTED 订单 -> 只读 NAS RAW 套正式 XMP 转 16-bit TIFF ->
  PS UXP 静默导出纯画面 1024px sRGB -> Gemini 多模态仿色出安全钳位 JSON 方案 ->
  PS UXP 执行 batchPlay 精修与导出 -> (按需) 局部蒙版 Patch 生图修补叠回 ->
  回写 retouch_jobs，整单完成后自动进入 REVIEW_PENDING（绝不跳过人工终审直接交付）。

支持 --once 单次运行与守护轮询模式。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

ENGINE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = ENGINE_DIR.parents[1]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

load_dotenv(WORKSPACE_ROOT / ".env", override=False)

from gemini_colorist import GeminiColorist  # noqa: E402
from mcp_planner import Local8BMCPPlanner  # noqa: E402
from ps_bridge import PSUXPBridgeClient, apply_local_inpaint_patch, generate_local_inpaint_patch  # noqa: E402
from revision_engine import RevisionEngine, RevisionRequest  # noqa: E402
from xmp_raw_stage import convert_raw_with_xmp_to_tiff, resolve_preset_entry  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [retouch_worker] %(message)s",
)
logger = logging.getLogger("retouch_engine.worker")


class RetouchWorker:
    def __init__(
        self,
        booking_api_url: Optional[str] = None,
        internal_api_key: Optional[str] = None,
        work_dir: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        retouch_xmp_dir: Optional[Path] = None,
        presets_map_path: Optional[Path] = None,
        colorist: Optional[GeminiColorist] = None,
        ps_bridge: Optional[PSUXPBridgeClient] = None,
        image_gen_api_url: Optional[str] = None,
        image_gen_api_key: Optional[str] = None,
        mcp_planner: Optional[Local8BMCPPlanner] = None,
        use_mcp_planner: Optional[bool] = None,
    ):
        self.booking_api_url = (
            booking_api_url or os.getenv("BOOKING_API_URL") or "http://127.0.0.1:3000"
        ).rstrip("/")
        self.internal_api_key = (
            internal_api_key if internal_api_key is not None else os.getenv("INTERNAL_API_KEY", "")
        ).strip()
        if not self.internal_api_key:
            raise RuntimeError("缺少 INTERNAL_API_KEY，无法访问 booking-site 内部修图队列接口")

        self.work_dir = Path(
            work_dir
            or os.getenv("RETOUCH_WORK_DIR")
            or str(WORKSPACE_ROOT / "work" / "retouch")
        )
        self.output_dir = Path(
            output_dir
            or os.getenv("RETOUCH_OUTPUT_DIR")
            or str(WORKSPACE_ROOT / "apps" / "booking-site" / "uploads" / "outputs")
        )
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.retouch_xmp_dir = Path(retouch_xmp_dir) if retouch_xmp_dir else None
        self.presets_map_path = Path(presets_map_path) if presets_map_path else None
        self.colorist = colorist or GeminiColorist()
        self.ps_bridge = ps_bridge or PSUXPBridgeClient()
        self.image_gen_api_url = image_gen_api_url
        self.image_gen_api_key = image_gen_api_key
        env_mcp = str(os.getenv("RETOUCH_USE_MCP_PLANNER", "false")).strip().lower()
        self.use_mcp_planner = (
            bool(use_mcp_planner)
            if use_mcp_planner is not None
            else (mcp_planner is not None or env_mcp in ("1", "true", "yes", "on"))
        )
        self.mcp_planner = mcp_planner or (Local8BMCPPlanner() if self.use_mcp_planner else None)

        # 传输模式：
        #   局域网模式（默认）—— worker 与 booking-site 在同一台机器，走 /api/internal/*
        #   公网模式（随身修图机）—— 配置了 WORKER_SYNC_TOKEN 且未显式指定局域网 booking_api_url 时启用，走 /api/worker/* + HMAC
        self._sync_client = None
        self._sync_enabled = bool((os.getenv("WORKER_SYNC_TOKEN") or "").strip()) and (booking_api_url is None)
        self._job_cache: Dict[int, Dict[str, Any]] = {}
        if self._sync_enabled:
            logger.info(
                "随身修图机模式：通过公网 %s 访问 Mac 主站并实时回传成片",
                os.getenv("WORKER_SYNC_URL") or "(未配置 WORKER_SYNC_URL)",
            )

        # 反馈修订引擎：看图 + 摄影师意见 -> 在现有 PSD 上叠加修改（公网模式自动回传新版 PSD/JPG）
        self.revision_engine = RevisionEngine(
            colorist=self.colorist,
            ps_bridge=self.ps_bridge,
            booking_api_url=self.booking_api_url,
            internal_api_key=self.internal_api_key,
            sync_client=self.sync,
        )

    def _api_headers(self) -> Dict[str, str]:
        return {
            "X-API-Key": self.internal_api_key,
            "Content-Type": "application/json",
        }

    # ==================== 传输层：局域网直连 or 公网 HMAC ====================

    @property
    def sync(self):
        """公网模式下的同步客户端；未配置 WORKER_SYNC_TOKEN 时返回 None（局域网模式）。"""
        if self._sync_client is None and self._sync_enabled:
            from worker_sync import build_sync_client_from_env
            self._sync_client = build_sync_client_from_env()
        return self._sync_client

    def fetch_pending_bookings(self) -> List[Dict[str, Any]]:
        # 公网模式：从主站 /api/worker/tasks 领任务
        if self.sync is not None:
            tasks = self.sync.fetch_tasks(limit=20)
            bookings = []
            for t in tasks:
                bookings.append({
                    "id": t["booking_id"],
                    "cn": t.get("cn"),
                    "character": t.get("character"),
                    "event_name": t.get("event_name"),
                    "preset_code": t.get("preset_code"),
                    "selected_assets": [
                        {
                            "asset_id": a["asset_id"],
                            "filename": a.get("filename", ""),
                            # 原片路径由 Windows 本地 NAS 挂载点解析
                            "nas_raw_path": a.get("raw_path", ""),
                        }
                        for a in t.get("selected_assets") or []
                    ],
                })
            return bookings

        with httpx.Client(timeout=20.0, trust_env=False) as client:
            resp = client.get(
                f"{self.booking_api_url}/api/internal/retouch/pending",
                headers=self._api_headers(),
            )
            resp.raise_for_status()
            return resp.json().get("bookings") or []

    def create_job(self, booking_id: int, asset_id: int, preset_code: str, stage: str = "QUEUED") -> Dict[str, Any]:
        # 公网模式：/api/worker/job 具备创建与更新双重语义
        if self.sync is not None:
            res = self.sync.report_job(
                booking_id=booking_id,
                asset_id=asset_id,
                stage=stage,
                preset_code=preset_code,
            )
            jid = int(res["job_id"])
            self._job_cache[jid] = {
                "booking_id": booking_id,
                "asset_id": asset_id,
                "preset_code": preset_code,
                "stage": stage,
            }
            return {"job": {"id": jid, "stage": stage}}

        with httpx.Client(timeout=20.0, trust_env=False) as client:
            resp = client.post(
                f"{self.booking_api_url}/api/internal/retouch/jobs",
                headers=self._api_headers(),
                json={
                    "booking_id": booking_id,
                    "asset_id": asset_id,
                    "preset_code": preset_code,
                    "stage": stage,
                },
            )
            resp.raise_for_status()
            return resp.json()

    def update_job(self, job_id: int, **fields: Any) -> Dict[str, Any]:
        if self.sync is not None:
            # 公网通道以 booking_id + asset_id 定位任务，这里用 job 反查
            job = self._job_cache.get(int(job_id)) or {}
            res = self.sync.report_job(
                booking_id=int(job.get("booking_id", 0)),
                asset_id=int(job.get("asset_id", 0)),
                stage=str(fields.get("stage") or job.get("stage") or "QUEUED"),
                preset_code=str(fields.get("preset_code") or job.get("preset_code") or ""),
                plan=fields.get("gemini_plan_json", job.get("plan")),
                output_path=str(fields.get("output_path") or job.get("output_path") or ""),
                psd_path=str(fields.get("psd_path") or job.get("psd_path") or ""),
                psd_url=str(fields.get("psd_url") or job.get("psd_url") or ""),
                preview_out_url=str(fields.get("preview_out_url") or job.get("preview_out_url") or ""),
                error_msg=str(fields.get("error_msg") or ""),
            )
            return {"job": {"id": res["job_id"], "stage": res.get("stage")},
                    "booking_status": res.get("booking_status")}

        with httpx.Client(timeout=20.0, trust_env=False) as client:
            resp = client.patch(
                f"{self.booking_api_url}/api/internal/retouch/jobs/{job_id}",
                headers=self._api_headers(),
                json=fields,
            )
            resp.raise_for_status()
            return resp.json()

    def process_single_asset(self, booking: Dict[str, Any], asset: Dict[str, Any]) -> Dict[str, Any]:
        booking_id = int(booking["id"])
        asset_id = int(asset["asset_id"])
        preset_code = str(booking.get("preset_code") or "cyan_air_event")
        raw_path_str = str(asset.get("nas_raw_path") or "").strip()
        nas_raw_path = Path(raw_path_str) if raw_path_str else None
        stem = Path(asset["filename"]).stem

        preset_entry = resolve_preset_entry(
            preset_code=preset_code,
            presets_map_path=self.presets_map_path,
            retouch_xmp_dir=self.retouch_xmp_dir,
        )

        # 1. 创建任务并进入 XMP_TIFF 阶段
        created = self.create_job(booking_id, asset_id, preset_code, stage="XMP_TIFF")
        job_id = int(created["job"]["id"])

        booking_work_dir = self.work_dir / f"booking_{booking_id}"
        booking_out_dir = self.output_dir / f"booking_{booking_id}"
        booking_work_dir.mkdir(parents=True, exist_ok=True)
        booking_out_dir.mkdir(parents=True, exist_ok=True)

        tiff_path = booking_work_dir / f"{stem}_a{asset_id}_16bit.tiff"
        srgb_preview_path = booking_work_dir / f"{stem}_a{asset_id}_srgb1024.jpg"
        # 主交付是 PSD（保留可编辑图层），JPEG 仅作网页预览
        psd_filename = f"{stem}_a{asset_id}_retouched.psd"
        psd_output_path = booking_out_dir / psd_filename
        final_filename = f"{stem}_a{asset_id}_retouched.jpg"
        final_output_path = booking_out_dir / final_filename
        preview_out_url = f"/outputs/booking_{booking_id}/{final_filename}"
        psd_out_url = f"/outputs/booking_{booking_id}/{psd_filename}"

        try:
            # 2. 定位原片：优先本地/局域网 NAS；若 Windows 随身带走不在局域网（或路径是 Mac 路径），通过公网从 Mac 实时拉取原片
            local_raw_path = nas_raw_path if (nas_raw_path is not None and nas_raw_path.is_file()) else None
            if local_raw_path is None:
                if self.sync is not None:
                    raw_ext = Path(asset.get("filename") or "src.arw").suffix or ".arw"
                    local_raw_path = booking_work_dir / f"{stem}_a{asset_id}_src{raw_ext}"
                    if not local_raw_path.is_file():
                        self.sync.download_asset_raw(asset_id=asset_id, dest_path=local_raw_path)
                else:
                    raise FileNotFoundError(f"原片不存在且未启用公网同步通道: {raw_path_str or '(空路径)'}")

            # 只读读取原片 -> 正式 XMP -> 本地 16-bit TIFF
            tiff_info = convert_raw_with_xmp_to_tiff(
                nas_raw_path=local_raw_path,
                out_tiff_path=tiff_path,
                preset_code=preset_code,
                presets_map_path=self.presets_map_path,
                retouch_xmp_dir=self.retouch_xmp_dir,
            )

            # 3. PS UXP Bridge 打开 16-bit TIFF 并静默导出纯画面 1024px sRGB JPEG
            self.update_job(job_id, stage="PS_EXPORT")
            self.ps_bridge.export_srgb_preview(tiff_path, srgb_preview_path, max_edge=1024)

            # 4. Gemini 多模态仿色大脑出方案（自动热加载 configs/retouch_skills/*.md + 参数钳位）
            self.update_job(job_id, stage="GEMINI_PLAN")
            plan = self.colorist.generate_plan(
                srgb_preview_path=srgb_preview_path,
                preset_code=preset_code,
                preset_notes=str(preset_entry.get("default_notes") or ""),
                character=str(booking.get("character") or ""),
                event_name=str(booking.get("event_name") or ""),
                reference_image_path=preset_entry.get("resolved_reference_path"),
            )

            # 5. 局部辅助生图（仅在需要局部去杂物/修补时）：
            #    在 Windows COM 模式下，先生成带羽化边缘的局部 Patch PNG，由 PS 作为独立可编辑图层贴入 PSD，
            #    确保核心调色与图层结构 100% 保留原生 Photoshop 操作。
            inpaint_info: Dict[str, Any] = {"applied": False}
            inpaint_patch_path: Optional[Path] = None
            inpaint_box_px: Optional[List[int]] = None
            if plan.get("needs_local_inpaint") and plan.get("inpaint_region") and getattr(self.ps_bridge, "backend", "") == "com":
                self.update_job(job_id, stage="LOCAL_INPAINT")
                patch_png = booking_work_dir / f"{stem}_a{asset_id}_patch.png"
                inpaint_info = generate_local_inpaint_patch(
                    source_image_path=tiff_path,
                    region_norm=plan["inpaint_region"],
                    prompt=str(plan.get("inpaint_prompt") or "clean background"),
                    out_patch_png=patch_png,
                    image_gen_api_url=self.image_gen_api_url,
                    image_gen_api_key=self.image_gen_api_key,
                )
                if inpaint_info.get("applied"):
                    inpaint_patch_path = Path(inpaint_info["patch_png_path"])
                    inpaint_box_px = inpaint_info.get("patch_box_px")

            # 6. PS 内执行调色（+ 可选局部生图图层）-> 另存 PSD（保留全部可编辑图层）-> 导出 JPG 预览
            self.update_job(job_id, stage="PS_RETOUCH", gemini_plan_json=plan)
            ps_action = None
            if preset_entry.get("ps_action_set") and preset_entry.get("ps_action_name"):
                ps_action = {
                    "action_set": preset_entry["ps_action_set"],
                    "action_name": preset_entry["ps_action_name"],
                }
            if self.use_mcp_planner and self.mcp_planner is not None:
                ps_export = self.mcp_planner.execute_retouch_session(
                    source_path=tiff_path,
                    high_level_plan=plan,
                    out_psd_path=psd_output_path,
                    out_preview_jpg=final_output_path,
                    brief_text=str(plan.get("summary") or ""),
                    ps_action=ps_action,
                )
                logger.info(
                    "已通过 8B+MCP (%s / %s) 另存 PSD 并导出 JPG: %s / %s",
                    ps_export.get("backend", "com"),
                    ps_export.get("planner_mode", ""),
                    psd_output_path.name,
                    final_output_path.name,
                )
            else:
                retouch_kwargs: Dict[str, Any] = {
                    "tiff_path": tiff_path,
                    "plan": plan,
                    "out_psd": psd_output_path,
                    "out_preview_jpg": final_output_path,
                    "ps_action": ps_action,
                }
                if inpaint_patch_path is not None and inpaint_box_px is not None:
                    retouch_kwargs["inpaint_patch_path"] = inpaint_patch_path
                    retouch_kwargs["inpaint_box_px"] = inpaint_box_px
                ps_export = self.ps_bridge.apply_retouch_and_export_psd(**retouch_kwargs)
                logger.info(
                    "PS 已另存 PSD 并导出 JPG: %s / %s",
                    psd_output_path.name,
                    final_output_path.name,
                )

            # 6b. 若为 UXP 队列模式且需要局部生图，回退为对导出 JPG 打羽化补丁
            if plan.get("needs_local_inpaint") and plan.get("inpaint_region") and not inpaint_info.get("applied"):
                self.update_job(job_id, stage="LOCAL_INPAINT")
                inpaint_info = apply_local_inpaint_patch(
                    image_path=final_output_path,
                    region_norm=plan["inpaint_region"],
                    prompt=str(plan.get("inpaint_prompt") or "clean background"),
                    image_gen_api_url=self.image_gen_api_url,
                    image_gen_api_key=self.image_gen_api_key,
                )

            # 7. 公网随身修图机模式：实时将修好的 JPG 预览图与分层 PSD 上传回 Mac 主站
            if self.sync is not None:
                if final_output_path.exists():
                    up_jpg = self.sync.upload_file(final_output_path, remote_name=f"b{booking_id}_{final_filename}")
                    preview_out_url = str(up_jpg.get("url") or preview_out_url)
                    logger.info("已公网实时回传预览图至 Mac: %s", preview_out_url)
                if psd_output_path.exists():
                    up_psd = self.sync.upload_file(psd_output_path, remote_name=f"b{booking_id}_{psd_filename}")
                    psd_out_url = str(up_psd.get("url") or psd_out_url)
                    logger.info("已公网实时回传分层 PSD 至 Mac: %s", psd_out_url)

            # 8. 回写 DONE；同时写入 output_path / psd_path / psd_url / preview_out_url，确保后续反馈修订可直接续修 PSD
            done_res = self.update_job(
                job_id,
                stage="DONE",
                gemini_plan_json=plan,
                output_path=str(psd_output_path),
                psd_path=str(psd_output_path),
                psd_url=psd_out_url,
                preview_out_url=preview_out_url,
                error_msg=None,
            )
            logger.info(
                "精修完成: booking=#%d asset=#%d job=#%d -> PSD %s + JPG %s (订单状态: %s)",
                booking_id,
                asset_id,
                job_id,
                psd_output_path.name,
                final_output_path.name,
                done_res.get("booking_status"),
            )
            if done_res.get("booking_status") == "REVIEW_PENDING":
                try:
                    gw_dir = str(WORKSPACE_ROOT / "services" / "im-gateway")
                    if gw_dir not in sys.path:
                        sys.path.insert(0, gw_dir)
                    from openclaw_notifier import OpenClawWeixinNotifier
                    pub_base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
                    full_preview = f"{pub_base}{preview_out_url}" if (pub_base and preview_out_url.startswith("/")) else preview_out_url
                    review_url = f"{pub_base}/review.html?booking_id={booking_id}" if pub_base else f"/review.html?booking_id={booking_id}"
                    OpenClawWeixinNotifier().push_review_ready(
                        booking_id=booking_id,
                        cn=str(booking.get("cn") or ""),
                        character=str(booking.get("character") or ""),
                        review_url=review_url,
                        preview_url=full_preview,
                        asset_count=len(booking.get("selected_assets") or [asset]),
                    )
                except Exception as notify_exc:  # noqa: BLE001
                    logger.warning("推送待终审微信通知失败（不影响修图结果）: %s", notify_exc)
            return {
                "job_id": job_id,
                "booking_id": booking_id,
                "asset_id": asset_id,
                "tiff_info": tiff_info,
                "plan": plan,
                "ps_export": ps_export,
                "inpaint_info": inpaint_info,
                "output_path": str(psd_output_path),
                "psd_path": str(psd_output_path),
                "psd_url": psd_out_url,
                "output_jpg_path": str(final_output_path),
                "preview_out_url": preview_out_url,
                "booking_status": done_res.get("booking_status"),
            }
        except Exception as exc:
            logger.error("精修失败 (booking=#%d asset=#%d job=#%d): %s", booking_id, asset_id, job_id, exc)
            try:
                self.update_job(job_id, stage="FAILED", error_msg=str(exc))
            except Exception:
                pass
            raise

    def run_once(self) -> Dict[str, Any]:
        """单次扫描并处理全部待修图订单。"""
        pending = self.fetch_pending_bookings()
        logger.info("扫描待修图队列：发现 %d 个订单待处理", len(pending))
        processed_jobs: List[Dict[str, Any]] = []

        for booking in pending:
            for asset in booking.get("selected_assets") or []:
                existing = asset.get("existing_job")
                if existing and existing.get("stage") == "DONE":
                    continue
                res = self.process_single_asset(booking, asset)
                processed_jobs.append(res)

        # 处理摄影师提交的修改意见（看图提意见 -> AI 再改）
        processed_revisions = self.process_pending_revisions()

        return {
            "pending_bookings": len(pending),
            "processed_jobs": processed_jobs,
            "processed_revisions": processed_revisions,
        }

    # ==================== 反馈修订 ====================

    def fetch_pending_revisions(self) -> List[Dict[str, Any]]:
        if self.sync is not None:
            return self.sync.fetch_revisions()
        with httpx.Client(timeout=20.0, trust_env=False) as client:
            resp = client.get(
                f"{self.booking_api_url}/api/internal/retouch/revisions/pending",
                headers=self._api_headers(),
            )
            resp.raise_for_status()
            return (resp.json() or {}).get("revisions") or []

    def process_pending_revisions(self) -> List[Dict[str, Any]]:
        """把所有待修订意见依次执行；单条失败不影响其它条。"""
        try:
            pending = self.fetch_pending_revisions()
        except Exception as exc:  # noqa: BLE001
            logger.error("拉取待修订意见失败: %s", exc)
            return []

        if not pending:
            return []

        logger.info("发现 %d 条待修订意见", len(pending))
        results: List[Dict[str, Any]] = []

        for item in pending:
            revision_id = item.get("revision_id")
            job_id = item.get("job_id")
            current_psd = item.get("current_psd_path") or ""
            current_preview = item.get("current_preview_path") or ""
            current_psd_url = item.get("current_psd_url") or ""
            current_preview_url = item.get("current_preview_url") or ""

            # 若处于公网随身机模式且本地缺少上一版 PSD/预览图，自动从 Mac 主站拉取到本地缓存
            rev_cache_dir = self.work_dir / f"booking_{int(item.get('booking_id') or 0)}" / "revisions"
            if self.sync is not None:
                if (not current_psd or not Path(current_psd).exists()) and current_psd_url:
                    local_psd = rev_cache_dir / f"job_{job_id}_v{int(item.get('current_version') or 0)}.psd"
                    try:
                        self.sync.download_output_file(current_psd_url, local_psd)
                        current_psd = str(local_psd)
                    except Exception as dl_exc:
                        logger.warning("从 Mac 拉取上一版 PSD 失败: %s", dl_exc)
                if (not current_preview or not Path(current_preview).exists()) and current_preview_url:
                    local_prev = rev_cache_dir / f"job_{job_id}_v{int(item.get('current_version') or 0)}.jpg"
                    try:
                        self.sync.download_output_file(current_preview_url, local_prev)
                        current_preview = str(local_prev)
                    except Exception as dl_exc:
                        logger.warning("从 Mac 拉取上一版预览图失败: %s", dl_exc)

            # 上一版没有 PSD/预览说明数据不全，只能跳过并给出原因
            if not current_psd or not current_preview:
                logger.warning(
                    "修订 job=#%s v%s 缺少上一版产物（psd=%s preview=%s），跳过",
                    job_id, item.get("target_version"), bool(current_psd), bool(current_preview),
                )
                results.append({
                    "revision_id": revision_id,
                    "job_id": job_id,
                    "ok": False,
                    "error": "缺少上一版 PSD 或预览图，无法在此基础上修订",
                })
                continue

            try:
                req = RevisionRequest(
                    job_id=int(job_id),
                    booking_id=int(item.get("booking_id") or 0),
                    asset_id=int(item.get("asset_id") or 0),
                    feedback_text=str(item.get("feedback_text") or ""),
                    current_psd_path=Path(current_psd),
                    current_preview_path=Path(current_preview),
                    version=int(item.get("current_version") or 0),
                    previous_summary=str(item.get("previous_summary") or ""),
                    preset_code=str(item.get("preset_code") or ""),
                )
                res = self.revision_engine.revise(req)
                results.append({
                    "revision_id": revision_id,
                    "job_id": job_id,
                    "ok": True,
                    "version": res.version,
                    "summary": res.summary,
                    "confidence": res.confidence,
                    "jsx_used": res.jsx_used,
                    "warnings": res.warnings,
                    "preview": str(res.output_preview_path) if res.output_preview_path else "",
                })
            except Exception as exc:  # noqa: BLE001
                logger.error("修订 job=#%s 失败: %s", job_id, exc)
                self._mark_revision_failed(revision_id, str(exc))
                results.append({"revision_id": revision_id, "job_id": job_id, "ok": False, "error": str(exc)})

        return results

    def _mark_revision_failed(self, revision_id: Any, error: str) -> None:
        """把失败的修订标记成可见状态，避免被无限重试。"""
        if not revision_id:
            return
        if self.sync is not None:
            try:
                self.sync.fail_revision(int(revision_id), str(error))
            except Exception as exc:  # noqa: BLE001
                logger.warning("公网标记修订失败状态异常: %s", exc)
            logger.error("修订 #%s 已标记失败: %s", revision_id, error)
            return
        try:
            with httpx.Client(timeout=15.0, trust_env=False) as client:
                resp = client.post(
                    f"{self.booking_api_url}/api/internal/retouch/revisions/{int(revision_id)}/fail",
                    headers=self._api_headers(),
                    json={"error": str(error)[:500]},
                )
                if resp.status_code >= 400:
                    logger.warning("标记修订失败状态未成功 (HTTP %s)", resp.status_code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("标记修订失败状态异常: %s", exc)
        logger.error("修订 #%s 已标记失败: %s", revision_id, error)

    def run_forever(self, poll_interval: float = 15.0) -> None:
        """守护轮询模式：有任务自动拉起 PS 处理，队列空闲超时后自动关闭 PS。"""
        logger.info("启动修图 Worker 守护轮询模式 (interval=%.1fs)...", poll_interval)
        try:
            while True:
                try:
                    res = self.run_once()
                    if not res.get("processed_jobs") and not res.get("processed_revisions"):
                        if hasattr(self.ps_bridge, "close_if_idle"):
                            self.ps_bridge.close_if_idle()
                except Exception as exc:
                    logger.error("修图轮询周期异常: %s", exc)
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            if hasattr(self.ps_bridge, "close_if_idle"):
                self.ps_bridge.close_if_idle(force=True)
            logger.info("收到退出信号，修图 Worker 已停止。")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ZERO STUDIO 正式修图管线 Worker")
    parser.add_argument("--once", action="store_true", help="单次扫描处理全部待修图订单后退出")
    parser.add_argument("--interval", type=float, default=15.0, help="守护轮询间隔秒数（默认 15s）")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    worker = RetouchWorker()
    if args.once:
        try:
            worker.run_once()
        finally:
            if hasattr(worker.ps_bridge, "close_if_idle"):
                worker.ps_bridge.close_if_idle(force=True)
    else:
        worker.run_forever(poll_interval=args.interval)


if __name__ == "__main__":
    main()
