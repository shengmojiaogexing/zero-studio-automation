#!/usr/bin/env python3
"""
Sprint 6 自动化冒烟测试：
(a) IM_GATEWAY_URL (8090) 与 NAPCAT_HTTP_URL (3002) 端口修正生效；
(b) im_rules.json 群监控白名单读写、brain.py 热读取过滤，以及微信/QQ 测试与状态接口可用；
(c) .xmp 文件（预览 XMP 与正式修图 XMP）与仿色参考图上传并正确更新 presets_map.json / PREVIEW_XMP_PATH；
(d) Skill 文件上传（.md/.txt）与启用/停用开关（.md ↔ .md.disabled）生效，且 gemini_colorist 仅加载 .md；
(e) 单图仿色实验室接口跑通并产出可访问的前后对比预览图与结构化 JSON；
(f) 分组设置 API 返回 6 大分组元数据且密钥全程脱敏，配置重载接口可用。
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from fastapi.testclient import TestClient
from PIL import Image

APP_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = APP_DIR.parents[1]
GATEWAY_DIR = WORKSPACE_ROOT / "services" / "im-gateway"
RETOUCH_DIR = WORKSPACE_ROOT / "pipelines" / "retouch-engine"

for p in (RETOUCH_DIR, GATEWAY_DIR, APP_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import importlib.util  # noqa: E402


def _load_module(name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(name, file_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cc_main = _load_module("control_center_main_s6", APP_DIR / "main.py")
create_cc_app = cc_main.create_app

from adapters.base import IMAdapter, IncomingMessage  # noqa: E402
from brain import StudioBrain, load_im_rules, save_im_rules  # noqa: E402
from gemini_colorist import load_retouch_skills  # noqa: E402
from rate_limiter import FriendRequestRateLimiter  # noqa: E402


def _load_gateway_module():
    return _load_module("im_gateway_main_s6", GATEWAY_DIR / "main.py")


class DummyAdapter(IMAdapter):
    def __init__(self, platform: str):
        self.platform = platform
        self.sent: List[Dict[str, Any]] = []

    async def send_text(self, target_id: str, text: str, chat_type: str = "private") -> Dict[str, Any]:
        rec = {"target_id": target_id, "text": text, "chat_type": chat_type}
        self.sent.append(rec)
        return {"status": "ok", "echo": rec}

    async def send_image(self, target_id: str, image: Any, chat_type: str = "private") -> Dict[str, Any]:
        return {"status": "ok"}

    async def send_location(self, target_id: str, lat: float, lng: float, title: str, address: str, chat_type: str = "private") -> Dict[str, Any]:
        return {"status": "ok"}

    async def request_add_friend(self, user_id: str, remark: str, group_id: str | None = None) -> Dict[str, Any]:
        return {"status": "ok"}


class DummyLLM:
    async def chat_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        return {
            "intent_score": 92,
            "is_potential_client": True,
            "city": "上海",
            "event_name": "CP31",
            "character": "芙宁娜",
            "shoot_time": "周六",
            "reason": "明确找摄影拍场照",
            "add_friend_remark": "你好呀，CP31 拍芙宁娜场照扩列吗~",
        }


SAMPLE_XMP = """<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   crs:Exposure2012="+0.35"
   crs:Contrast2012="+10"
   crs:Temperature="5100"
   crs:Tint="+4"
   crs:Shadows2012="+15"
   crs:Highlights2012="-12"/>
 </rdf:RDF>
