const managementGrid = {mode: 'pricing', data: null, edits: new Map(), pending: new Map(), busy: false, request: 0};

function managementGridDirty() { return managementGrid.edits.size > 0; }

function showExcelMode(mode, {autoLoad = true} = {}) {
  if (managementGridDirty() && managementGrid.mode !== mode && !confirm('薪資／排休月表格有未保存修改，確定切換嗎？')) return false;
  if (managementGrid.mode === 'workbook' && mode !== 'workbook' && companyWorkbookDirty() && !confirm('公司 Excel 月表格有未保存修改，確定切換嗎？')) return false;
  if (costGridDirty() && managementGrid.mode === 'pricing' && mode !== 'pricing' && !confirm('堆高機計價有未保存修改，確定切換嗎？')) return false;
  if (worksitePricingDirty() && managementGrid.mode === 'sitepricing' && mode !== 'sitepricing' && !confirm('國泰慈文計價有未保存修改，確定切換嗎？')) return false;
  if (managementGrid.mode !== mode) managementGrid.edits.clear();
  managementGrid.mode = mode;
  for (const name of ['Pricing', 'Sitepricing', 'Workbook', 'Finance', 'Payroll', 'Roster']) {
    document.getElementById(`excelMode${name}`).classList.toggle('active', name.toLowerCase() === mode);
  }
  document.getElementById('costGridSection').hidden = mode !== 'pricing';
  document.getElementById('worksitePricingSection').hidden = mode !== 'sitepricing';
  document.getElementById('companyWorkbookSection').hidden = mode !== 'workbook';
  document.getElementById('dataEditorSection').hidden = mode !== 'finance';
  document.getElementById('managementMonthGridSection').hidden = !['payroll', 'roster'].includes(mode);
  if (typeof syncDataWorkflowFromMode === 'function') syncDataWorkflowFromMode(mode);
  if (mode === 'finance' && autoLoad) loadDocumentData(true);
  if (mode === 'workbook') {
    renderCompanyWorkbookOptions();
    if (autoLoad && document.getElementById('companyWorkbookDocument').value) loadCompanyWorkbook();
  }
  if (mode === 'sitepricing') {
    const month = document.getElementById('worksitePricingMonth');
    if (!month.value) month.value = todayString().slice(0, 7) < '2026-11' ? '2026-11' : todayString().slice(0, 7);
    if (autoLoad) loadWorksitePricing();
  }
  if (mode === 'payroll' || mode === 'roster') {
    const month = document.getElementById('managementGridMonth');
    if (!month.value) month.value = todayString().slice(0, 7);
    if (autoLoad) loadManagementGridSources();
  }
  return true;
}

async function loadManagementGridSources() {
  const select = document.getElementById('managementGridSnapshot');
  const selected = select.value;
  const status = document.getElementById('managementGridStatus');
  try {
    const sources = await api('/api/management-reports/sources');
    const versions = sources.flatMap(source => source.versions.map(version => ({...version, name: source.name})))
      .sort((a, b) => b.id - a.id);
    select.innerHTML = versions.length ? versions.map(item =>
      `<option value="${item.id}">${escapeHtml(item.name)}｜版本 #${item.id}</option>`).join('') :
      '<option value="">尚無已保存的全年管理版本</option>';
    if (versions.some(item => String(item.id) === selected)) select.value = selected;
    if (select.value) await loadManagementMonthGrid();
    else status.textContent = '先至「月度與年度管理對照」預覽並保存全年管理 Excel 版本；原檔仍保留。';
  } catch (error) { status.textContent = `載入全年管理版本失敗：${error.message}`; }
}

