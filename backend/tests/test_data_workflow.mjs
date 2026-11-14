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
    'uploadedDataCards', 'uploadedDataStatus', 'uploadedDataHeading', 'uploadedDataScope',
    'uploadedDataHistory', 'uploadedDataHistorySummary', 'uploadedDataHistoryNote', 'uploadedDataHistoryBody',
    'uploadedDataExports', 'uploadedDataExportsSummary', 'uploadedDataExportsNote', 'uploadedDataExportsBody',
    'panel-excel-editor', 'monthlyRevenueAmount', 'monthlyRevenuePeriod', 'monthlyRevenueDetail'];
  const elements = Object.fromEntries(ids.map(id => [id, {
    value: '', textContent: '', innerHTML: '', hidden: false, open: false,
    scrollIntoView() {},
  }]));
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

const companyFiles = [
  {id: 12, category: '通訊錄與年度管理', original_file_name: '三通公司通訊錄_收支明細表_堆高機拉_全年管理版_20261113_185349_1.xlsx', created_at: '2026-11-13T10:53:52'},
  {id: 11, category: '通訊錄與年度管理', original_file_name: '三通公司通訊錄_收支明細表_堆高機拉_全年管理版.xlsx', created_at: '2026-11-07T12:18:59'},
  {id: 10, category: '收支明細', original_file_name: '三通公司收支明細表_六和_最終修正版.xlsx', created_at: '2026-11-07T12:16:37'},
  {id: 9, category: '推高機計價', original_file_name: '三通工程行115年推高機計價1107.xlsm', created_at: '2026-11-07T11:46:05'},
  {id: 8, category: '線上資料匯出', original_file_name: '線上資料匯出_contacts_f250b55d348c.xlsx', created_at: '2026-11-07T11:31:51'},
  {id: 7, category: '通訊錄與年度管理', original_file_name: '三通公司通訊錄_收支明細表_堆高機拉_全年管理版.xlsx', created_at: '2026-10-08T02:35:40'},
  {id: 6, category: '線上資料匯出', original_file_name: '線上資料匯出_finance_56020df1bd04.xlsx', created_at: '2026-09-20T10:53:37'},
  {id: 5, category: '推高機計價', original_file_name: '三通工程行115年推高機計價0730 (1).xlsm', created_at: '2026-09-20T10:17:58'},
  {id: 4, category: '推高機計價', original_file_name: '三通工程行115年推高機計價.xlsm', created_at: '2026-09-20T10:17:42'},
  {id: 3, category: '推高機計價', original_file_name: '三通工程行115年推高機計價.xlsm', created_at: '2026-09-20T07:33:03'},
  {id: 2, category: '收支明細', original_file_name: '三通公司收支明細表_六和.xlsx', created_at: '2026-09-20T07:32:02'},
  {id: 1, category: '通訊錄與年度管理', original_file_name: '三通公司通訊錄_收支明細表_堆高機拉_全年管理版.xlsx', created_at: '2026-09-20T07:31:59'},
];

test('workflow offers four clear jobs and a visible uploaded-file preview', () => {
  assert.match(html, /id="dataWorkflowKind"/);
  assert.match(html, /id="dataWorkflowMonth"/);
  assert.match(html, /2\. 作業月份/);
  assert.doesNotMatch(html, /2\. 資料來源/);
  assert.doesNotMatch(html, /3\. 作業月份/);
  assert.match(html, /id="uploadedDataCards"/);
  assert.match(html, /在 APP 預覽/);
  assert.match(html, /查看全部／上傳新檔/);
  assert.match(css, /@media \(min-width: 981px\) \{ \.grid \{ grid-template-columns: repeat\(12, minmax\(0, 1fr\)\); \} \}/);
  assert.match(css, /overflow:\s*auto/);
  const {context, elements} = fixture();
  context.initDataWorkflow();
  assert.equal(elements.dataWorkflowMonth.value, '2026-11');
  elements.dataWorkflowKind.value = 'finance';
  elements.dataWorkflowSource.value = 'workbook';
  context.state.managedDocuments = [{id: 5, category: '收支明細', original_file_name: '<檔案>.xlsx', created_at: '2026-11-14'}];
  context.renderUploadedDataCards();
  assert.match(elements.uploadedDataCards.innerHTML, /&lt;檔案>.xlsx/);
  assert.doesNotMatch(elements.uploadedDataCards.innerHTML, /<檔案>/);
  assert.match(elements.uploadedDataCards.innerHTML, /openUploadedDataCard\(5\)/);
  assert.match(elements.uploadedDataCards.innerHTML, /開啟/);
  assert.doesNotMatch(elements.uploadedDataCards.innerHTML, /openManagedDocument\(5\)/);
  context.openUploadedDataCard(5);
  assert.match(elements.uploadedDataCards.innerHTML, /openManagedDocument\(5\)/);
  assert.match(elements.uploadedDataCards.innerHTML, /預覽/);
  assert.match(elements.uploadedDataCards.innerHTML, /處理這個月/);
});

