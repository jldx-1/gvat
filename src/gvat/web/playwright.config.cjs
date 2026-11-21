const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
    testDir: './tests',
    testMatch: '**/*.spec.cjs',
    workers: 1,
    timeout: 45000,
    use: { browserName: 'chromium', headless: true, viewport: { width: 1440, height: 1000 }, screenshot: 'only-on-failure' },
});
