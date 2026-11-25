#!/usr/bin/env python3
"""
ZERO STUDIO · Photoshop AI 修图与 Skill 测试实验室 (test_ps_interactive.py)
==========================================================================
支持图形窗口（双击 `测试_PS修图实验室.bat`）与命令行双模式：
1. 自动扫描 `configs/retouch_skills/*.md`，所有修图 Skill 均可自由勾选组合；
2. 支持上传待修原图（.ARW/.CR3/.JPG/.PNG/.TIFF）+ 仿色参考图 + .xmp 预设；
3. 支持完整 AI 端到端闭环：
   原生 ACR 16-bit 解码 -> AI 读所选 Skill 出分区蒙版方案 -> PS MCP 执行并每 N 步存检查点 ->
   AI 终盘整体视觉检验打分 -> 若不达标自动逆推回退检查点 PSD 定向修改并复审！
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ENGINE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = ENGINE_DIR.parents[1]
SKILLS_DIR = WORKSPACE_ROOT / "configs" / "retouch_skills"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(WORKSPACE_ROOT / ".env", override=False)

from gemini_colorist import GeminiColorist, clamp_retouch_plan  # noqa: E402
from mcp_planner import Local8BMCPPlanner, compile_bright_low_contrast_stack  # noqa: E402
from ps_mcp_server import PSMCPToolkit  # noqa: E402
from xmp_raw_stage import convert_raw_with_xmp_to_tiff  # noqa: E402


def list_available_skills() -> List[str]:
    if not SKILLS_DIR.exists():
        return []
    return sorted([p.name for p in SKILLS_DIR.glob("*.md")], key=lambda s: s.lower())


def _fallback_masked_plan(src_preview: Path, ref_path: Optional[Path], notes: str) -> Dict[str, Any]:
    import numpy as np
    from PIL import Image

    with Image.open(src_preview) as im:
        src_arr = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0

    exp_delta, temp_delta = 0.12, -2.5
    if ref_path and ref_path.exists():
        with Image.open(ref_path) as rim:
            ref_arr = np.asarray(rim.convert("RGB"), dtype=np.float32) / 255.0
        exp_delta = float(np.clip((ref_arr.mean() - src_arr.mean()) * 1.4, -0.4, 0.4))
        src_rb = float(src_arr[:, :, 0].mean() - src_arr[:, :, 2].mean())
        ref_rb = float(ref_arr[:, :, 0].mean() - ref_arr[:, :, 2].mean())
        temp_delta = float(np.clip((ref_rb - src_rb) * 35.0, -8.0, 8.0))

    return clamp_retouch_plan({
        "exposure_delta": round(exp_delta, 3),
        "temp_delta": round(temp_delta, 3),
        "tint_delta": 0.5,
        "hsl_adjustments": {
            "oranges": {"hue": 1, "saturation": -4, "luminance": 6},
            "yellows": {"hue": -4, "saturation": -10, "luminance": 2},
            "blues": {"hue": -2, "saturation": 8, "luminance": 2},
        },
        "skin_luminance": 4.5,
        "curves_points": [[0, 2], [64, 60], [128, 134], [192, 198], [255, 252]],
        "selective_color": [{"color_range": "neutrals", "cyan": 3, "yellow": -2}],
        "subject_mask_adjust": {"enabled": True, "exposure_delta": 0.40, "temp_delta": 1.5, "feather_px": 10},
        "background_mask_adjust": {"enabled": True, "exposure_delta": -0.50, "temp_delta": -5.0, "saturation_delta": -18, "feather_px": 14},
        "skin_protect_mask": {"enabled": True, "fuzziness": 38, "exposure_delta": 0.25, "temp_delta": 3.0, "feather_px": 6},
        "finishing": {"high_pass_sharpen": True, "high_pass_radius": 2.0, "high_pass_opacity": 35, "neutral_gray_layer": True, "frequency_separation": False},
        "summary": f"本地分区蒙版兜底方案（{notes or '主体提亮+背景压暗+肤色保护+锐化'}）",
    })


def run_ps_test(
    input_path: Path,
    ref_path: Optional[Path] = None,
    xmp_path: Optional[Path] = None,
    backend: str = "com",
    mode: str = "ai_auto",
    selected_skills: Optional[List[str]] = None,
    notes: str = "",
    checkpoint_every: int = 8,
    enable_ai_final_review: bool = True,
    use_cloud_vision: bool = True,
    dry_run: bool = False,
    log_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """执行完整 AI 端到端修图测试（支持自定义勾选 Skill + 检查点 + AI 终盘检验与逆推回退）。"""
    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        if log_cb:
            log_cb(line)
        else:
            print(line, flush=True)

    src = Path(input_path)
    if not src.exists():
        raise FileNotFoundError(f"待修原图不存在: {src}")

    skills_to_use = selected_skills if selected_skills is not None else list_available_skills()
    run_id = time.strftime("%Y%m%d_%H%M%S")
    out_dir = WORKSPACE_ROOT / "work" / "ps_test_runs" / f"{run_id}_{src.stem}"
    out_dir.mkdir(parents=True, exist_ok=True)
    tiff_path = out_dir / f"{src.stem}_stage1_16bit.tiff"
    srgb_preview = out_dir / f"{src.stem}_stage1_srgb1024.jpg"
    out_psd = out_dir / f"{src.stem}_retouched.psd"
    out_jpg = out_dir / f"{src.stem}_retouched_preview.jpg"

    log(f"输出目录: {out_dir}")
    log(f"已勾选 Skill ({len(skills_to_use)}): {', '.join(skills_to_use) if skills_to_use else '（无）'}")
    log(f"1/5 读取原图并套用 XMP ({xmp_path.name if xmp_path else '无自定义XMP'}) -> 16-bit TIFF...")
    tiff_info = convert_raw_with_xmp_to_tiff(
        nas_raw_path=src,
        out_tiff_path=tiff_path,
        preset_code="bright_low_contrast" if mode == "bright_low_contrast" else "cyan_air_event",
        xmp_path=xmp_path,
        prefer_native_acr=not dry_run,
    )
    log(f"    解码后端: {tiff_info.get('backend')} -> {tiff_path.name}")

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

    # 2/5 调用视觉大脑（仅注入勾选的 Skill）生成分区蒙版方案
    colorist = GeminiColorist()
    plan_source = "local_fallback"
    if use_cloud_vision and not dry_run and colorist.api_key:
        log(f"2/5 调用 AI 视觉大脑 ({colorist.model}) + 勾选 Skill 生成分区蒙版方案...")
        try:
            plan = colorist.generate_plan(
                srgb_preview_path=srgb_preview,
                preset_code="bright_low_contrast" if mode == "bright_low_contrast" else "cyan_air_event",
                preset_notes=notes,
                reference_image_path=ref_path if (ref_path and ref_path.exists()) else None,
                selected_skills=skills_to_use,
            )
            plan_source = f"ai:{colorist.model}"
        except Exception as exc:  # noqa: BLE001
            log(f"    AI 规划异常 ({exc})，降级为本地分区蒙版方案")
            plan = _fallback_masked_plan(srgb_preview, ref_path, notes)
    else:
        log("2/5 使用本地分区蒙版兜底方案（未启用云端 Key 或演练模式）...")
        plan = _fallback_masked_plan(srgb_preview, ref_path, notes)

    (out_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"    方案摘要: {plan.get('summary')} (来源: {plan_source})")
    if plan.get("distraction_boxes"):
        log(f"    检测到边缘穿帮 ({len(plan['distraction_boxes'])} 处): " + ", ".join(d["label"] for d in plan["distraction_boxes"]))

    # 3/5 准备 AI 终盘整体视觉检验回调（保存完最终图后才检验，不达标则自动逆推回退）
    def ai_final_reviewer(ctx: Dict[str, Any]) -> Dict[str, Any]:
        log(f"4/5 触发 AI 终盘整体视觉检验（第 {ctx.get('round', 1)} 轮，共 {len(ctx.get('checkpoints') or [])} 个检查点）...")
        rev = colorist.review_final_retouch(
            original_preview_path=srgb_preview,
            final_preview_path=Path(ctx["final_preview_path"]),
            checkpoints=ctx.get("checkpoints") or [],
            selected_skills=skills_to_use,
            reference_image_path=ref_path if (ref_path and ref_path.exists()) else None,
            preset_notes=notes,
        )
        log(f"    AI 检验打分: {rev.get('score')} 分 | passed={rev.get('passed')} | 评语: {rev.get('summary')}")
        if not rev.get("passed"):
            log(f"    -> 逆推原因: {rev.get('root_cause')} | 回退检查点: {rev.get('rollback_to_checkpoint')}")
        return rev

    review_cb = ai_final_reviewer if (enable_ai_final_review and use_cloud_vision and not dry_run and colorist.api_key) else None

    # 4/5 驱动 Photoshop MCP 执行（含检查点存档 + 可选终盘逆推回退）
    log(f"3/5 启动 Photoshop MCP 执行 (mode={mode}, backend={backend}, checkpoint_every={checkpoint_every})...")
    toolkit = PSMCPToolkit(dry_run=dry_run)
    if mode == "bright_low_contrast":
        planner = Local8BMCPPlanner(
            toolkit=toolkit,
            ps_mcp_backend="com",
            mock_llm_fn=lambda ctx: compile_bright_low_contrast_stack(layer_prefix="BLC"),
        )
    else:
        planner = Local8BMCPPlanner(
            toolkit=toolkit,
            ps_mcp_backend="com" if dry_run else backend,
        )

    res = planner.execute_with_checkpoints_and_backtrack(
        source_path=tiff_path,
        high_level_plan=plan,
        out_psd_path=out_psd,
        out_preview_jpg=out_jpg,
        brief_text=notes,
        checkpoint_every=checkpoint_every,
        max_backtrack_rounds=1,
        review_fn=review_cb,
    )

    report = {
        "status": "ok",
        "out_dir": str(out_dir),
        "selected_skills": skills_to_use,
        "plan_source": plan_source,
        "plan": plan,
        "final_review": res.get("final_review"),
        "backtrack_history": res.get("backtrack_history"),
        "steps_count": res.get("steps_count"),
        "checkpoints_count": len(res.get("checkpoints") or []),
        "output_psd_path": str(out_psd),
        "output_preview_path": str(out_jpg),
    }
    (out_dir / "review_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"5/5 全部完成！执行 {report['steps_count']} 步，检查点 {report['checkpoints_count']} 个，回退修正 {len(report['backtrack_history'] or [])} 次。")
    log(f"    分层 PSD: {out_psd}")
    log(f"    成品预览: {out_jpg}")
    log(f"    检验报告: {out_dir / 'review_report.json'}")
    return report


def launch_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext

    root = tk.Tk()
    root.title("ZERO STUDIO · AI 修图与 Skill 选择测试实验室")
    root.geometry("820x700")
    root.configure(bg="#111720")

    fg, muted, bg, card = "#edf3f6", "#94a3b8", "#111720", "#1a2230"

    v_input = tk.StringVar()
    v_ref = tk.StringVar()
    v_xmp = tk.StringVar()
    v_notes = tk.StringVar(value="亮调低反差通透人像：主体提亮、背景压暗去杂色、肤色白里透粉")
    v_mode = tk.StringVar(value="ai_auto")
    v_backend = tk.StringVar(value="com")
    v_cloud = tk.BooleanVar(value=True)
    v_review = tk.BooleanVar(value=True)
    v_dry = tk.BooleanVar(value=False)

    frame = tk.Frame(root, bg=bg, padx=16, pady=12)
    frame.pack(fill="both", expand=True)

    tk.Label(frame, text="ZERO STUDIO · AI 修图与 Skill 测试实验室", font=("Microsoft YaHei", 13, "bold"), fg="#79b8b2", bg=bg).pack(anchor="w")
    tk.Label(frame, text="自由勾选 configs/retouch_skills/*.md 修图 Skill，支持原图/参考图/XMP 上传 + 检查点存档 + AI 终盘整体检验与逆推", font=("Microsoft YaHei", 9), fg=muted, bg=bg).pack(anchor="w", pady=(2, 8))

    def pick_file(var: tk.StringVar, title: str, ftypes):
        p = filedialog.askopenfilename(title=title, filetypes=ftypes)
        if p:
            var.set(p)

    def add_picker(label: str, var: tk.StringVar, title: str, ftypes):
        row = tk.Frame(frame, bg=bg)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, width=13, anchor="w", fg=fg, bg=bg, font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Entry(row, textvariable=var, bg=card, fg=fg, insertbackground=fg, relief="flat").pack(side="left", fill="x", expand=True, padx=6, ipady=4)
        tk.Button(row, text="选择文件", command=lambda: pick_file(var, title, ftypes), bg="#2b3648", fg=fg, relief="flat", padx=10).pack(side="right")

    add_picker("待修原图 *：", v_input, "选择待修原图", [("图像/RAW", "*.arw *.cr3 *.jpg *.jpeg *.png *.tif *.tiff"), ("所有文件", "*.*")])
    add_picker("仿色参考图：", v_ref, "选择参考图（可选）", [("图像", "*.jpg *.jpeg *.png *.webp"), ("所有文件", "*.*")])
    add_picker("XMP 预设：", v_xmp, "选择 .xmp 预设（可选）", [("Camera Raw XMP", "*.xmp"), ("所有文件", "*.*")])

    row_notes = tk.Frame(frame, bg=bg)
    row_notes.pack(fill="x", pady=3)
    tk.Label(row_notes, text="附加要求：", width=13, anchor="w", fg=fg, bg=bg, font=("Microsoft YaHei", 9)).pack(side="left")
    tk.Entry(row_notes, textvariable=v_notes, bg=card, fg=fg, insertbackground=fg, relief="flat").pack(side="left", fill="x", expand=True, padx=6, ipady=4)

    # 动态 Skill 多选框卡片
    skill_card = tk.LabelFrame(frame, text=" 选择要注入的修图 Skill（自动扫描 configs/retouch_skills/*.md） ", fg="#a0d8d2", bg=card, font=("Microsoft YaHei", 9, "bold"), padx=10, pady=6)
    skill_card.pack(fill="x", pady=8)

    skill_vars: Dict[str, tk.BooleanVar] = {}
    skill_grid = tk.Frame(skill_card, bg=card)
    skill_grid.pack(fill="x")

    def refresh_skills():
        for w in skill_grid.winfo_children():
            w.destroy()
        skill_vars.clear()
        names = list_available_skills()
        if not names:
            tk.Label(skill_grid, text="（未找到 .md Skill 文件）", fg=muted, bg=card).pack(anchor="w")
            return
        for idx, name in enumerate(names):
            var = tk.BooleanVar(value=True)
            skill_vars[name] = var
            cb = tk.Checkbutton(skill_grid, text=name, variable=var, bg=card, fg=fg, selectcolor=bg, activebackground=card, activeforeground=fg, font=("Consolas", 9))
            cb.grid(row=idx // 2, column=idx % 2, sticky="w", padx=(0, 18), pady=2)

    skill_btn_row = tk.Frame(skill_card, bg=card)
    skill_btn_row.pack(fill="x", pady=(6, 0))
    tk.Button(skill_btn_row, text="全选", command=lambda: [v.set(True) for v in skill_vars.values()], bg="#2b3648", fg=fg, relief="flat", padx=8, font=("Microsoft YaHei", 8)).pack(side="left")
    tk.Button(skill_btn_row, text="清空", command=lambda: [v.set(False) for v in skill_vars.values()], bg="#2b3648", fg=fg, relief="flat", padx=8, font=("Microsoft YaHei", 8)).pack(side="left", padx=6)
    tk.Button(skill_btn_row, text="仅选亮调低反差", command=lambda: [v.set("bright_low_contrast" in k) for k, v in skill_vars.items()], bg="#2b3648", fg="#79b8b2", relief="flat", padx=8, font=("Microsoft YaHei", 8)).pack(side="left")
    tk.Button(skill_btn_row, text="刷新 Skill", command=refresh_skills, bg="#2b3648", fg=fg, relief="flat", padx=8, font=("Microsoft YaHei", 8)).pack(side="right")
    refresh_skills()

    # 模式与开关
    opt1 = tk.Frame(frame, bg=bg)
    opt1.pack(fill="x", pady=4)
    tk.Label(opt1, text="执行模式：", fg=fg, bg=bg, font=("Microsoft YaHei", 9)).pack(side="left")
    for txt, val in (("AI 读所选 Skill 自主分区规划", "ai_auto"), ("亮调低反差标准 36 步分层栈", "bright_low_contrast")):
        tk.Radiobutton(opt1, text=txt, variable=v_mode, value=val, bg=bg, fg=fg, selectcolor=card, activebackground=bg, activeforeground=fg).pack(side="left", padx=6)

    opt2 = tk.Frame(frame, bg=bg)
    opt2.pack(fill="x", pady=2)
    tk.Checkbutton(opt2, text="启用云端 AI 视觉规划", variable=v_cloud, bg=bg, fg=fg, selectcolor=card, activebackground=bg).pack(side="left", padx=(0, 10))
    tk.Checkbutton(opt2, text="启用 AI 终盘检验 + 逆推回退", variable=v_review, bg=bg, fg="#72d5aa", selectcolor=card, activebackground=bg).pack(side="left", padx=6)
    tk.Checkbutton(opt2, text="仅演练(不连PS)", variable=v_dry, bg=bg, fg="#efc278", selectcolor=card, activebackground=bg).pack(side="left", padx=6)

    log_box = scrolledtext.ScrolledText(frame, height=13, bg="#0b0f14", fg="#cbd5e1", font=("Consolas", 9), relief="flat")
    log_box.pack(fill="both", expand=True, pady=(6, 10))

    last_out_dir = {"path": ""}

    def append_log(msg: str):
        root.after(0, lambda: (log_box.insert("end", msg + "\n"), log_box.see("end")))

    btn_row = tk.Frame(frame, bg=bg)
    btn_row.pack(fill="x")
    run_btn = tk.Button(btn_row, text="▶ 开始 AI 全链路实机修图与检验", bg="#79b8b2", fg="#071311", font=("Microsoft YaHei", 10, "bold"), relief="flat", padx=18, pady=6)
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
        picked_skills = [k for k, var in skill_vars.items() if var.get()]
        run_btn.config(state="disabled", text="AI 全链路执行中...")
        log_box.delete("1.0", "end")

        def worker():
            try:
                res = run_ps_test(
                    input_path=Path(v_input.get().strip()),
                    ref_path=Path(v_ref.get().strip()) if v_ref.get().strip() else None,
                    xmp_path=Path(v_xmp.get().strip()) if v_xmp.get().strip() else None,
                    backend=v_backend.get(),
                    mode=v_mode.get(),
                    selected_skills=picked_skills,
                    notes=v_notes.get().strip(),
                    enable_ai_final_review=bool(v_review.get()),
                    use_cloud_vision=bool(v_cloud.get()),
                    dry_run=bool(v_dry.get()),
                    log_cb=append_log,
                )
                last_out_dir["path"] = res["out_dir"]
                append_log("=== ✅ AI 全链路修图与终盘检验全部完成！点击「打开输出目录」查看 PSD 与检验报告 ===")
            except Exception as exc:  # noqa: BLE001
                append_log(f"=== ❌ 执行失败: {exc} ===")
            finally:
                root.after(0, lambda: run_btn.config(state="normal", text="▶ 开始 AI 全链路实机修图与检验"))

        threading.Thread(target=worker, daemon=True).start()

    run_btn.config(command=start_run)
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="ZERO STUDIO AI 修图与 Skill 测试实验室")
    parser.add_argument("--input", help="待修原图路径 (.ARW/.CR3/.JPG/.PNG/.TIFF)")
    parser.add_argument("--ref", help="仿色参考图路径（可选）")
    parser.add_argument("--xmp", help="Camera Raw .xmp 预设路径（可选）")
    parser.add_argument("--skills", default="", help="逗号分隔的 Skill 文件名（留空则加载全部）")
    parser.add_argument("--mode", choices=("ai_auto", "bright_low_contrast"), default="ai_auto")
    parser.add_argument("--backend", choices=("com", "adb_mcp"), default="com")
    parser.add_argument("--notes", default="", help="附加风格说明")
    parser.add_argument("--checkpoint-every", type=int, default=8)
    parser.add_argument("--no-review", action="store_true", help="跳过 AI 终盘整体视觉检验")
    parser.add_argument("--no-cloud", action="store_true", help="跳过云端视觉规划")
    parser.add_argument("--dry-run", action="store_true", help="离线演练（不连接真实 Photoshop）")
    args = parser.parse_args()

    if not args.input:
        launch_gui()
        return

    picked = [s.strip() for s in args.skills.split(",") if s.strip()] if args.skills.strip() else None
    run_ps_test(
        input_path=Path(args.input),
        ref_path=Path(args.ref) if args.ref else None,
        xmp_path=Path(args.xmp) if args.xmp else None,
        backend=args.backend,
        mode=args.mode,
        selected_skills=picked,
        notes=args.notes,
        checkpoint_every=args.checkpoint_every,
        enable_ai_final_review=not args.no_review,
        use_cloud_vision=not args.no_cloud,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
