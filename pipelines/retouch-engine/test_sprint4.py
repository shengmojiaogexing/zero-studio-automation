#!/usr/bin/env python3
"""
Sprint 4 全链路端到端闭环自动化测试：
启动真实 booking-site + 真实 im-gateway (FastAPI/uvicorn) + 本地 Mock PS UXP Bridge + 本地 Mock 局部生图 API，
配合 Mock Gemini（含故意越界参数验证安全钳位 + 局部蒙版 Patch 生图验证），完整跑通：
  1. 创建预约单 -> 写入批次资产 -> 客户选图锁单为 SELECTED；
  2. worker.py (run_once) 执行「正式 XMP 转 16-bit TIFF -> PS 导出 1024px sRGB (含 busy 重试) ->
     Gemini 热加载 Skill 仿色 + 越界参数安全钳位 -> PS batchPlay 精修导出 -> 局部蒙版 Patch 生图叠回」；
  3. 验证订单严格停留在 REVIEW_PENDING、未提前推送任何消息给客户、NAS 原片零修改；
  4. 验证 review.html 与 GET /api/admin/review-queue 鉴权保护与待审数据展示；
  5. 模拟摄影师点击「一键放行交付」(POST /api/admin/bookings/:id/approve-delivery) ->
     验证订单流转为 DELIVERED、成品交付页 /delivery/:token 可访问，且 im-gateway 成功将成片链接推送回客户。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import httpx
import numpy as np
import uvicorn
from PIL import Image

ENGINE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = ENGINE_DIR.parents[1]
BOOKING_SITE_DIR = WORKSPACE_ROOT / "apps" / "booking-site"
GATEWAY_DIR = WORKSPACE_ROOT / "services" / "im-gateway"

sys.path.insert(0, str(ENGINE_DIR))
sys.path.insert(0, str(GATEWAY_DIR))

from gemini_colorist import GeminiColorist  # noqa: E402
from ps_bridge import PSUXPBridgeClient  # noqa: E402
from worker import RetouchWorker  # noqa: E402
from adapters.base import IMAdapter  # noqa: E402
from main import create_app as create_gateway_app  # noqa: E402
from rate_limiter import FriendRequestRateLimiter  # noqa: E402

BOOKING_PORT = 3460
GATEWAY_PORT = 3461
PS_BRIDGE_PORT = 3462
INPAINT_PORT = 3463

BOOKING_URL = f"http://127.0.0.1:{BOOKING_PORT}"
GATEWAY_URL = f"http://127.0.0.1:{GATEWAY_PORT}"
PS_BRIDGE_URL = f"http://127.0.0.1:{PS_BRIDGE_PORT}"
INPAINT_URL = f"http://127.0.0.1:{INPAINT_PORT}/inpaint"

INTERNAL_KEY = "sprint4-e2e-internal-key"
ADMIN_USER = "admin"
ADMIN_PASS = "sprint4-admin-pass"
BRIDGE_TOKEN = "sprint4-ps-bridge-token"
INPAINT_KEY = "sprint4-inpaint-key"


class RecordingIMAdapter(IMAdapter):
    """挂载到真实 FastAPI im-gateway 的记录型适配器，用于断言客户收到的交付推送。"""

    def __init__(self, platform: str):
        self.platform = platform
        self.messages: List[Dict[str, Any]] = []

    async def send_text(self, target_id: str, text: str, chat_type: str = "private") -> Dict[str, Any]:
        rec = {"type": "text", "target_id": target_id, "text": text, "chat_type": chat_type}
        self.messages.append(rec)
        return {"status": "ok", **rec}

    async def send_image(self, target_id: str, image: Union[str, Path, bytes], chat_type: str = "private") -> Dict[str, Any]:
        rec = {"type": "image", "target_id": target_id}
        self.messages.append(rec)
        return {"status": "ok", **rec}

    async def send_location(
        self, target_id: str, lat: float, lng: float, title: str, address: str, chat_type: str = "private"
    ) -> Dict[str, Any]:
        return {"status": "ok"}

    async def request_add_friend(self, user_id: str, remark: str, group_id: Optional[str] = None) -> Dict[str, Any]:
        return {"status": "ok"}


def make_ps_bridge_server(state: Dict[str, Any]) -> ThreadingHTTPServer:
    """本地 Mock「booking-site PS 桥接队列」：验证 X-API-Key、任务类型与队列语义。

    新协议（插件轮询队列）：
      POST /api/internal/ps-bridge/run  { type, payload, timeout_ms } -> { status, task_id, result }
      GET  /api/ps-bridge/status                                      -> { plugin: { online } }
    mock 直接「扮演」服务端 + 插件：收到任务就地执行并返回结果。
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, code: int, obj: Dict[str, Any]) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/api/ps-bridge/status":
                return self._send_json(200, {
                    "status": "ok",
                    "plugin": {"online": bool(state.get("plugin_online", True))},
                })
            return self._send_json(404, {"error": "unknown_route"})

        def do_POST(self) -> None:
            key = self.headers.get("X-API-Key", "")
            if key != BRIDGE_TOKEN:
                return self._send_json(401, {"status": "error", "message": "Unauthorized: invalid X-API-Key"})

            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            task_type = body.get("type") or "retouch"
            payload = body.get("payload") or {}
            state["task_types"].append(task_type)

            # 模拟「PS 未打开」：投递方应收到明确的 504
            if not state.get("plugin_online", True):
                return self._send_json(504, {"status": "error", "message": "等待 Photoshop 处理超时"})

            if task_type == "export_srgb":
                state["export_calls"] += 1
                # 第一次故意返回 503 验证退避重试
                if state["export_calls"] == 1:
                    return self._send_json(503, {"status": "error", "message": "服务繁忙"})

                tiff_path = Path(payload["tiff_path"])
                out_jpg = Path(payload["output_jpg_path"])
                max_edge = int(payload.get("max_edge", 1024))

                buf = np.fromfile(str(tiff_path), dtype=np.uint8)
                bgr16 = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
                assert bgr16 is not None and bgr16.dtype == np.uint16, f"应收到 16-bit TIFF，实际 {getattr(bgr16, 'dtype', None)}"
                state["verified_16bit_tiffs"].append(str(tiff_path))

                rgb8 = (bgr16[:, :, ::-1] / 257.0).clip(0, 255).astype(np.uint8)
                im = Image.fromarray(rgb8, mode="RGB")
                if max(im.size) > max_edge:
                    im.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                out_jpg.parent.mkdir(parents=True, exist_ok=True)
                im.save(out_jpg, format="JPEG", quality=92)
                return self._send_json(200, {"status": "ok", "task_id": "mock_1",
                                             "result": {"status": "ok", "output_jpg_path": str(out_jpg)}})

            if task_type == "retouch":
                state["retouch_calls"] += 1
                tiff_path = Path(payload["tiff_path"])
                plan = payload.get("plan") or {}
                state["received_plans"].append(plan)

                buf = np.fromfile(str(tiff_path), dtype=np.uint8)
                bgr16 = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
                img_f = bgr16.astype(np.float32) / 65535.0
                exp = float(plan.get("exposure_delta", 0.0))
                img_f = np.clip(img_f * (2.0 ** (exp * 0.25)), 0.0, 1.0)
                bgr8 = np.clip(img_f * 255.0, 0, 255).astype(np.uint8)

                result = {"status": "ok", "psd_saved": True, "applied": list(plan.keys()), "warnings": []}

                # 存 PSD（内容不重要，只要文件存在且非空，验证「主交付是 PSD」）
                psd_path = payload.get("output_psd_path")
                if psd_path:
                    p = Path(psd_path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"8BPS-mock-psd" + b"\x00" * 64)
                    result["output_psd_path"] = str(p)

                # 存预览 JPEG
                jpg_path = payload.get("output_preview_jpg_path")
                if jpg_path:
                    p = Path(jpg_path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    ok, enc = cv2.imencode(".jpg", bgr8, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
                    assert ok
                    enc.tofile(str(p))
                    result["output_preview_jpg_path"] = str(p)

                return self._send_json(200, {"status": "ok", "task_id": "mock_2", "result": result})

            return self._send_json(400, {"status": "error", "message": f"未知任务类型 {task_type}"})

    return ThreadingHTTPServer(("127.0.0.1", PS_BRIDGE_PORT), Handler)


def make_inpaint_server(state: Dict[str, Any]) -> ThreadingHTTPServer:
    """本地 Mock 局部 Patch 生图服务：验证仅接收小范围裁切 Patch 并返回干净背景补丁。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            auth_h = self.headers.get("Authorization", "")
            if auth_h != f"Bearer {INPAINT_KEY}":
                self.send_response(401)
                self.end_headers()
                return

            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            assert payload.get("mode") == "local_patch_only"

            patch_bytes = base64.b64decode(payload["patch_image_base64"])
            patch_pil = Image.open(io.BytesIO(patch_bytes)).convert("RGB")
            pw, ph = patch_pil.size
            state["calls"].append({"patch_size": [pw, ph], "prompt": payload.get("prompt"), "region_norm": payload.get("region_norm")})

            # 返回干净的深灰背景补丁以替换绿色安全出口牌
            clean_patch = Image.new("RGB", (pw, ph), color=(42, 46, 54))
            out_io = io.BytesIO()
            clean_patch.save(out_io, format="PNG")
            resp = json.dumps({"edited_patch_base64": base64.b64encode(out_io.getvalue()).decode("ascii")}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    return ThreadingHTTPServer(("127.0.0.1", INPAINT_PORT), Handler)


def start_booking_server(db_path: Path, preview_dir: Path, output_dir: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "PORT": str(BOOKING_PORT),
        "DB_PATH": str(db_path),
        "PREVIEW_OUTPUT_DIR": str(preview_dir),
        "RETOUCH_OUTPUT_DIR": str(output_dir),
        "IM_GATEWAY_URL": GATEWAY_URL,
        "INTERNAL_API_KEY": INTERNAL_KEY,
        "ADMIN_USER": ADMIN_USER,
        "ADMIN_PASSWORD": ADMIN_PASS,
        "PUSHPLUS_TOKEN": "",
    }
    proc = subprocess.Popen(
        ["node", str(BOOKING_SITE_DIR / "server.js")],
        cwd=str(BOOKING_SITE_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    deadline = time.monotonic() + 12.0
    with httpx.Client(timeout=1.0, trust_env=False) as probe:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise RuntimeError(f"booking-site 提前退出: {out} {err}")
            try:
                if probe.get(f"{BOOKING_URL}/api/settings").status_code == 200:
                    return proc
            except httpx.HTTPError:
                time.sleep(0.2)
    proc.kill()
    proc.wait(timeout=5)
    raise RuntimeError("booking-site 启动超时")


def start_real_im_gateway(gateway_db: Path, wx_adapter: RecordingIMAdapter) -> Tuple[uvicorn.Server, threading.Thread]:
    limiter = FriendRequestRateLimiter(db_path=gateway_db, dry_run=True)
    app = create_gateway_app(
        adapters={"wechat": wx_adapter, "qq": RecordingIMAdapter("qq")},
        rate_limiter=limiter,
        booking_api_url=BOOKING_URL,
        internal_api_key=INTERNAL_KEY,
        enable_background_workers=False,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=GATEWAY_PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    with httpx.Client(timeout=1.0, trust_env=False) as probe:
        while time.monotonic() < deadline:
            try:
                if probe.get(f"{GATEWAY_URL}/health").status_code == 200:
                    return server, thread
            except httpx.HTTPError:
                time.sleep(0.15)
    raise RuntimeError("im-gateway 启动超时")


def make_test_photo(out_path: Path, with_exit_sign: bool = False) -> str:
    """生成 1200x900 模拟原片，with_exit_sign=True 时在左上角绘制亮绿色安全出口杂物。"""
    img = np.full((900, 1200, 3), (54, 46, 42), dtype=np.uint8)  # BGR
    # 中心 Coser 主体（固定像素，用于校验局部修补绝不篡改中心主体）
    cv2.rectangle(img, (440, 260), (760, 820), (210, 175, 195), -1)
    cv2.circle(img, (600, 195), 75, (215, 225, 240), -1)
    if with_exit_sign:
        # 左上角亮绿色穿帮安全出口牌 [0.05..0.20, 0.05..0.20]
        cv2.rectangle(img, (60, 45), (240, 180), (40, 235, 40), -1)
        cv2.putText(img, "EXIT", (90, 130), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (255, 255, 255), 3)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)).save(out_path, format="JPEG", quality=95)
    return hashlib.sha256(out_path.read_bytes()).hexdigest()


def run_sprint4_e2e() -> None:
    print("=== Sprint 4 正式修图管线与人工终审交付闭环测试开始 ===")

    with tempfile.TemporaryDirectory(prefix="zero_sprint4_") as tmp_str:
        tmp_root = Path(tmp_str)
        booking_db = tmp_root / "booking_sprint4.db"
        gateway_db = tmp_root / "gateway_sprint4.db"
        nas_dir = tmp_root / "mock_nas" / "RAW"
        xmp_dir = tmp_root / "retouch_xmp"
        work_dir = tmp_root / "work"
        preview_dir = tmp_root / "previews"
        output_dir = tmp_root / "outputs"
        for d in (nas_dir, xmp_dir, work_dir, preview_dir, output_dir):
            d.mkdir(parents=True, exist_ok=True)

        # 准备正式修图 XMP 预设文件
        (xmp_dir / "cyan_air_event.xmp").write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
      crs:Exposure2012="+0.25"
      crs:Shadows2012="+20"
      crs:Highlights2012="-15"
      crs:Temperature="5200" />
  </rdf:RDF>
</x:xmpmeta>
""",
            encoding="utf-8",
        )

        # 准备 2 张模拟 NAS 原片（第 2 张左上角含绿色安全出口杂物）
        nas_raw_1 = nas_dir / "DSC0101.JPG"
        nas_raw_2 = nas_dir / "DSC0102.JPG"
        sha_raw_1 = make_test_photo(nas_raw_1, with_exit_sign=False)
        sha_raw_2 = make_test_photo(nas_raw_2, with_exit_sign=True)

        # 启动真实 booking-site、真实 im-gateway、本地 Mock PS Bridge & Mock Inpaint Server
        wx_recorder = RecordingIMAdapter("wechat")
        gw_server, gw_thread = start_real_im_gateway(gateway_db, wx_recorder)
        booking_proc = start_booking_server(booking_db, preview_dir, output_dir)

        ps_state: Dict[str, Any] = {
            "export_calls": 0,
            "retouch_calls": 0,
            "verified_16bit_tiffs": [],
            "received_plans": [],
            "received_batch_plays": [],
            "plugin_online": True,
            "task_types": [],
        }
        ps_server = make_ps_bridge_server(ps_state)
        ps_thread = threading.Thread(target=ps_server.serve_forever, daemon=True)
        ps_thread.start()

        inpaint_state: Dict[str, Any] = {"calls": []}
        inpaint_server = make_inpaint_server(inpaint_state)
        inpaint_thread = threading.Thread(target=inpaint_server.serve_forever, daemon=True)
        inpaint_thread.start()

        try:
            print(f"[1/6] 真实 booking-site({BOOKING_PORT})、im-gateway({GATEWAY_PORT}) 及 Mock PS/Inpaint 已就绪")

            with httpx.Client(timeout=20.0, trust_env=False) as client:
                int_headers = {"X-API-Key": INTERNAL_KEY, "Content-Type": "application/json"}

                # 1. 创建微信预约单 -> 回写 2 张有效资产 -> 客户选图锁单为 SELECTED
                r_book = client.post(
                    f"{BOOKING_URL}/api/internal/bookings",
                    headers=int_headers,
                    json={
                        "platform": "wechat",
                        "im_user_id": "wx_coser_final",
                        "wechat": "wx_coser_final",
                        "cn": "知更鸟CN",
                        "character": "知更鸟",
                        "event_name": "CP31",
                        "date": "2026-05-01",
                        "time": "10:00-12:00",
                        "deposit": "180",
                        "quantity": 2,
                    },
                )
                assert r_book.status_code == 201
                booking_id = int(r_book.json()["booking_id"])
                gallery_token = str(r_book.json()["gallery_token"])

                r_batch = client.post(
                    f"{BOOKING_URL}/api/internal/batches",
                    headers=int_headers,
                    json={
                        "batch_id": "20260501_CP31_ILCE-7M4_001",
                        "camera_model": "ILCE-7M4",
                        "event_name": "CP31",
                        "venue": "国展",
                        "nas_path": str(nas_dir),
                        "total_files": 2,
                        "verified_files": 2,
                        "assets": [
                            {
                                "filename": nas_raw_1.name,
                                "nas_raw_path": str(nas_raw_1),
                                "preview_url": "/previews/mock/1.webp",
                                "sha256": sha_raw_1,
                                "is_rejected": 0,
                                "cluster_id": f"qr_booking_{booking_id}",
                                "captured_at": "2026-05-01T10:05:00",
                                "booking_ids": [booking_id],
                            },
                            {
                                "filename": nas_raw_2.name,
                                "nas_raw_path": str(nas_raw_2),
                                "preview_url": "/previews/mock/2.webp",
                                "sha256": sha_raw_2,
                                "is_rejected": 0,
                                "cluster_id": f"qr_booking_{booking_id}",
                                "captured_at": "2026-05-01T10:05:20",
                                "booking_ids": [booking_id],
                            },
                        ],
                    },
                )
                assert r_batch.status_code == 201

                g_data = client.get(f"{BOOKING_URL}/api/gallery/{gallery_token}").json()
                selected_ids = [a["id"] for a in g_data["assets"]]
                assert len(selected_ids) == 2

                r_sel = client.post(
                    f"{BOOKING_URL}/api/gallery/{gallery_token}/select",
                    json={"asset_ids": selected_ids, "preset_code": "cyan_air_event"},
                )
                assert r_sel.status_code == 200 and r_sel.json()["booking_status"] == "SELECTED"
                print(f"[2/6] 订单 #{booking_id} 已完成客户选图并锁单为 SELECTED (preset=cyan_air_event, 2张)")

                # 2. 配置 Mock Gemini（第 1 张故意输出极端越界参数测试钳位；第 2 张触发局部蒙版 Patch 去出口牌）
                gemini_call_idx = {"n": 0}

                def mock_gemini_fn(ctx: Dict[str, Any]) -> Dict[str, Any]:
                    assert "default_cosplay_skill.md" in ctx["loaded_skills"], "应热加载 configs/retouch_skills/*.md"
                    assert "严禁整图重绘人脸与服设" in ctx["skills_text"]
                    gemini_call_idx["n"] += 1
                    if gemini_call_idx["n"] == 1:
                        # 故意返回严重越界参数，验证安全边界钳位保护
                        return {
                            "exposure_delta": 5.5,
                            "temp_delta": 80.0,
                            "tint_delta": -40.0,
                            "hsl_adjustments": {
                                "oranges": {"hue": 35, "saturation": 60, "luminance": 50},
                                "blues": {"hue": -5, "saturation": 12, "luminance": 4},
                            },
                            "skin_luminance": 45.0,
                            "curves_points": [[0, 0], [128, 240], [255, 255]],
                            "needs_local_inpaint": False,
                            "inpaint_region": None,
                            "inpaint_prompt": "",
                            "summary": "苍蓝通透微调（测试越界参数钳位保护）",
                        }
                    # 第 2 张：合法清冷场照参数 + 局部蒙版去除左上角绿色安全出口牌
                    return {
                        "exposure_delta": 0.25,
                        "temp_delta": -5.0,
                        "tint_delta": 2.0,
                        "hsl_adjustments": {
                            "oranges": {"hue": 2, "saturation": -4, "luminance": 6},
                            "blues": {"hue": -3, "saturation": 10, "luminance": 3},
                        },
                        "skin_luminance": 5.0,
                        "curves_points": [[0, 8], [64, 62], [128, 134], [192, 198], [255, 252]],
                        "needs_local_inpaint": True,
                        "inpaint_region": [0.04, 0.04, 0.22, 0.22],
                        "inpaint_prompt": "remove green exit sign on dark studio wall",
                        "summary": "苍蓝通透场照调色并局部消除左上角绿色出口牌",
                    }

                colorist = GeminiColorist(mock_fn=mock_gemini_fn)
                ps_client = PSUXPBridgeClient(
                    bridge_url=PS_BRIDGE_URL,
                    bridge_token=BRIDGE_TOKEN,
                    max_busy_retries=3,
                    retry_base_delay=0.1,
                )
                # 本测试验证「局域网模式」：显式屏蔽 .env 里可能存在的公网配置，
                # 否则 worker 会误切到随身修图机模式去连真实 Mac。
                _saved_sync = os.environ.pop("WORKER_SYNC_TOKEN", None)
                _saved_sync_url = os.environ.pop("WORKER_SYNC_URL", None)
                try:
                    worker = RetouchWorker(
                        booking_api_url=BOOKING_URL,
                        internal_api_key=INTERNAL_KEY,
                        work_dir=work_dir,
                        output_dir=output_dir,
                        retouch_xmp_dir=xmp_dir,
                        colorist=colorist,
                        ps_bridge=ps_client,
                        image_gen_api_url=INPAINT_URL,
                        image_gen_api_key=INPAINT_KEY,
                    )
                finally:
                    if _saved_sync is not None:
                        os.environ["WORKER_SYNC_TOKEN"] = _saved_sync
                    if _saved_sync_url is not None:
                        os.environ["WORKER_SYNC_URL"] = _saved_sync_url

                # 执行 worker --once
                run_res = worker.run_once()
                assert run_res["pending_bookings"] == 1
                assert len(run_res["processed_jobs"]) == 2

                # 校验第 1 张越界参数已被代码层严格钳位
                plan1 = run_res["processed_jobs"][0]["plan"]
                assert plan1["exposure_delta"] == 1.5
                assert plan1["temp_delta"] == 15.0
                assert plan1["tint_delta"] == -15.0
                assert plan1["skin_luminance"] == 15.0
                assert plan1["hsl_adjustments"]["oranges"]["hue"] == 8.0
                assert plan1["hsl_adjustments"]["oranges"]["saturation"] == 15.0
                assert plan1["curves_points"][1][1] == 128 + 35
                assert len(plan1["clamped_fields"]) >= 6

                # 校验 PS Bridge busy 重试生效 + 收到 2 个 16-bit TIFF + 2 次 batchPlay
                assert ps_state["export_calls"] == 3, "第1次应触发 503 重试，总计 3 次调用"
                assert len(ps_state["verified_16bit_tiffs"]) == 2
                assert ps_state["retouch_calls"] == 2

                # 校验第 2 张局部蒙版 Patch 生图已执行，且仅裁切局部小图发送（绝非整图）
                assert len(inpaint_state["calls"]) == 1
                pw, ph = inpaint_state["calls"][0]["patch_size"]
                assert pw < 400 and ph < 400, f"局部生图应仅裁切小 Patch，实际 {pw}x{ph}"
                # 主交付是 PSD（保留可编辑图层），像素检查用配套的预览 JPEG
                out2_psd = Path(run_res["processed_jobs"][1]["output_path"])
                assert out2_psd.suffix.lower() == ".psd", f"主交付应为 PSD，实际 {out2_psd.name}"
                assert out2_psd.exists() and out2_psd.stat().st_size > 0, "PSD 应真实落盘"
                out2_path = Path(run_res["processed_jobs"][1]["output_jpg_path"])
                out2_bgr = cv2.imdecode(np.fromfile(str(out2_path), dtype=np.uint8), cv2.IMREAD_COLOR)
                # 左上角绿色出口牌中心区域绿色通道应显著下降（已被干净暗灰背景替换）
                top_left_green = float(np.mean(out2_bgr[80:140, 100:200, 1]))
                assert top_left_green < 90.0, f"左上角绿色出口牌应已被局部消除，实际 G={top_left_green}"

                # 校验 NAS 原片 SHA-256 严格零修改
                assert hashlib.sha256(nas_raw_1.read_bytes()).hexdigest() == sha_raw_1
                assert hashlib.sha256(nas_raw_2.read_bytes()).hexdigest() == sha_raw_2
                print(
                    "[3/6] worker --once 跑通 XMP 16-bit TIFF -> PS busy重试/sRGB -> "
                    "Gemini热加载Skill+安全钳位 -> batchPlay -> 局部Patch生图(NAS原片零修改)"
                )

                # 3. 验证人工终审门禁：订单严格停留在 REVIEW_PENDING，且绝不提前推送客户！
                assert run_res["processed_jobs"][-1]["booking_status"] == "REVIEW_PENDING"
                assert len(wx_recorder.messages) == 0, "在摄影师人工终审放行前，绝不允许推送任何交付消息给客户！"

                # 未放行前客户访问 /api/delivery/:token 必须被拒 (404)
                pre_deliv = client.get(f"{BOOKING_URL}/api/delivery/{gallery_token}")
                assert pre_deliv.status_code == 404

                # 校验 review.html 与 /api/admin/review-queue 的 adminAuth 鉴权保护
                assert client.get(f"{BOOKING_URL}/review.html").status_code == 401
                assert client.get(f"{BOOKING_URL}/api/admin/review-queue").status_code == 401

                admin_auth = (ADMIN_USER, ADMIN_PASS)
                rev_html = client.get(f"{BOOKING_URL}/review.html", auth=admin_auth)
                assert rev_html.status_code == 200 and "摄影师人工终审与交付门禁台" in rev_html.text

                rev_q = client.get(f"{BOOKING_URL}/api/admin/review-queue", auth=admin_auth)
                assert rev_q.status_code == 200
                rev_data = rev_q.json()
                assert rev_data["pending_count"] == 1
                pending_b = rev_data["bookings"][0]
                assert pending_b["id"] == booking_id and pending_b["status"] == "REVIEW_PENDING"
                assert len(pending_b["jobs"]) == 2
                # 验证成片静态目录 /outputs/... 可直接访问
                out_http = client.get(f"{BOOKING_URL}{pending_b['jobs'][0]['preview_out_url']}")
                assert out_http.status_code == 200 and out_http.content[:2] == b"\xff\xd8"
                print("[4/6] 人工终审门禁验证通过（严格停留在 REVIEW_PENDING，未提前推客户，review.html 与队列鉴权生效）")

                # 4. 模拟摄影师在终审接口点击「一键放行交付」
                approve_resp = client.post(
                    f"{BOOKING_URL}/api/admin/bookings/{booking_id}/approve-delivery",
                    auth=admin_auth,
                )
                assert approve_resp.status_code == 200, f"放行交付失败: {approve_resp.status_code} {approve_resp.text}"
                app_data = approve_resp.json()
                assert app_data["status"] == "ok"
                assert app_data["booking_status"] == "DELIVERED"
                assert app_data["notify_result"]["ok"] is True
                delivery_url = app_data["delivery_url"]
                assert f"/delivery/{gallery_token}" in delivery_url
                print(f"[5/6] 摄影师一键放行交付成功 -> 状态流转为 DELIVERED，delivery_url={delivery_url}")

                # 5. 验证成品交付页可访问 + im-gateway 已成功将成片链接推送回微信客户 + SQLite 落盘一致
                deliv_api = client.get(f"{BOOKING_URL}/api/delivery/{gallery_token}")
                assert deliv_api.status_code == 200 and len(deliv_api.json()["outputs"]) == 2
                deliv_html = client.get(f"{BOOKING_URL}/delivery/{gallery_token}")
                assert deliv_html.status_code == 200 and "精修成片交付" in deliv_html.text

                assert len(wx_recorder.messages) == 1, f"期望客户收到 1 条交付推送，实际 {len(wx_recorder.messages)}"
                pushed = wx_recorder.messages[0]
                assert pushed["target_id"] == "wx_coser_final"
                assert delivery_url in pushed["text"]

                conn = sqlite3.connect(str(booking_db))
                try:
                    b_row = conn.execute(
                        "SELECT status, delivery_url FROM bookings_v3 WHERE id = ?",
                        (booking_id,),
                    ).fetchone()
                    assert b_row == ("DELIVERED", delivery_url)
                    passed_cnt = conn.execute(
                        "SELECT COUNT(*) FROM retouch_jobs WHERE booking_id = ? AND review_passed = 1",
                        (booking_id,),
                    ).fetchone()[0]
                    assert passed_cnt == 2
                finally:
                    conn.close()

                print("[6/6] im-gateway 成片交付推送与 /delivery/:token 成品页、数据库终态一致性验证通过")
            print("=== Sprint 4 全链路闭环冒烟测试全部通过 (6/6) ===")
        finally:
            ps_server.shutdown()
            ps_server.server_close()
            inpaint_server.shutdown()
            inpaint_server.server_close()
            gw_server.should_exit = True
            gw_thread.join(timeout=5)
            booking_proc.terminate()
            try:
                booking_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                booking_proc.kill()
                booking_proc.wait(timeout=5)


if __name__ == "__main__":
    run_sprint4_e2e()
