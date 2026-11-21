"""Windows PC 微信适配器（兼容 wxauto / wxauto4，保留 desktop / web 切换开关）。"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from .base import IMAdapter, IncomingMessage

logger = logging.getLogger("im_gateway.wechat_wxauto")


SYSTEM_IGNORE_SESSIONS = {
    "微信团队", "文件传输助手", "微信支付", "服务通知", "订阅号消息",
    "公众号", "腾讯新闻", "微信运动", "群助手", "折叠的群聊",
    "折叠置顶聊天", "微信游戏", "腾讯客服", "QQ邮箱提醒", "企业微信通知",
}


def _co_init() -> None:
    """确保当前 Windows 线程已初始化 COM (UIAutomation / wxauto4 必需)。"""
    try:
        import pythoncom  # type: ignore
        pythoncom.CoInitialize()
    except Exception:
        pass


def _is_wechat_process_running() -> bool:
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            n = (p.info.get("name") or "").lower()
            if n in ("wechat.exe", "weixin.exe"):
                return True
        return False
    except Exception:
        return False


def _detect_wxauto_package() -> str:
    for mod_name in ("wxauto4", "wxauto"):
        if importlib.util.find_spec(mod_name) is not None:
            return mod_name
    return ""


class WeChatWxAutoAdapter(IMAdapter):
    """
    对接 Windows PC 微信（优先兼容 wxauto4，其次 wxauto），
    并在配置中保留 WECHAT_MODE (desktop | web) 切换开关以便未来扩展网页端协议。
    支持由中控台一键启停的后台群聊轮询守护线程。
    """

    platform = "wechat"

    def __init__(
        self,
        mode: Optional[str] = None,
        listen_groups: Optional[List[str]] = None,
        wx_client: Any = None,
    ):
        self.mode = (mode or os.getenv("WECHAT_MODE") or "desktop").strip().lower()
        if self.mode not in ("desktop", "web"):
            raise ValueError(f"Unsupported WECHAT_MODE: {self.mode}. Expected 'desktop' or 'web'.")
        self.listen_groups: List[str] = list(listen_groups or [])
        self._wx = wx_client
        self._backend_name = "injected" if wx_client is not None else "uninitialized"
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()
        self._monitor_running: bool = False
        self._monitor_lock = threading.Lock()
        self._registered_groups: set = set()
        self._last_poll_at: float = 0.0
        self._last_error: str = ""
        self._messages_processed: int = 0
        # 保存启动参数供看门狗自动重启
        self._restart_params: Optional[Dict[str, Any]] = None

    def detect_status(self) -> Dict[str, Any]:
        """非阻塞检测本机 PC 微信登录状态、wxauto 安装状态及后台群监控线程状态。

        若监控线程假死（last_poll_at 超过 30 秒未更新），自动触发重启。
        """
        proc_running = _is_wechat_process_running() if self._wx is None else True
        pkg_name = "injected" if self._wx is not None else _detect_wxauto_package()
        logged_in = False
        err = self._last_error

        if self._wx is not None:
            logged_in = True
        elif self.mode == "desktop" and proc_running and pkg_name:
            try:
                self._ensure_client()
                logged_in = True
                err = ""
            except Exception as exc:
                err = str(exc)
        elif not proc_running:
            err = "未检测到运行中的 PC 微信进程 (WeChat.exe / Weixin.exe)"
        elif not pkg_name:
            err = "PC 微信已运行，但尚未安装 wxauto 或 wxauto4 Python 依赖"

        # 看门狗：监控线程假死检测（30 秒无 poll 视为异常），自动完整重启
        watchdog_action = ""
        if self._monitor_running and self._last_poll_at > 0 and self._restart_params:
            stale_sec = time.time() - self._last_poll_at
            if stale_sec > 30.0:
                watchdog_action = f"monitor_stale_{int(stale_sec)}s_auto_restart"
                logger.warning("PC 微信监控假死（%.0f 秒未轮询），自动重启...", stale_sec)
                self._last_error = f"监控假死（{int(stale_sec)}s），已自动重启"
                params = dict(self._restart_params)
                def _restart() -> None:
                    try:
                        self.stop_monitor()
                        time.sleep(1)
                        self.start_monitor(**params)
                    except Exception as restart_exc:
                        logger.warning("看门狗重启监控失败: %s", restart_exc)
                        self._monitor_running = False
                threading.Thread(target=_restart, daemon=True).start()

        return {
            "online": logged_in,
            "logged_in": logged_in,
            "process_running": proc_running,
            "wxauto_installed": bool(pkg_name),
            "backend": self._backend_name if logged_in else (pkg_name or "none"),
            "mode": self.mode,
            "monitor_running": self._monitor_running,
            "monitored_groups": list(self.listen_groups),
            "messages_processed": self._messages_processed,
            "last_poll_at": self._last_poll_at or None,
            "error": err,
            "watchdog": watchdog_action or None,
        }

    def list_sessions(self) -> List[Dict[str, Any]]:
        """获取当前 PC 微信左侧会话列表（供中控台勾选监控群聊/好友）。"""
        _co_init()
        wx = self._ensure_client()
        if not hasattr(wx, "GetSession"):
            return []
        sessions = wx.GetSession() or []
        out: List[Dict[str, Any]] = []
        seen = set()
        for s in sessions:
            name = str(getattr(s, "name", "") or "").strip()
            if not name or name in SYSTEM_IGNORE_SESSIONS or name in seen:
                continue
            seen.add(name)
            out.append({
                "name": name,
                "preview": str(getattr(s, "content", "") or "")[:60],
                "is_new": bool(getattr(s, "isnew", False)),
                "new_count": int(getattr(s, "new_count", 0) or 0),
            })
        return out

    def start_monitor(
        self,
        on_message: Callable[[IncomingMessage], Awaitable[Any]],
        loop: asyncio.AbstractEventLoop,
        get_groups_fn: Optional[Callable[[], List[str]]] = None,
        get_private_chats_fn: Optional[Callable[[], List[str]]] = None,
        get_rules_fn: Optional[Callable[[], Dict[str, Any]]] = None,
        poll_interval: float = 2.0,
    ) -> Dict[str, Any]:
        """启动后台 PC 微信监控守护线程（自动兼容 wxauto4 回调监听与 wxauto 轮询模式）。"""
        with self._monitor_lock:
            if self._monitor_running and self._monitor_thread and self._monitor_thread.is_alive():
                return {"status": "already_running", "monitor_running": True, "monitored_groups": list(self.listen_groups)}

            # 保存启动参数供看门狗假死后自动重启
            self._restart_params = {
                "on_message": on_message,
                "loop": loop,
                "get_groups_fn": get_groups_fn,
                "get_private_chats_fn": get_private_chats_fn,
                "get_rules_fn": get_rules_fn,
                "poll_interval": poll_interval,
            }

            if get_groups_fn is not None:
                self.listen_groups = list(get_groups_fn() or [])
            private_targets = list(get_private_chats_fn() or []) if get_private_chats_fn is not None else []

            try:
                self._ensure_client()
                self._last_error = ""
            except Exception as exc:
                self._last_error = str(exc)
                return {
                    "status": "error",
                    "monitor_running": False,
                    "error": self._last_error,
                    "monitored_groups": list(self.listen_groups),
                }

            self._monitor_stop.clear()
            self._registered_groups.clear()
            self._monitor_running = True

            def _is_wxauto4_callback_api(wx_obj: Any) -> bool:
                import inspect
                try:
                    sig = inspect.signature(wx_obj.AddListenChat)
                    return "callback" in sig.parameters
                except Exception:
                    return self._backend_name == "wxauto4"

            def _on_wxauto4_msg(m: Any, chat: Any) -> None:
                try:
                    attr = str(getattr(m, "attr", "") or "").lower()
                    mtype = str(getattr(m, "type", "") or "").lower()
                    if attr in ("self", "system", "time", "tickle", "recall") or mtype in ("self", "sys", "system", "time", "recall"):
                        return

                    chat_name = str(getattr(chat, "who", "") or "")
                    if not chat_name and hasattr(chat, "ChatInfo"):
                        try:
                            info = chat.ChatInfo() or {}
                            chat_name = str(info.get("chat_name") or "")
                        except Exception:
                            pass
                    chat_kind = str(getattr(chat, "chat_type", "") or "").lower()

                    cur_priv = set(get_private_chats_fn() or []) if get_private_chats_fn is not None else set(private_targets)
                    is_private = (chat_name in cur_priv) or (chat_kind == "friend")

                    sender = str(getattr(m, "sender", "") or chat_name).strip()
                    content = str(getattr(m, "content", "") or "").strip()
                    images: List[str] = []
                    if content and Path(content).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and Path(content).exists():
                        images.append(content)
                        content = ""
                    if not content and not images:
                        return

                    incoming = IncomingMessage(
                        platform="wechat",
                        chat_type="private" if is_private else "group",
                        group_id=None if is_private else chat_name,
                        user_id=chat_name if is_private else sender,
                        nickname=sender or chat_name,
                        text=content,
                        images=images,
                        raw={
                            "chat": chat_name,
                            "chat_type": chat_kind,
                            "attr": attr,
                            "type": mtype,
                            "event_id": f"{getattr(m, 'id', '')}|{getattr(m, 'hash_text', '')}|{attr}|{sender}|{content}",
                        },
                    )
                    self._messages_processed += 1
                    self._last_poll_at = time.time()
                    self._last_error = ""
                    logger.info(
                        "收到 PC 微信%s消息 [%s / %s]: %s",
                        "私聊" if is_private else "群聊",
                        chat_name,
                        sender,
                        content[:60],
                    )
                    fut = asyncio.run_coroutine_threadsafe(on_message(incoming), loop)
                    try:
                        fut.result(timeout=30.0)
                    except Exception as exc:
                        self._last_error = f"AI 处理回复异常: {exc}"
                        logger.warning("PC 微信消息送入 brain 异常: %s", exc)
                except Exception as exc:
                    self._last_error = str(exc)
                    logger.warning("wxauto4 回调处理异常: %s", exc)

            def _worker() -> None:
                _co_init()
                logger.info("PC 微信后台监控守护线程已启动 (groups=%s, private=%s)", self.listen_groups, private_targets)
                use_callback = False
                try:
                    wx = self._ensure_client()
                    use_callback = _is_wxauto4_callback_api(wx)
                except Exception as exc:
                    self._last_error = str(exc)
                    self._monitor_running = False
                    return

                if use_callback:
                    # wxauto4 多会话无限制监控引擎（通过 GetSession + ChatWith + GetAllMessage 绕过免费版 1 聊限制）
                    seen_hashes: Dict[str, set] = {}
                    last_session_preview: Dict[str, str] = {}

                    def _msg_sig(m: Any) -> str:
                        msg_time = getattr(m, "timestamp", "") or getattr(m, "time", "") or getattr(m, "create_time", "")
                        return (
                            f"{getattr(m, 'id', '')}|"
                            f"{getattr(m, 'hash_text', '')}|"
                            f"{msg_time}|"
                            f"{getattr(m, 'attr', '')}|"
                            f"{getattr(m, 'sender', '')}|"
                            f"{getattr(m, 'content', '')}"
                        )

                    def _inspect_chat(target_name: str, is_private: Optional[bool], unread_count: int = 0, allowed_groups: Optional[set] = None) -> None:
                        cur_name = ""
                        cur_type = ""
                        try:
                            if hasattr(wx, "ChatInfo"):
                                ci = wx.ChatInfo() or {}
                                cur_name = str(ci.get("chat_name") or "").strip()
                                cur_type = str(ci.get("chat_type") or "").strip().lower()
                        except Exception:
                            pass

                        if cur_name != target_name:
                            wx.ChatWith(target_name, exact=False)
                            time.sleep(0.25)
                            try:
                                if hasattr(wx, "ChatInfo"):
                                    ci = wx.ChatInfo() or {}
                                    cur_type = str(ci.get("chat_type") or "").strip().lower()
                            except Exception:
                                pass

                        # 自动判定会话类型：若 is_private 未强制指定，根据 chat_type 区分私聊(friend)与群聊(group)
                        if is_private is None:
                            if cur_type == "group":
                                if allowed_groups is not None and target_name not in allowed_groups:
                                    # 未勾选监控的群聊，直接跳过
                                    return
                                resolved_private = False
                            else:
                                resolved_private = True
                        else:
                            resolved_private = is_private

                        msgs = wx.GetAllMessage() or []
                        if target_name not in seen_hashes:
                            seen_hashes[target_name] = set()
                            seed_list = msgs[:-min(unread_count, 3)] if unread_count > 0 else msgs
                            for old_m in seed_list:
                                seen_hashes[target_name].add(_msg_sig(old_m))

                        new_friend_msgs = []
                        for m in msgs:
                            sig = _msg_sig(m)
                            if sig in seen_hashes[target_name]:
                                continue
                            seen_hashes[target_name].add(sig)
                            attr = str(getattr(m, "attr", "") or "").lower()
                            mtype = str(getattr(m, "type", "") or "").lower()
                            if attr in ("self", "system", "time", "tickle", "recall") or mtype in ("self", "sys", "system", "time", "recall"):
                                continue
                            content = str(getattr(m, "content", "") or "").strip()
                            if not content:
                                continue
                            new_friend_msgs.append(m)

                        if not new_friend_msgs:
                            return

                        combined_text = "\n".join(str(getattr(m, "content", "") or "").strip() for m in new_friend_msgs)
                        last_sender = str(getattr(new_friend_msgs[-1], "sender", "") or target_name).strip()
                        incoming = IncomingMessage(
                            platform="wechat",
                            chat_type="private" if resolved_private else "group",
                            group_id=None if resolved_private else target_name,
                            user_id=target_name if resolved_private else last_sender,
                            nickname=last_sender or target_name,
                            text=combined_text,
                            images=[],
                            raw={
                                "chat": target_name,
                                "chat_type": cur_type,
                                "merged_count": len(new_friend_msgs),
                                "event_id": _msg_sig(new_friend_msgs[-1]),
                            },
                        )
                        self._messages_processed += len(new_friend_msgs)
                        self._last_poll_at = time.time()
                        self._last_error = ""
                        logger.info(
                            "收到 PC 微信%s新消息 [%s / %s]: %s",
                            "私聊" if resolved_private else "群聊",
                            target_name,
                            last_sender,
                            combined_text[:80],
                        )
                        fut = asyncio.run_coroutine_threadsafe(on_message(incoming), loop)
                        try:
                            fut.result(timeout=35.0)
                        except Exception as exc:
                            self._last_error = f"AI 处理回复异常: {exc}"
                            logger.warning("PC 微信消息送入 brain 异常: %s", exc)

                        try:
                            for post_m in (wx.GetAllMessage() or []):
                                seen_hashes[target_name].add(_msg_sig(post_m))
                        except Exception:
                            pass

                    while not self._monitor_stop.is_set():
                        try:
                            rules_now = get_rules_fn() if get_rules_fn is not None else {}
                            latest_groups = list(rules_now.get("wechat_groups") or (get_groups_fn() if get_groups_fn else self.listen_groups))
                            latest_priv = list(rules_now.get("wechat_private_chats") or (get_private_chats_fn() if get_private_chats_fn else private_targets))
                            auto_all_priv = bool(rules_now.get("wechat_auto_reply_all_private", True))
                            priv_blacklist = set(rules_now.get("wechat_private_blacklist") or [])
                            self.listen_groups = latest_groups
                            priv_set = set(latest_priv)
                            group_set = set(latest_groups)

                            sessions = wx.GetSession() or []
                            session_map: Dict[str, Any] = {}
                            for s in sessions:
                                sname = str(getattr(s, "name", "") or "").strip()
                                if sname:
                                    session_map[sname] = s

                            cur_open = ""
                            cur_open_type = ""
                            try:
                                if hasattr(wx, "ChatInfo"):
                                    ci = wx.ChatInfo() or {}
                                    cur_open = str(ci.get("chat_name") or "").strip()
                                    cur_open_type = str(ci.get("chat_type") or "").strip().lower()
                            except Exception:
                                pass

                            # 1) 检查显式配置的私聊托管名单 + 自选微信群白名单
                            explicit_targets = list(dict.fromkeys(latest_priv + latest_groups))
                            checked_names = set()
                            for target_name in explicit_targets:
                                if not target_name:
                                    continue
                                checked_names.add(target_name)
                                is_priv = target_name in priv_set
                                s_obj = session_map.get(target_name)
                                unread_cnt = int(getattr(s_obj, "new_count", 0) or 0) if s_obj else 0
                                is_new = bool(getattr(s_obj, "isnew", False)) if s_obj else False
                                preview = str(getattr(s_obj, "content", "") or "") if s_obj else ""
                                preview_changed = bool(s_obj and target_name in last_session_preview and last_session_preview[target_name] != preview)
                                if s_obj:
                                    last_session_preview[target_name] = preview

                                if (unread_cnt > 0) or is_new or preview_changed or (cur_open == target_name) or (target_name not in seen_hashes):
                                    _inspect_chat(target_name, is_private=is_priv, unread_count=max(unread_cnt, 1 if is_new else 0), allowed_groups=group_set)

                            # 2) 全微信私聊自动托管：扫描所有带未读红点或当前打开的非黑名单私聊
                            if auto_all_priv:
                                for sname, s_obj in session_map.items():
                                    if sname in checked_names or sname in SYSTEM_IGNORE_SESSIONS or sname in priv_blacklist:
                                        continue
                                    unread_cnt = int(getattr(s_obj, "new_count", 0) or 0)
                                    is_new = bool(getattr(s_obj, "isnew", False))
                                    preview = str(getattr(s_obj, "content", "") or "")
                                    preview_changed = bool(sname in last_session_preview and last_session_preview[sname] != preview)
                                    last_session_preview[sname] = preview
                                    if (unread_cnt > 0) or is_new or preview_changed:
                                        _inspect_chat(sname, is_private=None, unread_count=max(unread_cnt, 1 if is_new else 0), allowed_groups=group_set)

                                # 当前窗口正停留在某位好友私聊（无红点），同样实时监听
                                if (
                                    cur_open
                                    and cur_open not in checked_names
                                    and cur_open not in SYSTEM_IGNORE_SESSIONS
                                    and cur_open not in priv_blacklist
                                    and cur_open_type == "friend"
                                ):
                                    _inspect_chat(cur_open, is_private=True, unread_count=0, allowed_groups=group_set)

                            self._last_poll_at = time.time()
                            self._last_error = ""
                        except Exception as exc:
                            self._last_error = str(exc)
                            logger.warning("wxauto4 多会话监控异常: %s", exc)
                        self._monitor_stop.wait(poll_interval)
                else:
                    # 兼容旧版 wxauto 3.9 轮询模式
                    while not self._monitor_stop.is_set():
                        try:
                            if get_groups_fn is not None:
                                latest_groups = list(get_groups_fn() or [])
                                self.listen_groups = latest_groups
                                for g in latest_groups:
                                    if g not in self._registered_groups:
                                        try:
                                            wx.AddListenChat(who=g, savepic=True)
                                        except TypeError:
                                            wx.AddListenChat(g)
                                        self._registered_groups.add(g)

                            batch = self.poll_once()
                            self._last_poll_at = time.time()
                            self._last_error = ""
                            for msg in batch:
                                self._messages_processed += 1
                                fut = asyncio.run_coroutine_threadsafe(on_message(msg), loop)
                                try:
                                    fut.result(timeout=30.0)
                                except Exception as exc:
                                    logger.warning("PC 微信消息送入 brain 异常: %s", exc)
                        except Exception as exc:
                            self._last_error = str(exc)
                            logger.warning("PC 微信后台轮询异常: %s", exc)
                        self._monitor_stop.wait(poll_interval)

                self._monitor_running = False
                logger.info("PC 微信后台监控守护线程已停止")

            self._monitor_thread = threading.Thread(target=_worker, name="wechat-wxauto-monitor", daemon=True)
            self._monitor_thread.start()
            return {
                "status": "started",
                "monitor_running": True,
                "monitored_groups": list(self.listen_groups),
                "monitored_private_chats": private_targets,
            }

    def stop_monitor(self) -> Dict[str, Any]:
        """停止后台 PC 微信群聊轮询守护线程。"""
        with self._monitor_lock:
            self._monitor_stop.set()
            if self._monitor_thread and self._monitor_thread.is_alive():
                self._monitor_thread.join(timeout=3.0)
            self._monitor_running = False
            return {"status": "stopped", "monitor_running": False}

    def _ensure_client(self) -> Any:
        _co_init()
        if self._wx is not None:
            return self._wx

        if self.mode == "web":
            raise RuntimeError("WECHAT_MODE=web 当前为预留模式，请切换 desktop 或接入外部 Web 微信客户端。")

        # 优先尝试 wxauto4（适配微信 4.x），其次回退 wxauto（适配微信 3.9.x）
        last_err = ""
        imported_any = False
        for mod_name in ("wxauto4", "wxauto"):
            try:
                mod = __import__(mod_name)
                imported_any = True
            except ImportError:
                continue
            try:
                wx_cls = getattr(mod, "WeChat")
                try:
                    self._wx = wx_cls(ads=False)
                except TypeError:
                    self._wx = wx_cls()
                self._backend_name = mod_name
                logger.info("已连接 Windows PC 微信 (%s)", mod_name)
                return self._wx
            except Exception as exc:
                last_err = f"{mod_name}: {exc}"
                continue

        if imported_any and last_err:
            raise RuntimeError(
                f"wxauto4 已安装，但连接 PC 微信窗口失败（{last_err}）。"
                "请点击电脑右下角微信托盘图标，将 PC 微信主窗口打开到桌面上（不要关闭到托盘）后重试！"
            )

        raise RuntimeError(
            "未检测到可用的 wxauto/wxauto4 或 Windows PC 微信客户端未登录。"
            "请先启动 PC 微信并安装 wxauto / wxauto4。"
        )

    def _register_listen_targets(self) -> None:
        wx = self._ensure_client()
        for group_name in self.listen_groups:
            try:
                wx.AddListenChat(who=group_name, savepic=True)
            except TypeError:
                wx.AddListenChat(group_name)

    async def send_text(
        self,
        target_id: str,
        text: str,
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        def _do_send() -> None:
            _co_init()
            wx = self._ensure_client()
            wx.SendMsg(text, target_id)

        await asyncio.to_thread(_do_send)
        return {"status": "ok", "platform": self.platform, "mode": self.mode, "target_id": target_id}

    async def send_image(
        self,
        target_id: str,
        image: Union[str, Path, bytes],
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        tmp_path: Optional[Path] = None
        try:
            if isinstance(image, bytes):
                fd, tmp_name = tempfile.mkstemp(prefix="wx_img_", suffix=".png")
                os.close(fd)
                tmp_path = Path(tmp_name)
                tmp_path.write_bytes(image)
                file_to_send = str(tmp_path)
            else:
                file_to_send = str(Path(image).resolve())

            def _do_send_file() -> None:
                _co_init()
                wx = self._ensure_client()
                wx.SendFiles(file_to_send, target_id)

            await asyncio.to_thread(_do_send_file)
            return {
                "status": "ok",
                "platform": self.platform,
                "mode": self.mode,
                "target_id": target_id,
                "file": file_to_send,
            }
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    async def send_location(
        self,
        target_id: str,
        lat: float,
        lng: float,
        title: str,
        address: str,
        chat_type: str = "private",
    ) -> Dict[str, Any]:
        """PC 微信桌面端无原生 GPS 卡片发送接口，以高可读性定位引导文本下发。"""
        guide_text = (
            f"📍 拍摄集合定位：{title}\n"
            f"详细地址：{address}\n"
            f"坐标参考：{lat:.6f}, {lng:.6f}\n"
            f"高德/百度地图直接搜索「{title}」即可导航集合~"
        )
        return await self.send_text(target_id, guide_text, chat_type=chat_type)

    async def request_add_friend(
        self,
        user_id: str,
        remark: str,
        group_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """触发 PC 微信加好友流程（支持指定来源群或按微信号/手机号检索）。"""
        wx = self._ensure_client()

        def _do_add() -> Any:
            if group_id and hasattr(wx, "AddFriendFromGroup"):
                return wx.AddFriendFromGroup(who=group_id, member=user_id, addmsg=remark)
            if hasattr(wx, "AddNewFriend"):
                return wx.AddNewFriend(keywords=user_id, addmsg=remark)
            raise NotImplementedError("当前 wxauto 版本不支持自动加好友 API")

        res = await asyncio.to_thread(_do_add)
        return {
            "status": "ok",
            "platform": self.platform,
            "mode": self.mode,
            "user_id": user_id,
            "group_id": group_id,
            "result": str(res),
        }

    def poll_once(self) -> List[IncomingMessage]:
        """单次轮询已监听的群聊与私聊消息，转换为标准 IncomingMessage 列表。"""
        wx = self._ensure_client()
        raw_map = wx.GetListenMessage() or {}
        messages: List[IncomingMessage] = []

        for chat_obj, msg_list in raw_map.items():
            chat_name = getattr(chat_obj, "who", str(chat_obj))
            is_group = chat_name in self.listen_groups
            for m in msg_list or []:
                msg_type = getattr(m, "type", "friend")
                if msg_type in ("self", "sys", "time", "recall"):
                    continue
                sender = str(getattr(m, "sender", "") or chat_name)
                content = str(getattr(m, "content", "") or "").strip()
                images: List[str] = []
                if content and Path(content).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and Path(content).exists():
                    images.append(content)
                    content = ""

                messages.append(
                    IncomingMessage(
                        platform="wechat",
                        chat_type="group" if is_group else "private",
                        group_id=chat_name if is_group else None,
                        user_id=sender,
                        nickname=sender,
                        text=content,
                        images=images,
                        raw={"chat": chat_name, "type": msg_type},
                    )
                )
        return messages

    async def listen_loop(
        self,
        on_message: Callable[[IncomingMessage], Awaitable[Any]],
        stop_event: Optional[asyncio.Event] = None,
        poll_interval: float = 1.5,
    ) -> None:
        """持续轮询 PC 微信指定群聊与私聊消息。"""
        self._register_listen_targets()
        logger.info("进入 PC 微信监听模式 (mode=%s, groups=%s)", self.mode, self.listen_groups)
        while stop_event is None or not stop_event.is_set():
            try:
                batch = await asyncio.to_thread(self.poll_once)
                for msg in batch:
                    await on_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("PC 微信轮询异常: %s", exc)
            await asyncio.sleep(poll_interval)
