import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {test} from 'node:test';
import vm from 'node:vm';

const base = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'app');
const code = readFileSync(path.join(base, 'static', 'data_workflow.js'), 'utf8');
const html = readFileSync(path.join(base, 'templates', 'index.html'), 'utf8');
const css = readFileSync(path.join(base, 'static', 'app.css'), 'utf8');

function fixture() {
  const ids = ['dataWorkflowKind', 'dataWorkflowSource', 'dataWorkflowMonth', 'dataWorkflowHint',
    'companyWorkbookMonth', 'companyWorkbookKind', 'companyWorkbookDocument', 'costGridMonth',
    'costGridDocument', 'worksitePricingMonth', 'managementGridMonth', 'dataMonth',
    'uploadedDataCards', 'uploadedDataStatus', 'monthlyRevenueAmount', 'monthlyRevenuePeriod',
    'monthlyRevenueDetail'];
  const elements = Object.fromEntries(ids.map(id => [id, {value: '', textContent: '', innerHTML: ''}]));
  const context = {
    document: {getElementById: id => elements[id]},
    state: {managedDocuments: []}, managementGrid: {mode: 'pricing'},
    todayString: () => '2026-11-14',
    formatTW: () => '2026/11/14',
    escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;'),
    escapeAttr: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'),
    costGridDirty: () => false, companyWorkbookDirty: () => false,
  };
  vm.createContext(context);
  vm.runInContext(code, context);
  return {context, elements};
}

test('workflow offers four clear jobs and a visible uploaded-file preview', () => {
  assert.match(html, /id="dataWorkflowKind"/);
  assert.match(html, /id="dataWorkflowMonth"/);
  assert.match(html, /id="uploadedDataCards"/);
  assert.match(html, /在 APP 預覽/);
  assert.match(css, /grid-template-columns: repeat\(12, minmax\(0, 1fr\)\)/);
  const {context, elements} = fixture();
  context.initDataWorkflow();
  assert.equal(elements.dataWorkflowMonth.value, '2026-11');
  context.state.managedDocuments = [{id: 5, category: '收支明細', original_file_name: '<檔案>.xlsx', created_at: '2026-11-14'}];
  context.renderUploadedDataCards();
  assert.match(elements.uploadedDataCards.innerHTML, /&lt;檔案>.xlsx/);
  assert.doesNotMatch(elements.uploadedDataCards.innerHTML, /<檔案>/);
  assert.match(elements.uploadedDataCards.innerHTML, /openManagedDocument\(5\)/);
});

test('month and source selection use existing save-safe editor rather than creating records', async () => {
  const {context, elements} = fixture();
  context.initDataWorkflow();
  context.state.managedDocuments = [
    {id: 7, category: '收支明細', original_file_name: '收支.xlsx'},
    {id: 9, category: '通訊錄與年度管理', original_file_name: '全年管理.xlsx'},
  ];
  elements.companyWorkbookDocument.value = '7';
  elements.dataWorkflowKind.value = 'roster';
  elements.dataWorkflowSource.value = 'workbook';
  elements.dataWorkflowMonth.value = '2026-10';
  let loaded = 0;
  context.showExcelMode = (mode, options) => {
    assert.equal(mode, 'workbook');
    assert.equal(options.autoLoad, false);
    context.managementGrid.mode = mode;
    return true;
  };
  context.loadCompanyWorkbook = async () => { loaded++; };
  await context.applyDataWorkflow();
  assert.equal(elements.companyWorkbookKind.value, 'roster');
  assert.equal(elements.companyWorkbookDocument.value, '9');
  assert.equal(elements.companyWorkbookMonth.value, '2026-10');
  assert.equal(elements.dataWorkflowMonth.value, '2026-10');
  assert.equal(loaded, 1);
});

test('revenue tile distinguishes known subtotal from a complete estimate', async () => {
  const {context, elements} = fixture();
  context.api = async () => ({month: '2026-11', as_of: '2026-11-14', complete: false,
    legacy_amount: null, legacy_document: null, site_amount: 6400,
    known_total: 6400, issue: '原計價檔暫時無法讀取'});
  await context.loadMonthlyRevenueEstimate();
  assert.match(elements.monthlyRevenueAmount.textContent, /已知小計 NT\$ 6,400/);
  assert.match(elements.monthlyRevenueDetail.textContent, /未完整/);
  context.api = async () => { throw Error('Google Drive 暫時無法讀取'); };
  await context.loadMonthlyRevenueEstimate();
  assert.equal(elements.monthlyRevenueAmount.textContent, '目前無法核對');
  assert.doesNotMatch(elements.monthlyRevenueAmount.textContent, /0/);
});
