#!/usr/bin/env python3
"""
ZERO STUDIO · Photoshop 实机修图交互测试实验室 (test_ps_interactive.py)
======================================================================
支持两种运行方式：
1. 图形界面（默认，双击 `测试_PS修图实验室.bat`）：
   - 选择待修原图（.ARW / .CR3 / .JPG / .PNG / .TIFF）
   - 选择仿色参考图（可选）
   - 选择基础调色 .xmp 预设文件（可选）
   - 选择驱动方式：PS COM（全 26 工具）/ adb-mcp UXP / 离线演练（不连 PS）
   - 勾选是否启用检查点保存（每 3 步存一次 PSD）
   - 实时查看处理日志，完成后一键打开输出目录查验分层 PSD 与预览 JPG！
2. 命令行模式：
   python pipelines/retouch-engine/test_ps_interactive.py --input a.arw --ref ref.jpg --xmp p.xmp --backend com
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

ENGINE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = ENGINE_DIR.parents[1]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(WORKSPACE_ROOT / ".env", override=False)

from gemini_colorist import GeminiColorist, clamp_retouch_plan  # noqa: E402
from mcp_planner import Local8BMCPPlanner  # noqa: E402
from ps_mcp_server import PSMCPToolkit  # noqa: E402
from xmp_raw_stage import convert_raw_with_xmp_to_tiff  # noqa: E402


def _fallback_histogram_plan(src_preview: Path, ref_path: Optional[Path], notes: str) -> Dict[str, Any]:
    """当未配置云端视觉 API Key 或离线演练时，基于原图/参考图直方图生成安全合理的测试调色方案。"""
    import numpy as np
    from PIL import Image

    with Image.open(src_preview) as im:
        src_arr = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0

    exp_delta, temp_delta, tint_delta, skin_lum = 0.15, -2.5, 0.5, 4.5
    if ref_path and ref_path.exists():
        with Image.open(ref_path) as rim:
            ref_arr = np.asarray(rim.convert("RGB"), dtype=np.float32) / 255.0
        exp_delta = float(np.clip((ref_arr.mean() - src_arr.mean()) * 1.6, -0.8, 0.8))
        src_rb = float(src_arr[:, :, 0].mean() - src_arr[:, :, 2].mean())
        ref_rb = float(ref_arr[:, :, 0].mean() - ref_arr[:, :, 2].mean())
        temp_delta = float(np.clip((ref_rb - src_rb) * 40.0, -10.0, 10.0))

    return clamp_retouch_plan({
        "exposure_delta": round(min(0.25, exp_delta), 3),
        "temp_delta": round(temp_delta, 3),
        "tint_delta": round(tint_delta, 3),
        "hsl_adjustments": {
            "oranges": {"hue": 1, "saturation": -4, "luminance": 6},
            "yellows": {"hue": -4, "saturation": -10, "luminance": 2},
            "blues": {"hue": -2, "saturation": 8, "luminance": 2},
        },
        "skin_luminance": skin_lum,
        "curves_points": [[0, 2], [64, 60], [128, 134], [192, 198], [255, 252]],
        "selective_color": [{"color_range": "neutrals", "cyan": 3, "yellow": -2}],
        "subject_mask_adjust": {"enabled": True, "exposure_delta": 0.45, "temp_delta": 1.5, "feather_px": 10},
        "background_mask_adjust": {"enabled": True, "exposure_delta": -0.55, "temp_delta": -5.0, "saturation_delta": -18, "feather_px": 14},
        "skin_protect_mask": {"enabled": True, "fuzziness": 38, "exposure_delta": 0.25, "temp_delta": 3.0, "feather_px": 6},
        "finishing": {"high_pass_sharpen": True, "high_pass_radius": 1.8, "high_pass_opacity": 45, "neutral_gray_layer": True, "frequency_separation": False},
        "summary": f"分区蒙版测试方案（{notes or '主体提亮+背景压暗+肤色保护+高反差锐化'}）",
    })


def run_ps_test(
    input_path: Path,
    ref_path: Optional[Path] = None,
    xmp_path: Optional[Path] = None,
    backend: str = "com",
    notes: str = "",
    preset_code: str = "cyan_air_event",
    checkpoint_every: int = 3,
    use_cloud_vision: bool = True,
    dry_run: bool = False,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """执行一次完整的「原图 + XMP + 参考图 -> 方案 -> PS MCP（含检查点）-> 分层 PSD & 预览图」测试。"""
    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        if log_cb:
            log_cb(line)
        else:
            print(line, flush=True)

    src = Path(input_path)
    if not src.exists():
        raise FileNotFoundError(f"待修原图不存在: {src}")

    run_id = time.strftime("%Y%m%d_%H%M%S")
    out_dir = WORKSPACE_ROOT / "work" / "ps_test_runs" / f"{run_id}_{src.stem}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tiff_path = out_dir / f"{src.stem}_stage1_16bit.tiff"
    srgb_preview = out_dir / f"{src.stem}_stage1_srgb1024.jpg"
    out_psd = out_dir / f"{src.stem}_retouched.psd"
    out_jpg = out_dir / f"{src.stem}_retouched_preview.jpg"

    log(f"输出目录: {out_dir}")
    log(f"1/4 读取原图并套用 XMP ({xmp_path.name if xmp_path else '无自定义XMP，使用默认转换'}) -> 16-bit TIFF...")
    convert_raw_with_xmp_to_tiff(
        nas_raw_path=src,
        out_tiff_path=tiff_path,
        preset_code=preset_code,
        xmp_path=xmp_path,
        prefer_native_acr=not dry_run,
    )

    # 生成 1024px sRGB 给视觉模型看图
    import cv2
    import numpy as np
    from PIL import Image

    buf = np.fromfile(str(tiff_path), dtype=np.uint8)
    arr = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    if arr.dtype == np.uint16:
        arr = np.clip(np.round(arr.astype(np.float32) / 257.0), 0, 255).astype(np.uint8)
    rgb = cv2.cvtColor(arr[:, :, :3], cv2.COLOR_BGR2RGB)
    im = Image.fromarray(rgb)
    im.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    im.save(srgb_preview, format="JPEG", quality=92)
    log(f"    已生成 16-bit TIFF 与 1024px 预览: {srgb_preview.name}")

    # 2/4 生成仿色方案（优先云端视觉模型，无 Key 或失败时自动降级本地直方图仿色）
    plan: Dict[str, Any]
    plan_source = "local_histogram_fallback"
    colorist = GeminiColorist()
    if use_cloud_vision and not dry_run and colorist.api_key:
        log(f"2/4 调用视觉大脑 ({colorist.model}) + 热加载 Skill 分析原图与参考图...")
        try:
            plan = colorist.generate_plan(
                srgb_preview_path=srgb_preview,
                preset_code=preset_code,
                preset_notes=notes,
                reference_image_path=ref_path if (ref_path and ref_path.exists()) else None,
            )
            plan_source = f"vision:{colorist.model}"
        except Exception as exc:  # noqa: BLE001
            log(f"    云端视觉调用失败 ({exc})，自动降级为本地参考图仿色方案")
            plan = _fallback_histogram_plan(srgb_preview, ref_path, notes)
    else:
        log("2/4 使用本地参考图直方图安全仿色方案（未启用云端 Key 或演练模式）...")
        plan = _fallback_histogram_plan(srgb_preview, ref_path, notes)

    (out_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"    方案摘要: {plan.get('summary')} (来源: {plan_source})")

    # 3/4 驱动 Photoshop MCP 执行（含每 N 步检查点保存）
    log(f"3/4 正在调用 Photoshop MCP (backend={backend}, dry_run={dry_run}, checkpoint_every={checkpoint_every})...")
    toolkit = PSMCPToolkit(dry_run=dry_run)
    planner = Local8BMCPPlanner(
        toolkit=toolkit,
        ps_mcp_backend="com" if dry_run else backend,
    )
    if backend == "adb_mcp" and not dry_run:
        res = planner.execute_retouch_session(
            source_path=tiff_path,
            high_level_plan=plan,
            out_psd_path=out_psd,
            out_preview_jpg=out_jpg,
            brief_text=notes,
        )
    else:
        res = planner.execute_with_checkpoints_and_backtrack(
            source_path=tiff_path,
            high_level_plan=plan,
            out_psd_path=out_psd,
            out_preview_jpg=out_jpg,
            brief_text=notes,
            checkpoint_every=checkpoint_every,
        )

    log(f"4/4 执行完成！共执行 {res.get('steps_count', 0)} 步，检查点 {len(res.get('checkpoints') or [])} 个。")
    log(f"    分层 PSD: {out_psd}")
    log(f"    成品预览: {out_jpg}")
    return {
        "status": "ok",
        "out_dir": str(out_dir),
        "plan_source": plan_source,
        "plan": plan,
        "mcp_result": res,
    }


def launch_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    root = tk.Tk()
    root.title("ZERO STUDIO · Photoshop MCP 实机修图测试实验室")
    root.geometry("760x600")
    root.configure(bg="#111720")

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass

    fg = "#edf3f6"
    muted = "#94a3b8"
    bg = "#111720"
    card = "#1a2230"

    v_input = tk.StringVar()
    v_ref = tk.StringVar()
    v_xmp = tk.StringVar()
    v_notes = tk.StringVar(value="苍蓝通透，肤色清透自然不发黄")
    v_backend = tk.StringVar(value="com")
    v_ckpt = tk.IntVar(value=3)
    v_cloud = tk.BooleanVar(value=True)
    v_dry = tk.BooleanVar(value=False)

    def pick_file(var: tk.StringVar, title: str, filetypes):
        p = filedialog.askopenfilename(title=title, filetypes=filetypes)
        if p:
            var.set(p)

    frame = tk.Frame(root, bg=bg, padx=16, pady=12)
    frame.pack(fill="both", expand=True)

    tk.Label(frame, text="ZERO STUDIO · Photoshop MCP 实机修图测试实验室", font=("Microsoft YaHei", 13, "bold"), fg="#79b8b2", bg=bg).pack(anchor="w")
    tk.Label(frame, text="上传原图 + 可选参考图 + 可选 .xmp 预设，一键驱动本机 Photoshop 输出 16-bit 分层 PSD 与检查点", font=("Microsoft YaHei", 9), fg=muted, bg=bg).pack(anchor="w", pady=(2, 10))

    def add_picker(label: str, var: tk.StringVar, title: str, ftypes):
        row = tk.Frame(frame, bg=bg)
        row.pack(fill="x", pady=4)
        tk.Label(row, text=label, width=14, anchor="w", fg=fg, bg=bg, font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Entry(row, textvariable=var, bg=card, fg=fg, insertbackground=fg, relief="flat").pack(side="left", fill="x", expand=True, padx=6, ipady=4)
        tk.Button(row, text="选择文件", command=lambda: pick_file(var, title, ftypes), bg="#2b3648", fg=fg, relief="flat", padx=10).pack(side="right")

    add_picker("待修原图 *：", v_input, "选择待修原图", [("图像/RAW", "*.arw *.cr3 *.jpg *.jpeg *.png *.tif *.tiff"), ("所有文件", "*.*")])
    add_picker("仿色参考图：", v_ref, "选择参考图（可选）", [("图像", "*.jpg *.jpeg *.png *.webp"), ("所有文件", "*.*")])
    add_picker("XMP 预设：", v_xmp, "选择 .xmp 预设（可选）", [("Camera Raw XMP", "*.xmp"), ("所有文件", "*.*")])

    row_notes = tk.Frame(frame, bg=bg)
    row_notes.pack(fill="x", pady=4)
    tk.Label(row_notes, text="修图要求：", width=14, anchor="w", fg=fg, bg=bg, font=("Microsoft YaHei", 9)).pack(side="left")
    tk.Entry(row_notes, textvariable=v_notes, bg=card, fg=fg, insertbackground=fg, relief="flat").pack(side="left", fill="x", expand=True, padx=6, ipady=4)

    opt_row = tk.Frame(frame, bg=bg)
    opt_row.pack(fill="x", pady=8)
    tk.Label(opt_row, text="PS 驱动：", fg=fg, bg=bg, font=("Microsoft YaHei", 9)).pack(side="left")
    for text, val in (("PS COM 零干预 (全26工具)", "com"), ("adb-mcp UXP 插件 (3001)", "adb_mcp")):
        tk.Radiobutton(opt_row, text=text, variable=v_backend, value=val, bg=bg, fg=fg, selectcolor=card, activebackground=bg, activeforeground=fg).pack(side="left", padx=6)
    tk.Checkbutton(opt_row, text="调用云端视觉大脑", variable=v_cloud, bg=bg, fg=fg, selectcolor=card, activebackground=bg).pack(side="left", padx=8)
    tk.Checkbutton(opt_row, text="仅演练(不连PS)", variable=v_dry, bg=bg, fg="#efc278", selectcolor=card, activebackground=bg).pack(side="left", padx=6)

    log_box = scrolledtext.ScrolledText(frame, height=14, bg="#0b0f14", fg="#cbd5e1", font=("Consolas", 9), relief="flat")
    log_box.pack(fill="both", expand=True, pady=(6, 10))

    last_out_dir = {"path": ""}

    def append_log(msg: str):
        root.after(0, lambda: (log_box.insert("end", msg + "\n"), log_box.see("end")))

    btn_row = tk.Frame(frame, bg=bg)
    btn_row.pack(fill="x")

    run_btn = tk.Button(btn_row, text="▶ 开始 PS 实机试修", bg="#79b8b2", fg="#071311", font=("Microsoft YaHei", 10, "bold"), relief="flat", padx=18, pady=6)
    run_btn.pack(side="left")

    def open_out():
        if last_out_dir["path"] and Path(last_out_dir["path"]).exists():
            os.startfile(last_out_dir["path"])
        else:
            messagebox.showinfo("提示", "还没有生成输出目录，请先运行一次测试。")

    tk.Button(btn_row, text="📂 打开输出目录", command=open_out, bg="#2b3648", fg=fg, font=("Microsoft YaHei", 9), relief="flat", padx=14, pady=6).pack(side="left", padx=10)

    def start_run():
        if not v_input.get().strip():
            messagebox.showwarning("缺少原图", "请先选择一张待修原图（RAW / JPG / PNG / TIFF）。")
            return
        run_btn.config(state="disabled", text="正在执行中...")
        log_box.delete("1.0", "end")

        def worker():
            try:
                res = run_ps_test(
                    input_path=Path(v_input.get().strip()),
                    ref_path=Path(v_ref.get().strip()) if v_ref.get().strip() else None,
                    xmp_path=Path(v_xmp.get().strip()) if v_xmp.get().strip() else None,
                    backend=v_backend.get(),
                    notes=v_notes.get().strip(),
                    checkpoint_every=max(1, int(v_ckpt.get())),
                    use_cloud_vision=bool(v_cloud.get()),
                    dry_run=bool(v_dry.get()),
                    log_cb=append_log,
                )
                last_out_dir["path"] = res["out_dir"]
                append_log("=== ✅ 测试完成！点击「打开输出目录」查看分层 PSD 与检查点 ===")
            except Exception as exc:  # noqa: BLE001
                append_log(f"=== ❌ 执行失败: {exc} ===")
            finally:
                root.after(0, lambda: run_btn.config(state="normal", text="▶ 开始 PS 实机试修"))

        threading.Thread(target=worker, daemon=True).start()

    run_btn.config(command=start_run)
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="ZERO STUDIO Photoshop MCP 实机修图测试实验室")
    parser.add_argument("--input", help="待修原图路径 (.ARW/.CR3/.JPG/.PNG/.TIFF)")
    parser.add_argument("--ref", help="仿色参考图路径（可选）")
    parser.add_argument("--xmp", help="Camera Raw .xmp 预设文件路径（可选）")
    parser.add_argument("--backend", choices=("com", "adb_mcp"), default="com")
    parser.add_argument("--notes", default="", help="附加修图风格要求")
    parser.add_argument("--checkpoint-every", type=int, default=3)
    parser.add_argument("--no-cloud", action="store_true", help="跳过云端视觉模型，仅本地直方图仿色")
    parser.add_argument("--dry-run", action="store_true", help="离线演练（不连接真实 Photoshop）")
    args = parser.parse_args()

    if not args.input:
        launch_gui()
        return

    run_ps_test(
        input_path=Path(args.input),
        ref_path=Path(args.ref) if args.ref else None,
        xmp_path=Path(args.xmp) if args.xmp else None,
        backend=args.backend,
        notes=args.notes,
        checkpoint_every=args.checkpoint_every,
        use_cloud_vision=not args.no_cloud,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
