// Exercise the actual CVAT decoder with explicit bitmap/worker ownership.
// Deterministic resource counts catch leaks without relying on GC timing or RSS.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const babel = require('@babel/core');
const JSZip = require('jszip');
const dataRoot = path.resolve(__dirname, '../../../cvat/cvat-data/src/ts');
const tick = () => new Promise((resolve) => setImmediate(resolve));

function source(name) {
    const filename = path.join(dataRoot, name);
    return babel.transformSync(fs.readFileSync(filename, 'utf8').replaceAll('import.meta.url', '"file:///cvat-data.ts"'), {
        filename, babelrc: false, configFile: false,
        presets: [['@babel/preset-env', { targets: { node: 'current' } }], '@babel/preset-typescript'],
    }).code;
}
const decoderCode = source('cvat-data.ts');
const workerCode = source('unzip_imgs.worker.ts');

function harness() {
    const live = new Set();
    const workers = [];
    class Bitmap {
        constructor() { this.closed = false; this.closeCalls = 0; live.add(this); }
        close() { this.closeCalls++; this.closed = true; live.delete(this); }
    }
    class Worker {
        constructor() { this.terminated = false; this.messages = []; workers.push(this); }
        postMessage(message) { this.messages.push(message); if (this.failPost) throw new Error('Cannot post'); }
        terminate() { this.terminated = true; }
        frame(index, bitmap = new Bitmap()) { this.onmessage({ data: { index, fileName: `${index}.png`, data: bitmap } }); return bitmap; }
        fail() { this.onerror({ error: new Error('Decode failed') }); }
    }
    class Reader {
        constructor() {
            this.tracks = { 1: {
                trak: { mdia: { minf: { stbl: { stsd: { avc1: { avcC: { sps: [[]], pps: [[]] } } } } } } },
                getSampleCount: () => 0,
            } };
        }
        read() {}
    }
    const exported = {};
    const context = vm.createContext({
        exports: exported, URL, Blob, Error, ArrayBuffer, Uint8ClampedArray, Uint32Array,
        ImageBitmap: Bitmap, Worker,
        ImageData: class { constructor(data, width, height) { Object.assign(this, { data, width, height }); } },
        createImageBitmap: () => Promise.resolve(new Bitmap()),
        require: (name) => name === './3rdparty/mp4' ? { MP4Reader: Reader, Bytestream: class {} } : require(name),
    });
    vm.runInContext(decoderCode, context);
    const decoder = (limit = 2, type = 'archive') => new exported.FrameDecoder(type, limit, (frame) => Math.floor(frame / 4));
    const request = (instance, chunk, numbers = [chunk * 4, chunk * 4 + 1]) => {
        const result = { done: 0, errors: [], frames: [] };
        instance.requestDecodeBlock(new ArrayBuffer(8), chunk, numbers,
            (frame, data) => result.frames.push({ frame, data }), () => result.done++, (error) => result.errors.push(error));
        return result;
    };
    return { ...exported, live, workers, Bitmap, context, decoder, request };
}

test('failed chunks release partial bitmaps, encoded input, and worker; the next decode works', async () => {
    const h = harness();
    const decoder = h.decoder();
    const failed = h.request(decoder, 0);
    await tick();
    const first = h.workers[0].frame(0);
    assert.equal(decoder.chunkIsBeingDecoded.block, null);
    h.workers[0].fail();
    assert.equal(first.closeCalls, 1);
    assert.equal(failed.errors.length, 1);
    assert.equal(h.workers[0].terminated, true);
    const retried = h.request(decoder, 1);
    await tick();
    h.workers[1].frame(0); h.workers[1].frame(1);
    assert.equal(retried.done, 1);
    decoder.close();
    assert.equal(h.live.size, 0);
});

