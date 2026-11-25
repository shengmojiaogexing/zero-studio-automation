#!/usr/bin/env python3
"""
ZERO STUDIO · 拍前动作沟通与手机汇总推送模块 (pose_briefing.py)
================================================================
完整闭环：
    客户锁档后，AI 在私聊中与客户讨论想拍的动作 / Pose / 机位 / 道具 / 参考氛围
        ↓
    结构化沉淀至本地订单动作档案 (work/pose_sheets/booking_{id}.json)
        ↓
    按场次/日期自动生成适合手机浏览的「现场拍摄动作速查卡片」
        ↓
    一键推送至摄影师手机（支持 PushPlus 微信推送 / OpenClaw Webhook / 手机中控台读取）
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

GATEWAY_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = GATEWAY_DIR.parents[1] if (GATEWAY_DIR.parents[1] / "apps").exists() else GATEWAY_DIR
load_dotenv(WORKSPACE_ROOT / ".env", override=False)

logger = logging.getLogger("im_gateway.pose_briefing")

DEFAULT_POSE_SHEETS_DIR = WORKSPACE_ROOT / "work" / "pose_sheets"

POSE_DISCUSSION_PROMPT = """你是 ZERO STUDIO 摄影工作室的拍前动作沟通助手。
当前客户已完成档期预约，请结合聊天记录，用简短真人气泡和客户确认想拍的动作、机位、道具与避雷点，并提取结构化动作清单。

严格仅输出合法 JSON：
{
  "poses": ["动作1（如：回眸看镜头手扶假发）", "动作2（如：低机位仰拍拔剑）"],
  "props": ["道具1"],
  "lighting_mood": "想要的氛围/光感（如：清冷逆光、高反差红蓝撞色）",
  "avoid": ["避雷点（如：不拍左侧脸、不拍俯拍）"],
  "replies": ["短句1", "短句2"]
}"""


def _dedup_clean(items: Any) -> List[str]:
    if not isinstance(items, list):
        return []
    out: List[str] = []
    seen = set()
    for raw in items:
        s = str(raw or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


class PoseBriefingStore:
    """管理每个订单的拍前动作清单，并生成/推送手机端速查汇总。"""

    def __init__(self, sheets_dir: Optional[Path] = None):
        self.sheets_dir = Path(sheets_dir or DEFAULT_POSE_SHEETS_DIR)
        self.sheets_dir.mkdir(parents=True, exist_ok=True)

    def _sheet_path(self, booking_id: int) -> Path:
        return self.sheets_dir / f"booking_{int(booking_id)}.json"

    def load_sheet(self, booking_id: int) -> Dict[str, Any]:
        p = self._sheet_path(booking_id)
        if not p.exists():
            return {
                "booking_id": int(booking_id),
                "cn": "",
                "character": "",
                "event_name": "",
                "date": "",
                "time": "",
                "poses": [],
                "props": [],
                "lighting_mood": "",
                "avoid": [],
                "reference_images": [],
                "updated_at": "",
            }
        return json.loads(p.read_text(encoding="utf-8"))

    def upsert_sheet(
        self,
        booking_id: int,
        cn: str = "",
        character: str = "",
        event_name: str = "",
        date: str = "",
        time_slot: str = "",
        poses: Optional[List[str]] = None,
        props: Optional[List[str]] = None,
        lighting_mood: str = "",
        avoid: Optional[List[str]] = None,
        reference_images: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """增量合并客户最新讨论的动作/道具/参考图，绝不覆盖丢失已记录内容。"""
        cur = self.load_sheet(booking_id)
        if cn:
            cur["cn"] = cn.strip()
        if character:
            cur["character"] = character.strip()
        if event_name:
            cur["event_name"] = event_name.strip()
        if date:
            cur["date"] = date.strip()
        if time_slot:
            cur["time"] = time_slot.strip()
        if lighting_mood:
            cur["lighting_mood"] = lighting_mood.strip()

        cur["poses"] = _dedup_clean(list(cur.get("poses") or []) + list(poses or []))
        cur["props"] = _dedup_clean(list(cur.get("props") or []) + list(props or []))
        cur["avoid"] = _dedup_clean(list(cur.get("avoid") or []) + list(avoid or []))
        cur["reference_images"] = _dedup_clean(
            list(cur.get("reference_images") or []) + list(reference_images or [])
        )
        cur["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        p = self._sheet_path(booking_id)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)
        return cur

    def list_sheets(self, event_name: str = "", date: str = "") -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for f in sorted(self.sheets_dir.glob("booking_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if event_name and str(data.get("event_name") or "").strip().lower() != event_name.strip().lower():
                continue
            if date and str(data.get("date") or "").strip() != date.strip():
                continue
            items.append(data)
        items.sort(key=lambda x: (str(x.get("date") or ""), str(x.get("time") or ""), int(x.get("booking_id") or 0)))
        return items

    def render_mobile_summary(self, event_name: str = "", date: str = "") -> str:
        """生成适合手机快速浏览的漫展现场动作速查汇总（Markdown）。"""
        sheets = self.list_sheets(event_name=event_name, date=date)
        title_parts = [p for p in (event_name, date) if p]
        header = "ZERO STUDIO · 现场拍摄动作汇总" + (f"（{' / '.join(title_parts)}）" if title_parts else "")
        if not sheets:
            return f"# {header}\n\n暂无已登记的拍前动作清单。"

        lines = [f"# {header}", f"共 {len(sheets)} 组待拍动作：", ""]
        for s in sheets:
            slot = " ".join(x for x in (s.get("date"), s.get("time")) if x) or "待定时间"
            lines.append(
                f"## #{s['booking_id']} {s.get('cn') or '未填CN'} · {s.get('character') or '未填角色'}（{slot}）"
            )
            poses = s.get("poses") or []
            if poses:
                lines.append("- **必拍动作**：" + " / ".join(poses))
            if s.get("props"):
                lines.append("- **携带道具**：" + "、".join(s["props"]))
            if s.get("lighting_mood"):
                lines.append(f"- **光影氛围**：{s['lighting_mood']}")
            if s.get("avoid"):
                lines.append("- **避雷注意**：" + "、".join(s["avoid"]))
            if s.get("reference_images"):
                lines.append(f"- **参考图数**：{len(s['reference_images'])} 张")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def push_summary_to_mobile(
        self,
        event_name: str = "",
        date: str = "",
        pushplus_token: Optional[str] = None,
        openclaw_webhook_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """将动作汇总保存到本地并推送到摄影师手机（PushPlus 微信 / OpenClaw Webhook）。"""
        md = self.render_mobile_summary(event_name=event_name, date=date)
        tag = "_".join(x for x in (date or "all", event_name or "all") if x).replace("/", "-")
        summary_file = self.sheets_dir / f"mobile_summary_{tag}.md"
        summary_file.write_text(md, encoding="utf-8")

        from openclaw_notifier import OpenClawWeixinNotifier
        notifier = OpenClawWeixinNotifier(
            webhook_url=openclaw_webhook_url,
            pushplus_token=pushplus_token,
        )
        notify_res = notifier.push_pose_summary(markdown=md, event_name=event_name, date=date)

        return {
            "status": "ok",
            "summary_file": str(summary_file),
            "channels": notify_res.get("channels") or {},
            "markdown": md,
        }
