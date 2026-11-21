#!/usr/bin/env python3
"""ps-bridge 回归测试（自包含）：自己起服务端 + 自己当假插件，跑完整队列链路。

为什么自包含：这台机器上 DSH 的进程跑手会重启宿主，长期后台进程会变成
孤儿并继续占端口 —— 每次起服务前必须先按端口清干净，测完自己收尾。

覆盖：
  1. 未带密钥的插件拉取 -> 401
  2. 内部接口投递 -> 插件拉取 -> 回传成功 -> 投递方拿到结果
  3. 已派发的任务不会重复派发
  4. 插件回传失败 -> 投递方 504 且带错误原因
  5. 状态接口能反映「插件在线 / 最近一次失败」
  6. 非法路径（相对路径）投递 -> 400
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEY = "ps-bridge-e2e-key"
RESULTS = []


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(base, method, path, body=None, key=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-API-Key", key)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))


def wait_ready(base, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/settings", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def run_round(base, ok_flag, tiff_path):
    """投递一个任务，扮演插件取走并回传，返回投递方看到的 (status, body)"""
    box = {}

    def submit():
        box["res"] = http(
            base,
            "POST",
            "/api/internal/ps-bridge/run",
            {"type": "retouch", "payload": {"tiff_path": tiff_path}, "timeout_ms": 20000},
            key=KEY,
        )

    t = threading.Thread(target=submit)
    t.start()

    task = None
    deadline = time.time() + 10
    while time.time() < deadline and task is None:
        st, body = http(base, "GET", "/api/ps-bridge/pending", key=KEY, headers={"X-Bridge-Agent": "test-plugin"})
        if st == 200 and body.get("task"):
            task = body["task"]
        else:
            time.sleep(0.1)

    check("插件能拉取到任务", task is not None)
    if task is None:
        t.join(timeout=25)
        return box.get("res")

    st_dup, body_dup = http(base, "GET", "/api/ps-bridge/pending", key=KEY)
    check("已派发任务不会重复派发", st_dup == 200 and body_dup.get("task") is None, f"body={body_dup}")

    st_res, _ = http(
        base,
        "POST",
        "/api/ps-bridge/result",
        {
            "task_id": task.get("task_id"),
            "ok": ok_flag,
            "result": {"applied": [1, 2, 3], "output_psd_path": "C:/tmp/out.psd"},
            "error": None if ok_flag else "模拟 photoshop 崩溃",
        },
        key=KEY,
    )
    check("插件回传结果被接受", st_res == 200, f"status={st_res}")

    t.join(timeout=25)
    return box.get("res")


def main():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({
        "PORT": str(port),
        "BIND_HOST": "127.0.0.1",
        "DB_PATH": str(HERE / "test_psbridge.db"),
        "INTERNAL_API_KEY": KEY,
        "ADMIN_USER": "admin",
        "ADMIN_PASSWORD": "pw",
        "BIND_ONLY_LOCAL": "1",
    })
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(HERE / "test_psbridge.db") + suffix)
        except OSError:
            pass

    child = subprocess.Popen(
        ["node", "server.js"], cwd=str(HERE), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    try:
        check("服务端启动", wait_ready(base), f"port={port}")
        if not any(ok for _, ok in RESULTS):
            out = child.stdout.read().decode(errors="replace") if child.stdout else ""
            print(out[-2000:])
            return 1

        st, body = http(base, "GET", "/api/ps-bridge/pending")
        check("无密钥拉取被拒绝 (401)", st == 401, f"status={st}")

        res = run_round(base, True, "C:/imgs/ZS_001.tif")
        if res:
            st, body = res
            check("投递方拿到成功结果", st == 200 and body.get("status") == "ok", f"status={st} body={body}")
            check("结果透传插件回包", (body.get("result") or {}).get("applied") == [1, 2, 3], f"body={body}")

        res = run_round(base, False, "C:/imgs/ZS_002.tif")
        if res:
            st, body = res
            check("插件失败 -> 投递方 504", st == 504, f"status={st} body={body}")
            check("失败原因透传", "模拟 photoshop 崩溃" in json.dumps(body, ensure_ascii=False), f"body={body}")

        st, body = http(base, "POST", "/api/internal/ps-bridge/run",
                        {"payload": {"tiff_path": "relative/path.tif"}, "timeout_ms": 500}, key=KEY)
        check("相对路径被拒绝 (400)", st == 400, f"status={st} body={body}")

        # ---- 新增任务类型的服务端校验 ----
        st, body = http(base, "POST", "/api/internal/ps-bridge/run",
                        {"type": "nonsense", "payload": {}, "timeout_ms": 300}, key=KEY)
        check("未知任务类型被拒绝 (400)", st == 400 and "未知任务类型" in json.dumps(body, ensure_ascii=False),
              f"status={st} body={body}")

        st, body = http(base, "POST", "/api/internal/ps-bridge/run",
                        {"type": "jsx", "payload": {"jsx": "   "}, "timeout_ms": 300}, key=KEY)
        check("空 jsx 脚本被拒绝 (400)", st == 400 and "payload.jsx" in json.dumps(body, ensure_ascii=False),
              f"status={st} body={body}")

        st, body = http(base, "POST", "/api/internal/ps-bridge/run",
                        {"type": "place_image", "payload": {"selection": {"left": 0, "top": 0, "width": 10, "height": 10}}, "timeout_ms": 300}, key=KEY)
        check("place_image 缺图源被拒绝 (400)", st == 400 and "image_base64" in json.dumps(body, ensure_ascii=False),
              f"status={st} body={body}")

        st, body = http(base, "POST", "/api/internal/ps-bridge/run",
                        {"type": "place_image", "payload": {"image_base64": "x"}, "timeout_ms": 300}, key=KEY)
        check("place_image 缺选区被拒绝 (400)", st == 400 and "selection" in json.dumps(body, ensure_ascii=False),
              f"status={st} body={body}")

        st, body = http(base, "POST", "/api/internal/ps-bridge/run",
                        {"type": "capture_region", "payload": {"selection": {"left": 0, "top": 0, "right": 5, "bottom": 5}}, "timeout_ms": 1200}, key=KEY)
        check("capture_region 可投递（无插件则 504）", st == 504, f"status={st} body={body}")

        st, body = http(base, "POST", "/api/internal/ps-bridge/run",
                        {"type": "jsx", "payload": {"jsx": "return app.activeDocument.layers.length;"}, "timeout_ms": 1200}, key=KEY)
        check("jsx 可投递（无插件则 504）", st == 504, f"status={st} body={body}")

        st, body = http(base, "GET", "/api/ps-bridge/status")
        plugin = (body or {}).get("plugin") or {}
        check("状态接口显示插件在线", plugin.get("online") is True, f"plugin={plugin}")
        check("状态接口记录最近失败", plugin.get("last_ok") is False and "模拟 photoshop 崩溃" in (plugin.get("last_error") or ""), f"plugin={plugin}")

        passed = sum(1 for _, ok in RESULTS if ok)
        print(f"\n{passed}/{len(RESULTS)} checks passed")
        return 0 if passed == len(RESULTS) else 1
    finally:
        try:
            child.terminate()
            child.wait(timeout=10)
        except Exception:
            child.kill()


if __name__ == "__main__":
    sys.exit(main())
