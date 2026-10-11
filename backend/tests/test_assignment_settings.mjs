import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = readFileSync(new URL('../app/static/assignment_settings.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../app/templates/index.html', import.meta.url), 'utf8');
const row = {id: 7, version: 'a'.repeat(64), work_date: '2026-11-02', site_id: 1, site_name: '齊裕53',
  work_item: '移料', supervisor_code: 'ADMIN001', supervisor_name: '主管', member_codes: ['EMP001', 'EMP002'],
  members: ['甲', '乙'], start_time: '08:00:00', end_time: '17:00:00', vehicle: '3T×2', equipment: '堆高機', notes: '原備註'};

test('cancel confirms before writing, sends version and reports backup failure', async () => {
  const calls = [];
  const {ctx, controls} = fixture(async (url, options) => {
    calls.push({url, options}); return {message: '已取消', backup_status: 'failed'};
  });
  ctx.confirm = () => false;
  await ctx.deleteAssignment(7, row.version);
  assert.equal(calls.length, 0);
  ctx.confirm = () => true;
  await ctx.deleteAssignment(7, row.version);
  assert.equal(calls[0].url, '/api/assignments/7/cancel');
  assert.equal(JSON.parse(calls[0].options.body).version, row.version);
  assert.ok(controls.get('assignmentListFeedback').textContent.includes('雲端備份未完成'));
  ctx.renderAssignments([{...row, status: 'cancelled'}]);
  assert.ok(!controls.get('assignmentRows').innerHTML.includes('onclick='));
});

