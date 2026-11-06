import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { test } from 'node:test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const script = path.join(root, '..', 'app', 'static', 'cost_grid.js');
const chromePath = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
let chromium;
try { ({chromium} = await import('playwright')); } catch { /* Browser QA is optional in CI. */ }

test('month grid edits, previews, confirms and saves without losing the selected sheet',
  {skip: !chromium || !existsSync(chromePath)}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: chromePath});
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <select id="costGridDocument"></select><input id="costGridMonth" type="month">
      <p id="costGridStatus"></p><div id="costGridTabs"></div><div id="costGridSheet"></div>
      <button id="costGridPreview"></button><input id="costGridConfirm" type="checkbox">
      <button id="costGridSave"></button><div id="costGridReview" hidden></div>
      <button id="costGridBackup"></button>
    `);
    await page.evaluate(() => {
      window.state = {managedDocuments: [{id: 7, category: '推高機計價', original_file_name: '計價.xlsm'}]};
      window.todayString = () => '2026-10-01';
      window.escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
      window.escapeAttr = window.escapeHtml;
      window.confirm = () => true;
      window.requests = [];
      const row = {day: '2026-10-01', row_number: 5, is_holiday: false,
        normal_hours: null, normal_amount: 0, overtime_hours: null, overtime_amount: 0,
        support_hours: null, support_amount: 0, external_customer: null, fuel_cost: null, payer: null};
      const rates = {normal_hourly: 800, daily: 6000, ot_rate: 1000, holiday_rate: 1000, support_rate: 800};
      window.api = async (url, options) => {
        window.requests.push({url, body: options?.body ? JSON.parse(options.body) : null});
        if (!options) return {document_id: 7, document_name: '計價.xlsm', year: 2026, month: 10, sha256: 'a'.repeat(64), tax_rate: 0,
          sheets: [{label: '29', worksheet_name: '11510-29', rates, rows: [row]},
            {label: '53', worksheet_name: '11510-53', rates, rows: [row]}]};
        if (options.body && JSON.parse(options.body).preview_only) return {changed_rows: 1, amount_changes: [{day: '2026-10-01', previous: 0, new: 6000}]};
        return {saved: 1, backup_status: 'saved'};
      };
    });
    await page.addScriptTag({path: script});
    await page.evaluate(async () => { renderCostGridDocumentOptions(); await loadCostGrid(); selectCostGridSheet(1); });
    await page.locator('input[data-field="normal_hours"]').fill('8');
    assert.equal(await page.locator('#costGridPreview').isDisabled(), false);
    await page.evaluate(() => previewCostGrid());
    assert.match(await page.locator('#costGridReview').innerText(), /6,000/);
    assert.equal(await page.locator('#costGridSave').isDisabled(), true);
    await page.locator('#costGridConfirm').check();
    await page.evaluate(() => saveCostGrid());
    const result = await page.evaluate(() => ({requests: window.requests, status: document.getElementById('costGridStatus').textContent}));
    assert.equal(result.requests[1].body.label, '53');
    assert.equal(result.requests[2].body.confirm_amount_changes, true);
    assert.match(result.status, /已保存 1 個日期/);
    assert.equal(await page.locator('#costGridTabs [aria-selected="true"]').innerText(), '53');
  } finally { await browser.close(); }
});
