let assignmentEditingId = null;
let assignmentEditingVersion = null;
let assignmentBaseline = null;
let assignmentOpening = false;
let assignmentDeleting = false;

function assignmentPayload() {
  const value = id => document.getElementById(id).value;
  return {
    work_date: value('workDate'), site_id: Number(value('siteId')), work_item: value('workItem').trim(),
    supervisor_code: value('supervisorCode') || null,
    employee_codes: [...document.getElementById('employeeCodes').selectedOptions].map(option => option.value).sort(),
    start_time: value('startTime') || null, end_time: value('endTime') || null,
    vehicle: value('assignmentVehicle').trim() || null, equipment: value('equipment').trim() || null,
    notes: value('assignmentNotes').trim() || null,
  };
}

function confirmAssignmentDiscard() {
  const payload = assignmentPayload();
  const dirty = assignmentBaseline ? JSON.stringify(payload) !== assignmentBaseline :
    Boolean(payload.work_item || payload.notes || payload.vehicle || payload.equipment || payload.employee_codes.length);
  return !dirty || confirm('目前派工設定尚未保存，確定放棄修改嗎？');
}

function rememberAssignmentSelections() {
  return Object.fromEntries(['siteId', 'supervisorCode', 'employeeCodes'].map(id => [id,
    [...document.getElementById(id).selectedOptions].map(option => ({value: option.value, label: option.textContent}))]));
}

function ensureAssignmentOption(select, value, label) {
  if (value && ![...select.options].some(option => option.value === String(value))) {
    select.insertAdjacentHTML('beforeend', `<option value="${escapeAttr(value)}">${escapeHtml(label || value)}</option>`);
  }
}

function restoreAssignmentSelections(selections) {
  for (const [id, selected] of Object.entries(selections)) {
    if (!selected.length) continue;
    const control = document.getElementById(id);
    selected.forEach(option => ensureAssignmentOption(control, option.value, option.label));
    for (const option of control.options) option.selected = selected.some(item => item.value === option.value);
  }
}

function fillAssignmentEditor(row) {
  const fields = {workDate: row.work_date, siteId: row.site_id, workItem: row.work_item,
    supervisorCode: row.supervisor_code, startTime: row.start_time?.slice(0, 5),
    endTime: row.end_time?.slice(0, 5), assignmentVehicle: row.vehicle,
    equipment: row.equipment, assignmentNotes: row.notes};
  ensureAssignmentOption(document.getElementById('siteId'), row.site_id, row.site_name);
  ensureAssignmentOption(document.getElementById('supervisorCode'), row.supervisor_code, row.supervisor_name);
  (row.member_codes || []).forEach((code, index) => ensureAssignmentOption(document.getElementById('employeeCodes'), code,
    `${code}｜${row.members?.[index] || code}`));
  for (const [id, value] of Object.entries(fields)) document.getElementById(id).value = value == null ? '' : String(value);
  for (const option of document.getElementById('employeeCodes').options) option.selected = (row.member_codes || []).includes(option.value);
  assignmentEditingId = row.id;
  assignmentEditingVersion = row.version;
  assignmentBaseline = JSON.stringify(assignmentPayload());
  document.getElementById('assignmentHeading').textContent = `修改每日工作安排（編號 ${row.id}）`;
  document.getElementById('assignmentSaveButton').textContent = '保存派工修改';
  document.getElementById('assignmentCancelButton').hidden = false;
}

async function editAssignment(id) {
  if (assignmentDeleting || assignmentOpening || document.getElementById('assignmentSaveButton').disabled || !confirmAssignmentDiscard()) return;
  assignmentOpening = true;
  try {
    const row = await api(`/api/assignments/${id}`);
    if (row.status === 'cancelled') throw new Error('此派工已刪除（取消），請另建工作安排。');
    fillAssignmentEditor(row);
    document.getElementById('assignmentFeedback').textContent = '已載入原派工設定，修改後請按「保存派工修改」；尚未保存的輸入不會寫入資料庫。';
    openPanelFor('assignmentForm');
  } catch (error) { setConsole(error.message); }
  finally { assignmentOpening = false; }
}

function resetAssignmentEditor() {
  if (assignmentOpening || document.getElementById('assignmentSaveButton').disabled || !confirmAssignmentDiscard()) return;
  document.getElementById('assignmentForm').reset();
  document.getElementById('workDate').value = todayString();
  assignmentEditingId = null;
  assignmentEditingVersion = null;
  assignmentBaseline = null;
  document.getElementById('assignmentHeading').textContent = '建立每日工作安排';
  document.getElementById('assignmentSaveButton').textContent = '建立工作安排';
  document.getElementById('assignmentCancelButton').hidden = true;
  document.getElementById('assignmentFeedback').textContent = '已返回新增；原派工記錄沒有刪除。';
}

