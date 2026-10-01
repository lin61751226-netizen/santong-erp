import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const html = readFileSync(new URL('../app/templates/index.html', import.meta.url), 'utf8');
const navigation = html.slice(html.indexOf('    const workspaceDefaults'), html.indexOf('    function api('));

function fixture() {
  const groups = {overview: 2, documents: 5, operations: 7, fleet: 6, people: 2, calendar: 1};
  const panels = Object.entries(groups).flatMap(([workspace, count]) =>
    Array.from({length: count}, (_, index) => ({
      dataset: {workspace}, hidden: true, classList: {toggle() {}},
      querySelector() { return {textContent: `${workspace} ${index}`}; },
      draft: index === 0 ? '22' : '',
    })));
  const controls = new Map();
  controls.set('reassignmentRows', {closest() {return panels.filter(panel => panel.dataset.workspace === 'operations')[6];}});
  const state = {workspace: 'overview', workspaceViews: {}};
  const document = {
    getElementById(id) {
      if (!controls.has(id)) controls.set(id, {value: '', hidden: false});
      return controls.get(id);
    },
    querySelectorAll(selector) {
      if (selector === '[data-workspace]') return panels;
      if (selector === '[data-workspace-tab]') return Object.keys(groups).map(workspace => ({
        dataset: {workspaceTab: workspace}, textContent: workspace,
        classList: {toggle() {}}, setAttribute() {},
      }));
      const workspace = selector.match(/data-workspace="([^"]+)"/)?.[1];
      return panels.filter(panel => panel.dataset.workspace === workspace);
    },
  };
  const context = {state, document, window: {scrollTo() {}}, escapeHtml: String, escapeAttr: String};
  runInNewContext(navigation, context);
  return {context, panels, state, controls};
}

test('one panel is visible, with journal and library as defaults', () => {
  const {context, panels, controls} = fixture();
  context.setWorkspace('documents');
  assert.equal(panels.filter(panel => !panel.hidden).length, 1);
  assert.equal(controls.get('workspaceView').value, '2');
  context.setWorkspace('overview');
  assert.equal(controls.get('workspaceView').value, '1');
  context.setWorkspace('calendar');
  assert.equal(controls.get('workspaceTools').hidden, true);
});

test('selection and unsaved fields survive switching workspaces', () => {
  const {context, panels, controls} = fixture();
  context.openDocumentTask(0);
  const form = panels.find(panel => !panel.hidden);
  context.setWorkspace('fleet');
  context.setWorkspace('documents');
  assert.equal(controls.get('workspaceView').value, '0');
  assert.equal(form.hidden, false);
  assert.equal(form.draft, '22');
});

test('journal links select pricing or saved sign slips, not library', () => {
  const {context, controls} = fixture();
  context.openDocumentTask(4);
  assert.equal(controls.get('workspaceView').value, '4');
  context.openDocumentTask(0);
  assert.equal(controls.get('workspaceView').value, '0');
  context.setWorkspaceView(999);
  assert.equal(controls.get('workspaceView').value, '4');
});

test('reassignment results reveal their own panel', () => {
  const {context, controls, panels} = fixture();
  context.openPanelFor('reassignmentRows');
  assert.equal(controls.get('workspaceMain').value, 'operations');
  assert.equal(controls.get('workspaceView').value, '6');
  assert.equal(panels.filter(panel => !panel.hidden).length, 1);
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
