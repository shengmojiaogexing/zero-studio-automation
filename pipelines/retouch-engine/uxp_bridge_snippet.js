/**
 * ZERO STUDIO · Photoshop UXP Bridge 本地接收端模板（可直接集成至 ZHUANG-AI UXP 插件）
 *
 * 职责：
 * 1. 通过 127.0.0.1 + 共享密钥 Token (X-Bridge-Token) 接收来自 Python retouch-engine 的修图任务；
 * 2. 使用 core.executeAsModal 安全包裹所有文档打开、batchPlay 调色与静默导出操作；
 * 3. 内置 isBusy 互斥锁：当 Photoshop 正在执行其他模态操作时立即返回 { error: "photoshop_busy" }，
 *    交由 Python 侧 ps_bridge.py 自动退避重试，杜绝死锁或崩溃；
 * 4. 导出 1024px 纯画面 sRGB JPEG 给 Gemini 仿色（绝不使用含 UI 偏差的系统截屏）。
 */

const { app, core, action } = require("photoshop");
const uxpStorage = require("uxp").storage;
const fs = uxpStorage.localFileSystem;

const BRIDGE_CONFIG = {
    // 建议由插件设置面板注入，与 .env 中 PS_UXP_BRIDGE_TOKEN 保持一致
    token: "change-me-ps-uxp-bridge-token",
    host: "127.0.0.1",
    port: 8765,
};

let isPhotoshopBusy = false;

/**
 * 校验本地请求共享密钥
 */
function verifyBridgeToken(headers) {
    const provided = headers && (headers["x-bridge-token"] || headers["X-Bridge-Token"]);
    return Boolean(BRIDGE_CONFIG.token && provided && provided === BRIDGE_CONFIG.token);
}

/**
 * 创建/获取本地文件 Entry 并生成 UXP Session Token（供 batchPlay 静默读写）
 */
async function createTokenForPath(nativePath, isOutput = false) {
    if (isOutput) {
        const lastSlash = Math.max(nativePath.lastIndexOf("\\"), nativePath.lastIndexOf("/"));
        const dirPath = nativePath.slice(0, lastSlash);
        const fileName = nativePath.slice(lastSlash + 1);
        const folderEntry = await fs.getEntryWithUrl("file:" + dirPath.replace(/\\/g, "/"));
        const fileEntry = await folderEntry.createFile(fileName, { overwrite: true });
        return fs.createSessionToken(fileEntry);
    }
    const entry = await fs.getEntryWithUrl("file:" + nativePath.replace(/\\/g, "/"));
    return fs.createSessionToken(entry);
}

/**
 * 阶段 A：打开 16-bit TIFF 并静默导出纯画面 1024px sRGB JPEG
 */
async function handleExportSrgb({ tiff_path, output_jpg_path, max_edge = 1024 }) {
    if (isPhotoshopBusy) {
        return { status: 503, body: { error: "photoshop_busy" } };
    }
    isPhotoshopBusy = true;
    try {
        await core.executeAsModal(
            async () => {
                const openToken = await createTokenForPath(tiff_path, false);
                await action.batchPlay(
                    [{ _obj: "open", null: { _path: openToken, _kind: "local" } }],
                    { synchronousExecution: true }
                );

                const doc = app.activeDocument;
                // 1. 转换为标准 sRGB IEC61966-2.1 并压平到 8-bit 供 JPEG 预览导出
                await action.batchPlay(
                    [
                        {
                            _obj: "convertToProfile",
                            _target: [{ _ref: "document", _enum: "ordinal", _value: "targetEnum" }],
                            to: "sRGB IEC61966-2.1",
                            intent: { _enum: "intent", _value: "perceptual" },
                            mapBlack: true,
                            dither: true,
                            flatten: true,
                        },
                        {
                            _obj: "convertMode",
                            depth: 8,
                            merge: false,
                        },
                    ],
                    { synchronousExecution: true }
                );

                // 2. 长边缩放到 max_edge (1024px)
                const w = doc.width;
                const h = doc.height;
                if (Math.max(w, h) > max_edge) {
                    const scale = max_edge / Math.max(w, h);
                    await doc.resizeImage(Math.round(w * scale), Math.round(h * scale));
                }

                // 3. 静默保存为 sRGB JPEG
                const outToken = await createTokenForPath(output_jpg_path, true);
                await action.batchPlay(
                    [
                        {
                            _obj: "save",
                            as: { _obj: "JPEG", extendedQuality: 10, matteColor: { _enum: "matteColor", _value: "none" } },
                            in: { _path: outToken, _kind: "local" },
                            copy: true,
                            embedColorProfile: true,
                        },
                    ],
                    { synchronousExecution: true }
                );

                await doc.closeWithoutSaving();
            },
            { commandName: "ZERO STUDIO Export 1024px sRGB Preview" }
        );

        return { status: 200, body: { status: "ok", output_jpg_path } };
    } finally {
        isPhotoshopBusy = false;
    }
}

