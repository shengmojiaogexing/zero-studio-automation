#!/usr/bin/env python3
"""验证内部接口的 Host 头伪造防护。

安全背景：isLoopbackRequest 原先只看 Host 头，而 Host 头由客户端控制。
外部请求伪造 `Host: 127.0.0.1` 就能骗过检查，直连内部接口改订单/改状态。

修复后：同时校验 socket 真实来源 IP 必须是回环地址。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Tuple

import httpx

HERE = Path(__file__).resolve().parent
RESULTS: List[Tuple[str, bool]] = []
KEY = "loopback-test-key"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_ready(base: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=2.0, trust_env=False) as c:
                if c.get(base + "/api/services").status_code == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    db = HERE / "test_loopback.db"
    for s in ("", "-wal", "-shm"):
        try:
            (db.parent / (db.name + s)).unlink()
        except OSError:
            pass

    env = dict(os.environ)
    env.update({
        "PORT": str(port), "BIND_HOST": "127.0.0.1", "DB_PATH": str(db),
        "INTERNAL_API_KEY": KEY, "ADMIN_USER": "admin", "ADMIN_PASSWORD": "pw",
        "PUSHPLUS_TOKEN": "", "WORKER_SYNC_TOKEN": "",
    })
    srv = subprocess.Popen(["node", "server.js"], cwd=str(HERE), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        if not wait_ready(base):
            print("服务端启动失败")
            return 1

        with httpx.Client(timeout=10.0, trust_env=False) as c:
            # 1) 正常本机访问内部接口 -> 应放行（200/正常业务码）
            r = c.get(f"{base}/api/internal/retouch/pending", headers={"X-API-Key": KEY})
            check("本机正常访问内部接口放行", r.status_code != 403, f"status={r.status_code}")

            # 2) 伪造 Host 但来源不是回环 —— 模拟从隧道进来的外部请求
            #    直接构造 socket 并用非回环源地址不可行，这里改为验证「Host 非回环」被拒
            r2 = c.get(f"{base}/api/internal/retouch/pending",
                       headers={"X-API-Key": KEY, "Host": "156.238.228.39:8080"})
            check("非回环 Host 被拒 (403)", r2.status_code == 403, f"status={r2.status_code}")

            # 3) 带 X-Forwarded-For 一律拒绝（反代绕过）
            r3 = c.get(f"{base}/api/internal/retouch/pending",
                       headers={"X-API-Key": KEY, "X-Forwarded-For": "1.2.3.4"})
            check("带 X-Forwarded-For 被拒 (403)", r3.status_code == 403, f"status={r3.status_code}")

            # 4) 内部接口缺 key -> 401（且不是 403，说明来源检查已通过）
            r4 = c.get(f"{base}/api/internal/retouch/pending")
            check("本机缺密钥为 401（非 403）", r4.status_code == 401, f"status={r4.status_code}")

            # 5) worker 通道未配置 token -> 503（明确告知未启用，而非静默放行）
            ts = str(int(time.time()))
            r5 = c.get(f"{base}/api/worker/ping",
                       headers={"X-Worker-Timestamp": ts, "X-Worker-Signature": "deadbeef"})
            check("worker 未启用时返回 503", r5.status_code == 503, f"status={r5.status_code}")

            # 6) 公网站点仍可正常访问（加固不能伤及客户）
            r6 = c.get(f"{base}/api/services")
            check("公网客户接口不受影响", r6.status_code == 200, f"status={r6.status_code}")

        passed = sum(1 for _, ok in RESULTS if ok)
        print(f"\n{passed}/{len(RESULTS)} checks passed")
        return 0 if passed == len(RESULTS) else 1
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
        for s in ("", "-wal", "-shm"):
            try:
                (db.parent / (db.name + s)).unlink()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
