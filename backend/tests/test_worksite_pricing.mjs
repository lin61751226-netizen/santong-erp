import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {test} from 'node:test';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const script = path.join(root, '..', 'app', 'static', 'worksite_pricing.js');
const chromePath = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
let chromium;
try { ({chromium} = await import('playwright')); } catch { /* Browser QA is optional in CI. */ }

test('Guotai November pricing previews and saves per vehicle and day',
  {skip: !chromium || !existsSync(chromePath)}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: chromePath});
  try {
    const page = await browser.newPage();
    await page.setContent('<input id="worksitePricingMonth" type="month" value="2026-11"><p id="worksitePricingStatus"></p><div id="worksitePricingTabs"></div><div id="worksitePricingJournalImport" hidden><strong id="worksitePricingJournalDate"></strong><input id="worksitePricingJournalUnits" type="number" value="2"><input id="worksitePricingJournalNormal" type="number"><input id="worksitePricingJournalOvertime" type="number"><input id="worksitePricingJournalDetail"><input id="worksitePricingJournalOvertimeDetail"></div><div id="worksitePricingTable"></div>');
    await page.evaluate(() => {
      window.escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;');
      window.escapeAttr = window.escapeHtml;
      window.confirm = () => true;
      Object.defineProperty(window.crypto, 'randomUUID', {value: () => 'test-price-request-0001'});
      window.requests = [];
      const rate = {weekday_hourly: 1200, weekday_daily: 8000, holiday_hourly: 1500,
        holiday_daily: 10000, overtime_hourly: 1500, effective_from: '2026-11-01'};
      const row = {day: '2026-11-02', revision: 0, units: 0, normal_hours: 0,
        overtime_hours: 0, holiday_reason: '', external_customer: '', fuel_cost: 0, payer: '',
        normal_amount: 0, overtime_amount: 0, calculated_total: 0, total_amount: 0,
        manual_total_amount: null, manual_amount_reason: ''};
      window.api = async (url, options) => {
        window.requests.push({url, body: options?.body ? JSON.parse(options.body) : null});
        if (url === '/api/worksites') return [{id: 14, code: '國泰慈文', name: '國泰慈文'}];
        if (url.startsWith('/api/worksite-pricing/month?')) return {site_id: 14, site_name: '國泰慈文', year: 2026, month: 11,
          sheets: [{vehicle_type: '2.5噸（2節桿）', rate, rows: [row]}]};
        if (options?.body && JSON.parse(options.body).preview_only) return {changed: true, normal_amount: 8000, overtime_amount: 3000, calculated_total: 11000, total_amount: 12500};
        return {saved: true, revision: 1, normal_amount: 8000, overtime_amount: 3000, calculated_total: 11000, total_amount: 12500, backup: {status: 'saved'}};
      };
    });
    await page.addScriptTag({path: script});
    await page.evaluate(() => loadWorksitePricing());
    await page.locator('input[data-field="units"]').fill('1');
    await page.locator('input[data-field="normal_hours"]').fill('8');
    await page.locator('input[data-field="overtime_hours"]').fill('2');
    await page.locator('input[data-field="manual_total_amount"]').fill('12500');
    await page.locator('input[data-field="manual_amount_reason"]').fill('依核定金額');
    await page.locator('#worksitePricingTable button').click();
    const result = await page.evaluate(() => ({requests: window.requests, status: document.getElementById('worksitePricingStatus').textContent}));
    assert.equal(result.requests.find(item => item.body?.preview_only).body.work_date, '2026-11-02');
    assert.equal(result.requests.find(item => item.body?.confirmed).body.normal_hours, 8);
    assert.equal(result.requests.find(item => item.body?.confirmed).body.manual_total_amount, 12500);
    assert.match(result.status, /備份成功/);
    await page.evaluate(() => {
      worksitePricing.journalImport = {day: '2026-11-02', normalHours: 10, overtimeHours: 4};
      renderWorksitePricingJournalImport();
      applyJournalToWorksitePricing();
    });
    assert.equal(await page.locator('input[data-field="normal_hours"]').inputValue(), '5');
    assert.equal(await page.locator('input[data-field="overtime_hours"]').inputValue(), '2');
    assert.equal(await page.locator('input[data-field="manual_total_amount"]').inputValue(), '');
    assert.match(await page.locator('#worksitePricingStatus').textContent(), /平均每台 5\/2 小時/);
    await page.evaluate(() => {
      worksitePricing.journalImport = {day: '2026-11-02', normalHours: 5, overtimeHours: 0};
      renderWorksitePricingJournalImport();
    });
    await page.locator('#worksitePricingJournalDetail').fill('2,3');
    await page.evaluate(() => applyJournalToWorksitePricing());
    assert.equal(await page.locator('input[data-field="normal_hours_detail"]').inputValue(), '2,3');
    assert.equal(await page.locator('input[data-field="normal_hours"]').inputValue(), '0');
    assert.match(await page.locator('#worksitePricingStatus').textContent(), /逐台 2,3 小時/);
  } finally { await browser.close(); }
});