/**
 * 阶段 B：打开 16-bit TIFF -> 执行 Gemini 翻译后的确定性 batchPlay 调色与动作 -> 导出成片 JPEG
 */
async function handleRetouchAndExport({ tiff_path, output_jpg_path, batch_play = [], jpeg_quality = 11 }) {
    if (isPhotoshopBusy) {
        return { status: 503, body: { error: "photoshop_busy" } };
    }
    isPhotoshopBusy = true;
    try {
        await core.executeAsModal(
            async () => {
                const openToken = await createTokenForPath(tiff_path, false);
                await action.batchPlay(
                    [{ _obj: "open", null: { _path: openToken, _kind: "local" } }],
                    { synchronousExecution: true }
                );

                // 执行来自 Python ps_bridge.py 的确定性 batchPlay 描述符序列
                if (Array.isArray(batch_play) && batch_play.length > 0) {
                    await action.batchPlay(batch_play, { synchronousExecution: true });
                }

                // 合并转 sRGB 8-bit 并静默导出高质量成片 JPEG
                const outToken = await createTokenForPath(output_jpg_path, true);
                await action.batchPlay(
                    [
                        {
                            _obj: "convertToProfile",
                            _target: [{ _ref: "document", _enum: "ordinal", _value: "targetEnum" }],
                            to: "sRGB IEC61966-2.1",
                            intent: { _enum: "intent", _value: "perceptual" },
                            mapBlack: true,
                            dither: true,
                            flatten: true,
                        },
                        { _obj: "convertMode", depth: 8, merge: false },
                        {
                            _obj: "save",
                            as: {
                                _obj: "JPEG",
                                extendedQuality: Number(jpeg_quality) || 11,
                                matteColor: { _enum: "matteColor", _value: "none" },
                            },
                            in: { _path: outToken, _kind: "local" },
                            copy: true,
                            embedColorProfile: true,
                        },
                    ],
                    { synchronousExecution: true }
                );

                await app.activeDocument.closeWithoutSaving();
            },
            { commandName: "ZERO STUDIO Apply Gemini BatchPlay & Export" }
        );

        return { status: 200, body: { status: "ok", output_jpg_path, applied_ops: batch_play.length } };
    } finally {
        isPhotoshopBusy = false;
    }
}

/**
 * 阶段 C：导出可交付的 PSD 工程文件（保留所有调整图层与蒙版）
 *
 * 与 JPEG 导出的区别：
 * - 不合并图层，不降位深（保留 16-bit 以便二次调整）
 * - 不可见图层、图层蒙版全部保留
 * - 输出 PSB 大文档格式由 Photoshop 根据尺寸自动决定
 */
async function saveAsPSD(outputPsdPath, { maximizeCompatibility = true } = {}) {
    const outToken = await createTokenForPath(outputPsdPath, true);
    await action.batchPlay(
        [
            {
                _obj: "save",
                as: {
                    _obj: "photoshop35Format",
                    maximizeCompatibility,
                },
                in: { _path: outToken, _kind: "local" },
                documentID: app.activeDocument.id,
                copy: false,
                lowerCase: true,
            },
        ],
        { synchronousExecution: true }
    );
}

