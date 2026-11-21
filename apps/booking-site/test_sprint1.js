const assert = require('assert');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const sqlite3 = require('sqlite3').verbose();

const TEST_PORT = 3456;
const BASE_URL = `http://127.0.0.1:${TEST_PORT}`;
const API_KEY = 'sprint1-test-internal-key';
const TEST_DB_PATH = path.join(__dirname, 'test_sprint1.db');

function removeTestDbFiles() {
    for (const suffix of ['', '-wal', '-shm']) {
        const file = TEST_DB_PATH + suffix;
        if (fs.existsSync(file)) {
            try {
                fs.unlinkSync(file);
            } catch (_) {}
        }
    }
}

function queryDb(dbPath, fn) {
    return new Promise((resolve, reject) => {
        const db = new sqlite3.Database(dbPath, (err) => {
            if (err) return reject(err);
            Promise.resolve()
                .then(() => fn(db))
                .then((res) => {
                    db.close(() => resolve(res));
                })
                .catch((e) => {
                    db.close(() => reject(e));
                });
        });
    });
}

function dbRun(db, sql, params = []) {
    return new Promise((resolve, reject) => {
        db.run(sql, params, function (err) {
            if (err) return reject(err);
            resolve(this);
        });
    });
}

function dbGet(db, sql, params = []) {
    return new Promise((resolve, reject) => {
        db.get(sql, params, (err, row) => {
            if (err) return reject(err);
            resolve(row);
        });
    });
}

function dbAll(db, sql, params = []) {
    return new Promise((resolve, reject) => {
        db.all(sql, params, (err, rows) => {
            if (err) return reject(err);
            resolve(rows);
        });
    });
}

