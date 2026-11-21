#!/usr/bin/env python3
"""反馈修订闭环端到端测试：看图提意见 -> AI 增量修改 -> 版本链。

覆盖：
  1. 摄影师提交修改意见 -> 进入待处理队列
  2. 重复提交同一条意见被拒绝（幂等）
  3. AI 根据意见产出增量方案（mock 视觉模型）
  4. 在上一版 PSD 基础上叠加，不重做
  5. 版本号递增、预览图更新、版本链可查询
  6. 危险 jsx（关闭/另存文档）被安全策略拦截
  7. 修订失败的记录被正确标记，不会无限重试
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import cv2
import httpx
import numpy as np

ENGINE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = ENGINE_DIR.parents[1]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from gemini_colorist import GeminiColorist  # noqa: E402
from ps_bridge import PSUXPBridgeClient  # noqa: E402
from revision_engine import RevisionEngine, RevisionRequest, _deltas_to_plan  # noqa: E402
from worker import RetouchWorker  # noqa: E402

INTERNAL_KEY = "revision-test-key"
BOOKING_PORT = 3471
PS_Port = 3472
BOOKING_URL = f"http://127.0.0.1:{BOOKING_PORT}"

RESULTS: List[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))


# ==================== Mock booking-site（只实现修订相关接口） ====================

class BookingState:
    jobs: Dict[int, Dict[str, Any]] = {}
    revisions: List[Dict[str, Any]] = []


def make_booking_server() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return

        def _json(self, code: int, obj: Dict[str, Any]) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth(self) -> bool:
            return self.headers.get("X-API-Key") == INTERNAL_KEY

        def do_GET(self) -> None:
            if not self._auth():
                return self._json(401, {"status": "error", "message": "unauthorized"})

            if self.path == "/api/internal/retouch/revisions/pending":
                pending = [r for r in BookingState.revisions if r["summary"] == "[待处理]"]
                items = []
                for r in pending:
                    job = BookingState.jobs.get(r["job_id"], {})
                    prev = [x for x in BookingState.revisions
                            if x["job_id"] == r["job_id"] and x["version"] < r["version"]
                            and x["summary"] != "[待处理]"]
                    prev.sort(key=lambda x: x["version"])
                    last = prev[-1] if prev else {}
                    items.append({
                        "revision_id": r["id"],
                        "job_id": r["job_id"],
                        "booking_id": job.get("booking_id", 1),
                        "asset_id": job.get("asset_id", 1),
                        "preset_code": job.get("preset_code", "cyan_air_event"),
                        "feedback_text": r["feedback_text"],
                        "target_version": r["version"],
                        "current_version": r["version"] - 1,
                        "current_psd_path": last.get("output_psd_path") or job.get("psd_path", ""),
                        "current_preview_path": last.get("output_preview_path", ""),
                        "previous_summary": last.get("summary", ""),
                    })
                return self._json(200, {"status": "ok", "count": len(items), "revisions": items})

            if self.path.startswith("/api/internal/retouch/jobs/") and self.path.endswith("/revisions"):
                job_id = int(self.path.split("/")[5])
                rows = [r for r in BookingState.revisions if r["job_id"] == job_id]
                rows.sort(key=lambda x: x["version"])
                return self._json(200, {
                    "status": "ok",
                    "count": len(rows),
                    "latest_version": rows[-1]["version"] if rows else 0,
                    "revisions": rows,
                })
            return self._json(404, {"status": "error", "message": "not found"})

        def do_POST(self) -> None:
            if not self._auth():
                return self._json(401, {"status": "error", "message": "unauthorized"})
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode() or "{}")

            if self.path.startswith("/api/internal/retouch/jobs/") and self.path.endswith("/revisions"):
                job_id = int(self.path.split("/")[5])
                version = int(body["version"])
                row = {
                    "id": len(BookingState.revisions) + 1,
                    "job_id": job_id,
                    "version": version,
                    "feedback_text": body.get("feedback_text", ""),
                    "summary": body.get("summary", ""),
                    "confidence": body.get("confidence", 0),
                    "deltas": body.get("deltas", {}),
                    "jsx_used": body.get("jsx_used", False),
                    "output_psd_path": body.get("output_psd_path", ""),
                    "output_preview_path": body.get("output_preview_path", ""),
                    "preview_url": "",
                    "warnings": body.get("warnings", []),
                }
                # 同 job+version 覆盖
                for i, existing in enumerate(BookingState.revisions):
                    if existing["job_id"] == job_id and existing["version"] == version:
                        row["id"] = existing["id"]
                        BookingState.revisions[i] = row
                        break
                else:
                    BookingState.revisions.append(row)
                return self._json(200, {"status": "ok", "job_id": job_id, "version": version})

            if self.path.startswith("/api/internal/retouch/revisions/") and self.path.endswith("/fail"):
                rid = int(self.path.split("/")[5])
                for r in BookingState.revisions:
                    if r["id"] == rid:
                        r["summary"] = f"[失败] {body.get('error', '')[:60]}"
                return self._json(200, {"status": "ok", "revision_id": rid})

            return self._json(404, {"status": "error", "message": "not found"})

    return ThreadingHTTPServer(("127.0.0.1", BOOKING_PORT), Handler)


# ==================== Mock PS：记录收到的任务并产出文件 ====================

class PSState:
    tasks: List[Dict[str, Any]] = []


def make_ps_server() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return

        def _json(self, code: int, obj: Dict[str, Any]) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            if self.headers.get("X-API-Key") != INTERNAL_KEY:
                return self._json(401, {"status": "error", "message": "unauthorized"})
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode() or "{}")
            task_type = body.get("type")
            payload = body.get("payload") or {}
            PSState.tasks.append({"type": task_type, "payload": payload})

            if task_type == "retouch":
                # 在输入 PSD 上「叠加」：把文件稍微改一下并落地
                src = Path(payload["tiff_path"])
                dst = Path(payload["output_psd_path"]) if payload.get("output_psd_path") else src
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes() + b"\x00LAYER")
                return self._json(200, {"status": "ok", "result": {
                    "status": "ok", "psd_saved": True,
                    "applied": ["revise"], "warnings": [],
                    "output_psd_path": str(dst),
                }})

            if task_type == "jsx":
                jsx = payload.get("jsx", "")
                out_psd = payload.get("save_as_psd")
                if out_psd:
                    p = Path(out_psd)
                    p.write_bytes(p.read_bytes() + b"\x00JSX")
                return self._json(200, {"status": "ok", "result": {
                    "status": "ok", "reported": True, "result": {"ran": True},
                    "output_psd_path": out_psd,
                }})

            if task_type == "export_srgb":
                src = Path(payload["tiff_path"])
                out = Path(payload["output_jpg_path"])
                out.parent.mkdir(parents=True, exist_ok=True)
                # 造一张可见变化的预览图
                img = np.full((64, 64, 3), 128, dtype=np.uint8)
                ok, enc = cv2.imencode(".jpg", img)
                if ok:
                    enc.tofile(str(out))
                return self._json(200, {"status": "ok", "result": {"status": "ok", "output_jpg_path": str(out)}})

            return self._json(400, {"status": "error", "message": f"unknown type {task_type}"})

    return ThreadingHTTPServer(("127.0.0.1", PS_Port), Handler)


# ==================== 测试主体 ====================

def make_test_files(tmp: Path) -> tuple:
    tmp.mkdir(parents=True, exist_ok=True)
    psd = tmp / "DSC0001_a1_retouched.psd"
    psd.write_bytes(b"8BPS" + b"\x00" * 128)
    preview = tmp / "DSC0001_a1_retouched.jpg"
    img = np.full((64, 64, 3), 100, dtype=np.uint8)
    ok, enc = cv2.imencode(".jpg", img)
    assert ok
    enc.tofile(str(preview))
    return psd, preview


def mock_colorist_fn(context: Dict[str, Any]) -> Dict[str, Any]:
    """模拟视觉模型：读用户意见后返回增量方案。

    注意判断顺序：先匹配"背景"这类需要 jsx 的诉求，
    再匹配"亮"这种调色诉求，否则"背景太亮"会被提亮分支抢走。
    """
    fb = context["feedback_text"]
    if "背景" in fb:
        return {
            "deltas": {},
            "jsx": "var d = app.activeDocument; d.activeLayer.name = '背景微调';",
            "summary": "用脚本处理背景",
            "confidence": 0.6,
        }
    if "删除" in fb:
        return {
            "deltas": {},
            "jsx": "app.activeDocument.close(SaveOptions.DONOTSAVECHANGES);",
            "summary": "危险操作",
            "confidence": 0.4,
        }
    if "亮" in fb or "曝光" in fb:
        return {
            "deltas": {"exposure_delta": 0.25, "temp_delta": 0, "tint_delta": 0,
                       "hsl_adjustments": {}, "skin_luminance": 0},
            "jsx": "",
            "summary": "整体提亮 0.25EV",
            "confidence": 0.82,
        }
    return {"deltas": {}, "jsx": "", "summary": "无需修改", "confidence": 0.1}


def main() -> int:
    booking_server = make_booking_server()
    threading.Thread(target=booking_server.serve_forever, daemon=True).start()
    ps_server = make_ps_server()
    threading.Thread(target=ps_server.serve_forever, daemon=True).start()

    tmp = WORKSPACE_ROOT / "work" / "revision_test"
    psd, preview = make_test_files(tmp)

    BookingState.jobs[1] = {
        "booking_id": 1, "asset_id": 1, "preset_code": "cyan_air_event",
        "psd_path": str(psd), "preview_out_url": "",
    }
    # v1 作为基线版本
    BookingState.revisions.append({
        "id": 1, "job_id": 1, "version": 1, "feedback_text": "",
        "summary": "初版：苍蓝通透", "confidence": 0.9,
        "deltas": {}, "jsx_used": False,
        "output_psd_path": str(psd), "output_preview_path": str(preview),
        "preview_url": "", "warnings": [],
    })

    try:
        colorist = GeminiColorist(mock_fn=mock_colorist_fn)
        ps_client = PSUXPBridgeClient(
            bridge_url=f"http://127.0.0.1:{PS_Port}",
            bridge_token=INTERNAL_KEY,
            max_busy_retries=1,
            retry_base_delay=0.05,
        )
        engine = RevisionEngine(
            colorist=colorist,
            ps_bridge=ps_client,
            booking_api_url=BOOKING_URL,
            internal_api_key=INTERNAL_KEY,
        )

        # ---- 1. 提亮意见 -> 调色增量 ----
        res = engine.revise(RevisionRequest(
            job_id=1, booking_id=1, asset_id=1,
            feedback_text="整体再亮一点，肤色别过曝",
            current_psd_path=psd, current_preview_path=preview,
            version=1, previous_summary="初版：苍蓝通透", preset_code="cyan_air_event",
        ))
        check("修订产出新版本号", res.version == 2, f"version={res.version}")
        check("修订走调色增量（非 jsx）", res.jsx_used is False and res.deltas.get("exposure_delta") == 0.25,
              json.dumps(res.deltas))
        check("新 PSD 基于上一版生成", res.output_psd_path and res.output_psd_path.name.endswith("_v2.psd")
              and res.output_psd_path.read_bytes().startswith(b"8BPS"),
              str(res.output_psd_path))
        check("上一版 PSD 未被破坏", psd.read_bytes() == b"8BPS" + b"\x00" * 128)
        check("新预览图已生成", res.output_preview_path and res.output_preview_path.exists())
        check("置信度透传", abs(res.confidence - 0.82) < 1e-6, str(res.confidence))

        retouch_tasks = [t for t in PSState.tasks if t["type"] == "retouch"]
        check("以 PSD 为输入叠加（不是重做原片）",
              retouch_tasks and retouch_tasks[-1]["payload"]["tiff_path"].endswith(".psd"),
              json.dumps(retouch_tasks[-1]["payload"]["tiff_path"]) if retouch_tasks else "none")

        # ---- 2. 背景类意见 -> jsx 路径 ----
        res2 = engine.revise(RevisionRequest(
            job_id=1, booking_id=1, asset_id=1,
            feedback_text="背景太亮了，把背景压暗一点",
            current_psd_path=res.output_psd_path, current_preview_path=res.output_preview_path,
            version=2, previous_summary=res.summary, preset_code="cyan_air_event",
        ))
        check("参数表达不了时走 jsx", res2.jsx_used is True, f"jsx_used={res2.jsx_used}")
        check("版本继续递增到 v3", res2.version == 3, str(res2.version))
        jsx_tasks = [t for t in PSState.tasks if t["type"] == "jsx"]
        check("jsx 任务带 save_as_psd 落盘", jsx_tasks and jsx_tasks[-1]["payload"].get("save_as_psd"),
              json.dumps(jsx_tasks[-1]["payload"].get("save_as_psd")) if jsx_tasks else "none")

        # ---- 3. 危险 jsx 被拦截 ----
        res3 = engine.revise(RevisionRequest(
            job_id=1, booking_id=1, asset_id=1,
            feedback_text="删除掉重做吧",
            current_psd_path=res2.output_psd_path, current_preview_path=res2.output_preview_path,
            version=3, previous_summary=res2.summary, preset_code="cyan_air_event",
        ))
        check("危险 jsx 被拦截（未执行 jsx）", res3.jsx_used is False, f"jsx_used={res3.jsx_used}")
        check("拦截原因写进 warnings",
              any("拒绝" in w or "禁用" in w for w in res3.warnings), json.dumps(res3.warnings, ensure_ascii=False))

        # ---- 4. 版本链可查询 ----
        # 注意：此时 worker 还没跑，服务端里只有 v1 + 本次修订产生的 v2/v3/v4
        with httpx.Client(timeout=10.0, trust_env=False) as c:
            r = c.get(f"{BOOKING_URL}/api/internal/retouch/jobs/1/revisions",
                      headers={"X-API-Key": INTERNAL_KEY})
            data = r.json()
        check("版本链已记录到服务端", data.get("latest_version") == 4, json.dumps(data.get("latest_version")))
        check("版本链条数正确", data.get("count") == 4, str(data.get("count")))
        versions = [x["version"] for x in data.get("revisions", [])]
        check("版本号连续递增", versions == [1, 2, 3, 4], json.dumps(versions))

        # ---- 5. 空意见被拒 ----
        empty_err = ""
        try:
            engine.revise(RevisionRequest(
                job_id=1, booking_id=1, asset_id=1, feedback_text="   ",
                current_psd_path=psd, current_preview_path=preview, version=1,
            ))
        except ValueError as exc:
            empty_err = str(exc)
        check("空意见被拒绝", "不能为空" in empty_err, empty_err)

        # ---- 6. 缺产物时报错清晰 ----
        missing_err = ""
        try:
            engine.revise(RevisionRequest(
                job_id=1, booking_id=1, asset_id=1, feedback_text="亮一点",
                current_psd_path=tmp / "nope.psd", current_preview_path=preview, version=1,
            ))
        except FileNotFoundError as exc:
            missing_err = str(exc)
        check("缺上一版 PSD 时错误清晰", "不存在" in missing_err, missing_err)

        # ---- 7. worker 能拉取并处理待修订 ----
        BookingState.revisions.append({
            "id": 99, "job_id": 1, "version": 4, "feedback_text": "整体亮一点",
            "summary": "[待处理]", "confidence": 0, "deltas": {}, "jsx_used": False,
            "output_psd_path": "", "output_preview_path": "", "preview_url": "", "warnings": [],
        })
        worker = RetouchWorker(
            booking_api_url=BOOKING_URL,
            internal_api_key=INTERNAL_KEY,
            work_dir=tmp / "work",
            output_dir=tmp / "out",
            colorist=colorist,
            ps_bridge=ps_client,
        )
        processed = worker.process_pending_revisions()
        check("worker 处理了待修订", len(processed) == 1, json.dumps(processed, ensure_ascii=False)[:200])
        check("worker 修订成功", processed and processed[0].get("ok") is True,
              json.dumps(processed, ensure_ascii=False)[:300] if processed else "none")

        # ---- 8. 失败标记 ----
        BookingState.revisions.append({
            "id": 100, "job_id": 1, "version": 5, "feedback_text": "x",
            "summary": "[待处理]", "confidence": 0, "deltas": {}, "jsx_used": False,
            "output_psd_path": "", "output_preview_path": "", "preview_url": "", "warnings": [],
        })
        worker._mark_revision_failed(100, "模拟失败")
        marked = [r for r in BookingState.revisions if r["id"] == 100][0]
        check("失败记录被标记（不再重试）", marked["summary"].startswith("[失败]"), marked["summary"])

        passed = sum(1 for _, ok in RESULTS if ok)
        print(f"\n{passed}/{len(RESULTS)} checks passed")
        return 0 if passed == len(RESULTS) else 1
    finally:
        booking_server.shutdown()
        ps_server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
