#!/usr/bin/env python3
"""
ZERO STUDIO Automation · 商业一体化一键部署初始化向导 (setup_studio.py)
======================================================================
让任意摄影工作室拿到代码后 3 分钟内完成环境体检、强密钥生成、配置初始化与依赖安装：
  python setup_studio.py                # 交互式向导
  python setup_studio.py --check        # 仅做环境体检与配置初始化（非交互，适合脚本/CI）
  python setup_studio.py --install-deps # 初始化并自动安装 Node / Python 依赖
"""

from __future__ import annotations

import argparse
import glob
import os
import secrets
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple

ROOT_DIR = Path(__file__).resolve().parent


def _cmd_version(cmd: List[str]) -> Tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
        out = (p.stdout or p.stderr or "").strip().splitlines()
        if p.returncode == 0 and out:
            return True, out[0].strip()
    except Exception:
        pass
    return False, "未检测到"


def _find_photoshop() -> List[str]:
    found: List[str] = []
    if os.name == "nt":
        for pat in (
            r"C:\Program Files\Adobe\Adobe Photoshop*\Photoshop.exe",
            r"D:\Program Files\Adobe\Adobe Photoshop*\Photoshop.exe",
        ):
            found.extend(sorted(glob.glob(pat), reverse=True))
    elif sys.platform == "darwin":
        found.extend(sorted(glob.glob("/Applications/Adobe Photoshop*/Adobe Photoshop*.app"), reverse=True))
    return found


def _check_lm_studio(url: str = "http://127.0.0.1:1234/v1/models") -> Tuple[bool, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ZeroStudioSetup/1.0"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return True, f"在线 ({url})"
    except Exception:
        pass
    return False, "未启动（可选，用于 2B/4B 闲聊守门与 8B MCP 拆解）"


def init_config_files() -> Dict[str, str]:
    """从模板初始化 .env 与 configs/im_rules.json，并自动生成强随机密钥。"""
    results: Dict[str, str] = {}

    env_path = ROOT_DIR / ".env"
    env_example = ROOT_DIR / ".env.example"
    if not env_path.exists() and env_example.exists():
        lines = env_example.read_text(encoding="utf-8").splitlines()
        out_lines: List[str] = []
        for line in lines:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                key = k.strip()
                val = v.strip()
                if val.startswith("change-me") and key != "ADMIN_PASSWORD":
                    val = secrets.token_hex(24)
                out_lines.append(f"{key}={val}")
            else:
                out_lines.append(line)
        env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        results[".env"] = "已从 .env.example 生成并自动填充高强度随机密钥（请修改 ADMIN_PASSWORD 与 API Key）"
    elif env_path.exists():
        results[".env"] = "已存在（保持原配置不变）"
    else:
        results[".env"] = "缺少 .env.example 模板"

    im_rules = ROOT_DIR / "configs" / "im_rules.json"
    im_example = ROOT_DIR / "configs" / "im_rules.example.json"
    if not im_rules.exists() and im_example.exists():
        shutil.copy2(im_example, im_rules)
        results["configs/im_rules.json"] = "已从商业模板 configs/im_rules.example.json 初始化"
    elif im_rules.exists():
        results["configs/im_rules.json"] = "已存在（保持原配置不变）"

    for rel in ("logs", "work/retouch", "work/pose_sheets", "configs/retouch_xmp", "configs/preview_xmp"):
        (ROOT_DIR / rel).mkdir(parents=True, exist_ok=True)
    results["work_dirs"] = "运行目录已就绪"
    return results


def check_environment() -> List[Tuple[str, bool, str]]:
    checks: List[Tuple[str, bool, str]] = []
    py_ok = sys.version_info >= (3, 10)
    checks.append(("Python (>=3.10)", py_ok, f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"))

    node_ok, node_ver = _cmd_version(["node", "-v"])
    checks.append(("Node.js (>=18)", node_ok, node_ver))

    npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
    npm_ok, npm_ver = _cmd_version([npm_cmd, "-v"])
    checks.append(("npm", npm_ok, npm_ver))

    ps_list = _find_photoshop()
    checks.append(("Adobe Photoshop", bool(ps_list), ps_list[0] if ps_list else "未检测到（仅修图机需要）"))

    adb_proxy = ROOT_DIR / "infra" / "adb-mcp" / "adb-proxy-socket" / "proxy.js"
    checks.append(("mikechambers/adb-mcp", adb_proxy.exists(), str(adb_proxy) if adb_proxy.exists() else "未克隆（可选：git clone https://github.com/mikechambers/adb-mcp.git infra/adb-mcp）"))

    exif_ok, exif_ver = _cmd_version(["exiftool", "-ver"])
    checks.append(("ExifTool (可选)", exif_ok, exif_ver))

    lm_ok, lm_msg = _check_lm_studio()
    checks.append(("LM Studio 本地模型 (可选)", lm_ok, lm_msg))
    return checks


def install_dependencies() -> None:
    print("\n[3/3] 正在安装 Node.js 与 Python 依赖...")
    booking_dir = ROOT_DIR / "apps" / "booking-site"
    npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
    if (booking_dir / "package.json").exists():
        subprocess.run([npm_cmd, "install"], cwd=str(booking_dir), check=False)

    req_files = [
        ROOT_DIR / "apps" / "control-center" / "requirements.txt",
        ROOT_DIR / "services" / "im-gateway" / "requirements.txt",
        ROOT_DIR / "pipelines" / "ingest-cull-cluster" / "requirements.txt",
        ROOT_DIR / "pipelines" / "retouch-engine" / "requirements.txt",
    ]
    for req in req_files:
        if req.exists():
            print(f"  -> pip install -r {req.relative_to(ROOT_DIR)}")
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)], check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="ZERO STUDIO Automation 一键部署初始化向导")
    parser.add_argument("--check", action="store_true", help="非交互模式：仅检查环境并初始化缺失配置")
    parser.add_argument("--install-deps", action="store_true", help="自动安装 Node 与 Python 依赖")
    args = parser.parse_args()

    print("=" * 64)
    print("  ZERO STUDIO Automation · 商业一体化初始化向导 (Setup Wizard)")
    print("=" * 64)

    print("\n[1/3] 环境体检:")
    for name, ok, detail in check_environment():
        mark = "OK  " if ok else "WARN"
        print(f"  [{mark}] {name:<26} : {detail}")

    print("\n[2/3] 配置文件初始化:")
    for k, msg in init_config_files().items():
        print(f"  - {k}: {msg}")

    if args.install_deps:
        install_dependencies()
    elif not args.check:
        ans = input("\n是否现在自动安装 Node.js 与 Python 依赖？[y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            install_dependencies()

    print("\n" + "=" * 64)
    print("  初始化完成！下一步：")
    print("  1. 编辑 .env，设置 ADMIN_PASSWORD 与 DEEPSEEK_API_KEY / VISION_API_KEY")
    print("  2. 启动全栈服务：双击 启动_ZERO_STUDIO.bat 或运行 python studio_supervisor.py")
    print("  3. 打开 http://127.0.0.1:3000/admin.html 填写工作室品牌名、微信号与 QQ 群")
    print("  4. 打开 http://127.0.0.1:3080 进入统一可视化中控台")
    print("=" * 64)


if __name__ == "__main__":
    main()
