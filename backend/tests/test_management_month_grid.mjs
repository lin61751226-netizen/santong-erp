import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {test} from 'node:test';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const script = path.join(root, '..', 'app', 'static', 'management_month_grid.js');
const chromePath = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
let chromium;
try { ({chromium} = await import('playwright')); } catch { /* Browser QA is optional in CI. */ }

test('salary month grid previews, saves, and keeps original source version separate',
  {skip: !chromium || !existsSync(chromePath)}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: chromePath});
  try {
    const page = await browser.newPage();
    await page.setContent(`<div id="costGridSection"></div><div id="worksitePricingSection" hidden></div><div id="companyWorkbookSection" hidden></div><div id="dataEditorSection" hidden></div>
      <div id="managementMonthGridSection" hidden><select id="managementGridSnapshot"></select>
      <input id="managementGridMonth" type="month"><p id="managementGridStatus"></p><div id="managementGridTable"></div></div>
      <button id="excelModePricing"></button><button id="excelModeSitepricing"></button><button id="excelModeWorkbook"></button><button id="excelModeFinance"></button>
      <button id="excelModePayroll"></button><button id="excelModeRoster"></button>`);
    await page.evaluate(() => {
      window.todayString = () => '2026-09-01';
      window.escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;');
      window.escapeAttr = window.escapeHtml;
      window.confirm = () => true;
      window.costGridDirty = () => false;
      window.worksitePricingDirty = () => false;
      window.loadDocumentData = () => {};
      Object.defineProperty(window.crypto, 'randomUUID', {value: () => 'test-month-request-0001'});
      window.requests = [];
      const fields = ['底薪', '職務津貼', '交通/伙食', '加班費', '獎金', '其他加給', '勞保', '健保', '勞退自提', '請假扣款', '其他扣款'];
      const values = Object.fromEntries(fields.map(field => [field, 0]));
      values['底薪'] = 55000; values.status = ''; values.note = '';
      window.api = async (url, options) => {
        window.requests.push({url, body: options?.body ? JSON.parse(options.body) : null});
        if (url.endsWith('/sources')) return [{id: 5, name: '全年管理版.xlsx', versions: [{id: 9}]}];
        if (url.includes('/month-grid?')) return {snapshot_id: 9, month: '2026-09', kind: 'payroll', fields,
          rows: [{source: '115年09月_薪資表!6', name: '林育弘', employee_code: 'EMP001', values, revision: 0}]};
        if (options?.body && JSON.parse(options.body).preview_only) return {changed: true, before: {...values}, after: JSON.parse(options.body).values};
        return {saved: true, revision: 1, backup: {status: 'saved'}};
      };
    });
    await page.addScriptTag({path: script});
    await page.evaluate(() => showExcelMode('payroll'));
    assert.equal(await page.locator('[data-total="2"]').innerText(), '55,000');
    await page.locator('input[data-field="底薪"]').fill('60000');
    assert.equal(await page.locator('[data-total="2"]').innerText(), '60,000');
    await page.locator('#managementGridTable button').click();
    const result = await page.evaluate(() => ({requests: window.requests, status: document.getElementById('managementGridStatus').textContent}));
    assert.ok(result.requests.find(item => item.body?.preview_only), JSON.stringify(result));
    assert.equal(result.requests.find(item => item.body?.preview_only).body.values['底薪'], 60000);
    assert.equal(result.requests.find(item => item.body?.confirmed).body.snapshot_id, 9);
    assert.match(result.status, /備份成功/);
  } finally { await browser.close(); }
});
