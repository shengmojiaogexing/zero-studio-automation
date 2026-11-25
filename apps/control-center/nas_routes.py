#!/usr/bin/env python3
"""
ZERO STUDIO 中控台原生 NAS 模块 (无 iframe，免二次登录)
直接对接 VPS 挂载的 /srv/tr3000_files、分享库 /opt/tr3000-panel/shares.json 与 TR3000 设备管理。
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import subprocess
import time
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

ROOT_VIEW = os.getenv("NAS_VIEW_DIR", "/srv/tr3000_files")
ROOT_RAW = os.getenv("NAS_RAW_DIR", "/mnt/tr3000_raw")
SHARES_FILE = "/opt/tr3000-panel/shares.json"
CRED_FILE = "/opt/tr3000-panel/smb.cred"
TR_HOST = "10.126.126.1"
SHARE_PUBLIC_BASE = "https://zero-ai.cloud"

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("application/pdf", ".pdf")


def _human_size(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0:
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} PB"


def _resolve_safe(rel_path: str) -> str:
    rel = urllib.parse.unquote(rel_path or "").strip("/")
    parts = [p for p in rel.split("/") if p and p not in (".", "..")]
    full = os.path.join(ROOT_VIEW, *parts) if parts else ROOT_VIEW
    real = os.path.realpath(full)
    allowed = [os.path.realpath(ROOT_VIEW), os.path.realpath(ROOT_RAW)]
    if not any(real == a or real.startswith(a + os.sep) for a in allowed):
        raise HTTPException(status_code=403, detail="非法路径")
    return real


def _load_shares() -> Dict[str, Any]:
    if os.path.exists(SHARES_FILE):
        try:
            with open(SHARES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def _save_shares(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(SHARES_FILE), exist_ok=True)
    with open(SHARES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _ssh_tr(cmd: str, timeout: int = 15):
    full = [
        "ssh", "-i", "/root/.ssh/id_ed25519",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=6",
        "-o", "BatchMode=yes",
        f"root@{TR_HOST}", cmd,
    ]
    p = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _safe_sh(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


class ShareCreateReq(BaseModel):
    path: str
    days: int = 7
    code: str = ""


class ShareDeleteReq(BaseModel):
    token: str


class RenameReq(BaseModel):
    uuid: str
    name: str
    enabled: str = "1"


class PassReq(BaseModel):
    password: str


class WifiApReq(BaseModel):
    ap24_ssid: str
    ap5_ssid: str
    ap_key: str


class WifiStaReq(BaseModel):
    sta_ssid: str
    sta_key: str


def register_nas_routes(app: FastAPI, require_session) -> None:
    deps = [Depends(require_session)]

    @app.get("/api/nas/list", dependencies=deps)
    def nas_list(path: str = Query(default="")):
        real = _resolve_safe(path)
        if not os.path.isdir(real):
            return {"items": [], "path": path}
        items: List[Dict[str, Any]] = []
        try:
            for entry in os.scandir(real):
                name = entry.name
                if name in ("$RECYCLE.BIN", "System Volume Information") or name.startswith("._"):
                    continue
                try:
                    st = entry.stat(follow_symlinks=True)
                    is_dir = entry.is_dir(follow_symlinks=True)
                    mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                    items.append({
                        "name": name,
                        "is_dir": is_dir,
                        "size": 0 if is_dir else st.st_size,
                        "size_str": "-" if is_dir else _human_size(st.st_size),
                        "mtime": mtime,
                    })
                except OSError:
                    continue
        except OSError:
            pass
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {"items": items, "path": path}

    @app.get("/api/nas/raw", dependencies=deps)
    def nas_raw(path: str = Query(...)):
        real = _resolve_safe(path)
        if not os.path.isfile(real):
            raise HTTPException(status_code=404, detail="文件不存在")
        ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
        return FileResponse(real, media_type=ctype)

    @app.get("/api/nas/download", dependencies=deps)
    def nas_download(path: str = Query(...)):
        real = _resolve_safe(path)
        if not os.path.isfile(real):
            raise HTTPException(status_code=404, detail="文件不存在")
        fname = os.path.basename(real)
        ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
        return FileResponse(real, media_type=ctype, filename=fname)

    @app.get("/api/nas/text", dependencies=deps)
    def nas_text(path: str = Query(...)):
        real = _resolve_safe(path)
        if not os.path.isfile(real):
            raise HTTPException(status_code=404, detail="文件不存在")
        with open(real, "rb") as f:
            data = f.read(524288)
        try:
            txt = data.decode("utf-8")
        except UnicodeDecodeError:
            txt = data.decode("gbk", errors="replace")
        return PlainTextResponse(txt)

    @app.get("/api/nas/download-zip", dependencies=deps)
    def nas_download_zip(path: str = Query(...)):
        real = _resolve_safe(path)
        if not os.path.isdir(real):
            raise HTTPException(status_code=404, detail="文件夹不存在")
        parent = os.path.dirname(real.rstrip("/"))
        folder = os.path.basename(real.rstrip("/"))
        qname = urllib.parse.quote((folder or "archive") + ".zip")

        def iter_zip():
            proc = subprocess.Popen(
                ["zip", "-0", "-q", "-r", "-", folder, "-x", "*/$RECYCLE.BIN/*", "*/System Volume Information/*", "*/._*"],
                cwd=parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    proc.stdout.close()
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()

        headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{qname}"}
        return StreamingResponse(iter_zip(), media_type="application/zip", headers=headers)

    @app.get("/api/nas/share/list", dependencies=deps)
    def nas_share_list():
        shares = _load_shares()
        now = time.time()
        out = []
        for tok, sh in shares.items():
            exp = sh.get("expires_at", 0)
            if exp and now > exp:
                continue
            out.append({
                "token": tok,
                "url": f"{SHARE_PUBLIC_BASE}/s/{tok}",
                "name": sh.get("name", ""),
                "path": sh.get("path", ""),
                "code": sh.get("code", ""),
                "expire_str": "永久有效" if not exp else datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M"),
                "created_at": sh.get("created_at", 0),
            })
        out.sort(key=lambda x: x["created_at"], reverse=True)
        return {"shares": out}

    @app.post("/api/nas/share/create", dependencies=deps)
    def nas_share_create(req: ShareCreateReq):
        rel = req.path.strip("/")
        real = _resolve_safe(rel)
        if not os.path.exists(real):
            raise HTTPException(status_code=404, detail="文件或目录不存在")
        shares = _load_shares()
        token = secrets.token_urlsafe(6).replace("-", "a").replace("_", "b")
        now = int(time.time())
        shares[token] = {
            "path": rel,
            "name": os.path.basename(rel) or rel,
            "code": req.code.strip()[:16],
            "created_at": now,
            "expires_at": (now + req.days * 86400) if req.days > 0 else 0,
        }
        _save_shares(shares)
        return {"ok": True, "token": token, "url": f"{SHARE_PUBLIC_BASE}/s/{token}"}

    @app.post("/api/nas/share/delete", dependencies=deps)
    def nas_share_delete(req: ShareDeleteReq):
        shares = _load_shares()
        if req.token in shares:
            del shares[req.token]
            _save_shares(shares)
        return {"ok": True}

    @app.get("/api/nas/device/status", dependencies=deps)
    def nas_device_status():
        cmd = (
            "/usr/bin/tr3000-disk-info.sh; echo '===WIFI==='; "
            "printf 'ap24_ssid=%s\\n' \"$(uci -q get wireless.default_radio0.ssid)\"; "
            "printf 'ap5_ssid=%s\\n' \"$(uci -q get wireless.default_radio1.ssid)\"; "
            "printf 'ap_key=%s\\n' \"$(uci -q get wireless.default_radio0.key)\"; "
            "printf 'sta_ssid=%s\\n' \"$(uci -q get wireless.wifinet_wwan.ssid)\"; "
            "printf 'sta_key=%s\\n' \"$(uci -q get wireless.wifinet_wwan.key)\"; "
            "printf 'ipv6=%s\\n' \"$(/usr/bin/tr3000-get-v6.sh 2>/dev/null)\""
        )
        rc, out, _ = _ssh_tr(cmd)
        disks, wifi = [], {}
        if rc == 0:
            sec = "disks"
            for line in out.splitlines():
                if line.strip() == "===WIFI===":
                    sec = "wifi"
                    continue
                if sec == "disks":
                    parts = line.split("|")
                    if len(parts) >= 10:
                        disks.append({
                            "part": parts[0], "uuid": parts[1], "fstype": parts[2],
                            "size": parts[3], "used": parts[4], "avail": parts[5],
                            "usepct": parts[6], "mounted": parts[7],
                            "alias": parts[8], "enabled": parts[9],
                        })
                elif "=" in line:
                    k, v = line.split("=", 1)
                    wifi[k.strip()] = v.strip()
        et = ""
        try:
            et = subprocess.run(["easytier-cli", "peer"], capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            pass
        return {"online": rc == 0, "disks": disks, "wifi": wifi, "easytier": et}

    @app.post("/api/nas/device/rename", dependencies=deps)
    def nas_device_rename(req: RenameReq):
        name = req.name.strip()
        if not req.uuid or not name or any(ch in name for ch in '|/\\:*?"<>`\'\n\r'):
            raise HTTPException(status_code=400, detail="非法名称或 UUID")
        enabled = "1" if req.enabled == "1" else "0"
        cmd = (
            f"touch /etc/tr3000_aliases.conf; "
            f"grep -v '^{req.uuid}|' /etc/tr3000_aliases.conf > /tmp/al.tmp || true; "
            f"echo '{req.uuid}|{name}|{enabled}' >> /tmp/al.tmp; "
            f"mv /tmp/al.tmp /etc/tr3000_aliases.conf; "
            f"/usr/bin/tr3000-sync-shares.sh"
        )
        rc, _, err = _ssh_tr(cmd, timeout=20)
        if rc == 0:
            subprocess.run(["/usr/local/bin/tr3000-vps-sync.sh"], timeout=20)
        return {"ok": rc == 0, "error": err}

    @app.post("/api/nas/device/remount", dependencies=deps)
    def nas_device_remount():
        rc, _, err = _ssh_tr("/usr/bin/tr3000-mount.sh && /usr/bin/tr3000-sync-shares.sh", timeout=25)
        if rc == 0:
            subprocess.run(["/usr/local/bin/tr3000-vps-sync.sh"], timeout=20)
        return {"ok": rc == 0, "error": err}

    @app.post("/api/nas/device/smbpass", dependencies=deps)
    def nas_device_smbpass(req: PassReq):
        pwd = req.password.strip()
        if len(pwd) < 4 or "'" in pwd or "\n" in pwd:
            raise HTTPException(status_code=400, detail="密码至少4位且不能含单引号")
        cmd = f"ksmbd.adduser -u admin -p {_safe_sh(pwd)} 2>/dev/null || ksmbd.adduser -a admin -p {_safe_sh(pwd)}; /etc/init.d/ksmbd restart"
        rc, _, err = _ssh_tr(cmd, timeout=20)
        if rc == 0:
            with open(CRED_FILE, "w", encoding="utf-8") as cf:
                cf.write(f"username=admin\npassword={pwd}\n")
            os.chmod(CRED_FILE, 0o600)
        return {"ok": rc == 0, "error": err}

    @app.post("/api/nas/device/wifi_ap", dependencies=deps)
    def nas_device_wifi_ap(req: WifiApReq):
        if not req.ap24_ssid.strip() or not req.ap5_ssid.strip() or len(req.ap_key.strip()) < 8:
            raise HTTPException(status_code=400, detail="名称不能为空且密码至少8位")
        cmd = (
            f"uci set wireless.default_radio0.ssid={_safe_sh(req.ap24_ssid.strip())}; "
            f"uci set wireless.default_radio0.encryption='psk2'; "
            f"uci set wireless.default_radio0.key={_safe_sh(req.ap_key.strip())}; "
            f"uci set wireless.default_radio1.ssid={_safe_sh(req.ap5_ssid.strip())}; "
            f"uci set wireless.default_radio1.encryption='psk2'; "
            f"uci set wireless.default_radio1.key={_safe_sh(req.ap_key.strip())}; "
            f"uci commit wireless; (sleep 1; wifi reload) >/dev/null 2>&1 &"
        )
        rc, _, err = _ssh_tr(cmd, timeout=15)
        return {"ok": rc == 0, "error": err}

    @app.post("/api/nas/device/wifi_sta", dependencies=deps)
    def nas_device_wifi_sta(req: WifiStaReq):
        if not req.sta_ssid.strip() or len(req.sta_key.strip()) < 8:
            raise HTTPException(status_code=400, detail="名称不能为空且密码至少8位")
        cmd = (
            f"uci set wireless.wifinet_wwan.ssid={_safe_sh(req.sta_ssid.strip())}; "
            f"uci set wireless.wifinet_wwan.encryption='psk2'; "
            f"uci set wireless.wifinet_wwan.key={_safe_sh(req.sta_key.strip())}; "
            f"uci commit wireless; (sleep 1; wifi reload) >/dev/null 2>&1 &"
        )
        rc, _, err = _ssh_tr(cmd, timeout=15)
        return {"ok": rc == 0, "error": err}