function fixture(api = async () => row) {
  const controls = new Map();
  const document = {getElementById(id) {
    if (!controls.has(id)) {
      const control = {value: '', innerHTML: '', textContent: '', disabled: false, hidden: false, options: [],
        insertAdjacentHTML(_where, html) {
          this.options.push({value: html.match(/value="([^"]+)"/)[1], textContent: html, selected: false});
        }, reset() {for (const input of controls.values()) input.value = '';}};
      Object.defineProperty(control, 'selectedOptions', {get() {return this.options.filter(item => item.selected);}});
      controls.set(id, control);
    }
    return controls.get(id);
  }};
  const opened = [], loads = [], warnings = [];
  const escapeHtml = value => String(value ?? '').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
  const ctx = {document, api, escapeHtml, escapeAttr: escapeHtml,
    todayString: () => '2026-10-11', confirm: message => {warnings.push(message); return true;},
    openPanelFor: id => opened.push(id), setConsole: message => warnings.push(message),
    leaveBackupMessage: status => status === 'saved' ? '備份成功' : '雲端備份未完成'};
  for (const name of ['loadDashboard', 'loadAssignments', 'loadCalendar', 'loadAttendance', 'loadAttendanceExceptions', 'loadAuditLogs'])
    ctx[name] = async () => loads.push(name);
  runInNewContext(source, ctx);
  return {ctx, document, controls, opened, loads, warnings};
}

test('list and form expose modification without adding panels', () => {
  assert.ok(html.includes('id="assignmentVehicle"'));
  assert.ok(html.includes('onclick="openAssignmentList()"'));
  assert.ok(html.includes('saveAssignmentSettings'));
  assert.ok(!html.includes('function renderAssignments(rows)'));
  const {ctx, controls} = fixture();
  ctx.renderAssignments([{...row, status: 'scheduled', work_item: '<img src=x>', notes: '<script>'}]);
  assert.ok(controls.get('assignmentRows').innerHTML.includes('editAssignment(7)'));
  assert.ok(controls.get('assignmentRows').innerHTML.includes('&lt;img'));
  assert.ok(!controls.get('assignmentRows').innerHTML.includes('<script>'));
});

test('opening an existing record loads every field and multi-selected employees', async () => {
  const {ctx, controls, opened} = fixture();
  await ctx.editAssignment(7);
  assert.equal(controls.get('assignmentVehicle').value, '3T×2');
  assert.equal(controls.get('startTime').value, '08:00');
  assert.equal(controls.get('assignmentNotes').value, '原備註');
  assert.deepEqual([...controls.get('employeeCodes').selectedOptions].map(item => item.value), ['EMP001', 'EMP002']);
  assert.equal(controls.get('assignmentCancelButton').hidden, false);
  assert.deepEqual(opened, ['assignmentForm']);
});

test('save uses PATCH with version, preserves vehicle, and remains in edit mode', async () => {
  const requests = [];
  const {ctx, controls, loads} = fixture(async (url, options) => {
    requests.push({url, options});
    return {message: '工作安排已修改', assignment_id: 7, assignment: {...row, version: 'b'.repeat(64)}, backup_status: 'saved'};
  });
  ctx.fillAssignmentEditor(row);
  controls.get('workItem').value = ' 修改 ';
  await ctx.saveAssignmentSettings({preventDefault() {}});
  assert.equal(requests[0].url, '/api/assignments/7');
  assert.equal(requests[0].options.method, 'PATCH');
  const payload = JSON.parse(requests[0].options.body);
  assert.equal(payload.work_item, '修改');
  assert.equal(payload.version, row.version);
  assert.equal(payload.vehicle, '3T×2');
  assert.equal(controls.get('assignmentSaveButton').textContent, '保存派工修改');
  assert.ok(controls.get('assignmentFeedback').textContent.includes('備份成功'));
  assert.ok(loads.includes('loadCalendar'));
  assert.ok(loads.includes('loadAuditLogs'));
});

test('validation, server conflict, and backup failure never clear or misreport inputs', async () => {
  const {ctx, controls} = fixture(async () => {throw new Error('派工已被修改');});
  ctx.fillAssignmentEditor(row);
  controls.get('workItem').value = '保留輸入';
  await ctx.saveAssignmentSettings({preventDefault() {}});
  assert.equal(controls.get('workItem').value, '保留輸入');
  assert.equal(controls.get('assignmentFeedback').textContent, '派工已被修改');
  assert.equal(controls.get('assignmentSaveButton').disabled, false);
  const failed = fixture(async () => ({message: '工作安排已修改', assignment_id: 7, assignment: row, backup_status: 'failed'}));
  failed.ctx.fillAssignmentEditor(row);
  await failed.ctx.saveAssignmentSettings({preventDefault() {}});
  assert.ok(failed.controls.get('assignmentFeedback').textContent.includes('雲端備份未完成'));
});

test('double click posts once and dirty editor requires confirmation to replace', async () => {
  let calls = 0, finish;
  const {ctx, controls} = fixture(() => {calls++; return new Promise(resolve => {finish = resolve;});});
  ctx.fillAssignmentEditor(row);
  const saving = ctx.saveAssignmentSettings({preventDefault() {}});
  await ctx.saveAssignmentSettings({preventDefault() {}});
  assert.equal(calls, 1);
  finish({message: '已存', assignment_id: 7, assignment: row, backup_status: 'saved'});
  await saving;
  controls.get('workItem').value = '未保存';
  ctx.confirm = () => false;
  ctx.resetAssignmentEditor();
  assert.equal(controls.get('workItem').value, '未保存');
  await ctx.editAssignment(8);
  assert.equal(calls, 1);
});

test('metadata refresh retains selected site, supervisor and people including inactive originals', () => {
  const {ctx, controls} = fixture();
  ctx.fillAssignmentEditor(row);
  controls.get('siteId').options[0].selected = true;
  controls.get('supervisorCode').options[0].selected = true;
  const selections = ctx.rememberAssignmentSelections();
  for (const id of ['siteId', 'supervisorCode', 'employeeCodes']) controls.get(id).options = [];
  ctx.restoreAssignmentSelections(selections);
  assert.equal(controls.get('siteId').selectedOptions[0].value, '1');
  assert.equal(controls.get('supervisorCode').selectedOptions[0].value, 'ADMIN001');
  assert.equal(controls.get('employeeCodes').selectedOptions.length, 2);
});

test('create stays on the saved record and refresh errors do not suggest repeating creation', async () => {
  let request;
  const {ctx, controls} = fixture(async (url, options) => {request = {url, options};
    return {message: '工作安排已建立', assignment_id: 7, assignment: row, backup_status: 'saved'};});
  ctx.fillAssignmentEditor(row);
  ctx.resetAssignmentEditor();
  ctx.fillAssignmentEditor(row);
  runInNewContext('assignmentEditingId = null; assignmentEditingVersion = null;', ctx);
  ctx.loadAssignments = async () => {throw new Error('網路中斷');};
  await ctx.saveAssignmentSettings({preventDefault() {}});
  assert.equal(request.url, '/api/assignments');
  assert.equal(request.options.method, 'POST');
  assert.ok(controls.get('assignmentFeedback').textContent.includes('資料已保存'));
  assert.equal(controls.get('assignmentSaveButton').textContent, '保存派工修改');
});
