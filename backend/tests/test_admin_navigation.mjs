import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const html = readFileSync(new URL('../app/templates/index.html', import.meta.url), 'utf8');
const navigation = html.slice(html.indexOf('    const legacyDocumentTasks'), html.indexOf('    function api('));
const keptTitles = [
  '系統總覽', '今日各工地工作日誌', '行事曆', '合約與證照管理', '堆高機出勤統計表', '堆高機出租管理',
  '堆高機車隊管理', '工地管理', '管理操作紀錄', '堆高機點檢記錄', '點檢與保養通知紀錄', '工作日誌匯入計價工時',
  '整月批次匯入（工作日誌自動彙整）', '公司 Excel 文件庫', '收支與通訊錄資料匯入', '當日簽單紀錄', 'Excel 線上資料處理',
  '月度與年度管理對照', 'LINE 正式接線', '建立每日工作安排', '臨時通知', '工作安排清單', '休假／排休設定與審核',
  '會議記錄', '主管異常審核', '自動改派建議', '員工名冊與綁定狀態', 'LINE 模擬測試', '考勤與回報查詢',
  '使用者管理', '工作相片上傳記錄', '登入稽核日誌', '工作內容與機具管理', '今日總覽',
];

function attr(source, name) {
  return source.match(new RegExp(`${name}="([^"]*)"`))?.[1] || '';
}

const navHtml = html.slice(html.indexOf('<nav class="workspace-nav"'), html.indexOf('</nav>'));

function loadPanels() {
  const panels = [];
  for (const match of html.matchAll(/<section\b([^>]*)>/g)) {
    const source = match[1];
    if (!source.includes('data-workspace=')) continue;
    const title = html.slice(match.index, match.index + 900).match(/<h2(?:\s[^>]*)?>([^<]*)<\/h2>/)?.[1] || '';
    panels.push({
      id: attr(source, 'id'),
      dataset: {
        workspace: attr(source, 'data-workspace'),
        panel: attr(source, 'data-panel'),
        navOrder: attr(source, 'data-nav-order') || '0',
        legacy: attr(source, 'data-legacy'),
        navAliases: attr(source, 'data-nav-aliases'),
      },
      title,
      hidden: false,
      classList: {toggle() {}},
      querySelector(selector) {
        return selector === 'h2' ? {textContent: title} : null;
      },
    });
  }
  return panels;
}

function fixture() {
  const panels = loadPanels();
  const tabs = [...navHtml.matchAll(/data-workspace-tab="([^"]+)"[^>]*>([^<]+)/g)].map((match) => ({
    dataset: {workspaceTab: match[1]},
    textContent: match[2],
    classList: {toggle() {}},
    setAttribute() {},
  }));
  const controls = new Map();
  const state = {workspace: 'today', workspaceViews: {}};
  const document = {
    getElementById(id) {
      if (id === 'reassignmentRows') return {closest: () => panels.find((panel) => panel.dataset.panel === 'reassignment')};
      if (!controls.has(id)) controls.set(id, {value: '', hidden: false, textContent: '', innerHTML: ''});
      return controls.get(id);
    },
    querySelectorAll(selector) {
      if (selector === '[data-workspace]' || selector === '[data-panel]' || selector === '[data-legacy]') return panels;
      if (selector === '[data-workspace-tab]') return tabs;
      const workspace = selector.match(/data-workspace="([^"]+)"/)?.[1];
      if (workspace && selector.startsWith('[data-workspace=')) return panels.filter((panel) => panel.dataset.workspace === workspace);
      const panelKey = selector.match(/data-panel="([^"]+)"/)?.[1];
      if (panelKey) return panels.filter((panel) => panel.dataset.panel === panelKey);
      const tab = selector.match(/data-workspace-tab="([^"]+)"/)?.[1];
      if (tab) return tabs.filter((item) => item.dataset.workspaceTab === tab);
      return [];
    },
    querySelector(selector) {
      return this.querySelectorAll(selector)[0] || null;
    },
  };
  const context = {
    state, document, window: {scrollTo() {}}, escapeHtml: String, escapeAttr: String,
    managementReports: {loadingSources: false, sourcesLoaded: true},
  };
  runInNewContext(navigation, context);
  return {context, panels, state, controls, tabs};
}

function visibleKey(panels) {
  const visible = panels.filter((panel) => !panel.hidden);
  assert.equal(visible.length, 1);
  return visible[0].dataset.panel;
}

test('every existing function stays reachable in one of seven groups', () => {
  const panels = loadPanels();
  assert.equal(panels.length, 34);
  assert.equal(new Set(panels.map((panel) => panel.dataset.panel)).size, 34);
  const groups = [...navHtml.matchAll(/data-workspace-tab="([^"]+)"[^>]*>([^<]+)/g)].map((match) => match[2]);
  assert.deepEqual(groups, ['今日總覽', '人員與考勤', '派工與工地', '堆高機', '日誌與簽單', '計價與收支', '文件與設定']);
  for (const title of keptTitles) assert.ok(panels.some((panel) => panel.title === title), title);
  assert.ok(html.includes('id="navSearch"'));
  assert.ok(html.includes('openDocumentTask(0)'));
  assert.ok(html.includes('openDocumentTask(2)'));
  assert.ok(html.includes('openDocumentTask(4)'));
});