test('closing mid-decode releases partial/late bitmaps and rejects active and queued requests', async () => {
    const h = harness();
    const decoder = h.decoder();
    const active = h.request(decoder, 0);
    await tick();
    const worker = h.workers[0];
    const lateMessage = worker.onmessage;
    const partial = worker.frame(0);
    const queued = h.request(decoder, 1);
    decoder.close(); decoder.close();
    await tick();
    assert.equal(active.errors.length, 1);
    assert.equal(queued.errors.length, 1);
    assert.equal(partial.closeCalls, 1);
    assert.equal(worker.terminated, true);
    const late = new h.Bitmap();
    lateMessage({ data: { index: 1, data: late } });
    assert.equal(late.closeCalls, 1);
    assert.equal(h.live.size, 0);
    assert.equal(h.workers.length, 1);
    assert.equal(h.request(decoder, 2).errors.length, 1);
});

test('repeated scrubbing keeps only the latest queued input and settles replaced callbacks', async () => {
    const h = harness();
    const decoder = h.decoder();
    const active = h.request(decoder, 0);
    await tick();
    const requests = Array.from({ length: 200 }, (_, index) => h.request(decoder, index + 1));
    assert.equal(requests.slice(0, -1).every((result) => result.errors.length === 1), true);
    h.workers[0].frame(0); h.workers[0].frame(1);
    await tick();
    assert.equal(active.done, 1);
    assert.equal(h.workers.length, 1);
    assert.equal(decoder.chunkIsBeingDecoded.chunkIndex, 200);
    h.workers[0].frame(0); h.workers[0].frame(1);
    assert.equal(requests.at(-1).done, 1);
    decoder.close();
    assert.equal(h.live.size, 0);
});

test('re-requesting the active chunk replaces completion callbacks without another decoder', async () => {
    const h = harness();
    const decoder = h.decoder();
    const first = h.request(decoder, 0);
    await tick();
    const queued = h.request(decoder, 1);
    const replacement = h.request(decoder, 0);
    const next = h.request(decoder, 2);
    h.workers[0].frame(0); h.workers[0].frame(1);
    await tick();
    assert.equal(first.errors.length, 1);
    assert.equal(queued.errors.length, 1);
    assert.equal(first.done, 0);
    assert.equal(replacement.done, 1);
    h.workers[0].frame(0); h.workers[0].frame(1);
    assert.equal(next.done, 1);
    decoder.close();
    assert.equal(h.live.size, 0);
});

test('hundreds of decodes and cached-chunk replacements stay within the bitmap limit', async () => {
    const h = harness();
    const decoder = h.decoder(2);
    for (let iteration = 0; iteration < 200; iteration++) {
        const chunk = Math.floor(iteration / 2);
        const request = h.request(decoder, chunk, [chunk * 4, chunk * 4 + 1, chunk * 4 + 2, chunk * 4 + 3]);
        await tick();
        for (let index = 0; index < 4; index++) {
            h.workers[0].frame(index);
            assert.ok(h.live.size <= 8, `Retained ${h.live.size} bitmaps with an eight-frame limit`);
        }
        assert.equal(request.done, 1);
        assert.ok(decoder.cachedChunks().length <= 2);
    }
    decoder.close();
    assert.equal(h.live.size, 0);
});

test('asynchronous video bitmaps finish out of order and late bitmaps close after cancellation', async () => {
    const h = harness();
    const pending = [];
    h.context.createImageBitmap = () => new Promise((resolve) => pending.push(resolve));
    const decoder = h.decoder(2, 'mp4video');
    decoder.setRenderSize(1, 1);
    const result = h.request(decoder, 0);
    await tick();
    const message = { data: { buf: new ArrayBuffer(4), width: 1, height: 1 } };
    h.workers[0].onmessage(message); h.workers[0].onmessage(message);
    pending[1](new h.Bitmap());
    await tick();
    assert.equal(result.done, 0);
    decoder.close();
    const late = new h.Bitmap(); pending[0](late);
    await tick();
    assert.equal(result.errors.length, 1);
    assert.equal(late.closeCalls, 1);
    assert.equal(h.live.size, 0);
});

test('context decoder releases partial bitmaps and its mutex after an error', async () => {
    const h = harness();
    const failed = h.decodeContextImages(new ArrayBuffer(8), 0, 2);
    const rejected = assert.rejects(failed, /Decode failed/);
    await tick();
    h.workers[0].frame(0); h.workers[0].fail();
    await rejected;
    assert.equal(h.live.size, 0);
    const recovered = h.decodeContextImages(new ArrayBuffer(8), 1, 3);
    await tick();
    assert.equal(h.workers[1].messages[0].end, 2);
    h.workers[1].frame(1); h.workers[1].frame(2);
    const images = await recovered;
    Object.values(images).forEach((bitmap) => bitmap.close());
    assert.equal(h.live.size, 0);
});