async function seedLegacyDatabase() {
    removeTestDbFiles();
    await queryDb(TEST_DB_PATH, async (db) => {
        await dbRun(
            db,
            "CREATE TABLE bookings_v3 (id INTEGER PRIMARY KEY AUTOINCREMENT, wechat TEXT, cn TEXT, character TEXT, event_name TEXT, date TEXT, time TEXT, deposit TEXT, quantity INTEGER, remarks TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        );
        await dbRun(
            db,
            "INSERT INTO bookings_v3 (wechat, cn, character, event_name, date, time, deposit, quantity, remarks) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ['legacy_wx', '老客户CN', '初音未来', '五一漫展专属企划', '2026-05-01', '09:00-10:00', '80', 1, '存量数据测试']
        );
    });
}

function startServer() {
    return new Promise((resolve, reject) => {
        const child = spawn(process.execPath, [path.join(__dirname, 'server.js')], {
            cwd: __dirname,
            env: {
                ...process.env,
                PORT: String(TEST_PORT),
                DB_PATH: TEST_DB_PATH,
                INTERNAL_API_KEY: API_KEY,
                ADMIN_USER: 'admin',
                ADMIN_PASSWORD: 'test-password',
                PUSHPLUS_TOKEN: ''
            },
            stdio: ['ignore', 'pipe', 'pipe']
        });

        let stdoutBuf = '';
        let stderrBuf = '';
        const timeout = setTimeout(() => {
            child.kill();
            reject(new Error(`Server startup timeout. stdout=${stdoutBuf} stderr=${stderrBuf}`));
        }, 10000);

        child.stdout.on('data', (chunk) => {
            stdoutBuf += chunk.toString();
            if (stdoutBuf.includes('Node Server is running')) {
                clearTimeout(timeout);
                resolve(child);
            }
        });

        child.stderr.on('data', (chunk) => {
            stderrBuf += chunk.toString();
        });

        child.on('exit', (code) => {
            clearTimeout(timeout);
            reject(new Error(`Server exited early with code ${code}. stderr=${stderrBuf}`));
        });
    });
}

async function stopServer(child) {
    if (!child || child.killed) return;
    await new Promise((resolve) => {
        child.once('exit', () => resolve());
        child.kill();
        setTimeout(() => resolve(), 2000);
    });
}

async function runTests() {
    let serverProc;
    try {
        console.log('=== Sprint 1 自动化冒烟测试开始 ===');

        // 预先构造一份旧版表结构与存量数据，验证无损升级
        await seedLegacyDatabase();
        serverProc = await startServer();
        console.log(`[启动] 服务已启动于 ${BASE_URL}`);

        // (a) 验证 WAL 模式生效 + 无损迁移 + 新建 3 张表
        await queryDb(TEST_DB_PATH, async (db) => {
            const jm = await dbGet(db, "PRAGMA journal_mode;");
            assert.strictEqual(
                String(jm.journal_mode).toLowerCase(),
                'wal',
                `Expected WAL mode, got ${jm.journal_mode}`
            );

            const cols = await dbAll(db, "PRAGMA table_info(bookings_v3);");
            const colNames = new Set(cols.map(c => c.name));
            const expectedCols = [
                'qq', 'platform', 'im_user_id', 'character_ref_img',
                'status', 'shoot_code', 'gallery_token', 'slate_qr_payload'
            ];
            for (const c of expectedCols) {
                assert.ok(colNames.has(c), `Missing column bookings_v3.${c}`);
            }

            // 确认旧数据保留且默认值生效
            const legacyRow = await dbGet(db, "SELECT * FROM bookings_v3 WHERE wechat = 'legacy_wx'");
            assert.ok(legacyRow, 'Legacy booking row should be preserved');
            assert.strictEqual(legacyRow.cn, '老客户CN');
            assert.strictEqual(legacyRow.platform, 'web');
            assert.strictEqual(legacyRow.status, 'BOOKED');

            // 确认 3 张新表存在
            const tables = await dbAll(db, "SELECT name FROM sqlite_master WHERE type='table'");
            const tableNames = new Set(tables.map(t => t.name));
            for (const tbl of ['ingest_batches', 'photo_assets', 'asset_booking_links']) {
                assert.ok(tableNames.has(tbl), `Missing new table ${tbl}`);
            }
        });
        console.log('[PASS] (a) WAL 模式生效、bookings_v3 无损扩列、3 张新表创建完成');

        // (b) 同一时段并发提交 2 次预约（Web 端 /api/save-schedule），验证第 2 次被拦截
        const makeWebPayload = (idx) => ({
            wechat: `wx_concurrent_${idx}`,
            cn: `并发测试CN_${idx}`,
            character: '雷电将军',
            event_name: '五一漫展专属企划',
            date: '2026-05-01',
            times: ['10:00-12:00'],
            deposit: '80',
            quantity: 1,
            remarks: `concurrent request #${idx}`,
            invite_code: 'zero2026'
        });

        const [res1, res2] = await Promise.all([
            fetch(`${BASE_URL}/api/save-schedule`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(makeWebPayload(1))
            }),
            fetch(`${BASE_URL}/api/save-schedule`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(makeWebPayload(2))
            })
        ]);

        const body1 = await res1.json();
        const body2 = await res2.json();
        const statuses = [res1.status, res2.status].sort((a, b) => a - b);
        const okCount = [body1, body2].filter(b => b.status === 'ok').length;
        const errCount = [body1, body2].filter(b => b.status === 'error').length;

        assert.strictEqual(okCount, 1, `Expected exactly 1 success, got ${okCount}`);
        assert.strictEqual(errCount, 1, `Expected exactly 1 rejection, got ${errCount}`);
        assert.deepStrictEqual(statuses, [200, 409], `Expected HTTP [200, 409], got ${JSON.stringify(statuses)}`);
        console.log('[PASS] (b) 同一时段并发提交 2 次预约：1 次成功(200)，第 2 次被事务锁正确拦截(409)');

        // (c) X-API-Key 鉴权拦截与放行 + 内部接口全链路验证
        const noKeyRes = await fetch(`${BASE_URL}/api/internal/bookings/by-event?event_name=五一漫展专属企划`);
        assert.strictEqual(noKeyRes.status, 401, 'Missing X-API-Key should return 401');

        const wrongKeyRes = await fetch(`${BASE_URL}/api/internal/bookings/by-event?event_name=五一漫展专属企划`, {
            headers: { 'X-API-Key': 'wrong-key' }
        });
        assert.strictEqual(wrongKeyRes.status, 401, 'Wrong X-API-Key should return 401');

        // 放行：内部接口并发创建订单（也验证内部接口走并发锁）
        const makeBotPayload = (idx) => ({
            qq: `1000000${idx}`,
            platform: 'qq',
            im_user_id: `qq_uid_${idx}`,
            cn: `机器人预约CN_${idx}`,
            character: '流萤',
            character_ref_img: 'https://example.com/ref/firefly.jpg',
            event_name: 'CP31',
            date: '2026-05-02',
            time: '14:00-16:00',
            deposit: '180',
            quantity: 1,
            shoot_code: `CP31-00${idx}`,
            remarks: 'AI机器人测试单'
        });

        const [botRes1, botRes2] = await Promise.all([
            fetch(`${BASE_URL}/api/internal/bookings`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-API-Key': API_KEY
                },
                body: JSON.stringify(makeBotPayload(1))
            }),
            fetch(`${BASE_URL}/api/internal/bookings`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-API-Key': API_KEY
                },
                body: JSON.stringify(makeBotPayload(2))
            })
        ]);

        const botBody1 = await botRes1.json();
        const botBody2 = await botRes2.json();
        const botStatuses = [botRes1.status, botRes2.status].sort((a, b) => a - b);
        assert.deepStrictEqual(botStatuses, [201, 409], `Internal concurrent booking expected [201, 409], got ${JSON.stringify(botStatuses)}`);

        const createdBookingResp = botRes1.status === 201 ? botBody1 : botBody2;
        assert.ok(createdBookingResp.booking_id, 'Should return booking_id');
        assert.ok(createdBookingResp.gallery_token && createdBookingResp.gallery_token.length >= 12, 'Should auto-generate gallery_token');
        const qrPayload = createdBookingResp.slate_qr_payload;
        if (String(qrPayload).startsWith('ZERO-STUDIO-BOOKING|')) {
            // 新版：AI 可直接读取的 key=value 场记码，需包含完整预约信息
            assert.ok(qrPayload.includes(`order=${createdBookingResp.booking_id}`), 'QR 应包含订单号');
            assert.ok(qrPayload.includes('character=流萤'), 'QR 应包含角色');
            assert.ok(qrPayload.includes('event=CP31'), 'QR 应包含活动');
            assert.ok(qrPayload.includes('date=2026-05-02'), 'QR 应包含日期');
            assert.ok(qrPayload.includes('time=14:00-16:00'), 'QR 应包含时段');
        } else {
            const qrJson = JSON.parse(qrPayload);
            assert.strictEqual(qrJson.booking_id, createdBookingResp.booking_id);
            assert.strictEqual(qrJson.character, '流萤');
            assert.strictEqual(qrJson.event_name, 'CP31');
        }

        // 测试按活动与日期查询
        const byEventRes = await fetch(
            `${BASE_URL}/api/internal/bookings/by-event?event_name=${encodeURIComponent('CP31')}&date=2026-05-02`,
            { headers: { 'X-API-Key': API_KEY } }
        );
        assert.strictEqual(byEventRes.status, 200);
        const byEventData = await byEventRes.json();
        assert.strictEqual(byEventData.count, 1);
        assert.strictEqual(byEventData.bookings[0].id, createdBookingResp.booking_id);
        assert.strictEqual(byEventData.bookings[0].character, '流萤');

        // 测试状态机更新（非法状态拦截 + 合法状态与 gallery_token 更新）
        const badStatusRes = await fetch(`${BASE_URL}/api/internal/bookings/${createdBookingResp.booking_id}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
            body: JSON.stringify({ status: 'INVALID_STATE' })
        });
        assert.strictEqual(badStatusRes.status, 400, 'Invalid status should return 400');

        const patchRes = await fetch(`${BASE_URL}/api/internal/bookings/${createdBookingResp.booking_id}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
            body: JSON.stringify({ status: 'SHOT', gallery_token: 'customtoken12345' })
        });
        assert.strictEqual(patchRes.status, 200);
        const patchData = await patchRes.json();
        assert.strictEqual(patchData.booking.status, 'SHOT');
        assert.strictEqual(patchData.booking.gallery_token, 'customtoken12345');
        console.log('[PASS] (c) X-API-Key 鉴权拦截(401)与放行成功，内部创建/查询/状态流转接口验证通过');

        // (d) 测试场记二维码 PNG 接口
        const qrNoKeyRes = await fetch(`${BASE_URL}/api/internal/bookings/${createdBookingResp.booking_id}/slate-qr`);
        assert.strictEqual(qrNoKeyRes.status, 401, 'slate-qr without key should return 401');

        const qrRes = await fetch(`${BASE_URL}/api/internal/bookings/${createdBookingResp.booking_id}/slate-qr`, {
            headers: { 'X-API-Key': API_KEY }
        });
        assert.strictEqual(qrRes.status, 200, `Expected 200 from slate-qr, got ${qrRes.status}`);
        const contentType = qrRes.headers.get('content-type') || '';
        assert.ok(contentType.includes('image/png'), `Expected image/png content-type, got ${contentType}`);

        const pngBuf = Buffer.from(await qrRes.arrayBuffer());
        const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
        assert.ok(pngBuf.length > 100, `PNG buffer unexpectedly small: ${pngBuf.length} bytes`);
        assert.ok(pngBuf.subarray(0, 8).equals(PNG_MAGIC), 'Response body does not start with valid PNG magic bytes');
        console.log(`[PASS] (d) 场记二维码接口成功返回有效 PNG (${pngBuf.length} bytes, magic=89504E470D0A1A0A)`);

        console.log('=== 全部冒烟测试通过 (4/4) ===');
    } finally {
        await stopServer(serverProc);
        removeTestDbFiles();
    }
}

runTests().catch((err) => {
    console.error('[FAIL] 冒烟测试失败:', err);
    process.exit(1);
});
