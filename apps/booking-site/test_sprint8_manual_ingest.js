/**
 * Sprint 8 自动化测试：插卡拷卡「手动导入 + 手动分类分人」后端接口。
 *
 * 覆盖：
 *  1) /api/internal/batches 批次列表（来源 auto/manual、有效张数、已分人张数、未分配张数）
 *  2) /api/internal/batches/:batchId/assets 素材明细（每张图的归属客户）
 *  3) /api/internal/assets/bind-bookings 手动分人：replace 换人 / add 合影加人 / remove 取消归属
 *  4) 关键回归：手动分人后重跑聚类回写（POST /api/internal/batches）不得清空人工归属
 *  5) source=manual 的批次来源标记、手动绑定时间戳、参数校验与鉴权
 */

const assert = require('assert');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const TEST_PORT = 3461;
const BASE = `http://127.0.0.1:${TEST_PORT}`;
const API_KEY = 'sprint8-manual-ingest-key';
const TEST_DB = path.join(__dirname, 'test_sprint8_manual.db');
const PREVIEW_DIR = path.join(__dirname, 'test_sprint8_previews');
const H = { 'X-API-Key': API_KEY, 'Content-Type': 'application/json' };

function cleanup() {
    for (const s of ['', '-wal', '-shm']) {
        try { fs.unlinkSync(TEST_DB + s); } catch (_) {}
    }
    try { fs.rmSync(PREVIEW_DIR, { recursive: true, force: true }); } catch (_) {}
}

function startServer() {
    return new Promise((resolve, reject) => {
        const child = spawn(process.execPath, [path.join(__dirname, 'server.js')], {
            cwd: __dirname,
            env: {
                ...process.env,
                PORT: String(TEST_PORT),
                DB_PATH: TEST_DB,
                PREVIEW_OUTPUT_DIR: PREVIEW_DIR,
                INTERNAL_API_KEY: API_KEY,
                ADMIN_USER: 'admin',
                ADMIN_PASSWORD: 'test-password',
                PUSHPLUS_TOKEN: ''
            },
            stdio: ['ignore', 'pipe', 'pipe']
        });
        let out = '', err = '';
        const t = setTimeout(() => { child.kill(); reject(new Error(`startup timeout out=${out} err=${err}`)); }, 15000);
        child.stdout.on('data', c => { out += c; if (out.includes('Node Server is running')) { clearTimeout(t); resolve(child); } });
        child.stderr.on('data', c => { err += c; });
        child.on('exit', code => { clearTimeout(t); reject(new Error(`exited ${code} ${err}`)); });
    });
}

async function js(url, opts = {}) {
    const res = await fetch(BASE + url, opts);
    const ct = res.headers.get('content-type') || '';
    const body = ct.includes('json') ? await res.json() : await res.text();
    return { status: res.status, body };
}

const post = (url, body) => js(url, { method: 'POST', headers: H, body: JSON.stringify(body) });
const get = (url) => js(url, { headers: H });

