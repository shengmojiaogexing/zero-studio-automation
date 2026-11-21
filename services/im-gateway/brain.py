"""DeepSeek 群聊拓客漏斗 + 私聊自动接单大脑（OpenAI 兼容客户端）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from openai import AsyncOpenAI

from adapters.base import IMAdapter, IncomingMessage
from conversation_store import ConversationStore
from qr_booking import decode_booking_from_images
from rate_limiter import FriendRequestRateLimiter

logger = logging.getLogger("im_gateway.brain")

# 规则文件查找：独立部署时在网关目录下（configs/ 与 .env 同级），
# 完整仓库里在仓库根目录。就近优先，避免独立节点读不到配置。
_GATEWAY_DIR = Path(__file__).resolve().parent
if (_GATEWAY_DIR / "configs" / "im_rules.json").exists():
    WORKSPACE_ROOT = _GATEWAY_DIR
else:
    WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IM_RULES_PATH = WORKSPACE_ROOT / "configs" / "im_rules.json"

DEFAULT_KEYWORDS = [
    "约拍", "场照", "找摄", "摄影", "漫展", "后勤", "返图",
    "正片", "棚拍", "外景", "cos摄", "蹲摄", "扩列约拍", "接拍",
]

# 本地群聊关键词正则快筛（未命中直接 0 Token 放过）
GROUP_INTENT_REGEX = re.compile(
    "(" + "|".join(re.escape(k) for k in DEFAULT_KEYWORDS) + ")",
    re.IGNORECASE,
)


DEFAULT_PRIVATE_PERSONA_PROMPT = """你就是二次元 Cosplay 摄影师「CN.庄泽 / 零柒」本人，正在用手机微信/QQ 跟客户私聊接单。

【铁律：绝不重复追问】
1. 先看「已登记信息 slots」，里面**已经有值的字段一律不要再问**。
2. 同一条信息客户已经说过、或者已经通过二维码/网站预约提交过，就以它为准，直接确认，不要让他再说一遍。
3. 客户发来图片（尤其是自己网站的预约成功截图或预约二维码）时，说明他已经预约好了——不要再让他重报 CN/角色/日期。
4. 缺什么只问什么，**一次最多问 1 个问题**，绝不连环追问、绝不把「cn 角色 哪天几点」整串甩给客户。

【收集锁档信息（按这个优先级，缺哪个问哪个）】
cn（怎么称呼）→ character（拍什么角色/风格）→ date（哪天）→ event_name（哪场漫展）→ time（大概几点）
remarks（备注）可选，客户不说就不要问。

【说话风格（去掉 AI 味）】
1. 统一叫对方「老大」，不叫老师/宝子/亲。
2. 禁用语：呀、啦、哦、～、！、😊、麻烦您、这边帮您、别急哈、收到预约啦。
3. 每次 1~3 条极短气泡，每条 2~10 个字，像真人敲键盘分条发。
4. 干脆实在，口头禅：ok、接的、可以、行。

【真实对话示范（照这个味儿）】
- 老大你还在吗，27号漫展接吗 → ["在的", "27号bacc接的", "老大拍啥角色"]
- 客户发来预约二维码/截图 → ["ok收到", "看到你的预约了", "身份信息没问题的话我就按这个给你排"]
- 客户说"我搞完了/我提交了" → ["好的老大", "那我这边确认下了"]
- 问价格 → ["场照25一张", "合成99一张"]
- 时间随便 → ["那27号下午行吗"]
- 男出女/女出男 → ["不好意思老大", "这个不接"]

【核心资料（问到才说，说的时候拆成短气泡）】
1. 价格：场照 25 一张，合成 99 一张，不满意不收费。接单 30 天内返图，互勉 3 个月内返图。
2. 配置：4 灯灯阵、三步梯、包动作指导。
3. 互勉：卡服化道和表现力；优先 IP：碧蓝航线、NIKKE、棕色尘埃2、碧蓝档案、FGO、明日方舟。
4. 婉拒：男出女、女出男。
5. 器材（仅对方问才说）：R5 / A7M3 / 尼康大F；RF 28-70 F2.8 / RF 70-200 F2.8L / RF 50 1.8 / EF 85 1.4L / FE 24-70 GM2 / FE 55 1.8 / FE 24-240；AD200Pro×2 / AD300Pro / V1 / MT00B / F100B / Action 5 Pro / Pocket 3 Pro / RS3。"""

PRIVATE_JSON_CONTRACT_SUFFIX = """

【系统强制判断与输出格式（请勿忽略）】
1. 先判断「最新客户消息」结合上下文，是否与摄影接单业务相关（约拍、场照、合成、互勉、档期、漫展、报价、返图、器材、角色/Cos，或正在继续之前的约拍沟通）。客户发来预约截图/二维码也属于相关。
2. 【漫展档期防漏单铁律】：`active_events` 里是摄影师当前**已排期并接单的漫展及具体日期**。只要对方私聊问「去不去 XX 漫展 / 某天去吗 / 这几天去不去 / 哪几天在展子 / 接不接」，**必须**将 `is_photography_related` 设为 `true`（绝不静默！）：
   - 对方问的漫展或日期落在 `active_events` 接单日期内（且未锁满）：立刻干脆回复确认会去并接单（如 `["去的", "那天接的", "老大拍啥角色"]`），顺势问缺项推进锁单；
   - 对方问的日期不在 `active_events` 内：简短回复那天不去，并顺带报出 `active_events` 里最近可约的漫展名称与日期。
