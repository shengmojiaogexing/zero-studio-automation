#!/usr/bin/env python3
"""
ZERO STUDIO · mikechambers/adb-mcp Socket.IO 客户端适配器 (adb_mcp_client.py)
=============================================================================
直接对接 `infra/adb-mcp/adb-proxy-socket/proxy.js`（默认 http://localhost:3001）
与 Photoshop 内加载的 `infra/adb-mcp/uxp/ps` UXP 插件。

支持：
1. 探测 `adb-proxy-socket` 端口连通性（`is_proxy_online()`）；
2. 发送标准 `adb-mcp` Photoshop UXP 命令（`openPhotoshopFile`、`getDocumentInfo`、
   `selectSubject`、`addColorBalanceAdjustmentLayer`、`addBrightnessContrastAdjustmentLayer`、
   `addVibranceAdjustmentLayer`、`harmonizeLayer`、`saveDocumentAs` 等）；
3. 代码层安全边界钳位（防止 8B / 云端模型给出极端调色值）；
4. 支持 `mock_send_fn` 注入，便于离线单元测试。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

ENGINE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = ENGINE_DIR.parents[1]
load_dotenv(WORKSPACE_ROOT / ".env", override=False)

logger = logging.getLogger("retouch_engine.adb_mcp_client")


def _clamp_int(val: Any, lo: int, hi: int) -> int:
    try:
        num = int(round(float(val)))
    except (TypeError, ValueError):
        num = 0
    return max(lo, min(hi, num))


class AdbMCPPhotoshopClient:
    """直接与 `mikechambers/adb-mcp` 的 `adb-proxy-socket` 通信的 Photoshop 客户端。"""

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        timeout: float = 20.0,
        application: str = "photoshop",
        mock_send_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self.proxy_url = (
            proxy_url
            or os.getenv("ADB_MCP_PROXY_URL")
            or "http://127.0.0.1:3001"
        ).strip().rstrip("/")
        self.timeout = float(timeout)
        self.application = application
        self.mock_send_fn = mock_send_fn
        self.call_history: List[Dict[str, Any]] = []

    def is_proxy_online(self, timeout: float = 1.5) -> bool:
        """快速 TCP 探测 `adb-proxy-socket` 是否已启动监听。"""
        if self.mock_send_fn is not None:
            return True
        parsed = urlparse(self.proxy_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 3001)
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def send_command(self, action: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发送一条 `adb-mcp` 协议命令包 `{action, options}` 并同步等待 UXP 插件回报。"""
        command = {"action": str(action), "options": dict(options or {})}

        if self.mock_send_fn is not None:
            resp = self.mock_send_fn(command)
            self.call_history.append({"command": command, "response": resp})
            return resp

        try:
            import socketio  # type: ignore
        except ImportError as exc:
            raise RuntimeError("缺少 python-socketio，请执行: pip install python-socketio websocket-client") from exc

        sio = socketio.Client(logger=False, engineio_logger=False)
        q: Queue[Any] = Queue()

        @sio.event
        def connect():
            sio.emit(
                "command_packet",
                {
                    "type": "command",
                    "application": self.application,
                    "command": command,
                },
            )

        @sio.event
        def packet_response(data):
            q.put(data)
            try:
                sio.disconnect()
            except Exception:
                pass

        @sio.event
        def connect_error(err):
            q.put({"status": "ERROR", "message": f"adb-proxy-socket connect_error: {err}"})

        @sio.event
        def disconnect():
            if q.empty():
                q.put({"status": "ERROR", "message": "disconnected before response"})

        def _worker():
            try:
                sio.connect(self.proxy_url, transports=["websocket"])
                sio.wait()
            except Exception as exc:  # noqa: BLE001
                if q.empty():
                    q.put({"status": "ERROR", "message": str(exc)})
                try:
                    if sio.connected:
                        sio.disconnect()
                except Exception:
                    pass

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        try:
            resp = q.get(timeout=self.timeout)
        except Empty as exc:
            try:
                if sio.connected:
                    sio.disconnect()
            except Exception:
                pass
            raise TimeoutError(f"等待 adb-mcp Photoshop UXP 响应超时 ({self.timeout}s): {action}") from exc

        self.call_history.append({"command": command, "response": resp})
        if isinstance(resp, dict) and resp.get("status") == "ERROR":
            raise RuntimeError(f"adb-mcp 执行失败 ({action}): {resp.get('message')}")
        return resp if isinstance(resp, dict) else {"status": "SUCCESS", "response": resp}

    # ==================== 常用 Cosplay 精修原子方法（带安全钳位） ====================

    def open_photoshop_file(self, file_path: str) -> Dict[str, Any]:
        return self.send_command("openPhotoshopFile", {"filePath": str(Path(file_path))})

    def get_document_info(self) -> Dict[str, Any]:
        return self.send_command("getDocumentInfo", {})

    def get_layers(self) -> Dict[str, Any]:
        return self.send_command("getLayers", {})

    def select_subject(self, layer_id: int) -> Dict[str, Any]:
        return self.send_command("selectSubject", {"layerId": int(layer_id)})

    def add_brightness_contrast(
        self,
        layer_id: int,
        brightness: int = 0,
        contrast: int = 0,
    ) -> Dict[str, Any]:
        return self.send_command(
            "addBrightnessContrastAdjustmentLayer",
            {
                "layerId": int(layer_id),
                "brightness": _clamp_int(brightness, -40, 40),
                "contrast": _clamp_int(contrast, -35, 35),
            },
        )

    def add_vibrance(
        self,
        layer_id: int,
        vibrance: int = 0,
        saturation: int = 0,
    ) -> Dict[str, Any]:
        return self.send_command(
            "addVibranceAdjustmentLayer",
            {
                "layerId": int(layer_id),
                "vibrance": _clamp_int(vibrance, -30, 30),
                "saturation": _clamp_int(saturation, -25, 25),
            },
        )

    def add_color_balance(
        self,
        layer_id: int,
        shadows: Optional[List[int]] = None,
        midtones: Optional[List[int]] = None,
        highlights: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        def _clamp_triple(vals: Optional[List[int]]) -> List[int]:
            arr = list(vals or [0, 0, 0])
            while len(arr) < 3:
                arr.append(0)
            return [_clamp_int(arr[i], -25, 25) for i in range(3)]

        return self.send_command(
            "addColorBalanceAdjustmentLayer",
            {
                "layerId": int(layer_id),
                "shadows": _clamp_triple(shadows),
                "midtones": _clamp_triple(midtones),
                "highlights": _clamp_triple(highlights),
            },
        )

    def save_document_as(self, file_path: str, file_type: str = "PSD") -> Dict[str, Any]:
        return self.send_command(
            "saveDocumentAs",
            {"filePath": str(Path(file_path)), "fileType": str(file_type).upper()},
        )

    def save_preview_png(self, file_path: str) -> Dict[str, Any]:
        return self.send_command("saveDocumentImageAsPng", {"filePath": str(Path(file_path))})
