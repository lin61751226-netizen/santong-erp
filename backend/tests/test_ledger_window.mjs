import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {test} from 'node:test';
import vm from 'node:vm';

const base = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'app');
const code = readFileSync(path.join(base, 'static', 'document_data.js'), 'utf8');
const html = readFileSync(path.join(base, 'templates', 'index.html'), 'utf8');

function fixture() {
  const elements = Object.fromEntries([
    'dataKind', 'dataMonth', 'dataWorkflowMonth', 'dataEditorSection', 'dataEditForm',
    'dataWindowPeriod', 'dataWindowTotal', 'dataWindowFiltered', 'dataWindowIncome', 'dataWindowExpense',
  ].map(id => [id, {value: '', textContent: '', scrollIntoView() {}, addEventListener() {}}]));
  const calls = [];
  const context = {
    document: {getElementById: id => elements[id] || {addEventListener() {}}},
    window: {addEventListener() {}},
    showExcelMode: (...args) => { calls.push(['mode', ...args]); return true; },
    loadDocumentData: (...args) => { calls.push(['load', ...args]); },
  };
  vm.createContext(context);
  vm.runInContext(code, context);
  context.loadDocumentData = (...args) => { calls.push(['load', ...args]); };
  return {context, elements, calls};
}

test('the visible window shortcut opens existing finance data for selected month', () => {
  assert.match(html, /開啟視窗式記帳工作台/);
  const {context, elements, calls} = fixture();
  elements.dataWorkflowMonth.value = '2026-11';
  context.openDataLedgerWindow();
  assert.equal(elements.dataKind.value, 'finance');
  assert.equal(elements.dataMonth.value, '2026-11');
  assert.equal(calls[0][0], 'mode');
  assert.equal(calls[0][1], 'finance');
  assert.equal(calls.filter(call => call[0] === 'load').length, 1);
});

test('summary labels amounts as current page and does not invent contact totals', () => {
  const {context, elements} = fixture();
  elements.dataMonth.value = '2026-11';
  context.renderDataWindowSummary({kind: 'finance', document: {name: '公司原檔'},
    dataset_total: 40, total: 30, rows: [{values: {income_amount: 100, expense_amount: 20}}, {values: {income_amount: 50, expense_amount: 0}}]});
  assert.equal(elements.dataWindowTotal.textContent, '40');
  assert.equal(elements.dataWindowFiltered.textContent, '30');
  assert.equal(elements.dataWindowIncome.textContent, 'NT$ 150');
  assert.equal(elements.dataWindowExpense.textContent, 'NT$ 20');
  context.renderDataWindowSummary({kind: 'contacts', document: {name: '公司原檔'}, dataset_total: 4, total: 4, rows: []});
  assert.equal(elements.dataWindowIncome.textContent, '不適用');
});
