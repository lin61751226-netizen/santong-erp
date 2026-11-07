const companyWorkbook = {data: null, edits: new Map(), reviewed: null, busy: false, request: 0};

function companyWorkbookDirty() { return companyWorkbook.edits.size > 0; }

function renderCompanyWorkbookOptions() {
  const select = document.getElementById('companyWorkbookDocument');
  if (!select) return;
  const selected = select.value;
  const documents = state.managedDocuments.filter(item =>
    ['通訊錄與年度管理', '收支明細'].includes(item.category) && item.original_file_name.toLowerCase().endsWith('.xlsx')
  ).sort((a, b) => b.id - a.id);
  select.innerHTML = documents.map(item =>
    `<option value="${item.id}">${escapeHtml(item.original_file_name)}（#${item.id}）</option>`
  ).join('');
  if (documents.some(item => String(item.id) === selected)) select.value = selected;
}

function companyWorkbookResetReview() {
  companyWorkbook.reviewed = null;
  document.getElementById('companyWorkbookReview').hidden = true;
  document.getElementById('companyWorkbookConfirm').checked = false;
  document.getElementById('companyWorkbookConfirm').disabled = true;
  document.getElementById('companyWorkbookSave').disabled = true;
  document.getElementById('companyWorkbookPreview').disabled = !companyWorkbookDirty();
}

async function loadCompanyWorkbook() {
  const select = document.getElementById('companyWorkbookDocument');
  const month = document.getElementById('companyWorkbookMonth');
  const kind = document.getElementById('companyWorkbookKind');
  if (!select.value || !month.value) return;
  if (companyWorkbookDirty() && !confirm('有未保存的 Excel 修改。重新載入會捨棄修改，確定嗎？')) {
    if (companyWorkbook.data) {
      select.value = String(companyWorkbook.data.document_id);
      month.value = companyWorkbook.data.month;
      kind.value = companyWorkbook.data.kind;
    }
    return;
  }
  companyWorkbook.edits.clear();
  companyWorkbook.data = null;
  companyWorkbookResetReview();
  document.getElementById('companyWorkbookTable').innerHTML = '';
  const request = ++companyWorkbook.request;
  const status = document.getElementById('companyWorkbookStatus');
  status.textContent = '正在讀取 Google Drive 原始 Excel 工作表…';
  try {
    const query = new URLSearchParams({document_id: select.value, month: month.value, kind: kind.value});
    const result = await api(`/api/management-workbook-grid?${query}`);
    if (request !== companyWorkbook.request) return;
    companyWorkbook.data = result;
    renderCompanyWorkbook();
    status.textContent = `${result.document_name}｜${result.sheet_name}｜${result.rows.length} 列。保存前請先預覽。`;
  } catch (error) {
    if (request === companyWorkbook.request) status.textContent = `讀取失敗：${error.message}`;
  }
}

function renderCompanyWorkbook() {
  const grid = companyWorkbook.data;
  if (!grid) return;
  const fields = grid.fields;
  const headers = fields.map(field => `<th>${escapeHtml(field.label)}</th>`).join('');
  const rows = grid.rows.map(row => {
    const name = grid.kind === 'finance' ? `Excel ${row.row_number}` : `${escapeHtml(row.name)}<br><small>Excel ${row.row_number}</small>`;
    const cells = fields.map(field => {
      const value = row.values[field.key];
      if (row.locked.includes(field.key)) return `<td class="cost-grid-calculated">${escapeHtml(value ?? '')} <small>公式</small></td>`;
      const type = field.type === 'date' ? 'date' : field.type === 'number' ? 'number' : 'text';
      const constraints = type === 'number' ? 'min="0" max="1000000000" step="0.01"' : type === 'text' ? 'maxlength="500"' : '';
      return `<td><input aria-label="${escapeAttr(name.replace(/<[^>]*>/g, ' '))} ${escapeAttr(field.label)}" data-row="${row.row_number}" data-key="${escapeAttr(field.key)}" type="${type}" ${constraints} value="${escapeAttr(value ?? '')}" oninput="editCompanyWorkbookCell(this)"></td>`;
    }).join('');
    return `<tr><th>${name}</th>${cells}</tr>`;
  }).join('');
  document.getElementById('companyWorkbookTable').innerHTML = `<div class="cost-grid-caption"><strong>${escapeHtml(grid.sheet_name)}</strong><span>來源檔 #${grid.document_id} · 只編輯資料欄</span></div><div class="cost-grid-scroll"><table><thead><tr><th>來源列</th>${headers}</tr></thead><tbody>${rows || `<tr><td colspan="${fields.length + 1}">這個月份尚無可編輯列。</td></tr>`}</tbody></table></div>`;
}