</x:xmpmeta>
"""


def _make_jpeg_bytes(color=(120, 150, 190), size=(320, 240)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_a_port_configs() -> None:
    env_example = cc_main.read_env_file(WORKSPACE_ROOT / ".env.example")
    env_real = cc_main.read_env_file(WORKSPACE_ROOT / ".env")
    assert env_example.get("IM_GATEWAY_URL") == "http://127.0.0.1:8090", f".env.example IM_GATEWAY_URL wrong: {env_example.get('IM_GATEWAY_URL')}"
    assert env_example.get("NAPCAT_HTTP_URL") == "http://127.0.0.1:3002", f".env.example NAPCAT_HTTP_URL wrong: {env_example.get('NAPCAT_HTTP_URL')}"
    assert env_real.get("IM_GATEWAY_URL") == "http://127.0.0.1:8090", f".env IM_GATEWAY_URL wrong: {env_real.get('IM_GATEWAY_URL')}"
    assert env_real.get("NAPCAT_HTTP_URL") == "http://127.0.0.1:3002", f".env NAPCAT_HTTP_URL wrong: {env_real.get('NAPCAT_HTTP_URL')}"
    print("[PASS] (a) IM_GATEWAY_URL (8090) 与 NAPCAT_HTTP_URL (3002) 端口修正生效")


def test_b_im_rules_and_gateway(tmp: Path) -> None:
    gw_mod = _load_gateway_module()
    rules_file = tmp / "im_rules.json"
    limiter = FriendRequestRateLimiter(db_path=tmp / "limiter.db", dry_run=True)
    qq_dummy = DummyAdapter("qq")
    wx_dummy = DummyAdapter("wechat")
    gw_app = gw_mod.create_app(
        adapters={"qq": qq_dummy, "wechat": wx_dummy, "wecom_kf": DummyAdapter("wecom_kf")},
        rate_limiter=limiter,
        llm_client=DummyLLM(),
        internal_api_key="test-s6-key",
        enable_background_workers=False,
        rules_path=rules_file,
    )
    gw_client = TestClient(gw_app)
    headers = {"X-API-Key": "test-s6-key"}

    # 写入群白名单规则
    r_put = gw_client.put(
        "/api/gateway/rules",
        headers=headers,
        json={
            "qq_groups": ["888888"],
            "wechat_groups": ["CP31约拍总群"],
            "keywords": ["约拍", "场照"],
            "intent_threshold": 85,
        },
    )
    assert r_put.status_code == 200
    assert r_put.json()["rules"]["qq_groups"] == ["888888"]
    assert load_im_rules(rules_file)["intent_threshold"] == 85

    # 非白名单 QQ 群消息应被 brain.py 热读取过滤
    r_ign = gw_client.post(
        "/api/gateway/incoming",
        headers=headers,
        json={
            "platform": "qq",
            "chat_type": "group",
            "group_id": "999999",
            "user_id": "10001",
            "nickname": "路人",
            "text": "CP31求约拍场照",
        },
    )
    assert r_ign.status_code == 200
    assert r_ign.json()["result"]["action"] == "IGNORED_GROUP_NOT_WHITELISTED"

    # 白名单 QQ 群消息应正常评分并入队
    r_hit = gw_client.post(
        "/api/gateway/incoming",
        headers=headers,
        json={
            "platform": "qq",
            "chat_type": "group",
            "group_id": "888888",
            "user_id": "10002",
            "nickname": "小雪",
            "text": "CP31求约拍场照",
        },
    )
    assert r_hit.status_code == 200
    assert r_hit.json()["result"]["action"] == "ENQUEUED_ADD_FRIEND"

    # 通道状态与 QQ/微信测试发送接口
    r_st = gw_client.get("/api/gateway/channels/status", headers=headers)
    assert r_st.status_code == 200
    assert "/webhook/napcat" in r_st.json()["qq"]["webhook_url"]

    r_qq = gw_client.post("/api/gateway/qq/test-send", headers=headers, json={"qq": "123456", "text": "hello qq"})
    assert r_qq.status_code == 200 and r_qq.json()["status"] == "ok"
    assert qq_dummy.sent and qq_dummy.sent[0]["target_id"] == "123456"

    r_wx = gw_client.post("/api/gateway/wechat/test-send", headers=headers, json={"target": "文件传输助手", "text": "hello wx"})
    assert r_wx.status_code == 200 and r_wx.json()["status"] == "ok"
    assert wx_dummy.sent and wx_dummy.sent[0]["target_id"] == "文件传输助手"

    print("[PASS] (b) im_rules.json 群监控白名单热读取与 QQ/微信测试接口通过")


def test_c_d_e_f_control_center(tmp: Path) -> None:
    env_file = tmp / ".env"
    env_file.write_text(
        "ADMIN_USER=admin\n"
        "ADMIN_PASSWORD=TestS6Pass!99\n"
        "INTERNAL_API_KEY=s6-internal-secret-xyz\n"
        "DEEPSEEK_API_KEY=sk-deepseek-secret-123456\n"
        "GEMINI_API_KEY=\n"
        "IM_GATEWAY_URL=http://127.0.0.1:8090\n"
        "NAPCAT_HTTP_URL=http://127.0.0.1:3002\n"
        "PREVIEW_XMP_PATH=\n",
        encoding="utf-8",
    )
    skills_dir = tmp / "retouch_skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "default_cosplay_skill.md").write_text("# Default Cosplay Skill\n保肤色通透\n", encoding="utf-8")

    presets_map = tmp / "presets_map.json"
    presets_map.write_text("{}\n", encoding="utf-8")

    app = create_cc_app(
        env_file_path=env_file,
        skills_dir_path=skills_dir,
        presets_map_file=presets_map,
        preview_xmp_dir=tmp / "preview_xmp",
        retouch_xmp_dir=tmp / "retouch_xmp",
        retouch_refs_dir=tmp / "retouch_refs",
        sandbox_dir=tmp / "sandbox",
        im_rules_file=tmp / "im_rules.json",
        pid_file_path=tmp / "no_pids.json",
    )
    client = TestClient(app)

    # 登录获取 HttpOnly Session Cookie
    r_login = client.post("/api/auth/login", json={"username": "admin", "password": "TestS6Pass!99"})
    assert r_login.status_code == 200, r_login.text

    # ---- (c) XMP 预设与仿色参考图上传 ----
    r_prev = client.post(
        "/api/presets/upload-xmp",
        data={"category": "preview"},
        files={"file": ("event_preview.xmp", SAMPLE_XMP.encode("utf-8"), "application/xml")},
    )
    assert r_prev.status_code == 200, r_prev.text
    assert "event_preview.xmp" in r_prev.json()["preview_xmp_path"]
    assert "event_preview.xmp" in cc_main.read_env_file(env_file).get("PREVIEW_XMP_PATH", "")

    r_ret = client.post(
        "/api/presets/upload-xmp",
        data={"category": "retouch", "preset_code": "cyan_test", "name": "苍蓝测试预设"},
        files={"file": ("cyan_test.xmp", SAMPLE_XMP.encode("utf-8"), "application/xml")},
    )
    assert r_ret.status_code == 200, r_ret.text
    pmap = json.loads(presets_map.read_text(encoding="utf-8"))
    assert "cyan_test" in pmap and pmap["cyan_test"]["xmp_file"] == "cyan_test.xmp"

    ref_bytes = _make_jpeg_bytes(color=(180, 200, 220))
    r_ref = client.post(
        "/api/presets/upload-ref",
        data={"preset_code": "cyan_test"},
        files={"file": ("target_ref.jpg", ref_bytes, "image/jpeg")},
    )
    assert r_ref.status_code == 200, r_ref.text
    ref_url = r_ref.json()["ref_preview_url"]
    assert client.get(ref_url).status_code == 200
    pmap = json.loads(presets_map.read_text(encoding="utf-8"))
    assert pmap["cyan_test"]["reference_image"] != ""
    print("[PASS] (c) 预览/正式 XMP 与仿色参考图上传、presets_map.json 及 PREVIEW_XMP_PATH 回写通过")

    # ---- (d) Skill 上传与启用/停用开关 (.md <-> .md.disabled) ----
    r_sk_up = client.post(
        "/api/skills/upload",
        files={"file": ("night_neon_note.txt", "# 夜景霓虹修图笔记\n压低高光洋红溢出\n".encode("utf-8"), "text/plain")},
    )
    assert r_sk_up.status_code == 200, r_sk_up.text
    assert r_sk_up.json()["filename"] == "night_neon_note.md"

    _, loaded_before = load_retouch_skills(skills_dir)
    assert "night_neon_note.md" in loaded_before

    # 停用 -> .md.disabled
    r_tog1 = client.post("/api/skills/night_neon_note.md/toggle")
    assert r_tog1.status_code == 200
    assert r_tog1.json()["filename"] == "night_neon_note.md.disabled"
    assert r_tog1.json()["enabled"] is False
    _, loaded_disabled = load_retouch_skills(skills_dir)
    assert "night_neon_note.md" not in loaded_disabled

    # 重新启用 -> .md
    r_tog2 = client.post("/api/skills/night_neon_note.md.disabled/toggle")
    assert r_tog2.status_code == 200
    assert r_tog2.json()["filename"] == "night_neon_note.md"
    assert r_tog2.json()["enabled"] is True
    _, loaded_reenabled = load_retouch_skills(skills_dir)
    assert "night_neon_note.md" in loaded_reenabled
    print("[PASS] (d) Skill 文件上传与启用/停用开关 (.md <-> .md.disabled) 及热加载过滤通过")

    # ---- (e) 单图仿色实验室 (Color-Match & Retouch Sandbox) ----
    src_bytes = _make_jpeg_bytes(color=(90, 110, 130))
    r_sb = client.post(
        "/api/retouch/sandbox",
        data={"preset_code": "cyan_test", "send_to_ps": "false"},
        files={
            "image": ("cos_sample.jpg", src_bytes, "image/jpeg"),
            "reference_image": ("mood_ref.jpg", ref_bytes, "image/jpeg"),
        },
    )
    assert r_sb.status_code == 200, r_sb.text
    sb = r_sb.json()
    assert sb["xmp_applied"] == "cyan_test.xmp"
    assert "default_cosplay_skill.md" in sb["loaded_skills"]
    assert "exposure_delta" in sb["plan_json"] and "hsl_adjustments" in sb["plan_json"]
    assert client.get(sb["original_url"]).status_code == 200
    assert client.get(sb["result_url"]).status_code == 200
    assert client.get(sb["reference_url"]).status_code == 200
    print("[PASS] (e) 单图仿色实验室跑通并产出可访问的前后对比预览图与结构化 JSON")

    # ---- (f) 6 大分组设置中心 API 与脱敏保护 + 服务重载接口 ----
    r_grp = client.get("/api/settings/groups")
    assert r_grp.status_code == 200
    groups = r_grp.json()["groups"]
    assert len(groups) == 6
    group_ids = [g["id"] for g in groups]
    assert group_ids == ["storage", "ai", "im", "retouch", "tunnel", "security"]

    # 校验所有敏感字段已脱敏（不含明文密码/Key）
    raw_text = r_grp.text
    assert "TestS6Pass!99" not in raw_text, "ADMIN_PASSWORD 明文泄露！"
    assert "s6-internal-secret-xyz" not in raw_text, "INTERNAL_API_KEY 明文泄露！"
    assert "sk-deepseek-secret-123456" not in raw_text, "DEEPSEEK_API_KEY 明文泄露！"

    r_rel = client.post("/api/settings/reload-services", json={"services": ["im-gateway"]})
    assert r_rel.status_code == 200 and r_rel.json()["env_reloaded_in_control_center"] is True
    print("[PASS] (f) 6 大分组设置中心 API、全程密钥脱敏与配置重载接口通过")


def main() -> None:
    test_a_port_configs()
    with tempfile.TemporaryDirectory(prefix="zs_sprint6_") as td:
        tmp = Path(td)
        test_b_im_rules_and_gateway(tmp)
        test_c_d_e_f_control_center(tmp)
    print("\n========================================================")
    print("🎉 Sprint 6 全部 6 项自动化冒烟测试 100% 通过！")
    print("========================================================")


if __name__ == "__main__":
    main()
