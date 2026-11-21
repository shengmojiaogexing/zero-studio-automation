const express = require('express');
const sqlite3 = require('sqlite3').verbose();
const multer = require('multer');
const path = require('path');
const auth = require('express-basic-auth');
const fs = require('fs');
const https = require('https');
const crypto = require('crypto');
const QRCode = require('qrcode');
const { v4: uuidv4 } = require('uuid');

const app = express();
const PORT = process.env.PORT || 3000;
const DB_PATH = process.env.DB_PATH || './data.db';
const PREVIEW_OUTPUT_DIR = process.env.PREVIEW_OUTPUT_DIR || path.join(__dirname, 'uploads', 'previews');
const RETOUCH_OUTPUT_DIR = process.env.RETOUCH_OUTPUT_DIR || path.join(__dirname, 'uploads', 'outputs');
const IM_GATEWAY_URL = (process.env.IM_GATEWAY_URL || 'http://127.0.0.1:8090').replace(/\/+$/, '');
const ADMIN_USER = process.env.ADMIN_USER || 'admin';
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || 'change-me';
const PUSHPLUS_TOKEN = process.env.PUSHPLUS_TOKEN || '';

const adminAuth = auth({ users: { [ADMIN_USER]: ADMIN_PASSWORD }, challenge: true, realm: 'ZERO STUDIO ADMIN' });

// 注意：PS 桥接路由必须在任何 catch-all / 前端路由之前注册，否则插件的轮询会被 404 吞掉。




const ALLOWED_BOOKING_STATUSES = new Set([
    'BOOKED',
    'SHOT',
    'CLUSTERED',
    'SELECTED',
    'RETOUCHING',
    'REVIEW_PENDING',
    'DELIVERED'
]);

const LOCKED_GALLERY_STATUSES = new Set([
    'SELECTED',
    'RETOUCHING',
    'REVIEW_PENDING',
    'DELIVERED'
]);

const GALLERY_PRESETS = [
    {
        code: 'cyan_air_event',
        name: '苍蓝通透场照',
        desc: '清冷空气感肤色，高通透二次元漫展场照质感'
    },
    {
        code: 'warm_film_daily',
        name: '暖调胶片日常',
        desc: '复古日系暖调，柔和胶片颗粒与温柔高光过渡'
    },
    {
        code: 'high_contrast_studio',
        name: '高反差棚拍',
        desc: '电影级强明暗对比，立体轮廓与深邃暗部氛围'
    },
    {
        code: 'cyber_studio',
        name: '赛博霓虹棚拍',
        desc: '冷暖撞色霓虹氛围与高立体度人物光影'
    }
];
const ALLOWED_PRESET_CODES = new Set(GALLERY_PRESETS.map(p => p.code));

// ==================== 首页套餐展示区（中控台可编辑） ====================
// id 与 index.html 中的四张卡片一一对应；中控台通过 id 覆盖任何字段。
const SERVICE_DEFAULTS = [
    {
        id: 'exhibition',
        badge: '热门',
        catalog: 'EXHIBITION',
        title: '漫展场照拍摄',
        subtitle: 'Exhibition',
        note: '',
        priceMode: 'dual',
        price: '',
        region_a: '沪广杭地区',
        price_a: '99',
        region_b: '石家庄地区',
        price_b: '25',
        unit: '/4张',
        features: ['漫展现场跟拍捕捉神态', '包含基础调色与面部精修', '出片率高，极速返图'],
        cta: '立即自助预约',
        accent: '#e2bac8',
        layout: 'wide',
        hidden: false
    },
    {
        id: 'visualfx',
        badge: 'POST-PROCESS',
        catalog: 'POST-PROCESS',
        title: '场照特效合成',
        subtitle: 'Visual FX',
        note: '',
        priceMode: 'single',
        price: '248',
        region_a: '',
        price_a: '',
        region_b: '',
        price_b: '',
        unit: '/张',
        features: ['深度抠图与二次元场景融合', '专属特效、光影与氛围重塑', '打破次元壁'],
        cta: '立即自助预约',
        accent: '#e2bac8',
        layout: 'tall',
        hidden: false
    },
    {
        id: 'outdoor',
        badge: 'OUTDOOR',
        catalog: 'OUTDOOR',
        title: '绝美外景拍摄',
        subtitle: 'Location',
        note: '',
        priceMode: 'single',
        price: '299',
        region_a: '',
        price_a: '',
        region_b: '',
        price_b: '',
        unit: '/套',
        features: ['自然光影捕捉与环境氛围融合', '深度构图设计，前期动作指导', '包含多张高规格氛围感成片'],
        cta: '立即自助预约',
        accent: '#6fa8a5',
        layout: 'tall',
        hidden: false
    },
    {
        id: 'studio',
        badge: '',
        catalog: 'STUDIO',
        title: '定制棚拍企划',
        subtitle: 'Studio Plan',
        note: '',
        priceMode: 'single',
        price: '299',
        region_a: '',
        price_a: '',
        region_b: '',
        price_b: '',
        unit: '/套',
        features: ['专业影棚灯光阵列布置与重塑', '高质感服装细节与特效光渲染', '极致后期处理与质感提升'],
        cta: '立即自助预约',
        accent: '#c4c698',
        layout: 'wide',
        hidden: false
    }
];

const SERVICE_FIELDS = new Set(Object.keys(SERVICE_DEFAULTS[0]));

// 合并默认值与中控台覆盖值：缺失字段回退默认，特征列表规范为字符串数组
function resolveServices(overrides) {
    const raw = Array.isArray(overrides) ? overrides : [];
    const byId = new Map();
    for (const item of raw) {
        if (item && typeof item === 'object' && typeof item.id === 'string') {
            byId.set(item.id, item);
        }
    }
    return SERVICE_DEFAULTS.map(def => {
        const merged = { ...def, ...(byId.get(def.id) || {}) };
        merged.id = def.id;
        if (Array.isArray(merged.features)) {
            merged.features = merged.features.map(f => String(f).trim()).filter(Boolean);
        } else if (typeof merged.features === 'string') {
            merged.features = merged.features.split('\n').map(f => f.trim()).filter(Boolean);
        } else {
            merged.features = def.features;
        }
        merged.hidden = merged.hidden === true || merged.hidden === 'true';
        merged.priceMode = merged.priceMode === 'dual' ? 'dual' : 'single';
        return merged;
    });
}

// ==================== 作品图库展示位 ====================
// 历史数据里中控台上传写入的是 'gallery'，而前台筛选的却是中文枚举，
// 导致上传成功却永远不显示。这里统一做一次枚举归一化。
const PHOTO_LOCATIONS = ['首页画廊', '个人档案', '双端展示'];
const LOCATION_ALIASES = new Map([
    ['gallery', '双端展示'],
    ['home', '首页画廊'],
    ['index', '首页画廊'],
    ['portfolio', '个人档案'],
    ['both', '双端展示'],
    ['all', '双端展示'],
    ['首页', '首页画廊'],
    ['作品页', '个人档案'],
    ['双端', '双端展示']
]);

function normalizeDisplayLocation(value, fallback = '双端展示') {
    const raw = String(value == null ? '' : value).trim();
    if (!raw) return fallback;
    if (PHOTO_LOCATIONS.includes(raw)) return raw;
    const alias = LOCATION_ALIASES.get(raw.toLowerCase());
    if (alias) return alias;
    return fallback;
}

function showsOnIndex(location) {
    return location === '首页画廊' || location === '双端展示';
}

function showsOnPortfolio(location) {
    return location === '个人档案' || location === '双端展示';
}

// 必须在 express.json 之前挂载：/api/worker/* 用 HMAC 验签，需要未解析的原始请求体
app.use((req, res, next) => {
    if (!req.path.startsWith('/api/worker/')) return next();
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
        req.rawBody = Buffer.concat(chunks).toString('utf8');
    });
    next();
});

app.use(express.json({ limit: '20mb' }));

// ==================== Photoshop 桥接任务队列 ====================
// UXP 平台不允许插件开本地服务端，所以反过来：PS 插件定时来拉任务。
// 这里维护一个内存队列，由 retouch-engine（或仿真实验室）投递、PS 插件消费。
//
// 鉴权：PS 跑在 Windows 上，经局域网访问本服务，因此不能复用 localhost-only 的
// requireInternalKey。这里用「共享密钥」：X-API-Key 或 X-PS-Bridge-Token 任一匹配
// INTERNAL_API_KEY / PS_UXP_BRIDGE_TOKEN 即放行，比较过程为定时安全比较。
const psBridgeTasks = new Map();   // task_id -> { task_id, type, payload, status, result, error, createdAt, updatedAt }
const psBridgeWaiters = new Map(); // task_id -> resolve 回调
let psBridgeSeq = 0;
const PS_BRIDGE_TASK_TTL_MS = 10 * 60 * 1000;
const PS_BRIDGE_POLL_STALE_MS = 15 * 1000;   // 插件报告「未在轮询」的判定阈值
const psBridgeAgent = {            // PS 插件最近一次轮询/回报的状态
    last_seen: 0,
    last_task_id: '',
    last_ok: null,
    last_error: '',
    handled: 0,
    server_base: ''
};

function psBridgeSweep() {
    const now = Date.now();
    for (const [id, t] of psBridgeTasks) {
        if (now - t.createdAt > PS_BRIDGE_TASK_TTL_MS) {
            psBridgeTasks.delete(id);
            const waiter = psBridgeWaiters.get(id);
            if (waiter) {
                psBridgeWaiters.delete(id);
                waiter({ ok: false, error: '任务超时（PS 未在 10 分钟内处理）' });
            }
            continue;
        }
        // 插件取走后崩溃/重装会留下 running 僵尸任务，超过 TTL 一半自动回收
        if (t.status === 'running' && now - t.updatedAt > PS_BRIDGE_TASK_TTL_MS / 2) {
            t.status = 'failed';
            t.error = t.error || '插件已领取但长时间无结果（PS 可能被关闭）';
            t.updatedAt = now;
        }
    }
}

function psBridgeCreateTask(type, payload) {
    psBridgeSweep();
    const id = `pstask_${Date.now()}_${++psBridgeSeq}`;
    const task = {
        task_id: id,
        id,
        type: type || 'retouch',
        payload,
        status: 'pending',
        result: null,
        error: null,
        createdAt: Date.now(),
        updatedAt: Date.now()
    };
    psBridgeTasks.set(id, task);
    return task;
}

function psBridgeWaitForResult(taskId, timeoutMs) {
    return new Promise((resolve) => {
        const timer = setTimeout(() => {
            psBridgeWaiters.delete(taskId);
            resolve({ ok: false, error: '等待 Photoshop 处理超时' });
        }, timeoutMs);

        psBridgeWaiters.set(taskId, (r) => {
            clearTimeout(timer);
            psBridgeWaiters.delete(taskId);
            resolve(r);
        });
    });
}

function psBridgeTimingSafeEqual(a, b) {
    const ba = Buffer.from(String(a || ''));
    const bb = Buffer.from(String(b || ''));
    return ba.length === bb.length && crypto.timingSafeEqual(ba, bb);
}

/** 校验 PS 插件携带的共享密钥（允许关闭，便于本机联调） */
function psBridgeAuthorized(req) {
    if (String(process.env.PS_BRIDGE_REQUIRE_KEY || 'true').toLowerCase() === 'false') return true;
    const provided = String(req.get('X-API-Key') || req.get('X-PS-Bridge-Token') || '').trim();
    if (!provided) return false;
    const accepted = [process.env.INTERNAL_API_KEY, process.env.PS_UXP_BRIDGE_TOKEN]
        .map(v => String(v || '').trim())
        .filter(Boolean);
    return accepted.some(v => psBridgeTimingSafeEqual(v, provided));
}

function psBridgeRequireKey(req, res, next) {
    if (psBridgeAuthorized(req)) return next();
    return res.status(401).json({ status: 'error', message: 'Unauthorized: invalid or missing X-API-Key' });
}