test('video completion waits for every bitmap even when the last frame resolves first', async () => {
    const h = harness();
    const pending = [];
    h.context.createImageBitmap = () => new Promise((resolve) => pending.push(resolve));
    const decoder = h.decoder(2, 'mp4video');
    decoder.setRenderSize(1, 1);
    const result = h.request(decoder, 0);
    await tick();
    const message = { data: { buf: new ArrayBuffer(4), width: 1, height: 1 } };
    h.workers[0].onmessage(message); h.workers[0].onmessage(message);
    pending[1](new h.Bitmap()); await tick();
    assert.equal(result.done, 0);
    pending[0](new h.Bitmap()); await tick();
    assert.equal(result.done, 1);
    assert.equal(h.live.size, 2);
    decoder.close();
    assert.equal(h.live.size, 0);
});

test('worker setup errors reject the request and release the decoder mutex', async () => {
    const h = harness();
    const Worker = h.context.Worker;
    h.context.Worker = class extends Worker { constructor() { super(); this.failPost = true; } };
    const decoder = h.decoder();
    const failed = h.request(decoder, 0);
    await tick();
    assert.equal(failed.errors.length, 1);
    assert.equal(h.workers[0].terminated, true);
    h.context.Worker = Worker;
    const next = h.request(decoder, 1);
    await tick();
    h.workers[1].frame(0); h.workers[1].frame(1);
    assert.equal(next.done, 1);
    decoder.close();
    assert.equal(h.live.size, 0);
});

test('context worker construction failures do not strand the mutex or future requests', async () => {
    const h = harness();
    const Worker = h.context.Worker;
    h.context.Worker = class { constructor() { throw new Error('Worker unavailable'); } };
    await assert.rejects(h.decodeContextImages(new ArrayBuffer(8), 0, 1), /Worker unavailable/);
    h.context.Worker = Worker;
    const next = h.decodeContextImages(new ArrayBuffer(8), 0, 1);
    await tick();
    h.workers[0].frame(0);
    const images = await next;
    Object.values(images).forEach((bitmap) => bitmap.close());
    assert.equal(h.live.size, 0);
});

async function workerHarness(options = {}) {
    const h = harness();
    const messages = [];
    let count = 0;
    const context = vm.createContext({
        require, exports: {}, onmessage: null,
        createImageBitmap: async () => {
            if (++count === options.failDecode) throw new Error('Invalid pixels');
            return new h.Bitmap();
        },
        postMessage: (message, transfer = []) => {
            if (message.data && options.failPost) throw new Error('Transfer failed');
            messages.push({ message, transfer });
        },
    });
    vm.runInContext(workerCode, context);
    const zip = new JSZip();
    zip.file('images/0.png', 'pixels'); zip.file('images/1.png', 'pixels'); zip.file('images/2.png', 'pixels');
    await context.onmessage({ data: { block: await zip.generateAsync({ type: 'arraybuffer' }), start: 0, end: 2, dimension: '2d', dimension2D: '2d' } });
    return { ...h, messages, count };
}

test('ZIP worker transfers bitmaps rather than cloning decoded pixel storage', async () => {
    const h = await workerHarness();
    assert.equal(h.messages.length, 3);
    h.messages.forEach(({ message, transfer }) => {
        assert.equal(transfer.length, 1);
        assert.equal(transfer[0], message.data);
        message.data.close();
    });
    assert.equal(h.live.size, 0);
});

test('ZIP worker reports bitmap failures and stops creating further bitmaps', async () => {
    const h = await workerHarness({ failDecode: 2 });
    assert.equal(h.count, 2);
    assert.match(h.messages[1].message.error.message, /Invalid pixels/);
    h.messages[0].message.data.close();
    assert.equal(h.live.size, 0);
    const failedPost = await workerHarness({ failPost: true });
    assert.match(failedPost.messages[0].message.error.message, /Transfer failed/);
    assert.equal(failedPost.live.size, 0);
});
