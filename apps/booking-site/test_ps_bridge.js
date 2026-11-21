const assert = require('assert');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const PORT = 3461;
const BASE = `http://127.0.0.1:${PORT}`;
const KEY = 'ps-bridge-test-key';
const DB = path.join(__dirname, 'test_psbridge.db');

function cleanup() {
    for (const s of ['', '-wal', '-shm']) {
        try { fs.unlinkSync(DB + s); } catch (_) {}
    }
}

function startServer() {
    return new Promise((resolve, reject) => {
        const child = spawn(process.execPath, [path.join(__dirname, 'server.js')], {
            cwd: __dirname,
            env: { ...process.env, PORT: String(PORT), DB_PATH: DB, INTERNAL_API_KEY: KEY,
                   ADMIN_USER: 'admin', ADMIN_PASSWORD: 'pw', PUSHPLUS_TOKEN: '' },
            stdio: ['ignore', 'pipe', 'pipe']
        });
        let out = '', err = '';
        const t = setTimeout(() => { child.kill(); reject(new Error('timeout ' + out + err)); }, 15000);
        child.stdout.on('data', c => { out += c; if (out.includes('Node Server is running')) { clearTimeout(t); resolve(child); } });
        child.stderr.on('data', c => { err += c; });
        child.on('exit', code => { clearTimeout(t); reject(new Error(`exit ${code} ${err}`)); });
    });
}

// 模拟 PS 插件：轮询拉任务 -> 回传结果
async function fakePhotoshopPlugin(durationMs) {
    const deadline = Date.now() + durationMs;
    let handled = 0;
    while (Date.now() < deadline) {
        // PS 插件必须带共享密钥，否则服务端 401（安全加固后）
        const r = await fetch(`${BASE}/api/ps-bridge/pending`, { headers: { 'X-API-Key': KEY } });
        const data = await r.json();
        if (data.task) {
            handled++;
            // 模拟 PS 处理：返回成功的 PSD/JPG 路径
            await fetch(`${BASE}/api/ps-bridge/result`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-API-Key': KEY },
                body: JSON.stringify({
                    task_id: data.task.task_id || data.task.id,
                    ok: true,
                    result: {
                        status: 'ok',
                        output_psd_path: data.task.payload.output_psd_path,
                        output_preview_jpg_path: data.task.payload.output_preview_jpg_path,
                        applied_ops: 3
                    }
                })
            });
        } else {
            await new Promise(r => setTimeout(r, 120));
        }
    }
    return handled;
}

async function run() {
    let proc;
    try {
        cleanup();
        proc = await startServer();
        console.log('[start] server up');

        // 1) 空队列时 pending 返回 null（该接口需 X-API-Key，漏带会被 401 挡掉）
        const empty = await (await fetch(`${BASE}/api/ps-bridge/pending`, {
            headers: { 'X-API-Key': KEY }
        })).json();
        assert.strictEqual(empty.task, null, '空队列应返回 null');
        console.log('[PASS] 1) 空队列返回 task=null');

        // 2) 投递任务 + 插件模拟消费，应拿到结果
        const pluginPromise = fakePhotoshopPlugin(15000);
        const submit = await fetch(`${BASE}/api/internal/ps-bridge/run`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-API-Key': KEY },
            body: JSON.stringify({
                type: 'retouch',
                payload: {
                    tiff_path: 'C:/tmp/in.tiff',
                    output_psd_path: 'C:/tmp/out.psd',
                    output_preview_jpg_path: 'C:/tmp/out.jpg',
                    plan: { style: '清冷通透' }
                },
                timeout_ms: 20000
            })
        });
        const submitBody = await submit.json();
        const handled = await pluginPromise;

        assert.strictEqual(submit.status, 200, `投递应成功: ${JSON.stringify(submitBody)}`);
        assert.strictEqual(submitBody.result.output_psd_path, 'C:/tmp/out.psd');
        assert.strictEqual(submitBody.result.applied_ops, 3);
        assert.ok(handled >= 1, '插件应至少处理 1 个任务');
        console.log(`[PASS] 2) 任务投递→PS消费→结果回传 全链路通 (插件处理 ${handled} 个)`);

        // 3) 无插件消费时应超时
        const t0 = Date.now();
        const timeoutRes = await fetch(`${BASE}/api/internal/ps-bridge/run`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-API-Key': KEY },
            body: JSON.stringify({ type: 'retouch', payload: { tiff_path: 'C:/tmp/none.tif' }, timeout_ms: 1500 })
        });
        const elapsed = Date.now() - t0;
        assert.strictEqual(timeoutRes.status, 504, `无消费应超时 504，实际 ${timeoutRes.status}`);
        assert.ok(elapsed >= 1400, `应真的等待了: ${elapsed}ms`);
        console.log(`[PASS] 3) 无 PS 消费时正确超时 504 (${elapsed}ms)`);

        // 4) 鉴权：无 key 被拒
        const noKey = await fetch(`${BASE}/api/internal/ps-bridge/run`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ type: 'retouch', payload: {} })
        });
        assert.strictEqual(noKey.status, 401, '无 key 应 401');
        console.log('[PASS] 4) 内部接口鉴权生效 (401)');

        // 5) 状态接口
        const st = await (await fetch(`${BASE}/api/ps-bridge/status`)).json();
        assert.strictEqual(st.status, 'ok');
        console.log(`[PASS] 5) 状态接口正常 (pending=${st.pending}, running=${st.running})`);

        console.log('=== PS Bridge 全部通过 (5/5) ===');
    } finally {
        if (proc && !proc.killed) { proc.kill(); await new Promise(r => setTimeout(r, 400)); }
        cleanup();
    }
}

run().catch(e => { console.error('[FAIL]', e); process.exit(1); });
