#!/usr/bin/env python3
"""
综合单元测试：验证本次新增的 4 大核心能力：
1. Photoshop 标准 MCP Server (`ps_mcp_server.py`) 参数安全钳位、可选颜色与 FastMCP 实例化；
2. `mikechambers/adb-mcp` Socket.IO 客户端 (`adb_mcp_client.py`) 命令打包与边界钳位；
3. 云端方案 -> 本地 8B 模型拆解调用 PS MCP (`mcp_planner.py`) 与确定性降级回退；
4. 拍前 + 拍后双二维码闭合分组 (`cull_and_cluster.assign_qr_segments`)；
5. 拍前动作沟通清单与手机汇总渲染 (`pose_briefing.PoseBriefingStore`)。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE_ROOT / "pipelines" / "retouch-engine"))
sys.path.insert(0, str(WORKSPACE_ROOT / "pipelines" / "ingest-cull-cluster"))
sys.path.insert(0, str(WORKSPACE_ROOT / "services" / "im-gateway"))

from adb_mcp_client import AdbMCPPhotoshopClient  # noqa: E402
from cull_and_cluster import assign_qr_segments  # noqa: E402
from mcp_planner import Local8BMCPPlanner, compile_plan_to_mcp_calls  # noqa: E402
from pose_briefing import PoseBriefingStore  # noqa: E402
from ps_mcp_server import PSMCPToolkit, create_fastmcp_server  # noqa: E402


class TestPSMCPAndDualModel(unittest.TestCase):
    def test_ps_mcp_toolkit_clamping_and_selective_color(self):
        kit = PSMCPToolkit(dry_run=True)
        res_exp = kit.ps_add_exposure_layer(9.9)
        self.assertEqual(res_exp["applied_exposure_delta"], 1.5)

        res_cb = kit.ps_add_color_balance_layer(temp_delta=-99, tint_delta=50)
        self.assertEqual(res_cb["applied_temp_delta"], -15.0)
        self.assertEqual(res_cb["applied_tint_delta"], 15.0)

        res_hsl = kit.ps_add_hsl_layer({"oranges": {"hue": 50, "saturation": -80, "luminance": 40}}, skin_luminance=99)
        self.assertIn("hsl.oranges.hue", res_hsl["clamped_fields"])
        self.assertIn("skin_luminance", res_hsl["clamped_fields"])

        res_sc = kit.ps_add_selective_color_layer(color_range="neutrals", cyan=80, black=-60)
        self.assertEqual(res_sc["status"], "ok")
        self.assertEqual(kit.call_history[-1]["args"]["cyan"], 30.0)
        self.assertEqual(kit.call_history[-1]["args"]["black"], -30.0)

        # 验证标准 FastMCP Server 可正常构建
        server = create_fastmcp_server(dry_run=True)
        self.assertIsNotNone(server)
        self.assertGreaterEqual(len(PSMCPToolkit.get_openai_tool_schemas()), 20)

    def test_advanced_retouch_tools_universal_engines_and_visual_check(self):
        kit = PSMCPToolkit(dry_run=True)
        # 1) 高频精修原子工具全覆盖
        self.assertEqual(kit.ps_create_neutral_gray_layer()["status"], "ok")
        self.assertEqual(kit.ps_create_frequency_separation(gaussian_radius=8.0)["status"], "ok")
        self.assertEqual(kit.ps_add_levels_layer(black_point=8, gamma=1.08, white_point=248)["status"], "ok")
        self.assertEqual(kit.ps_select_color_range(preset="skinTones", fuzziness=35)["status"], "ok")
        self.assertEqual(kit.ps_modify_selection(feather_px=6.0, contract_px=2, invert=False)["status"], "ok")
        self.assertEqual(kit.ps_add_layer_mask_from_selection(invert=False)["status"], "ok")
        self.assertEqual(kit.ps_set_layer_blend_opacity(blend_mode="softlight", opacity=75.0)["status"], "ok")
        self.assertEqual(kit.ps_convert_to_smart_object()["status"], "ok")
        self.assertEqual(kit.ps_apply_high_pass_sharpen(radius=1.8, opacity=55.0)["status"], "ok")
        self.assertEqual(kit.ps_select_subject()["status"], "ok")
        kit.ps_apply_liquify(face_slim=120, eye_size=-99, jawline=80)
        self.assertEqual(kit.call_history[-1]["args"]["face_slim"], 40)
        self.assertEqual(kit.call_history[-1]["args"]["eye_size"], -30)

        # 2) 万能引擎 ①：ExtendScript 安全审查（合法通过，危险调用拦截）
        ok_jsx = kit.ps_execute_extendscript("var n = app.activeDocument.name; return n;")
        self.assertEqual(ok_jsx["status"], "ok")
        with self.assertRaises(ValueError):
            kit.ps_execute_extendscript("app.system('calc.exe'); app.activeDocument.close();")

        # 3) 万能引擎 ②：batchPlay 描述符校验与执行
        bp_res = kit.ps_execute_batchplay([{"_obj": "select", "_target": [{"_ref": "layer", "_enum": "ordinal", "_value": "targetEnum"}]}])
        self.assertEqual(bp_res["status"], "ok")
        with self.assertRaises(ValueError):
            kit.ps_execute_batchplay([{"missing_obj": True}])

        # 4) 视觉自检闭环
        prev = kit.ps_get_canvas_preview(max_edge=768, return_base64=False)
        self.assertEqual(prev["status"], "ok")
        self.assertIn("mean_luma", prev["metrics"])

    def test_checkpoint_save_and_ai_backtrack_rollback(self):
        kit = PSMCPToolkit(dry_run=True)
        planner = Local8BMCPPlanner(
            toolkit=kit,
            mock_llm_fn=lambda ctx: [
                {"name": "ps_add_exposure_layer", "arguments": {"exposure_delta": 0.2}},
                {"name": "ps_add_color_balance_layer", "arguments": {"temp_delta": -3.0, "tint_delta": 0.0}},
                {"name": "ps_add_curves_layer", "arguments": {"curves_points": [[0, 0], [128, 95], [255, 255]]}},
                {"name": "ps_apply_high_pass_sharpen", "arguments": {"radius": 2.0, "opacity": 60.0}},
            ],
        )

        review_calls = []

        def mock_review(ctx):
            review_calls.append(ctx["round"])
            if ctx["round"] == 1:
                # 首次终盘审查发现第 3 步曲线压得过暗 -> 逆推回退到 ckpt_01（第 2 步后的干净 PSD）并修正
                return {
                    "passed": False,
                    "root_cause": "第 3 步曲线中间调压暗过度，导致肤色偏灰",
                    "rollback_to_checkpoint": "ckpt_01",
                    "fix_calls": [
                        {"name": "ps_add_curves_layer", "arguments": {"curves_points": [[0, 6], [128, 132], [255, 255]]}},
                        {"name": "ps_apply_high_pass_sharpen", "arguments": {"radius": 1.6, "opacity": 50.0}},
                    ],
                }
            return {"passed": True, "reason": "回退修正后肤色通透、高光自然，审查通过"}

        res = planner.execute_with_checkpoints_and_backtrack(
            source_path=Path("E:/work/in.tiff"),
            high_level_plan={"exposure_delta": 0.2},
            out_psd_path=Path("E:/work/final.psd"),
            out_preview_jpg=Path("E:/work/final.jpg"),
            checkpoint_every=2,
            max_backtrack_rounds=2,
            review_fn=mock_review,
        )

        # 验证每 2 步保存 1 次检查点（共 4 步 -> ckpt_01, ckpt_02）
        self.assertEqual([c["id"] for c in res["checkpoints"]], ["ckpt_01", "ckpt_02"])
        # 验证终盘审查共触发 2 轮（第 1 轮发现问题逆推回退，第 2 轮复审通过）
        self.assertEqual(review_calls, [1, 2])
        self.assertEqual(len(res["backtrack_history"]), 1)
        self.assertEqual(res["backtrack_history"][0]["rollback_to_checkpoint"], "ckpt_01")
        self.assertTrue(res["final_review"]["passed"])
        # 验证回退时确实重新打开了 ckpt_01_step2.psd
        opened_files = [h["args"]["file_path"] for h in kit.call_history if h["tool"] == "ps_open_document"]
        self.assertEqual(len(opened_files), 2)
        self.assertTrue(opened_files[1].endswith("ckpt_01_step2.psd"))

    def test_adb_mcp_client_protocol_and_clamping(self):
        received = []

        def mock_send(cmd):
            received.append(cmd)
            return {"status": "SUCCESS", "echo": cmd["action"]}

        client = AdbMCPPhotoshopClient(mock_send_fn=mock_send)
        self.assertTrue(client.is_proxy_online())
        client.open_photoshop_file("E:/raw/demo.tiff")
        client.add_brightness_contrast(layer_id=2, brightness=120, contrast=-90)
        client.add_color_balance(layer_id=2, midtones=[-80, 5, 99])
        client.save_document_as("E:/out/demo.psd", file_type="PSD")

        self.assertEqual([c["action"] for c in received], [
            "openPhotoshopFile",
            "addBrightnessContrastAdjustmentLayer",
            "addColorBalanceAdjustmentLayer",
            "saveDocumentAs",
        ])
        self.assertEqual(received[1]["options"]["brightness"], 40)
        self.assertEqual(received[1]["options"]["contrast"], -35)
        self.assertEqual(received[2]["options"]["midtones"], [-25, 5, 25])

    def test_local_8b_planner_and_deterministic_fallback(self):
        sample_plan = {
            "exposure_delta": 0.25,
            "temp_delta": -4.0,
            "tint_delta": 1.0,
            "hsl_adjustments": {"oranges": {"hue": 2, "saturation": -5, "luminance": 6}},
            "skin_luminance": 4.0,
            "curves_points": [[0, 8], [64, 60], [128, 132], [192, 198], [255, 250]],
            "selective_color": [{"color_range": "neutrals", "cyan": 4, "yellow": -3}],
        }

        # 1) 模拟本地 8B 模型返回 Tool Calls（同时过滤掉非法工具名）
        kit_8b = PSMCPToolkit(dry_run=True)
        planner_8b = Local8BMCPPlanner(
            toolkit=kit_8b,
            mock_llm_fn=lambda ctx: [
                {"name": "ps_add_exposure_layer", "arguments": {"exposure_delta": 0.25}},
                {"name": "ps_add_selective_color_layer", "arguments": {"color_range": "neutrals", "cyan": 4}},
                {"name": "evil_shell_tool", "arguments": {"cmd": "rm -rf"}},
            ],
        )
        res = planner_8b.execute_retouch_session(
            source_path=Path("E:/work/in.tiff"),
            high_level_plan=sample_plan,
            out_psd_path=Path("E:/work/out.psd"),
            out_preview_jpg=Path("E:/work/out.jpg"),
        )
        self.assertEqual(res["planner_mode"], "mock_8b")
        self.assertEqual(res["steps_count"], 2)
        called_names = [h["tool"] for h in kit_8b.call_history]
        self.assertEqual(called_names, [
            "ps_open_document",
            "ps_add_exposure_layer",
            "ps_add_selective_color_layer",
            "ps_save_psd",
            "ps_export_preview",
            "ps_close_document",
        ])

        # 2) 模拟本地 8B -> mikechambers/adb-mcp 完整路由
        adb_cmds = []
        mock_adb = AdbMCPPhotoshopClient(mock_send_fn=lambda cmd: (adb_cmds.append(cmd) or {"status": "SUCCESS", "layers": [{"id": 3}]}))
        planner_adb = Local8BMCPPlanner(
            adb_client=mock_adb,
            ps_mcp_backend="adb_mcp",
            mock_llm_fn=lambda ctx: [
                {"name": "ps_add_exposure_layer", "arguments": {"exposure_delta": 0.25}},
                {"name": "ps_add_color_balance_layer", "arguments": {"temp_delta": -4.0, "tint_delta": 1.0}},
            ],
        )
        res_adb = planner_adb.execute_retouch_session(
            source_path=Path("E:/work/in.tiff"),
            high_level_plan=sample_plan,
            out_psd_path=Path("E:/work/out.psd"),
            out_preview_jpg=None,
        )
        self.assertEqual(res_adb["backend"], "adb_mcp")
        self.assertEqual([c["action"] for c in adb_cmds], [
            "openPhotoshopFile",
            "getLayers",
            "addBrightnessContrastAdjustmentLayer",
            "addColorBalanceAdjustmentLayer",
            "saveDocumentAs",
        ])

        # 3) 本地 8B 关闭时自动降级为确定性 MCP 编译
        calls = compile_plan_to_mcp_calls(sample_plan)
        self.assertEqual([c["name"] for c in calls], [
            "ps_add_exposure_layer",
            "ps_add_color_balance_layer",
            "ps_add_curves_layer",
            "ps_add_hsl_layer",
            "ps_add_selective_color_layer",
        ])


class TestDualQRGrouping(unittest.TestCase):
    def test_implicit_start_and_end_qr_closes_group_cleanly(self):
        # 场景：订单 101 拍前连拍两张开始码（防误关）-> 2 张正片 -> 拍后扫同一订单二维码闭合 -> 1 张中场试光片（不应归入 101）-> 订单 102 显式 START/END
        assets = [
            {"filename": "01_qr101_a.jpg", "_ts": 1000, "_qr_payload": {"booking_id": 101}, "booking_ids": []},
            {"filename": "02_qr101_b.jpg", "_ts": 1001, "_qr_payload": {"booking_id": 101}, "booking_ids": []},
            {"filename": "03_p1.jpg", "_ts": 1010, "_qr_payload": None, "booking_ids": []},
            {"filename": "04_p2.jpg", "_ts": 1020, "_qr_payload": None, "booking_ids": []},
            {"filename": "05_qr101_end.jpg", "_ts": 1030, "_qr_payload": {"booking_id": 101}, "booking_ids": []},
            {"filename": "06_test_light.jpg", "_ts": 1040, "_qr_payload": None, "booking_ids": []},
            {"filename": "07_qr102_start.jpg", "_ts": 1050, "_qr_payload": {"booking_id": 102, "slate_role": "start"}, "booking_ids": []},
            {"filename": "08_p3.jpg", "_ts": 1060, "_qr_payload": None, "booking_ids": []},
            {"filename": "09_qr102_end.jpg", "_ts": 1070, "_qr_payload": {"booking_id": 102, "slate_role": "end"}, "booking_ids": []},
            {"filename": "10_after_all.jpg", "_ts": 1080, "_qr_payload": None, "booking_ids": []},
        ]
        stats = assign_qr_segments(assets)
        self.assertEqual(stats["opened_groups"], 2)
        self.assertEqual(stats["closed_groups"], 2)

        self.assertEqual(assets[2]["booking_ids"], [101])
        self.assertEqual(assets[3]["booking_ids"], [101])
        self.assertEqual(assets[4]["reject_reason"], "SLATE_QR_END")
        # 拍后闭合之后的试光片不会被误绑到 101
        self.assertEqual(assets[5]["booking_ids"], [])
        self.assertEqual(assets[6]["reject_reason"], "SLATE_QR_START")
        self.assertEqual(assets[7]["booking_ids"], [102])
        self.assertEqual(assets[8]["reject_reason"], "SLATE_QR_END")
        self.assertEqual(assets[9]["booking_ids"], [])


class TestPoseBriefingStore(unittest.TestCase):
    def test_upsert_merge_and_mobile_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PoseBriefingStore(Path(tmp))
            store.upsert_sheet(
                booking_id=7,
                cn="小桃",
                character="碧蓝档案·爱丽丝",
                event_name="BACC",
                date="2026-11-27",
                time_slot="14:00-15:00",
                poses=["回眸扶发", "双手捧光环道具"],
                props=["光环道具"],
                lighting_mood="苍蓝通透逆光",
                avoid=["不拍俯拍"],
            )
            # 第二轮追加动作，不应覆盖第一轮记录
            merged = store.upsert_sheet(
                booking_id=7,
                poses=["低机位仰拍半身", "回眸扶发"],
                reference_images=["ref1.jpg"],
            )
            self.assertEqual(merged["poses"], ["回眸扶发", "双手捧光环道具", "低机位仰拍半身"])
            self.assertEqual(merged["reference_images"], ["ref1.jpg"])

            out = store.push_summary_to_mobile(event_name="BACC", date="2026-11-27", pushplus_token="", openclaw_webhook_url="")
            self.assertEqual(out["status"], "ok")
            self.assertTrue(Path(out["summary_file"]).exists())
            self.assertIn("碧蓝档案·爱丽丝", out["markdown"])
            self.assertIn("回眸扶发", out["markdown"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
