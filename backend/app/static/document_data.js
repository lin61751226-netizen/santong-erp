/* Structured business fields only; the original Excel remains untouched. */
const dataEditor = {data: null, row: null, initial: '', pending: null, busy: false, request: 0};

function renderDataDocumentOptions() {
  const select = document.getElementById('dataDocument');
  const selected = select.value;
  const documents = state.managedDocuments.filter(item => ['通訊錄與年度管理','收支明細'].includes(item.category));
  select.innerHTML = documents.length ? documents.map(item => `<option value="${item.id}">${escapeHtml(item.original_file_name)}</option>`).join('') : '<option value="">尚無可處理的公司 Excel</option>';
  if (documents.some(item => String(item.id) === selected)) select.value = selected;
}

async function openDocumentData(id) {
  const documentItem = state.managedDocuments.find(item => item.id === id);
  if (!documentItem) return;
  if (documentItem.category === '推高機計價') {
    openDocumentTask(1);
    document.getElementById('costMonthDocument').value = String(id);
    return;
  }
  openPanelFor('dataDocument');
  document.getElementById('dataDocument').value = String(id);
  await loadDocumentData(true);
}

async function loadDocumentData(reset = false, offset = 0) {
  const id = document.getElementById('dataDocument').value;
  const kind = document.getElementById('dataKind').value;
  const request = ++dataEditor.request;
  dataEditor.data = null;
  document.getElementById('dataExport').disabled = true;
  document.getElementById('dataPrevious').disabled = true;
  document.getElementById('dataNext').disabled = true;
  document.getElementById('dataRows').innerHTML = '';
  if (!id) return;
  if (reset) {
    document.getElementById('dataSheet').innerHTML = '<option value="">全部工作表</option>';
    document.getElementById('dataKeyword').value = '';
    document.getElementById('dataFeedback').textContent = '';
  }
  const sheet = document.getElementById('dataSheet').value;
  const params = new URLSearchParams({kind, sheet, keyword: document.getElementById('dataKeyword').value, offset, limit: 25});
  document.getElementById('dataStatus').textContent = '讀取已保存的資料…';
  try {
    const result = await api(`/api/document-data/${id}?${params}`);
    if (request !== dataEditor.request) return;
    dataEditor.data = {...result, documentId: id};
    document.getElementById('dataSheet').innerHTML = '<option value="">全部工作表</option>' + result.sheets.map(value => `<option>${escapeHtml(value)}</option>`).join('');
    document.getElementById('dataSheet').value = sheet;
    const keys = kind === 'finance' ? ['entry_date','entry_type','category','summary','vendor_name','income_amount','expense_amount','project_name'] : ['name','category','contact_person','phone','mobile','email'];
    const fields = keys.map(key => result.fields.find(field => field.key === key));
    document.getElementById('dataHeaders').innerHTML = `<tr>${fields.map(field => `<th>${escapeHtml(field.label)}</th>`).join('')}<th>來源</th><th>操作</th></tr>`;
    document.getElementById('dataRows').innerHTML = result.rows.length ? result.rows.map(row => `<tr>${fields.map(field => `<td>${escapeHtml(row.values[field.key] ?? '')}</td>`).join('')}<td>${escapeHtml(row.source_sheet || '線上新增')} ${row.source_row ? `第 ${row.source_row} 列` : ''}</td><td><button type="button" class="button-compact" onclick="editDocumentData(${row.id})">修改</button></td></tr>`).join('') : `<tr><td colspan="${fields.length + 2}">沒有資料。可新增一筆，或先預覽匯入來源 Excel。</td></tr>`;
    document.getElementById('dataStatus').textContent = `已存 ${result.dataset_total} 筆｜篩選 ${result.total} 筆｜${result.total ? result.offset + 1 : 0}–${Math.min(result.offset + 25, result.total)}。畫面修改需按「保存到資料庫」才生效。`;
    document.getElementById('dataPrevious').disabled = result.offset === 0;
    document.getElementById('dataNext').disabled = result.offset + 25 >= result.total;
    document.getElementById('dataExport').disabled = result.dataset_total === 0;
    const labels = Object.fromEntries(result.fields.map(field => [field.key, field.label]));
    document.getElementById('dataHistory').innerHTML = `<h3>最近修改（保留完整前後值）</h3>${result.history.map(item => `<p>${formatTW(item.created_at)}｜${item.action} #${item.record_id}｜${escapeHtml(item.changed_fields.map(key => labels[key]).join('、'))}</p>`).join('') || '<p>尚無線上修改</p>'}<h3>Excel 匯出版本</h3>${result.exports.map(item => `<p>${formatTW(item.created_at)} <a href="${escapeAttr(item.url)}" target="_blank" rel="noopener">${escapeHtml(item.name)}</a></p>`).join('') || '<p>尚無匯出版本</p>'}`;
  } catch (error) {
    if (request === dataEditor.request) document.getElementById('dataStatus').textContent = `讀取失敗：${error.message}`;
  }
}

function changeDataPage(direction) {
  if (dataEditor.data) loadDocumentData(false, Math.max(0, dataEditor.data.offset + direction * 25));
}

function dataFormValues() {
  const values = Object.fromEntries(new FormData(document.getElementById('dataEditForm')));
  for (const field of dataEditor.data.fields) {
    if (field.type === 'number') values[field.key] = Number(values[field.key] || 0);
    if (field.type === 'date' && !values[field.key]) values[field.key] = null;
  }
  return values;
}

