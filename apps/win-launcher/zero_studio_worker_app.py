#!/usr/bin/env python3
"""
ZERO STUDIO · Windows 随身修图机桌面客户端 (ZeroStudio随身修图机.exe)
==================================================================
双击即可打开可视化控制面板：
- 一键启动 / 停止 Windows 随身修图引擎 (worker.py --interval 10)
- 自动检测本机 Photoshop COM 零干预状态与 Mac 公网主站连通性
- 实时查看修图与公网回传日志
- 一键打开 Mac 外网中控台（看成片、在图片下方提修改意见给 AI、一键放行）
- 一键同步最新代码与后台监控到 Mac
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, scrolledtext


def resolve_project_root() -> Path:
    """自动定位 zero-studio-automation 项目根目录（兼容脚本运行与 PyInstaller .exe 打包运行）。"""
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([exe_dir, exe_dir.parent, exe_dir.parent.parent])
    else:
        here = Path(__file__).resolve().parent
        candidates.extend([here.parents[1], here.parent, here])
    candidates.append(Path(r"D:\zero-studio-automation"))

    for c in candidates:
        if (c / "pipelines" / "retouch-engine" / "worker.py").exists():
            return c
    return candidates[0]


def load_env_dict(root: Path) -> dict[str, str]:
    env_path = root / ".env"
    data: dict[str, str] = {}
    if not env_path.exists():
        return data
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def find_ps_exe(explicit: str = "") -> str:
    if explicit and Path(explicit).is_file():
        return explicit
    for pat in (
        r"C:\Program Files\Adobe\Adobe Photoshop*\Photoshop.exe",
        r"D:\Program Files\Adobe\Adobe Photoshop*\Photoshop.exe",
    ):
        matches = sorted(glob.glob(pat), reverse=True)
        if matches:
            return matches[0]
    return ""


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return str(pid) in (proc.stdout or "")
    except Exception:
        return False


class ZeroStudioWorkerApp:
    def __init__(self) -> None:
        self.root_dir = resolve_project_root()
        self.logs_dir = self.root_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file = self.logs_dir / "win_worker.pid"
        self.err_log = self.logs_dir / "win-retouch-worker.stderr.log"
        self.out_log = self.logs_dir / "win-retouch-worker.stdout.log"
        self.env_cfg = load_env_dict(self.root_dir)

        self.win = tk.Tk()
        self.win.title("ZERO STUDIO · Windows 随身修图机")
        self.win.geometry("760x560")
        self.win.minsize(680, 500)
        self.win.configure(bg="#11141a")
        self.win.protocol("WM_DELETE_WINDOW", self.on_close)

        self._last_log_text = ""
        self._build_ui()
        self._refresh_status()
        self._schedule_tick()

    def _build_ui(self) -> None:
        bg = "#11141a"
        card = "#1a1f28"
        fg = "#eef0f4"
        muted = "#94a3b8"
        teal = "#6fa8a5"
        pink = "#e2bac8"

        # Header
        hdr = tk.Frame(self.win, bg=bg)
        hdr.pack(fill="x", padx=20, pady=(18, 10))
        tk.Label(
            hdr,
            text="ZERO STUDIO · Windows 随身修图机",
            font=("Microsoft YaHei UI", 16, "bold"),
            bg=bg,
            fg=fg,
        ).pack(anchor="w")
        tk.Label(
            hdr,
            text=f"项目目录：{self.root_dir}   |   Mac 主站常驻 · Windows 随时带走 · 公网实时回传",
            font=("Microsoft YaHei UI", 9),
            bg=bg,
            fg=muted,
        ).pack(anchor="w", pady=(3, 0))

        # Status Cards
        cards = tk.Frame(self.win, bg=bg)
        cards.pack(fill="x", padx=20, pady=6)
        for i in range(3):
            cards.columnconfigure(i, weight=1, uniform="stat")

        self.lbl_worker = self._make_card(cards, 0, "修图 Worker 状态", "检测中...", card, muted)
        self.lbl_ps = self._make_card(cards, 1, "Photoshop 零干预 COM", "检测中...", card, muted)
        self.lbl_mac = self._make_card(cards, 2, "Mac 公网主站通道", "检测中...", card, muted)

        # Action Buttons
        btns = tk.Frame(self.win, bg=bg)
        btns.pack(fill="x", padx=20, pady=12)

        self.btn_start = tk.Button(
            btns,
            text="▶  一键启动修图机",
            command=self.start_worker,
            font=("Microsoft YaHei UI", 10, "bold"),
            bg=teal,
            fg="#0b0d12",
            activebackground="#84beb9",
            relief="flat",
            padx=16,
            pady=9,
            cursor="hand2",
        )
        self.btn_start.pack(side="left", padx=(0, 8))

        self.btn_stop = tk.Button(
            btns,
            text="⏹  停止修图机",
            command=self.stop_worker,
            font=("Microsoft YaHei UI", 10, "bold"),
            bg="#2b3240",
            fg="#fca5a5",
            activebackground="#394254",
            relief="flat",
            padx=14,
            pady=9,
            cursor="hand2",
        )
        self.btn_stop.pack(side="left", padx=(0, 8))

        tk.Button(
            btns,
            text="🌐  打开 Mac 中控台",
            command=self.open_control_center,
            font=("Microsoft YaHei UI", 10, "bold"),
            bg=pink,
            fg="#0b0d12",
            activebackground="#ecccd8",
            relief="flat",
            padx=14,
            pady=9,
            cursor="hand2",
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            btns,
            text="🔄  同步并部署到 Mac",
            command=self.sync_to_mac,
            font=("Microsoft YaHei UI", 9),
            bg="#232936",
            fg=fg,
            activebackground="#303849",
            relief="flat",
            padx=12,
            pady=9,
            cursor="hand2",
        ).pack(side="right")

        # Live Log Box
        log_hdr = tk.Frame(self.win, bg=bg)
        log_hdr.pack(fill="x", padx=20, pady=(4, 4))
        tk.Label(
            log_hdr,
            text="实时运行日志（领任务 · XMP · 自动启停 PS · 公网回传 Mac）",
            font=("Microsoft YaHei UI", 9, "bold"),
            bg=bg,
            fg=muted,
        ).pack(side="left")
        tk.Button(
            log_hdr,
            text="测试 Mac 连通",
            command=self.ping_mac_async,
            font=("Microsoft YaHei UI", 8),
            bg="#232936",
            fg=teal,
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
        ).pack(side="right")

        self.log_box = scrolledtext.ScrolledText(
            self.win,
            bg="#0b0e13",
            fg="#a7f3d0",
            insertbackground="#ffffff",
            font=("Consolas", 9),
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=10,
        )
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(0, 18))
        self.log_box.configure(state="disabled")

    def _make_card(self, parent: tk.Frame, col: int, title: str, initial: str, bg: str, muted: str) -> tk.Label:
        f = tk.Frame(parent, bg=bg, highlightbackground="#2a3140", highlightthickness=1)
        f.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0))
        tk.Label(f, text=title, font=("Microsoft YaHei UI", 8), bg=bg, fg=muted).pack(anchor="w", padx=12, pady=(10, 2))
        val = tk.Label(f, text=initial, font=("Microsoft YaHei UI", 10, "bold"), bg=bg, fg="#eef0f4")
        val.pack(anchor="w", padx=12, pady=(0, 10))
        return val

    def _read_pid(self) -> int:
        try:
            return int(self.pid_file.read_text(encoding="ascii").strip())
        except Exception:
            return 0

    def _refresh_status(self) -> None:
        pid = self._read_pid()
        alive = is_pid_alive(pid)
        if alive:
            self.lbl_worker.configure(text=f"● 运行中 (PID {pid})", fg="#6ee7b7")
            self.btn_start.configure(text="↻  重启修图机")
        else:
            self.lbl_worker.configure(text="○ 已停止", fg="#fcd34d")
            self.btn_start.configure(text="▶  一键启动修图机")

        ps_exe = find_ps_exe(self.env_cfg.get("PS_EXE_PATH", ""))
        if ps_exe:
            ps_ver = Path(ps_exe).parent.name.replace("Adobe ", "")
            self.lbl_ps.configure(text=f"● 就绪 ({ps_ver})", fg="#6ee7b7")
        else:
            self.lbl_ps.configure(text="⚠ 未找到 Photoshop", fg="#fca5a5")

        sync_url = self.env_cfg.get("WORKER_SYNC_URL") or f"http://{self.env_cfg.get('VPS_HOST', 'your-vps-ip')}:8083"
        self.lbl_mac.configure(text=sync_url.replace("http://", "").replace("https://", ""), fg="#93c5fd")

        # Update log tail
        if self.err_log.exists():
            try:
                lines = self.err_log.read_text(encoding="utf-8", errors="ignore").splitlines()[-80:]
                text = "\n".join(lines)
                if text != self._last_log_text:
                    self._last_log_text = text
                    self.log_box.configure(state="normal")
                    self.log_box.delete("1.0", "end")
                    self.log_box.insert("end", text)
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
            except Exception:
                pass

    def _schedule_tick(self) -> None:
        self._refresh_status()
        self.win.after(2500, self._schedule_tick)

    def start_worker(self) -> None:
        ps1 = self.root_dir / "start_win_worker.ps1"
        if not ps1.exists():
            messagebox.showerror("缺少脚本", f"未找到 {ps1}")
            return
        self.btn_start.configure(state="disabled", text="启动中...")

        def _run() -> None:
            try:
                subprocess.run(
                    ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                    cwd=str(self.root_dir),
                    capture_output=True,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            finally:
                self.win.after(200, lambda: (self.btn_start.configure(state="normal"), self._refresh_status()))

        threading.Thread(target=_run, daemon=True).start()

    def stop_worker(self) -> None:
        ps1 = self.root_dir / "stop_win_worker.ps1"
        if not ps1.exists():
            return
        subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            cwd=str(self.root_dir),
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._refresh_status()

    def open_control_center(self) -> None:
        vps = self.env_cfg.get("VPS_HOST") or "your-vps-ip"
        port = self.env_cfg.get("FRP_PUBLIC_STUDIO_PORT") or "8081"
        webbrowser.open(f"http://{vps}:{port}")

    def sync_to_mac(self) -> None:
        bat = self.root_dir / "同步并部署到Mac.bat"
        if not bat.exists():
            messagebox.showerror("缺少脚本", f"未找到 {bat}")
            return
        subprocess.Popen(["cmd", "/c", "start", "", str(bat)], cwd=str(self.root_dir))

    def ping_mac_async(self) -> None:
        def _ping() -> None:
            vps = self.env_cfg.get("VPS_HOST") or "your-vps-ip"
            port = self.env_cfg.get("FRP_PUBLIC_BOOKING_PORT") or "8080"
            url = f"http://{vps}:{port}/api/settings"
            t0 = time.time()
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url, timeout=5) as resp:
                    ms = int((time.time() - t0) * 1000)
                    msg = f"● Mac 在线 ({ms}ms)"
                    color = "#6ee7b7"
            except Exception:
                msg = "✗ Mac 暂不可达"
                color = "#fca5a5"
            self.win.after(0, lambda: self.lbl_mac.configure(text=msg, fg=color))

        threading.Thread(target=_ping, daemon=True).start()

    def on_close(self) -> None:
        pid = self._read_pid()
        if is_pid_alive(pid):
            ans = messagebox.askyesnocancel(
                "关闭窗口",
                "Windows 随身修图机正在后台运行。\n\n"
                "• 点击「是」：保持修图机在后台继续运行，仅关闭此窗口\n"
                "• 点击「否」：停止修图机并退出\n"
                "• 点击「取消」：留在当前窗口",
            )
            if ans is None:
                return
            if ans is False:
                self.stop_worker()
        self.win.destroy()

    def run(self) -> None:
        self.ping_mac_async()
        self.win.mainloop()


if __name__ == "__main__":
    ZeroStudioWorkerApp().run()