function editCompanyWorkbookCell(input) {
  const rowNumber = Number(input.dataset.row);
  const key = input.dataset.key;
  const row = companyWorkbook.data?.rows.find(item => item.row_number === rowNumber);
  if (!row) return;
  const field = companyWorkbook.data.fields.find(item => item.key === key);
  const current = input.value === '' ? null : field.type === 'number' ? Number(input.value) : input.value;
  const values = {...(companyWorkbook.edits.get(rowNumber) || {})};
  if (current === row.values[key]) delete values[key];
  else values[key] = current;
  if (Object.keys(values).length) companyWorkbook.edits.set(rowNumber, values);
  else companyWorkbook.edits.delete(rowNumber);
  companyWorkbookResetReview();
  document.getElementById('companyWorkbookStatus').textContent = `未保存 ${companyWorkbook.edits.size} 列；請先預覽逐格變更。`;
}

function companyWorkbookPayload(previewOnly) {
  const grid = companyWorkbook.data;
  if (!grid || !companyWorkbookDirty()) throw new Error('沒有需要保存的修改');
  if (document.querySelector('#companyWorkbookTable input:invalid')) throw new Error('請先修正紅色標記的欄位');
  return {document_id: grid.document_id, month: grid.month, kind: grid.kind,
    expected_sha256: grid.sha256,
    rows: [...companyWorkbook.edits].map(([row_number, values]) => ({row_number, values})),
    preview_only: previewOnly, confirmed: document.getElementById('companyWorkbookConfirm').checked};
}

async function previewCompanyWorkbook() {
  const status = document.getElementById('companyWorkbookStatus');
  try {
    const payload = companyWorkbookPayload(true);
    const result = await api('/api/management-workbook-grid', {method: 'POST', body: JSON.stringify(payload)});
    const review = document.getElementById('companyWorkbookReview');
    review.textContent = result.changes.map(change =>
      `Excel 第 ${change.row_number} 列\n` + Object.keys(change.after).map(key =>
        `  ${companyWorkbook.data.fields.find(field => field.key === key)?.label || key}: ${change.before[key] ?? '空白'} → ${change.after[key] ?? '空白'}`
      ).join('\n')
    ).join('\n\n') || '沒有實際變更';
    review.hidden = false;
    companyWorkbook.reviewed = JSON.stringify(payload.rows);
    document.getElementById('companyWorkbookConfirm').disabled = result.changed_rows === 0;
    status.textContent = `已預覽 ${result.changed_rows} 列差異。確認後才會建立新 Excel 版本。`;
  } catch (error) {
    companyWorkbookResetReview();
    status.textContent = `預覽失敗：${error.message}`;
  }
}

async function saveCompanyWorkbook() {
  if (companyWorkbook.busy) return;
  const status = document.getElementById('companyWorkbookStatus');
  try {
    const payload = companyWorkbookPayload(false);
    if (!payload.confirmed || JSON.stringify(payload.rows) !== companyWorkbook.reviewed) throw new Error('請重新預覽並核對差異');
    companyWorkbook.busy = true;
    document.getElementById('companyWorkbookSave').disabled = true;
    status.textContent = '正在建立 Google Drive 新版本並保存稽核紀錄…';
    const result = await api('/api/management-workbook-grid', {method: 'POST', body: JSON.stringify(payload)});
    companyWorkbook.edits.clear();
    companyWorkbookResetReview();
    await loadManagedDocuments();
    document.getElementById('companyWorkbookDocument').value = String(result.document_id);
    await loadCompanyWorkbook();
    status.textContent = `已建立新 Excel 版本 #${result.document_id}，保存 ${result.saved} 列。${result.backup_status === 'saved' ? '資料庫雲端備份成功。' : '資料庫雲端備份未確認；重新部署前請重試備份。'}`;
  } catch (error) {
    status.textContent = `保存狀態未確認：${error.message}。請先檢查文件庫，勿直接重複送出。`;
  } finally {
    companyWorkbook.busy = false;
  }
}

document.getElementById('companyWorkbookConfirm').addEventListener('change', event => {
  document.getElementById('companyWorkbookSave').disabled = !event.target.checked || !companyWorkbook.reviewed;
});
window.addEventListener('beforeunload', event => {
  if (companyWorkbookDirty()) { event.preventDefault(); event.returnValue = ''; }
});
