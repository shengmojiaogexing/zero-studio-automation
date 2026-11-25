#!/usr/bin/env python3
"""
ZERO STUDIO 统一 IM 网关 FastAPI 启动入口。

功能：
1. 统一接入 QQ (NapCatQQ OneBot v11)、Windows PC 微信 (wxauto/wxauto4) 与企业微信客服 (WeCom KF)；
2. 后台定时消费防封冷却队列 (rate_limiter)；
3. 提供 NapCatQQ HTTP Webhook、企业微信客服回调验签路由、通用入站调试路由；
4. 提供内部回调接口 POST /api/gateway/notify-gallery（需 X-API-Key），供聚类完成时自动下发 /gallery/{token} 选图短链。
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

GATEWAY_DIR = Path(__file__).resolve().parent
# 独立部署时（如 Windows 虚拟机节点），.env 就放在网关目录旁边；
# 完整仓库里则位于仓库根目录。按「就近优先」查找，两种布局都能正确加载。
_LOCAL_ENV = GATEWAY_DIR / ".env"
if _LOCAL_ENV.exists():
    WORKSPACE_ROOT = GATEWAY_DIR.parent
    _ENV_FILE = _LOCAL_ENV
else:
    WORKSPACE_ROOT = GATEWAY_DIR.parents[1]
    _ENV_FILE = WORKSPACE_ROOT / ".env"
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

load_dotenv(_ENV_FILE, override=False)

from adapters.base import IMAdapter, IncomingMessage  # noqa: E402
from adapters.qq_napcat import QQNapCatAdapter  # noqa: E402
from adapters.wechat_bridge import WeChatHTTPBridgeAdapter  # noqa: E402
from adapters.wechat_wxauto import WeChatWxAutoAdapter  # noqa: E402
from adapters.wecom_kf import WeComKFAdapter  # noqa: E402
from brain import StudioBrain, load_im_rules, save_im_rules  # noqa: E402
from rate_limiter import FriendRequestRateLimiter  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [im_gateway] %(message)s",
)
logger = logging.getLogger("im_gateway.main")


class IncomingMessagePayload(BaseModel):
    platform: str
    chat_type: str = "private"
    group_id: Optional[str] = None
    user_id: str
    nickname: str = ""
    text: str = ""
    images: List[str] = Field(default_factory=list)
    raw: Dict[str, Any] = Field(default_factory=dict)


class NotifyGalleryPayload(BaseModel):
    platform: str
    user_id: str
    booking_id: int
    cn: str = ""
    character: str = ""
    event_name: str = ""
    gallery_token: str
    gallery_url: Optional[str] = None
    thumb_media_id: str = ""


class NotifyDeliveryPayload(BaseModel):
    platform: str
    user_id: str
    booking_id: int
    cn: str = ""
    character: str = ""
    event_name: str = ""
    gallery_token: str = ""
    delivery_url: str
    output_count: int = 0
    thumb_media_id: str = ""


class ConversationReplyPayload(BaseModel):
    platform: str
    chat_id: str
    text: str
    chat_type: str = "private"
    group_id: str = ""
    user_id: str = ""
    nickname: str = ""


def make_internal_key_verifier(override_key: Optional[str] = None):
    def _verify(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
        expected = (override_key if override_key is not None else os.getenv("INTERNAL_API_KEY", "")).strip()
        if not expected or not x_api_key or not hmac.compare_digest(x_api_key, expected):
            raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing X-API-Key")
    return _verify


def create_app(
    adapters: Optional[Dict[str, IMAdapter]] = None,
    rate_limiter: Optional[FriendRequestRateLimiter] = None,
    llm_client: Optional[Any] = None,
    booking_api_url: Optional[str] = None,
    internal_api_key: Optional[str] = None,
    enable_background_workers: bool = False,
    rules_path: Optional[Path] = None,
) -> FastAPI:
    """工厂方法构建 FastAPI 应用（便于生产运行与测试依赖注入）。"""
    if adapters is not None:
        resolved_adapters: Dict[str, IMAdapter] = adapters
    else:
        resolved_adapters: Dict[str, IMAdapter] = {}
        # QQ 只在有 NapCatQQ 的机器上启用（Mac 宿主机）；Windows 微信节点不需要
        if os.getenv("ENABLE_QQ_ADAPTER", "true").lower() in ("1", "true", "yes"):
            resolved_adapters["qq"] = QQNapCatAdapter()
        # 对外微信通道按 WECHAT_MODE 灵活定型：
        #   - bridge_http: 新一代微信 4.x HTTP/Webhook 桥接（openclaw-wechat-channel / wechatbot-new / gewechat）
        #   - desktop:     传统 PC 微信 wxauto/wxauto4
        #   - wecom_kf:    仅使用官方企业微信客服（合规商业首选，不启个人微信）
        if os.getenv("ENABLE_WECHAT_ADAPTER", "true").lower() in ("1", "true", "yes"):
            wechat_mode = os.getenv("WECHAT_MODE", "desktop").strip().lower()
            if wechat_mode == "bridge_http":
                resolved_adapters["wechat"] = WeChatHTTPBridgeAdapter()
            elif wechat_mode != "wecom_kf":
                resolved_adapters["wechat"] = WeChatWxAutoAdapter()
        # 企微客服走公网回调，不需要本机客户端，默认启用
        resolved_adapters["wecom_kf"] = WeComKFAdapter()
    logger.info("启用的适配器: %s", ", ".join(sorted(resolved_adapters.keys())))
    resolved_limiter = rate_limiter or FriendRequestRateLimiter()
    resolved_rules_path = Path(rules_path) if rules_path is not None else (WORKSPACE_ROOT / "configs" / "im_rules.json")
    resolved_brain = StudioBrain(
        rate_limiter=resolved_limiter,
        adapters=resolved_adapters,
        llm_client=llm_client,
        booking_api_url=booking_api_url,
        internal_api_key=internal_api_key,
        rules_path=resolved_rules_path,
    )

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        stop_event = asyncio.Event()
        tasks: List[asyncio.Task] = []

        if enable_background_workers:
            async def _queue_worker():
                while not stop_event.is_set():
                    try:
                        await resolved_limiter.process_due_requests(resolved_adapters)
                    except Exception as exc:
                        logger.error("冷却队列消费异常: %s", exc)
                    await asyncio.sleep(5.0)

            tasks.append(asyncio.create_task(_queue_worker()))

            async def _conversation_cleanup_worker():
                while not stop_event.is_set():
                    try:
                        stats = await asyncio.to_thread(resolved_brain.conversation_store.cleanup)
                        if any(stats.values()):
                            logger.info("会话记忆定期清理完成: %s", stats)
                    except Exception as exc:
                        logger.error("会话记忆定期清理异常: %s", exc)
                    await asyncio.sleep(resolved_brain.conversation_store.cleanup_interval_sec)

            tasks.append(asyncio.create_task(_conversation_cleanup_worker()))

            if os.getenv("ENABLE_NAPCAT_WS", "false").lower() in ("1", "true", "yes"):
                qq_adapter = resolved_adapters.get("qq")
                if isinstance(qq_adapter, QQNapCatAdapter):
                    tasks.append(asyncio.create_task(qq_adapter.listen_ws(resolved_brain.handle_message, stop_event)))

        yield

        stop_event.set()
        wx_adapter = resolved_adapters.get("wechat")
        if isinstance(wx_adapter, WeChatWxAutoAdapter):
            wx_adapter.stop_monitor()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="ZERO STUDIO IM Unified Gateway", version="6.0.0", lifespan=lifespan)
    app.state.adapters = resolved_adapters
    app.state.rate_limiter = resolved_limiter
    app.state.brain = resolved_brain
    app.state.rules_path = resolved_rules_path
    internal_key_dep = Depends(make_internal_key_verifier(internal_api_key))

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "adapters": list(resolved_adapters.keys()),
            "dry_run_add_friend": resolved_limiter.dry_run,
        }

    @app.post("/webhook/napcat")
    async def napcat_webhook(request: Request) -> Dict[str, Any]:
        """接收 NapCatQQ OneBot v11 HTTP POST 上报事件。"""
        event = await request.json()
        qq_adapter = resolved_adapters.get("qq")
        if isinstance(qq_adapter, QQNapCatAdapter) and isinstance(event, dict):
            qq_adapter.record_event_heartbeat(event, source="webhook")
        incoming = QQNapCatAdapter.parse_onebot_event(event)
        if incoming is None:
            return {"status": "ignored"}
        result = await resolved_brain.handle_message(incoming)
        return {"status": "ok", "result": result}

    @app.websocket("/ws/napcat")
    async def napcat_reverse_ws(websocket: WebSocket) -> None:
        """NapCatQQ OneBot v11 反向 WebSocket 监听端点（双模监听）。"""
        await websocket.accept()
        qq_adapter = resolved_adapters.get("qq")
        if isinstance(qq_adapter, QQNapCatAdapter):
            qq_adapter.reverse_ws_connected = True
        try:
            while True:
                raw_text = await websocket.receive_text()
                try:
                    import json as _json
                    event = _json.loads(raw_text)
                    if isinstance(qq_adapter, QQNapCatAdapter) and isinstance(event, dict):
                        qq_adapter.record_event_heartbeat(event, source="reverse_ws")
                    if isinstance(event, dict) and event.get("post_type") == "meta_event":
                        continue
                    incoming = QQNapCatAdapter.parse_onebot_event(event)
                    if incoming is not None:
                        await resolved_brain.handle_message(incoming)
                except Exception as exc:
                    logger.warning("处理反向 WS NapCatQQ 事件异常: %s", exc)
        except WebSocketDisconnect:
            pass
        finally:
            if isinstance(qq_adapter, QQNapCatAdapter):
                qq_adapter.reverse_ws_connected = False

    @app.get("/webhook/wecom-kf", response_class=PlainTextResponse)
    async def wecom_kf_verify(
        msg_signature: str = Query(...),
        timestamp: str = Query(...),
        nonce: str = Query(...),
        echostr: str = Query(...),
    ) -> PlainTextResponse:
        """企业微信客服回调 URL 验签（GET）。"""
        wecom = resolved_adapters.get("wecom_kf")
        if not isinstance(wecom, WeComKFAdapter):
            raise HTTPException(status_code=503, detail="WeCom KF adapter unavailable")
        try:
            plain = wecom.verify_url_callback(msg_signature, timestamp, nonce, echostr)
            return PlainTextResponse(content=plain, status_code=200)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/webhook/wecom-kf")
    async def wecom_kf_callback(
        request: Request,
        msg_signature: str = Query(...),
        timestamp: str = Query(...),
        nonce: str = Query(...),
    ) -> Dict[str, Any]:
        """企业微信客服回调事件验签入口（POST）。"""
        wecom = resolved_adapters.get("wecom_kf")
        if not isinstance(wecom, WeComKFAdapter):
            raise HTTPException(status_code=503, detail="WeCom KF adapter unavailable")
        raw_body = (await request.body()).decode("utf-8", errors="ignore")
        try:
            encrypt_str = wecom.extract_encrypt_from_xml(raw_body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not wecom.verify_signature(msg_signature, timestamp, nonce, encrypt_str):
            raise HTTPException(status_code=403, detail="Invalid WeCom callback signature")
        return {"status": "ok", "verified": True}

    @app.post("/api/gateway/incoming", dependencies=[internal_key_dep])
    async def gateway_incoming(payload: IncomingMessagePayload) -> Dict[str, Any]:
        """统一标准消息入站接口（供 wxauto 轮询器或测试脚本投递）。"""
        msg = IncomingMessage(
            platform=payload.platform,
            chat_type=payload.chat_type,
            group_id=payload.group_id,
            user_id=payload.user_id,
            nickname=payload.nickname,
            text=payload.text,
            images=payload.images,
            raw=payload.raw,
        )
        result = await resolved_brain.handle_message(msg)
        return {"status": "ok", "result": result}

    @app.post("/api/gateway/notify-gallery", dependencies=[internal_key_dep])
    async def notify_gallery(payload: NotifyGalleryPayload) -> Dict[str, Any]:
        """内部回调接口：供 Sprint 2 聚类完成后自动向客户推送 /gallery/{token} 选图短链。"""
        adapter = resolved_adapters.get(payload.platform)
        if adapter is None:
            raise HTTPException(status_code=400, detail=f"Unsupported platform: {payload.platform}")

        public_base = (
            os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
            or (booking_api_url or os.getenv("BOOKING_API_URL") or "http://127.0.0.1:3000").rstrip("/")
        )
        gallery_url = payload.gallery_url or f"{public_base}/gallery/{payload.gallery_token}"
        title = f"{payload.cn or '你好'}，你的「{payload.character or '本次拍摄'}」选图相册已就绪！"
        desc = (
            f"活动：{payload.event_name or 'ZERO STUDIO'} | 订单 #{payload.booking_id}\n"
            "系统已自动过滤黑片与虚焦废片，请进入链接勾选精修照片并选择出片调色预设~"
        )
        send_res = await adapter.send_gallery_link(
            target_id=payload.user_id,
            title=title,
            desc=desc,
            gallery_url=gallery_url,
            thumb_media_id=payload.thumb_media_id,
        )
        message_id = await asyncio.to_thread(
            resolved_brain.conversation_store.record_outgoing,
            platform=payload.platform,
            chat_id=payload.user_id,
            text=f"{title}\n{desc}\n{gallery_url}",
            user_id=payload.user_id,
            nickname=payload.cn,
            action="GALLERY_NOTIFICATION",
        )
        return {
            "status": "ok",
            "platform": payload.platform,
            "user_id": payload.user_id,
            "gallery_url": gallery_url,
            "message_id": message_id,
            "delivery": send_res,
        }

    @app.post("/api/gateway/notify-delivery", dependencies=[internal_key_dep])
    async def notify_delivery(payload: NotifyDeliveryPayload) -> Dict[str, Any]:
        """内部回调接口：摄影师终审放行交付后，自动向客户微信/QQ/企微推送精修成品链接。"""
        adapter = resolved_adapters.get(payload.platform)
        if adapter is None:
            raise HTTPException(status_code=400, detail=f"Unsupported platform: {payload.platform}")

        title = f"🎉 {payload.cn or '你好'}，你的「{payload.character or '精修成片'}」已通过终审可以领取啦！"
        count_hint = f"共 {payload.output_count} 张高清精修成片" if payload.output_count > 0 else "高清精修成片已备好"
        desc = (
            f"活动：{payload.event_name or 'ZERO STUDIO'} | 订单 #{payload.booking_id}\n"
            f"{count_hint}，摄影师已完成最终质检，点击下方链接即可查看与下载原图~"
        )
        send_res = await adapter.send_gallery_link(
            target_id=payload.user_id,
            title=title,
            desc=desc,
            gallery_url=payload.delivery_url,
            thumb_media_id=payload.thumb_media_id,
        )
        message_id = await asyncio.to_thread(
            resolved_brain.conversation_store.record_outgoing,
            platform=payload.platform,
            chat_id=payload.user_id,
            text=f"{title}\n{desc}\n{payload.delivery_url}",
            user_id=payload.user_id,
            nickname=payload.cn,
            action="DELIVERY_NOTIFICATION",
        )
        return {
            "status": "ok",
            "platform": payload.platform,
            "user_id": payload.user_id,
            "booking_id": payload.booking_id,
            "delivery_url": payload.delivery_url,
            "message_id": message_id,
            "delivery": send_res,
        }

    @app.get("/api/gateway/queue/stats", dependencies=[internal_key_dep])
    async def queue_stats() -> Dict[str, Any]:
        return {"status": "ok", "stats": resolved_limiter.get_stats()}

    @app.get("/api/gateway/leads", dependencies=[internal_key_dep])
    async def list_leads(limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
        return {
            "status": "ok",
            "stats": resolved_limiter.get_stats(),
            "leads": resolved_limiter.list_leads(limit=limit),
        }

    @app.post("/api/gateway/leads/{queue_id}/action", dependencies=[internal_key_dep])
    async def lead_action(queue_id: int, request: Request) -> Dict[str, Any]:
        body = await request.json()
        action = str(body.get("action") or "").strip().lower()
        try:
            res = await resolved_limiter.handle_lead_action(queue_id, action, resolved_adapters)
            return {"status": "ok", "result": res, "stats": resolved_limiter.get_stats()}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/gateway/config/dry-run", dependencies=[internal_key_dep])
    async def set_dry_run(request: Request) -> Dict[str, Any]:
        body = await request.json()
        resolved_limiter.dry_run = bool(body.get("dry_run", True))
        return {"status": "ok", "dry_run": resolved_limiter.dry_run}

    # ==================== Sprint 6: IM 规则持久化 & 微信/QQ 真实连接工作台接口 ====================

    @app.get("/api/gateway/rules", dependencies=[internal_key_dep])
    async def get_rules() -> Dict[str, Any]:
        return {"status": "ok", "rules": load_im_rules(resolved_rules_path)}

    @app.put("/api/gateway/rules", dependencies=[internal_key_dep])
    async def update_rules(request: Request) -> Dict[str, Any]:
        body = await request.json()
        saved = save_im_rules(body if isinstance(body, dict) else {}, resolved_rules_path)
        wx_adapter = resolved_adapters.get("wechat")
        if isinstance(wx_adapter, WeChatWxAutoAdapter):
            wx_adapter.listen_groups = list(saved.get("wechat_groups") or [])
        return {"status": "ok", "rules": saved}

    @app.get("/api/gateway/channels/status", dependencies=[internal_key_dep])
    async def channels_status() -> Dict[str, Any]:
        gw_port = int(os.getenv("IM_GATEWAY_PORT", "8090"))
        qq_adapter = resolved_adapters.get("qq")
        if isinstance(qq_adapter, QQNapCatAdapter):
            qq_st = await qq_adapter.get_status()
        else:
            qq_st = {"online": qq_adapter is not None, "http_url": os.getenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3002")}
        qq_st["webhook_url"] = f"http://127.0.0.1:{gw_port}/webhook/napcat"
        qq_st["reverse_ws_url"] = f"ws://127.0.0.1:{gw_port}/ws/napcat"

        wx_adapter = resolved_adapters.get("wechat")
        if isinstance(wx_adapter, WeChatWxAutoAdapter):
            wx_st = await asyncio.to_thread(wx_adapter.detect_status)
        else:
            wx_st = {"online": wx_adapter is not None, "monitor_running": False, "mode": os.getenv("WECHAT_MODE", "desktop")}

        wecom_adapter = resolved_adapters.get("wecom_kf")
        wecom_configured = bool(
            os.getenv("WECOM_CORP_ID", "").strip()
            and os.getenv("WECOM_KF_SECRET", "").strip()
            and os.getenv("WECOM_OPEN_KFID", "").strip()
        )
        wecom_st = {
            "online": wecom_configured,
            "configured": wecom_configured,
            "adapter_ready": wecom_adapter is not None,
            "callback_url": f"http://127.0.0.1:{gw_port}/webhook/wecom-kf",
        }
        return {
            "status": "ok",
            "qq": qq_st,
            "wechat": wx_st,
            "wecom_kf": wecom_st,
            "rules": load_im_rules(resolved_rules_path),
        }

    @app.post("/api/gateway/qq/test-send", dependencies=[internal_key_dep])
    async def qq_test_send(request: Request) -> Dict[str, Any]:
        body = await request.json()
        qq_num = str(body.get("qq") or body.get("user_id") or "").strip()
        text = str(body.get("text") or "【ZERO STUDIO 测试消息】QQ NapCatQQ 通道连接正常！").strip()
        if not qq_num:
            raise HTTPException(status_code=400, detail="请填写目标 QQ 号")
        qq_adapter = resolved_adapters.get("qq")
        if qq_adapter is None:
            raise HTTPException(status_code=503, detail="QQ 适配器未启用")
        try:
            res = await qq_adapter.send_text(target_id=qq_num, text=text, chat_type="private")
            return {"status": "ok", "qq": qq_num, "text": text, "result": res}
        except Exception as exc:
            return {"status": "error", "qq": qq_num, "error": str(exc)}

    @app.get("/api/gateway/wechat/status", dependencies=[internal_key_dep])
    async def wechat_status() -> Dict[str, Any]:
        wx_adapter = resolved_adapters.get("wechat")
        if isinstance(wx_adapter, WeChatWxAutoAdapter):
            st = await asyncio.to_thread(wx_adapter.detect_status)
            return {"status": "ok", "wechat": st}
        return {"status": "ok", "wechat": {"online": False, "error": "WeChat adapter unavailable"}}

    @app.post("/api/gateway/wechat/monitor", dependencies=[internal_key_dep])
    async def wechat_monitor_control(request: Request) -> Dict[str, Any]:
        body = await request.json()
        action = str(body.get("action") or "").strip().lower()
        wx_adapter = resolved_adapters.get("wechat")
        if not isinstance(wx_adapter, WeChatWxAutoAdapter):
            raise HTTPException(status_code=503, detail="WeChat wxauto 适配器不可用")
        if action == "start":
            loop = asyncio.get_running_loop()
            res = wx_adapter.start_monitor(
                on_message=resolved_brain.handle_message,
                loop=loop,
                get_groups_fn=lambda: load_im_rules(resolved_rules_path).get("wechat_groups", []),
                get_private_chats_fn=lambda: load_im_rules(resolved_rules_path).get("wechat_private_chats", []),
                get_rules_fn=lambda: load_im_rules(resolved_rules_path),
            )
            return {"status": "ok" if res.get("monitor_running") else "error", **res}
        if action == "stop":
            res = wx_adapter.stop_monitor()
            return {"status": "ok", **res}
        raise HTTPException(status_code=400, detail="action 必须为 start 或 stop")

    @app.get("/api/gateway/qq/groups", dependencies=[internal_key_dep])
    async def list_qq_groups() -> Dict[str, Any]:
        """从 NapCatQQ 拉取当前 QQ 号已加入的全部 QQ 群列表，供中控台勾选监控。"""
        qq_adapter = resolved_adapters.get("qq")
        if not isinstance(qq_adapter, QQNapCatAdapter):
            return {"status": "error", "groups": [], "error": "QQ 适配器未启用"}
        try:
            res = await qq_adapter._call_api("get_group_list", {})
            raw_list = res.get("data") if isinstance(res, dict) else []
            groups = [
                {
                    "group_id": str(g.get("group_id", "")),
                    "group_name": str(g.get("group_name") or g.get("group_id") or ""),
                    "member_count": int(g.get("member_count") or 0),
                }
                for g in (raw_list or [])
                if isinstance(g, dict) and g.get("group_id")
            ]
            return {"status": "ok", "groups": groups}
        except Exception as exc:
            return {"status": "error", "groups": [], "error": str(exc)}

    @app.get("/api/gateway/wechat/sessions", dependencies=[internal_key_dep])
    async def list_wechat_sessions() -> Dict[str, Any]:
        """从 PC 微信拉取当前左侧会话列表，供中控台勾选监控群聊或拉黑私聊。"""
        wx_adapter = resolved_adapters.get("wechat")
        if not isinstance(wx_adapter, WeChatWxAutoAdapter):
            return {"status": "error", "sessions": [], "error": "微信适配器未启用"}
        try:
            sessions = await asyncio.to_thread(wx_adapter.list_sessions)
            return {"status": "ok", "sessions": sessions}
        except Exception as exc:
            return {"status": "error", "sessions": [], "error": str(exc)}

    @app.get("/api/gateway/conversations", dependencies=[internal_key_dep])
    async def list_conversations(
        status: str = Query(default="all"),
        q: str = Query(default=""),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> Dict[str, Any]:
        try:
            return await asyncio.to_thread(
                resolved_brain.conversation_store.list_conversations,
                status,
                q,
                limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/gateway/conversations/detail", dependencies=[internal_key_dep])
    async def conversation_detail(
        platform: str = Query(...),
        chat_id: str = Query(...),
        limit: int = Query(default=500, ge=1, le=1000),
        before_id: Optional[int] = Query(default=None),
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            resolved_brain.conversation_store.get_conversation,
            platform,
            chat_id,
            limit,
            before_id,
        )

    @app.post("/api/gateway/conversations/reply", dependencies=[internal_key_dep])
    async def reply_to_conversation(payload: ConversationReplyPayload) -> Dict[str, Any]:
        text = payload.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="回复内容不能为空")
        adapter = resolved_adapters.get(payload.platform)
        if adapter is None:
            raise HTTPException(status_code=503, detail=f"{payload.platform} 适配器未启用")
        try:
            delivery = await adapter.send_text(
                target_id=payload.chat_id,
                text=text,
                chat_type=payload.chat_type,
            )
            message_id = await asyncio.to_thread(
                resolved_brain.conversation_store.record_outgoing,
                platform=payload.platform,
                chat_id=payload.chat_id,
                text=text,
                chat_type=payload.chat_type,
                group_id=payload.group_id,
                user_id=payload.user_id,
                nickname=payload.nickname,
                action="MANUAL_REPLY",
                resolve_pending=True,
            )
            return {"status": "ok", "message_id": message_id, "delivery": delivery}
        except Exception as exc:
            logger.error("人工回复发送失败 [%s/%s]: %s", payload.platform, payload.chat_id, exc)
            raise HTTPException(status_code=502, detail=f"回复发送失败: {exc}") from exc

    @app.post("/api/gateway/wechat/test-send", dependencies=[internal_key_dep])
    async def wechat_test_send(request: Request) -> Dict[str, Any]:
        body = await request.json()
        target = str(body.get("target") or "文件传输助手").strip()
        text = str(body.get("text") or "【ZERO STUDIO 测试消息】PC 微信 wxauto 通道连通正常！").strip()
        wx_adapter = resolved_adapters.get("wechat")
        if wx_adapter is None:
            raise HTTPException(status_code=503, detail="微信适配器未启用")
        try:
            res = await wx_adapter.send_text(target_id=target, text=text, chat_type="private")
            return {"status": "ok", "target": target, "text": text, "result": res}
        except Exception as exc:
            return {"status": "error", "target": target, "error": str(exc)}

    @app.post("/webhook/wechat-bridge")
    async def wechat_bridge_webhook(request: Request) -> Dict[str, Any]:
        """接收外部微信 4.x 桥接服务（如 openclaw-wechat-channel / wechatbot-new）推送的入站消息。"""
        expected_token = (os.getenv("WECHAT_BRIDGE_TOKEN") or "").strip()
        if expected_token:
            got = (
                request.headers.get("X-Bridge-Token")
                or request.headers.get("Authorization", "").replace("Bearer ", "").strip()
            )
            if got != expected_token:
                raise HTTPException(status_code=401, detail="Invalid WECHAT_BRIDGE_TOKEN")
        body = await request.json()
        incoming = WeChatHTTPBridgeAdapter.parse_webhook_event(body)
        if incoming is None:
            return {"status": "ignored", "reason": "invalid_payload"}
        result = await resolved_brain.handle_message(incoming)
        return {"status": "ok", "result": result}

    return app


app = create_app(enable_background_workers=True)

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("IM_GATEWAY_PORT", "8090"))
    host = os.getenv("IM_GATEWAY_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)