3. 若只是与摄影/漫展/约拍完全无关的日常闲聊、广告、纯表情包：将 "is_photography_related" 设为 false，"replies" 设为 []，"reply" 留空字符串 ""（保持静默）。
4. 相关时才输出 1~3 条极短气泡到 "replies"。
5. 已经登记在 "current_slots" 里的字段，**不要再问**，也不要在 replies 里重复索要。
6. 若客户发来图片（has_images=true）且内容是预约截图/二维码：把 "received_booking_image" 设为 true，回复只做确认，不要追问已提交的信息。
请严格仅输出一个合法 JSON 对象：
{
  "is_photography_related": true,
  "relevance_reason": "简短理由",
  "received_booking_image": false,
  "asked_field": "本次唯一追问的字段名，没追问则填空字符串",
  "extracted": {
    "cn": "",
    "character": "",
    "event_name": "",
    "date": "",
    "time": "",
    "remarks": ""
  },
  "replies": ["短句1", "短句2"],
  "reply": "短句1\\n短句2"
}"""


def load_im_rules(rules_path: Optional[Path] = None) -> Dict[str, Any]:
    """热读取 configs/im_rules.json 群监控白名单、全私聊托管开关、关键词、意向分阈值与 AI 系统提示词。"""
    path = Path(rules_path or DEFAULT_IM_RULES_PATH)
    default_rules: Dict[str, Any] = {
        "qq_groups": [],
        "wechat_groups": [],
        "wechat_private_chats": [],
        "wechat_auto_reply_all_private": True,
        "private_only_reply_related": True,
        "wechat_private_blacklist": [],
        "keywords": list(DEFAULT_KEYWORDS),
        "intent_threshold": 80,
        "private_system_prompt": DEFAULT_PRIVATE_PERSONA_PROMPT,
        "group_system_prompt": "",
    }
    if not path.exists():
        return default_rules
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_rules
        qq_groups = [str(g).strip() for g in (data.get("qq_groups") or []) if str(g).strip()]
        wechat_groups = [str(g).strip() for g in (data.get("wechat_groups") or []) if str(g).strip()]
        wechat_private_chats = [str(g).strip() for g in (data.get("wechat_private_chats") or []) if str(g).strip()]
        wechat_private_blacklist = [str(g).strip() for g in (data.get("wechat_private_blacklist") or []) if str(g).strip()]
        auto_all_priv = bool(data.get("wechat_auto_reply_all_private", True))
        only_related = bool(data.get("private_only_reply_related", True))
        keywords = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()]
        try:
            threshold = max(0, min(100, int(data.get("intent_threshold", 80))))
        except (TypeError, ValueError):
            threshold = 80
        priv_prompt = str(data.get("private_system_prompt") or "").strip() or DEFAULT_PRIVATE_PERSONA_PROMPT
        grp_prompt = str(data.get("group_system_prompt") or "").strip()
        return {
            "qq_groups": qq_groups,
            "wechat_groups": wechat_groups,
            "wechat_private_chats": wechat_private_chats,
            "wechat_auto_reply_all_private": auto_all_priv,
            "private_only_reply_related": only_related,
            "wechat_private_blacklist": wechat_private_blacklist,
            "keywords": keywords or list(DEFAULT_KEYWORDS),
            "intent_threshold": threshold,
            "private_system_prompt": priv_prompt,
            "group_system_prompt": grp_prompt,
        }
    except Exception as exc:
        logger.warning("读取 im_rules.json 失败，使用默认规则: %s", exc)
        return default_rules


def save_im_rules(rules: Dict[str, Any], rules_path: Optional[Path] = None) -> Dict[str, Any]:
    """原子化写入 configs/im_rules.json。"""
    path = Path(rules_path or DEFAULT_IM_RULES_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = {
        "qq_groups": list(dict.fromkeys(str(g).strip() for g in (rules.get("qq_groups") or []) if str(g).strip())),
        "wechat_groups": list(dict.fromkeys(str(g).strip() for g in (rules.get("wechat_groups") or []) if str(g).strip())),
        "wechat_private_chats": list(dict.fromkeys(str(g).strip() for g in (rules.get("wechat_private_chats") or []) if str(g).strip())),
        "wechat_auto_reply_all_private": bool(rules.get("wechat_auto_reply_all_private", True)),
        "private_only_reply_related": bool(rules.get("private_only_reply_related", True)),
        "wechat_private_blacklist": list(dict.fromkeys(str(g).strip() for g in (rules.get("wechat_private_blacklist") or []) if str(g).strip())),
        "keywords": list(dict.fromkeys(str(k).strip() for k in (rules.get("keywords") or []) if str(k).strip())) or list(DEFAULT_KEYWORDS),
        "intent_threshold": max(0, min(100, int(rules.get("intent_threshold", 80)))),
        "private_system_prompt": str(rules.get("private_system_prompt") or "").strip() or DEFAULT_PRIVATE_PERSONA_PROMPT,
        "group_system_prompt": str(rules.get("group_system_prompt") or "").strip(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return normalized

REQUIRED_BOOKING_SLOTS = ("cn", "character", "event_name", "date", "time")

# 客户已经预约完成的信号（用来触发「按会话找回订单」，而不是继续问信息）
RECAP_HINT_RE = re.compile(r"(约好|订好|拍好|填好|提交|搞定|搞完|弄完|做好了|约上|预约好|约完)")
# 客户私聊询问是否出席漫展 / 哪天去 / 接不接单的信号（防本地守门误拦截漏单）
EVENT_ATTENDANCE_RE = re.compile(
    r"(去不去|去吗|去嘛|会去吗|去哪天|哪几天去|来不来|参展|去展|漫展|几号去|哪天去|接不接|接吗|有空吗|有档期|在展|出展)",
    re.IGNORECASE,
)
SLOT_LABELS_ZH = {
    "cn": "CN（称呼）",
    "character": "拍摄角色/风格",
    "event_name": "漫展/活动名称",
    "date": "拍摄日期",
    "time": "具体拍摄时段",
}

GROUP_SCORING_SYSTEM_PROMPT = """你是 ZERO STUDIO Cosplay 摄影工作室的群聊拓客意向评估助手。
请分析一条来自二次元/漫展群聊的发言，判断发言人是否是正在寻找摄影师的潜在 Coser 客户。
注意排除：同行摄影师自己发接单广告、纯闲聊吐槽、买卖假发衣服、与约拍无关的日常聊天。
严格以 JSON 格式输出以下字段（不要输出 Markdown 代码块以外的多余文字）：
{
  "intent_score": 0到100的整数（80及以上代表明确在找摄影师约拍/场照/棚拍/外景）,
  "is_potential_client": true或false,
  "city": "城市，未知填空字符串",
  "event_name": "漫展或活动名称，未知填空字符串",
  "character": "角色名称，未知填空字符串",
  "shoot_time": "拍摄日期或时间，未知填空字符串",
  "reason": "简短判定依据",
  "add_friend_remark": "当 intent_score>=80 时生成一句自然、不生硬、不超过40字的加好友验证话术，提及对方漫展或角色"
}"""

PRIVATE_INTAKE_SYSTEM_PROMPT = """你是 ZERO STUDIO Cosplay 摄影工作室的私聊自动接单客服，语气热情、自然、二次元友好、简明高效。
你需要通过多轮对话，从客户消息中提取预约所需字段：
- cn: 客户称呼 / CN
- character: 拍摄角色或风格
- event_name: 漫展或企划活动名称
- date: 拍摄日期（尽量规范为 YYYY-MM-DD，若客户只说几月几日请保留或按上下文补全）
- time: 拍摄时段（如 10:00-12:00、14:00-16:00）
- remarks: 特殊要求或备注（可选，没有则为空字符串）

