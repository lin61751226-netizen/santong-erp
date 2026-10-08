function leaveBackupMessage(status) {
  return status === 'saved' ? 'Google Drive 資料庫備份成功。' :
    '已存入目前資料庫，但雲端備份未完成；請至文件與計價 → Excel 線上資料處理重試雲端備份，成功前不要重新部署。';
}

function renderLeaveSettingsOptions() {
  const select = document.getElementById('leaveEmployee');
  const previous = select.value;
  select.innerHTML = '<option value="">請選擇員工</option>' + state.employees.map(employee =>
    `<option value="${escapeAttr(employee.employee_code)}">${escapeHtml(employee.name)}｜${escapeHtml(employee.employee_code)}</option>`).join('');
  if (state.employees.some(employee => employee.employee_code === previous)) select.value = previous;
}

function openLeaveSettings(fromReport = false) {
  const month = fromReport ? document.getElementById('reportMonth').value : '';
  const initial = /^20\d{2}-(0[1-9]|1[0-2])$/.test(month) ? `${month}-01` : todayString();
  // Keep the unsaved form intact when navigating from a report or another workspace.
  const start = document.getElementById('leaveStart');
  const end = document.getElementById('leaveEnd');
  if (!start.value) start.value = initial;
  if (!end.value) end.value = start.value;
  openPanelFor('leaveSettingsForm');
}

async function saveLeaveSettings(event) {
  event.preventDefault();
  const button = document.getElementById('leaveSaveButton');
  if (button.disabled) return;
  const feedback = document.getElementById('leaveSettingsFeedback');
  const payload = {
    employee_code: document.getElementById('leaveEmployee').value,
    leave_type: document.getElementById('leaveType').value,
    start_date: document.getElementById('leaveStart').value,
    end_date: document.getElementById('leaveEnd').value,
    reason: document.getElementById('leaveReason').value.trim(),
  };
  if (!payload.employee_code || !payload.start_date || !payload.end_date || !payload.reason) {
    feedback.textContent = '請填寫員工、起訖日期及休假原因。';
    return;
  }
  if (payload.end_date < payload.start_date) {
    feedback.textContent = '結束日期不得早於開始日期。';
    return;
  }
  button.disabled = true;
  feedback.textContent = '正在保存休假／排休並備份…';
  try {
    const result = await api('/api/leave-requests', {method: 'POST', body: JSON.stringify(payload)});
    feedback.textContent = `休假／排休已保存（編號 ${result.leave_request_id}），目前待核准；請在下方按「核准」。 ${leaveBackupMessage(result.backup_status)}${result.policy_note ? ` 提醒：${result.policy_note}` : ''}`;
    document.getElementById('leaveReason').value = '';
    document.getElementById('leaveStatusFilter').value = 'pending';
    try { await loadLeaves(); await loadDashboard(); }
    catch (error) { feedback.textContent += ` 清單更新失敗：${error.message}；資料已存入，勿重複送出。`; }
  } catch (error) { feedback.textContent = error.message; }
  finally { button.disabled = false; }
}
