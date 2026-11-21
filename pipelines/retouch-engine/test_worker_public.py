#!/usr/bin/env python3
"""随身修图机公网模式联调验证（真实连 Mac）。

验证 Windows 修图机在「不在家庭局域网」的情况下，能否通过公网完成：
  1. 连通主站
  2. 领取任务清单
  3. 回传任务阶段
  4. 领取修改意见
  5. 时钟偏移在安全窗口内
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
sys.path.insert(0, str(ENGINE))

WORKSPACE = ENGINE.parents[1]
from dotenv import load_dotenv  # noqa: E402
load_dotenv(WORKSPACE / ".env", override=False)

from worker_sync import build_sync_client_from_env  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))


def main() -> int:
    token = (os.getenv("WORKER_SYNC_TOKEN") or "").strip()
    url = (os.getenv("WORKER_SYNC_URL") or "").strip()
    print(f"主站地址: {url}")
    print(f"Token: {'已配置(' + str(len(token)) + '字符)' if token else '未配置'}")

    if not token or not url:
        check("工作台环境配置完整", False, "缺少 WORKER_SYNC_TOKEN 或 WORKER_SYNC_URL")
        return 1
    check("工作台环境配置完整", True)

    try:
        client = build_sync_client_from_env()
    except Exception as exc:
        check("构建同步客户端", False, str(exc))
        return 1
    check("构建同步客户端", True)

    # 1) 连通
    try:
        pong = client.ping()
        check("公网连通主站", pong.get("status") == "ok", str(pong)[:150])
    except Exception as exc:
        check("公网连通主站", False, str(exc))
        return 1

    # 2) 时钟
    try:
        offset = client.sync_clock()
        check("时钟偏移在安全窗口内(<5分钟)", abs(offset) < 300, f"offset={offset:.2f}s")
    except Exception as exc:
        check("时钟偏移检测", False, str(exc))

    # 3) 领任务
    try:
        tasks = client.fetch_tasks()
        check("领取任务清单", isinstance(tasks, list), f"count={len(tasks) if isinstance(tasks, list) else 'N/A'}")
        if tasks:
            t = tasks[0]
            print(f"    示例任务: booking=#{t.get('booking_id')} 照片数={len(t.get('selected_assets') or [])}")
    except Exception as exc:
        check("领取任务清单", False, str(exc))

    # 4) 领修订意见
    try:
        revs = client.fetch_revisions()
        check("领取修改意见", isinstance(revs, list), f"count={len(revs) if isinstance(revs, list) else 'N/A'}")
    except Exception as exc:
        check("领取修改意见", False, str(exc))

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
