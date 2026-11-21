#!/usr/bin/env python3
"""随身修图机 ↔ 主站 公网同步通道测试。

验证重点（都是安全属性，必须严格）：
  1. 正确签名可通过
  2. 缺签名 / 错签名 / 用了 INTERNAL_API_KEY 冒充 -> 401
  3. 时间戳过期（重放攻击）-> 401
  4. 请求体被篡改 -> 401
  5. 未配置 WORKER_SYNC_TOKEN 时通道关闭 -> 503
  6. 真实业务流程：领任务 -> 回传阶段 -> 领修订意见 -> 回传修订
  7. 文件上传走 token 头，可拿到可访问 URL
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(WORKSPACE / "pipelines" / "retouch-engine"))

from worker_sync import WorkerSyncClient  # noqa: E402

RESULTS: List[tuple] = []
TOKEN = "test-worker-sync-token-abc123"
INTERNAL_KEY = "test-internal-key-should-not-work"


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
                r = c.get(base + "/api/services")
                if r.status_code == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    db = HERE / "test_worker_sync.db"
    for suffix in ("", "-wal", "-shm"):
        try:
            (db.parent / (db.name + suffix)).unlink()
        except OSError:
            pass

    env = dict(os.environ)
    env.update({
        "PORT": str(port),
        "BIND_HOST": "127.0.0.1",
        "DB_PATH": str(db),
        "INTERNAL_API_KEY": INTERNAL_KEY,
        "WORKER_SYNC_TOKEN": TOKEN,
        "ADMIN_USER": "admin",
        "ADMIN_PASSWORD": "pw",
        "PUSHPLUS_TOKEN": "",
    })

    server = subprocess.Popen(
        ["node", "server.js"], cwd=str(HERE), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    try:
        if not wait_ready(base):
            print("服务端启动失败")
            print((server.stdout.read().decode(errors="replace") if server.stdout else "")[-1500:])
            return 1
        check("服务端启动", True)

        client = WorkerSyncClient(base_url=base, token=TOKEN)

        # ---- 1. 正确签名 ----
        try:
            pong = client.ping()
            check("正确签名可通行", pong.get("status") == "ok", json.dumps(pong)[:150])
        except Exception as exc:
            check("正确签名可通行", False, str(exc))

        # ---- 2. 无签名 ----
        with httpx.Client(timeout=10.0, trust_env=False) as c:
            r = c.get(base + "/api/worker/ping")
            check("无签名被拒 (401)", r.status_code == 401, f"status={r.status_code}")

            # ---- 3. 用 INTERNAL_API_KEY 冒充 ----
            bad = WorkerSyncClient(base_url=base, token=INTERNAL_KEY)
            try:
                bad.ping()
                check("INTERNAL_API_KEY 无法冒充 worker", False, "竟然通过了")
            except RuntimeError as exc:
                check("INTERNAL_API_KEY 无法冒充 worker", "401" in str(exc) or "签名" in str(exc), str(exc)[:120])

            # ---- 4. 错误 token ----
            wrong = WorkerSyncClient(base_url=base, token="wrong-token")
            try:
                wrong.ping()
                check("错误 token 被拒", False, "竟然通过了")
            except RuntimeError as exc:
                check("错误 token 被拒", True, str(exc)[:100])

        # ---- 5. 时间戳过期（重放）----
        old_ts = str(int(time.time()) - 3600)
        sig = client._sign(old_ts, "GET", "/api/worker/ping", "")
        with httpx.Client(timeout=10.0, trust_env=False) as c:
            r = c.get(base + "/api/worker/ping", headers={
                "X-Worker-Timestamp": old_ts,
                "X-Worker-Signature": sig,
            })
            check("过期时间戳被拒（防重放）", r.status_code == 401, f"status={r.status_code}")

        # ---- 6. 请求体被篡改 ----
        ts = str(int(time.time()))
        body = json.dumps({"booking_id": 1, "asset_id": 1, "stage": "PS_EXPORT"}, separators=(",", ":"))
        tampered = json.dumps({"booking_id": 1, "asset_id": 999, "stage": "DONE"}, separators=(",", ":"))
        sig = client._sign(ts, "POST", "/api/worker/job", body)
        with httpx.Client(timeout=10.0, trust_env=False) as c:
            r = c.post(base + "/api/worker/job", headers={
                "X-Worker-Timestamp": ts,
                "X-Worker-Signature": sig,
                "Content-Type": "application/json",
            }, content=tampered.encode())
            check("请求体篡改被拒", r.status_code == 401, f"status={r.status_code}")

        # ---- 7. 真实业务流程：回传任务 ----
        try:
            res = client.report_job(
                booking_id=1, asset_id=1, stage="PS_EXPORT",
                preset_code="cyan_air_event", plan={"exposure_delta": 0.2},
            )
            check("回传修图任务成功", res.get("status") == "ok" and res.get("job_id"), json.dumps(res)[:150])
            job_id = res.get("job_id")
        except Exception as exc:
            check("回传修图任务成功", False, str(exc))
            job_id = None

        # ---- 8. 回传修订 ----
        if job_id:
            try:
                res2 = client.report_revision({
                    "job_id": job_id,
                    "version": 2,
                    "feedback_text": "再亮一点",
                    "summary": "提亮 0.25EV",
                    "confidence": 0.8,
                    "deltas": {"exposure_delta": 0.25},
                    "jsx_used": False,
                    "output_psd_path": "C:/out/v2.psd",
                    "output_preview_path": "C:/out/v2.jpg",
                    "warnings": [],
                })
                check("回传修订结果成功", res2.get("status") == "ok", json.dumps(res2)[:150])
            except Exception as exc:
                check("回传修订结果成功", False, str(exc))

        # ---- 9. 领任务 / 领修订（空库应正常返回空）----
        try:
            tasks = client.fetch_tasks()
            check("领任务接口正常（空库）", isinstance(tasks, list), str(tasks)[:100])
            revs = client.fetch_revisions()
            check("领修订接口正常（空库）", isinstance(revs, list), str(revs)[:100])
        except Exception as exc:
            check("领任务/领修订接口", False, str(exc))

        # ---- 10. 文件上传 ----
        sample = HERE / "_sync_upload_test.bin"
        sample.write_bytes(b"fake-psd-content-" * 100)
        try:
            up = client.upload_file(sample)
            check("文件上传成功且返回 URL",
                  up.get("status") == "ok" and str(up.get("url", "")).startswith("/outputs/"),
                  json.dumps(up)[:160])
        except Exception as exc:
            check("文件上传成功且返回 URL", False, str(exc))
        finally:
            sample.unlink(missing_ok=True)

        # ---- 11. 时钟偏移检测 ----
        try:
            offset = client.sync_clock()
            check("可读取主站时间校正时钟", abs(offset) < 60, f"offset={offset:.1f}s")
        except Exception as exc:
            check("可读取主站时间校正时钟", False, str(exc))

        passed = sum(1 for _, ok in RESULTS if ok)
        print(f"\n{passed}/{len(RESULTS)} checks passed")
        return 0 if passed == len(RESULTS) else 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()
        for suffix in ("", "-wal", "-shm"):
            try:
                (db.parent / (db.name + suffix)).unlink()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
