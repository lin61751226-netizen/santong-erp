/* Source snapshots are immutable; switching reports never writes business data. */
const managementReports = {sources: [], preview: null, previewDocument: null, offset: 0, request: 0, loadingSources: false, sourcesLoaded: false};

function reportParams() {
  const params = new URLSearchParams({month: document.getElementById('reportMonth').value,
    kind: document.getElementById('reportKind').value});
  const version = document.getElementById('reportVersion').value;
  if (version) params.set('snapshot_id', version);
  return params;
}

function openManagementReports(documentId) {
  managementReports.preferredSource = String(documentId);
  managementReports.sourcesLoaded = false;
  openPanelFor('reportSource');
}

async function loadReportSources() {
  const feedback = document.getElementById('reportFeedback');
  managementReports.loadingSources = true;
  try {
    const sources = await api('/api/management-reports/sources');
    const select = document.getElementById('reportSource');
    const old = managementReports.preferredSource || select.value;
    managementReports.preferredSource = null;
    managementReports.sources = sources;
    managementReports.sourcesLoaded = true;
    select.innerHTML = '<option value="">只看系統資料</option>' + sources.map(item =>
      `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join('');
    if (sources.some(item => String(item.id) === old)) select.value = old;
    else if (sources.length) select.value = String(sources[0].id);
    changeReportSource();
    feedback.textContent = sources.length ? '請先預覽來源檔，或選擇已存版本查詢。' : '尚無全年管理文件，請先上傳原始Excel。系統資料仍可查詢。';
  } catch (error) { feedback.textContent = error.message; }
  finally { managementReports.loadingSources = false; }
}

function changeReportSource() {
  managementReports.preview = null;
  managementReports.previewDocument = null;
  document.getElementById('reportSaveButton').disabled = true;
  document.getElementById('reportImportPreview').hidden = true;
  const source = managementReports.sources.find(item => String(item.id) === document.getElementById('reportSource').value);
  const select = document.getElementById('reportVersion');
  select.innerHTML = source?.versions.length ? source.versions.map(item =>
    `<option value="${item.id}">版本${item.id}｜${escapeHtml(formatTW(item.created_at))}</option>`).join('') : '<option value="">尚未保存Excel版本（只看系統資料）</option>';
  document.getElementById('reportPreviewButton').disabled = !source;
  loadManagementReport();
}

async function previewManagementWorkbook() {
  const sourceId = document.getElementById('reportSource').value;
  if (!sourceId) return;
  const button = document.getElementById('reportPreviewButton');
  const feedback = document.getElementById('reportFeedback');
  button.disabled = true;
  document.getElementById('reportSaveButton').disabled = true;
  feedback.textContent = '正在讀取原表薪資、排休與行事曆；尚未寫入資料。';
  try {
    const data = await api(`/api/management-reports/preview?document_id=${sourceId}`);
    if (sourceId !== document.getElementById('reportSource').value) return;
    managementReports.preview = data;
    managementReports.previewDocument = sourceId;
    const labels = {finance: '收支', payroll: '薪資', roster: '排休', calendar: '行事曆', contacts: '通訊錄'};
    document.getElementById('reportImportCounts').textContent = Object.entries(data.counts).map(([key, count]) => `${labels[key]} ${count}筆`).join('；');
    document.getElementById('reportEmployeeMapping').innerHTML = data.matches.map((item, index) =>
      `<div><label for="reportMap${index}">Excel：${escapeHtml(item.name)} ${item.source_codes.length ? `（編號${escapeHtml(item.source_codes.join('、'))}）` : ''}</label><select id="reportMap${index}" data-report-name="${escapeAttr(item.name)}"><option value="">保留待對應，不更動員工</option>${data.employees.map(employee => `<option value="${escapeAttr(employee.code)}" ${employee.code === item.employee_code ? 'selected' : ''}>${escapeHtml(employee.code)} ${escapeHtml(employee.name)}</option>`).join('')}</select></div>`).join('');
    document.getElementById('reportImportWarnings').innerHTML = data.warnings.length ?
      `<strong>待確認 ${data.warnings.length}筆（不自動入帳）</strong><ul>${data.warnings.map(item => `<li>${escapeHtml(item.source || `${item.sheet}列${item.row}`)}：${escapeHtml(item.reason)}</li>`).join('')}</ul>` : '沒有辨識警告。';
    const panel = document.getElementById('reportImportPreview');
    panel.hidden = false;
    panel.open = true;
    document.getElementById('reportSaveButton').disabled = false;
    feedback.textContent = '預覽完成。請確認不同姓名與編號的員工對應，再保存；待對應人員也會保留原表。';
  } catch (error) { feedback.textContent = error.message; }
  finally { button.disabled = !document.getElementById('reportSource').value; }
}

async function saveManagementWorkbook() {
  if (!managementReports.preview || managementReports.previewDocument !== document.getElementById('reportSource').value) return;
  const button = document.getElementById('reportSaveButton');
  button.disabled = true;
  const mapping = Object.fromEntries([...document.querySelectorAll('[data-report-name]')].map(item => [item.dataset.reportName, item.value || null]));
  const feedback = document.getElementById('reportFeedback');
  try {
    const result = await api('/api/management-reports/import', {method: 'POST', body: JSON.stringify({
      document_id: Number(managementReports.previewDocument), expected_content_sha256: managementReports.preview.content_sha256,
      employee_mapping: mapping})});
    await loadReportSources();
    document.getElementById('reportVersion').value = String(result.snapshot_id);
    await loadManagementReport();
    feedback.textContent = `${result.replayed ? '此版本已保存，沒有重複匯入。' : '對照版本已保存到資料庫。'} ${result.backup.status === 'saved' ? 'Google Drive資料庫備份成功。' : '雲端備份未完成，重新部署前請在Excel線上資料處理重試備份。'}`;
  } catch (error) { feedback.textContent = error.message; button.disabled = false; }
}

async function loadManagementReport(offset = 0) {
  const month = document.getElementById('reportMonth');
  if (!month.value) month.value = todayString().slice(0, 7);
  const id = ++managementReports.request;
  document.getElementById('reportExport').disabled = true;
  document.getElementById('reportPrevious').disabled = true;
  document.getElementById('reportNext').disabled = true;
  document.getElementById('reportRows').innerHTML = '<tr><td>正在載入對照資料…</td></tr>';
  try {
    const params = reportParams();
    params.set('offset', offset);
    const data = await api(`/api/management-reports/report?${params}`);
    if (id !== managementReports.request) return;
    managementReports.offset = data.offset;
    document.getElementById('reportHeaders').innerHTML = `<tr>${data.fields.map(field => `<th>${escapeHtml(field)}</th>`).join('')}</tr>`;
    document.getElementById('reportRows').innerHTML = data.rows.length ? data.rows.map(row => `<tr>${row.map(value => `<td class="${typeof value === 'number' ? 'report-number' : ''}">${value == null ? '<span class="muted">待確認／無對應</span>' : escapeHtml(typeof value === 'number' ? value.toLocaleString('zh-TW', {maximumFractionDigits: 2}) : value)}</td>`).join('')}</tr>`).join('') : `<tr><td colspan="${data.fields.length}">此月份尚無資料${['payroll', 'roster'].includes(data.kind) && !data.snapshot_id ? '；請先預覽並保存Excel版本' : ''}。</td></tr>`;
    document.getElementById('reportSummary').innerHTML = Object.entries(data.summary).map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${value == null ? '待確認' : escapeHtml(value.toLocaleString('zh-TW'))}</strong></div>`).join('');
    document.getElementById('reportCount').textContent = `${data.title}｜${data.month}｜共${data.total}列｜${data.rows.length ? `${data.offset + 1}–${data.offset + data.rows.length}` : 0}｜${data.snapshot_id ? `Excel版本${data.snapshot_id}` : '僅系統資料'}`;
    document.getElementById('reportPrevious').disabled = !data.offset;
    document.getElementById('reportNext').disabled = data.offset + data.rows.length >= data.total;
    document.getElementById('reportExport').disabled = !data.total;
  } catch (error) {
    if (id !== managementReports.request) return;
    document.getElementById('reportRows').innerHTML = `<tr><td>${escapeHtml(error.message)}</td></tr>`;
  }
}

function changeManagementReportPage(delta) { loadManagementReport(Math.max(0, managementReports.offset + delta * 50)); }

async function exportManagementReport() {
  const button = document.getElementById('reportExport');
  button.disabled = true;
  try {
    const params = reportParams();
    const response = await fetch(`/api/management-reports/export?${params}`, {credentials: 'same-origin'});
    if (!response.ok) {
      if (response.status === 401) showLogin();
      const error = await response.json();
      throw new Error(error.detail || '匯出失敗');
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${params.get('month')}_${document.getElementById('reportKind').selectedOptions[0].textContent}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    document.getElementById('reportFeedback').textContent = response.headers.get('X-Database-Backup') === 'saved'
      ? '此表全部資料已下載，匯出稽核紀錄已備份。' : '此表已下載；匯出稽核雲端備份未完成，重新部署前請重試備份。';
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { document.getElementById('reportFeedback').textContent = error.message; }
  finally { button.disabled = false; }
}