async function openAssignmentList() {
  openPanelFor('assignmentRows');
  try { await loadAssignments(); } catch (error) { setConsole(error.message); }
}

function renderAssignments(rows) {
  const statusLabels = {scheduled: '已排定', in_progress: '進行中', completed: '已完成', cancelled: '已取消'};
  document.getElementById('assignmentRows').innerHTML = rows.map(row => `<tr>
    <td>${escapeHtml(row.work_date)}<br><span class="muted">${escapeHtml(row.start_time?.slice(0, 5) || '-')}～${escapeHtml(row.end_time?.slice(0, 5) || '-')}</span></td>
    <td>${escapeHtml(row.site_name)}</td>
    <td>${escapeHtml(row.work_item)}<br><span class="muted">${escapeHtml(row.notes || '')}${row.vehicle || row.equipment ? `<br>車輛／機具：${escapeHtml([row.vehicle, row.equipment].filter(Boolean).join('、'))}` : ''}</span></td>
    <td>${escapeHtml(row.supervisor_name)}</td><td>${escapeHtml((row.members || []).join('、'))}</td>
    <td><span class="pill">${escapeHtml(statusLabels[row.status] || row.status)}</span></td>
    <td>${row.status === 'cancelled' ? '已刪除・歷史保留' : `<button type="button" class="button-secondary button-compact" onclick="editAssignment(${Number(row.id)})">修改</button> <button type="button" class="button-secondary button-compact" onclick="deleteAssignment(${Number(row.id)}, '${escapeAttr(row.version)}')">刪除</button>`}</td>
  </tr>`).join('') || '<tr><td colspan="7">此日期尚無工作安排；可清空日期查詢全部。</td></tr>';
}

async function deleteAssignment(id, version) {
  if (assignmentDeleting || assignmentOpening || document.getElementById('assignmentSaveButton').disabled) return;
  if (!confirmAssignmentDiscard() || !confirm(`確定刪除派工 ${id} 並保存？\n此派工將取消，人員不再收到此筆每日行程；打卡、回報、照片與修改紀錄仍保留。`)) return;
  assignmentDeleting = true;
  try {
    const result = await api(`/api/assignments/${id}/cancel`, {method: 'POST', body: JSON.stringify({version})});
    if (assignmentEditingId === id) {
      assignmentBaseline = JSON.stringify(assignmentPayload());
      resetAssignmentEditor();
    }
    const message = `${result.message}。${leaveBackupMessage(result.backup_status)}`;
    document.getElementById('assignmentListFeedback').textContent = message;
    try { await Promise.all([loadDashboard(), loadAssignments(), loadCalendar(), loadAttendance(), loadAttendanceExceptions(), loadAuditLogs()]); }
    catch (error) { document.getElementById('assignmentListFeedback').textContent += ` 清單更新失敗：${error.message}，請重新查詢。`; }
  } catch (error) { document.getElementById('assignmentListFeedback').textContent = error.message; }
  finally { assignmentDeleting = false; }
}

async function saveAssignmentSettings(event) {
  event.preventDefault();
  const button = document.getElementById('assignmentSaveButton');
  if (button.disabled || assignmentOpening || assignmentDeleting) return;
  const feedback = document.getElementById('assignmentFeedback');
  const payload = assignmentPayload();
  if (!payload.work_date || !payload.site_id || !payload.work_item || !payload.employee_codes.length) {
    feedback.textContent = '請填寫日期、工地、工作內容，並選擇至少一位執行人員。'; return;
  }
  const editingId = assignmentEditingId;
  if (editingId) payload.version = assignmentEditingVersion;
  button.disabled = true;
  feedback.textContent = '正在保存派工並備份…';
  try {
    const result = await api(editingId ? `/api/assignments/${editingId}` : '/api/assignments', {
      method: editingId ? 'PATCH' : 'POST', body: JSON.stringify(payload),
    });
    // Stay on the saved record so a second click cannot accidentally create another assignment.
    fillAssignmentEditor(result.assignment);
    feedback.textContent = `${result.message}（編號 ${result.assignment_id}）。 ${leaveBackupMessage(result.backup_status)} 未自動重發 LINE 通知；需要通知請至「臨時通知」。`;
    document.getElementById('assignmentFilterDate').value = result.assignment.work_date;
    try { await Promise.all([loadDashboard(), loadAssignments(), loadCalendar(), loadAttendance(), loadAttendanceExceptions(), loadAuditLogs()]); }
    catch (error) { feedback.textContent += ` 清單更新失敗：${error.message}；資料已保存，勿重複新增。`; }
  } catch (error) { feedback.textContent = error.message; }
  finally { button.disabled = false; }
}