test('other sources and old entries stay folded, with the uploaded workbook as the default', () => {
  const start = html.indexOf('id="dataWorkflowOtherSources"');
  const fold = html.slice(start - 80, html.indexOf('</details>', start));
  assert.match(fold, /<details class="data-workflow-advanced" id="dataWorkflowOtherSources">/);
  assert.doesNotMatch(fold.slice(0, 160), /\sopen(?:=""|\s|>)/);
  assert.match(fold, /其他資料來源（系統資料／舊快照等）/);
  assert.match(fold, /id="dataWorkflowSource"/);
  assert.match(fold, /切換其他來源或檢查舊版入口/);
  assert.match(fold, /excelModeSitepricing/);
  assert.match(fold, /excelModePayroll/);
  assert.match(html, /id="uploadedDataUpload"/);
  assert.match(html, /上傳新檔案/);
  assert.match(html, /id="uploadedDataHistory"/);
  assert.match(html, /id="uploadedDataExports"/);
  const {context, elements} = fixture();
  elements.companyWorkbookDocument.value = '12';
  elements.dataWorkflowKind.value = 'finance';
  context.showExcelMode = () => true;
  context.loadCompanyWorkbook = async () => {};
  context.changeDataWorkflowKind();
  assert.equal(elements.dataWorkflowSource.value, 'workbook');
  assert.match(elements.dataWorkflowSource.innerHTML, /已上傳 Excel 原表/);
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

test('type filter keeps newest workbook versions and folds history plus system exports', () => {
  const {context, elements} = fixture();
  elements.dataWorkflowSource.value = 'workbook';
  elements.companyWorkbookDocument.value = '12';
  elements.costGridDocument.value = '9';
  context.state.managedDocuments = companyFiles;

  elements.dataWorkflowKind.value = 'finance';
  context.renderUploadedDataCards();
  assert.match(elements.uploadedDataHeading.textContent, /收支明細的檔案/);
  assert.match(elements.uploadedDataCards.innerHTML, /最新 #12/);
  assert.match(elements.uploadedDataCards.innerHTML, /全年管理版_20261113_185349_1\.xlsx/);
  assert.match(elements.uploadedDataCards.innerHTML, /目前使用中/);
  assert.match(elements.uploadedDataCards.innerHTML, /最新 #10/);
  assert.match(elements.uploadedDataCards.innerHTML, /六和_最終修正版/);
  assert.doesNotMatch(elements.uploadedDataCards.innerHTML, /計價1107|計價0730|計價\.xlsm|contacts_|finance_/);
  assert.equal(elements.uploadedDataHistory.hidden, false);
  assert.match(elements.uploadedDataHistorySummary.textContent, /歷史版本（4 份）/);
  assert.match(elements.uploadedDataHistoryNote.textContent, /#1、#7、#11 全年管理版，#2 六和/);
  assert.match(elements.uploadedDataHistoryBody.innerHTML, /data-document-id="11"/);
  assert.match(elements.uploadedDataHistoryBody.innerHTML, /data-document-id="1"/);
  assert.doesNotMatch(elements.uploadedDataHistoryBody.innerHTML, /最新 #/);
  assert.equal(elements.uploadedDataExports.hidden, false);
  assert.match(elements.uploadedDataExportsSummary.textContent, /系統匯出紀錄（2 份）/);
  assert.match(elements.uploadedDataExportsNote.textContent, /contacts／finance/);
  assert.match(elements.uploadedDataExportsNote.textContent, /不列入主清單/);
  assert.doesNotMatch(elements.uploadedDataCards.innerHTML, /data-document-id="8"|data-document-id="6"/);

  elements.dataWorkflowKind.value = 'pricing';
  elements.dataWorkflowSource.value = 'pricing';
  context.renderUploadedDataCards();
  assert.match(elements.uploadedDataHeading.textContent, /計價的檔案/);
  assert.match(elements.uploadedDataCards.innerHTML, /最新 #9/);
  assert.match(elements.uploadedDataCards.innerHTML, /計價1107\.xlsm/);
  assert.match(elements.uploadedDataCards.innerHTML, /目前使用中/);
  assert.doesNotMatch(elements.uploadedDataCards.innerHTML, /六和|全年管理|contacts_|finance_/);
  assert.match(elements.uploadedDataHistorySummary.textContent, /歷史版本（3 份）/);
  assert.match(elements.uploadedDataHistoryBody.innerHTML, /data-document-id="5"/);
  assert.match(elements.uploadedDataHistoryBody.innerHTML, /data-document-id="3"/);
  assert.equal(elements.uploadedDataExports.hidden, true);

  elements.dataWorkflowKind.value = 'payroll';
  elements.dataWorkflowSource.value = 'workbook';
  context.renderUploadedDataCards();
  assert.match(elements.uploadedDataCards.innerHTML, /最新 #12/);
  assert.doesNotMatch(elements.uploadedDataCards.innerHTML, /六和|計價/);
  assert.match(elements.uploadedDataHistorySummary.textContent, /歷史版本（3 份）/);

  const edited = {id: 13, category: '通訊錄與年度管理', original_file_name: '三通公司通訊錄_收支明細表_堆高機拉_全年管理版_線上編輯_20261114_090000_12.xlsx', created_at: '2026-11-14T01:00:00'};
  const withEdit = context.dataWorkflowPartition([...companyFiles, edited], 'finance');
  assert.equal([...withEdit.current].map(item => item.id).join(','), '13,10');
  assert.equal([...withEdit.history].map(item => item.id).join(','), '12,11,7,2,1');

  const unrelated = context.dataWorkflowPartition(
    [{id: 9, category: '推高機計價', original_file_name: '三通工程行115年推高機計價1107.xlsm'}],
    'roster',
  );
  assert.equal(unrelated.current.length, 0);
  assert.equal(unrelated.history.length, 0);
  assert.equal(unrelated.exports.length, 0);
  elements.dataWorkflowKind.value = 'roster';
  context.state.managedDocuments = [{id: 9, category: '推高機計價', original_file_name: '三通工程行115年推高機計價1107.xlsm'}];
  context.renderUploadedDataCards();
  assert.equal(elements.uploadedDataCards.innerHTML, '');
  assert.match(elements.uploadedDataStatus.textContent, /尚無已上傳的 Excel/);
  assert.match(elements.uploadedDataStatus.textContent, /上傳新檔案/);
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