async function loadManagementMonthGrid() {
  const snapshotId = document.getElementById('managementGridSnapshot').value;
  const month = document.getElementById('managementGridMonth').value;
  const kind = managementGrid.mode;
  if (!['payroll', 'roster'].includes(kind) || !snapshotId || !month) return;
  if (managementGridDirty() && !confirm('月表格有未保存修改，重新載入會捨棄，確定嗎？')) return;
  managementGrid.edits.clear();
  managementGrid.pending.clear();
  const request = ++managementGrid.request;
  document.getElementById('managementGridStatus').textContent = '正在讀取月表格…';
  try {
    const params = new URLSearchParams({snapshot_id: snapshotId, month, kind});
    const result = await api(`/api/management-reports/month-grid?${params}`);
    if (request !== managementGrid.request || kind !== managementGrid.mode) return;
    managementGrid.data = result;
    renderManagementMonthGrid();
    document.getElementById('managementGridStatus').textContent = `${month}｜${kind === 'payroll' ? '薪資' : '排休'} ${result.rows.length} 列；原始 Excel 不覆蓋。`;
  } catch (error) { if (request === managementGrid.request) document.getElementById('managementGridStatus').textContent = `讀取失敗：${error.message}`; }
}

function managementGridValues(index) {
  return managementGrid.edits.get(index) || managementGrid.data.rows[index].values;
}

function managementPayrollTotals(values) {
  const fields = managementGrid.data.fields;
  const sum = keys => keys.some(key => values[key] === null || values[key] === '' || values[key] === undefined)
    ? null : keys.reduce((total, key) => total + Number(values[key]), 0);
  const gross = sum(fields.slice(0, 6));
  const deductions = sum(fields.slice(6));
  return [gross, deductions, gross === null || deductions === null ? null : gross - deductions];
}

function renderManagementMonthGrid() {
  const data = managementGrid.data;
  if (!data) return;
  const payroll = data.kind === 'payroll';
  const fields = data.fields;
  const heading = payroll ? fields.map(field => `<th>${escapeHtml(field)}</th>`).join('') + '<th>發薪狀態</th><th>備註</th><th>應領</th><th>扣款</th><th>實領</th>' :
    fields.map(day => `<th>${day}日</th>`).join('') + '<th>備註</th>';
  const rows = data.rows.map((row, index) => {
    const values = managementGridValues(index);
    const totals = payroll ? managementPayrollTotals(values) : [];
    const fieldCells = payroll ? fields.map(field =>
      `<td><input type="number" min="0" max="1000000000" step="0.01" aria-label="${escapeAttr(row.name)} ${escapeAttr(field)}" data-index="${index}" data-field="${escapeAttr(field)}" value="${escapeAttr(values[field] ?? '')}" oninput="editManagementGridCell(this)"></td>`).join('') +
      `<td><input data-index="${index}" data-field="status" value="${escapeAttr(values.status || '')}" oninput="editManagementGridCell(this)"></td>` :
      fields.map(day => `<td><input aria-label="${escapeAttr(row.name)} ${day}日" data-index="${index}" data-day="${day}" value="${escapeAttr(values.marks?.[day] || '')}" oninput="editManagementGridCell(this)"></td>`).join('');
    return `<tr><th scope="row">${escapeHtml(row.name)}</th><td>${escapeHtml(row.employee_code || '')}</td>${fieldCells}
      <td><input data-index="${index}" data-field="note" value="${escapeAttr(values.note || '')}" oninput="editManagementGridCell(this)"></td>
      ${payroll ? totals.map((value, n) => `<td class="cost-grid-calculated" data-total="${n}">${value === null ? '待確認' : Number(value).toLocaleString('zh-TW', {maximumFractionDigits: 2})}</td>`).join('') : ''}
      <td><button type="button" class="button-compact" onclick="saveManagementGridRow(${index})" ${managementGrid.edits.has(index) ? '' : 'disabled'}>核對並保存</button></td></tr>`;
  }).join('');
  document.getElementById('managementGridTable').innerHTML = `<div class="cost-grid-scroll"><table><thead><tr><th>姓名</th><th>員工代碼</th>${heading}<th>操作</th></tr></thead><tbody>${rows || `<tr><td colspan="${fields.length + (payroll ? 8 : 4)}">此月份沒有原表資料；請先匯入相應月份的全年管理檔。</td></tr>`}</tbody></table></div>`;
}

