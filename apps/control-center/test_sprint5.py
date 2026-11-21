#!/usr/bin/env python3
"""
Sprint 5 统一可视化中控台与公网 VPS 穿透套件冒烟测试：
(a) 强鉴权防线：未登录访问页面跳 /login (302)、未登录访问 API 返回 401、错误密码 401、登录后 HttpOnly Cookie 放行 200；
(b) 健康聚合接口正常返回 booking-site / im-gateway / PS Bridge / NAS / GPU / VPS 穿透隧道状态；
(c) 订单看板与场记二维码 PNG 透传正常、PUBLIC_BASE_URL 正确拼装外网 /gallery/{token} 链接，且 /api/internal/* 拒绝外网 Host 访问；
(d) Skill 编辑器可新增/读回/清理自定义 .md Skill，并拦截路径穿越；
(e) .env 脱敏接口绝不泄露明文密钥（且回写不覆盖原密钥），frps.toml / frpc.toml / frpc_visitor.toml TOML 语法合法且无硬编码密码。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

import httpx

APP_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = APP_DIR.parents[1]
BOOKING_SITE_DIR = WORKSPACE_ROOT / "apps" / "booking-site"
TUNNEL_DIR = WORKSPACE_ROOT / "infra" / "tunnel"

sys.path.insert(0, str(APP_DIR))
from main import create_app  # noqa: E402

BOOKING_PORT = 3465
BOOKING_URL = f"http://127.0.0.1:{BOOKING_PORT}"
PUBLIC_BASE = "http://156.238.228.39:8080"
INTERNAL_KEY = "sprint5-internal-secret-9999"
ADMIN_USER = "admin"
ADMIN_PASS = "StrongStudioPass#2026"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def start_booking_server(db_path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "PORT": str(BOOKING_PORT),
        "DB_PATH": str(db_path),
        "INTERNAL_API_KEY": INTERNAL_KEY,
        "ADMIN_USER": ADMIN_USER,
        "ADMIN_PASSWORD": ADMIN_PASS,
        "PUBLIC_BASE_URL": PUBLIC_BASE,
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


async def run_tests() -> None:
    print("=== Sprint 5 中控台与公网穿透套件冒烟测试开始 ===")

    with tempfile.TemporaryDirectory(prefix="zero_sprint5_") as tmp_str:
        tmp_root = Path(tmp_str)
        booking_db = tmp_root / "booking_sprint5.db"
        temp_env_file = tmp_root / ".env"

        # 写入含明文敏感密钥的临时 .env 用于测试脱敏保护
        secret_deepseek = "sk-deepseek-plain-secret-abcd1234"
        secret_gemini = "AIzaSyPlainGeminiSecretXYZ9876"
        secret_frp = "FrpSuperSecretToken2026!"
        temp_env_file.write_text(
            "\n".join(
                [
                    f"INTERNAL_API_KEY={INTERNAL_KEY}",
                    f"ADMIN_USER={ADMIN_USER}",
                    f"ADMIN_PASSWORD={ADMIN_PASS}",
                    f"DEEPSEEK_API_KEY={secret_deepseek}",
                    f"GEMINI_API_KEY={secret_gemini}",
                    f"FRP_TOKEN={secret_frp}",
                    f"PUBLIC_BASE_URL={PUBLIC_BASE}",
                    f"BOOKING_API_URL={BOOKING_URL}",
                    "VPS_HOST=156.238.228.39",
                    "FRP_PUBLIC_BOOKING_PORT=8080",
                    "FRP_PUBLIC_STUDIO_PORT=8081",
                    f"NAS_DRIVE_PATH={tmp_root}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        booking_proc = start_booking_server(booking_db)
        try:
            # 预先在真实 booking-site 创建 1 条预约单
            with httpx.Client(timeout=10.0, trust_env=False) as bclient:
                r_create = bclient.post(
                    f"{BOOKING_URL}/api/internal/bookings",
                    headers={"X-API-Key": INTERNAL_KEY, "Content-Type": "application/json"},
                    json={
                        "platform": "qq",
                        "im_user_id": "20260501",
                        "qq": "20260501",
                        "cn": "外网测试CN",
                        "character": "雷电将军",
                        "event_name": "CP31",
                        "date": "2026-05-01",
                        "time": "10:00-12:00",
                        "deposit": "80",
                        "quantity": 1,
                    },
                )
                assert r_create.status_code == 201
                booking_id = int(r_create.json()["booking_id"])
                gallery_token = str(r_create.json()["gallery_token"])

                # 校验外网 Host 访问 booking-site /api/internal/* 必须被拒绝 (403)
                r_ext_block = bclient.get(
                    f"{BOOKING_URL}/api/internal/bookings/by-event?event_name=CP31",
                    headers={"X-API-Key": INTERNAL_KEY, "Host": "156.238.228.39:8080"},
                )
                assert r_ext_block.status_code == 403, f"外网 Host 访问 /api/internal/* 应被拦截 403，实际 {r_ext_block.status_code}"

            app = create_app(
                config_overrides={
                    "BOOKING_API_URL": BOOKING_URL,
                    "IM_GATEWAY_URL": "http://127.0.0.1:18080",
                    "PS_UXP_BRIDGE_URL": "http://127.0.0.1:18765",
                    "ADMIN_USER": ADMIN_USER,
                    "ADMIN_PASSWORD": ADMIN_PASS,
                    "INTERNAL_API_KEY": INTERNAL_KEY,
                    "PUBLIC_BASE_URL": PUBLIC_BASE,
                    "VPS_HOST": "156.238.228.39",
                    "FRP_PUBLIC_BOOKING_PORT": "8080",
                    "FRP_PUBLIC_STUDIO_PORT": "8081",
                    "NAS_UNC_PATH": "",
                    "NAS_DRIVE_PATH": str(tmp_root),
                    "DEEPSEEK_API_KEY": secret_deepseek,
                    "GEMINI_API_KEY": secret_gemini,
                    "FRP_TOKEN": secret_frp,
                },
                env_file_path=temp_env_file,
            )

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:3080", trust_env=False) as client:
                # ---------- (a) 强鉴权防线测试 ----------
                r_home_anon = await client.get("/", follow_redirects=False)
                assert r_home_anon.status_code == 302 and r_home_anon.headers.get("location") == "/login"

                for protected_ep in ("/api/overview/health", "/api/bookings", "/api/skills", "/api/settings/env"):
                    r_anon = await client.get(protected_ep)
                    assert r_anon.status_code == 401, f"未登录访问 {protected_ep} 应返回 401，实际 {r_anon.status_code}"

                r_bad_login = await client.post("/api/auth/login", json={"username": ADMIN_USER, "password": "wrong"})
                assert r_bad_login.status_code == 401

                r_login = await client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
                assert r_login.status_code == 200
                set_cookie = r_login.headers.get("set-cookie", "").lower()
                assert "zs_session=" in set_cookie and "httponly" in set_cookie

                r_home_authed = await client.get("/")
                assert r_home_authed.status_code == 200 and "ZERO STUDIO" in r_home_authed.text
                print("[PASS] (a) 外网强鉴权防线通过（未登录 302/401，登录后 HttpOnly Signed Cookie 放行 200）")

                # ---------- (b) 健康聚合接口 ----------
                r_health = await client.get("/api/overview/health")
                assert r_health.status_code == 200
                hdata = r_health.json()
                assert hdata["services"]["booking_site"]["online"] is True
                assert "im_gateway" in hdata["services"] and "ps_uxp_bridge" in hdata["services"]
                assert hdata["nas"]["online"] is True and hdata["nas"]["free_gb"] > 0
                assert "gpu" in hdata
                assert hdata["tunnel"]["public_booking_url"] == PUBLIC_BASE
                assert hdata["tunnel"]["public_studio_url"] == "http://156.238.228.39:8081"
                print("[PASS] (b) 健康聚合接口通过（返回微服务、NAS容量、GPU 及公网 VPS 隧道状态）")

                # ---------- (c) 订单看板、场记二维码透传与 PUBLIC_BASE_URL 外网链接 ----------
                r_books = await client.get("/api/bookings")
                assert r_books.status_code == 200
                bdata = r_books.json()
                assert bdata["public_base_url"] == PUBLIC_BASE
                found = [b for b in bdata["bookings"] if b["id"] == booking_id]
                assert len(found) == 1
                expected_gallery_url = f"{PUBLIC_BASE}/gallery/{gallery_token}"
                assert found[0]["public_gallery_url"] == expected_gallery_url

                r_qr = await client.get(f"/api/bookings/{booking_id}/slate-qr")
                assert r_qr.status_code == 200 and "image/png" in r_qr.headers.get("content-type", "")
                assert len(r_qr.content) > 100 and r_qr.content[:8] == PNG_MAGIC
                print(
                    f"[PASS] (c) 订单看板、场记二维码透传({len(r_qr.content)}B PNG)、PUBLIC_BASE_URL 外网链接"
                    f"({expected_gallery_url})及 /api/internal/* 外网隔离通过"
                )

                # ---------- (d) Skill 编辑器新增/读回/清理 + 路径穿越拦截 ----------
                r_trav = await client.put("/api/skills/..%2Fevil.md", json={"content": "hack"})
                assert r_trav.status_code in (400, 404)

                custom_skill = "sprint5_temp_skill.md"
                skill_body = "# Sprint 5 热加载测试规则\n- 霓虹夜景肤色保护"
                try:
                    r_put = await client.put(f"/api/skills/{custom_skill}", json={"content": skill_body})
                    assert r_put.status_code == 200
                    r_get = await client.get(f"/api/skills/{custom_skill}")
                    assert r_get.status_code == 200 and r_get.json()["content"] == skill_body
                    r_list = await client.get("/api/skills")
                    assert any(s["filename"] == custom_skill for s in r_list.json()["skills"])
                finally:
                    r_del = await client.delete(f"/api/skills/{custom_skill}")
                    assert r_del.status_code == 200
                assert not (WORKSPACE_ROOT / "configs" / "retouch_skills" / custom_skill).exists()
                print("[PASS] (d) Skill 编辑器新增/读回/清理与路径穿越拦截验证通过")

                # ---------- (e) .env 脱敏防泄露 + FRP TOML 语法与零硬编码密码校验 ----------
                r_env = await client.get("/api/settings/env")
                assert r_env.status_code == 200
                env_raw_text = r_env.text
                for secret_val in (secret_deepseek, secret_gemini, secret_frp, ADMIN_PASS, INTERNAL_KEY):
                    assert secret_val not in env_raw_text, f"严禁泄露明文密钥: {secret_val}"

                # 更新非敏感项 + 提交脱敏占位符，确认磁盘原明文密钥不被 **** 破坏
                items_map = {it["key"]: it["value"] for it in r_env.json()["items"]}
                r_save = await client.put(
                    "/api/settings/env",
                    json={
                        "updates": {
                            "PUBLIC_BASE_URL": "http://156.238.228.39:8080",
                            "DEEPSEEK_API_KEY": items_map["DEEPSEEK_API_KEY"],
                        }
                    },
                )
                assert r_save.status_code == 200
                assert "DEEPSEEK_API_KEY" not in r_save.json()["updated_keys"]
                assert secret_deepseek in temp_env_file.read_text(encoding="utf-8")

                # 校验 frps.toml / frpc.toml / frpc_visitor.toml 语法合法且无硬编码真实密码
                for toml_name in ("frps.toml", "frpc.toml", "frpc_visitor.toml"):
                    tpath = TUNNEL_DIR / toml_name
                    assert tpath.exists(), f"缺少隧道模板 {tpath}"
                    text = tpath.read_text(encoding="utf-8")
                    parsed = tomllib.loads(text)
                    assert parsed, f"TOML 解析为空: {toml_name}"
                    assert not re.search(r"sshpass|VPS_SSH_PASSWORD\s*=\s*['\"]?[^'\"\s]+", text, re.IGNORECASE)

                frpc_parsed = tomllib.loads((TUNNEL_DIR / "frpc.toml").read_text(encoding="utf-8"))
                proxy_names = {p["name"]: p for p in frpc_parsed["proxies"]}
                assert {"booking-public", "studio-public", "studio-p2p"}.issubset(proxy_names.keys())
                assert proxy_names["studio-p2p"]["type"] == "xtcp"

                deploy_ps1 = (TUNNEL_DIR / "deploy_vps.ps1").read_text(encoding="utf-8")
                assert "sshpass" not in deploy_ps1.lower()
                assert not re.search(
                    r"password\s*=\s*['\"](?!\$|\{\{)[A-Za-z0-9!@#%^&*_.-]{6,}['\"]",
                    deploy_ps1,
                    re.IGNORECASE,
                )
                print("[PASS] (e) .env 脱敏防泄露与回写保护、frps/frpc/visitor TOML 语法及零硬编码密码校验通过")

            print("=== Sprint 5 全部冒烟测试通过 (5/5) ===")
        finally:
            booking_proc.terminate()
            try:
                booking_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                booking_proc.kill()
                booking_proc.wait(timeout=5)


if __name__ == "__main__":
    asyncio.run(run_tests())
