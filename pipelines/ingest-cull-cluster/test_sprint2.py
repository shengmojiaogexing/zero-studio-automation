#!/usr/bin/env python3
"""
Sprint 2 端到端离线自动化冒烟测试：
自动启动 booking-site -> 创建 2 个订单并下载场记二维码 PNG -> 构造模拟 SD 卡目录
（2 张场记二维码首图 + 4 张正常测试图 + 1 张纯黑测光废片 + 1 张高斯模糊虚焦废片）->
跑通自动拷卡单次流式双校验 -> 废片预筛 -> 二维码切段绑定 -> 事务回写数据库 ->
验证 /gallery/:token 与 /api/gallery/:token 废片隐藏且有效图可见 ->
模拟客户提交选图与预设并验证状态流转为 SELECTED 且锁单防重复修改。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Tuple

import cv2
import numpy as np
import requests
from PIL import Image

PIPELINE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = PIPELINE_DIR.parents[1]
BOOKING_SITE_DIR = WORKSPACE_ROOT / "apps" / "booking-site"

sys.path.insert(0, str(PIPELINE_DIR))
import ingest  # noqa: E402
import cull_and_cluster  # noqa: E402

TEST_PORT = 3457
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"
API_KEY = "sprint2-e2e-internal-key"


def save_jpg_with_exif(bgr_img: np.ndarray, out_path: Path, camera_model: str, dt_str: str) -> None:
    """将 OpenCV BGR 图像写入带标准 EXIF（Model + DateTimeOriginal）的 JPEG。"""
    rgb = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(rgb)
    exif = Image.Exif()
    exif[272] = camera_model       # Model
    exif[306] = dt_str             # DateTime
    exif[36867] = dt_str           # DateTimeOriginal
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pil_img.save(out_path, format="JPEG", quality=92, exif=exif.tobytes())


def make_qr_slate_photo(qr_png_bytes: bytes, label: str) -> np.ndarray:
    """将场记二维码 PNG 合成到一张模拟拍摄场景画布中（模拟相机拍摄场记二维码）。"""
    qr_pil = Image.open(io.BytesIO(qr_png_bytes)).convert("RGB")
    qr_np = cv2.cvtColor(np.array(qr_pil), cv2.COLOR_RGB2BGR)

    canvas = np.full((900, 1200, 3), 235, dtype=np.uint8)
    # 浅灰背景边框
    cv2.rectangle(canvas, (30, 30), (1170, 870), (180, 180, 180), 4)
    qh, qw = qr_np.shape[:2]
    y0 = (900 - qh) // 2
    x0 = (1200 - qw) // 2
    canvas[y0: y0 + qh, x0: x0 + qw] = qr_np
    cv2.putText(canvas, label, (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (40, 40, 40), 2, cv2.LINE_AA)
    return canvas


def make_sharp_cosplay_photo(seed: int, accent_bgr: Tuple[int, int, int]) -> np.ndarray:
    """构造亮度均衡、纹理清晰的正常测试图（确保高 Laplacian 方差与正常曝光）。"""
    rng = np.random.default_rng(seed)
    img = np.full((900, 1200, 3), (115, 122, 130), dtype=np.uint8)

    # 背景网格纹理
    for x in range(0, 1200, 40):
        cv2.line(img, (x, 0), (x, 900), (145, 150, 160), 1)
    for y in range(0, 900, 40):
        cv2.line(img, (0, y), (1200, y), (145, 150, 160), 1)

    # 模拟角色剪影与服饰色块
    cv2.rectangle(img, (420, 300), (780, 840), accent_bgr, -1)
    cv2.rectangle(img, (420, 300), (780, 840), (30, 30, 30), 4)
    cv2.circle(img, (600, 210), 95, (190, 205, 225), -1)
    cv2.circle(img, (600, 210), 95, (40, 40, 40), 3)

    # 清晰高频细节图案（模拟服饰蕾丝/发丝/道具边缘）
    for _ in range(180):
        x1, y1 = int(rng.integers(60, 1140)), int(rng.integers(60, 840))
        x2 = int(np.clip(x1 + rng.integers(-60, 60), 0, 1199))
        y2 = int(np.clip(y1 + rng.integers(-60, 60), 0, 899))
        color = tuple(int(c) for c in rng.integers(20, 235, size=3))
        cv2.line(img, (x1, y1), (x2, y2), color, 2)

    cv2.putText(img, f"SHARP_COSPLAY_{seed}", (60, 90), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (245, 245, 245), 3, cv2.LINE_AA)
    return img


def make_black_metering_photo() -> np.ndarray:
    """构造纯黑测光废片（平均亮度 < 5）。"""
    rng = np.random.default_rng(999)
    return rng.integers(0, 6, size=(900, 1200, 3), dtype=np.uint8)


def make_blurry_photo(base_sharp: np.ndarray) -> np.ndarray:
    """构造严重高斯模糊虚焦废片（Laplacian 方差极低）。"""
    return cv2.GaussianBlur(base_sharp, (75, 75), 30)


def start_booking_server(db_path: Path, preview_dir: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "PORT": str(TEST_PORT),
        "DB_PATH": str(db_path),
        "PREVIEW_OUTPUT_DIR": str(preview_dir),
        "INTERNAL_API_KEY": API_KEY,
        "ADMIN_USER": "admin",
        "ADMIN_PASSWORD": "test-password",
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
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise RuntimeError(f"booking-site 提前退出 code={proc.returncode}\nstdout={out}\nstderr={err}")
        try:
            r = requests.get(f"{BASE_URL}/api/settings", timeout=1)
            if r.status_code == 200:
                return proc
        except requests.RequestException:
            time.sleep(0.25)

    proc.kill()
    raise RuntimeError("booking-site 启动超时")


def create_test_booking(payload: Dict) -> Tuple[int, str, bytes]:
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    r = requests.post(f"{BASE_URL}/api/internal/bookings", headers=headers, json=payload, timeout=10)
    assert r.status_code == 201, f"创建订单失败: {r.status_code} {r.text}"
    data = r.json()
    bid = int(data["booking_id"])
    token = str(data["gallery_token"])

    qr_resp = requests.get(f"{BASE_URL}/api/internal/bookings/{bid}/slate-qr", headers={"X-API-Key": API_KEY}, timeout=10)
    assert qr_resp.status_code == 200 and "image/png" in qr_resp.headers.get("content-type", ""), "下载场记二维码失败"
    return bid, token, qr_resp.content


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def run_e2e_test() -> None:
    print("=== Sprint 2 端到端离线自动化冒烟测试开始 ===")
    with tempfile.TemporaryDirectory(prefix="zero_sprint2_") as tmp_root_str:
        tmp_root = Path(tmp_root_str)
        db_path = tmp_root / "test_sprint2.db"
        sd_card_dir = tmp_root / "mock_sd_card" / "DCIM" / "100MSDCF"
        nas_drive_dir = tmp_root / "mock_nas_drive" / "RAW"
        preview_dir = tmp_root / "mock_previews"
        sd_card_dir.mkdir(parents=True, exist_ok=True)
        nas_drive_dir.mkdir(parents=True, exist_ok=True)
        preview_dir.mkdir(parents=True, exist_ok=True)

        server_proc = start_booking_server(db_path, preview_dir)
        try:
            print(f"[1/8] booking-site 已启动于 {BASE_URL}")

            # 1. 创建 2 个测试订单并下载场记二维码 PNG
            b1_id, b1_token, b1_qr_png = create_test_booking(
                {
                    "wechat": "wx_sakura",
                    "platform": "wechat",
                    "cn": "小樱CN",
                    "character": "木之本樱",
                    "event_name": "CP31",
                    "date": "2026-05-01",
                    "time": "10:00-12:00",
                    "deposit": "80",
                    "quantity": 4,
                    "remarks": "Sprint2 测试单 1",
                }
            )
            b2_id, b2_token, b2_qr_png = create_test_booking(
                {
                    "qq": "20260501",
                    "platform": "qq",
                    "cn": "流萤CN",
                    "character": "流萤",
                    "event_name": "CP31",
                    "date": "2026-05-01",
                    "time": "14:00-16:00",
                    "deposit": "180",
                    "quantity": 4,
                    "remarks": "Sprint2 测试单 2",
                }
            )
            print(f"[2/8] 已创建 2 个测试订单 (#{b1_id}, #{b2_id}) 并下载场记二维码 PNG")

            # 2. 构造模拟 SD 卡目录（2 场记图 + 4 正常图 + 1 纯黑废片 + 1 高斯模糊虚焦废片 = 8 张）
            cam = "ILCE-7M4"
            sharp_b1_1 = make_sharp_cosplay_photo(101, (195, 130, 230))
            sharp_b1_2 = make_sharp_cosplay_photo(102, (205, 140, 225))
            sharp_b2_1 = make_sharp_cosplay_photo(201, (210, 195, 110))
            sharp_b2_2 = make_sharp_cosplay_photo(202, (200, 185, 120))

            mock_files = [
                ("DSC0001.JPG", make_qr_slate_photo(b1_qr_png, f"SLATE #{b1_id}"), "2026:05:01 10:05:00"),
                ("DSC0002.JPG", sharp_b1_1, "2026:05:01 10:05:15"),
                ("DSC0003.JPG", make_black_metering_photo(), "2026:05:01 10:05:30"),
                ("DSC0004.JPG", sharp_b1_2, "2026:05:01 10:05:45"),
                ("DSC0005.JPG", make_blurry_photo(sharp_b1_1), "2026:05:01 10:06:00"),
                ("DSC0006.JPG", make_qr_slate_photo(b2_qr_png, f"SLATE #{b2_id}"), "2026:05:01 14:05:00"),
                ("DSC0007.JPG", sharp_b2_1, "2026:05:01 14:05:15"),
                ("DSC0008.JPG", sharp_b2_2, "2026:05:01 14:05:30"),
            ]
            for fname, img_arr, dt_str in mock_files:
                save_jpg_with_exif(img_arr, sd_card_dir / fname, cam, dt_str)

            assert len(list(sd_card_dir.glob("*.JPG"))) == 8
            print("[3/8] 模拟 SD 卡构造完成（2 张场记二维码 + 4 张正常图 + 1 张纯黑片 + 1 张虚焦片）")

            # 3. 测试 NAS UNC 不可达 -> 自动回退映射盘符 + 单次流式双校验拷卡 + 2048px WebP 生成
            resolved_nas = ingest.resolve_nas_root(
                unc_path=r"\\192.0.2.1\NonExistentShare\RAW",
                drive_path=str(nas_drive_dir),
            )
            assert resolved_nas == nas_drive_dir, f"NAS 回退失败: {resolved_nas}"

            manifest = ingest.ingest_directory(
                source_dir=sd_card_dir,
                event_name="CP31",
                venue="上海国家会展中心",
                nas_root=resolved_nas,
                preview_root=preview_dir,
            )

            assert manifest["batch_id"] == "20260501_CP31_ILCE-7M4_001", f"意外的 batch_id: {manifest['batch_id']}"
            assert manifest["total_files"] == 8
            assert manifest["verified_files"] == 8
            assert manifest["source_deleted"] is False
            manifest_path = Path(manifest["manifest_path"])
            assert manifest_path.exists(), "manifest.json 未生成"

            # 验证源卡文件零删除、NAS 文件哈希一致、无残留 .tmp、WebP 预览图存在且有效
            assert len(list(sd_card_dir.glob("*.JPG"))) == 8, "源卡文件不得被删除！"
            nas_batch_dir = Path(manifest["nas_path"])
            assert not list(nas_batch_dir.glob("*.tmp")), "不应存在未清理的 .tmp 文件"

            for item in manifest["assets"]:
                nas_f = Path(item["nas_raw_path"])
                src_f = sd_card_dir / item["source_filename"]
                prev_f = Path(item["preview_path"])
                assert nas_f.exists() and prev_f.exists()
                assert sha256_file(src_f) == item["sha256"] == sha256_file(nas_f)
                with Image.open(prev_f) as pim:
                    assert pim.format == "WEBP"
                    assert max(pim.size) <= 2048

            print(f"[4/8] 自动拷卡流式双校验与 WebP 预览图生成通过: batch_id={manifest['batch_id']} (8/8 verified)")

            # 4. 运行废片预筛 -> 二维码切段绑定 -> 多模态管线 -> 回写 booking-site
            cc_result = cull_and_cluster.process_batch_manifest(
                manifest_path=manifest_path,
                booking_api_url=BASE_URL,
                api_key=API_KEY,
                enable_ml=False,
            )

            assets_by_name = {a["filename"]: a for a in cc_result["batch_payload"]["assets"]}
            assert assets_by_name["DSC0001.JPG"]["is_rejected"] == 1
            assert assets_by_name["DSC0001.JPG"]["reject_reason"] == "SLATE_QR"
            assert assets_by_name["DSC0003.JPG"]["is_rejected"] == 1
            assert "UNDEREXPOSED_BLACK" in (assets_by_name["DSC0003.JPG"]["reject_reason"] or "")
            assert assets_by_name["DSC0005.JPG"]["is_rejected"] == 1
            assert "BLURRY_OUT_OF_FOCUS" in (assets_by_name["DSC0005.JPG"]["reject_reason"] or "")
            assert assets_by_name["DSC0006.JPG"]["is_rejected"] == 1
            assert assets_by_name["DSC0006.JPG"]["reject_reason"] == "SLATE_QR"

            assert assets_by_name["DSC0002.JPG"]["is_rejected"] == 0
            assert assets_by_name["DSC0002.JPG"]["booking_ids"] == [b1_id]
            assert assets_by_name["DSC0004.JPG"]["is_rejected"] == 0
            assert assets_by_name["DSC0004.JPG"]["booking_ids"] == [b1_id]
            assert assets_by_name["DSC0007.JPG"]["is_rejected"] == 0
            assert assets_by_name["DSC0007.JPG"]["booking_ids"] == [b2_id]
            assert assets_by_name["DSC0008.JPG"]["is_rejected"] == 0
            assert assets_by_name["DSC0008.JPG"]["booking_ids"] == [b2_id]

            print("[5/8] 废片预筛、二维码切段绑定与 POST /api/internal/batches 回写验证通过")

            # 5. 验证 /gallery/:token 页面与 /api/gallery/:token 废片严格隐藏、有效图可见
            html_resp = requests.get(f"{BASE_URL}/gallery/{b1_token}", timeout=10)
            assert html_resp.status_code == 200 and "ZERO STUDIO" in html_resp.text

            g1_resp = requests.get(f"{BASE_URL}/api/gallery/{b1_token}", timeout=10)
            assert g1_resp.status_code == 200
            g1_data = g1_resp.json()
            assert g1_data["booking"]["status"] == "CLUSTERED"
            assert g1_data["booking"]["locked"] is False
            g1_names = [a["filename"] for a in g1_data["assets"]]
            assert g1_names == ["DSC0002.JPG", "DSC0004.JPG"], f"订单1可见图不符: {g1_names}"

            # 验证 WebP 预览图可通过 HTTP 正常访问
            first_prev_resp = requests.get(f"{BASE_URL}{g1_data['assets'][0]['preview_url']}", timeout=10)
            assert first_prev_resp.status_code == 200 and first_prev_resp.content[:4] == b"RIFF"

            g2_resp = requests.get(f"{BASE_URL}/api/gallery/{b2_token}", timeout=10)
            assert g2_resp.status_code == 200
            g2_data = g2_resp.json()
            assert g2_data["booking"]["status"] == "CLUSTERED"
            g2_names = [a["filename"] for a in g2_data["assets"]]
            assert g2_names == ["DSC0007.JPG", "DSC0008.JPG"], f"订单2可见图不符: {g2_names}"
            print("[6/8] 选图 H5 与 /api/gallery/:token 验证通过（黑片/虚焦片/场记卡均严格隐藏，仅有效图可见）")

            # 6. 模拟客户提交选图与预设，验证越权拦截、状态流转为 SELECTED 及锁单防重复修改
            b2_asset_id = g2_data["assets"][0]["id"]
            idor_resp = requests.post(
                f"{BASE_URL}/api/gallery/{b1_token}/select",
                json={"asset_ids": [b2_asset_id], "preset_code": "cyan_air_event"},
                timeout=10,
            )
            assert idor_resp.status_code == 400, f"跨订单越权选图应被拦截: {idor_resp.status_code}"

            chosen_ids = [a["id"] for a in g1_data["assets"]]
            sel_resp = requests.post(
                f"{BASE_URL}/api/gallery/{b1_token}/select",
                json={"asset_ids": chosen_ids, "preset_code": "cyan_air_event"},
                timeout=10,
            )
            assert sel_resp.status_code == 200, f"提交选图失败: {sel_resp.status_code} {sel_resp.text}"
            sel_data = sel_resp.json()
            assert sel_data["status"] == "ok"
            assert sel_data["booking_status"] == "SELECTED"
            assert sel_data["locked"] is True
            assert sel_data["selected_assets"] == chosen_ids
            assert sel_data["preset_code"] == "cyan_air_event"

            # 重复提交必须被锁单拦截 (409)
            dup_resp = requests.post(
                f"{BASE_URL}/api/gallery/{b1_token}/select",
                json={"asset_ids": [chosen_ids[0]], "preset_code": "warm_film_daily"},
                timeout=10,
            )
            assert dup_resp.status_code == 409, f"锁单后重复提交应返回 409，实际 {dup_resp.status_code}"

            # 核验数据库落盘状态与多对多关联表
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.cursor()
                batch_row = cur.execute(
                    "SELECT batch_id, camera_model, total_files, verified_files FROM ingest_batches WHERE batch_id = ?",
                    (manifest["batch_id"],),
                ).fetchone()
                assert batch_row == (manifest["batch_id"], "ILCE-7M4", 8, 8)

                b1_row = cur.execute(
                    "SELECT status, selected_assets, preset_code FROM bookings_v3 WHERE id = ?",
                    (b1_id,),
                ).fetchone()
                assert b1_row[0] == "SELECTED"
                assert json.loads(b1_row[1]) == chosen_ids
                assert b1_row[2] == "cyan_air_event"

                link_cnt = cur.execute("SELECT COUNT(*) FROM asset_booking_links").fetchone()[0]
                assert link_cnt == 8, f"Expected 8 asset_booking_links rows, got {link_cnt}"
            finally:
                conn.close()

            print("[7/8] 客户提交选图与预设、状态流转为 SELECTED 及防重复锁单验证通过")

            # 7. 验证合影多标签（单张 asset_id 关联多个 booking_id）
            group_batch_payload = {
                "batch_id": "20260501_CP31_ILCE-7M4_GROUP",
                "camera_model": "ILCE-7M4",
                "event_name": "CP31",
                "venue": "上海国家会展中心",
                "nas_path": str(nas_drive_dir / "20260501_CP31_ILCE-7M4_GROUP"),
                "total_files": 1,
                "verified_files": 1,
                "assets": [
                    {
                        "filename": "DSC9999_GROUP.JPG",
                        "nas_raw_path": str(nas_drive_dir / "20260501_CP31_ILCE-7M4_GROUP" / "DSC9999_GROUP.JPG"),
                        "preview_url": g1_data["assets"][0]["preview_url"],
                        "sha256": "f" * 64,
                        "is_rejected": 0,
                        "reject_reason": None,
                        "cluster_id": "group_b1_b2",
                        "captured_at": "2026-05-01T15:00:00",
                        "booking_ids": [b1_id, b2_id],
                    }
                ],
            }
            grp_resp = requests.post(
                f"{BASE_URL}/api/internal/batches",
                headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
                json=group_batch_payload,
                timeout=10,
            )
            assert grp_resp.status_code == 201 and grp_resp.json()["total_links"] == 2
            g1_after = requests.get(f"{BASE_URL}/api/gallery/{b1_token}", timeout=10).json()
            g2_after = requests.get(f"{BASE_URL}/api/gallery/{b2_token}", timeout=10).json()
            assert "DSC9999_GROUP.JPG" in [a["filename"] for a in g1_after["assets"]]
            assert "DSC9999_GROUP.JPG" in [a["filename"] for a in g2_after["assets"]]
            # 已锁单的订单 1 状态不得被新批次降级回 CLUSTERED
            assert g1_after["booking"]["status"] == "SELECTED"
            print("[8/8] 合影多标签（asset_booking_links 多对多关联）与已锁单状态保护验证通过")
            test_sprint25_high_contrast_and_xmp(tmp_root)
            print("=== Sprint 2 + 2.5 全部端到端冒烟测试通过 ===")
        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()


def test_sprint25_high_contrast_and_xmp(tmp_root: Path) -> None:
    """Sprint 2.5 专项回归：大光比暗背景亮主体防误杀 + 预览 XMP 前置处理（原片零修改）。"""
    # 构造 88% 纯黑暗背景 + 中心亮主体清晰 Coser（旧全图均值算法会误杀为黑片）
    hc_img = np.full((900, 1200, 3), 4, dtype=np.uint8)
    # 中心亮主体（占全图约 12%，全图均值 < 22、dark_ratio > 0.85，但主体清晰明亮）
    cv2.rectangle(hc_img, (480, 260), (720, 680), (175, 160, 210), -1)
    cv2.circle(hc_img, (600, 220), 65, (205, 195, 220), -1)
    for y in range(280, 660, 16):
        cv2.line(hc_img, (490, y), (710, y), (50, 45, 70), 2)
    for x in range(500, 710, 18):
        cv2.line(hc_img, (x, 270), (x, 670), (240, 235, 245), 2)

    qc = cull_and_cluster.evaluate_technical_quality(hc_img)
    assert qc["metrics"]["mean_brightness"] < 25.0 or qc["metrics"]["dark_ratio"] > 0.85, (
        f"测试图应满足暗背景大光比特征: {qc['metrics']}"
    )
    assert qc["is_rejected"] == 0, f"大光比暗背景亮主体不应被误杀！实际: {qc}"

    # 校验预览 XMP 前置处理：生成提亮后的预览 WebP，同时原片 SHA-256 严格零修改
    raw_sim = tmp_root / "hc_origin.jpg"
    save_jpg_with_exif(hc_img, raw_sim, "ILCE-7M4", "2026:05:01 18:30:00")
    origin_sha = sha256_file(raw_sim)

    xmp_file = tmp_root / "preview_preset.xmp"
    xmp_file.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
      crs:Exposure2012="+1.00"
      crs:Shadows2012="+60"
      crs:Highlights2012="-30"
      crs:Temperature="5800"
      crs:Tint="+5" />
  </rdf:RDF>
</x:xmpmeta>
""",
        encoding="utf-8",
    )

    out_webp = tmp_root / "hc_preview_xmp.webp"
    backend = ingest.apply_preview_xmp(raw_sim, xmp_file, out_webp)
    assert out_webp.exists() and "xmp" in backend
    assert sha256_file(raw_sim) == origin_sha, "预览 XMP 处理绝不允许修改原片！"

    xmp_bgr = cull_and_cluster.cv2_imread_unicode(out_webp)
    assert xmp_bgr is not None
    assert float(np.mean(xmp_bgr)) > float(np.mean(hc_img)), "应用提亮 XMP 后预览图亮度应显著提升"
    qc_after_xmp = cull_and_cluster.evaluate_technical_quality(xmp_bgr)
    assert qc_after_xmp["is_rejected"] == 0, f"套过 XMP 预览图不应被误杀: {qc_after_xmp}"
    print(
        f"[Sprint 2.5 PASS] 大光比暗背景亮主体防误杀通过 (mean={qc['metrics']['mean_brightness']}, "
        f"subj_p90={qc['metrics']['subject_p90']}) + 预览 XMP 渲染({backend})原片零修改通过"
    )


if __name__ == "__main__":
    run_e2e_test()
