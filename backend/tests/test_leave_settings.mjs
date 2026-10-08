import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = readFileSync(new URL('../app/static/leave_settings.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../app/templates/index.html', import.meta.url), 'utf8');
function fixture(api = async () => ({leave_request_id: 9, backup_status: 'saved'})) {
  const controls = new Map();
  const document = {getElementById(id) {
    if (!controls.has(id)) controls.set(id, {value: '', innerHTML: '', textContent: '', disabled: false});
    return controls.get(id);
  }};
  const opened = [], loads = [];
  const escapeHtml = value => String(value).replaceAll('<', '&lt;').replaceAll('"', '&quot;');
  const ctx = {document, api, state: {employees: [{employee_code: 'EMP001', name: '<測試>'}]},
    escapeHtml, escapeAttr: escapeHtml, todayString: () => '2026-10-08',
    openPanelFor: id => opened.push(id), loadLeaves: async () => loads.push('leaves'), loadDashboard: async () => loads.push('dashboard')};
  runInNewContext(source, ctx);
  for (const [id, value] of Object.entries({leaveEmployee: 'EMP001', leaveType: '排休', leaveStart: '2026-11-02', leaveEnd: '2026-11-02', leaveReason: ' 排休 '})) document.getElementById(id).value = value;
  return {ctx, controls, document, opened, loads};
}

test('settings are discoverable from people and monthly roster without new panel indices', () => {
  assert.ok(html.includes('onclick="openLeaveSettings()"'));
  assert.ok(html.includes('id="reportRosterActions"'));
  assert.ok(html.includes('onclick="openLeaveSettings(true)"'));
  assert.ok(html.includes('<h2>休假／排休設定與審核</h2>'));
  assert.equal((html.match(/id="leaveSettingsForm"/g) || []).length, 1);
});

test('report month initializes dates but does not overwrite an unsaved form', () => {
  const {ctx, controls, document, opened} = fixture();
  document.getElementById('reportMonth').value = '2026-12';
  ctx.openLeaveSettings(true);
  assert.equal(controls.get('leaveStart').value, '2026-11-02');
  controls.get('leaveStart').value = '';
  controls.get('leaveEnd').value = '';
  ctx.openLeaveSettings(true);
  assert.equal(controls.get('leaveStart').value, '2026-12-01');
  assert.equal(controls.get('leaveEnd').value, '2026-12-01');
  assert.deepEqual(opened, ['leaveSettingsForm', 'leaveSettingsForm']);
});

test('employee options are escaped and retain selected person', () => {
  const {ctx, controls} = fixture();
  ctx.renderLeaveSettingsOptions();
  assert.ok(controls.get('leaveEmployee').innerHTML.includes('&lt;測試>'));
  assert.equal(controls.get('leaveEmployee').value, 'EMP001');
});

test('save submits explicit employee and dates, stays pending and refreshes list', async () => {
  let request;
  const {ctx, controls, loads} = fixture(async (url, options) => {request = {url, body: JSON.parse(options.body)}; return {leave_request_id: 9, backup_status: 'saved'};});
  await ctx.saveLeaveSettings({preventDefault() {}});
  assert.equal(request.url, '/api/leave-requests');
  assert.equal(request.body.employee_code, 'EMP001');
  assert.equal(request.body.reason, '排休');
  assert.equal(request.body.status, undefined);
  assert.deepEqual(loads, ['leaves', 'dashboard']);
  assert.equal(controls.get('leaveStatusFilter').value, 'pending');
  assert.ok(controls.get('leaveSettingsFeedback').textContent.includes('待核准'));
  assert.ok(controls.get('leaveSettingsFeedback').textContent.includes('備份成功'));
});

test('backup failure preserves honest saved-versus-backed-up distinction', async () => {
  const {ctx, controls} = fixture(async () => ({leave_request_id: 9, backup_status: 'failed'}));
  await ctx.saveLeaveSettings({preventDefault() {}});
  assert.ok(controls.get('leaveSettingsFeedback').textContent.includes('雲端備份未完成'));
  assert.ok(!controls.get('leaveSettingsFeedback').textContent.includes('備份成功'));
});

test('save errors preserve inputs and double clicks do not post twice', async () => {
  let calls = 0, finish;
  const {ctx, controls} = fixture(() => {calls++; return new Promise((resolve, reject) => {finish = reject;});});
  const saving = ctx.saveLeaveSettings({preventDefault() {}});
  await ctx.saveLeaveSettings({preventDefault() {}});
  assert.equal(calls, 1);
  finish(new Error('同時段已有申請'));
  await saving;
  assert.equal(controls.get('leaveReason').value, ' 排休 ');
  assert.equal(controls.get('leaveSaveButton').disabled, false);
  assert.equal(controls.get('leaveSettingsFeedback').textContent, '同時段已有申請');
});