async function run() {
    let proc;
    try {
        cleanup();
        proc = await startServer();
        console.log(`[start] ${BASE}`);

        // ---------- 准备两张订单 ----------
        const b1 = await post('/api/internal/bookings', { cn: '小雪', character: '芙宁娜', event_name: 'BACC', date: '2026-11-20', time: '10:00-12:00', platform: 'internal' });
        const b2 = await post('/api/internal/bookings', { cn: '阿乐', character: '雷神', event_name: 'BACC', date: '2026-11-20', time: '14:00-16:00', platform: 'internal' });
        assert.strictEqual(b1.status, 201, JSON.stringify(b1.body));
        assert.strictEqual(b2.status, 201, JSON.stringify(b2.body));
        const id1 = b1.body.booking_id, id2 = b2.body.booking_id;

        // ---------- 1) 自动拷卡批次回写 ----------
        const batchId = '20261120_BACC_AUTO_1';
        const makeAssets = () => ([
            { filename: 'A001.jpg', preview_url: `/previews/${batchId}/A001.webp`, sha256: 'h1', cluster_id: `qr_booking_${id1}`, captured_at: '2026-11-20 10:00:00', booking_ids: [id1] },
            { filename: 'A002.jpg', preview_url: `/previews/${batchId}/A002.webp`, sha256: 'h2', cluster_id: null, captured_at: '2026-11-20 10:05:00', booking_ids: [] },
            { filename: 'A003.jpg', preview_url: `/previews/${batchId}/A003.webp`, sha256: 'h3', cluster_id: null, captured_at: '2026-11-20 10:09:00', booking_ids: [] }
        ]);
        const payload = { batch_id: batchId, camera_model: 'A7M4', event_name: 'BACC', nas_path: 'X', total_files: 3, verified_files: 3, source: 'auto', assets: makeAssets() };
        const r1 = await post('/api/internal/batches', payload);
        assert.strictEqual(r1.status, 201, JSON.stringify(r1.body));
        assert.strictEqual(r1.body.inserted_assets, 3);
        assert.strictEqual(r1.body.total_links, 1);
        console.log('[PASS] 1) 自动拷卡批次回写成功（3 张图 / 1 条二维码锚定关联）');

        // ---------- 2) 批次列表统计 ----------
        let bl = await get('/api/internal/batches');
        assert.strictEqual(bl.status, 200);
        const auto = bl.body.batches.find(b => b.batch_id === batchId);
        assert.ok(auto, '批次应出现在列表中');
        assert.strictEqual(auto.source, 'auto');
        assert.strictEqual(auto.valid_asset_count, 3);
        assert.strictEqual(auto.linked_asset_count, 1);
        assert.strictEqual(auto.unassigned_asset_count, 2);
        assert.strictEqual(auto.booking_count, 1);
        console.log('[PASS] 2) 批次列表返回来源/有效张数/已分人张数/未分配张数');

        // ---------- 3) 素材明细 ----------
        let al = await get(`/api/internal/batches/${batchId}/assets`);
        assert.strictEqual(al.status, 200);
        assert.strictEqual(al.body.assets.length, 3);
        const byName = Object.fromEntries(al.body.assets.map(a => [a.filename, a]));
        assert.deepStrictEqual(byName['A001.jpg'].booking_ids, [id1]);
        assert.deepStrictEqual(byName['A002.jpg'].booking_ids, []);
        assert.strictEqual(byName['A001.jpg'].source, 'auto');
        assert.strictEqual(byName['A002.jpg'].bookings.length, 0);
        console.log('[PASS] 3) 素材明细返回每张图的当前归属客户');

        // ---------- 4) 手动分人 replace ----------
        const idA2 = byName['A002.jpg'].id, idA3 = byName['A003.jpg'].id, idA1 = byName['A001.jpg'].id;
        const rb = await post('/api/internal/assets/bind-bookings', { asset_ids: [idA2, idA3], booking_id: id2, mode: 'replace' });
        assert.strictEqual(rb.status, 200, JSON.stringify(rb.body));
        assert.strictEqual(rb.body.affected, 2);
        assert.ok(rb.body.batch_ids.includes(batchId));

        al = await get(`/api/internal/batches/${batchId}/assets`);
        const map2 = Object.fromEntries(al.body.assets.map(a => [a.filename, a]));
        assert.deepStrictEqual(map2['A002.jpg'].booking_ids, [id2]);
        assert.deepStrictEqual(map2['A003.jpg'].booking_ids, [id2]);
        assert.ok(map2['A002.jpg'].manual_bound_at, '手动分人应写入 manual_bound_at 时间戳');
        assert.strictEqual(map2['A001.jpg'].manual_bound_at, null, '未手动处理的图不应有手动时间戳');
        console.log('[PASS] 4) 手动分人 replace：2 张图换给新客户并打上手动归属时间');

        // ---------- 5) 回归：重跑聚类回写不得清空人工归属 ----------
        const rerun = await post('/api/internal/batches', { ...payload, assets: makeAssets() });
        assert.strictEqual(rerun.status, 201, JSON.stringify(rerun.body));
        assert.strictEqual(rerun.body.kept_manual_links, 2, '重跑应保留 2 条人工归属');

        al = await get(`/api/internal/batches/${batchId}/assets`);
        const map3 = Object.fromEntries(al.body.assets.map(a => [a.filename, a]));
        assert.deepStrictEqual(map3['A002.jpg'].booking_ids, [id2], '重跑聚类后人工分人结果必须保留');
        assert.deepStrictEqual(map3['A003.jpg'].booking_ids, [id2], '重跑聚类后人工分人结果必须保留');
        assert.ok(map3['A002.jpg'].manual_bound_at, '重跑后 manual_bound_at 应保留');
        assert.deepStrictEqual(map3['A001.jpg'].booking_ids, [id1], '自动锚定结果不受影响');
        console.log('[PASS] 5) 关键回归：手动分人后重跑聚类，人工归属与时间戳完整保留（不再被清空）');

        // ---------- 6) add 合影加人 / remove 取消归属 ----------
        const idNow2 = map3['A002.jpg'].id;
        const radd = await post('/api/internal/assets/bind-bookings', { asset_ids: [idNow2], booking_id: id1, mode: 'add' });
        assert.strictEqual(radd.status, 200);
        al = await get(`/api/internal/batches/${batchId}/assets`);
        const a2 = al.body.assets.find(a => a.id === idNow2);
        assert.deepStrictEqual([...a2.booking_ids].sort((x, y) => x - y), [id1, id2].sort((x, y) => x - y), 'add 模式应保留原归属并追加新客户');
        assert.strictEqual(a2.bookings.length, 2);

        const rrem = await post('/api/internal/assets/bind-bookings', { asset_ids: [idNow2], mode: 'remove' });
        assert.strictEqual(rrem.status, 200);
        al = await get(`/api/internal/batches/${batchId}/assets`);
        const a2b = al.body.assets.find(a => a.id === idNow2);
        assert.deepStrictEqual(a2b.booking_ids, [], 'remove 应清空该图全部归属');
        console.log('[PASS] 6) add（合影加人）与 remove（取消归属）两种手动模式正确');

        // ---------- 7) 手动导入批次：source=manual ----------
        const manualBatch = '20261120__MANUAL_XIAOXUE_BACC_AB12';
        const manualAssets = [
            { filename: 'M001.png', preview_url: `/previews/${manualBatch}/M001.webp`, sha256: 'm1', cluster_id: 'manual', captured_at: '2026-11-20 11:00:00', booking_ids: [id1], source: 'manual' },
            { filename: 'M002.png', preview_url: `/previews/${manualBatch}/M002.webp`, sha256: 'm2', cluster_id: null, captured_at: '2026-11-20 11:01:00', booking_ids: [], source: 'manual' }
        ];
        const rm = await post('/api/internal/batches', {
            batch_id: manualBatch, camera_model: '手动导入', event_name: 'BACC', nas_path: '/tmp',
            total_files: 2, verified_files: 2, source: 'manual', imported_at: '2026-11-20 11:02:00', assets: manualAssets
        });
        assert.strictEqual(rm.status, 201, JSON.stringify(rm.body));
        bl = await get('/api/internal/batches?source=manual');
        assert.ok(bl.body.batches.length === 1, 'source=manual 过滤应只返回手动导入批次');
        assert.strictEqual(bl.body.batches[0].batch_id, manualBatch);
        const ml = await get(`/api/internal/batches/${manualBatch}/assets`);
        assert.strictEqual(ml.body.assets.length, 2);
        assert.strictEqual(ml.body.assets[0].source, 'manual');
        assert.ok(ml.body.assets[0].imported_at, '手动导入批次应记录 imported_at');
        console.log('[PASS] 7) 手动导入批次按照 source=manual 独立归类，与自动拷卡批次可区分');

        // ---------- 8) 客户选图短链只返回真实归属的图 ----------
        const tok = (await get(`/api/internal/bookings/${id1}`)).body.booking.gallery_token || b1.body.gallery_token;
        const g1 = await js(`/api/gallery/${tok}`);
        assert.strictEqual(g1.status, 200);
        const gFiles = g1.body.assets.map(a => a.filename).sort();
        assert.ok(gFiles.includes('A001.jpg'), '客户 1 应能看到二维码锚定的 A001');
        assert.ok(!gFiles.includes('A003.jpg'), '客户 1 不应看到已被人工换给客户 2 的 A003');
        console.log('[PASS] 8) 手动分人后客户选图短链内容同步更新（不串图）');

        // ---------- 9) 参数校验与鉴权 ----------
        assert.strictEqual((await post('/api/internal/assets/bind-bookings', { asset_ids: [], booking_id: id1 })).status, 400);
        assert.strictEqual((await post('/api/internal/assets/bind-bookings', { asset_ids: [idA1], booking_id: 99999 })).status, 404);
        assert.strictEqual((await post('/api/internal/assets/bind-bookings', { asset_ids: [99999], booking_id: id1 })).status, 404);
        assert.strictEqual((await js('/api/internal/batches')).status, 401, '无 X-API-Key 必须被拒绝');
        assert.strictEqual((await js(`/api/internal/batches/${batchId}/assets`)).status, 401);
        console.log('[PASS] 9) 空选择/不存在订单/不存在素材/缺失密钥均被正确拒绝');

        console.log('\n========================================================');
        console.log('✅ test_sprint8_manual_ingest.js 全部 9 项断言通过');
        console.log('========================================================');
    } finally {
        if (proc) proc.kill();
        await new Promise(r => setTimeout(r, 300));
        cleanup();
    }
}

run().catch(err => {
    console.error('[FAIL]', err && err.stack ? err.stack : err);
    process.exit(1);
});
