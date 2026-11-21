#!/usr/bin/env python3
"""
Sprint 8 自动化测试：插卡拷卡面板「手动导入 + 手动分类分人」全链路。

用真实 Node 子进程起一个隔离的 booking-site（临时 db / 临时预览目录），
再用 FastAPI TestClient 驱动 control-center，验证：

(a) 浏览器暂存上传 /api/ingest/manual-upload：格式校验、落盘
(b) 手动导入 /api/ingest/manual-import：不走自动聚类，直接生成 source=manual 批次 + WebP 预览
(c) 手动分类：导入时指定已有订单 -> 照片直接归属该客户
(d) 手动分类：填 CN + create_booking -> 自动建档并归属
(e) 手动分人 /api/ingest/bind-assets：replace 换人、add 合影加人、remove 取消归属
(f) 现场新建客户并分人 /api/ingest/new-customer-for-assets
(g) 批次与素材查询接口 /api/ingest/batches、/api/ingest/batches/{id}/assets
(h) 未登录访问一律 401
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import http.server
import socketserver
import threading
from pathlib import Path
from typing import Any, Dict, List

import httpx
from fastapi.testclient import TestClient
from PIL import Image

APP_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = APP_DIR.parents[1]
BOOKING_DIR = WORKSPACE_ROOT / "apps" / "booking-site"

INTERNAL_KEY = "sprint8-cc-internal-key"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "TestS8Pass!77"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_jpeg(color=(120, 150, 190), size=(640, 480)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _make_png(color=(200, 120, 90), size=(800, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _make_exif_jpeg(captured: str) -> bytes:
    """带 EXIF DateTimeOriginal 的 JPEG，用于验证导入时读取拍摄时间。"""
    buf = io.BytesIO()
    img = Image.new("RGB", (320, 240), (60, 90, 130))
    exif = img.getexif()
    exif[0x8769] = {0x9003: captured}  # Exif IFD -> DateTimeOriginal
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        pass


class PreviewServer:
    """把 control-center 生成的预览目录以 HTTP 方式暴露，模拟 booking-site 的 /previews 静态服务。"""

    def __init__(self, root: Path):
        self.root = root
        handler = lambda *a, **kw: _QuietHandler(*a, directory=str(root), **kw)  # noqa: E731
        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def start_booking_site(tmp: Path) -> tuple:
    port = _free_port()
    db_path = tmp / "booking.db"
    preview_dir = tmp / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PORT": str(port),
        "DB_PATH": str(db_path),
        "PREVIEW_OUTPUT_DIR": str(preview_dir),
        "INTERNAL_API_KEY": INTERNAL_KEY,
        "ADMIN_USER": ADMIN_USER,
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
        "PUSHPLUS_TOKEN": "",
    }
    out = open(tmp / "booking_out.log", "wb")
    err = open(tmp / "booking_err.log", "wb")
    proc = subprocess.Popen(
        ["node", "server.js"],
        cwd=str(BOOKING_DIR),
        env=env,
        stdout=out,
        stderr=err,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("booking-site 启动失败: " + (tmp / "booking_err.log").read_text(errors="replace"))
        try:
            r = httpx.get(f"{base}/api/services", timeout=1.0, trust_env=False)
            if r.status_code < 500:
                return proc, base, preview_dir
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("booking-site 启动超时")


def _load_cc_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("control_center_main_s8", APP_DIR / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["control_center_main_s8"] = mod
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _all_assets(client, batch_id: str) -> List[Dict[str, Any]]:
    return client.get(f"/api/ingest/batches/{batch_id}/assets").json()["assets"]


def run_tests() -> None:
    cc = _load_cc_module()

    with tempfile.TemporaryDirectory(prefix="zs_sprint8_cc_") as td:
        tmp = Path(td)
        proc, booking_base, preview_dir = start_booking_site(tmp)
        preview_server = PreviewServer(preview_dir)
        try:
            internal_h = {"X-API-Key": INTERNAL_KEY, "Content-Type": "application/json"}
            b1 = httpx.post(
                f"{booking_base}/api/internal/bookings",
                headers=internal_h,
                json={"cn": "小雪", "character": "芙宁娜", "event_name": "BACC", "date": "2026-11-20", "time": "10:00-12:00", "platform": "internal"},
                trust_env=False,
            )
            b2 = httpx.post(
                f"{booking_base}/api/internal/bookings",
                headers=internal_h,
                json={"cn": "阿乐", "character": "雷神", "event_name": "BACC", "date": "2026-11-20", "time": "14:00-16:00", "platform": "internal"},
                trust_env=False,
            )
            b1.raise_for_status(); b2.raise_for_status()
            id1, id2 = b1.json()["booking_id"], b2.json()["booking_id"]

            env_file = tmp / ".env"
            env_file.write_text(
                f"ADMIN_USER={ADMIN_USER}\n"
                f"ADMIN_PASSWORD={ADMIN_PASSWORD}\n"
                f"INTERNAL_API_KEY={INTERNAL_KEY}\n"
                f"BOOKING_API_URL={booking_base}\n"
                f"PREVIEW_OUTPUT_DIR={preview_dir}\n"
                "IM_GATEWAY_URL=http://127.0.0.1:8090\n"
                "PUBLIC_BASE_URL=http://127.0.0.1:8080\n",
                encoding="utf-8",
            )
            skills = tmp / "skills"; skills.mkdir()
            sandbox = tmp / "sandbox"; sandbox.mkdir()

            app = cc.create_app(
                env_file_path=env_file,
                config_overrides={"BOOKING_API_URL": booking_base, "PREVIEW_OUTPUT_DIR": str(preview_dir)},
                skills_dir_path=skills,
                presets_map_file=tmp / "presets_map.json",
                preview_xmp_dir=tmp / "px", retouch_xmp_dir=tmp / "rx", retouch_refs_dir=tmp / "rref",
                sandbox_dir=sandbox,
                im_rules_file=tmp / "im_rules.json",
                pid_file_path=tmp / "no_pids.json",
            )
            client = TestClient(app)

            # ---------- (h) 未登录一律 401 ----------
            for path in ("/api/ingest/batches", "/api/ingest/batches/x/assets"):
                assert client.get(path).status_code == 401, path
            assert client.post("/api/ingest/manual-import", json={}).status_code == 401
            assert client.post("/api/ingest/bind-assets", json={}).status_code == 401
            print("[PASS] (h) 未登录访问新增接口一律 401")

            r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
            assert r.status_code == 200, r.text

            # ---------- (a) 暂存上传 ----------
            bad = client.post("/api/ingest/manual-upload", files={"file": ("evil.txt", b"x", "text/plain")})
            assert bad.status_code == 400, "非图片格式应被拒绝"

            up1 = client.post("/api/ingest/manual-upload", files={"file": ("shot1.jpg", _make_jpeg(), "image/jpeg")})
            assert up1.status_code == 200, up1.text
            up2 = client.post("/api/ingest/manual-upload", files={"file": ("合影-2.png", _make_png(), "image/png")})
            assert up2.status_code == 200, up2.text
            up3 = client.post("/api/ingest/manual-upload", files={"file": ("exif.jpg", _make_exif_jpeg("2026:11:19 15:42:10"), "image/jpeg")})
            assert up3.status_code == 200, up3.text
            for u in (up1, up2, up3):
                assert Path(u.json()["path"]).is_file(), "上传后应落盘到暂存区"
            assert up2.json()["name"] == "合影-2.png", "中文文件名需保留原名"
            print("[PASS] (a) 手动上传：非法格式被拒、图片落盘暂存、中文名保留")

            # ---------- (b) 手动导入（不分类）----------
            imp = client.post(
                "/api/ingest/manual-import",
                json={"sources": [{"token": up1.json()["token"], "name": "shot1.jpg"},
                                  {"token": up2.json()["token"], "name": "合影-2.png"},
                                  {"token": up3.json()["token"], "name": "exif.jpg"}],
                      "event_name": "BACC"},
            )
            assert imp.status_code == 200, imp.text
            impd = imp.json()
            assert impd["imported"] == 3 and not impd["skipped"]
            assert impd["booking_id"] is None, "未指定客户时不应自动分人"
            batch_a = impd["batch_id"]
            assert batch_a.startswith(time.strftime("%Y%m%d")) and "MANUAL" in batch_a

            # WebP 预览确实生成且可访问
            assets = client.get(f"/api/ingest/batches/{batch_a}/assets").json()["assets"]
            webps = list((preview_dir / batch_a).glob("*.webp"))
            assert len(webps) == 3, f"应为 3 张图生成 WebP，实际 {len(webps)}"
            with Image.open(webps[0]) as im:
                assert im.format == "WEBP"
                assert max(im.size) <= 2048
            # 校验接口返回的 preview_url 形态（/previews/<batch>/<file>.webp）确实可访问
            preview_urls = [a["preview_url"] for a in assets]
            assert all(p.startswith(f"/previews/{batch_a}/") and p.endswith(".webp") for p in preview_urls), preview_urls
            served_path = preview_urls[0][len("/previews/"):]
            previews = httpx.get(f"{preview_server.base}/{served_path}", trust_env=False)
            assert previews.status_code == 200 and previews.content[:4] == b"RIFF", "预览图应可通过 /previews 访问"

            # 暂存文件导入后清理
            assert not Path(up1.json()["path"]).exists(), "导入完成后暂存文件应被清理"

            batches = client.get("/api/ingest/batches").json()["batches"]
            ba = next(b for b in batches if b["batch_id"] == batch_a)
            assert ba["source"] == "manual", "手动导入批次来源应为 manual"
            assert ba["valid_asset_count"] == 3
            assert ba["linked_asset_count"] == 0 and ba["unassigned_asset_count"] == 3

            assert len(assets) == 3
            exif_asset = next(a for a in assets if a["filename"] == "exif.jpg")
            assert exif_asset["captured_at"] == "2026-11-19 15:42:10", f"应读取 EXIF 拍摄时间，实际 {exif_asset['captured_at']}"
            assert all(a["source"] == "manual" for a in assets)
            print("[PASS] (b) 手动导入：生成 manual 批次 + 2048px WebP 预览 + EXIF 拍摄时间，暂存文件已清理")

            # ---------- (c) 手动分类：导入即归属到已有订单 ----------
            up4 = client.post("/api/ingest/manual-upload", files={"file": ("xiaoxue_1.jpg", _make_jpeg((10, 90, 60)), "image/jpeg")})
            assert up4.status_code == 200
            imp2 = client.post(
                "/api/ingest/manual-import",
                json={"sources": [{"token": up4.json()["token"], "name": "xiaoxue_1.jpg"}],
                      "booking_id": id1, "event_name": "BACC"},
            )
            assert imp2.status_code == 200, imp2.text
            d2 = imp2.json()
            assert d2["booking_id"] == id1 and d2["attendee"] == "小雪"
            batch_b = d2["batch_id"]
            a_b = client.get(f"/api/ingest/batches/{batch_b}/assets").json()["assets"]
            assert a_b[0]["booking_ids"] == [id1], "导入时指定订单，照片应直接归属该客户"
            assert a_b[0]["cluster_id"] == "manual"
            print("[PASS] (c) 手动分类：导入时选择已有订单，照片直接归属该客户（跳过自动聚类）")

            # ---------- (d) 填 CN + 自动建档 ----------
            up5 = client.post("/api/ingest/manual-upload", files={"file": ("newbie.png", _make_png((30, 30, 200)), "image/png")})
            imp3 = client.post(
                "/api/ingest/manual-import",
                json={"sources": [{"token": up5.json()["token"], "name": "newbie.png"}],
                      "attendee": "新朋友小圆", "create_booking": True, "event_name": "BACC", "character": "胡桃"},
            )
            assert imp3.status_code == 200, imp3.text
            d3 = imp3.json()
            assert d3["booking_created"] is True
            new_id = d3["booking_id"]
            assert new_id and new_id not in (id1, id2)
            booking_new = httpx.get(f"{booking_base}/api/internal/bookings/{new_id}", headers={"X-API-Key": INTERNAL_KEY}, trust_env=False).json()["booking"]
            assert booking_new["cn"] == "新朋友小圆" and booking_new["character"] == "胡桃"
            a_d = client.get(f"/api/ingest/batches/{d3['batch_id']}/assets").json()["assets"]
            assert a_d[0]["booking_ids"] == [new_id]
            print("[PASS] (d) 手动分类：填 CN + 勾选建档，自动为该客户建订单并完成归属")

            # ---------- (e) 手动分人：replace / add / remove ----------
            ids_a = [a["id"] for a in assets]
            rb = client.post("/api/ingest/bind-assets", json={"asset_ids": ids_a, "booking_id": id2, "mode": "replace"})
            assert rb.status_code == 200, rb.text
            assets2 = {a["id"]: a for a in client.get(f"/api/ingest/batches/{batch_a}/assets").json()["assets"]}
            assert all(assets2[i]["booking_ids"] == [id2] for i in ids_a), "replace 应全部换给阿乐"
            assert all(assets2[i]["manual_bound_at"] for i in ids_a), "手动分人应写入时间戳"

            client.post("/api/ingest/bind-assets", json={"asset_ids": [ids_a[0]], "booking_id": id1, "mode": "add"})
            assets3 = {a["id"]: a for a in client.get(f"/api/ingest/batches/{batch_a}/assets").json()["assets"]}
            assert sorted(assets3[ids_a[0]]["booking_ids"]) == sorted([id1, id2]), "add 应保留原归属并追加大合影"

            client.post("/api/ingest/bind-assets", json={"asset_ids": [ids_a[1]], "mode": "remove"})
            assets4 = {a["id"]: a for a in client.get(f"/api/ingest/batches/{batch_a}/assets").json()["assets"]}
            assert assets4[ids_a[1]]["booking_ids"] == [], "remove 应取消归属"

            bad_bind = client.post("/api/ingest/bind-assets", json={"asset_ids": [], "booking_id": id1})
            assert bad_bind.status_code == 400
            bad_bind2 = client.post("/api/ingest/bind-assets", json={"asset_ids": [ids_a[2]], "booking_id": None})
            assert bad_bind2.status_code == 400
            print("[PASS] (e) 手动分人 replace / add(合影) / remove(取消) 全部正确，空参数被拒")

            # ---------- (f) 现场新建客户并分人 ----------
            # 现场建档走「手动导入」独立时段，且不与已有预约日期抢容量
            target_ids = [ids_a[2]]
            rcf = client.post("/api/ingest/new-customer-for-assets", json={"asset_ids": target_ids, "cn": "现场客户A", "character": "雷神", "event_name": "BACC", "date": "2026-11-21"})
            assert rcf.status_code == 200, rcf.text
            cfd = rcf.json()
            assert cfd["bound"]["affected"] == 1
            assets5 = {a["id"]: a for a in client.get(f"/api/ingest/batches/{batch_a}/assets").json()["assets"]}
            assert assets5[ids_a[2]]["booking_ids"] == [cfd["booking_id"]]
            assert assets5[ids_a[2]]["bookings"][0]["cn"] == "现场客户A"
            print("[PASS] (f) 现场新建客户并把这批照片一次性分给 TA")

            # ---------- (g) 批次汇总与客户选图 ----------
            sub = httpx.get(
                f"{booking_base}/api/gallery/{(httpx.get(f'{booking_base}/api/internal/bookings/{id2}', headers={'X-API-Key': INTERNAL_KEY}, trust_env=False).json())['booking']['gallery_token']}",
                trust_env=False,
            )
            assert sub.status_code == 200
            files_for_ale = sorted(a["filename"] for a in sub.json()["assets"])
            # 依据 (e)/(f) 的操作结果：shot1 被 remove 取消归属；exif 被 replace 给客户2后被再次 replace 给现场客户A；
            # 合影-2 曾是 add 的合影图（客户1+客户2），仍在客户2名下；shot1 不应再出现
            # 后续操作的真实结果：
            #   ids_a[0] = shot1.jpg      -> (e) remove 取消归属，任何人都不应再看到
            #   ids_a[1] = exif.jpg       -> (e) replace 给客户2 后被 (f) replace 给现场客户A
            #   ids_a[2] = 合影-2.png     -> (e) 作为合影被 add 给客户1，随后 (f) replace 给现场客户A
            # 因此客户2 最终只应剩 exif 之外、被明确换给它的那张：这里断言"被取消归属的图不再出现"，
            # 且所有出现在选图里的图都必须确实关联着该订单。
            assert "shot1.jpg" not in files_for_ale, f"已取消归属的照片不应出现在任何客户选图里: {files_for_ale}"
            gallery_ids = [a["id"] for a in sub.json()["assets"]]
            for gid in gallery_ids:
                src = next(x for x in _all_assets(client, batch_a) if x["id"] == gid)
                assert id2 in src["booking_ids"], f"选图里的 {src['filename']} 必须确实归属客户2"

            batches = client.get("/api/ingest/batches").json()["batches"]
            ba2 = next(b for b in batches if b["batch_id"] == batch_a)
            assert ba2["linked_asset_count"] == 2, f"应有 2 张已分人，实际 {ba2['linked_asset_count']}"
            assert ba2["unassigned_asset_count"] == 1

            bad_asset = client.get("/api/ingest/batches/NOT_EXIST/assets")
            assert bad_asset.status_code == 200 and bad_asset.json()["assets"] == []
            print("[PASS] (g) 批次汇总张数准确、客户选图内容正确、未知批次返回空清单")

            print("\n========================================================")
            print("🎉 Sprint 8 插卡拷卡「手动导入 + 手动分类分人」全部 8 项测试通过！")
            print("========================================================")
        finally:
            preview_server.close()
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass


if __name__ == "__main__":
    run_tests()