test('today opens the dashboard, and journal and library keep their homes', () => {
  const {context, panels, controls} = fixture();
  context.setWorkspace('today');
  assert.equal(visibleKey(panels), 'today-board');
  assert.equal(controls.get('workspaceTools').hidden, false);
  context.setWorkspace('journals');
  assert.equal(visibleKey(panels), 'journal');
  context.setWorkspace('files');
  assert.equal(controls.get('workspaceView').value, '0');
  assert.equal(visibleKey(panels), 'excel-library');
});

test('a single-item group hides the operation menu', () => {
  const {context, panels, controls} = fixture();
  panels.push({
    dataset: {workspace: 'solo', panel: 'solo', navOrder: '0', legacy: '', navAliases: ''},
    title: '單獨', hidden: true, classList: {toggle() {}}, querySelector() { return {textContent: '單獨'}; },
  });
  context.setWorkspace('solo');
  assert.equal(controls.get('workspaceTools').hidden, true);
  assert.equal(visibleKey(panels), 'solo');
});

test('selection survives switching, including the old documents name', () => {
  const {context, panels, controls} = fixture();
  context.openDocumentTask(0);
  assert.equal(visibleKey(panels), 'pricing-hours');
  context.setWorkspace('fleet');
  context.setWorkspace('documents');
  assert.equal(controls.get('workspaceMain').value, 'pricing');
  assert.equal(visibleKey(panels), 'pricing-hours');
});

test('old document indexes still open the same functions', () => {
  const {context, panels} = fixture();
  const expected = {
    0: 'pricing-hours', 1: 'pricing-month', 2: 'excel-library', 3: 'finance-import',
    4: 'sign-slips', 5: 'excel-editor', 6: 'management-reports',
  };
  for (const [view, key] of Object.entries(expected)) {
    context.openDocumentTask(Number(view));
    assert.equal(visibleKey(panels), key);
  }
  context.setWorkspace('pricing');
  context.setWorkspaceView(999);
  assert.equal(visibleKey(panels), 'management-reports');
});

test('reassignment results reveal their own panel', () => {
  const {context, panels, controls} = fixture();
  context.openPanelFor('reassignmentRows');
  assert.equal(controls.get('workspaceMain').value, 'dispatch');
  assert.equal(visibleKey(panels), 'reassignment');
});

test('search jumps to a function by name or alias', () => {
  const {context, panels, controls} = fixture();
  assert.equal(context.searchNav('').length, 0);
  assert.equal(context.searchNav('簽單')[0].key, 'sign-slips');
  assert.equal(context.searchNav('檢查簽單與計價金額')[0].key, 'pricing-month');
  assert.equal(context.searchNav('檢查簽單與計價金額')[0].group, '計價與收支');
  assert.ok(context.searchNav('請假').some((item) => item.key === 'leave'));
  assert.ok(context.searchNav('點檢').some((item) => item.key === 'inspections'));
  assert.equal(context.searchNav('登入名稱')[0].key, 'users');
  assert.equal(context.searchNav('修改派工設定')[0].key, 'assignment-list');
  context.renderNavSearch('簽單');
  assert.equal(controls.get('navSearchResults').hidden, false);
  assert.match(controls.get('navSearchResults').innerHTML, /當日簽單紀錄/);
  context.openNamedPanel(context.searchNav('請假')[0].key);
  assert.equal(visibleKey(panels), 'leave');
});

test('old hashes and panel links still open the same screen', () => {
  const {context, panels} = fixture();
  context.applyDeepLink('#documents/4');
  assert.equal(visibleKey(panels), 'sign-slips');
  context.applyDeepLink('#documents');
  assert.equal(visibleKey(panels), 'excel-library');
  context.applyDeepLink('#operations/6');
  assert.equal(visibleKey(panels), 'reassignment');
  context.applyDeepLink('#overview/1');
  assert.equal(visibleKey(panels), 'journal');
  context.applyDeepLink('#overview');
  assert.equal(visibleKey(panels), 'today-board');
  context.applyDeepLink('#panel-attendance');
  assert.equal(visibleKey(panels), 'attendance');
});

test('operation feedback remains visible outside the overview', () => {
  const {context, controls} = fixture();
  const feedback = html.slice(html.indexOf('    function setConsole('), html.indexOf('    function escapeHtml('));
  runInNewContext(feedback, context);
  context.setConsole('Google Drive 讀取失敗');
  assert.equal(controls.get('actionResult').hidden, false);
  assert.equal(controls.get('actionResultTitle').textContent, 'Google Drive 讀取失敗');
  context.setWorkspace('documents');
  assert.equal(controls.get('actionResult').hidden, true);
});
