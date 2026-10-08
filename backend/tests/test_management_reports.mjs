import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = readFileSync(new URL('../app/static/management_reports.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../app/templates/index.html', import.meta.url), 'utf8');
function fixture(api) {
  const controls = new Map();
  const document = {getElementById(id) {
    if (!controls.has(id)) controls.set(id, {value: '', innerHTML: '', textContent: '', disabled: false});
    return controls.get(id);
  }};
  document.getElementById('reportMonth').value = '2026-09';
  document.getElementById('reportKind').value = 'finance';
  const escapeHtml = value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;');
  const ctx = {document, api, URLSearchParams, escapeHtml, escapeAttr: escapeHtml,
    formatTW: String, todayString: () => '2026-10-08'};
  runInNewContext(source, ctx);
  return {ctx, controls};
}
const report = {title: '每月收支明細表', kind: 'finance', month: '2026-09', fields: ['摘要', '金額'],
  rows: [['<svg onload=bad>', 0]], summary: {'收入': 0, '支出': null}, offset: 0, total: 51, snapshot_id: 1};

test('seven report types are present in one dedicated final document panel', () => {
  const panel = html.slice(html.indexOf('<section id="managementReportsPanel"'), html.indexOf('<section class="panel span-12" data-workspace="settings">'));
  assert.equal((panel.match(/<option value="(finance|payroll|roster|calendar|annual|categories|contacts)"/g) || []).length, 7);
  assert.ok(panel.includes('保存對照版本到資料庫'));
});
test('render escapes data, preserves real zero, and enables pagination', async () => {
  const {ctx, controls} = fixture(async () => report);
  await ctx.loadManagementReport();
  assert.ok(controls.get('reportRows').innerHTML.includes('&lt;svg'));
  assert.ok(!controls.get('reportRows').innerHTML.includes('<svg'));
  assert.ok(controls.get('reportSummary').innerHTML.includes('<strong>0</strong>'));
  assert.ok(controls.get('reportSummary').innerHTML.includes('待確認'));
  assert.equal(controls.get('reportNext').disabled, false);
});
test('stale async responses never replace the newly selected report', async () => {
  let finishFirst, finishSecond;
  let calls = 0;
  const {ctx, controls} = fixture(() => new Promise(resolve => {if (++calls === 1) finishFirst = resolve; else finishSecond = resolve;}));
  const first = ctx.loadManagementReport();
  controls.get('reportKind').value = 'payroll';
  const second = ctx.loadManagementReport();
  finishSecond({...report, title: '薪資新資料', rows: [['最新', 10]]});
  await second;
  finishFirst(report);
  await first;
  assert.ok(controls.get('reportRows').innerHTML.includes('最新'));
  assert.ok(controls.get('reportCount').textContent.includes('薪資新資料'));
});
