#!/usr/bin/env python3
"""
ZERO STUDIO · 对内微信通知通道 (openclaw_notifier.py)
====================================================
面向摄影师本人的「对内」推送通道（零封号风险）：
- 优先走 OpenClaw 官方微信扫码通道 (`@tencent-weixin/openclaw-weixin` / Webhook 或 CLI)；
- 自动兼容 PushPlus 微信推送作为备用兜底通道；
- 专注三大对内场景：
  1. 拍前动作清单汇总推送（漫展现场手机直看）
  2. AI + PS 精修完成待终审通知（带预览图链接 + 一键审核放行链接）
  3. 拷卡入库完成 / 异常告警通知
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import httpx
from dotenv import load_dotenv

GATEWAY_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = GATEWAY_DIR.parents[1] if (GATEWAY_DIR.parents[1] / "apps").exists() else GATEWAY_DIR
load_dotenv(WORKSPACE_ROOT / ".env", override=False)

logger = logging.getLogger("im_gateway.openclaw_notifier")


class OpenClawWeixinNotifier:
    """对内推送器：将动作汇总、终审提醒与系统通知推送到摄影师本人微信。"""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        gateway_token: Optional[str] = None,
        weixin_target: Optional[str] = None,
        cli_enabled: Optional[bool] = None,
        pushplus_token: Optional[str] = None,
        timeout: float = 15.0,
        mock_send_fn: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self.webhook_url = (
            webhook_url if webhook_url is not None else os.getenv("OPENCLAW_NOTIFY_WEBHOOK_URL", "")
        ).strip()
        self.gateway_token = (
            gateway_token if gateway_token is not None else os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
        ).strip()
        self.weixin_target = (
            weixin_target if weixin_target is not None else os.getenv("OPENCLAW_WEIXIN_TARGET", "")
        ).strip()
        env_cli = str(os.getenv("OPENCLAW_CLI_ENABLED", "false")).strip().lower()
        self.cli_enabled = (
            bool(cli_enabled) if cli_enabled is not None else (env_cli in ("1", "true", "yes", "on"))
        )
        self.pushplus_token = (
            pushplus_token if pushplus_token is not None else os.getenv("PUSHPLUS_TOKEN", "")
        ).strip()
        self.timeout = float(timeout)
        self.mock_send_fn = mock_send_fn

    def send(
        self,
        title: str,
        markdown: str,
        image_url: str = "",
        action_url: str = "",
    ) -> Dict[str, Any]:
        """向所有已启用的对内通道发送通知，返回各通道发送状态。"""
        channels: Dict[str, Any] = {}
        payload = {
            "channel": "openclaw-weixin",
            "target": self.weixin_target,
            "title": title,
            "text": markdown,
            "markdown": markdown,
            "image_url": image_url,
            "action_url": action_url,
        }

        if self.mock_send_fn is not None:
            channels["mock"] = self.mock_send_fn("openclaw-weixin", payload)
            return {"status": "ok", "channels": channels}

        # 1) OpenClaw Gateway Webhook
        if self.webhook_url:
            headers = {"Content-Type": "application/json"}
            if self.gateway_token:
                headers["Authorization"] = f"Bearer {self.gateway_token}"
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(self.webhook_url, headers=headers, json=payload)
                    channels["openclaw_webhook"] = f"ok:{resp.status_code}" if resp.status_code < 400 else f"http_{resp.status_code}"
            except Exception as exc:  # noqa: BLE001
                logger.warning("OpenClaw Webhook 推送失败: %s", exc)
                channels["openclaw_webhook"] = f"error:{exc}"

        # 2) 本机 openclaw CLI（已通过 openclaw-weixin 扫码登录后直接发微信）
        if self.cli_enabled and not self.webhook_url:
            openclaw_bin = shutil.which("openclaw")
            if openclaw_bin:
                cmd = [
                    openclaw_bin,
                    "message",
                    "send",
                    "--channel",
                    "openclaw-weixin",
                    "--message",
                    f"{title}\n\n{markdown}",
                ]
                if self.weixin_target:
                    cmd.extend(["--target", self.weixin_target])
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, check=False)
                    channels["openclaw_cli"] = "ok" if proc.returncode == 0 else f"exit_{proc.returncode}"
                except Exception as exc:  # noqa: BLE001
                    channels["openclaw_cli"] = f"error:{exc}"
            else:
                channels["openclaw_cli"] = "skipped:openclaw_not_found"

        # 3) PushPlus 微信推送（可选备用）
        if self.pushplus_token:
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(
                        "https://www.pushplus.plus/send",
                        json={
                            "token": self.pushplus_token,
                            "title": title,
                            "content": markdown,
                            "template": "markdown",
                        },
                    )
                    channels["pushplus"] = f"ok:{resp.status_code}"
            except Exception as exc:  # noqa: BLE001
                channels["pushplus"] = f"error:{exc}"

        return {"status": "ok", "channels": channels}

    def push_pose_summary(self, markdown: str, event_name: str = "", date: str = "") -> Dict[str, Any]:
        suffix = " ".join(x for x in (date, event_name) if x).strip()
        title = f"ZERO STUDIO 现场动作汇总 {suffix}".strip()
        return self.send(title=title, markdown=markdown)

    def push_review_ready(
        self,
        booking_id: int,
        cn: str,
        character: str,
        review_url: str,
        preview_url: str = "",
        asset_count: int = 1,
    ) -> Dict[str, Any]:
        title = f"成片待终审 #{booking_id} · {cn or '客户'} ({character or '未填角色'})"
        lines = [
            f"### 订单 #{booking_id} 已完成 AI 仿色 + PS 精修，等待人工终审",
            f"- **客户 / 角色**：{cn or '-'} · {character or '-'}",
            f"- **成片数量**：{int(asset_count)} 张（已保留 16-bit 分层 PSD）",
        ]
        if preview_url:
            lines.append(f"- **最新预览图**：{preview_url}")
        if review_url:
            lines.append(f"- **手机一键终审 / 提意见修订**：{review_url}")
        return self.send(
            title=title,
            markdown="\n".join(lines),
            image_url=preview_url,
            action_url=review_url,
        )

    def push_system_alert(self, title: str, body: str) -> Dict[str, Any]:
        return self.send(title=f"ZERO STUDIO 通知 · {title}", markdown=body)
