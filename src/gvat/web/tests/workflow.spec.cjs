const { test: base, expect } = require('@playwright/test');
const { spawn, spawnSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const readline = require('readline');
const repo = path.resolve(__dirname, '../../../..');
const python = path.join(repo, '.venv-gvat/bin/python');

const test = base.extend({
    mediaType: ['videos', { option: true }],
    mode: ['annotate', { option: true }],
    videoFrames: [3, { option: true }],
    configuration: [null, { option: true }],
    session: async ({ mediaType, mode, videoFrames, configuration }, use) => {
        const directory = fs.mkdtempSync(path.join(repo, '.gvat-cache/e2e-'));
        const generated = spawnSync(python, [path.join(repo, 'src/gvat/tests/sample_dataset.py'), directory, String(videoFrames)]);
        if (generated.status !== 0) throw new Error(generated.stderr.toString());
        const data = path.join(directory, mediaType);
        const configArgs = [];
        if (configuration) {
            const config = path.join(directory, 'config.yml');
            fs.writeFileSync(config, configuration);
            configArgs.push('--config', config);
        }
        let child;
        let lines;
        let errors = '';
        async function stop() {
            lines?.close();
            if (child && child.exitCode === null && child.signalCode === null) {
                child.kill('SIGTERM');
                await new Promise((resolve) => child.once('exit', resolve));
            }
        }
        async function start() {
            child = spawn(python, ['-m', 'gvat_local', '--data_path', data,
                '--labels', path.join(directory, 'labels.txt'), '--mode', mode, '--port', '0', '--no-browser', ...configArgs],
                { cwd: repo, env: { ...process.env, PYTHONUNBUFFERED: '1', XDG_CACHE_HOME: path.join(repo, '.gvat-cache/browser-tests'), XDG_CONFIG_HOME: path.join(directory, 'user-config') } });
            child.stderr.on('data', (data) => { errors += data; });
            lines = readline.createInterface({ input: child.stdout });
            return new Promise((resolve, reject) => {
                const timer = setTimeout(() => reject(new Error(`GVAT did not start: ${errors}`)), 15000);
                lines.on('line', (line) => { if (line.startsWith('Open http')) { clearTimeout(timer); resolve(line.slice(5)); } });
                child.on('exit', (code) => { clearTimeout(timer); reject(new Error(`GVAT exited ${code}: ${errors}`)); });
            });
        }
        try {
            const session = { url: await start(), directory, data, xml: path.join(data, 'annotations.xml'), mediaType, errors: () => errors };
            session.restart = async () => { await stop(); session.url = await start(); };
            await use(session);
        } finally {
            await stop();
            fs.rmSync(directory, { recursive: true, force: true });
        }
    },
});

function snapshot(directory) {
    return Object.fromEntries(fs.readdirSync(directory).sort().map((name) => {
        const file = path.join(directory, name);
        expect(fs.statSync(file).isFile()).toBe(true);
        return [name, { hash: crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'), mtime: fs.statSync(file, { bigint: true }).mtimeNs }];
    }));
}
async function open(page, session) {
    await page.goto(session.url);
    await expect(page.locator('.cvat-annotation-page')).toBeVisible({ timeout: 20000 });
    await expect(page.locator('#cvat_canvas_background')).toBeVisible();
    await expect(page.locator('#cvat_canvas_shape_1')).toBeVisible();
    await expect(page).toHaveTitle('GVAT');
}
async function save(page) {
    const response = page.waitForResponse((item) => item.url().includes('/api/cvat/annotations') && item.request().method() === 'PUT');
    await page.locator('.cvat-annotation-header-save-button').click();
    expect((await response).status()).toBe(200);
    await expect(page.locator('.cvat-saving-job-modal')).toBeHidden();
}
async function chooseWorkspace(page, name) {
    await page.locator('.cvat-workspace-selector').click();
    await page.getByText(name, { exact: true }).last().click();
}
async function draw(page, type, points) {
    await page.locator(`.cvat-draw-${type}-control`).click();
    await page.locator(`.cvat-draw-${type}-shape-button`).click();
    const background = await page.locator('#cvat_canvas_background').boundingBox();
    for (const [x, y] of points) await page.mouse.click(background.x + background.width * x, background.y + background.height * y);
    if (type !== 'rectangle') await page.locator('.cvat-annotation-header-done-button').click();
}

for (const mediaType of ['images', 'videos']) {
    test.describe(`Upstream CVAT ${mediaType}`, () => {
        test.use({ mediaType });
        test('opens the native annotation page and moves/saves XML with no custom editor', async ({ page, session }) => {
            const errors = [];
            page.on('pageerror', (error) => errors.push(error.message));
            const before = snapshot(session.data);
            await open(page, session);
            await expect(page.locator('.cvat-standard-workspace')).toBeVisible();
            await expect(page.getByRole('tab', { name: 'Objects', exact: true })).toBeVisible();
            await expect(page.getByRole('tab', { name: 'Labels', exact: true })).toBeVisible();
            await expect(page.getByRole('tab', { name: 'Issues', exact: true })).toBeVisible();
            await expect(page.locator('#canvas-host, #image-list, .brand')).toHaveCount(0);
            await save(page);
            expect(snapshot(session.data)).toEqual(before);
            const shape = page.locator('#cvat_canvas_shape_1');
            await shape.hover();
            const bounds = await shape.boundingBox();
            await page.mouse.move(bounds.x + bounds.width * .6, bounds.y + bounds.height * .6);
            await page.mouse.down();
            await page.mouse.move(bounds.x + bounds.width * .6 + 25, bounds.y + bounds.height * .6 + 20, { steps: 8 });
            await page.mouse.up();
            await save(page);
            const moved = fs.readFileSync(session.xml, 'utf8');
            expect(moved).toContain('group_id="7"');
            expect(moved).toContain('<attribute name="pose">standing</attribute>');
            expect(Number(moved.match(/<box[^>]*xtl="([^"]+)"/)[1])).toBeGreaterThan(240);
            const saved = snapshot(session.data);
            await save(page);
            expect(snapshot(session.data)).toEqual(saved);
            await page.reload();
            await expect(page.locator('#cvat_canvas_shape_1')).toBeVisible();
            for (const [name, original] of Object.entries(before)) {
                if (name !== 'annotations.xml') expect(snapshot(session.data)[name]).toEqual(original);
            }
            expect(errors).toEqual([]);
        });
    });
}

test('native annotation menu exposes no project, task, upload, or account workflow', async ({ page, session }) => {
    await open(page, session);
    await page.locator('.cvat-annotation-header-menu-button').click();
    await expect(page.getByRole('menuitem', { name: 'Copy feedback', exact: true })).toBeVisible();
    await expect(page.getByRole('menuitem', { name: 'Export dataset', exact: true })).toBeVisible();
    await expect(page.getByRole('menuitem', { name: 'Export labels', exact: true })).toBeVisible();
    for (const name of ['Open the task', 'Change job state', 'Finish the job', 'Upload annotations']) {
        await expect(page.getByRole('menuitem', { name, exact: true })).toHaveCount(0);
    }
    await expect(page.locator('.cvat-header, .cvat-login-form, .cvat-tasks-page, .cvat-projects-page')).toHaveCount(0);
});

test('native rectangle and polygon tools save CVAT shapes and support native undo', async ({ page, session }) => {
    await open(page, session);
    await page.locator('.cvat-player-next-button').click();
    await expect(page.locator('#cvat_canvas_shape_1')).toBeHidden();
    await draw(page, 'rectangle', [[.1, .15], [.3, .4]]);
    await expect(page.locator('[id^="cvat_canvas_shape_"]:visible')).toHaveCount(1);
    await page.locator('.cvat-annotation-header-undo-button').click();
    await expect(page.locator('[id^="cvat_canvas_shape_"]:visible')).toHaveCount(0);
    await page.locator('.cvat-annotation-header-redo-button').click();
    await expect(page.locator('[id^="cvat_canvas_shape_"]:visible')).toHaveCount(1);
    await draw(page, 'polygon', [[.55, .2], [.8, .2], [.7, .5]]);
    await expect(page.locator('[id^="cvat_canvas_shape_"]:visible')).toHaveCount(2);
    await save(page);
    const xml = fs.readFileSync(session.xml, 'utf8');
    expect(xml).toContain('<polygon');
    expect(xml).toContain('<box');
    expect(xml).not.toContain('<image ');
    await page.locator('.cvat-player-next-button').click();
    await draw(page, 'rectangle', [[.1, .15], [.3, .4]]);
    await save(page);
    await page.reload();
    await expect(page.locator('.cvat-annotation-page')).toBeVisible();
    await page.locator('.cvat-player-next-button').click();
    await page.locator('.cvat-player-next-button').click();
    await expect(page.locator('[id^="cvat_canvas_shape_"]:visible')).toHaveCount(1);
    expect(fs.readdirSync(session.data).sort()).toEqual(['annotations.xml', 'source.mp4']);
});

test('attribute annotation uses CVAT own labeling workspace', async ({ page, session }) => {
    await open(page, session);
    await chooseWorkspace(page, 'Attribute annotation');
    await expect(page.locator('.attribute-annotation-workspace')).toBeVisible();
    await expect(page.getByText('pose', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('standing', { exact: true }).first()).toBeVisible();
    await page.locator('.attribute-annotation-sidebar .ant-select-selector').last().click();
    await page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').filter({ hasText: /^running$/ }).click();
    await save(page);
    expect(fs.readFileSync(session.xml, 'utf8')).toContain('<attribute name="pose">running</attribute>');
});

test('native export dialog lists the complete CVAT catalog and downloads COCO annotations', async ({ page, session }) => {
    const before = snapshot(session.data);
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await open(page, session);
    await page.locator('.cvat-annotation-header-menu-button').click();
    await page.getByRole('menuitem', { name: 'Export labels', exact: true }).click();
    const dialog = page.locator('.cvat-modal-export-job');
    await expect(dialog).toBeVisible();
    await page.locator('.cvat-modal-export-select').click();
    await expect(page.locator('.ant-select-dropdown:visible .ant-select-item-option')).toHaveCount(29);
    await page.getByText('COCO 1.0', { exact: true }).last().click();
    await page.getByPlaceholder('Custom name for a dataset').fill('review-annotations');
    const ready = page.waitForEvent('download', { timeout: 60000 });
    await dialog.getByRole('button', { name: 'OK', exact: true }).click();
    const download = await ready;
    expect(download.suggestedFilename()).toBe('review-annotations.zip');
    const zip = await require('jszip').loadAsync(fs.readFileSync(await download.path()));
    const file = Object.values(zip.files).find((file) => file.name.endsWith('.json'));
    const annotations = JSON.parse(await file.async('string'));
    expect(annotations.annotations[0].bbox).toEqual([240, 180, 320, 240]);
    expect(annotations.images).toHaveLength(3);
    expect(snapshot(session.data)).toEqual(before);
    expect(errors).toEqual([]);
    expect(session.errors()).not.toMatch(/Traceback|BrokenPipeError/);
});

test('Export dataset includes media while Export labels leaves media out', async ({ page, session }) => {
    const before = snapshot(session.data);
    await open(page, session);
    for (const [menu, includeMedia] of [['Export dataset', true], ['Export labels', false]]) {
        await page.locator('.cvat-annotation-header-menu-button').click();
        await page.getByRole('menuitem', { name: menu, exact: true }).click();
        const dialog = page.locator('.cvat-modal-export-job');
        await expect(dialog.locator('.ant-modal-title')).toHaveText(menu);
        const ready = page.waitForEvent('download', { timeout: 60000 });
        await dialog.getByRole('button', { name: 'OK', exact: true }).click();
        const download = await ready;
        const zip = await require('jszip').loadAsync(fs.readFileSync(await download.path()));
        expect(Object.keys(zip.files).some((name) => name.startsWith('images/'))).toBe(includeMedia);
        expect(await zip.file('annotations.xml').async('string')).toBe(fs.readFileSync(session.xml, 'utf8'));
    }
    expect(snapshot(session.data)).toEqual(before);
});

test('label exports include CVAT interpolated track positions between keyframes', async ({ page, session }) => {
    let xml = fs.readFileSync(session.xml, 'utf8');
    xml = xml.replace(/<box[^>]*frame="1"[^>]*\/>/, '<box xtl="440" ytl="280" xbr="760" ybr="520" occluded="0" frame="2" outside="0" keyframe="1"/>');
    fs.writeFileSync(session.xml, xml);
    const before = snapshot(session.data);
    await open(page, session);
    await page.locator('.cvat-annotation-header-menu-button').click();
    await page.getByRole('menuitem', { name: 'Export labels', exact: true }).click();
    await page.locator('.cvat-modal-export-select').click();
    await page.getByText('COCO 1.0', { exact: true }).last().click();
    const ready = page.waitForEvent('download', { timeout: 60000 });
    await page.locator('.cvat-modal-export-job').getByRole('button', { name: 'OK', exact: true }).click();
    const download = await ready;
    const zip = await require('jszip').loadAsync(fs.readFileSync(await download.path()));
    const file = Object.values(zip.files).find((file) => file.name.endsWith('.json'));
    const annotations = JSON.parse(await file.async('string'));
    expect(annotations.annotations.map((item) => item.bbox)).toEqual([
        [240, 180, 320, 240], [340, 230, 320, 240], [440, 280, 320, 240],
    ]);
    expect(snapshot(session.data)).toEqual(before);
});

test('native settings switch to pixelated zoom and retain the browser preference', async ({ page, session }) => {
    const before = snapshot(session.data);
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await open(page, session);
    await page.keyboard.press('F2');
    await expect(page.locator('.cvat-settings-modal')).toBeVisible();
    const smooth = page.getByRole('checkbox', { name: 'Smooth image', exact: true });
    await expect(smooth).toBeChecked();
    await smooth.uncheck();
    await expect(page.locator('#cvat_canvas_background')).toHaveCSS('image-rendering', 'pixelated');
    await page.locator('.cvat-close-settings-button').click();
    await page.reload();
    await expect(page.locator('#cvat_canvas_background')).toHaveCSS('image-rendering', 'pixelated');
    await page.locator('.cvat-annotation-header-menu-button').click();
    await page.getByRole('menuitem', { name: 'Settings', exact: true }).click();
    await expect(smooth).not.toBeChecked();
    await smooth.check();
    await expect(page.locator('#cvat_canvas_background')).toHaveCSS('image-rendering', 'auto');
    await page.locator('.cvat-close-settings-button').focus();
    await page.keyboard.press('F2');
    await expect(page.locator('.cvat-settings-modal')).toBeHidden();
    expect(snapshot(session.data)).toEqual(before);
    expect(errors).toEqual([]);
});

test.describe('Direct native review', () => {
    test.use({ mode: 'review' });
    test('opens Review directly, persists findings and restores them after a process restart', async ({ page, context, session }) => {
        const before = snapshot(session.data);
        await open(page, session);
        await expect(page.locator('.cvat-review-workspace')).toBeVisible();
        await expect(page.locator('.cvat-draw-rectangle-control')).toHaveCount(0);
        await page.locator('.cvat-issue-control').click();
        const area = await page.locator('#cvat_canvas_background').boundingBox();
        await page.mouse.move(area.x + area.width * .2, area.y + area.height * .2);
        await page.mouse.down();
        await page.mouse.move(area.x + area.width * .4, area.y + area.height * .5, { steps: 8 });
        await page.mouse.up();
        await page.getByPlaceholder('Please, describe the issue').fill('Tighten this native CVAT box.');
        const response = page.waitForResponse((item) => item.url().includes('/api/cvat/issues') && item.request().method() === 'POST');
        await page.locator('.cvat-create-issue-dialog-submit-button').click();
        expect((await response).status()).toBe(200);
        await context.grantPermissions(['clipboard-read', 'clipboard-write']);
        await page.locator('.cvat-annotation-header-menu-button').click();
        await page.getByRole('menuitem', { name: 'Copy feedback', exact: true }).click();
        await expect(page.getByRole('dialog')).toContainText('Review feedback');
        await expect(page.locator('.ant-modal textarea')).toHaveValue(/Tighten this native CVAT box/);
        expect(await page.evaluate(() => navigator.clipboard.readText())).toContain('source.mp4/frame_000001.png');
        await page.getByRole('button', { name: 'Close', exact: true }).click();
        await page.reload();
        await expect(page.locator('.cvat-review-workspace')).toBeVisible();
        await page.getByRole('tab', { name: 'Issues', exact: true }).click();
        await expect(page.getByText('Tighten this native CVAT box.', { exact: true }).first()).toBeVisible();
        const files = snapshot(session.data);
        const reviews = JSON.parse(fs.readFileSync(path.join(session.data, 'reviews.json'), 'utf8'));
        expect(reviews.issues[0].image).toBe('source.mp4/frame_000001.png');
        expect(JSON.stringify(reviews)).not.toContain(session.directory);
        delete files['reviews.json'];
        expect(files).toEqual(before);
        await session.restart();
        await page.goto(session.url);
        await expect(page.locator('.cvat-review-workspace')).toBeVisible();
        await page.getByRole('tab', { name: 'Issues', exact: true }).click();
        await expect(page.getByText('Tighten this native CVAT box.', { exact: true }).first()).toBeVisible();
    });
});

test.describe('Pillow pages', () => {
    test.use({ mediaType: 'images' });
    test('opens TIFF and animated GIF pages in CVAT own frame player', async ({ page, session }) => {
        await open(page, session);
        await page.locator('.cvat-player-next-button').click();
        await expect(page.getByText('sequence.gif/frame_000001.png', { exact: true })).toBeVisible();
        await draw(page, 'rectangle', [[.1, .1], [.25, .3]]);
        await save(page);
        expect(fs.readFileSync(session.xml, 'utf8')).toContain('name="sequence.gif/frame_000001.png"');
        await page.locator('.cvat-player-next-button').click();
        await expect(page.getByText('sequence.gif/frame_000002.png', { exact: true })).toBeVisible();
        expect(fs.readdirSync(session.data).sort()).toEqual(['annotations.xml', 'image_01.tiff', 'sequence.gif']);
    });
});

test.describe('Native video chunk buffering', () => {
    test.use({ videoFrames: 96, mode: 'review' });
    test('plays buffered frames and keeps the newest scrub position after an abandoned request', async ({ page, session }) => {
        const before = snapshot(session.data);
        const errors = [];
        const chunks = [];
        const singles = [];
        page.on('pageerror', (error) => errors.push(error.message));
        page.on('request', (request) => {
            if (request.url().includes('/api/cvat/chunks?')) chunks.push(request.url());
            if (request.url().includes('/api/image?')) singles.push(request.url());
        });
        const bootstrap = page.waitForResponse((response) => response.url().endsWith('/api/cvat/session'));
        await open(page, session);
        const boot = await (await bootstrap).json();
        expect(boot.meta.chunk_size).toBeGreaterThan(1);
        const input = page.locator('.cvat-player-frame-selector input');
        let delayed = false;
        await page.route('**/api/cvat/chunks?**', async (route) => {
            const chunk = Number(new URL(route.request().url()).searchParams.get('chunk'));
            if (chunk === 2 && !delayed) {
                delayed = true;
                await new Promise((resolve) => setTimeout(resolve, 700));
            }
            await route.continue().catch(() => {});
        });
        await input.fill(String(boot.meta.chunk_size * 2));
        await input.press('Enter');
        await expect.poll(() => delayed).toBe(true);
        await input.fill('80');
        await input.press('Enter');
        await expect(page.locator('.cvat-player-filename-wrapper')).toHaveText('source.mp4/frame_000081.png');
        await page.waitForTimeout(800);
        await expect(input).toHaveValue('80');
        await expect(page.locator('.cvat-player-filename-wrapper')).toHaveText('source.mp4/frame_000081.png');
        await input.fill('0');
        await input.press('Enter');
        await expect(page.locator('.cvat-player-filename-wrapper')).toHaveText('source.mp4/frame_000001.png');
        const requestsBeforePlay = chunks.length;
        await page.locator('.cvat-player-play-button').click();
        await expect.poll(async () => Number(await input.inputValue())).toBeGreaterThanOrEqual(boot.meta.chunk_size + 3);
        await page.locator('.cvat-player-pause-button').click();
        expect(chunks.length - requestsBeforePlay).toBeLessThan(8);
        expect(singles).toEqual([]);
        await expect(page.locator('.ant-notification-notice-error')).toHaveCount(0);
        expect(errors).toEqual([]);
        expect(session.errors()).not.toMatch(/BrokenPipeError|ConnectionResetError|Traceback/);
        expect(snapshot(session.data)).toEqual(before);
    });
});

test.describe('Configured native CVAT defaults', () => {
    test.use({ configuration: 'player:\n  step: 2\n  pixelated: true\nexport:\n  dataset_format: "COCO 1.0"\n  labels_format: "YOLO 1.1"\nbuffer:\n  seconds: 8\n  max_memory: 64MiB\n' });
    test('applies display, player, buffer and separate export format defaults', async ({ page, session }) => {
        const bootstrap = page.waitForResponse((response) => response.url().endsWith('/api/cvat/session'));
        await open(page, session);
        const boot = await (await bootstrap).json();
        expect(boot.settings.frame_step).toBe(2);
        expect(boot.buffer.seconds).toBe(8);
        expect(boot.buffer.max_bytes).toBeLessThanOrEqual(64 * 1024 * 1024);
        await expect(page.locator('#cvat_canvas_background')).toHaveCSS('image-rendering', 'pixelated');
        for (const [menu, format] of [['Export dataset', 'COCO 1.0'], ['Export labels', 'YOLO 1.1']]) {
            await page.locator('.cvat-annotation-header-menu-button').click();
            await page.getByRole('menuitem', { name: menu, exact: true }).click();
            const dialog = page.locator('.cvat-modal-export-job');
            await expect(dialog.locator('.ant-select-selection-item')).toHaveText(format);
            await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
        }
        await page.keyboard.press('F2');
        await expect(page.getByRole('checkbox', { name: 'Smooth image', exact: true })).not.toBeChecked();
        await expect(page.locator('.cvat-player-settings-step input')).toHaveValue('2');
    });
});