function editManagementGridCell(input) {
  const index = Number(input.dataset.index);
  const row = managementGrid.data?.rows[index];
  if (!row) return;
  const prior = managementGridValues(index);
  const values = {...prior, marks: prior.marks ? {...prior.marks} : undefined};
  if (input.dataset.day) {
    if (input.value.trim()) values.marks[input.dataset.day] = input.value.trim();
    else delete values.marks[input.dataset.day];
  } else if (input.type === 'number') values[input.dataset.field] = input.value === '' ? null : Number(input.value);
  else values[input.dataset.field] = input.value;
  if (JSON.stringify(values) === JSON.stringify(row.values)) managementGrid.edits.delete(index);
  else managementGrid.edits.set(index, values);
  managementGrid.pending.delete(index);
  const button = input.closest('tr').querySelector('button');
  button.disabled = !managementGrid.edits.has(index);
  if (managementGrid.data.kind === 'payroll') {
    managementPayrollTotals(values).forEach((value, n) => {
      input.closest('tr').querySelector(`[data-total="${n}"]`).textContent =
        value === null ? '待確認' : Number(value).toLocaleString('zh-TW', {maximumFractionDigits: 2});
    });
  }
  document.getElementById('managementGridStatus').textContent = `未保存 ${managementGrid.edits.size} 列。每列按「核對並保存」。`;
}

async function saveManagementGridRow(index) {
  if (managementGrid.busy || !managementGrid.edits.has(index)) return;
  const data = managementGrid.data;
  const row = data.rows[index];
  const values = managementGrid.edits.get(index);
  const invalid = document.querySelector('#managementGridTable input:invalid');
  if (invalid) { invalid.focus(); document.getElementById('managementGridStatus').textContent = '有無效的金額或文字，請先修正。'; return; }
  const signature = JSON.stringify(values);
  if (managementGrid.pending.get(index)?.signature !== signature) managementGrid.pending.set(index, {signature, requestId: crypto.randomUUID()});
  const payload = {snapshot_id: data.snapshot_id, month: data.month, kind: data.kind, source: row.source,
    expected_revision: row.revision, request_id: managementGrid.pending.get(index).requestId, values};
  const status = document.getElementById('managementGridStatus');
  managementGrid.busy = true;
  try {
    const preview = await api('/api/management-reports/month-grid', {method: 'POST', body: JSON.stringify({...payload, preview_only: true})});
    if (preview.replayed) {
      row.values = values;
      row.revision = preview.revision;
      managementGrid.edits.delete(index);
      managementGrid.pending.delete(index);
      renderManagementMonthGrid();
      status.textContent = `${row.name} 前一次請求已保存；請重新載入核對雲端紀錄。`;
      return;
    }
    if (!preview.changed) { managementGrid.edits.delete(index); renderManagementMonthGrid(); status.textContent = '內容沒有變更。'; return; }
    const changes = Object.keys(values).filter(key => JSON.stringify(values[key]) !== JSON.stringify(preview.before[key]));
    if (!confirm(`核對 ${row.name} 的${data.kind === 'payroll' ? '薪資' : '排休'}修改：${changes.join('、')}。\n原始 Excel 不覆蓋；確定保存系統修訂嗎？`)) return;
    const result = await api('/api/management-reports/month-grid', {method: 'POST', body: JSON.stringify({...payload, confirmed: true})});
    row.values = values;
    row.revision = result.revision;
    managementGrid.edits.delete(index);
    managementGrid.pending.delete(index);
    renderManagementMonthGrid();
    status.textContent = `${row.name} 已保存修訂。${result.backup?.status === 'saved' ? 'Google Drive 資料庫備份成功。' : '雲端備份未成功；重試前請勿重新部署。'}`;
  } catch (error) { status.textContent = `保存未確認：${error.message}；先重新載入核對，避免重複寫入。`; }
  finally { managementGrid.busy = false; }
}

window.addEventListener('beforeunload', event => {
  if (managementGridDirty()) { event.preventDefault(); event.returnValue = ''; }
});