请结合「已收集字段」与「最新客户消息」，更新提取结果，并在仍有必填字段缺失时生成自然简短的追问话术（一次只追问缺项）。
严格输出 JSON：
{
  "extracted": {
    "cn": "",
    "character": "",
    "event_name": "",
    "date": "",
    "time": "",
    "remarks": ""
  },
  "reply": "给客户的简短回复"
}"""


LOCAL_GATEKEEPER_PROMPT = """你是二次元 Cosplay 摄影师「庄泽 / 零柒」的本地前置守门员。
你的唯一任务：判断对方发来的最新消息（结合简短上下文），是否与「摄影约拍业务」有关。
有关的情况包括但不限于：
- 咨询约拍、场照、合成、互勉、价格、档期、漫展（BACC/CP/NE等）、返图、选图、器材、角色/Cos、预约网站操作
- 正在继续上一轮约拍对话（例如只回复了“27号”、“下午”、“初音未来”、“ok”、“可以”、“2张”等）
无关的情况：
- 亲友日常闲聊、吃饭游戏吐槽、群发祝福、广告推销、与摄影完全无关的私事

请严格只输出一个词：YES 或 NO（不要输出其他解释）。"""


class LocalGatekeeperClient:
    """
    LM Studio 本地小模型前置守门员（如 DeepSeek-R1-Distill-Qwen-1.5B/7B、Qwen2.5-3B）。
    用于 0 成本过滤日常闲聊：
    - 返回 False -> 直接静默拦截，不消耗 DeepSeek 云端 API Token；
    - 返回 True  -> 放行交给 DeepSeek 云端 API 生成高质量真人回复；
    - 返回 None  -> LM Studio 未启动或超时，自动无缝降级回 DeepSeek 云端判断，绝不漏单。
    """

    def __init__(
        self,
        enabled: Optional[bool] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 15.0,
    ):
        env_enabled = os.getenv("LOCAL_LLM_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
        self.enabled = env_enabled if enabled is None else bool(enabled)
        self.base_url = (base_url or os.getenv("LOCAL_LLM_BASE_URL") or "http://127.0.0.1:1234/v1").strip().rstrip("/")
        self.model = (model or os.getenv("LOCAL_LLM_MODEL") or "auto").strip()
        self.timeout = timeout
        self._resolved_model: Optional[str] = None

    async def _resolve_model(self, client: httpx.AsyncClient) -> str:
        if self.model and self.model.lower() != "auto":
            return self.model
        if self._resolved_model:
            return self._resolved_model
        resp = await client.get(f"{self.base_url}/models")
        resp.raise_for_status()
        data = resp.json().get("data") or []
        if data and isinstance(data[0], dict) and data[0].get("id"):
            self._resolved_model = str(data[0]["id"])
            return self._resolved_model
        return "local-model"

    @staticmethod
    def _strip_think(text: str) -> str:
        """剥离 DeepSeek-R1 系列模型的 <think>...</think> 思考过程，只留最终结论。"""
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        if "</think>" in cleaned.lower():
            cleaned = re.split(r"</think>", cleaned, flags=re.IGNORECASE)[-1]
        return cleaned.strip()

    async def is_photography_related(
        self,
        latest_message: str,
        recent_history: List[Dict[str, Any]],
        has_active_booking: bool = False,
        extra_keywords: Optional[List[str]] = None,
    ) -> Optional[bool]:
        if not self.enabled:
            return None

        # 0ms 强相关关键词直通车：命中明显约拍/档期/漫展/报价词直接放行，绝不被本地小模型误杀
        fast_pass_words = {
            "约拍", "接单", "接拍", "场照", "合成", "互勉", "摄影", "漫展",
            "bacc", "cp", "郑州ne", "返图", "选图", "档期", "预约",
            "多少钱", "价格", "报价", "几张", "拍吗", "找摄", "蹲摄", "正片",
        }
        if extra_keywords:
            fast_pass_words.update(str(k).strip().lower() for k in extra_keywords if str(k).strip())
        low_msg = (latest_message or "").lower()
        if any(w in low_msg for w in fast_pass_words):
            return True

        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                model_id = await self._resolve_model(client)
                ctx_lines = []
                for h in recent_history[-4:]:
                    role = "客户" if h.get("role") == "user" else "摄影师"
                    ctx_lines.append(f"{role}: {h.get('text', '')}")
                user_text = (
                    f"是否已有进行中的约拍登记: {'是' if has_active_booking else '否'}\n"
                    f"最近对话:\n" + ("\n".join(ctx_lines) if ctx_lines else "无") + "\n"
                    f"最新客户消息: {latest_message}\n"
                    f"请判断最新消息是否与摄影约拍相关（仅输出 YES 或 NO）："
                )
                payload = {
                    "model": model_id,
                    "temperature": 0.0,
                    "max_tokens": 512,
                    "messages": [
                        {"role": "system", "content": LOCAL_GATEKEEPER_PROMPT},
                        {"role": "user", "content": user_text},
                    ],
                }
                resp = await client.post(f"{self.base_url}/chat/completions", json=payload)
                resp.raise_for_status()
                raw = (resp.json()["choices"][0]["message"]["content"] or "").strip()
                verdict = self._strip_think(raw).upper()
                if "NO" in verdict and "YES" not in verdict:
                    return False
                if "YES" in verdict:
                    return True
                # 兜底：若模型输出含糊，放行给云端 DeepSeek 二次判断，宁可多花极少 Token 也绝不漏单
                logger.info("LM Studio 本地模型返回模糊结果 (%s)，放行给云端 DeepSeek 判断", verdict[:40])
                return True
        except Exception as exc:
            logger.warning("LM Studio 本地守门员不可达/超时 (%s)，自动降级由云端 DeepSeek 直接判断", exc)
            self._resolved_model = None
            return None


class DeepSeekLLMClient:
    """封装 OpenAI 兼容客户端调用 DeepSeek (deepseek-chat) JSON Mode。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = "deepseek-chat",
    ):
        self.api_key = (api_key if api_key is not None else os.getenv("DEEPSEEK_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip()
        self.model = model
        self._client: Optional[AsyncOpenAI] = None

    def _get_client(self) -> AsyncOpenAI:
        if not self.api_key:
            self.api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法调用 DeepSeek API（请到中控台「分组配置中心 → AI 模型与生图接口」填写并保存）")
            self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    async def chat_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        client = self._get_client()
        resp = await client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = (resp.choices[0].message.content or "{}").strip()
        return json.loads(content)


class GRSLLMClient:
    """GRS 中转站 OpenAI 兼容客户端（Gemini / GPT 等模型）。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = "gpt-6.1-sol",
    ):
        self.api_key = (api_key if api_key is not None else os.getenv("GRS_API_KEY", os.getenv("VISION_API_KEY", ""))).strip()
        self.base_url = (base_url or os.getenv("GRS_BASE_URL") or "https://grsaiapi.com/v1").strip().rstrip("/")
        self.model = model
        self._client: Optional[AsyncOpenAI] = None

    def _get_client(self) -> AsyncOpenAI:
        if not self.api_key:
            self.api_key = os.getenv("GRS_API_KEY", os.getenv("VISION_API_KEY", "")).strip()
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("未配置 GRS_API_KEY / VISION_API_KEY")
            self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    async def chat_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        client = self._get_client()
        resp = await client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = (resp.choices[0].message.content or "{}").strip()
        return json.loads(content)


class FallbackLLMClient:
    """三级故障转移 LLM 客户端：DeepSeek → GRS → LM Studio。"""

    def __init__(self):
        self._clients: List[Tuple[str, Any]] = []
        # 1. DeepSeek（主）
        try:
            self._clients.append(("deepseek", DeepSeekLLMClient()))
        except Exception:
            pass
        # 2. GRS 中转站（第二备用）
        try:
            self._clients.append(("grs", GRSLLMClient()))
        except Exception:
            pass
        # 3. LM Studio（第三备用，需启用 LOCAL_LLM_ENABLED）
        local = LocalGatekeeperClient()
        if local.enabled:
            self._clients.append(("lmstudio", _LMStudioChatAdapter(local)))

        if not self._clients:
            logger.warning("FallbackLLMClient: 无任何可用 LLM 客户端")

    async def chat_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        last_exc: Optional[Exception] = None
        for name, client in self._clients:
            try:
                return await client.chat_json(system_prompt, user_prompt)
            except Exception as exc:
                last_exc = exc
                logger.warning("LLM %s 调用失败，尝试下一备用: %s", name, exc)
                continue
        raise RuntimeError(f"所有 LLM 备用均失败: {last_exc}")


class _LMStudioChatAdapter:
    """把 LocalGatekeeperClient 包装出 chat_json 接口，用于第三级备用。"""

    def __init__(self, gatekeeper: LocalGatekeeperClient):
        self._gk = gatekeeper

    async def chat_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._gk.timeout, trust_env=False) as client:
            model_id = await self._gk._resolve_model(client)
            payload = {
                "model": model_id,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            resp = await client.post(f"{self._gk.base_url}/chat/completions", json=payload)
            resp.raise_for_status()
            raw = (resp.json()["choices"][0]["message"]["content"] or "{}").strip()
            return json.loads(raw)


class StudioBrain:
    """
    双模式核心决策大脑：
    1. 群聊拓客漏斗：正则快筛 -> DeepSeek JSON 意向评分 -> 高意向 (>=80) 自动入防封冷却队列；
    2. 私聊自动接单：多轮收集 5 项预约信息 -> 调 booking-site 写库 -> 拉取场记二维码 PNG 直接下发客户。
    """

    def __init__(
        self,
        rate_limiter: FriendRequestRateLimiter,
        adapters: Dict[str, IMAdapter],
        llm_client: Optional[Any] = None,
        booking_api_url: Optional[str] = None,
        internal_api_key: Optional[str] = None,
        intent_threshold: Optional[int] = None,
        rules_path: Optional[Path] = None,
        conversation_store: Optional[ConversationStore] = None,
    ):
        self.rate_limiter = rate_limiter
        self.adapters = adapters
        self.llm = llm_client or FallbackLLMClient()
        self.local_gatekeeper = LocalGatekeeperClient()
        self.booking_api_url = (
            booking_api_url or os.getenv("BOOKING_API_URL") or "http://127.0.0.1:3000"
        ).rstrip("/")
        self.internal_api_key = (
            internal_api_key if internal_api_key is not None else os.getenv("INTERNAL_API_KEY", "")
        ).strip()
        self._override_threshold = int(intent_threshold) if intent_threshold is not None else None
        self.rules_path = Path(rules_path or DEFAULT_IM_RULES_PATH)

        # 私聊多轮接单状态机（key = (platform, user_id)）；SQLite 为跨重启权威数据源
        self.sessions: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.conversation_store = conversation_store or ConversationStore()
        self._session_locks: Dict[Tuple[str, str], asyncio.Lock] = {}

    def get_active_rules(self) -> Dict[str, Any]:
        rules = load_im_rules(self.rules_path)
        if self._override_threshold is not None:
            rules["intent_threshold"] = self._override_threshold
        return rules

    @staticmethod
    def _message_identity(msg: IncomingMessage) -> Tuple[str, int]:
        raw = msg.raw or {}
        source_id = ""
        for field in ("event_id", "message_id", "msgid", "msg_id"):
            if raw.get(field) not in (None, ""):
                source_id = str(raw[field])
                break
        if not source_id and raw.get("message_keys"):
            keys = list(raw["message_keys"])
            source_id = str(keys[-1])
        stable = bool(source_id)
        if not source_id:
            source_id = json.dumps(
                {"text": (msg.text or "").strip(), "images": list(msg.images or [])},
                ensure_ascii=False,
                sort_keys=True,
            )
        chat_id = str(msg.group_id or "") if msg.chat_type == "group" else msg.user_id
        identity = "|".join((msg.platform, msg.chat_type, chat_id, "id" if stable else "content", source_id))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        # 有平台消息 ID 时长期防重；纯内容兜底仅短期，允许客户以后再次发送相同文字。
        return digest, (30 * 86400 if stable else 600)

    async def handle_message(self, msg: IncomingMessage) -> Dict[str, Any]:
        if not (msg.text or "").strip() and not msg.images:
            return {"action": "IGNORED_EMPTY"}
        self.conversation_store.maybe_cleanup()
        chat_id = str(msg.group_id or "") if msg.chat_type == "group" else msg.user_id
        dedupe_key, dedupe_ttl = self._message_identity(msg)
        if not self.conversation_store.claim_message(dedupe_key, msg.platform, chat_id, dedupe_ttl):
            logger.info("重复入站消息已跳过 [%s/%s] key=%s", msg.platform, chat_id, dedupe_key[:12])
            return {"action": "IGNORED_DUPLICATE", "user_id": msg.user_id}
        incoming_id = self.conversation_store.record_incoming(
            platform=msg.platform,
            chat_type=msg.chat_type,
            group_id=msg.group_id,
            user_id=msg.user_id,
            nickname=msg.nickname,
            text=(msg.text or "").strip(),
            images=list(msg.images or []),
            dedupe_key=dedupe_key,
        )
        try:
            if msg.chat_type == "group":
                result = await self.handle_group_message(msg)
            else:
                result = await self.handle_private_message(msg)
        except Exception as exc:
            self.conversation_store.record_failure(incoming_id, exc)
            raise
        self.conversation_store.record_result(incoming_id, result)
        return result

    # ==================== 模式 1：群聊拓客漏斗 ====================

    async def handle_group_message(self, msg: IncomingMessage) -> Dict[str, Any]:
        text = (msg.text or "").strip()
        if not text:
            return {"action": "IGNORED_EMPTY"}

        # 热读取 configs/im_rules.json（群白名单、关键词与意向分阈值）
        rules = self.get_active_rules()
        if msg.platform == "qq" and rules["qq_groups"]:
            if str(msg.group_id or "").strip() not in rules["qq_groups"]:
                return {
                    "action": "IGNORED_GROUP_NOT_WHITELISTED",
                    "platform": msg.platform,
                    "group_id": msg.group_id,
                }
        elif msg.platform == "wechat" and rules["wechat_groups"]:
            if str(msg.group_id or "").strip() not in rules["wechat_groups"]:
                return {
                    "action": "IGNORED_GROUP_NOT_WHITELISTED",
                    "platform": msg.platform,
                    "group_id": msg.group_id,
                }

        # 第一关：本地关键词正则快筛（零 Token 过滤日常闲聊）
        kw_list = rules.get("keywords") or DEFAULT_KEYWORDS
        kw_regex = re.compile("(" + "|".join(re.escape(k) for k in kw_list) + ")", re.IGNORECASE)
        if not kw_regex.search(text):
            return {"action": "IGNORED_NO_KEYWORD", "user_id": msg.user_id, "text": text}

        threshold = int(rules.get("intent_threshold", 80))

        # 第二关：DeepSeek JSON Mode 意向评分与实体抽取
        user_prompt = json.dumps(
            {
                "platform": msg.platform,
                "group_id": msg.group_id,
                "nickname": msg.nickname,
                "message": text,
            },
            ensure_ascii=False,
        )
        group_sys_prompt = rules.get("group_system_prompt") or GROUP_SCORING_SYSTEM_PROMPT
        assessment = await self.llm.chat_json(group_sys_prompt, user_prompt)
        score = int(assessment.get("intent_score", 0))
        is_potential = bool(assessment.get("is_potential_client", score >= threshold))

        if not is_potential or score < threshold:
            return {
                "action": "IGNORED_LOW_INTENT",
                "intent_score": score,
                "threshold": threshold,
                "assessment": assessment,
            }

        # 第三关：生成定制验证话术并推入防封冷却队列
        remark = str(assessment.get("add_friend_remark") or "").strip()
        if not remark:
            ev = assessment.get("event_name") or "漫展"
            ch = assessment.get("character") or "Cos"
            remark = f"你好呀！在群里看到你{ev}出{ch}找摄影，这边拍场照/正片，扩列约拍吗~"

        queue_res = self.rate_limiter.enqueue_friend_request(
            platform=msg.platform,
            user_id=msg.user_id,
            group_id=msg.group_id,
            nickname=msg.nickname,
            remark=remark,
            intent_score=score,
        )

        action = "ENQUEUED_ADD_FRIEND" if queue_res.get("enqueued") else f"BLOCKED_{queue_res.get('reason', 'RATE_LIMIT')}"
        return {
            "action": action,
            "intent_score": score,
            "assessment": assessment,
            "remark": remark,
            "queue": queue_res,
        }

    async def _fetch_active_events(self) -> List[Dict[str, Any]]:
        """拉取官网当前接单中的漫展排期与日期（60 秒内存缓存），用于私聊自动回答「去不去某漫展/哪几天去」。"""
        import time as _t
        cache = getattr(self, "_events_cache", None)
        if cache and (_t.time() - cache[0]) < 60.0:
            return cache[1]
        events_out: List[Dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=6.0, trust_env=False) as client:
                r = await client.get(f"{self.booking_api_url}/api/portal_config")
                if r.status_code == 200:
                    data = r.json() or {}
                    for ev in (data.get("events") or []):
                        if not isinstance(ev, dict):
                            continue
                        status = str(ev.get("status") or "").strip().lower()
                        if status in ("archived", "closed", "ended", "已归档", "已结束"):
                            continue
                        events_out.append({
                            "name": str(ev.get("name") or "").strip(),
                            "dates": ev.get("dates") or [],
                            "start_date": str(ev.get("start_date") or "").strip(),
                            "end_date": str(ev.get("end_date") or "").strip(),
                            "status": status or "open",
                            "venue": str(ev.get("venue") or "").strip(),
                        })
        except Exception as exc:  # noqa: BLE001
            logger.warning("拉取漫展排期失败（不影响对话）: %s", exc)
        self._events_cache = (_t.time(), events_out)
        return events_out

    async def _lookup_booking_by_session(self, platform: str, user_id: str) -> Optional[Dict[str, Any]]:
        """按 IM 会话查客户已在官网下的最新订单（扫图/说约好了时调用）。"""
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                r = await client.get(
                    f"{self.booking_api_url}/api/booking-lookup",
                    params={"platform": platform, "user_id": user_id},
                )
                if r.status_code != 200:
                    return None
                data = r.json()
                return data.get("booking") if data.get("found") else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询客户已有预约失败: %s", exc)
            return None

    async def _lookup_booking_by_images(self, image_refs: List[str]) -> Optional[Dict[str, Any]]:
        """直接从客户发来的截图里解出预约二维码，再查订单。

        比「按会话找」更准：不管客户用哪个微信号/QQ 在网站下的单，
        只要截图里有二维码就能认出来。
        """
        try:
            decoded = await decode_booking_from_images(image_refs)
            if not decoded:
                return None
            token = decoded.get("token")
            order_id = decoded.get("order_id")

            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                if token:
                    r = await client.get(f"{self.booking_api_url}/api/booking-info/{token}")
                    if r.status_code == 200:
                        data = r.json()
                        booking = dict(data.get("booking") or {})
                        booking.setdefault("gallery_token", token)
                        logger.info("扫码命中订单 #%s (%s)", booking.get("id"), token)
                        return booking
                if order_id is not None:
                    r2 = await client.get(
                        f"{self.booking_api_url}/api/internal/bookings/{order_id}",
                        headers={"X-API-Key": self.internal_api_key},
                    )
                    if r2.status_code == 200:
                        return r2.json().get("booking")
        except Exception as exc:  # noqa: BLE001
            logger.warning("按二维码查订单失败: %s", exc)
        return None

    @staticmethod
    def _fill_slots_from_booking(slots: Dict[str, str], booking: Dict[str, Any]) -> None:
        """把已有订单信息回填进槽位，只填空缺，不覆盖客户刚说的话。"""
        mapping = {
            "cn": "cn",
            "character": "character",
            "event_name": "event_name",
            "date": "date",
            "time": "time",
            "remarks": "remarks",
        }
        for slot_key, booking_key in mapping.items():
            val = booking.get(booking_key)
            if val and not slots.get(slot_key):
                slots[slot_key] = str(val).strip()

    # ==================== 模式 2：私聊多轮自动接单 + 下发场记二维码 ====================

    async def handle_private_message(self, msg: IncomingMessage) -> Dict[str, Any]:
        key = (msg.platform, msg.user_id)
        lock = self._session_locks.setdefault(key, asyncio.Lock())
        async with lock:
            self.sessions[key] = self.conversation_store.load_session(
                msg.platform, msg.user_id, msg.nickname or msg.user_id
            )
            try:
                return await self._handle_private_message(msg)
            finally:
                if key in self.sessions:
                    self.conversation_store.save_session(
                        msg.platform, msg.user_id, msg.nickname or msg.user_id, self.sessions[key]
                    )
                else:
                    self.conversation_store.clear_session(msg.platform, msg.user_id)

    async def _handle_private_message(self, msg: IncomingMessage) -> Dict[str, Any]:
        key = (msg.platform, msg.user_id)
        session = self.sessions[key]
        slots: Dict[str, str] = session["slots"]
        text_in = (msg.text or "").strip()
        images = list(getattr(msg, "images", None) or [])
        session["history"].append({"role": "user", "text": text_in or ("[图片]" if images else "")})

        # 客户发来图片（多半是官网预约成功截图/二维码）或说「约好了」时，
        # 先按会话去预约库里把她已下的单捞回来，直接回填，绝不再从头问一遍。
        booking_hit = None
        if images or RECAP_HINT_RE.search(text_in) or "约好" in text_in:
            # 优先从截图里的二维码直接解出订单（最准，不依赖客户用哪个账号下单）
            if images:
                booking_hit = await self._lookup_booking_by_images(images)
            # 解不出来再退回「按会话找」
            if not booking_hit:
                booking_hit = await self._lookup_booking_by_session(msg.platform, msg.user_id)
            if booking_hit:
                self._fill_slots_from_booking(slots, booking_hit)
                session["booking"] = booking_hit
                logger.info(
                    "按会话找回已有预约 #%s，回填槽位 %s", booking_hit.get("id"), slots
                )

        # 拉取摄影师当前正在接单的漫展排期与具体日期，供 AI 准确回答「去不去 / 哪几天去」
        active_events = await self._fetch_active_events()
        from datetime import datetime as _dt
        today_str = _dt.now().strftime("%Y-%m-%d")

        # 告诉模型「哪些已经登记过」，避免它反复索要已有信息
        missing = [f for f in REQUIRED_BOOKING_SLOTS if not slots.get(f)]
        known = {f: slots.get(f) for f in REQUIRED_BOOKING_SLOTS if slots.get(f)}

        user_prompt = json.dumps(
            {
                "today": today_str,
                "active_events": active_events,
                "contact_remark": session.get("auto_remark", ""),
                "memory_version": session.get("_version", 0),
                "memory_scope": f"{msg.platform}:{msg.user_id}",
                "current_slots": slots,
                "known_fields": known,
                "missing_fields": missing,
                "next_field_to_ask": missing[0] if missing else "",
                "already_booked_on_site": bool(booking_hit),
                "existing_booking": booking_hit,
                "has_images": bool(images),
                "image_count": len(images),
                "recent_history": session["history"][-8:],
                "latest_message": text_in,
                "nickname": msg.nickname,
            },
            ensure_ascii=False,
        )
        rules = self.get_active_rules()
        only_related = bool(rules.get("private_only_reply_related", True))

        # 判断是否提及「去不去漫展 / 档期 / 接单中漫展名」，命中则强制跳过本地小模型拦截，绝不漏单
        mentions_active_event = any(
            ev.get("name") and ev["name"].lower() in text_in.lower()
            for ev in active_events
        )
        is_attendance_inquiry = bool(text_in) and (bool(EVENT_ATTENDANCE_RE.search(text_in)) or mentions_active_event)

        # 纯图片消息或漫展档期/出席询问：本地守门直接放行，不交给小模型误杀
        skip_local_gate = (bool(images) and not text_in) or is_attendance_inquiry

        # 第一关（可选 0 成本本地守门）：若启用了 Mac LM Studio，先由本地小模型判断是否需要回复
        if only_related and self.local_gatekeeper.enabled and not skip_local_gate:
            has_active = any(bool(slots.get(k)) for k in REQUIRED_BOOKING_SLOTS)
            local_verdict = await self.local_gatekeeper.is_photography_related(
                latest_message=text_in,
                recent_history=session["history"][:-1],
                has_active_booking=has_active,
                extra_keywords=rules.get("keywords"),
            )
            if local_verdict is False:
                logger.info("LM Studio 本地模型拦截无关私聊（0云端Token）[%s:%s]: %s", msg.platform, msg.user_id, text_in[:40])
                return {
                    "action": "IGNORED_UNRELATED_BY_LOCAL_LLM",
                    "is_photography_related": False,
                    "reason": "LM Studio 本地小模型判定与摄影约拍无关，已静默拦截",
                }

        custom_priv = (rules.get("private_system_prompt") or "").strip() or DEFAULT_PRIVATE_PERSONA_PROMPT
        effective_priv_prompt = custom_priv if '"is_photography_related"' in custom_priv else (custom_priv + PRIVATE_JSON_CONTRACT_SUFFIX)
        llm_res = await self.llm.chat_json(effective_priv_prompt, user_prompt)

        # 第二关（云端兜底拦截）。注意：带图片的消息一律不静默，否则客户发了二维码却收不到任何回应
        is_related = llm_res.get("is_photography_related", True)
        if only_related and not images and (is_related is False or str(is_related).lower() == "false"):
            reason = str(llm_res.get("relevance_reason") or "日常闲聊/与摄影接单无关")
            logger.info("私聊消息与摄影接单无关，保持静默 [%s:%s]: %s (%s)", msg.platform, msg.user_id, text_in[:40], reason)
            return {
                "action": "IGNORED_UNRELATED_PRIVATE",
                "is_photography_related": False,
                "reason": reason,
            }

        extracted = llm_res.get("extracted") or {}

        for field_name in ("cn", "character", "event_name", "date", "time", "remarks"):
            val = extracted.get(field_name)
            if isinstance(val, str) and val.strip():
                slots[field_name] = val.strip()

        missing = [f for f in REQUIRED_BOOKING_SLOTS if not slots.get(f)]
        adapter = self.adapters.get(msg.platform)
        ask_counts: Dict[str, int] = session.setdefault("ask_counts", {})
        MAX_ASK_PER_FIELD = 2

        # 信息未齐 -> 真人短句分条气泡追问缺项
        if missing:
            raw_replies = llm_res.get("replies")
            bubbles: List[str] = []
            if isinstance(raw_replies, list):
                for item in raw_replies:
                    for seg in str(item or "").splitlines():
                        s = seg.strip()
                        if s:
                            bubbles.append(s)
            if not bubbles:
                reply_text = str(llm_res.get("reply") or "").strip()
                if reply_text:
                    bubbles = [seg.strip() for seg in reply_text.splitlines() if seg.strip()]

            # 只追问「下一个真正缺的字段」，且同一字段最多追问 2 次，杜绝无限复读
            target = missing[0]
            target_count = ask_counts.get(target, 0)
            if not bubbles:
                if target_count < MAX_ASK_PER_FIELD:
                    bubbles = ["ok", f"老大说下{SLOT_LABELS_ZH[target]}"]
                else:
                    # 已经问过两次还拿不到，就让客户直接去网站自助填，别继续纠缠
                    bubbles = ["老大直接网站上填吧", "填完把码发我就行"]

            # 防复读：同一字段/同一句话只保留一次，避免连发两条「老大怎么称呼」
            if any(SLOT_LABELS_ZH[target] in b for b in bubbles):
                deduped: List[str] = []
                for b in bubbles:
                    if SLOT_LABELS_ZH[target] in b and any(SLOT_LABELS_ZH[target] in d for d in deduped):
                        continue
                    deduped.append(b)
                bubbles = deduped
            # 完全相同的重复气泡也去掉
            bubbles = list(dict.fromkeys(bubbles))

            # 刚找回的订单信息已经齐了，就不该再进追问分支
            if target_count < MAX_ASK_PER_FIELD:
                ask_counts[target] = target_count + 1

            # 最多连发 3 条气泡，防止刷屏
            bubbles = bubbles[:3]
            joined_reply = "\n".join(bubbles)
            session["history"].append({"role": "assistant", "text": joined_reply})
            if adapter is not None:
                for idx, b in enumerate(bubbles):
                    await adapter.send_text(msg.user_id, b, chat_type="private")
                    if idx < len(bubbles) - 1:
                        await asyncio.sleep(random.uniform(0.45, 0.95))
            return {
                "action": "ASK_MISSING_SLOTS",
                "missing": missing,
                "asked_field": target,
                "asked_count": ask_counts.get(target, 0),
                "slots": dict(slots),
                "replies": bubbles,
                "reply": joined_reply,
            }

        # 客户本来就在官网约好了：直接确认，绝不重复下单
        if booking_hit:
            confirm = [
                "ok收到",
                f"{booking_hit.get('date') or ''} {booking_hit.get('event_name') or ''}".strip(),
                f"{booking_hit.get('character') or ''} 我记下了",
            ]
            confirm = [c for c in confirm if c.strip()]
            joined = "\n".join(confirm)
            session["history"].append({"role": "assistant", "text": joined})
            if adapter is not None:
                for idx, b in enumerate(confirm):
                    await adapter.send_text(msg.user_id, b, chat_type="private")
                    if idx < len(confirm) - 1:
                        await asyncio.sleep(random.uniform(0.45, 0.9))
            return {
                "action": "CONFIRMED_EXISTING_BOOKING",
                "booking_id": booking_hit.get("id"),
                "slots": dict(slots),
                "replies": confirm,
                "reply": joined,
            }

        # 信息齐全 -> 调用 booking-site 写入订单并拉取场记二维码 PNG 直接下发
        booking_payload: Dict[str, Any] = {
            "platform": msg.platform,
            "im_user_id": msg.user_id,
            "qq": msg.user_id if msg.platform == "qq" else None,
            "wechat": msg.user_id if msg.platform in ("wechat", "wecom_kf") else "",
            "cn": slots["cn"],
            "character": slots["character"],
            "event_name": slots["event_name"],
            "date": slots["date"],
            "time": slots["time"],
            "remarks": slots.get("remarks") or "",
            "deposit": "待付定金",
            "quantity": 1,
        }

        headers = {"X-API-Key": self.internal_api_key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            book_resp = await client.post(
                f"{self.booking_api_url}/api/internal/bookings",
                headers=headers,
                json=booking_payload,
            )

            if book_resp.status_code == 409:
                conflict_bubbles = [
                    "不好意思老大",
                    f"{slots['date']} {slots['time']} 这个点满了",
                    "换个时间行吗",
                ]
                conflict_msg = "\n".join(conflict_bubbles)
                slots["time"] = ""
                if adapter is not None:
                    for idx, b in enumerate(conflict_bubbles):
                        await adapter.send_text(msg.user_id, b, chat_type="private")
                        if idx < len(conflict_bubbles) - 1:
                            await asyncio.sleep(random.uniform(0.45, 0.85))
                return {"action": "BOOKING_SLOT_CONFLICT", "reply": conflict_msg, "slots": dict(slots)}

            if book_resp.status_code not in (200, 201):
                raise RuntimeError(f"调用 booking-site 写单失败 (HTTP {book_resp.status_code}): {book_resp.text}")

            book_data = book_resp.json()
            booking_id = int(book_data["booking_id"])
            gallery_token = str(book_data["gallery_token"])

            # 拉取场记二维码 PNG
            qr_resp = await client.get(
                f"{self.booking_api_url}/api/internal/bookings/{booking_id}/slate-qr",
                headers={"X-API-Key": self.internal_api_key},
            )
            if qr_resp.status_code != 200:
                raise RuntimeError(f"拉取场记二维码失败 (HTTP {qr_resp.status_code}): {qr_resp.text}")
            qr_png_bytes = qr_resp.content

        confirm_text = (
            f"🎉 档期已成功锁定！（订单号 #{booking_id}）\n"
            f"• CN：{slots['cn']}\n"
            f"• 角色：{slots['character']}\n"
            f"• 活动：{slots['event_name']}\n"
            f"• 时间：{slots['date']} {slots['time']}\n"
            "📸 下方是你的专属【场记二维码】，漫展见面开拍前请出示给摄影师先拍 1 张，系统会自动归档并推送你的专属选图链接！"
        )

        if adapter is not None:
            await adapter.send_text(msg.user_id, confirm_text, chat_type="private")
            await adapter.send_image(msg.user_id, qr_png_bytes, chat_type="private")

        completed_slots = dict(slots)
        self.sessions.pop(key, None)

        return {
            "action": "BOOKING_CREATED_AND_QR_SENT",
            "booking_id": booking_id,
            "gallery_token": gallery_token,
            "qr_size_bytes": len(qr_png_bytes),
            "slots": completed_slots,
            "reply": confirm_text,
        }
