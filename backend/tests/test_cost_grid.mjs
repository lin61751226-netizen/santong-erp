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
      <section id="costGridAdjustment" hidden><h3 id="costGridAdjustmentTitle"></h3><p id="costGridAdjustmentFormula"></p>
        <input id="costGridAdjustmentAmount" type="number" min="0" max="1000000000" step="0.01" oninput="editCostGridAdjustment()">
        <input id="costGridAdjustmentReason" oninput="editCostGridAdjustment()">
        <button id="costGridAdjustmentSave" disabled></button><div id="costGridAdjustmentReview" hidden></div></section>
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

test('53 adjustment previews 6000 to 6400 and saves without changing Excel hours',
  {skip: !chromium || !existsSync(chromePath)}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: chromePath});
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <select id="costGridDocument"></select><input id="costGridMonth" type="month">
      <p id="costGridStatus"></p><div id="costGridTabs"></div><div id="costGridSheet"></div>
      <section id="costGridAdjustment" hidden><h3 id="costGridAdjustmentTitle"></h3><p id="costGridAdjustmentFormula"></p>
        <input id="costGridAdjustmentAmount" type="number" min="0" max="1000000000" step="0.01" oninput="editCostGridAdjustment()">
        <input id="costGridAdjustmentReason" oninput="editCostGridAdjustment()">
        <button id="costGridAdjustmentSave" disabled></button><div id="costGridAdjustmentReview" hidden></div></section>
      <button id="costGridPreview"></button><input id="costGridConfirm" type="checkbox">
      <button id="costGridSave"></button><div id="costGridReview" hidden></div>
    `);
    await page.evaluate(() => {
      window.state = {managedDocuments: [{id: 7, category: '推高機計價', original_file_name: '計價.xlsm'}]};
      window.todayString = () => '2026-10-23';
      window.escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
      window.escapeAttr = window.escapeHtml;
      window.confirm = () => true;
      window.requests = [];
      window.savedAdjustment = false;
      window.api = async (url, options) => {
        const body = options?.body ? JSON.parse(options.body) : null;
        window.requests.push({url, body});
        if (url.includes('/amount-adjustment')) {
          if (body.preview_only) return {day: body.day, formula_amount: 6000, before: 6000,
            after: body.manual_total_amount, reason: body.reason, changed: true};
          window.savedAdjustment = true;
          return {day: body.day, saved: true, backup_status: 'saved'};
        }
        return {document_id: 7, document_name: '計價.xlsm', year: 2026, month: 10,
          sha256: 'a'.repeat(64), tax_rate: 0.05, sheets: [{label: '53', worksheet_name: '11510-53',
            rates: {normal_hourly: 800, daily: 6000, ot_rate: 1000, holiday_rate: 1000, support_rate: 800},
            rows: [{day: '2026-10-23', normal_hours: 8, normal_amount: 6000, overtime_hours: 0,
              overtime_amount: 0, support_hours: 0, support_amount: 0, total: 6000,
              effective_total: window.savedAdjustment ? 6400 : 6000,
              manual_total_amount: window.savedAdjustment ? 6400 : null,
              amount_adjustment_reason: window.savedAdjustment ? '依簽單核定' : '',
              adjustment_revision: window.savedAdjustment ? 1 : 0, adjustment_stale: false,
              external_customer: null, fuel_cost: null, payer: null}]}]};
      };
    });
    await page.addScriptTag({path: script});
    await page.evaluate(async () => { renderCostGridDocumentOptions(); await loadCostGrid(); });
    await page.locator('#costGridSheet button').click();
    await page.locator('#costGridAdjustmentAmount').fill('6400');
    await page.locator('#costGridAdjustmentReason').fill('依簽單核定');
    await page.evaluate(() => previewCostGridAdjustment());
    assert.match(await page.locator('#costGridAdjustmentReview').innerText(), /6,000 → 核定後 6,400/);
    assert.equal(await page.locator('#costGridAdjustmentSave').isDisabled(), false);
    await page.evaluate(() => saveCostGridAdjustment());
    const result = await page.evaluate(() => ({requests: window.requests, status: document.getElementById('costGridStatus').textContent}));
    assert.equal(result.requests.filter(item => item.url.includes('/amount-adjustment')).length, 2);
    assert.equal(result.requests.find(item => item.body?.confirmed).body.manual_total_amount, 6400);
    assert.match(result.status, /Google Drive 資料庫備份成功/);
    assert.match(await page.locator('#costGridSheet').innerText(), /6,400/);
    assert.equal(await page.locator('input[data-field="normal_hours"]').inputValue(), '8');
  } finally { await browser.close(); }
});
