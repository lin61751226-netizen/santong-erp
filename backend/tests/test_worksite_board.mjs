import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {test} from 'node:test';
import vm from 'node:vm';

const base = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'app');
const code = readFileSync(path.join(base, 'static', 'worksite_board.js'), 'utf8');
const template = readFileSync(path.join(base, 'templates', 'index.html'), 'utf8');

function fixture() {
  const elements = Object.fromEntries(['worksiteBoardDate', 'worksiteBoardSite', 'worksiteBoardStatus',
    'worksiteBoardCards'].map(id => [id, {value: '', textContent: '', innerHTML: '', replaceChildren() { this.innerHTML = ''; }}]));
  const context = {
    document: {getElementById: id => elements[id]},
    state: {actor: {role: 'admin'}, worksites: [{id: 1, name: '齊裕53', code: '53'}]},
    escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;'),
    URLSearchParams,
  };
  vm.createContext(context);
  vm.runInContext(code, context);
  return {context, elements};
}

test('workbench renders seven readable sections and escapes record text', () => {
  const {context, elements} = fixture();
  const data = {
    date: '2026-10-09', site: {name: '齊裕53'},
    assignments: {missing: false, rows: [{work_item: '<script>', members: ['司機'], completed: false}]},
    attendance: {rows: [], missing: true, missing_names: ['待查員工']},
    inspections: {rows: [], missing: false, missing_machines: [], abnormal: 0},
    photos: {count: 0, recent: []}, journal: {group_text_count: 1, saved_hours: null},
    sign_slips: {rows: [], missing: true}, pricing: {state: '待核對', import_count: 0},
  };
  context.renderWorksiteBoard(data);
  const html = elements.worksiteBoardCards.innerHTML;
  assert.equal((html.match(/class="worksite-board-card /g) || []).length, 7);
  assert.ok(html.includes('&lt;script>'));
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('前往處理'));
  assert.ok(html.includes('此工地未見上班打卡'));
  assert.ok(html.includes('尚無此施工日的工作照片'));
  assert.match(template, /id="panel-worksite-board"/);
  assert.match(template, /worksite_board\.js\?v=20261114/);
});

test('date and worksite must both be selected before API read', async () => {
  const {context, elements} = fixture();
  let calls = 0;
  context.api = async () => { calls++; return {}; };
  await context.loadWorksiteBoard();
  assert.equal(calls, 0);
  assert.equal(elements.worksiteBoardStatus.textContent, '請先選擇日期與工地。');
});

test('photo action carries selected construction date and site to the existing uploader', async () => {
  const {context, elements} = fixture();
  for (const id of ['photoFilterDateFrom', 'photoFilterDateTo', 'photoImportDate', 'photoImportSite']) {
    elements[id] = {value: ''};
  }
  elements.worksiteBoardDate.value = '2026-10-09';
  elements.worksiteBoardSite.value = '1';
  let opened = '', loaded = 0;
  context.openNamedPanel = value => { opened = value; };
  context.loadPhotoUploads = async () => { loaded++; };
  await context.openWorksiteBoardItem('photos');
  assert.equal(opened, 'photos');
  assert.equal(elements.photoImportDate.value, '2026-10-09');
  assert.equal(elements.photoImportSite.value, '1');
  assert.equal(elements.photoFilterDateFrom.value, '2026-10-09');
  assert.equal(loaded, 1);
});
