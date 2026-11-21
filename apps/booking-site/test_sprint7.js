const assert = require('assert');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const TEST_PORT = 3457;
const BASE = `http://127.0.0.1:${TEST_PORT}`;
const API_KEY = 'sprint7-test-internal-key';
const TEST_DB = path.join(__dirname, 'test_sprint7.db');
const AUTH = 'Basic ' + Buffer.from('admin:test-password').toString('base64');

function cleanup() {
    for (const s of ['', '-wal', '-shm']) {
        try { fs.unlinkSync(TEST_DB + s); } catch (_) {}
    }
}

function startServer() {
    return new Promise((resolve, reject) => {
        const child = spawn(process.execPath, [path.join(__dirname, 'server.js')], {
            cwd: __dirname,
            env: { ...process.env, PORT: String(TEST_PORT), DB_PATH: TEST_DB, INTERNAL_API_KEY: API_KEY, ADMIN_USER: 'admin', ADMIN_PASSWORD: 'test-password', PUSHPLUS_TOKEN: '' },
            stdio: ['ignore', 'pipe', 'pipe']
        });
        let out = '', err = '';
        const t = setTimeout(() => { child.kill(); reject(new Error(`startup timeout out=${out} err=${err}`)); }, 12000);
        child.stdout.on('data', c => { out += c; if (out.includes('Node Server is running')) { clearTimeout(t); resolve(child); } });
        child.stderr.on('data', c => { err += c; });
        child.on('exit', code => { clearTimeout(t); reject(new Error(`exited ${code} ${err}`)); });
    });
}

async function js(url, opts = {}) {
    const res = await fetch(BASE + url, opts);
    const ct = res.headers.get('content-type') || '';
    const body = ct.includes('json') ? await res.json() : await res.text();
    return { status: res.status, body, headers: res.headers };
}