function editDocumentData(id = null) {
  const data = dataEditor.data;
  if (!data) { document.getElementById('dataFeedback').textContent = '請先查詢／載入來源資料。'; return; }
  const row = id === null ? null : data.rows.find(item => item.id === id);
  if (id !== null && !row) return;
  dataEditor.row = row;
  dataEditor.pending = null;
  const values = row?.values || {entry_date: todayString(), entry_type: '支出', category: data.kind === 'contacts' ? '未分類' : ''};
  document.getElementById('dataEditTitle').textContent = `${row ? '修改' : '新增'}${data.kind === 'finance' ? '收支明細' : '公司通訊錄'}`;
  document.getElementById('dataEditSource').textContent = `${data.document.name}｜${row ? `#${row.id} ${row.source_sheet || ''}` : '線上新增'}。原始檔與來源列不改寫。`;
  document.getElementById('dataEditError').textContent = '';
  document.getElementById('dataEditFields').innerHTML = data.fields.map(field => {
    const value = escapeAttr(values[field.key] ?? '');
    const required = ['entry_date','entry_type','category','name'].includes(field.key) ? 'required' : '';
    let control;
    if (field.type === 'select') control = `<select id="edit-${field.key}" name="${field.key}"><option ${values[field.key] === '收入' ? 'selected' : ''}>收入</option><option ${values[field.key] !== '收入' ? 'selected' : ''}>支出</option></select>`;
    else if (field.type === 'textarea') control = `<textarea id="edit-${field.key}" name="${field.key}" rows="2" maxlength="4000">${escapeHtml(values[field.key] ?? '')}</textarea>`;
    else control = `<input id="edit-${field.key}" name="${field.key}" type="${field.type}" value="${value}" ${required} ${field.type === 'number' ? 'min="0" max="1000000000000" step="0.01"' : 'maxlength="4000"'}>`;
    return `<div class="${field.type === 'textarea' ? 'full' : ''}"><label for="edit-${field.key}">${escapeHtml(field.label)}</label>${control}</div>`;
  }).join('');
  dataEditor.initial = JSON.stringify(dataFormValues());
  document.getElementById('dataEditDialog').showModal();
}

function closeDataEdit() {
  if (dataEditor.busy) return;
  if (JSON.stringify(dataFormValues()) !== dataEditor.initial && !confirm('尚未保存的修改將不會寫入資料庫，確定取消？')) return;
  document.getElementById('dataEditDialog').close();
  dataEditor.pending = null;
}

function dataBackupText(result) {
  return result.backup?.status === 'saved' ? 'Google Drive 資料庫備份成功。' : `Google Drive 備份未成功（${result.backup?.status || 'unknown'}），請按「重試雲端備份」；在成功前不要重新部署。`;
}

document.getElementById('dataEditForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (dataEditor.busy) return;
  const data = dataEditor.data;
  const values = dataFormValues();
  const base = {kind: data.kind, record_id: dataEditor.row?.id || null, expected_revision: dataEditor.row?.revision || null, values};
  const signature = JSON.stringify(base);
  // Keep this request ID after an ambiguous network error, so retry cannot duplicate a save.
  if (dataEditor.pending?.signature !== signature) dataEditor.pending = {signature, payload: {...base, request_id: crypto.randomUUID()}};
  dataEditor.busy = true;
  const controls = [...document.querySelectorAll('#dataEditForm input, #dataEditForm select, #dataEditForm textarea, #dataEditForm button')];
  controls.forEach(control => control.disabled = true);
  document.getElementById('dataEditError').textContent = '正在保存資料與更新雲端備份…';
  try {
    const result = await api(`/api/document-data/${data.documentId}/save`, {method: 'POST', body: JSON.stringify(dataEditor.pending.payload)});
    document.getElementById('dataFeedback').textContent = `資料庫已保存 #${result.record_id}${result.unchanged ? '（內容未變更）' : ''}。${dataBackupText(result)}`;
    dataEditor.pending = null;
    document.getElementById('dataEditDialog').close();
    await loadDocumentData(false, data.offset);
  } catch (error) {
    document.getElementById('dataEditError').textContent = `${error.message}。內容保留，可確認後重試；衝突時請取消並重新載入。`;
  } finally {
    dataEditor.busy = false;
    controls.forEach(control => control.disabled = false);
  }
});

document.getElementById('dataEditDialog').addEventListener('cancel', event => { event.preventDefault(); closeDataEdit(); });
window.addEventListener('beforeunload', event => {
  if (document.getElementById('dataEditDialog').open && JSON.stringify(dataFormValues()) !== dataEditor.initial) {
    event.preventDefault(); event.returnValue = '';
  }
});

async function exportDocumentData() {
  const data = dataEditor.data;
  const button = document.getElementById('dataExport');
  if (!data || button.disabled) return;
  button.disabled = true;
  document.getElementById('dataFeedback').textContent = '正在匯出新的 Excel 版本到 Google Drive…';
  try {
    const result = await api(`/api/document-data/${data.documentId}/export`, {method: 'POST', body: JSON.stringify({kind: data.kind, expected_dataset: data.dataset_hash})});
    document.getElementById('dataFeedback').textContent = `新 Excel 已保存 ${result.count} 筆，原檔保留。${dataBackupText(result)}`;
    await loadManagedDocuments();
    await loadDocumentData(false, data.offset);
  } catch (error) {
    document.getElementById('dataFeedback').textContent = error.message;
  } finally { button.disabled = !dataEditor.data?.dataset_total; }
}

async function retryDataBackup() {
  const id = document.getElementById('dataDocument').value;
  const button = document.getElementById('dataBackup');
  if (!id || button.disabled) return;
  button.disabled = true;
  try {
    const result = await api(`/api/document-data/${id}/backup`, {method: 'POST'});
    document.getElementById('dataFeedback').textContent = dataBackupText(result);
  } catch (error) { document.getElementById('dataFeedback').textContent = error.message; }
  finally { button.disabled = false; }
}