/**
 * 阶段 D：一次完成「调色 -> 导出 PSD -> 导出预览 JPEG」
 * 这是客户交付的主链路：成片以 PSD 交付，JPEG 仅用于网页预览。
 */
async function handleRetouchExportPSD({
    tiff_path,
    output_psd_path,
    output_preview_jpg_path,
    batch_play = [],
    jpeg_quality = 10,
    preview_max_edge = 2048,
}) {
    if (isPhotoshopBusy) {
        return { status: 503, body: { error: "photoshop_busy" } };
    }
    isPhotoshopBusy = true;
    const appliedOps = Array.isArray(batch_play) ? batch_play.length : 0;
    try {
        await core.executeAsModal(
            async () => {
                const openToken = await createTokenForPath(tiff_path, false);
                await action.batchPlay(
                    [{ _obj: "open", null: { _path: openToken, _kind: "local" } }],
                    { synchronousExecution: true }
                );

                const doc = app.activeDocument;

                // 1. 应用 AI 决策生成的调整层（保留可编辑性）
                if (appliedOps > 0) {
                    await action.batchPlay(batch_play, { synchronousExecution: true });
                }

                // 2. 先存 PSD —— 此时图层结构完整
                if (output_psd_path) {
                    await saveAsPSD(output_psd_path);
                }

                // 3. 再导预览 JPEG（复制副本处理，不影响 PSD 的图层）
                if (output_preview_jpg_path) {
                    const w = doc.width;
                    const h = doc.height;
                    if (preview_max_edge && Math.max(w, h) > preview_max_edge) {
                        const scale = preview_max_edge / Math.max(w, h);
                        await doc.resizeImage(Math.round(w * scale), Math.round(h * scale));
                    }
                    await action.batchPlay(
                        [
                            {
                                _obj: "convertToProfile",
                                _target: [{ _ref: "document", _enum: "ordinal", _value: "targetEnum" }],
                                to: "sRGB IEC61966-2.1",
                                intent: { _enum: "intent", _value: "perceptual" },
                                mapBlack: true,
                                dither: true,
                                flatten: true,
                            },
                            { _obj: "convertMode", depth: 8, merge: false },
                        ],
                        { synchronousExecution: true }
                    );
                    const previewToken = await createTokenForPath(output_preview_jpg_path, true);
                    await action.batchPlay(
                        [
                            {
                                _obj: "save",
                                as: {
                                    _obj: "JPEG",
                                    extendedQuality: Number(jpeg_quality) || 10,
                                    matteColor: { _enum: "matteColor", _value: "none" },
                                },
                                in: { _path: previewToken, _kind: "local" },
                                copy: true,
                                embedColorProfile: true,
                            },
                        ],
                        { synchronousExecution: true }
                    );
                }

                await doc.closeWithoutSaving();
            },
            { commandName: "ZERO STUDIO Apply Plan & Export PSD" }
        );

        return {
            status: 200,
            body: {
                status: "ok",
                output_psd_path: output_psd_path || null,
                output_preview_jpg_path: output_preview_jpg_path || null,
                applied_ops: appliedOps,
            },
        };
    } finally {
        isPhotoshopBusy = false;
    }
}

/**
 * 统一路由分发函数（可挂接到 ZHUANG-AI UXP 现有的本地 HTTP/WS Bridge 通道）
 */
async function dispatchBridgeRequest(route, headers, payload) {
    if (!verifyBridgeToken(headers)) {
        return { status: 401, body: { error: "unauthorized_bridge_token" } };
    }
    if (route === "/bridge/export-srgb") {
        return handleExportSrgb(payload);
    }
    if (route === "/bridge/retouch-export") {
        return handleRetouchAndExport(payload);
    }
    if (route === "/bridge/retouch-export-psd") {
        return handleRetouchExportPSD(payload);
    }
    return { status: 404, body: { error: "unknown_route" } };
}

module.exports = {
    BRIDGE_CONFIG,
    verifyBridgeToken,
    handleExportSrgb,
    handleRetouchAndExport,
    handleRetouchExportPSD,
    saveAsPSD,
    dispatchBridgeRequest,
};
