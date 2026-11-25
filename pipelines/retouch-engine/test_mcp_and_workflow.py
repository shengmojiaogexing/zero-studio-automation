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
        self.assertGreaterEqual(len(PSMCPToolkit.get_openai_tool_schemas()), 6)

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

        # 2) 本地 8B 关闭时自动降级为确定性 MCP 编译
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
