#!/usr/bin/env python3
"""
ZERO STUDIO · 随身修图机（Windows）→ 主站（Mac）公网同步客户端
============================================================
Mac 常驻做主站；Windows 是随身修图机，可能被带到任何网络环境。
本模块负责让 Windows 的修图 worker 跨公网访问 Mac：

    Windows worker ──HTTPS/HTTP + HMAC ──▶ Mac 的 /api/worker/*

安全设计
--------
- 独立密钥 WORKER_SYNC_TOKEN，与 INTERNAL_API_KEY 完全隔离
- 每个请求带时间戳 + HMAC-SHA256 签名，服务端校验 5 分钟窗口内有效，防重放
- 不触碰 /api/internal/*（那条边界仍然只允许 Mac 本机）

签名算法（与服务端 server.js 保持一致）：
    signPayload = f"{timestamp}.{method}.{path}.{raw_body}"
    signature   = HMAC_SHA256(WORKER_SYNC_TOKEN, signPayload).hexdigest()
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("retouch_engine.worker_sync")


class WorkerSyncClient:
    """随身修图机访问主站的客户端。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 60.0,
        verify_tls: bool = True,
    ):
        """
        base_url: 主站地址，例如 http://156.238.228.39:8083
        token:    WORKER_SYNC_TOKEN（与服务端 .env 一致）
        """
        if not token:
            raise RuntimeError("缺少 WORKER_SYNC_TOKEN，无法通过公网访问主站")
        self.base_url = str(base_url).rstrip("/")
        self.token = token
        self.timeout = float(timeout)
        self.verify_tls = verify_tls

    # ---------- 签名 ----------

    def _sign(self, timestamp: str, method: str, path: str, raw_body: str) -> str:
        payload = f"{timestamp}.{method.upper()}.{path}.{raw_body}"
        return hmac.new(
            self.token.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _headers(self, method: str, path: str, raw_body: str) -> Dict[str, str]:
        ts = str(int(time.time()))
        return {
            "X-Worker-Timestamp": ts,
            "X-Worker-Signature": self._sign(ts, method, path, raw_body),
            "Content-Type": "application/json",
        }

    # ---------- 请求 ----------

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body is not None else ""
        url = f"{self.base_url}{path}"
        # 签名只覆盖 path（不含 query），与服务端 req.path 保持一致
        sign_path = path.split("?", 1)[0]
        headers = self._headers(method, sign_path, raw)

        try:
            with httpx.Client(timeout=self.timeout, verify=self.verify_tls, trust_env=False) as client:
                if method.upper() == "GET":
                    resp = client.get(url, headers=headers)
                else:
                    resp = client.post(url, headers=headers, content=raw.encode("utf-8"))
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"无法连接主站 {url}：{exc}。"
                "请确认 Mac 主站与 FRP 隧道在线。"
            ) from exc

        if resp.status_code == 401:
            raise RuntimeError(f"主站签名校验失败（401）：检查 WORKER_SYNC_TOKEN 是否一致、本机时钟是否准确")
        if resp.status_code == 503:
            raise RuntimeError(f"主站未启用随身修图机通道（503）：{resp.text[:200]}")
        if resp.status_code >= 400:
            raise RuntimeError(f"主站返回错误 (HTTP {resp.status_code}): {resp.text[:300]}")

        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    # ---------- 业务接口 ----------

    def ping(self) -> Dict[str, Any]:
        """连通性检查。"""
        return self._request("GET", "/api/worker/ping")

    def fetch_tasks(self, limit: int = 10) -> List[Dict[str, Any]]:
        """领取待修任务（SELECTED 订单里还没修完的照片）。"""
        data = self._request("GET", f"/api/worker/tasks?limit={int(limit)}")
        return data.get("tasks") or []

    def report_job(
        self,
        booking_id: int,
        asset_id: int,
        stage: str,
        preset_code: str = "",
        plan: Optional[Dict[str, Any]] = None,
        output_path: str = "",
        psd_path: str = "",
        psd_url: str = "",
        preview_out_url: str = "",
        error_msg: str = "",
    ) -> Dict[str, Any]:
        """回传一个修图任务的阶段/产物/错误。"""
        return self._request("POST", "/api/worker/job", {
            "booking_id": int(booking_id),
            "asset_id": int(asset_id),
            "stage": stage,
            "preset_code": preset_code,
            "plan": plan,
            "output_path": output_path,
            "psd_path": psd_path,
            "psd_url": psd_url,
            "preview_out_url": preview_out_url,
            "error_msg": error_msg,
        })

    def fetch_revisions(self) -> List[Dict[str, Any]]:
        """领取待处理的人工修改意见。"""
        data = self._request("GET", "/api/worker/revisions")
        return data.get("revisions") or []

    def report_revision(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """回传一次修订结果。"""
        return self._request("POST", "/api/worker/revision", payload)

    def fail_revision(self, revision_id: int, error: str) -> Dict[str, Any]:
        """标记修订失败，避免被无限重试。"""
        return self._request("POST", "/api/worker/revision/fail", {
            "revision_id": int(revision_id),
            "error": str(error)[:400],
        })

    # ---------- 文件上传 ----------

    def upload_file(self, local_path: Path, remote_name: Optional[str] = None) -> Dict[str, Any]:
        """把修图产物（PSD 或预览图）上传回 Mac，返回可访问 URL。

        注意：multipart 无法参与原始体签名，因此这里改用 X-Worker-Token 头。
        """
        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(f"待上传文件不存在: {local_path}")

        url = f"{self.base_url}/api/worker/upload"
        headers = {"X-Worker-Token": self.token}
        try:
            with httpx.Client(timeout=max(self.timeout, 300.0), verify=self.verify_tls, trust_env=False) as client:
                with open(local_path, "rb") as fh:
                    files = {"file": (remote_name or local_path.name, fh, "application/octet-stream")}
                    resp = client.post(url, headers=headers, files=files)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"上传到主站失败：{exc}") from exc

        if resp.status_code >= 400:
            raise RuntimeError(f"上传被拒绝 (HTTP {resp.status_code}): {resp.text[:300]}")

        return resp.json()

    # ---------- 公网文件下载（离家不在局域网时按需从 Mac 拉原片/上一版 PSD） ----------

    def download_asset_raw(self, asset_id: int, dest_path: Path) -> Path:
        """通过公网从 Mac 主站流式下载客户选中的原片到 Windows 本地缓存。"""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.base_url}/api/worker/assets/{int(asset_id)}/raw"
        headers = {"X-Worker-Token": self.token}
        tmp_path = dest_path.with_name(dest_path.name + ".part")
        with httpx.Client(timeout=max(self.timeout, 300.0), verify=self.verify_tls, trust_env=False) as client:
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(f"从 Mac 公网拉取原片失败 (asset #{asset_id}, HTTP {resp.status_code}): {resp.read().decode(errors='ignore')[:200]}")
                with open(tmp_path, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                        fh.write(chunk)
        tmp_path.replace(dest_path)
        logger.info("已通过公网从 Mac 拉取原片: asset=#%d -> %s", asset_id, dest_path.name)
        return dest_path

    def download_output_file(self, remote_url: str, dest_path: Path) -> Path:
        """通过公网从 Mac 主站下载上一版 PSD 或预览图到 Windows 本地缓存。"""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.base_url}/api/worker/file"
        headers = {"X-Worker-Token": self.token}
        tmp_path = dest_path.with_name(dest_path.name + ".part")
        with httpx.Client(timeout=max(self.timeout, 300.0), verify=self.verify_tls, trust_env=False) as client:
            with client.stream("GET", url, headers=headers, params={"url": remote_url}) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(f"从 Mac 公网拉取产物失败 ({remote_url}, HTTP {resp.status_code})")
                with open(tmp_path, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                        fh.write(chunk)
        tmp_path.replace(dest_path)
        return dest_path

    def sync_clock(self) -> float:
        """用主站时间校正本地时钟偏移（HMAC 对时钟敏感）。返回偏移秒数。"""
        data = self.ping()
        server_time = data.get("time")
        if not server_time:
            return 0.0
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(server_time.replace("Z", "+00:00"))
            return dt.timestamp() - time.time()
        except Exception:
            return 0.0


def build_sync_client_from_env(env: Optional[Dict[str, str]] = None) -> WorkerSyncClient:
    """从环境变量构建客户端（Windows 端使用）。"""
    import os
    e = env or os.environ
    base_url = (
        e.get("WORKER_SYNC_URL")
        or f"http://{e.get('VPS_HOST', '156.238.228.39')}:{e.get('FRP_WORKER_PORT', '8083')}"
    )
    return WorkerSyncClient(
        base_url=base_url,
        token=e.get("WORKER_SYNC_TOKEN", ""),
        verify_tls=str(e.get("WORKER_SYNC_TLS", "false")).lower() == "true",
    )