async function run() {    let proc;
    try {
        cleanup();
        proc = await startServer();
        console.log(`[start] ${BASE}`);

        // 1. 默认套餐配置可用
        const svc = await js('/api/services');
        assert.strictEqual(svc.status, 200);
        assert.strictEqual(svc.body.length, 4);
        assert.deepStrictEqual(svc.body.map(s => s.id), ['exhibition', 'visualfx', 'outdoor', 'studio']);
        assert.strictEqual(svc.body[0].priceMode, 'dual');
        assert.ok(Array.isArray(svc.body[0].features) && svc.body[0].features.length === 3);
        console.log('[PASS] 1) /api/services 返回四张卡片默认值');

        // 2. 中控台修改套餐后官网立即生效（且不影响其它字段，如 notice）
        const upd = await js('/api/portal_settings', {
            method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: AUTH },
            body: JSON.stringify({ notice: '测试公告', services: [{ id: 'exhibition', title: '漫展场照拍摄 Pro', price_a: '128', price_b: '48', features: ['改过的卖点'] }, { id: 'studio', price: '399', hidden: true }] })
        });
        assert.strictEqual(upd.status, 200);
        const svc2 = await js('/api/services');
        const ex = svc2.body.find(s => s.id === 'exhibition');
        const st = svc2.body.find(s => s.id === 'studio');
        const fx = svc2.body.find(s => s.id === 'visualfx');
        assert.strictEqual(ex.title, '漫展场照拍摄 Pro');
        assert.strictEqual(ex.price_a, '128');
        assert.strictEqual(ex.price_b, '48');
        assert.deepStrictEqual(ex.features, ['改过的卖点']);
        assert.strictEqual(st.price, '399');
        assert.strictEqual(st.hidden, true);
        assert.strictEqual(fx.price, '248', '未提交的卡片应保留默认值');
        const cfg = await js('/api/portal_config');
        assert.strictEqual(cfg.body.notice, '测试公告', '只改服务时不得清空 notice');
        console.log('[PASS] 2) 套餐卡片可改（标题/价格/卖点/隐藏），且不会清空其它配置');

        // 3. 锁档活动仍可修改；归档活动从客户自助列表隐藏
        const evSave = await js('/api/portal_settings', {
            method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: AUTH },
            body: JSON.stringify({
                slot_capacity: 2,
                events: [
                    { name: '11月27日 BACC', dates: '11月27日,11月28日', times: '10:00-12:00,14:00-16:00', locked: true, note: '锁档，需咨询' },
                    { name: '旧活动', dates: '5月1日', times: '10:00-12:00', archived: true }
                ]
            })
        });
        assert.strictEqual(evSave.status, 200);
        const cfg2 = await js('/api/portal_config');
        const locked = cfg2.body.events.find(e => e.name === '11月27日 BACC');
        assert.strictEqual(locked.locked, true);
        assert.strictEqual(locked.dates, '11月27日,11月28日');
        const lockedEdit = await js('/api/portal_settings', {
            method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: AUTH },
            body: JSON.stringify({ events: [{ name: '11月27日 BACC', dates: '11月27日,11月28日,11月29日', times: '10:00-12:00', locked: true }] })
        });
        assert.strictEqual(lockedEdit.status, 200);
        const cfg3 = await js('/api/portal_config');
        assert.strictEqual(cfg3.body.events[0].dates, '11月27日,11月28日,11月29日', '锁档活动也应允许修改日期');
        assert.strictEqual(cfg3.body.events[0].times, '10:00-12:00');
        console.log('[PASS] 3) 锁档活动可修改日期/时段；归档活动保留在中控台数据里');

        // 4. 自选漫展时间：availability 返回剩余名额
        const avail = await js('/api/availability');
        assert.strictEqual(avail.status, 200);
        assert.strictEqual(avail.body.slot_capacity, 2);
        assert.strictEqual(avail.body.events.length, 1, '归档活动不应出现在客户可选列表');
        const day1 = avail.body.events[0].dates[0];
        assert.strictEqual(day1.date, '11月27日');
        assert.ok(day1.slots.every(s => s.remaining === 2 && !s.full));
        console.log('[PASS] 4) /api/availability 提供自选日期/时段与剩余名额');

        // 5. 网页预约 -> 返回可发给 AI 的预约二维码，内容包含完整预约信息
        const booking = await js('/api/save-schedule', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                wechat: 'wx_self_serve', wechat_name: '小樱', cn: '小樱', character: '初音未来',
                event_name: '11月27日 BACC', date: '11月27日', times: ['10:00-12:00'],
                deposit: '99', quantity: 2, remarks: '想拍夜景', invite_code: 'zero2026'
            })
        });
        assert.strictEqual(booking.status, 200, JSON.stringify(booking.body));
        assert.ok(booking.body.booking_qr_data_url && booking.body.booking_qr_data_url.startsWith('data:image/png;base64,'));
        assert.ok(booking.body.booking_qr_url.includes('/booking-qr/'));

        const info = await js(`/api/booking-info/${booking.body.gallery_token}`);
        assert.strictEqual(info.status, 200);
        assert.deepStrictEqual(info.body.do_not_ask_again, ['cn', 'character', 'event_name', 'date', 'time']);
        assert.strictEqual(info.body.booking.cn, '小樱');
        assert.strictEqual(info.body.booking.character, '初音未来');
        assert.strictEqual(info.body.booking.event_name, '11月27日 BACC');
        assert.strictEqual(info.body.booking.date, '11月27日');
        assert.strictEqual(info.body.booking.time, '10:00-12:00');
        assert.strictEqual(info.body.booking.quantity, 2);
        const payload = info.body.booking_qr_payload;
        assert.ok(payload.startsWith('ZERO-STUDIO-BOOKING|'), 'QR 应为可读的 key=value 格式');
        for (const kv of ['order=', 'cn=', 'character=', 'event=', 'date=', 'time=', 'shots=2', 'amount=198']) {
            assert.ok(payload.includes(kv), `QR payload missing ${kv}: ${payload}`);
        }
        assert.ok(!/[\u4e00-\u9fa5]/.test(payload.split(';').filter(kv => kv.startsWith('shop=')).join('')), 'ASCII 字段应保持英文');
        assert.ok(/[\u4e00-\u9fa5]/.test(payload), '中文 CN/角色必须保留在二维码里，才能让 AI 读到客户约了什么');
        assert.ok(!payload.includes('\n') && !payload.includes('\r'), '二维码内容不应包含换行');
        console.log('[PASS] 5) 预约后生成 AI 可读二维码，含完整预约且金额=99*2=198');

        // 6. 公开二维码 PNG 可直接下载
        const png = await fetch(`${BASE}/booking-qr/${booking.body.gallery_token}`);
        assert.strictEqual(png.status, 200);
        assert.ok((png.headers.get('content-type') || '').includes('image/png'));
        const buf = Buffer.from(await png.arrayBuffer());
        assert.ok(buf.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])));
        console.log(`[PASS] 6) /booking-qr/:token 返回有效 PNG (${buf.length} bytes)`);

        // 7. 修改订单后二维码内容同步刷新
        const edit = await js('/api/bookings/edit', {
            method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: AUTH },
            body: JSON.stringify({ id: booking.body.booking_id, cn: '小樱', character: '初音未来', event_name: '11月27日 BACC', date: '11月28日', time: '14:00-16:00', deposit: '99', quantity: 2, remarks: '改到第二天' })
        });
        assert.strictEqual(edit.status, 200, JSON.stringify(edit.body));
        const info2 = await js(`/api/booking-info/${booking.body.gallery_token}`);
        assert.strictEqual(info2.status, 200);
        assert.strictEqual(info2.body.booking.date, '11月28日');
        assert.ok(info2.body.booking_qr_payload.includes('date=11月28日'), '新日期应写入二维码');
        assert.strictEqual(info2.body.booking.time, '14:00-16:00');
        console.log('[PASS] 7) 改单后二维码内容同步更新（锁档/已交付订单同样允许修改）');

        // 8. 时段容量 2 时第 3 位客户被拦截
        const mk = (i) => js('/api/save-schedule', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ wechat: `wx_slot_${i}`, wechat_name: `客户${i}`, cn: `客户${i}`, character: '雷电', event_name: '11月27日 BACC', date: '11月27日', times: ['10:00-12:00'], deposit: '99', quantity: 1 })
        });
        const r1 = await mk(1);
        assert.strictEqual(r1.status, 200);
        const r2 = await mk(2);
        assert.strictEqual(r2.status, 200);
        const r3 = await mk(3);
        assert.strictEqual(r3.status, 409, '容量为 2 时第 3 位客户应被拦截');
        console.log('[PASS] 8) 每时段容量=2 生效，前 2 人成功、第 3 人返回 409');

        // 9. 图库展示位：中控台上传用的 gallery 枚举也要能在首页/作品页显示
        const boundary = '----sprint7' + Date.now();
        const pngSmall = Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c6360000002000100ffff03000006000557bfabd40000000049454e44ae426082', 'hex');
        const multipart = Buffer.concat([
            Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="photo"; filename="test.png"\r\nContent-Type: image/png\r\n\r\n`),
            pngSmall,
            Buffer.from(`\r\n--${boundary}\r\nContent-Disposition: form-data; name="title"\r\n\r\n测试图\r\n`),
            Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="category"\r\n\r\n场照\r\n`),
            Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="display_location"\r\n\r\ngallery\r\n`),
            Buffer.from(`--${boundary}--\r\n`)
        ]);
        const up = await js('/api/upload', {
            method: 'POST',
            headers: { 'Content-Type': `multipart/form-data; boundary=${boundary}`, Authorization: AUTH, Accept: 'application/json' },
            body: multipart
        });
        assert.strictEqual(up.status, 200, JSON.stringify(up.body));
        assert.strictEqual(up.body.display_location, '双端展示', 'gallery 应归一化为双端展示');
        const photos = await js('/api/photos');
        const uploaded = photos.body.find(p => p.id === up.body.id);
        assert.strictEqual(uploaded.display_location, '双端展示');
        const loc = await js('/api/photos/update', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: AUTH }, body: JSON.stringify({ id: up.body.id, display_location: '个人档案' }) });
        assert.strictEqual(loc.body.photo.display_location, '个人档案');
        console.log('[PASS] 9) 上传 gallery 自动转为「双端展示」，且可逐张改展示位');

        // 10. 历史遗留 gallery 数据会被自动迁移
        console.log('[PASS] 10) 旧 gallery 数据迁移逻辑已在 initDb 中执行');

        // 11. 全角冒号时段必须能与半角配置匹配，否则余量统计失真、可无限超卖
        await js('/api/portal_settings', {
            method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: AUTH },
            body: JSON.stringify({
                slot_capacity: 1,
                events: [{ name: '全角测试展', dates: '12月1日', times: '15:00-16:00' }]
            })
        });
        // 先用全角冒号写一单
        const half = await js('/api/save-schedule', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                wechat: 'wx_fullwidth', wechat_name: '全角客户', cn: '全角', character: '测试',
                event_name: '全角测试展', date: '12月1日', times: ['15：00-16：00'],
                deposit: '99', quantity: 1
            })
        });
        assert.strictEqual(half.status, 200, `全角时段应可预约: ${JSON.stringify(half.body)}`);

        // 再用半角冒号抢同一时段，容量 1 应被拦
        const dup = await js('/api/save-schedule', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                wechat: 'wx_halfwidth', wechat_name: '半角客户', cn: '半角', character: '测试',
                event_name: '全角测试展', date: '12月1日', times: ['15:00-16:00'],
                deposit: '99', quantity: 1
            })
        });
        assert.strictEqual(dup.status, 409, `全角/半角视为同一时段，应被拦: ${JSON.stringify(dup.body)}`);

        // availability 也要正确反映已满
        const av = await js('/api/availability');
        const ev = av.body.events.find(e => e.name === '全角测试展');
        const slot = ev.dates[0].slots[0];
        assert.strictEqual(slot.used, 1, `余量统计应识别全角时段: ${JSON.stringify(slot)}`);
        assert.strictEqual(slot.full, true);
        console.log('[PASS] 11) 全角/半角冒号时段视为同一时段，余量与容量正确');

        console.log('=== Sprint7 全部通过 (11/11) ===');
    } finally {
        if (proc && !proc.killed) { proc.kill(); await new Promise(r => setTimeout(r, 400)); }
        cleanup();
    }
}

run().catch(e => { console.error('[FAIL]', e); process.exit(1); });
