import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {test} from 'node:test';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const script = path.join(root, '..', 'app', 'static', 'company_workbook_grid.js');
const chromePath = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
let chromium;
try { ({chromium} = await import('playwright')); } catch { /* Browser QA is optional in CI. */ }

test('direct company workbook grid previews and saves a new version',
  {skip: !chromium || !existsSync(chromePath)}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: chromePath});
  try {
    const page = await browser.newPage();
    await page.setContent(`<select id="companyWorkbookDocument"></select><input id="companyWorkbookMonth" type="month" value="2026-10">
      <select id="companyWorkbookKind"><option value="finance">收支</option></select><p id="companyWorkbookStatus"></p>
      <div id="companyWorkbookTable"></div><button id="companyWorkbookPreview" onclick="previewCompanyWorkbook()" disabled>預覽</button>
      <input id="companyWorkbookConfirm" type="checkbox" disabled><button id="companyWorkbookSave" onclick="saveCompanyWorkbook()" disabled>保存</button>
      <div id="companyWorkbookReview" hidden></div>`);
    await page.evaluate(() => {
      window.state = {managedDocuments: [
        {id: 7, category: '通訊錄與年度管理', original_file_name: '舊版.xlsx'},
        {id: 11, category: '通訊錄與年度管理', original_file_name: '新版11510.xlsx'},
        {id: 10, category: '收支明細', original_file_name: '六和11510.xlsx'},
      ]};
      window.escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;');
      window.escapeAttr = window.escapeHtml;
      window.requests = [];
      window.api = async (url, options) => {
        const body = options?.body ? JSON.parse(options.body) : null;
        window.requests.push({url, body});
        if (url.includes('?')) return {document_id: Number(new URLSearchParams(url.split('?')[1]).get('document_id')),
          document_name: '新版11510.xlsx', month: '2026-10', kind: 'finance', sheet_name: '115年10月_收支明細表',
          sha256: 'a'.repeat(64), fields: [{key: 'date', label: '日期', type: 'date'}, {key: 'summary', label: '摘要', type: 'text'}],
          rows: [{row_number: 4, name: '', values: {date: '2026-10-01', summary: '舊摘要'}, locked: []}]};
        if (body?.preview_only) return {changed_rows: 1, changes: [{row_number: 4, before: {summary: '舊摘要'}, after: {summary: '新摘要'}}]};
        return {saved: 1, document_id: 12, backup_status: 'saved'};
      };
      window.loadManagedDocuments = async () => {
        window.state.managedDocuments.push({id: 12, category: '通訊錄與年度管理', original_file_name: '新版11510_線上編輯.xlsx'});
        renderCompanyWorkbookOptions();
      };
    });
    await page.addScriptTag({path: script});
    await page.evaluate(() => { renderCompanyWorkbookOptions(); return loadCompanyWorkbook(); });
    assert.equal(await page.locator('#companyWorkbookDocument').inputValue(), '11');
    await page.locator('input[data-key="summary"]').fill('新摘要');
    await page.locator('#companyWorkbookPreview').click();
    await page.locator('#companyWorkbookReview').waitFor({state: 'visible'});
    assert.match(await page.locator('#companyWorkbookReview').innerText(), /舊摘要 → 新摘要/);
    await page.locator('#companyWorkbookConfirm').check();
    await page.locator('#companyWorkbookSave').click();
    await page.waitForFunction(() => document.getElementById('companyWorkbookDocument').value === '12');
    assert.equal(await page.locator('#companyWorkbookDocument').inputValue(), '12');
    assert.match(await page.locator('#companyWorkbookStatus').innerText(), /備份成功/);
    const requests = await page.evaluate(() => window.requests);
    assert.equal(requests.find(item => item.body?.preview_only).body.rows[0].values.summary, '新摘要');
    assert.equal(requests.find(item => item.body?.confirmed).body.document_id, 11);
  } finally { await browser.close(); }
});
