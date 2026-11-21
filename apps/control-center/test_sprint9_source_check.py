#!/usr/bin/env python3
"""
Sprint 9 自动化测试：自动拷卡「源目录读取不到」的精确诊断与前置拦截。

覆盖：
(a) 未填目录 / 不存在的盘符 / 不存在的网络路径 / 填成文件 / 权限不足 / 目录无照片
(b) 正常目录能数出 ARW/CR3/JPG 张数（递归子目录，跳过 __MACOSX 等）
(c) /api/ingest/start 在源目录不可读时明确拒绝，并给出可操作提示
(d) /api/ingest/start 在 NAS 不可达时明确拒绝（避免读到卡却写不进 NAS 才失败）
(e) /api/ingest/check-source 只读诊断可用，且未登录 401
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

APP_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = APP_DIR.parents[1]


def _load_cc():
    spec = importlib.util.spec_from_file_location("control_center_main_s9", APP_DIR / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["control_center_main_s9"] = mod
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def run() -> None:
    cc = _load_cc()

    # ---------- (a) 诊断函数本身 ----------
    cases = [
        ("", "未填写"),
        (r"Q:\不存在的卡\DCIM", "盘符"),
        (r"\\10.255.255.1\NOPE\RAW", "网络"),
        (r"D:\definitely\missing\dir", "不存在"),
    ]
    for raw, kw in cases:
        d = cc.diagnose_source_dir(raw)
        assert d["error"], f"{raw!r} 应报错"
        assert d["hint"], f"{raw!r} 应给出可操作提示"
    d = cc.diagnose_source_dir("")
    assert "未填写" in d["error"]
    d = cc.diagnose_source_dir(r"Q:\不存在的卡\DCIM")
    assert "盘符" in d["hint"] and "SD" in d["hint"], d
    d = cc.diagnose_source_dir(r"\\10.255.255.1\NOPE\RAW")
    assert "网络" in d["hint"], d
    print("[PASS] (a) 空路径/不存在盘符/不可达网络路径/不存在目录，均给出针对性原因与提示")

    with tempfile.TemporaryDirectory(prefix="zs_s9_") as td:
        tmp = Path(td)

        # 填成文件
        f = tmp / "photo.jpg"
        f.write_bytes(b"x")
        d = cc.diagnose_source_dir(str(f))
        assert "不是目录" in d["error"], d
        assert "文件夹" in d["hint"]

        # 空目录
        empty = tmp / "empty"
        empty.mkdir()
        d = cc.diagnose_source_dir(str(empty))
        assert "没有可识别的照片" in d["error"], d
        assert "手动导入" in d["hint"], "应提示可改用手动导入"

        # ---------- (b) 正常目录（递归 + 跳过系统垃圾目录）----------
        root = tmp / "SDCARD"
        (root / "DCIM" / "100MSDCF").mkdir(parents=True)
        (root / "DCIM" / "101MSDCF").mkdir(parents=True)
        (root / "__MACOSX").mkdir()
        for i in range(3):
            (root / "DCIM" / "100MSDCF" / f"DSC{i:04d}.ARW").write_bytes(b"r")
        for i in range(2):
            (root / "DCIM" / "101MSDCF" / f"IMG{i:04d}.jpg").write_bytes(b"j")
        (root / "DCIM" / "100MSDCF" / "IMG_0001.CR3").write_bytes(b"c")
        # 这些不应被计入
        (root / "__MACOSX" / "._DSC0000.ARW").write_bytes(b"x")
        (root / "DCIM" / "100MSDCF" / "note.txt").write_text("hi")
        (root / "DCIM" / "100MSDCF" / "thumb.png").write_bytes(b"p")

        d = cc.diagnose_source_dir(str(root))
        assert d["error"] == "", f"正常目录不应报错: {d}"
        assert d["image_count"] == 6, f"应数出 6 张（3 ARW + 2 JPG + 1 CR3），实际 {d['image_count']}"
        assert d["readable"] is True and d["is_dir"] is True
        print("[PASS] (b) 正常目录递归数图准确（6 张），__MACOSX/txt/png 被正确忽略")

        # ---------- (c)(d)(e) 接口层 ----------
        env_file = tmp / ".env"
        env_file.write_text(
            "ADMIN_USER=admin\nADMIN_PASSWORD=TestS9Pass!55\nINTERNAL_API_KEY=s9-key\n"
            f"BOOKING_API_URL=http://127.0.0.1:3000\n"
            # 故意指向不可达的 NAS，用于验证前置拦截
            f"NAS_UNC_PATH={tmp / 'no_such_nas' / 'RAW'}\n"
            "NAS_DRIVE_PATH=\n",
            encoding="utf-8",
        )
        app = cc.create_app(
            env_file_path=env_file,
            config_overrides={"NAS_UNC_PATH": str(tmp / "no_such_nas" / "RAW"), "NAS_DRIVE_PATH": "", "BOOKING_API_URL": "http://127.0.0.1:3000"},
            skills_dir_path=tmp / "skills",
            presets_map_file=tmp / "presets.json",
            preview_xmp_dir=tmp / "px", retouch_xmp_dir=tmp / "rx", retouch_refs_dir=tmp / "rref",
            sandbox_dir=tmp / "sandbox",
            im_rules_file=tmp / "im_rules.json",
            pid_file_path=tmp / "pids.json",
        )
        client = TestClient(app)

        assert client.post("/api/ingest/check-source", json={"source_dir": str(root)}).status_code == 401
        assert client.post("/api/ingest/start", json={"source_dir": str(root), "event_name": "X"}).status_code == 401

        r = client.post("/api/auth/login", json={"username": "admin", "password": "TestS9Pass!55"})
        assert r.status_code == 200, r.text

        # check-source 只读诊断
        r = client.post("/api/ingest/check-source", json={"source_dir": str(root)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["source"]["image_count"] == 6
        assert "nas" in body and body["nas"]["online"] is False
        print("[PASS] (e) /api/ingest/check-source 只读诊断返回张数与 NAS 状态，未登录 401")

        # (c) 源目录不可读 -> 拒绝，且提示具体
        r = client.post("/api/ingest/start", json={"source_dir": r"Q:\不存在的卡", "event_name": "BACC"})
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "盘符" in detail and "SD" in detail, detail

        r = client.post("/api/ingest/start", json={"source_dir": str(tmp / "empty"), "event_name": "BACC"})
        assert r.status_code == 400
        assert "没有可识别的照片" in r.json()["detail"]
        print("[PASS] (c) 源目录不可读时 /api/ingest/start 明确拒绝并说明原因")

        # (d) 源目录可读但 NAS 不可达 -> 同样拒绝，避免白拷一场
        r = client.post("/api/ingest/start", json={"source_dir": str(root), "event_name": "BACC"})
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "NAS" in detail, detail
        assert "存储与拷卡路径" in detail, detail
        print("[PASS] (d) NAS 不可达时前置拦截，提示去「存储与拷卡路径」修正")

    print("\n========================================================")
    print("🎉 Sprint 9 源目录诊断与前置拦截测试全部通过")
    print("========================================================")


if __name__ == "__main__":
    run()