// PS 插件拉取待处理任务（没有任务立即返回 null，插件按 poll_interval_ms 再来）
// 兼容：task_id 与 id 同时返回；同时回带插件需要的轮询间隔，避免两端写死不一致
app.get('/api/ps-bridge/pending', psBridgeRequireKey, (req, res) => {
    try {
        psBridgeSweep();
        const reported = String(req.get('X-Bridge-Agent') || '').trim();
        if (reported) psBridgeAgent.server_base = reported;

        let picked = null;
        for (const t of psBridgeTasks.values()) {
            if (t.status === 'pending') {
                picked = t;
                break;
            }
        }

        psBridgeAgent.last_seen = Date.now();

        if (!picked) {
            return res.json({
                status: 'ok',
                task: null,
                poll_interval_ms: Number(process.env.PS_BRIDGE_POLL_INTERVAL_MS) || 2000
            });
        }

        picked.status = 'running';
        picked.updatedAt = Date.now();
        res.json({
            status: 'ok',
            task: { task_id: picked.task_id, id: picked.id, type: picked.type, payload: picked.payload },
            poll_interval_ms: Number(process.env.PS_BRIDGE_POLL_INTERVAL_MS) || 2000
        });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// PS 插件回传结果（接受 task_id 或 id，两种字段名都兼容旧插件）
app.post('/api/ps-bridge/result', psBridgeRequireKey, (req, res) => {
    try {
        const { ok, result, error } = req.body || {};
        const taskId = String((req.body && (req.body.task_id || req.body.id)) || '').trim();
        if (!taskId) return res.status(400).json({ status: 'error', message: 'task_id 必填' });

        const task = psBridgeTasks.get(taskId);
        if (!task) return res.status(404).json({ status: 'error', message: '任务不存在或已过期' });

        task.status = ok ? 'done' : 'failed';
        task.result = result || null;
        task.error = ok ? null : (error || 'Photoshop 处理失败');
        task.updatedAt = Date.now();

        psBridgeAgent.last_seen = Date.now();
        psBridgeAgent.last_task_id = taskId;
        psBridgeAgent.last_ok = Boolean(ok);
        psBridgeAgent.last_error = ok ? '' : String(task.error || '').slice(0, 500);
        if (ok) psBridgeAgent.handled += 1;

        const waiter = psBridgeWaiters.get(taskId);
        if (waiter) waiter({ ok: Boolean(ok), result, error: task.error });

        res.json({ status: 'ok' });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 内部接口：投递任务并等待结果（供 retouch-engine / 仿真实验室调用，仅本机 + X-API-Key）
// 支持的任务类型：
//   retouch        { tiff_path, output_psd_path, plan }          AI 调色 -> 调整层 PSD
//   export_srgb    { tiff_path, output_jpg_path, max_edge }     导出 sRGB 预览
//   capture_region { document_id?, selection?, max_edge? }      抓像素 -> PNG/JPEG base64
//   place_image    { image_base64|image_path, selection, ... }  贴图回选区
//   jsx            { jsx, document_id?, save_as_psd? }          执行任意 ExtendScript
const PS_BRIDGE_TASK_TYPES = new Set(['retouch', 'export_srgb', 'capture_region', 'place_image', 'jsx']);

// 各任务类型必填的绝对路径字段（PS 跑在 Windows 上，路径必须是 PS 机器上的绝对路径）
// 注意：place_image 的 image_path 是二选一（也可给 image_base64），故不列入必填
const PS_BRIDGE_PATH_FIELDS = {
    retouch: ['tiff_path'],
    export_srgb: ['tiff_path', 'output_jpg_path']
};

function isAbsolutePathAnyPlatform(value) {
    return /^(?:[A-Za-z]:[\\/]|\\\\|\/)/.test(String(value || ''));
}

app.post('/api/internal/ps-bridge/run', requireInternalKey, async (req, res) => {
    try {
        const { type = 'retouch', payload = {}, timeout_ms = 180000 } = req.body || {};
        if (!payload || typeof payload !== 'object') {
            return res.status(400).json({ status: 'error', message: 'payload 必须是对象' });
        }
        if (!PS_BRIDGE_TASK_TYPES.has(type)) {
            return res.status(400).json({
                status: 'error',
                message: `未知任务类型 ${type}，支持：${[...PS_BRIDGE_TASK_TYPES].join('/')}`
            });
        }

        // 按类型校验必填路径（只有给了的字段才校验，避免误伤可选字段）
        const requiredPaths = PS_BRIDGE_PATH_FIELDS[type] || [];
        for (const field of requiredPaths) {
            if (!isAbsolutePathAnyPlatform(payload[field])) {
                return res.status(400).json({
                    status: 'error',
                    message: `payload.${field} 必须是 PS 机器上的绝对路径`
                });
            }
        }

        // jsx 任务必须有脚本内容
        if (type === 'jsx' && !String(payload.jsx || '').trim()) {
            return res.status(400).json({ status: 'error', message: 'jsx 任务需要 payload.jsx 脚本内容' });
        }
        // place_image 必须给图源和目标选区
        if (type === 'place_image') {
            if (!payload.image_base64 && !payload.image_path) {
                return res.status(400).json({ status: 'error', message: 'place_image 需要 image_base64 或 image_path' });
            }
            if (!payload.selection || typeof payload.selection !== 'object') {
                return res.status(400).json({ status: 'error', message: 'place_image 需要 payload.selection 目标矩形' });
            }
        }

        const task = psBridgeCreateTask(type, payload);
        console.log(`[ps-bridge] 任务已投递: ${task.task_id} (${type})`);

        const outcome = await psBridgeWaitForResult(task.task_id, Number(timeout_ms) || 180000);
        if (outcome.ok) {
            return res.json({ status: 'ok', task_id: task.task_id, result: outcome.result });
        }
        return res.status(504).json({
            status: 'error',
            task_id: task.task_id,
            message: outcome.error || 'Photoshop 处理失败'
        });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 桥接状态（排查用；含插件是否在轮询）
app.get('/api/ps-bridge/status', (req, res) => {
    psBridgeSweep();
    const now = Date.now();
    const tasks = [...psBridgeTasks.values()].map(t => ({
        task_id: t.task_id,
        id: t.id,
        type: t.type,
        status: t.status,
        age_ms: now - t.createdAt,
        error: t.error
    }));
    const seenAgo = psBridgeAgent.last_seen ? now - psBridgeAgent.last_seen : null;

    let hint = '';
    if (!psBridgeAgent.last_seen) {
        hint = 'PS 插件从未连接：确认 Photoshop 已重启、面板已打开，且插件 zero-bridge-boot.js 已加载';
    } else if (seenAgo > PS_BRIDGE_POLL_STALE_MS) {
        hint = `PS 插件已 ${Math.round(seenAgo / 1000)} 秒未轮询：Photoshop 可能被关闭或面板已关闭`;
    }

    res.json({
        status: 'ok',
        pending: tasks.filter(t => t.status === 'pending').length,
        running: tasks.filter(t => t.status === 'running').length,
        plugin: {
            online: Boolean(psBridgeAgent.last_seen) && seenAgo <= PS_BRIDGE_POLL_STALE_MS,
            last_seen_ago_ms: seenAgo,
            last_task_id: psBridgeAgent.last_task_id,
            last_ok: psBridgeAgent.last_ok,
            last_error: psBridgeAgent.last_error,
            handled: psBridgeAgent.handled,
            server_base: psBridgeAgent.server_base,
            hint
        },
        tasks: tasks.slice(-20)
    });
});
app.use('/uploads', express.static(path.join(__dirname, 'uploads')));
app.use('/previews', express.static(PREVIEW_OUTPUT_DIR));
app.use('/outputs', express.static(RETOUCH_OUTPUT_DIR));
app.get('/review.html', adminAuth, (req, res) => res.sendFile(path.join(__dirname, 'review.html')));
app.get('/review', adminAuth, (req, res) => res.sendFile(path.join(__dirname, 'review.html')));
// ==================== 随身修图机（Windows）公网回传通道 ====================
//
// 背景：Mac 常驻做主站，Windows 是随身修图机，会被带到没有家庭局域网的地方。
// Windows 的修图 worker 需要跨公网从 Mac 领取任务、回传产物与修改意见。
//
// 安全边界（刻意与 /api/internal/* 隔离）：
//   - /api/internal/* 仍然「仅 127.0.0.1」，绝不暴露公网，权限覆盖订单/支付/管理
//   - 本组 /api/worker/* 只做修图相关：领任务、回传产物、领修改意见
//   - 独立密钥 WORKER_SYNC_TOKEN（不是 INTERNAL_API_KEY），泄露也不影响订单系统
//   - 时间戳 + HMAC 签名防重放，容忍 5 分钟时钟偏移
//   - 可选 IP 白名单 WORKER_ALLOWED_IPS（配置后只接受这些来源）
//
// 签名算法：
//   signPayload = `${timestamp}.${method}.${path}.${rawBody}`
//   signature   = HMAC-SHA256(signPayload, WORKER_SYNC_TOKEN) 的 hex
//   请求头：X-Worker-Timestamp、X-Worker-Signature
//
const WORKER_SYNC_TOKEN = (process.env.WORKER_SYNC_TOKEN || '').trim();
const WORKER_CLOCK_SKEW_SEC = 300;
const WORKER_ALLOWED_IPS = String(process.env.WORKER_ALLOWED_IPS || '')
    .split(',')
    .map(s => s.trim())
    .filter(Boolean);

function workerSignatureValid(req) {
    if (!WORKER_SYNC_TOKEN) return false;

    const timestamp = String(req.get('X-Worker-Timestamp') || '').trim();
    const signature = String(req.get('X-Worker-Signature') || '').trim().toLowerCase();
    if (!timestamp || !signature) return false;

    const ts = Number(timestamp);
    if (!Number.isFinite(ts)) return false;
    const nowSec = Math.floor(Date.now() / 1000);
    if (Math.abs(nowSec - ts) > WORKER_CLOCK_SKEW_SEC) return false;

    const rawBody = req.rawBody || '';
    const payload = `${timestamp}.${req.method}.${req.path}.${rawBody}`;
    const expected = crypto.createHmac('sha256', WORKER_SYNC_TOKEN).update(payload).digest('hex');

    const a = Buffer.from(expected);
    const b = Buffer.from(signature);
    return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function workerAllowedIp(req) {
    if (!WORKER_ALLOWED_IPS.length) return true;
    const ip = String(req.ip || req.socket.remoteAddress || '').replace(/^::ffff:/, '');
    return WORKER_ALLOWED_IPS.includes(ip);
}

const workerHeartbeat = {
    last_seen_ms: 0,
    last_ip: '',
    last_path: ''
};

/** worker 通道鉴权中间件 */
function requireWorkerAuth(req, res, next) {
    if (!WORKER_SYNC_TOKEN) {
        return res.status(503).json({
            status: 'error',
            message: '服务端未配置 WORKER_SYNC_TOKEN，随身修图机通道未启用'
        });
    }
    if (!workerAllowedIp(req)) {
        return res.status(403).json({ status: 'error', message: '来源 IP 不在白名单' });
    }
    if (!workerSignatureValid(req)) {
        return res.status(401).json({ status: 'error', message: '签名无效或已过期' });
    }
    workerHeartbeat.last_seen_ms = Date.now();
    workerHeartbeat.last_ip = req.ip || (req.socket && req.socket.remoteAddress) || '';
    workerHeartbeat.last_path = req.path || '';
    next();
}

/** 上传接口专用：multipart 无法参与原始体签名，改为校验 token 头 */
function requireWorkerToken(req, res, next) {
    if (!WORKER_SYNC_TOKEN) {
        return res.status(503).json({
            status: 'error',
            message: '服务端未配置 WORKER_SYNC_TOKEN，随身修图机通道未启用'
        });
    }
    if (!workerAllowedIp(req)) {
        return res.status(403).json({ status: 'error', message: '来源 IP 不在白名单' });
    }
    const provided = String(req.get('X-Worker-Token') || '').trim();
    const a = Buffer.from(provided);
    const b = Buffer.from(WORKER_SYNC_TOKEN);
    if (!provided || a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
        return res.status(401).json({ status: 'error', message: 'X-Worker-Token 无效' });
    }
    next();
}

/** 健康检查：Windows 侧用来确认能否连回 Mac */
app.get('/api/worker/ping', requireWorkerAuth, (req, res) => {
    res.json({
        status: 'ok',
        server: 'zero-studio-mac',
        time: new Date().toISOString(),
        node: process.version
    });
});

/** 供中控台查询 Windows 随身修图机最近一次心跳是否在线 */
app.get('/api/internal/worker/status', requireInternalKey, (req, res) => {
    const now = Date.now();
    const seen = workerHeartbeat.last_seen_ms || 0;
    const secAgo = seen ? Math.max(0, Math.round((now - seen) / 1000)) : null;
    const online = Boolean(seen && (now - seen) < 45000);
    res.json({
        status: 'ok',
        worker: {
            online,
            last_seen_sec_ago: secAgo,
            last_ip: workerHeartbeat.last_ip,
            last_path: workerHeartbeat.last_path
        }
    });
});

/**
 * 领取待修任务（替代原先只能在局域网调用的内部接口）。
 * Windows worker 周期调用，拿到 SELECTED 订单的待精修照片清单。
 */
app.get('/api/worker/tasks', requireWorkerAuth, async (req, res) => {
    try {
        const limit = Math.min(50, Math.max(1, parseInt(req.query.limit, 10) || 10));
        const bookings = await dbAll(
            "SELECT * FROM bookings_v3 WHERE status IN ('SELECTED', 'RETOUCHING') ORDER BY id ASC LIMIT ?",
            [limit]
        );

        const tasks = [];
        for (const b of bookings) {
            let selectedIds = [];
            try {
                selectedIds = b.selected_assets ? JSON.parse(b.selected_assets) : [];
            } catch (_) { selectedIds = []; }
            if (!selectedIds.length) continue;

            const placeholders = selectedIds.map(() => '?').join(',');
            const assets = await dbAll(
                `SELECT * FROM photo_assets WHERE id IN (${placeholders}) AND is_rejected = 0`,
                selectedIds
            );

            const skipRows = await dbAll(
                "SELECT DISTINCT asset_id FROM retouch_jobs WHERE booking_id = ? AND stage IN ('DONE', 'FAILED')",
                [b.id]
            );
            const skipSet = new Set(skipRows.map(r => r.asset_id));

            const pendingAssets = [];
            for (const a of assets) {
                if (skipSet.has(a.id)) continue;
                pendingAssets.push({
                    asset_id: a.id,
                    filename: a.filename,
                    raw_path: a.nas_raw_path || a.source_path || a.raw_path || a.file_path || '',
                    preview_url: a.preview_url || ''
                });
            }
            if (!pendingAssets.length) continue;

            tasks.push({
                booking_id: b.id,
                cn: b.cn,
                character: b.character,
                event_name: b.event_name,
                preset_code: b.preset_code || '',
                selected_assets: pendingAssets
            });
        }

        res.json({
            status: 'ok',
            count: tasks.length,
            tasks,
            // 服务端时间让 Windows 校正本地时钟（HMAC 依赖它）
            server_time: new Date().toISOString()
        });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

/** Windows 回传修图任务进度（阶段、产物、错误） */
app.post('/api/worker/job', requireWorkerAuth, async (req, res) => {    try {
        const b = req.body || {};
        const bookingId = parseInt(b.booking_id, 10);
        const assetId = parseInt(b.asset_id, 10);
        if (!Number.isInteger(bookingId) || !Number.isInteger(assetId)) {
            return res.status(400).json({ status: 'error', message: 'booking_id / asset_id 必须是整数' });
        }
        const stage = String(b.stage || '').toUpperCase();
        if (!stage) return res.status(400).json({ status: 'error', message: 'stage 必填' });

        const existing = await dbGet(
            "SELECT * FROM retouch_jobs WHERE booking_id = ? AND asset_id = ? ORDER BY id DESC LIMIT 1",
            [bookingId, assetId]
        );

        const fields = {
            preset_code: b.preset_code != null ? String(b.preset_code) : null,
            gemini_plan_json: b.plan != null
                ? (typeof b.plan === 'string' ? b.plan : JSON.stringify(b.plan))
                : null,
            output_path: b.output_path != null ? String(b.output_path) : null,
            psd_path: b.psd_path != null ? String(b.psd_path) : null,
            psd_url: b.psd_url != null ? String(b.psd_url) : null,
            preview_out_url: b.preview_out_url != null ? String(b.preview_out_url) : null,
            error_msg: b.error_msg != null ? String(b.error_msg) : null
        };

        let jobId;
        if (existing) {
            const sets = [];
            const params = [];
            for (const [k, v] of Object.entries(fields)) {
                if (v !== null) { sets.push(`${k} = ?`); params.push(v); }
            }
            sets.push("stage = ?"); params.push(stage);
            sets.push("updated_at = CURRENT_TIMESTAMP");
            params.push(existing.id);
            await dbRun(`UPDATE retouch_jobs SET ${sets.join(', ')} WHERE id = ?`, params);
            jobId = existing.id;
        } else {
            const r = await dbRun(
                `INSERT INTO retouch_jobs
                 (booking_id, asset_id, preset_code, stage, gemini_plan_json,
                  output_path, psd_path, psd_url, preview_out_url, error_msg)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
                [
                    bookingId, assetId,
                    fields.preset_code || '',
                    stage,
                    fields.gemini_plan_json,
                    fields.output_path,
                    fields.psd_path,
                    fields.psd_url,
                    fields.preview_out_url,
                    fields.error_msg
                ]
            );
            jobId = r.lastID;
        }

        // 整单完成时推进到「待摄影师终审」
        let bookingStatus = null;
        try {
            bookingStatus = await maybeAdvanceBookingToReviewPending(bookingId);
        } catch (_) { /* 非致命 */ }

        res.json({ status: 'ok', job_id: jobId, stage, booking_status: bookingStatus });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

/** Windows 领取待修订意见 */
app.get('/api/worker/revisions', requireWorkerAuth, async (req, res) => {
    try {
        const rows = await dbAll(
            `SELECT rr.*, rj.booking_id, rj.asset_id, rj.preset_code, rj.psd_path, rj.psd_url, rj.output_path, rj.preview_out_url
             FROM retouch_revisions rr
             INNER JOIN retouch_jobs rj ON rj.id = rr.job_id
             WHERE rr.summary = '[待处理]'
             ORDER BY rr.id ASC
             LIMIT 20`
        );

        const items = [];
        for (const r of rows) {
            const prev = await dbGet(
                `SELECT * FROM retouch_revisions
                 WHERE job_id = ? AND version < ? AND summary != '[待处理]'
                 ORDER BY version DESC LIMIT 1`,
                [r.job_id, r.version]
            );
            const fallbackPsd = r.psd_path || r.output_path || '';
            const fallbackPreviewUrl = (prev && prev.preview_url) || r.preview_out_url || '';
            const fallbackPsdUrl = absoluteToOutputUrl((prev && prev.output_psd_path) || '') || r.psd_url || absoluteToOutputUrl(fallbackPsd) || '';
            items.push({
                revision_id: r.id,
                job_id: r.job_id,
                booking_id: r.booking_id,
                asset_id: r.asset_id,
                preset_code: r.preset_code || '',
                feedback_text: r.feedback_text || '',
                target_version: r.version,
                current_version: r.version - 1,
                current_psd_path: (prev && prev.output_psd_path) || fallbackPsd,
                current_preview_path: (prev && prev.output_preview_path) || outputUrlToAbsolute(fallbackPreviewUrl),
                current_psd_url: fallbackPsdUrl,
                current_preview_url: fallbackPreviewUrl,
                previous_summary: (prev && prev.summary) || ''
            });
        }
        res.json({ status: 'ok', count: items.length, revisions: items });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

/** Windows 回传修订结果 */
app.post('/api/worker/revision', requireWorkerAuth, async (req, res) => {
    try {
        const b = req.body || {};
        const jobId = parseInt(b.job_id, 10);
        const version = parseInt(b.version, 10);
        if (!Number.isInteger(jobId) || !Number.isInteger(version)) {
            return res.status(400).json({ status: 'error', message: 'job_id / version 必须是整数' });
        }
        const job = await dbGet("SELECT * FROM retouch_jobs WHERE id = ?", [jobId]);
        if (!job) return res.status(404).json({ status: 'error', message: '任务不存在' });

        const previewUrl = String(b.preview_url || '').trim() || absoluteToOutputUrl(b.output_preview_path);
        const psdUrl = String(b.psd_url || '').trim() || absoluteToOutputUrl(b.output_psd_path);
        const existing = await dbGet(
            "SELECT id FROM retouch_revisions WHERE job_id = ? AND version = ?",
            [jobId, version]
        );

        const vals = [
            String(b.feedback_text || ''),
            String(b.summary || ''),
            Number(b.confidence) || 0,
            JSON.stringify(b.deltas || {}),
            b.jsx_used ? 1 : 0,
            String(b.output_psd_path || ''),
            String(b.output_preview_path || ''),
            previewUrl,
            JSON.stringify(b.warnings || [])
        ];

        if (existing) {
            await dbRun(
                `UPDATE retouch_revisions
                 SET feedback_text=?, summary=?, confidence=?, deltas_json=?, jsx_used=?,
                     output_psd_path=?, output_preview_path=?, preview_url=?, warnings_json=?
                 WHERE id=?`,
                [...vals, existing.id]
            );
        } else {
            await dbRun(
                `INSERT INTO retouch_revisions
                 (job_id, version, feedback_text, summary, confidence, deltas_json, jsx_used,
                  output_psd_path, output_preview_path, preview_url, warnings_json)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
                [jobId, version, ...vals]
            );
        }

        if (previewUrl || b.output_psd_path || psdUrl) {
            await dbRun(
                `UPDATE retouch_jobs
                 SET preview_out_url = COALESCE(?, preview_out_url),
                     output_path = COALESCE(?, output_path),
                     psd_path = COALESCE(?, psd_path),
                     psd_url = COALESCE(?, psd_url),
                     updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?`,
                [previewUrl || null, b.output_psd_path || null, b.output_psd_path || null, psdUrl || null, jobId]
            );
        }

        res.json({ status: 'ok', job_id: jobId, version, preview_url: previewUrl, psd_url: psdUrl });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

/** Windows 标记修订失败 */
app.post('/api/worker/revision/fail', requireWorkerAuth, async (req, res) => {
    try {
        const b = req.body || {};
        const revisionId = parseInt(b.revision_id, 10);
        if (!Number.isInteger(revisionId)) {
            return res.status(400).json({ status: 'error', message: 'revision_id 必须是整数' });
        }
        const error = String(b.error || '修订失败').slice(0, 400);
        const row = await dbGet("SELECT * FROM retouch_revisions WHERE id = ?", [revisionId]);
        if (!row) return res.status(404).json({ status: 'error', message: '修订记录不存在' });

        await dbRun(
            "UPDATE retouch_revisions SET summary = ?, warnings_json = ? WHERE id = ?",
            [`[失败] ${error.slice(0, 80)}`, JSON.stringify([`[失败] ${error}`]), revisionId]
        );
        res.json({ status: 'ok', revision_id: revisionId });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

/** 产物上传：Windows 把 PSD/预览图传回 Mac（供中控台预览与交付） */
app.post('/api/worker/upload', requireWorkerToken, (req, res) => {
    const workerUpload = multer({
        storage: multer.diskStorage({
            destination: (req2, file, cb) => {
                const dir = path.join(RETOUCH_OUTPUT_DIR, 'worker_uploads');
                fs.mkdirSync(dir, { recursive: true });
                cb(null, dir);
            },
            filename: (req2, file, cb) => {
                const safe = path.basename(String(file.originalname || 'upload.bin')).replace(/[^\w.\-]/g, '_');
                cb(null, `${Date.now()}_${safe}`);
            }
        }),
        limits: { fileSize: 500 * 1024 * 1024 }
    }).single('file');

    workerUpload(req, res, async (err) => {
        if (err) return res.status(400).json({ status: 'error', message: err.message });
        if (!req.file) return res.status(400).json({ status: 'error', message: '未收到文件' });

        const rel = `worker_uploads/${req.file.filename}`;
        res.json({
            status: 'ok',
            filename: req.file.filename,
            bytes: req.file.size,
            url: `/outputs/${rel}`
        });
    });
});

/** 公网原片下载：Windows 随身修图机在外网（不在家庭局域网）时，按需从 Mac 拉取客户选中的原片 */
app.get('/api/worker/assets/:assetId/raw', requireWorkerToken, async (req, res) => {
    try {
        const assetId = parseInt(req.params.assetId, 10);
        if (!Number.isInteger(assetId)) {
            return res.status(400).json({ status: 'error', message: 'assetId 无效' });
        }
        const asset = await dbGet("SELECT id, filename, nas_raw_path, preview_url FROM photo_assets WHERE id = ?", [assetId]);
        if (!asset) return res.status(404).json({ status: 'error', message: '素材不存在' });

        const rawPath = String(asset.nas_raw_path || '').trim();
        if (rawPath && fs.existsSync(rawPath)) {
            return res.download(rawPath, asset.filename || path.basename(rawPath));
        }
        return res.status(404).json({ status: 'error', message: `Mac 上原片文件不存在或 NAS 未挂载: ${rawPath}` });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

/** 公网产物下载：Windows 在外网执行修订时，按需从 Mac 拉取上一版 PSD 或预览图 */
app.get('/api/worker/file', requireWorkerToken, (req, res) => {
    try {
        const relUrl = String(req.query.url || '').trim();
        if (!relUrl.startsWith('/outputs/')) {
            return res.status(400).json({ status: 'error', message: '仅允许下载 /outputs/ 下的文件' });
        }
        const absPath = path.resolve(outputUrlToAbsolute(relUrl));
        const outRoot = path.resolve((process.env.RETOUCH_OUTPUT_DIR || '').trim() || path.join(__dirname, 'uploads', 'outputs'));
        if (!absPath.startsWith(outRoot) || !fs.existsSync(absPath)) {
            return res.status(404).json({ status: 'error', message: '文件不存在' });
        }
        return res.download(absPath, path.basename(absPath));
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

app.use(express.static(__dirname));

const db = new sqlite3.Database(DB_PATH);

// 立即启用 WAL 模式与忙等待超时
db.serialize(() => {
    db.run("PRAGMA journal_mode = WAL;");
    db.run("PRAGMA busy_timeout = 5000;");
});

// Promise 化的 SQLite 基础方法
function dbRun(sql, params = []) {
    return new Promise((resolve, reject) => {
        db.run(sql, params, function (err) {
            if (err) return reject(err);
            resolve({ lastID: this.lastID, changes: this.changes });
        });
    });
}

function dbGet(sql, params = []) {
    return new Promise((resolve, reject) => {
        db.get(sql, params, (err, row) => {
            if (err) return reject(err);
            resolve(row);
        });
    });
}

function dbAll(sql, params = []) {
    return new Promise((resolve, reject) => {
        db.all(sql, params, (err, rows) => {
            if (err) return reject(err);
            resolve(rows || []);
        });
    });
}

// 串行化单连接内的写事务 + SQLite BEGIN IMMEDIATE 跨进程写锁，防止并发超卖
let txQueue = Promise.resolve();

function withImmediateTransaction(fn) {
    const run = async () => {
        await dbRun("BEGIN IMMEDIATE");
        try {
            const result = await fn();
            await dbRun("COMMIT");
            return result;
        } catch (err) {
            try {
                await dbRun("ROLLBACK");
            } catch (_) {}
            throw err;
        }
    };
    const next = txQueue.then(run, run);
    txQueue = next.catch(() => {});
    return next;
}

// 安全的列检测与无损 ALTER TABLE 升级
async function ensureColumns(tableName, columns) {
    const existingCols = await dbAll(`PRAGMA table_info(${tableName})`);
    const existingNames = new Set(existingCols.map(c => c.name));
    for (const col of columns) {
        if (!existingNames.has(col.name)) {
            await dbRun(`ALTER TABLE ${tableName} ADD COLUMN ${col.name} ${col.definition}`);
        }
    }
}

async function initDb() {
    await dbRun("PRAGMA journal_mode = WAL;");
    await dbRun("PRAGMA busy_timeout = 5000;");

    await dbRun("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, notice TEXT, price_cz TEXT, price_hgh TEXT)");
    await ensureColumns('settings', [
        { name: 'price_hgh', definition: 'TEXT' }
    ]);

    await dbRun("CREATE TABLE IF NOT EXISTS photos (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, title TEXT, category TEXT, display_location TEXT)");
    await ensureColumns('photos', [
        { name: 'display_location', definition: 'TEXT' },
        { name: 'created_at', definition: 'DATETIME' }
    ]);
    // 一次性归一化历史展示位（gallery / home / portfolio 等英文别名 -> 中文枚举）
    const legacyPhotos = await dbAll("SELECT id, display_location FROM photos");
    for (const photo of legacyPhotos) {
        const normalized = normalizeDisplayLocation(photo.display_location);
        if (normalized !== photo.display_location) {
            await dbRun("UPDATE photos SET display_location = ? WHERE id = ?", [normalized, photo.id]);
        }
    }
    await dbRun("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, contact TEXT, content TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)");
    await dbRun("CREATE TABLE IF NOT EXISTS portal_config (id INTEGER PRIMARY KEY, data TEXT)");
    await dbRun("CREATE TABLE IF NOT EXISTS bookings_v3 (id INTEGER PRIMARY KEY AUTOINCREMENT, wechat TEXT, cn TEXT, character TEXT, event_name TEXT, date TEXT, time TEXT, deposit TEXT, quantity INTEGER, remarks TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)");

    // 无损升级 bookings_v3 字段
    await ensureColumns('bookings_v3', [
        { name: 'wechat_name', definition: 'TEXT' },
        { name: 'qq', definition: 'TEXT' },
        { name: 'platform', definition: "TEXT DEFAULT 'web'" },
        { name: 'im_user_id', definition: 'TEXT' },
        { name: 'character_ref_img', definition: 'TEXT' },
        { name: 'status', definition: "TEXT DEFAULT 'BOOKED'" },
        { name: 'shoot_code', definition: 'TEXT' },
        { name: 'gallery_token', definition: 'TEXT' },
        { name: 'slate_qr_payload', definition: 'TEXT' },
        { name: 'selected_assets', definition: 'TEXT' },
        { name: 'preset_code', definition: 'TEXT' },
        { name: 'delivery_url', definition: 'TEXT' }
    ]);

    // 新建全链路资产、批次与精修队列 4 张表
    await dbRun(`
        CREATE TABLE IF NOT EXISTS ingest_batches (
            batch_id TEXT PRIMARY KEY,
            camera_model TEXT,
            event_name TEXT,
            venue TEXT,
            nas_path TEXT,
            total_files INTEGER,
            verified_files INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    `);

    // 批次来源：auto（插卡自动拷卡+聚类）/ manual（插卡拷卡面板手动导入）
    await ensureColumns('ingest_batches', [
        { name: 'source', definition: "TEXT DEFAULT 'auto'" }
    ]);

    await dbRun(`
        CREATE TABLE IF NOT EXISTS photo_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT,
            filename TEXT,
            nas_raw_path TEXT,
            preview_url TEXT,
            sha256 TEXT,
            is_rejected INTEGER DEFAULT 0,
            reject_reason TEXT,
            cluster_id TEXT,
            captured_at TEXT
        )
    `);

    // 资产来源：auto（插卡自动拷卡+聚类）/ manual（插卡拷卡面板手动导入）
    // 以及手动分类分人时的最后操作时间，供中控台区分与排序
    await ensureColumns('photo_assets', [
        { name: 'source', definition: "TEXT DEFAULT 'auto'" },
        { name: 'imported_at', definition: 'TEXT' },
        { name: 'manual_bound_at', definition: 'TEXT' }
    ]);

    await dbRun(`
        CREATE TABLE IF NOT EXISTS asset_booking_links (
            asset_id INTEGER,
            booking_id INTEGER,
            PRIMARY KEY (asset_id, booking_id)
        )
    `);

    await dbRun(`
        CREATE TABLE IF NOT EXISTS retouch_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER,
            asset_id INTEGER,
            preset_code TEXT,
            stage TEXT DEFAULT 'QUEUED',
            gemini_plan_json TEXT,
            output_path TEXT,
            preview_out_url TEXT,
            review_passed INTEGER DEFAULT 0,
            error_msg TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    `);

    // 精修产物新增 PSD 工程文件路径（与 JPEG 预览分开存放）
    await ensureColumns('retouch_jobs', [
        { name: 'psd_path', definition: 'TEXT' },
        { name: 'psd_url', definition: 'TEXT' }
    ]);

    // 反馈修订版本链：每一次「看图提意见 -> AI 再改」都留一条记录
    await dbRun(`
        CREATE TABLE IF NOT EXISTS retouch_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            feedback_text TEXT,
            summary TEXT,
            confidence REAL,
            deltas_json TEXT,
            jsx_used INTEGER DEFAULT 0,
            output_psd_path TEXT,
            output_preview_path TEXT,
            preview_url TEXT,
            warnings_json TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    `);
    await dbRun(
        "CREATE INDEX IF NOT EXISTS idx_retouch_revisions_job ON retouch_revisions (job_id, version)"
    );

    const row = await dbGet("SELECT count(*) as count FROM portal_config");
    if (row && row.count === 0) {
        const defaultData = JSON.stringify({
            notice: "欢迎使用自助预约系统",
            dates: "2026-05-01,2026-05-02",
            times: "10:00-12:00,14:00-16:00",
            slot_capacity: 1,
            price_cz: 80,
            price_hgh: 180,
            price_options: "石家庄场照(4张)|80\n沪广杭场照(4张)|180",
            invite_code: "zero2026",
            pay_qr: "",
            events: [{ name: '五一漫展专属企划', dates: '5月1日,5月2日', times: '10:00-12:00,14:00-16:00' }]
        });
        await dbRun("INSERT INTO portal_config (id, data) VALUES (1, ?)", [defaultData]);
    }
}

function getPublicBaseUrl(req) {
    const envPublic = (process.env.PUBLIC_BASE_URL || '').trim().replace(/\/+$/, '');
    if (envPublic) return envPublic;
    return `${req.protocol}://${req.get('host')}`;
}

function isLoopbackRequest(req) {
    if (req.get('X-Forwarded-For')) return false;

    // 1) Host 头必须是回环地址
    const hostHeader = String(req.get('host') || '').toLowerCase();
    const hostOnly = hostHeader.startsWith('[')
        ? hostHeader.slice(0, hostHeader.indexOf(']') + 1)
        : hostHeader.split(':')[0];
    const allowedHosts = new Set(['127.0.0.1', 'localhost', '[::1]', '::1']);
    if (!allowedHosts.has(hostOnly)) return false;

    // 2) 真实来源 IP 也必须是回环
    //    Host 头是客户端可控的：外部请求只要伪造 Host: 127.0.0.1 就能绕过上面那步。
    //    经 FRP 隧道进来的请求，socket 来源是公网/反代地址，必须在这里拦下。
    const remote = String(req.socket && req.socket.remoteAddress || '').replace(/^::ffff:/, '');
    const loopbackRemote = remote === '127.0.0.1' || remote === '::1' || remote === '';
    if (!loopbackRemote) return false;

    return true;
}

// 统一内部接口鉴权中间件（强制内网 loopback + X-API-Key 双重保护，绝不通过公网暴露）
function requireInternalKey(req, res, next) {
    if (!isLoopbackRequest(req)) {
        return res.status(403).json({ status: 'error', message: 'Forbidden: internal API is localhost-only' });
    }
    const expectedKey = process.env.INTERNAL_API_KEY || '';
    const providedKey = req.get('X-API-Key') || '';
    if (!expectedKey || !providedKey) {
        return res.status(401).json({ status: 'error', message: 'Unauthorized: invalid or missing X-API-Key' });
    }
    const expectedBuf = Buffer.from(expectedKey);
    const providedBuf = Buffer.from(providedKey);
    if (expectedBuf.length !== providedBuf.length || !crypto.timingSafeEqual(expectedBuf, providedBuf)) {
        return res.status(401).json({ status: 'error', message: 'Unauthorized: invalid or missing X-API-Key' });
    }
    next();
}

// 把「逗号分隔的时间串」统一切分：全角逗号、顿号、换行都当作分隔符
function splitSlotList(raw) {
    return String(raw == null ? '' : raw)
        .split(/[,，、;\n\r]+/)
        .map(s => s.trim())
        .filter(Boolean);
}

// 时段归一化：把全角冒号/横线/空格统一成半角。
// 历史数据里存在「10：00-11：00」（全角）与配置「10:00-11:00」（半角）不一致的情况，
// 导致同一时段永远匹配不上 → 余量统计失真、可以无限超卖。
function normalizeSlot(raw) {
    return String(raw == null ? '' : raw)
        .replace(/\uFF1A/g, ':')   // 全角冒号
        .replace(/\uFF0D/g, '-')   // 全角连字符
        .replace(/\u2013|\u2014|\u301C|\uFF5E/g, '-') // 各种破折号/波浪线
        .replace(/\s+/g, '')
        .trim();
}

function parseTimeSlots(times, time) {
    let slotList = [];
    if (Array.isArray(times) && times.length > 0) {
        slotList = times.flatMap(t => splitSlotList(t));
    } else if (typeof time === 'string' && time.trim()) {
        slotList = splitSlotList(time);
    }
    return [...new Set(slotList)];
}

function generateGalleryToken() {
    return uuidv4().replace(/-/g, '').slice(0, 16);
}

// 订单二维码：直接把「客户约了什么」编码进二维码，任何 AI 扫码即可读到完整预约，
// 不需要客户再重复登记。格式为纯 ASCII 的 key=value 串，避免中文在扫码器里乱码。
const BOOKING_QR_VERSION = 2;

function qrSafe(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '%26')
        .replace(/=/g, '%3D')
        .replace(/;/g, '%3B')
        .replace(/\s+/g, ' ')
        .trim();
}

function roundMoney(value) {
    return Number(value)
        .toFixed(2)
        .replace(/\.00$/, '')
        .replace(/(\.\d)0$/, '$1');
}

function computeBookingAmount(payment, config, quantity) {
    const qty = Math.max(1, parseInt(quantity, 10) || 1);
    let unitPrice = null;
    if (payment) {
        const fromOptions = String(config.price_options || '')
            .split('\n')
            .map(line => line.split('|'))
            .find(parts => parts.length === 2 && parts[0].trim() === String(payment).trim());
        if (fromOptions) {
            unitPrice = Number(fromOptions[1].trim());
        } else {
            const parsed = Number(String(payment).replace(/[^\d.]/g, ''));
            unitPrice = Number.isFinite(parsed) ? parsed : null;
        }
    }
    if (unitPrice != null && Number.isFinite(unitPrice)) {
        return { amount: roundMoney(unitPrice * qty), unit_price: roundMoney(unitPrice) };
    }
    return { amount: roundMoney(qty), unit_price: null };
}

function buildBookingQrPayload(booking, config = {}, baseUrl = '') {
    const quantity = parseInt(booking.quantity, 10) || 1;
    const money = computeBookingAmount(booking.deposit, config, quantity);
    // 载荷要短：扫码器对过长的中文内容解码不稳。这里只保留「认单」必需字段，
    // 完整信息始终以服务端订单为准（/api/booking-info/<token>）。
    const pairs = [
        ['v', String(BOOKING_QR_VERSION)],
        ['type', 'zero_studio_booking'],
        ['order', String(booking.id)],
        ['token', qrSafe(booking.gallery_token)],
        ['cn', qrSafe(booking.cn)],
        ['character', qrSafe(booking.character)],
        ['event', qrSafe(booking.event_name)],
        ['date', qrSafe(booking.date)],
        ['time', qrSafe(booking.time)],
        ['shots', String(quantity)],
        ['amount', money.amount],
        ['status', String(booking.status || 'BOOKED')]
    ].filter(([, v]) => v !== '');
    return 'ZERO-STUDIO-BOOKING|' + pairs.map(([k, v]) => `${k}=${v}`).join(';');
}

function buildSlateQrPayload(bookingOrId, cn, character, eventName, config = null, baseUrl = '') {
    if (bookingOrId && typeof bookingOrId === 'object') {
        return buildBookingQrPayload(bookingOrId, config || {}, baseUrl);
    }
    // 兼容旧调用：仅有 id 时仍返回可被老扫码逻辑解析的对象
    return JSON.stringify({
        booking_id: bookingOrId,
        cn: cn || '',
        character: character || '',
        event_name: eventName || ''
    });
}

// 读取旧版 JSON 场记码，让老订单也能产出新版二维码
function parseLegacySlatePayload(payload) {
    try {
        const parsed = JSON.parse(payload);
        if (parsed && typeof parsed === 'object' && parsed.booking_id != null) {
            return parsed;
        }
    } catch (_) {}
    return null;
}

function sendPushPlusNotification(titleText, bodyText) {
    if (!PUSHPLUS_TOKEN) return;
    const title = encodeURIComponent(titleText);
    const body = encodeURIComponent(bodyText);
    https.get(`https://www.pushplus.plus/send?token=${PUSHPLUS_TOKEN}&title=${title}&content=${body}`).on("error", () => {});
}

// 原子锁档核心逻辑（被 Web 预约接口与内部 Bot 预约接口共同复用）
async function createBookingWithLock(payload, options = { checkInviteCode: true }) {
    const {
        wechat = '',
        wechat_name = '',
        qq = null,
        platform = 'web',
        im_user_id = null,
        cn = '',
        character = '',
        character_ref_img = null,
        event_name = '',
        date = '',
        times,
        time,
        deposit = '',
        quantity = 1,
        remarks = '',
        invite_code,
        shoot_code = null,
        status = 'BOOKED'
    } = payload;

    const cleanWechat = String(wechat || '').trim();
    const cleanWechatName = String(wechat_name || '').trim();
    const hasWechatBinding = Boolean(cleanWechat && cleanWechatName);
    const resolvedPlatform = hasWechatBinding && platform === 'web' ? 'wechat' : platform;
    const resolvedImUserId = im_user_id || (cleanWechatName || cleanWechat || null);

    const requestedSlots = parseTimeSlots(times, time);
    if (requestedSlots.length === 0) {
        const err = new Error('❌ 必须选择至少一个时间段！请刷新重试。');
        err.statusCode = 400;
        throw err;
    }

    if (!date) {
        const err = new Error('❌ 必须提供拍摄日期！');
        err.statusCode = 400;
        throw err;
    }

    if (!ALLOWED_BOOKING_STATUSES.has(status)) {
        const err = new Error(`非法订单状态: ${status}`);
        err.statusCode = 400;
        throw err;
    }

    const finalTimeStr = requestedSlots.join(', ');

    return withImmediateTransaction(async () => {
        const configRow = await dbGet("SELECT data FROM portal_config WHERE id = 1");
        let config = {};
        if (configRow && configRow.data) {
            try {
                config = JSON.parse(configRow.data);
            } catch (_) {}
        }

        if (options.checkInviteCode && !hasWechatBinding && config.invite_code && invite_code !== config.invite_code) {
            const err = new Error('❌ 请绑定您的微信号与微信昵称，或填写正确的预约口令。');
            err.statusCode = 400;
            throw err;
        }

        const capacity = parseInt(config.slot_capacity, 10) || 1;
        const rows = await dbAll("SELECT time FROM bookings_v3 WHERE date = ?", [date]);

        const timeCounts = {};
        for (const r of rows) {
            if (r.time) {
                r.time.split(',').forEach(t => {
                    const key = normalizeSlot(t);
                    if (key) {
                        timeCounts[key] = (timeCounts[key] || 0) + 1;
                    }
                });
            }
        }

        // 现场手动建档（skip_capacity_check）不占用客户自助预约的时段容量，
        // 只在内部接口显式传参时生效，客户侧公开预约接口永远走容量校验。
        for (const slot of requestedSlots) {
            if (options.skipCapacityCheck) break;
            if ((timeCounts[normalizeSlot(slot)] || 0) >= capacity) {
                const err = new Error('❌ 部分时间段已被抢空，请重新选择');
                err.statusCode = 409;
                throw err;
            }
        }

        const galleryToken = generateGalleryToken();

        const insertResult = await dbRun(
            `INSERT INTO bookings_v3 (
                wechat, wechat_name, qq, platform, im_user_id, cn, character, character_ref_img,
                event_name, date, time, deposit, quantity, remarks, status, shoot_code, gallery_token
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
            [
                cleanWechat, cleanWechatName || null, qq, resolvedPlatform, resolvedImUserId, cn, character, character_ref_img,
                event_name, date, finalTimeStr, deposit, quantity, remarks, status, shoot_code, galleryToken
            ]
        );

        const bookingId = insertResult.lastID;
        const booking = await dbGet("SELECT * FROM bookings_v3 WHERE id = ?", [bookingId]);
        const slateQrPayload = buildBookingQrPayload(booking, config, options.baseUrl || '');

        await dbRun(
            "UPDATE bookings_v3 SET slate_qr_payload = ? WHERE id = ?",
            [slateQrPayload, bookingId]
        );

        const updatedBooking = await dbGet("SELECT * FROM bookings_v3 WHERE id = ?", [bookingId]);
        return { booking: updatedBooking, config };
    });
}

// 统一的二维码渲染参数。
//
// 两个坑（都实测踩过）：
// 1) qrcode 库默认会把内容拆成 Alphanumeric + Byte 多段混编，
//    部分扫码器/OpenCV 对多段码解不出来 —— 必须强制单一 Byte 分段。
// 2) 纠错级别 M 在内容偏长（>150 字节）时会超出可靠解析范围，
//    实测 M 解不出、L 能解 —— 用 L 级并控制载荷长度。
const QR_RENDER_OPTIONS = {
    type: 'png',
    errorCorrectionLevel: 'L',
    margin: 4,
    width: 900,
    color: { dark: '#000000', light: '#ffffff' }
};

async function renderQrPng(payload) {
    const segments = [{ data: Buffer.from(String(payload), 'utf8'), mode: 'byte' }];
    return QRCode.toBuffer(segments, QR_RENDER_OPTIONS);
}

// 旧版二维码载荷里包含 shop/wechat/gallery 等冗余字段，内容过长会导致
// 手机截图后难以识别。这里判断载荷是否已是「精简版」，否则重新生成。
function isCompactQrPayload(payload) {
    if (!payload || !String(payload).startsWith('ZERO-STUDIO-BOOKING|')) return false;
    const s = String(payload);
    // 精简版只有 token= / order= / cn= 等核心字段，且不含这些冗余键
    if (/(?:^|[;|])(?:shop|wechat_id|wechat|qq|platform|gallery|issued|deposit|remarks|package|gallery_token)=/.test(s)) {
        return false;
    }
    return /(?:^|[;|])token=/.test(s) || /(?:^|[;|])order=/.test(s);
}

// 生成订单信息二维码（DataURL），供网页端直接展示与保存
async function renderBookingQrPng(booking) {
    const payload = isCompactQrPayload(booking.slate_qr_payload)
        ? booking.slate_qr_payload
        : buildBookingQrPayload(booking, {}, '');
    const buffer = await renderQrPng(payload);
    if (booking.slate_qr_payload !== payload) {
        await dbRun("UPDATE bookings_v3 SET slate_qr_payload = ? WHERE id = ?", [payload, booking.id]).catch(() => {});
    }
    return { buffer, dataUrl: `data:image/png;base64,${buffer.toString('base64')}`, payload };
}

// 确保订单拥有可被 AI 直接读取的完整预约二维码
async function ensureBookingQrPayload(booking, baseUrl) {
    const current = booking.slate_qr_payload || '';
    if (isCompactQrPayload(current)) return current;
    const fresh = buildBookingQrPayload(booking, {}, baseUrl);
    await dbRun("UPDATE bookings_v3 SET slate_qr_payload = ? WHERE id = ?", [fresh, booking.id]);
    return fresh;
}

app.get('/api/settings', (req, res) => {
    db.get("SELECT * FROM settings WHERE id = 1", (err, row) => {
        if (!row) {
            res.json({ notice: '', price_cz: 80, price_hgh: 180 });
        } else {
            res.json({
                notice: row.notice || '',
                price_cz: row.price_cz || 80,
                price_hgh: row.price_hgh || 180
            });
        }
    });
});

app.post('/api/settings', adminAuth, (req, res) => {
    const { notice, price_cz, price_hgh } = req.body || {};
    db.run("INSERT OR REPLACE INTO settings (id, notice, price_cz, price_hgh) VALUES (1, ?, ?, ?)", [notice, price_cz, price_hgh], () => res.json({ status: 'ok' }));
});

app.get('/api/portal_settings', (req, res) => {
    db.get("SELECT data FROM portal_config WHERE id = 1", (err, row) => {
        if (row && row.data) res.json(JSON.parse(row.data));
        else res.json({ notice: '', invite_code: '', pay_qr: '', events: [] });
    });
});

// 中控台保存的配置是整体覆盖：先把旧配置合并进来，避免只改活动时把价格等字段清空
app.post('/api/portal_settings', adminAuth, async (req, res) => {
    try {
        const incoming = req.body || {};
        const row = await dbGet("SELECT data FROM portal_config WHERE id = 1");
        let existing = {};
        if (row && row.data) {
            try {
                existing = JSON.parse(row.data);
            } catch (_) {}
        }
        const merged = { ...existing, ...incoming };
        if (incoming.events !== undefined) {
            merged.events = normalizeEvents(incoming.events);
        }
        if (incoming.services !== undefined) {
            merged.services = normalizeServiceOverrides(incoming.services);
        }
        await dbRun("INSERT OR REPLACE INTO portal_config (id, data) VALUES (1, ?)", [JSON.stringify(merged)]);
        res.json({ status: 'ok', portal_settings: merged });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 活动企划：不论锁档还是归档，都允许中控台读写（锁档只限制客户端自助选位）
// 保存时顺手把日期/时段统一成半角逗号分隔，避免全角符号混入导致切分错误
function normalizeEvents(events) {
    if (!Array.isArray(events)) return [];
    return events
        .filter(ev => ev && typeof ev === 'object')
        .map(ev => ({
            name: String(ev.name || '').trim(),
            dates: splitSlotList(ev.dates).join(','),
            times: splitSlotList(ev.times).join(','),
            id: ev.id ? String(ev.id) : '',
            locked: ev.locked === true || ev.locked === 'true',
            archived: ev.archived === true || ev.archived === 'true',
            note: String(ev.note || '').trim()
        }))
        .filter(ev => ev.name);
}

function normalizeServiceOverrides(services) {
    if (!Array.isArray(services)) return [];
    return services
        .filter(s => s && typeof s === 'object' && typeof s.id === 'string')
        .map(s => {
            const clean = { id: s.id };
            for (const key of Object.keys(s)) {
                if (key !== 'id' && SERVICE_FIELDS.has(key)) clean[key] = s[key];
            }
            return clean;
        });
}

// 首页套餐卡片（四张卡全部字段可改）
app.get('/api/services', async (req, res) => {
    try {
        const row = await dbGet("SELECT data FROM portal_config WHERE id = 1");
        let config = {};
        if (row && row.data) {
            try {
                config = JSON.parse(row.data);
            } catch (_) {}
        }
        res.json(resolveServices(config.services));
    } catch (err) {
        res.json(resolveServices([]));
    }
});

app.get('/api/portal_config', async (req, res) => {
    try {
        const row = await dbGet("SELECT data FROM portal_config WHERE id = 1");
        let config = {};
        if (row && row.data) {
            try {
                config = JSON.parse(row.data);
            } catch (_) {}
        }
        res.json({
            ...config,
            events: normalizeEvents(config.events || []),
            services: resolveServices(config.services)
        });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 自选漫展时间：返回活动、日期、时段与剩余名额，供客户自助选择
app.get('/api/availability', async (req, res) => {
    try {
        const row = await dbGet("SELECT data FROM portal_config WHERE id = 1");
        let config = {};
        if (row && row.data) {
            try {
                config = JSON.parse(row.data);
            } catch (_) {}
        }
        const capacity = Math.max(1, parseInt(config.slot_capacity, 10) || 1);
        const rows = await dbAll("SELECT date, time FROM bookings_v3");
        const counts = {};
        for (const r of rows) {
            if (!r.date || !r.time) continue;
            for (const raw of String(r.time).split(',')) {
                const slot = normalizeSlot(raw);
                if (!slot) continue;
                const key = `${r.date}__${slot}`;
                counts[key] = (counts[key] || 0) + 1;
            }
        }
        const events = normalizeEvents(config.events || []).filter(ev => !ev.archived);
        const result = events.map(ev => {
            const dates = splitSlotList(ev.dates);
            const times = splitSlotList(ev.times);
            return {
                name: ev.name,
                locked: ev.locked,
                note: ev.note,
                dates: dates.map(date => ({
                    date,
                    slots: times.map(time => {
                        const used = counts[`${date}__${normalizeSlot(time)}`] || 0;
                        return { time, used, capacity, remaining: Math.max(0, capacity - used), full: used >= capacity };
                    })
                }))
            };
        });
        res.json({ status: 'ok', slot_capacity: capacity, events: result });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

app.get('/api/photos', (req, res) => db.all("SELECT * FROM photos ORDER BY id DESC", (err, rows) => res.json((rows || []).map(p => ({ ...p, display_location: normalizeDisplayLocation(p.display_location) })))));
const upload = multer({ dest: path.join(__dirname, 'uploads') });
app.post('/api/upload', adminAuth, upload.single('photo'), (req, res) => {
    if (!req.file) return res.status(400).json({ status: 'error', message: '未收到图片文件' });
    const url = `/uploads/${req.file.filename}`;
    const { title, category } = req.body || {};
    // 规范化展示位：中控台传来的 'gallery' 等旧枚举会自动落到「双端展示」
    const display_location = normalizeDisplayLocation((req.body || {}).display_location);
    db.run("INSERT INTO photos (url, title, category, display_location) VALUES (?, ?, ?, ?)", [url, title || '', category || '', display_location], function (err) {
        if (err) return res.status(500).json({ status: 'error', message: err.message });
        // 中控台走 JSON，浏览器表单走跳转
        if (req.accepts(['html', 'json']) === 'json') {
            return res.json({ status: 'ok', id: this.lastID, url, title: title || '', category: category || '', display_location });
        }
        res.redirect('/admin.html');
    });
});

// 修改作品标题 / 分类 / 展示位（无需重新上传）
app.post('/api/photos/update', adminAuth, async (req, res) => {
    const { id, title, category, display_location } = req.body || {};
    if (id == null) return res.status(400).json({ status: 'error', message: '缺少作品 id' });
    try {
        const existing = await dbGet("SELECT * FROM photos WHERE id = ?", [id]);
        if (!existing) return res.status(404).json({ status: 'error', message: '作品不存在' });
        await dbRun(
            "UPDATE photos SET title = ?, category = ?, display_location = ? WHERE id = ?",
            [
                title !== undefined ? title : existing.title,
                category !== undefined ? category : existing.category,
                display_location !== undefined ? normalizeDisplayLocation(display_location) : normalizeDisplayLocation(existing.display_location),
                id
            ]
        );
        const updated = await dbGet("SELECT * FROM photos WHERE id = ?", [id]);
        res.json({ status: 'ok', photo: updated });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

app.post('/api/photos/delete', adminAuth, (req, res) => {
    db.get("SELECT url FROM photos WHERE id = ?", [req.body.id], (err, row) => {
        if (row) fs.unlink(path.join(__dirname, 'uploads', path.basename(row.url)), () => {});
        db.run("DELETE FROM photos WHERE id = ?", [req.body.id], () => res.json({ status: 'ok' }));
    });
});

app.get('/api/booked-times', (req, res) => {
    db.get("SELECT data FROM portal_config LIMIT 1", (err, configRow) => {
        let capacity = 1;
        if (configRow && configRow.data) {
            try {
                const config = JSON.parse(configRow.data);
                capacity = parseInt(config.slot_capacity, 10) || 1;
            } catch (e) {}
        }

        db.all("SELECT date, time FROM bookings_v3", (err, rows) => {
            if (err) return res.json([]);

            const timeCounts = {};
            rows.forEach(row => {
                if (!row.date || !row.time) return;
                const times = row.time.split(',').map(t => normalizeSlot(t));
                times.forEach(t => {
                    if (!t) return;
                    const key = `${row.date}__${t}`;
                    timeCounts[key] = (timeCounts[key] || 0) + 1;
                });
            });

            const fullSlots = [];
            for (const key in timeCounts) {
                if (timeCounts[key] >= capacity) {
                    const sepIdx = key.indexOf('__');
                    const date = key.slice(0, sepIdx);
                    const time = key.slice(sepIdx + 2);
                    fullSlots.push({ date, time });
                }
            }

            res.json(fullSlots);
        });
    });
});

// 【核心修复】使用 BEGIN IMMEDIATE 事务锁防止高并发超卖
app.post('/api/save-schedule', async (req, res) => {
    const body = req.body || {};
    try {
        const { booking, config } = await createBookingWithLock(
            { ...body, platform: body.platform || 'web' },
            { checkInviteCode: true, baseUrl: getPublicBaseUrl(req) }
        );

        sendPushPlusNotification(
            '🚨 新档期锁定！',
            `CN: ${booking.cn}\n角色: ${booking.character}\n微信: ${booking.wechat}\n活动: ${booking.event_name}\n时间: ${booking.date} ${booking.time}\n定金: ${booking.deposit}\n数量: ${booking.quantity}张\n备注: ${booking.remarks || '无'}`
        );

        // 同步生成「已约信息二维码」：客户保存后发给任何 AI，AI 扫码即知她约了什么
        const bookingQrPng = await renderBookingQrPng(booking);
        res.json({
            status: 'ok',
            booking_id: booking.id,
            gallery_token: booking.gallery_token,
            slate_qr_payload: booking.slate_qr_payload,
            booking_qr_data_url: bookingQrPng.dataUrl,
            booking_qr_url: `${getPublicBaseUrl(req)}/booking-qr/${booking.gallery_token}`,
            pay_qr: config.pay_qr
        });
    } catch (err) {
        const statusCode = err.statusCode || 500;
        res.status(statusCode).json({ status: 'error', message: err.message });
    }
});

// ==================== 内部自动化接口 (需 X-API-Key 鉴权) ====================

// 1. 供微信/QQ AI 机器人写入预约单（自动生成 gallery_token & slate_qr_payload，走并发锁校验）
//    skip_capacity_check=true 仅供中控台"插卡拷卡 → 现场手动建档"使用，
//    因为现场拍照客户已到场，不能被客户自助预约的时段容量挡住。
app.post('/api/internal/bookings', requireInternalKey, async (req, res) => {
    const body = req.body || {};
    try {
        const inferredPlatform = body.platform || (body.qq && !body.wechat ? 'qq' : (body.wechat ? 'wechat' : 'internal'));
        const { booking } = await createBookingWithLock(
            { ...body, platform: inferredPlatform },
            {
                checkInviteCode: false,
                skipCapacityCheck: body.skip_capacity_check === true,
                baseUrl: getPublicBaseUrl(req)
            }
        );

        sendPushPlusNotification(
            '🤖 新内部机器人锁档！',
            `平台: ${booking.platform}\nCN: ${booking.cn}\n角色: ${booking.character}\n微信: ${booking.wechat || '-'}\nQQ: ${booking.qq || '-'}\n活动: ${booking.event_name}\n时间: ${booking.date} ${booking.time}`
        );

        res.status(201).json({
            status: 'ok',
            booking_id: booking.id,
            gallery_token: booking.gallery_token,
            slate_qr_payload: booking.slate_qr_payload,
            booking
        });
    } catch (err) {
        const statusCode = err.statusCode || 500;
        res.status(statusCode).json({ status: 'error', message: err.message });
    }
});

// 2. 按活动与日期查询预约单（供聚类与拍摄对照使用）
app.get('/api/internal/bookings/by-event', requireInternalKey, async (req, res) => {
    try {
        const eventName = typeof req.query.event_name === 'string' ? req.query.event_name.trim() : '';
        const date = typeof req.query.date === 'string' ? req.query.date.trim() : '';

        if (!eventName && !date) {
            return res.status(400).json({ status: 'error', message: 'Query parameter event_name or date is required' });
        }

        const conditions = [];
        const params = [];
        if (eventName) {
            conditions.push("event_name = ?");
            params.push(eventName);
        }
        if (date) {
            conditions.push("date = ?");
            params.push(date);
        }

        const sql = `SELECT * FROM bookings_v3 WHERE ${conditions.join(' AND ')} ORDER BY date ASC, time ASC, id ASC`;
        const bookings = await dbAll(sql, params);
        res.json({ status: 'ok', count: bookings.length, bookings });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 3. 更新订单状态与 gallery_token
app.patch('/api/internal/bookings/:id/status', requireInternalKey, async (req, res) => {
    try {
        const bookingId = parseInt(req.params.id, 10);
        if (Number.isNaN(bookingId)) {
            return res.status(400).json({ status: 'error', message: 'Invalid booking id' });
        }

        const { status, gallery_token } = req.body || {};
        if (status === undefined && gallery_token === undefined) {
            return res.status(400).json({ status: 'error', message: 'Provide at least status or gallery_token' });
        }

        if (status !== undefined && !ALLOWED_BOOKING_STATUSES.has(status)) {
            return res.status(400).json({
                status: 'error',
                message: `Invalid status. Allowed: ${[...ALLOWED_BOOKING_STATUSES].join(', ')}`
            });
        }

        const existing = await dbGet("SELECT * FROM bookings_v3 WHERE id = ?", [bookingId]);
        if (!existing) {
            return res.status(404).json({ status: 'error', message: 'Booking not found' });
        }

        const updates = [];
        const params = [];
        if (status !== undefined) {
            updates.push("status = ?");
            params.push(status);
        }
        if (gallery_token !== undefined) {
            updates.push("gallery_token = ?");
            params.push(gallery_token);
        }
        params.push(bookingId);

        await dbRun(`UPDATE bookings_v3 SET ${updates.join(', ')} WHERE id = ?`, params);
        const updated = await dbGet("SELECT * FROM bookings_v3 WHERE id = ?", [bookingId]);
        res.json({ status: 'ok', booking: updated });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 4. 返回订单对应的场记二维码 PNG 图片
app.get('/api/internal/bookings/:id/slate-qr', requireInternalKey, async (req, res) => {
    try {
        const bookingId = parseInt(req.params.id, 10);
        if (Number.isNaN(bookingId)) {
            return res.status(400).json({ status: 'error', message: 'Invalid booking id' });
        }

        const booking = await dbGet("SELECT * FROM bookings_v3 WHERE id = ?", [bookingId]);
        if (!booking) {
            return res.status(404).json({ status: 'error', message: 'Booking not found' });
        }

        const payload = await ensureBookingQrPayload(booking, getPublicBaseUrl(req));

        const pngBuffer = await renderQrPng(payload);

        res.setHeader('Content-Type', 'image/png');
        res.setHeader('Content-Disposition', `inline; filename="slate-qr-${booking.id}.png"`);
        res.send(pngBuffer);
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 4.1 客户侧公开二维码：客户在中控台/官网保存后可发给任何 AI，
// AI 扫码即可读到完整预约（订单号、CN、角色、活动、日期时段、张数、金额），无需客户重复登记。
app.get('/booking-qr/:token', async (req, res) => {
    try {
        const token = String(req.params.token || '').trim();
        const booking = await dbGet("SELECT * FROM bookings_v3 WHERE gallery_token = ?", [token]);
        if (!booking) return res.status(404).send('<h3>订单不存在或二维码已失效</h3>');
        const payload = await ensureBookingQrPayload(booking, getPublicBaseUrl(req));
        const png = await renderQrPng(payload);
        res.setHeader('Content-Type', 'image/png');
        res.setHeader('Cache-Control', 'no-store');
        res.setHeader('Content-Disposition', `inline; filename="zero-booking-${booking.id}.png"`);
        res.send(png);
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 4.2 中控台用：按订单 id 直接取二维码（base64 DataURL，方便复制 / 下载 / 微信转发）
app.get('/api/admin/bookings/:id/qr', adminAuth, async (req, res) => {
    try {
        const bookingId = parseInt(req.params.id, 10);
        if (!Number.isInteger(bookingId)) return res.status(400).json({ status: 'error', message: 'Invalid booking id' });
        const booking = await dbGet("SELECT * FROM bookings_v3 WHERE id = ?", [bookingId]);
        if (!booking) return res.status(404).json({ status: 'error', message: 'Booking not found' });
        const payload = await ensureBookingQrPayload(booking, getPublicBaseUrl(req));
        const { dataUrl } = await renderBookingQrPng({ ...booking, slate_qr_payload: payload });
        res.json({
            status: 'ok',
            booking_id: booking.id,
            booking_qr_data_url: dataUrl,
            booking_qr_url: `${getPublicBaseUrl(req)}/booking-qr/${booking.gallery_token}`,
            payload
        });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 4.3 AI 读取入口：客户把二维码链接或订单号发给 AI，AI 调此接口即可拿到结构化预约，
// 并明确告知「已登记字段」，避免重复询问。
app.get('/api/booking-info/:token', async (req, res) => {
    try {
        const token = String(req.params.token || '').trim();
        const booking = await dbGet("SELECT * FROM bookings_v3 WHERE gallery_token = ?", [token]);
        if (!booking) return res.status(404).json({ status: 'error', message: '订单不存在或二维码已失效' });
        const payload = await ensureBookingQrPayload(booking, getPublicBaseUrl(req));
        res.json({
            status: 'ok',
            known_fields: ['cn', 'character', 'event_name', 'date', 'time', 'quantity', 'payment'],
            do_not_ask_again: ['cn', 'character', 'event_name', 'date', 'time'],
            booking_qr_payload: payload,
            booking: {
                id: booking.id,
                cn: booking.cn,
                character: booking.character,
                event_name: booking.event_name,
                date: booking.date,
                time: booking.time,
                quantity: booking.quantity,
                payment: booking.deposit,
                remarks: booking.remarks,
                status: booking.status,
                platform: booking.platform,
                wechat_name: booking.wechat_name,
                gallery_token: booking.gallery_token
            }
        });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 4.4 按会话查最新预约：客户在网站自助预约后，AI 无需再问，直接按微信号/QQ 找回她刚约的那单
app.get('/api/booking-lookup', async (req, res) => {
    try {
        const platform = String(req.query.platform || '').trim();
        const userId = String(req.query.user_id || '').trim();
        if (!userId) return res.status(400).json({ status: 'error', message: 'user_id 必填' });

        // IM 私聊时 wechat/qq 字段存的就是对方的 IM user_id
        const field = platform === 'qq' ? 'qq' : 'wechat';
        const rows = await dbAll(
            `SELECT * FROM bookings_v3
             WHERE (${field} = ? OR im_user_id = ? OR wechat_name = ?)
             ORDER BY id DESC LIMIT 1`,
            [userId, userId, userId]
        );
        const booking = rows[0];
        if (!booking) return res.json({ status: 'ok', found: false, booking: null });

        const payload = await ensureBookingQrPayload(booking, getPublicBaseUrl(req));
        res.json({
            status: 'ok',
            found: true,
            booking_qr_payload: payload,
            booking: {
                id: booking.id,
                cn: booking.cn,
                character: booking.character,
                event_name: booking.event_name,
                date: booking.date,
                time: booking.time,
                quantity: booking.quantity,
                payment: booking.deposit,
                remarks: booking.remarks,
                status: booking.status,
                gallery_token: booking.gallery_token
            }
        });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 4.5 按订单号查（来自二维码里只带 order 的旧码）
app.get('/api/internal/bookings/:id', requireInternalKey, async (req, res) => {
    try {
        const bookingId = parseInt(req.params.id, 10);
        if (!Number.isInteger(bookingId)) {
            return res.status(400).json({ status: 'error', message: 'Invalid booking id' });
        }
        const booking = await dbGet("SELECT * FROM bookings_v3 WHERE id = ?", [bookingId]);
        if (!booking) return res.status(404).json({ status: 'error', message: 'Booking not found' });
        res.json({ status: 'ok', booking });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 5. 接收拷卡与聚类结果，事务写入批次、资产与多对多关联表，并将关联订单状态流转为 CLUSTERED
app.post('/api/internal/batches', requireInternalKey, async (req, res) => {
    const body = req.body || {};
    const {
        batch_id,
        camera_model = '',
        event_name = '',
        venue = '',
        nas_path = '',
        total_files = 0,
        verified_files = 0,
        assets = []
    } = body;

    if (!batch_id || typeof batch_id !== 'string') {
        return res.status(400).json({ status: 'error', message: 'batch_id is required' });
    }
    if (!Array.isArray(assets)) {
        return res.status(400).json({ status: 'error', message: 'assets must be an array' });
    }

    // 该批次的来源：默认自动拷卡；中控台手动导入时会显式传 source=manual
    const sourceHint = String(body.source || 'auto') === 'manual' ? 'manual' : 'auto';
    const importedAt = body.imported_at || new Date().toISOString().slice(0, 19);

    try {
        const result = await withImmediateTransaction(async () => {
            await dbRun(
                `INSERT OR REPLACE INTO ingest_batches (
                    batch_id, camera_model, event_name, venue, nas_path, total_files, verified_files, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
                [
                    batch_id,
                    camera_model,
                    event_name,
                    venue,
                    nas_path,
                    Number(total_files) || assets.length,
                    Number(verified_files) || 0,
                    sourceHint
                ]
            );

            // 若同 batch_id 重跑（典型场景：手动分人后又重跑聚类），
            // 必须先记住每张图的人工绑定与来源，重建后再恢复，绝不能一键抹掉人工分人结果
            const previousRows = await dbAll(
                `SELECT pa.filename, pa.source, pa.manual_bound_at, pa.imported_at, abl.booking_id
                 FROM photo_assets pa
                 LEFT JOIN asset_booking_links abl ON abl.asset_id = pa.id
                 WHERE pa.batch_id = ?`,
                [batch_id]
            );
            const previousLinks = new Map();
            const previousMeta = new Map();
            const normalizeFilename = (name) => String(name || '').trim().toLowerCase();
            for (const row of previousRows) {
                const fkey = normalizeFilename(row.filename);
                if (!fkey) continue;
                if (!previousLinks.has(fkey)) previousLinks.set(fkey, new Set());
                if (row.booking_id != null) previousLinks.get(fkey).add(parseInt(row.booking_id, 10));
                const prev = previousMeta.get(fkey) || { source: 'auto', manual_bound_at: null, imported_at: null };
                if (row.source === 'manual' || prev.source === 'manual') prev.source = 'manual';
                if (row.manual_bound_at) prev.manual_bound_at = row.manual_bound_at;
                if (row.imported_at) prev.imported_at = row.imported_at;
                previousMeta.set(fkey, prev);
            }

            await dbRun(
                `DELETE FROM asset_booking_links
                 WHERE asset_id IN (SELECT id FROM photo_assets WHERE batch_id = ?)`,
                [batch_id]
            );
            await dbRun("DELETE FROM photo_assets WHERE batch_id = ?", [batch_id]);

            const linkedBookingIds = new Set();
            const keptManualLinks = new Set();
            let insertedAssets = 0;
            let totalLinks = 0;

            for (const asset of assets) {
                const fkey = normalizeFilename(asset.filename);
                const prevMeta = previousMeta.get(fkey);
                const rowSource = asset.source === 'manual' || (prevMeta && prevMeta.source === 'manual')
                    ? 'manual'
                    : sourceHint;
                const insertRes = await dbRun(
                    `INSERT INTO photo_assets (
                        batch_id, filename, nas_raw_path, preview_url, sha256,
                        is_rejected, reject_reason, cluster_id, captured_at,
                        source, imported_at, manual_bound_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
                    [
                        batch_id,
                        asset.filename || '',
                        asset.nas_raw_path || '',
                        asset.preview_url || '',
                        asset.sha256 || '',
                        asset.is_rejected ? 1 : 0,
                        asset.reject_reason || null,
                        asset.cluster_id || null,
                        asset.captured_at || null,
                        rowSource,
                        asset.imported_at || (prevMeta && prevMeta.imported_at) || importedAt,                        (prevMeta && prevMeta.manual_bound_at) || asset.manual_bound_at || null
                    ]
                );
                const assetId = insertRes.lastID;
                insertedAssets++;

                const rawBookingIds = Array.isArray(asset.booking_ids)
                    ? asset.booking_ids
                    : (asset.booking_id != null ? [asset.booking_id] : []);

                const uniqueBookingIds = [
                    ...new Set(
                        rawBookingIds
                            .map(id => parseInt(id, 10))
                            .filter(id => Number.isInteger(id) && id > 0)
                    )
                ];

                // 恢复重跑前的人工绑定（人工结果优先，合并而非覆盖）。
                // 注意：只有该图此前被打过 manual_bound_at 的归属才算"人工结果"，
                // 否则会把上一轮自动聚类的归属也误计为人工保留。
                const prevManualAt = prevMeta && prevMeta.manual_bound_at;
                for (const bid of previousLinks.get(fkey) || []) {
                    if (!uniqueBookingIds.includes(bid)) uniqueBookingIds.push(bid);
                    if (prevManualAt) keptManualLinks.add(`${fkey}#${bid}`);
                }

                for (const bid of uniqueBookingIds) {
                    await dbRun(
                        "INSERT OR IGNORE INTO asset_booking_links (asset_id, booking_id) VALUES (?, ?)",
                        [assetId, bid]
                    );
                    linkedBookingIds.add(bid);
                    totalLinks++;
                }
            }

            const updatedBookings = [];
            for (const bid of linkedBookingIds) {
                const existing = await dbGet("SELECT * FROM bookings_v3 WHERE id = ?", [bid]);
                if (!existing) continue;

                const token = existing.gallery_token || generateGalleryToken();
                const shouldAdvance = ['BOOKED', 'SHOT', 'CLUSTERED', null, ''].includes(existing.status);
                const nextStatus = shouldAdvance ? 'CLUSTERED' : existing.status;

                await dbRun(
                    "UPDATE bookings_v3 SET status = ?, gallery_token = ? WHERE id = ?",
                    [nextStatus, token, bid]
                );

                const validCountRow = await dbGet(
                    `SELECT COUNT(*) AS cnt
                     FROM photo_assets pa
                     INNER JOIN asset_booking_links abl ON abl.asset_id = pa.id
                     WHERE abl.booking_id = ? AND pa.is_rejected = 0`,
                    [bid]
                );

                const publicBase = getPublicBaseUrl(req);
                updatedBookings.push({
                    id: bid,
                    cn: existing.cn,
                    character: existing.character,
                    event_name: existing.event_name,
                    status: nextStatus,
                    gallery_token: token,
                    gallery_path: `/gallery/${token}`,
                    gallery_url: `${publicBase}/gallery/${token}`,
                    valid_asset_count: validCountRow ? validCountRow.cnt : 0
                });
            }

            return {
                batch_id,
                inserted_assets: insertedAssets,
                total_links: totalLinks,
                kept_manual_links: keptManualLinks.size,
                bookings: updatedBookings
            };
        });

        res.status(201).json({ status: 'ok', ...result });
    } catch (err) {
        const statusCode = err.statusCode || 500;
        res.status(statusCode).json({ status: 'error', message: err.message });
    }
});

// ==================== 插卡拷卡：手动分类分人（批次 / 素材 / 归属） ====================

// 把素材的"归谁"落库：add（合影加人）/ replace（单人换人）/ remove（取消归属）
// replace 与 remove 会先清掉这些图原有的全部归属，再（按需）写入新客户。
async function applyAssetBookingLinks(assetIds, bookingId, mode) {
    const ids = [...new Set((assetIds || []).map(v => parseInt(v, 10)).filter(v => Number.isInteger(v) && v > 0))];
    if (ids.length === 0) {
        const err = new Error('asset_ids 不能为空');
        err.statusCode = 400;
        throw err;
    }
    const targetBid = parseInt(bookingId, 10);
    if (mode !== 'remove' && !(Number.isInteger(targetBid) && targetBid > 0)) {
        const err = new Error('booking_id 无效');
        err.statusCode = 400;
        throw err;
    }
    const now = new Date().toISOString().slice(0, 19);
    const ph = ids.map(() => '?').join(',');

    return withImmediateTransaction(async () => {
        if (mode !== 'remove') {
            const booking = await dbGet("SELECT id FROM bookings_v3 WHERE id = ?", [targetBid]);
            if (!booking) {
                const err = new Error('Booking not found');
                err.statusCode = 404;
                throw err;
            }
        }

        const rows = await dbAll(
            `SELECT id, batch_id FROM photo_assets WHERE id IN (${ph})`,
            ids
        );
        const foundIds = rows.map(r => r.id);
        if (foundIds.length === 0) {
            const err = new Error('素材不存在');
            err.statusCode = 404;
            throw err;
        }
        const fph = foundIds.map(() => '?').join(',');

        // 记录操作影响到的批次与订单，末尾统一补齐 gallery_token
        const touchedBatchIds = new Set(rows.map(r => r.batch_id));
        const touchedBookingIds = new Set();
        const beforeLinks = await dbAll(
            `SELECT booking_id FROM asset_booking_links WHERE asset_id IN (${fph})`,
            foundIds
        );
        beforeLinks.forEach(l => touchedBookingIds.add(l.booking_id));

        if (mode === 'replace' || mode === 'remove') {
            await dbRun(`DELETE FROM asset_booking_links WHERE asset_id IN (${fph})`, foundIds);
        }

        let affected = 0;
        if (mode !== 'remove') {
            for (const aid of foundIds) {
                const r = await dbRun(
                    "INSERT OR IGNORE INTO asset_booking_links (asset_id, booking_id) VALUES (?, ?)",
                    [aid, targetBid]
                );
                affected += r.changes || 0;
            }
            touchedBookingIds.add(targetBid);
        }

        // 标记人工分类分人时间，聚类重跑时据此保留人工结果
        await dbRun(`UPDATE photo_assets SET manual_bound_at = ? WHERE id IN (${fph})`, [now, ...foundIds]);

        // 被卷入的订单补齐选图短链（原有 token 保留，绝不换链接）
        for (const bid of touchedBookingIds) {
            if (!Number.isInteger(bid) || bid <= 0) continue;
            const booking = await dbGet("SELECT id, gallery_token FROM bookings_v3 WHERE id = ?", [bid]);
            if (booking && !booking.gallery_token) {
                await dbRun("UPDATE bookings_v3 SET gallery_token = ? WHERE id = ?", [generateGalleryToken(), bid]);
            }
        }

        return { asset_ids: foundIds, affected, mode, batch_ids: [...touchedBatchIds], booking_ids: [...touchedBookingIds] };
    });
}

// 8.1 批次列表（手动导入与自动拷卡统一归类）
app.get('/api/internal/batches', requireInternalKey, async (req, res) => {
    try {
        const limit = Math.min(Math.max(parseInt(req.query.limit, 10) || 60, 1), 300);
        const eventName = typeof req.query.event_name === 'string' ? req.query.event_name.trim() : '';
        const source = ['auto', 'manual'].includes(req.query.source) ? req.query.source : '';
        const conds = [];
        const params = [];
        if (eventName) { conds.push('b.event_name = ?'); params.push(eventName); }
        if (source) { conds.push("COALESCE(b.source, 'auto') = ?"); params.push(source); }
        const where = conds.length ? `WHERE ${conds.join(' AND ')}` : '';
        params.push(limit);

        const rows = await dbAll(
            `SELECT b.*,
                    COALESCE(b.source, 'auto') AS source,
                    (SELECT COUNT(*) FROM photo_assets pa
                      WHERE pa.batch_id = b.batch_id AND pa.is_rejected = 0) AS valid_asset_count,
                    (SELECT COUNT(*) FROM photo_assets pa
                      WHERE pa.batch_id = b.batch_id) AS asset_count,
                    (SELECT COUNT(DISTINCT pa.id)
                       FROM photo_assets pa
                       INNER JOIN asset_booking_links abl ON abl.asset_id = pa.id
                      WHERE pa.batch_id = b.batch_id AND pa.is_rejected = 0) AS linked_asset_count,
                    (SELECT COUNT(DISTINCT abl.booking_id)
                       FROM photo_assets pa
                       INNER JOIN asset_booking_links abl ON abl.asset_id = pa.id
                      WHERE pa.batch_id = b.batch_id) AS booking_count,
                    (SELECT MIN(pa.preview_url) FROM photo_assets pa
                      WHERE pa.batch_id = b.batch_id) AS sample_preview_url,
                    (SELECT MAX(pa.imported_at) FROM photo_assets pa
                      WHERE pa.batch_id = b.batch_id) AS last_imported_at
             FROM ingest_batches b
             ${where}
             ORDER BY b.created_at DESC, b.batch_id DESC
             LIMIT ?`,
            params
        );
        for (const r of rows) {
            r.valid_asset_count = Number(r.valid_asset_count) || 0;
            r.linked_asset_count = Number(r.linked_asset_count) || 0;
            r.unassigned_asset_count = Math.max(r.valid_asset_count - r.linked_asset_count, 0);
        }
        res.json({ status: 'ok', count: rows.length, batches: rows });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 8.2 批次下的素材明细（含每张图当前归属的客户与"手动分类分人"状态）
app.get('/api/internal/batches/:batchId/assets', requireInternalKey, async (req, res) => {
    try {
        const batchId = String(req.params.batchId || '').trim();
        if (!batchId) return res.status(400).json({ status: 'error', message: 'batch_id is required' });
        const includeRejected = String(req.query.include_rejected || '') === '1';

        const batch = await dbGet("SELECT * FROM ingest_batches WHERE batch_id = ?", [batchId]);
        const conds = ['pa.batch_id = ?'];
        if (!includeRejected) conds.push('pa.is_rejected = 0');
        const assets = await dbAll(
            `SELECT pa.id, pa.batch_id, pa.filename, pa.preview_url, pa.sha256, pa.cluster_id,
                    pa.captured_at, pa.is_rejected, pa.reject_reason,
                    COALESCE(pa.source, 'auto') AS source, pa.imported_at, pa.manual_bound_at
             FROM photo_assets pa
             WHERE ${conds.join(' AND ')}
             ORDER BY pa.captured_at ASC, pa.id ASC`,
            [batchId]
        );
        const links = await dbAll(
            `SELECT abl.asset_id, b.id AS booking_id, b.cn, b.character, b.event_name, b.status, b.gallery_token
             FROM asset_booking_links abl
             INNER JOIN photo_assets pa ON pa.id = abl.asset_id
             LEFT JOIN bookings_v3 b ON b.id = abl.booking_id
             WHERE pa.batch_id = ?`,
            [batchId]
        );
        const byAsset = new Map();
        for (const l of links) {
            if (!byAsset.has(l.asset_id)) byAsset.set(l.asset_id, []);
            byAsset.get(l.asset_id).push(l);
        }
        const data = assets.map(a => ({
            ...a,
            booking_ids: (byAsset.get(a.id) || []).map(l => l.booking_id),
            bookings: byAsset.get(a.id) || []
        }));
        res.json({ status: 'ok', batch, assets: data });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 8.3 手动分类分人：把若干张图归属到某个客户（add 合影加人 / replace 换人 / remove 取消归属）
app.post('/api/internal/assets/bind-bookings', requireInternalKey, async (req, res) => {
    const body = req.body || {};
    const mode = ['add', 'replace', 'remove'].includes(body.mode) ? body.mode : 'replace';
    const assetIds = Array.isArray(body.asset_ids) ? body.asset_ids : [];
    try {
        const result = await applyAssetBookingLinks(assetIds, body.booking_id, mode);
        res.json({ status: 'ok', ...result });
    } catch (err) {
        res.status(err.statusCode || 500).json({ status: 'error', message: err.message });
    }
});

// ==================== 客户在线选图 H5 公开路由 ====================

app.get('/gallery/:token', (req, res) => {
    res.sendFile(path.join(__dirname, 'gallery.html'));
});

app.get('/api/gallery/:token', async (req, res) => {
    try {
        const token = String(req.params.token || '').trim();
        if (!token) {
            return res.status(400).json({ status: 'error', message: 'Invalid gallery token' });
        }

        const booking = await dbGet("SELECT * FROM bookings_v3 WHERE gallery_token = ?", [token]);
        if (!booking) {
            return res.status(404).json({ status: 'error', message: '选图链接无效或不存在' });
        }

        // 严格仅返回关联该 booking_id 且 is_rejected = 0 的有效预览图
        const assets = await dbAll(
            `SELECT pa.id, pa.batch_id, pa.filename, pa.preview_url, pa.cluster_id, pa.captured_at
             FROM photo_assets pa
             INNER JOIN asset_booking_links abl ON abl.asset_id = pa.id
             WHERE abl.booking_id = ? AND pa.is_rejected = 0
             ORDER BY pa.captured_at ASC, pa.id ASC`,
            [booking.id]
        );

        let parsedSelected = [];
        if (booking.selected_assets) {
            try {
                parsedSelected = JSON.parse(booking.selected_assets);
            } catch (_) {}
        }

        res.json({
            status: 'ok',
            booking: {
                id: booking.id,
                cn: booking.cn,
                character: booking.character,
                event_name: booking.event_name,
                date: booking.date,
                time: booking.time,
                quantity: booking.quantity,
                status: booking.status,
                locked: LOCKED_GALLERY_STATUSES.has(booking.status),
                selected_assets: parsedSelected,
                preset_code: booking.preset_code || null
            },
            presets: GALLERY_PRESETS,
            assets
        });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

app.post('/api/gallery/:token/select', async (req, res) => {
    try {
        const token = String(req.params.token || '').trim();
        const body = req.body || {};
        const rawIds = Array.isArray(body.asset_ids)
            ? body.asset_ids
            : (Array.isArray(body.selected_assets) ? body.selected_assets : null);
        const presetCode = typeof body.preset_code === 'string' ? body.preset_code.trim() : '';

        if (!rawIds || rawIds.length === 0) {
            return res.status(400).json({ status: 'error', message: '请至少选择 1 张照片后再提交' });
        }

        const uniqueAssetIds = [
            ...new Set(
                rawIds
                    .map(v => parseInt(v, 10))
                    .filter(v => Number.isInteger(v) && v > 0)
            )
        ];
        if (uniqueAssetIds.length === 0) {
            return res.status(400).json({ status: 'error', message: '所选照片 ID 无效' });
        }

        if (!presetCode || !ALLOWED_PRESET_CODES.has(presetCode)) {
            return res.status(400).json({
                status: 'error',
                message: `请选择有效的出片预设: ${[...ALLOWED_PRESET_CODES].join(', ')}`
            });
        }

        const result = await withImmediateTransaction(async () => {
            const booking = await dbGet("SELECT * FROM bookings_v3 WHERE gallery_token = ?", [token]);
            if (!booking) {
                const err = new Error('选图链接无效或不存在');
                err.statusCode = 404;
                throw err;
            }

            if (LOCKED_GALLERY_STATUSES.has(booking.status)) {
                const err = new Error('该订单已完成选图并锁定，无法重复修改；如需调整请联系摄影师。');
                err.statusCode = 409;
                throw err;
            }

            const validRows = await dbAll(
                `SELECT pa.id
                 FROM photo_assets pa
                 INNER JOIN asset_booking_links abl ON abl.asset_id = pa.id
                 WHERE abl.booking_id = ? AND pa.is_rejected = 0`,
                [booking.id]
            );
            const validIdSet = new Set(validRows.map(r => r.id));

            for (const aid of uniqueAssetIds) {
                if (!validIdSet.has(aid)) {
                    const err = new Error(`照片 ID ${aid} 不属于当前订单或已被过滤，无法提交`);
                    err.statusCode = 400;
                    throw err;
                }
            }

            await dbRun(
                "UPDATE bookings_v3 SET selected_assets = ?, preset_code = ?, status = 'SELECTED' WHERE id = ?",
                [JSON.stringify(uniqueAssetIds), presetCode, booking.id]
            );

            return {
                booking_id: booking.id,
                cn: booking.cn,
                character: booking.character,
                booking_status: 'SELECTED',
                selected_assets: uniqueAssetIds,
                preset_code: presetCode,
                locked: true
            };
        });

        const presetObj = GALLERY_PRESETS.find(p => p.code === presetCode);
        sendPushPlusNotification(
            '✨ 客户已完成选图锁单！',
            `CN: ${result.cn}\n角色: ${result.character}\n选中张数: ${result.selected_assets.length} 张\n出片预设: ${presetObj ? presetObj.name : presetCode}\n选中ID: ${result.selected_assets.join(', ')}`
        );

        // 选图成功后自动把订单推进到修图队列：
        // 订单状态已置为 SELECTED，retouch-engine 的 worker 会轮询到它并开始
        // XMP 调色 -> 16-bit TIFF -> PS 精修。这里只做「确认可被接管」，
        // 不阻塞客户页面；worker 不在线也不会影响本次选图结果。
        const autoRetouch = String(process.env.AUTO_RETOUCH_ON_SELECT || 'true').toLowerCase() !== 'false';
        let retouchDispatch = { queued: false, detail: 'AUTO_RETOUCH_ON_SELECT=false，已关闭自动精修' };
        if (autoRetouch) {
            try {
                retouchDispatch = await notifyRetouchQueue(result);
            } catch (err) {
                retouchDispatch = { queued: false, detail: `派发修图失败（不影响选图）: ${err.message}` };
                console.error('[auto-retouch] 派发失败:', err.message);
            }
        }

        res.json({ status: 'ok', ...result, retouch_dispatch: retouchDispatch });
    } catch (err) {
        const statusCode = err.statusCode || 500;
        res.status(statusCode).json({ status: 'error', message: err.message });
    }
});

/**
 * 选图后的自动精修派发。
 * retouch-engine 以守护模式轮询 /api/internal/retouch/pending（状态 = SELECTED），
 * 因此这里的「派发」本质是确认订单已进入可被 worker 拾取的状态，
 * 并在有 worker 心跳时返回预计处理信息。
 */
async function notifyRetouchQueue(selectionResult) {
    const pending = await dbGet(
        "SELECT COUNT(*) AS c FROM bookings_v3 WHERE status = 'SELECTED'"
    );
    return {
        queued: true,
        booking_id: selectionResult.booking_id,
        selected_assets: selectionResult.selected_assets.length,
        preset_code: selectionResult.preset_code,
        pending_selected_bookings: pending ? Number(pending.c) : 0,
        detail: '订单已进入待精修队列，修图 Worker 会在下个轮询周期（默认 15s）自动接管'
    };
}

// ==================== Sprint 4: 正式修图队列与终审门禁接口 ====================

async function maybeAdvanceBookingToReviewPending(bookingId) {
    const booking = await dbGet("SELECT * FROM bookings_v3 WHERE id = ?", [bookingId]);
    if (!booking) return null;

    let selectedIds = [];
    if (booking.selected_assets) {
        try {
            selectedIds = JSON.parse(booking.selected_assets);
        } catch (_) {}
    }
    selectedIds = [...new Set(selectedIds.map(v => parseInt(v, 10)).filter(v => Number.isInteger(v) && v > 0))];
    if (selectedIds.length === 0) return booking.status;

    const doneRows = await dbAll(
        "SELECT DISTINCT asset_id FROM retouch_jobs WHERE booking_id = ? AND stage = 'DONE'",
        [bookingId]
    );
    const doneSet = new Set(doneRows.map(r => r.asset_id));
    const allDone = selectedIds.every(aid => doneSet.has(aid));

    // 严禁跳过人工终审直接 DELIVERED：全部修完仅流转至 REVIEW_PENDING
    if (allDone && ['SELECTED', 'RETOUCHING'].includes(booking.status)) {
        await dbRun("UPDATE bookings_v3 SET status = 'REVIEW_PENDING' WHERE id = ?", [bookingId]);
        return 'REVIEW_PENDING';
    }
    if (!allDone && booking.status === 'SELECTED') {
        await dbRun("UPDATE bookings_v3 SET status = 'RETOUCHING' WHERE id = ?", [bookingId]);
        return 'RETOUCHING';
    }
    return booking.status;
}

// 1. 拉取所有 status = 'SELECTED'（及断点续跑的 RETOUCHING）待修图订单与 NAS 原片路径
app.get('/api/internal/retouch/pending', requireInternalKey, async (req, res) => {
    try {
        const rows = await dbAll(
            "SELECT * FROM bookings_v3 WHERE status IN ('SELECTED', 'RETOUCHING') ORDER BY id ASC"
        );
        const pendingBookings = [];

        for (const b of rows) {
            let selectedIds = [];
            if (b.selected_assets) {
                try {
                    selectedIds = JSON.parse(b.selected_assets);
                } catch (_) {}
            }
            selectedIds = [...new Set(selectedIds.map(v => parseInt(v, 10)).filter(v => Number.isInteger(v) && v > 0))];
            if (selectedIds.length === 0) continue;

            const placeholders = selectedIds.map(() => '?').join(',');
            const assetRows = await dbAll(
                `SELECT id, batch_id, filename, nas_raw_path, preview_url, sha256, captured_at
                 FROM photo_assets WHERE id IN (${placeholders})`,
                selectedIds
            );
            const assetMap = new Map(assetRows.map(a => [a.id, a]));

            const jobRows = await dbAll(
                "SELECT * FROM retouch_jobs WHERE booking_id = ? ORDER BY id DESC",
                [b.id]
            );
            const jobByAsset = new Map();
            for (const j of jobRows) {
                if (!jobByAsset.has(j.asset_id)) jobByAsset.set(j.asset_id, j);
            }

            const selectedAssetsDetail = [];
            let hasUnfinished = false;
            for (const aid of selectedIds) {
                const a = assetMap.get(aid);
                if (!a) continue;
                const existingJob = jobByAsset.get(aid) || null;
                if (!existingJob || existingJob.stage !== 'DONE') {
                    hasUnfinished = true;
                }
                selectedAssetsDetail.push({
                    asset_id: a.id,
                    batch_id: a.batch_id,
                    filename: a.filename,
                    nas_raw_path: a.nas_raw_path,
                    preview_url: a.preview_url,
                    sha256: a.sha256,
                    captured_at: a.captured_at,
                    existing_job: existingJob
                });
            }

            if (hasUnfinished && selectedAssetsDetail.length > 0) {
                const presetMeta = GALLERY_PRESETS.find(p => p.code === (b.preset_code || 'cyan_air_event'));
                pendingBookings.push({
                    id: b.id,
                    cn: b.cn,
                    character: b.character,
                    character_ref_img: b.character_ref_img || null,
                    event_name: b.event_name,
                    date: b.date,
                    time: b.time,
                    platform: b.platform,
                    im_user_id: b.im_user_id,
                    wechat: b.wechat,
                    qq: b.qq,
                    status: b.status,
                    preset_code: b.preset_code || 'cyan_air_event',
                    preset_name: presetMeta ? presetMeta.name : (b.preset_code || 'cyan_air_event'),
                    preset_desc: presetMeta ? presetMeta.desc : '',
                    gallery_token: b.gallery_token,
                    selected_assets: selectedAssetsDetail
                });
            }
        }

        res.json({ status: 'ok', count: pendingBookings.length, bookings: pendingBookings });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 1b. 重新触发某订单未完成/失败的精修任务（将订单重置回 SELECTED 并清理未 DONE 的旧任务记录）
app.post('/api/internal/retouch/bookings/:id/retry', requireInternalKey, async (req, res) => {
    try {
        const bookingId = parseInt(req.params.id, 10);
        if (!Number.isInteger(bookingId)) {
            return res.status(400).json({ status: 'error', message: 'bookingId 无效' });
        }
        await dbRun("DELETE FROM retouch_jobs WHERE booking_id = ? AND stage != 'DONE'", [bookingId]);
        await dbRun("UPDATE bookings_v3 SET status = 'SELECTED' WHERE id = ?", [bookingId]);
        res.json({ status: 'ok', booking_id: bookingId });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 2. 创建/重置单张修图任务
app.post('/api/internal/retouch/jobs', requireInternalKey, async (req, res) => {
    const body = req.body || {};
    const bookingId = parseInt(body.booking_id, 10);
    const assetId = parseInt(body.asset_id, 10);
    if (!Number.isInteger(bookingId) || !Number.isInteger(assetId)) {
        return res.status(400).json({ status: 'error', message: 'booking_id and asset_id are required integers' });
    }

    const presetCode = body.preset_code || 'cyan_air_event';
    const stage = body.stage || 'QUEUED';
    const geminiPlanJson = body.gemini_plan_json != null
        ? (typeof body.gemini_plan_json === 'string' ? body.gemini_plan_json : JSON.stringify(body.gemini_plan_json))
        : null;
    const outputPath = body.output_path || null;
    const previewOutUrl = body.preview_out_url || null;
    const psdPath = body.psd_path || null;
    const psdUrl = body.psd_url || null;
    const errorMsg = body.error_msg || null;

    try {
        const result = await withImmediateTransaction(async () => {
            const existing = await dbGet(
                "SELECT * FROM retouch_jobs WHERE booking_id = ? AND asset_id = ? ORDER BY id DESC LIMIT 1",
                [bookingId, assetId]
            );

            let jobId;
            if (existing) {
                jobId = existing.id;
                await dbRun(
                    `UPDATE retouch_jobs
                     SET preset_code = ?, stage = ?, gemini_plan_json = COALESCE(?, gemini_plan_json),
                         output_path = COALESCE(?, output_path), preview_out_url = COALESCE(?, preview_out_url),
                         psd_path = COALESCE(?, psd_path), psd_url = COALESCE(?, psd_url),
                         error_msg = ?, updated_at = CURRENT_TIMESTAMP
                     WHERE id = ?`,
                    [presetCode, stage, geminiPlanJson, outputPath, previewOutUrl, psdPath, psdUrl, errorMsg, jobId]
                );
            } else {
                const ins = await dbRun(
                    `INSERT INTO retouch_jobs (
                        booking_id, asset_id, preset_code, stage, gemini_plan_json, output_path,
                        preview_out_url, psd_path, psd_url, error_msg
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
                    [bookingId, assetId, presetCode, stage, geminiPlanJson, outputPath,
                        previewOutUrl, psdPath, psdUrl, errorMsg]
                );
                jobId = ins.lastID;
            }

            const bookingStatus = await maybeAdvanceBookingToReviewPending(bookingId);
            const job = await dbGet("SELECT * FROM retouch_jobs WHERE id = ?", [jobId]);
            return { job, booking_status: bookingStatus };
        });

        res.status(201).json({ status: 'ok', ...result });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 3. 更新单张修图任务进度；全部完成后自动将订单流转为 REVIEW_PENDING
app.patch('/api/internal/retouch/jobs/:id', requireInternalKey, async (req, res) => {
    const jobId = parseInt(req.params.id, 10);
    if (!Number.isInteger(jobId)) {
        return res.status(400).json({ status: 'error', message: 'Invalid job id' });
    }
    const body = req.body || {};

    try {
        const result = await withImmediateTransaction(async () => {
            const existing = await dbGet("SELECT * FROM retouch_jobs WHERE id = ?", [jobId]);
            if (!existing) {
                const err = new Error('Retouch job not found');
                err.statusCode = 404;
                throw err;
            }

            const updates = [];
            const params = [];
            if (body.stage !== undefined) {
                updates.push("stage = ?");
                params.push(String(body.stage));
            }
            if (body.preset_code !== undefined) {
                updates.push("preset_code = ?");
                params.push(String(body.preset_code));
            }
            if (body.gemini_plan_json !== undefined) {
                updates.push("gemini_plan_json = ?");
                params.push(
                    typeof body.gemini_plan_json === 'string'
                        ? body.gemini_plan_json
                        : JSON.stringify(body.gemini_plan_json)
                );
            }
            if (body.output_path !== undefined) {
                updates.push("output_path = ?");
                params.push(body.output_path);
            }
            if (body.preview_out_url !== undefined) {
                updates.push("preview_out_url = ?");
                params.push(body.preview_out_url);
            }
            if (body.psd_path !== undefined) {
                updates.push("psd_path = ?");
                params.push(body.psd_path);
            }
            if (body.psd_url !== undefined) {
                updates.push("psd_url = ?");
                params.push(body.psd_url);
            }
            if (body.review_passed !== undefined) {
                updates.push("review_passed = ?");
                params.push(body.review_passed ? 1 : 0);
            }
            if (body.error_msg !== undefined) {
                updates.push("error_msg = ?");
                params.push(body.error_msg);
            }

            updates.push("updated_at = CURRENT_TIMESTAMP");
            params.push(jobId);

            await dbRun(`UPDATE retouch_jobs SET ${updates.join(', ')} WHERE id = ?`, params);
            const bookingStatus = await maybeAdvanceBookingToReviewPending(existing.booking_id);
            const updatedJob = await dbGet("SELECT * FROM retouch_jobs WHERE id = ?", [jobId]);
            return { job: updatedJob, booking_status: bookingStatus };
        });

        res.json({ status: 'ok', ...result });
    } catch (err) {
        const statusCode = err.statusCode || 500;
        res.status(statusCode).json({ status: 'error', message: err.message });
    }
});

// ==================== 反馈修订：看图提意见 -> AI 再改 ====================

/** 读取某个精修任务的全部修订版本 */
app.get('/api/internal/retouch/jobs/:jobId/revisions', requireInternalKey, async (req, res) => {
    try {
        const jobId = parseInt(req.params.jobId, 10);
        if (!Number.isInteger(jobId)) {
            return res.status(400).json({ status: 'error', message: 'jobId 无效' });
        }
        const rows = await dbAll(
            "SELECT * FROM retouch_revisions WHERE job_id = ? ORDER BY version ASC",
            [jobId]
        );
        res.json({
            status: 'ok',
            count: rows.length,
            latest_version: rows.length ? rows[rows.length - 1].version : 0,
            revisions: rows.map(parseRevisionRow)
        });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

/** retouch-engine 修订完成后回写一条版本记录 */
app.post('/api/internal/retouch/jobs/:jobId/revisions', requireInternalKey, async (req, res) => {
    try {
        const jobId = parseInt(req.params.jobId, 10);
        if (!Number.isInteger(jobId)) {
            return res.status(400).json({ status: 'error', message: 'jobId 无效' });
        }
        const job = await dbGet("SELECT * FROM retouch_jobs WHERE id = ?", [jobId]);
        if (!job) return res.status(404).json({ status: 'error', message: '任务不存在' });

        const b = req.body || {};
        const version = parseInt(b.version, 10);
        if (!Number.isInteger(version) || version < 1) {
            return res.status(400).json({ status: 'error', message: 'version 必须是正整数' });
        }

        // 预览图要能通过 /outputs 静态路由访问：把绝对路径换成相对 URL
        const previewUrl = absoluteToOutputUrl(b.output_preview_path);

        const existing = await dbGet(
            "SELECT id FROM retouch_revisions WHERE job_id = ? AND version = ?",
            [jobId, version]
        );
        if (existing) {
            await dbRun(
                `UPDATE retouch_revisions
                 SET feedback_text = ?, summary = ?, confidence = ?, deltas_json = ?, jsx_used = ?,
                     output_psd_path = ?, output_preview_path = ?, preview_url = ?, warnings_json = ?
                 WHERE id = ?`,
                [
                    String(b.feedback_text || ''),
                    String(b.summary || ''),
                    Number(b.confidence) || 0,
                    JSON.stringify(b.deltas || {}),
                    b.jsx_used ? 1 : 0,
                    String(b.output_psd_path || ''),
                    String(b.output_preview_path || ''),
                    previewUrl,
                    JSON.stringify(b.warnings || []),
                    existing.id
                ]
            );
        } else {
            await dbRun(
                `INSERT INTO retouch_revisions
                 (job_id, version, feedback_text, summary, confidence, deltas_json, jsx_used,
                  output_psd_path, output_preview_path, preview_url, warnings_json)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
                [
                    jobId,
                    version,
                    String(b.feedback_text || ''),
                    String(b.summary || ''),
                    Number(b.confidence) || 0,
                    JSON.stringify(b.deltas || {}),
                    b.jsx_used ? 1 : 0,
                    String(b.output_psd_path || ''),
                    String(b.output_preview_path || ''),
                    previewUrl,
                    JSON.stringify(b.warnings || [])
                ]
            );
        }

        // 最新版本成为该任务的交付产物
        if (previewUrl || b.output_psd_path) {
            await dbRun(
                `UPDATE retouch_jobs
                 SET preview_out_url = COALESCE(?, preview_out_url),
                     output_path = COALESCE(?, output_path),
                     psd_path = COALESCE(?, psd_path),
                     updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?`,
                [
                    previewUrl || null,
                    b.output_psd_path || null,
                    b.output_psd_path || null,
                    jobId
                ]
            );
        }

        res.json({ status: 'ok', job_id: jobId, version });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

/**
 * 中控台/摄影师提交修订意见。
 * 这里只做「记录 + 标记待处理」，真正的 AI 修订由 retouch-engine 的 RevisionWorker 轮询执行，
 * 避免在 HTTP 请求里跑几分钟的 PS 操作。
 */
app.post('/api/retouch/jobs/:jobId/feedback', adminAuth, async (req, res) => {
    try {
        const jobId = parseInt(req.params.jobId, 10);
        if (!Number.isInteger(jobId)) {
            return res.status(400).json({ status: 'error', message: 'jobId 无效' });
        }
        const feedback = String((req.body && req.body.feedback) || '').trim();
        if (!feedback) {
            return res.status(400).json({ status: 'error', message: '请填写修改意见' });
        }
        if (feedback.length > 1000) {
            return res.status(400).json({ status: 'error', message: '修改意见过长（最多 1000 字）' });
        }

        const job = await dbGet("SELECT * FROM retouch_jobs WHERE id = ?", [jobId]);
        if (!job) return res.status(404).json({ status: 'error', message: '任务不存在' });
        if (job.stage !== 'DONE') {
            return res.status(409).json({
                status: 'error',
                message: `任务当前阶段为 ${job.stage}，只有已完成（DONE）的任务才能提修改意见`
            });
        }

        const latest = await dbGet(
            "SELECT MAX(version) AS v FROM retouch_revisions WHERE job_id = ?",
            [jobId]
        );
        const currentVersion = latest && latest.v ? Number(latest.v) : 0;

        // 同一条意见只允许有一条未处理记录，避免重复投递
        const pending = await dbGet(
            "SELECT id FROM retouch_revisions WHERE job_id = ? AND version = ? AND summary = '[待处理]'",
            [jobId, currentVersion + 1]
        );
        if (pending) {
            return res.status(409).json({
                status: 'error',
                message: '这条修改意见已在排队处理，请等待结果'
            });
        }

        const result = await dbRun(
            `INSERT INTO retouch_revisions
             (job_id, version, feedback_text, summary, confidence, deltas_json, jsx_used, warnings_json)
             VALUES (?, ?, ?, '[待处理]', 0, '{}', 0, '[]')`,
            [jobId, currentVersion + 1, feedback]
        );

        console.log(`[feedback] job=#${jobId} 待修订 v${currentVersion + 1}: ${feedback.slice(0, 60)}`);

        res.json({
            status: 'ok',
            job_id: jobId,
            current_version: currentVersion,
            target_version: currentVersion + 1,
            revision_id: result.lastID,
            message: '修改意见已提交，修图 Worker 会在下个轮询周期自动执行'
        });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

/** retouch-engine 标记某条修订失败（避免被无限重试） */
app.post('/api/internal/retouch/revisions/:revisionId/fail', requireInternalKey, async (req, res) => {
    try {
        const revisionId = parseInt(req.params.revisionId, 10);
        if (!Number.isInteger(revisionId)) {
            return res.status(400).json({ status: 'error', message: 'revisionId 无效' });
        }
        const row = await dbGet("SELECT * FROM retouch_revisions WHERE id = ?", [revisionId]);
        if (!row) return res.status(404).json({ status: 'error', message: '修订记录不存在' });

        const error = String((req.body && req.body.error) || '修订失败').slice(0, 500);
        const warnings = [`[失败] ${error}`];
        await dbRun(
            "UPDATE retouch_revisions SET summary = ?, warnings_json = ? WHERE id = ?",
            [`[失败] ${error.slice(0, 80)}`, JSON.stringify(warnings), revisionId]
        );
        console.log(`[feedback] 修订 #${revisionId} 失败: ${error.slice(0, 120)}`);
        res.json({ status: 'ok', revision_id: revisionId });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

/** 把 /outputs/... URL 反推为本机磁盘绝对路径（用于首次修订 v0 -> v1 时定位当前预览图） */
function outputUrlToAbsolute(previewUrl) {
    const raw = String(previewUrl || '').trim();
    if (!raw) return '';
    if (raw.startsWith('/outputs/')) {
        const rel = raw.slice('/outputs/'.length).replace(/^\/+/, '');
        const outRoot = (process.env.RETOUCH_OUTPUT_DIR || '').trim() || path.join(__dirname, 'uploads', 'outputs');
        return path.join(outRoot, ...rel.split('/'));
    }
    return raw;
}

/** 供 retouch-engine 拉取待修订意见 */
app.get('/api/internal/retouch/revisions/pending', requireInternalKey, async (req, res) => {    try {
        const rows = await dbAll(
            `SELECT rr.*, rj.booking_id, rj.asset_id, rj.preset_code, rj.psd_path, rj.output_path, rj.preview_out_url
             FROM retouch_revisions rr
             INNER JOIN retouch_jobs rj ON rj.id = rr.job_id
             WHERE rr.summary = '[待处理]'
             ORDER BY rr.id ASC
             LIMIT 20`
        );

        const items = [];
        for (const r of rows) {
            // 上一版的 PSD 与预览：用于在其基础上叠加；首次修订 (v0 -> v1) 回退到 retouch_jobs 的初始产物
            const prev = await dbGet(
                `SELECT * FROM retouch_revisions
                 WHERE job_id = ? AND version < ? AND summary != '[待处理]'
                 ORDER BY version DESC LIMIT 1`,
                [r.job_id, r.version]
            );
            const fallbackPsd = r.psd_path || (String(r.output_path || '').toLowerCase().endsWith('.psd') ? r.output_path : '') || r.output_path || '';
            const fallbackPreview = outputUrlToAbsolute(r.preview_out_url);
            items.push({
                revision_id: r.id,
                job_id: r.job_id,
                booking_id: r.booking_id,
                asset_id: r.asset_id,
                preset_code: r.preset_code || '',
                feedback_text: r.feedback_text || '',
                target_version: r.version,
                current_version: r.version - 1,
                current_psd_path: (prev && prev.output_psd_path) || fallbackPsd,
                current_preview_path: (prev && prev.output_preview_path) || fallbackPreview,
                previous_summary: (prev && prev.summary) || ''
            });
        }

        res.json({ status: 'ok', count: items.length, revisions: items });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

/** 把绝对路径转成 /outputs 可访问的 URL；不在输出目录下则返回空串 */
function absoluteToOutputUrl(absPath) {
    const raw = String(absPath || '').trim();
    if (!raw) return '';
    const normalized = raw.replace(/\\/g, '/');
    const marker = '/uploads/outputs/';
    const idx = normalized.indexOf(marker);
    if (idx >= 0) return '/outputs/' + normalized.slice(idx + marker.length);
    if (normalized.startsWith('/outputs/')) return normalized;
    return '';
}

function parseRevisionRow(row) {
    let deltas = {};
    let warnings = [];
    try { deltas = JSON.parse(row.deltas_json || '{}'); } catch (_) { /* 忽略 */ }
    try { warnings = JSON.parse(row.warnings_json || '[]'); } catch (_) { /* 忽略 */ }
    return {
        id: row.id,
        job_id: row.job_id,
        version: row.version,
        feedback_text: row.feedback_text,
        summary: row.summary,
        confidence: row.confidence,
        deltas,
        jsx_used: Boolean(row.jsx_used),
        output_psd_path: row.output_psd_path,
        preview_url: row.preview_url,
        warnings,
        pending: row.summary === '[待处理]',
        created_at: row.created_at
    };
}

// 4. 摄影师终审队列 API（受 adminAuth 保护）
app.get('/api/admin/review-queue', adminAuth, async (req, res) => {
    try {
        const bookings = await dbAll(
            `SELECT * FROM bookings_v3
             WHERE status IN ('REVIEW_PENDING', 'DELIVERED')
             ORDER BY CASE WHEN status = 'REVIEW_PENDING' THEN 0 ELSE 1 END, id DESC`
        );

        const enriched = [];
        for (const b of bookings) {
            const jobs = await dbAll(
                `SELECT rj.*, pa.filename AS source_filename, pa.preview_url AS source_preview_url
                 FROM retouch_jobs rj
                 LEFT JOIN photo_assets pa ON pa.id = rj.asset_id
                 WHERE rj.booking_id = ?
                 ORDER BY rj.id ASC`,
                [b.id]
            );
            const parsedJobs = [];
            for (const j of jobs) {
                let geminiPlan = null;
                if (j.gemini_plan_json) {
                    try {
                        geminiPlan = JSON.parse(j.gemini_plan_json);
                    } catch (_) {}
                }
                const revRows = await dbAll(
                    "SELECT * FROM retouch_revisions WHERE job_id = ? ORDER BY version ASC",
                    [j.id]
                );
                const revisions = revRows.map(parseRevisionRow);
                const hasPendingRev = revisions.some(r => r.pending);
                const latestVersion = revisions.length ? revisions[revisions.length - 1].version : 0;
                parsedJobs.push({
                    ...j,
                    gemini_plan: geminiPlan,
                    revisions,
                    latest_version: latestVersion,
                    has_pending_revision: hasPendingRev
                });
            }

            enriched.push({
                id: b.id,
                cn: b.cn,
                character: b.character,
                event_name: b.event_name,
                date: b.date,
                time: b.time,
                platform: b.platform,
                im_user_id: b.im_user_id,
                wechat: b.wechat,
                qq: b.qq,
                preset_code: b.preset_code,
                status: b.status,
                gallery_token: b.gallery_token,
                delivery_url: b.delivery_url,
                jobs: parsedJobs
            });
        }

        const pendingCount = enriched.filter(b => b.status === 'REVIEW_PENDING').length;
        res.json({ status: 'ok', count: enriched.length, pending_count: pendingCount, bookings: enriched });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

// 5. 摄影师一键放行交付（受 adminAuth 保护）：更新 DELIVERED + 自动回调 im-gateway 推送成片链接
app.post('/api/admin/bookings/:id/approve-delivery', adminAuth, async (req, res) => {
    const bookingId = parseInt(req.params.id, 10);
    if (!Number.isInteger(bookingId)) {
        return res.status(400).json({ status: 'error', message: 'Invalid booking id' });
    }

    try {
        const publicBase = getPublicBaseUrl(req);
        const updated = await withImmediateTransaction(async () => {
            const booking = await dbGet("SELECT * FROM bookings_v3 WHERE id = ?", [bookingId]);
            if (!booking) {
                const err = new Error('Booking not found');
                err.statusCode = 404;
                throw err;
            }
            if (!['REVIEW_PENDING', 'DELIVERED'].includes(booking.status)) {
                const err = new Error(`订单当前状态为 ${booking.status}，尚未进入 REVIEW_PENDING，无法放行交付`);
                err.statusCode = 409;
                throw err;
            }

            const doneJobs = await dbAll(
                "SELECT * FROM retouch_jobs WHERE booking_id = ? AND stage = 'DONE' ORDER BY id ASC",
                [bookingId]
            );
            if (doneJobs.length === 0) {
                const err = new Error('该订单暂无已完成的精修成片，无法放行交付');
                err.statusCode = 400;
                throw err;
            }

            const token = booking.gallery_token || generateGalleryToken();
            const deliveryPath = `/delivery/${token}`;
            const deliveryUrl = `${publicBase}${deliveryPath}`;

            await dbRun(
                "UPDATE retouch_jobs SET review_passed = 1, updated_at = CURRENT_TIMESTAMP WHERE booking_id = ? AND stage = 'DONE'",
                [bookingId]
            );
            await dbRun(
                "UPDATE bookings_v3 SET status = 'DELIVERED', gallery_token = ?, delivery_url = ? WHERE id = ?",
                [token, deliveryUrl, bookingId]
            );

            return {
                booking: { ...booking, status: 'DELIVERED', gallery_token: token, delivery_url: deliveryUrl },
                delivery_path: deliveryPath,
                delivery_url: deliveryUrl,
                output_count: doneJobs.length
            };
        });

        // 回调 im-gateway 将成片链接发回客户微信/QQ/企微
        let notifyResult = { attempted: false, ok: false, detail: 'No IM target bound' };
        const b = updated.booking;
        const targetPlatform = b.platform && ['qq', 'wechat', 'wecom_kf'].includes(b.platform)
            ? b.platform
            : (b.qq ? 'qq' : (b.wechat ? 'wechat' : null));
        const targetUserId = b.im_user_id || (targetPlatform === 'qq' ? b.qq : b.wechat);

        if (targetPlatform && targetUserId && IM_GATEWAY_URL) {
            notifyResult.attempted = true;
            try {
                const gwResp = await fetch(`${IM_GATEWAY_URL}/api/gateway/notify-delivery`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-API-Key': process.env.INTERNAL_API_KEY || ''
                    },
                    body: JSON.stringify({
                        platform: targetPlatform,
                        user_id: String(targetUserId),
                        booking_id: b.id,
                        cn: b.cn || '',
                        character: b.character || '',
                        event_name: b.event_name || '',
                        gallery_token: b.gallery_token || '',
                        delivery_url: updated.delivery_url,
                        output_count: updated.output_count
                    })
                });
                const gwData = await gwResp.json().catch(() => ({}));
                notifyResult = {
                    attempted: true,
                    ok: gwResp.ok,
                    status_code: gwResp.status,
                    response: gwData
                };
            } catch (notifyErr) {
                notifyResult = {
                    attempted: true,
                    ok: false,
                    error: notifyErr.message
                };
            }
        }

        res.json({
            status: 'ok',
            booking_id: b.id,
            booking_status: 'DELIVERED',
            delivery_url: updated.delivery_url,
            delivery_path: updated.delivery_path,
            output_count: updated.output_count,
            notify_result: notifyResult
        });
    } catch (err) {
        const statusCode = err.statusCode || 500;
        res.status(statusCode).json({ status: 'error', message: err.message });
    }
});

// 6. 客户成片交付查收公开接口（仅展示已放行 review_passed = 1 且订单为 DELIVERED 的成片）
app.get('/api/delivery/:token', async (req, res) => {
    try {
        const token = String(req.params.token || '').trim();
        const booking = await dbGet("SELECT * FROM bookings_v3 WHERE gallery_token = ?", [token]);
        if (!booking || booking.status !== 'DELIVERED') {
            return res.status(404).json({ status: 'error', message: '成片尚未放行或链接无效' });
        }
        const outputs = await dbAll(
            `SELECT rj.id, rj.asset_id, rj.preset_code, rj.preview_out_url, rj.psd_url, rj.updated_at, pa.filename AS source_filename
             FROM retouch_jobs rj
             LEFT JOIN photo_assets pa ON pa.id = rj.asset_id
             WHERE rj.booking_id = ? AND rj.stage = 'DONE' AND rj.review_passed = 1
             ORDER BY rj.id ASC`,
            [booking.id]
        );
        res.json({
            status: 'ok',
            booking: {
                id: booking.id,
                cn: booking.cn,
                character: booking.character,
                event_name: booking.event_name,
                date: booking.date,
                preset_code: booking.preset_code,
                status: booking.status,
                delivery_url: booking.delivery_url
            },
            outputs
        });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

app.get('/delivery/:token', async (req, res) => {
    const token = String(req.params.token || '').trim();
    const booking = await dbGet("SELECT * FROM bookings_v3 WHERE gallery_token = ?", [token]).catch(() => null);
    if (!booking || booking.status !== 'DELIVERED') {
        return res.status(404).send('<h3>成片尚未放行交付或链接无效</h3>');
    }
    const outputs = await dbAll(
        "SELECT id, preview_out_url FROM retouch_jobs WHERE booking_id = ? AND stage = 'DONE' AND review_passed = 1 ORDER BY id ASC",
        [booking.id]
    ).catch(() => []);
    const cards = outputs.map((o, i) => `
        <div style="background:#1e222b;border-radius:14px;overflow:hidden;border:1px solid rgba(255,255,255,.08)">
            <img src="${o.preview_out_url}" style="width:100%;display:block" />
            <div style="padding:10px 14px;display:flex;justify-content:space-between;align-items:center;font-size:12px">
                <span>精修成片 #${i + 1}</span>
                <a href="${o.preview_out_url}" download style="color:#6fa8a5;font-weight:bold;text-decoration:none">下载 JPG ↓</a>
                ${o.psd_url ? `<a href="${o.psd_url}" download style="color:#c4c698;font-weight:bold;text-decoration:none;margin-left:10px">下载 PSD ↓</a>` : ''}
            </div>
        </div>`).join('');
    res.send(`<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>ZERO STUDIO | 精修成片交付</title></head>
        <body style="margin:0;padding:24px;background:#161a21;color:#eef0f4;font-family:sans-serif">
        <div style="max-width:960px;margin:0 auto">
            <h2 style="margin:0 0 6px;color:#6fa8a5">ZERO STUDIO · 精修成片交付</h2>
            <p style="margin:0 0 20px;color:#9ca3af;font-size:13px">${booking.cn || ''} · ${booking.character || ''} · ${booking.event_name || ''}（共 ${outputs.length} 张）</p>
            <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px">${cards}</div>
        </div></body></html>`);
});

// 7. 未打板未绑定角色簇查询与手动绑定（供中控台面板 3 使用）
app.get('/api/internal/clusters/unassigned', requireInternalKey, async (req, res) => {
    try {
        const rows = await dbAll(
            `SELECT pa.batch_id, pa.cluster_id, COUNT(*) AS asset_count,
                    MIN(pa.preview_url) AS sample_preview_url,
                    MIN(pa.captured_at) AS first_captured_at
             FROM photo_assets pa
             LEFT JOIN asset_booking_links abl ON abl.asset_id = pa.id
             WHERE pa.is_rejected = 0 AND abl.asset_id IS NULL
             GROUP BY pa.batch_id, pa.cluster_id
             ORDER BY first_captured_at DESC`
        );
        res.json({ status: 'ok', clusters: rows });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

app.post('/api/internal/clusters/bind', requireInternalKey, async (req, res) => {
    const body = req.body || {};
    const batchId = String(body.batch_id || '').trim();
    const clusterId = body.cluster_id != null ? String(body.cluster_id) : '';
    const bookingId = parseInt(body.booking_id, 10);
    if (!batchId || !Number.isInteger(bookingId)) {
        return res.status(400).json({ status: 'error', message: 'batch_id and booking_id are required' });
    }
    try {
        const publicBase = getPublicBaseUrl(req);
        const result = await withImmediateTransaction(async () => {
            const booking = await dbGet("SELECT * FROM bookings_v3 WHERE id = ?", [bookingId]);
            if (!booking) {
                const err = new Error('Booking not found');
                err.statusCode = 404;
                throw err;
            }
            const assets = clusterId
                ? await dbAll(
                    "SELECT id FROM photo_assets WHERE batch_id = ? AND cluster_id = ? AND is_rejected = 0",
                    [batchId, clusterId]
                )
                : await dbAll(
                    "SELECT id FROM photo_assets WHERE batch_id = ? AND (cluster_id IS NULL OR cluster_id = '') AND is_rejected = 0",
                    [batchId]
                );
            for (const a of assets) {
                await dbRun(
                    "INSERT OR IGNORE INTO asset_booking_links (asset_id, booking_id) VALUES (?, ?)",
                    [a.id, bookingId]
                );
            }
            const token = booking.gallery_token || generateGalleryToken();
            const nextStatus = ['BOOKED', 'SHOT', 'CLUSTERED', null, ''].includes(booking.status)
                ? 'CLUSTERED'
                : booking.status;
            await dbRun(
                "UPDATE bookings_v3 SET status = ?, gallery_token = ? WHERE id = ?",
                [nextStatus, token, bookingId]
            );
            return {
                bound_assets: assets.length,
                booking_id: bookingId,
                status: nextStatus,
                gallery_token: token,
                gallery_url: `${publicBase}/gallery/${token}`
            };
        });
        res.json({ status: 'ok', ...result });
    } catch (err) {
        const statusCode = err.statusCode || 500;
        res.status(statusCode).json({ status: 'error', message: err.message });
    }
});

// ==================== 原有后台管理与留言接口 ====================

app.get('/api/bookings', adminAuth, (req, res) => db.all("SELECT * FROM bookings_v3 ORDER BY id DESC", (err, rows) => res.json(rows || [])));
app.post('/api/bookings/delete', adminAuth, (req, res) => db.run("DELETE FROM bookings_v3 WHERE id = ?", [req.body.id], () => res.json({ status: 'ok' })));
app.post('/api/bookings/edit', adminAuth, async (req, res) => {
    const { id, wechat, cn, character, date, time, deposit, quantity, remarks, event_name } = req.body || {};
    if (id == null) return res.status(400).json({ status: 'error', message: '缺少订单 id' });
    try {
        const existing = await dbGet("SELECT * FROM bookings_v3 WHERE id = ?", [id]);
        if (!existing) return res.status(404).json({ status: 'error', message: '订单不存在' });

        // 锁档 / 已交付订单同样允许修改（摄影师有权更正任何档期信息）
        const nextDate = date !== undefined && String(date).trim() ? String(date).trim() : existing.date;
        if (!nextDate) return res.status(400).json({ status: 'error', message: '拍摄日期不能为空' });

        const next = {
            wechat: wechat !== undefined ? wechat : existing.wechat,
            cn: cn !== undefined ? cn : existing.cn,
            character: character !== undefined ? character : existing.character,
            event_name: event_name !== undefined ? event_name : existing.event_name,
            date: nextDate,
            time: time !== undefined ? time : existing.time,
            deposit: deposit !== undefined ? deposit : existing.deposit,
            quantity: quantity !== undefined ? quantity : existing.quantity,
            remarks: remarks !== undefined ? remarks : existing.remarks
        };

        await dbRun(
            "UPDATE bookings_v3 SET wechat=?, cn=?, character=?, event_name=?, date=?, time=?, deposit=?, quantity=?, remarks=? WHERE id=?",
            [next.wechat, next.cn, next.character, next.event_name, next.date, next.time, next.deposit, next.quantity, next.remarks, id]
        );

        // 档期一旦变化，重写二维码内容，保证客户手上的二维码与最新信息一致
        const row = await dbGet("SELECT data FROM portal_config WHERE id = 1");
        let config = {};
        if (row && row.data) {
            try {
                config = JSON.parse(row.data);
            } catch (_) {}
        }
        const updated = await dbGet("SELECT * FROM bookings_v3 WHERE id = ?", [id]);
        const payload = buildBookingQrPayload(updated, config, getPublicBaseUrl(req));
        await dbRun("UPDATE bookings_v3 SET slate_qr_payload = ? WHERE id = ?", [payload, id]);

        res.json({ status: 'ok', booking: { ...updated, slate_qr_payload: payload } });
    } catch (err) {
        res.status(500).json({ status: 'error', message: err.message });
    }
});

app.post('/api/messages', (req, res) => {
    const { name, contact, content } = req.body || {};
    db.run("INSERT INTO messages (name, contact, content) VALUES (?, ?, ?)", [name, contact, content], function (err) {
        if (err) return res.status(500).json({ error: err.message });
        sendPushPlusNotification('💬 网页咨询留言', `称呼: ${name}\n联系: ${contact}\n内容: ${content}`);
        res.json({ status: 'ok' });
    });
});

app.get('/api/messages', adminAuth, (req, res) => db.all("SELECT * FROM messages ORDER BY id DESC", (err, rows) => res.json(rows || [])));

const BIND_HOST = process.env.BIND_HOST || '127.0.0.1';

initDb()
    .then(() => {
        app.listen(PORT, BIND_HOST, () => console.log(`Node Server is running on http://${BIND_HOST}:${PORT}`));
    })
    .catch(err => {
        console.error('Failed to initialize database:', err);
        process.exit(1);
    });
