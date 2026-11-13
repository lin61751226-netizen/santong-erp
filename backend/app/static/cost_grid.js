const costGrid = {data: null, label: null, edits: new Map(), reviewed: null, busy: false, request: 0, adjustmentDraft: null, adjustmentBusy: false, sourceSlip: null};
const costGridFields = ['normal_hours', 'overtime_hours', 'support_hours', 'external_customer', 'fuel_cost', 'payer'];

function renderCostGridDocumentOptions() {
  const select = document.getElementById('costGridDocument');
  const selected = select.value;
  const documents = state.managedDocuments.filter(item => item.category === '推高機計價');
  select.innerHTML = documents.length
    ? documents.map(item => `<option value="${item.id}">${escapeHtml(item.original_file_name)}</option>`).join('')
    : '<option value="">尚無計價 Excel</option>';
  if (documents.some(item => String(item.id) === selected)) select.value = selected;
  const month = document.getElementById('costGridMonth');
  if (!month.value) month.value = todayString().slice(0, 7);
}

function costGridDirty() { return costGrid.edits.size > 0 || Boolean(costGrid.adjustmentDraft?.dirty); }

async function loadCostGrid() {
  const documentId = document.getElementById('costGridDocument').value;
  const month = document.getElementById('costGridMonth').value;
  const previousLabel = costGrid.data?.document_id === Number(documentId) ? costGrid.label : null;
  if (costGridDirty() && !confirm('月表格有未保存修改。重新載入會捨棄這些修改，確定嗎？')) {
    if (costGrid.data) {
      document.getElementById('costGridDocument').value = costGrid.data.document_id;
      document.getElementById('costGridMonth').value = `${costGrid.data.year}-${String(costGrid.data.month).padStart(2, '0')}`;
    }
    return;
  }
  costGrid.edits.clear();
  costGrid.sourceSlip = null;
  document.getElementById('costGridSlipAmountReason').hidden = true;
  costGrid.adjustmentDraft = null;
  document.getElementById('costGridAdjustment').hidden = true;
  costGrid.reviewed = null;
  costGrid.data = null;
  costGrid.label = null;
  costGridReviewReset();
  document.getElementById('costGridTabs').innerHTML = '';
  document.getElementById('costGridSheet').innerHTML = '';
  if (!documentId || !month) return;
  const request = ++costGrid.request;
  document.getElementById('costGridStatus').textContent = '正在讀取 Google Drive 計價月表格…';
  try {
    const result = await api(`/api/cost-hour-imports/grid?${new URLSearchParams({document_id: documentId, year: month.slice(0, 4), month: Number(month.slice(5))})}`);
    if (request !== costGrid.request) return;
    costGrid.data = result;
    costGrid.label = result.sheets.find(sheet => sheet.label === previousLabel)?.label || result.sheets[0]?.label || null;
    renderCostGrid();
    const pendingCount = result.sheets.reduce((count, sheet) => count + sheet.rows.filter(row => row.pending_sign_slip).length, 0);
    document.getElementById('costGridStatus').textContent = `${result.document_name}｜${result.year}/${String(result.month).padStart(2, '0')}｜${result.sheets.length} 個標別。${pendingCount ? `已存簽單待核對 ${pendingCount} 日；標黃列可直接帶入。` : '修改後先核對，再保存。'}`;
  } catch (error) {
    if (request === costGrid.request) document.getElementById('costGridStatus').textContent = `月表格讀取失敗：${error.message}`;
  }
}

function selectCostGridSheet(index) {
  if (costGridDirty() && !confirm('目前標別有未保存修改。切換會捨棄修改，確定嗎？')) return;
  costGrid.edits.clear();
  costGrid.sourceSlip = null;
  document.getElementById('costGridSlipAmountReason').hidden = true;
  costGrid.adjustmentDraft = null;
  document.getElementById('costGridAdjustment').hidden = true;
  costGrid.label = costGrid.data?.sheets[index]?.label || null;
  costGridReviewReset();
  renderCostGrid();
}

function costGridValue(value) { return value === null || value === undefined ? '' : String(value); }
function costGridNumber(value) { return value === '' ? null : Number(value); }
function costGridCurrent(row) {
  const edit = costGrid.edits.get(row.day);
  return edit || Object.fromEntries(costGridFields.map(field => [field, row[field]]));
}

function renderCostGrid() {
  const data = costGrid.data;
  if (!data) return;
  const active = data.sheets.find(sheet => sheet.label === costGrid.label);
  document.getElementById('costGridTabs').innerHTML = data.sheets.map((sheet, index) =>
    `<button type="button" role="tab" aria-selected="${sheet.label === costGrid.label}" class="${sheet.label === costGrid.label ? 'active' : ''}" onclick="selectCostGridSheet(${index})">${escapeHtml(sheet.label)}</button>`
  ).join('');
  if (!active) return;
  const rate = active.rates;
  const formatMoney = value => Number(value || 0).toLocaleString('zh-TW', {maximumFractionDigits: 2});
  const rows = active.rows.map(row => {
    const values = costGridCurrent(row);
    const weekday = new Date(`${row.day}T12:00:00`).getDay();
    const holiday = row.is_holiday || weekday === 6;
    const input = (field, type, step = '') => `<input aria-label="${row.day} ${field}" data-day="${row.day}" data-field="${field}" type="${type}" ${type === 'number' ? `min="0" max="${field === 'fuel_cost' ? '1000000000' : '10000'}" step="${step}"` : 'maxlength="200"'} value="${escapeAttr(costGridValue(values[field]))}" oninput="editCostGridCell(this)">`;
    const slip = row.pending_sign_slip;
    const slipButton = slip ? `<button type="button" class="button-compact" onclick="stageCostGridSignSlip('${row.day}')" ${slip.conflict ? 'disabled' : ''}>${slip.conflict ? '簽單衝突' : slip.hours_differ ? '帶入簽單' : '核對簽單金額'}</button>` : '';
    const slipHint = slip ? `<small class="cost-grid-slip-hint">${escapeHtml(slip.conflict ? slip.reason : `簽單 ${slip.slip_no}｜正常 ${slip.normal_hours} / 加班 ${slip.overtime_hours}｜公式 ${formatMoney(slip.proposed_formula_amount)}${slip.signed_amount != null ? `｜簽認 ${formatMoney(slip.signed_amount)}` : ''}`)}</small>` : '';
    return `<tr class="${slip ? 'cost-grid-pending' : holiday ? 'cost-grid-holiday' : ''}">
      <th scope="row">${escapeHtml(row.day.slice(5))} ${'日一二三四五六'[weekday]}${row.is_holiday ? ' · 假日' : ''}${slipHint}</th>
      <td>${input('normal_hours', 'number', 'any')}</td><td class="cost-grid-calculated">${formatMoney(row.normal_amount)}</td>
      <td>${input('overtime_hours', 'number', 'any')}</td><td class="cost-grid-calculated">${formatMoney(row.overtime_amount)}</td>
      <td>${input('support_hours', 'number', 'any')}</td><td class="cost-grid-calculated">${formatMoney(row.support_amount)}</td>
      <td class="cost-grid-calculated ${row.adjustment_stale ? 'cost-grid-stale' : row.manual_total_amount != null ? 'cost-grid-adjusted' : ''}">${formatMoney(row.effective_total ?? row.total)}${row.adjustment_stale ? ' · 需重核' : ''}</td>
      <td>${slipButton}<button type="button" class="button-compact" onclick="openCostGridAdjustment('${row.day}')">調整金額</button></td>
      <td>${input('external_customer', 'text')}</td><td>${input('fuel_cost', 'number', 'any')}</td><td>${input('payer', 'text')}</td>
    </tr>`;
  }).join('');
  document.getElementById('costGridSheet').innerHTML = `<div class="cost-grid-caption"><strong>${escapeHtml(active.worksheet_name)}</strong><span>時薪 ${formatMoney(rate.normal_hourly)} · 日薪8H ${formatMoney(rate.daily)} · 超時/H ${formatMoney(rate.ot_rate)} · 假日/H ${formatMoney(rate.holiday_rate)} · 支援/H ${formatMoney(rate.support_rate)} · 稅率 ${data.tax_rate}</span></div>
    <div class="cost-grid-scroll"><table><thead><tr><th>日期</th><th>正常<br>時數</th><th>正常<br>金額</th><th>加班<br>時數</th><th>加班<br>金額</th><th>支援<br>時數</th><th>支援<br>金額</th><th>核定<br>未稅金額</th><th>操作</th><th>外叫<br>客戶</th><th>油錢</th><th>付款人</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function costGridReviewReset() {
  costGrid.reviewed = null;
  document.getElementById('costGridReview').hidden = true;
  document.getElementById('costGridConfirm').checked = false;
  document.getElementById('costGridConfirm').disabled = true;
  document.getElementById('costGridSave').disabled = true;
  document.getElementById('costGridPreview').disabled = costGrid.edits.size === 0;
}

function editCostGridCell(input) {
  if (costGrid.busy) return;
  const active = costGrid.data?.sheets.find(sheet => sheet.label === costGrid.label);
  const row = active?.rows.find(item => item.day === input.dataset.day);
  if (!row) return;
  const values = {...costGridCurrent(row)};
  values[input.dataset.field] = input.type === 'number' ? costGridNumber(input.value) : (input.value.trim() || null);
  if (costGrid.sourceSlip && (input.dataset.day !== costGrid.sourceSlip.day ||
      ['normal_hours', 'overtime_hours'].includes(input.dataset.field) && values[input.dataset.field] !== costGrid.sourceSlip[input.dataset.field])) {
    costGrid.sourceSlip = null;
    document.getElementById('costGridSlipAmountReason').hidden = true;
  }
  if (costGridFields.every(field => values[field] === row[field])) costGrid.edits.delete(row.day);
  else costGrid.edits.set(row.day, values);
  costGridReviewReset();
  document.getElementById('costGridStatus').textContent = `未保存 ${costGrid.edits.size} 個日期。金額欄尚未重新計算，請按「核對變更與金額」。`;
  window.DraftRecovery?.captureGrid();
}

async function stageCostGridSignSlip(day) {
  if (costGridDirty()) { document.getElementById('costGridStatus').textContent = '請先保存或取消目前未保存的修改，再帶入簽單。'; return; }
  const sheet = costGrid.data?.sheets.find(item => item.label === costGrid.label);
  const row = sheet?.rows.find(item => item.day === day);
  const slip = row?.pending_sign_slip;
  if (!slip || slip.conflict) return;
  if (!slip.hours_differ) {
    openCostGridAdjustment(day);
    document.getElementById('costGridAdjustmentAmount').value = slip.signed_amount ?? '';
    editCostGridAdjustment();
    document.getElementById('costGridStatus').textContent = '工時已一致；請填寫核定金額原因後預覽保存。';
    return;
  }
  costGrid.sourceSlip = {id: slip.slip_id, updatedAt: slip.slip_updated_at, day,
    normal_hours: slip.normal_hours, overtime_hours: slip.overtime_hours,
    adjustmentRevision: row.adjustment_revision || 0};
  document.getElementById('costGridSlipAmountReason').hidden = !slip.signed_amount_differs;
  document.getElementById('costGridSlipReasonInput').value = '';
  costGrid.edits.set(day, {...costGridCurrent(row), normal_hours: slip.normal_hours, overtime_hours: slip.overtime_hours});
  costGridReviewReset();
  renderCostGrid();
  await previewCostGrid();
  window.DraftRecovery?.captureGrid();
  if (!costGrid.reviewed) return;
  const note = `簽單 ${slip.slip_no}：正常 ${slip.normal_hours}、加班 ${slip.overtime_hours} 小時；原表正常 ${row.normal_hours ?? 0}、加班 ${row.overtime_hours ?? 0}。`;
  const review = document.getElementById('costGridReview');
  review.textContent = `${note}\n${review.textContent}${slip.signed_amount_differs ? `\n簽認金額 ${Number(slip.signed_amount).toLocaleString()} 元與公式 ${Number(slip.proposed_formula_amount).toLocaleString()} 元不同；填寫核定原因後可與工時同次保存。` : ''}`;
  review.hidden = false;
}

function costGridPayload(previewOnly) {
  if (!costGrid.data || costGrid.edits.size === 0) throw new Error('沒有需要保存的變更。');
  const invalid = document.querySelector('#costGridSheet input:invalid');
  if (invalid) { invalid.focus(); throw new Error('有無效的數值或欄位長度，請先修正。'); }
  return {
    document_id: costGrid.data.document_id, year: costGrid.data.year, month: costGrid.data.month,
    label: costGrid.label, expected_sha256: costGrid.data.sha256,
    rows: [...costGrid.edits].map(([day, values]) => ({day, ...values})),
    ...(costGrid.sourceSlip ? {source_sign_slip_id: costGrid.sourceSlip.id,
      expected_sign_slip_updated_at: costGrid.sourceSlip.updatedAt,
      expected_adjustment_revision: costGrid.sourceSlip.adjustmentRevision,
      sign_slip_amount_reason: document.getElementById('costGridSlipReasonInput').value.trim()} : {}),
    preview_only: previewOnly, confirm_amount_changes: document.getElementById('costGridConfirm').checked,
  };
}

async function previewCostGrid() {
  const review = document.getElementById('costGridReview');
  try {
    const payload = costGridPayload(true);
    document.getElementById('costGridPreview').disabled = true;
    const result = await api('/api/cost-hour-imports/grid', {method: 'POST', body: JSON.stringify(payload)});
    const amounts = result.amount_changes || [];
    const slipMismatches = result.slip_mismatches || [];
    const signed = result.signed_adjustment;
    review.hidden = false;
    review.textContent = `僅預覽，未保存：${result.changed_rows} 個日期變更。${amounts.length ? '\n金額變動：\n' + amounts.map(item => `${item.day}　${Number(item.previous).toLocaleString()} → ${Number(item.new).toLocaleString()}`).join('\n') : '\n金額未變動。'}${signed ? `\n簽認金額：公式 ${Number(signed.formula).toLocaleString()} → 核定 ${Number(signed.signed).toLocaleString()}；原因：${signed.reason || '尚未填寫'}` : ''}${slipMismatches.length ? '\n與已存簽單工時不同：' + slipMismatches.join('、') : ''}\n核對後再按保存。`;
    costGrid.reviewed = JSON.stringify({rows: payload.rows, source_sign_slip_id: payload.source_sign_slip_id,
      sign_slip_amount_reason: payload.sign_slip_amount_reason});
    const needsConfirm = amounts.length > 0 || slipMismatches.length > 0 || Boolean(signed);
    document.getElementById('costGridConfirm').disabled = !needsConfirm || Boolean(signed && !signed.reason);
    document.getElementById('costGridSave').disabled = needsConfirm;
    if (signed && !signed.reason) document.getElementById('costGridStatus').textContent = '請先填寫簽認金額的核定原因，再重新預覽。';
  } catch (error) {
    document.getElementById('costGridStatus').textContent = `核對失敗：${error.message}`;
  } finally { document.getElementById('costGridPreview').disabled = costGrid.edits.size === 0; }
}

document.getElementById('costGridConfirm').addEventListener('change', () => {
  document.getElementById('costGridSave').disabled = !costGrid.reviewed || !document.getElementById('costGridConfirm').checked;
});

async function saveCostGrid() {
  if (costGrid.busy) return;
  try {
    const payload = costGridPayload(false);
    if (JSON.stringify({rows: payload.rows, source_sign_slip_id: payload.source_sign_slip_id,
      sign_slip_amount_reason: payload.sign_slip_amount_reason}) !== costGrid.reviewed) throw new Error('內容已改變，請重新核對。');
    costGrid.busy = true;
    window.DraftRecovery?.formal('cost-grid','saving');
    document.getElementById('costGridSave').disabled = true;
    document.getElementById('costGridStatus').textContent = '正在保存至 Google Drive 並建立資料庫備份…';
    const result = await api('/api/cost-hour-imports/grid', {method: 'POST', body: JSON.stringify(payload)});
    await window.DraftRecovery?.close('cost-grid',false);
    window.DraftRecovery?.formal('cost-grid','saved',result.backup_status);
    const savedLabel = costGrid.label;
    costGrid.edits.clear();
    costGrid.sourceSlip = null;
    document.getElementById('costGridSlipAmountReason').hidden = true;
    costGridReviewReset();
    await loadCostGrid();
    if (costGrid.data?.sheets.some(sheet => sheet.label === savedLabel)) {
      costGrid.label = savedLabel;
      renderCostGrid();
    }
    document.getElementById('costGridStatus').textContent = `已保存 ${result.saved} 個日期。${result.backup_status === 'saved' ? 'Google Drive 資料庫備份成功。' : '資料庫雲端備份未成功；在重試備份前請勿重新部署。'}`;
  } catch (error) {
    document.getElementById('costGridStatus').textContent = `保存未確認：${error.message}。修改仍保留；請先重新核對雲端狀態，勿直接重複送出。`;
    window.DraftRecovery?.formal('cost-grid','failed');
  } finally { costGrid.busy = false; }
}

async function retryCostGridBackup() {
  const button = document.getElementById('costGridBackup');
  button.disabled = true;
  try {
    const result = await api('/api/cost-hour-imports/grid/backup', {method: 'POST'});
    document.getElementById('costGridStatus').textContent = result.backup_status === 'saved'
      ? '資料庫雲端備份成功。' : '資料庫雲端備份仍未成功，請勿重新部署。';
  } catch (error) {
    document.getElementById('costGridStatus').textContent = `備份失敗：${error.message}`;
  } finally { button.disabled = false; }
}

function openCostGridAdjustment(day) {
  if (costGrid.edits.size) {
    document.getElementById('costGridStatus').textContent = '請先保存或取消未保存的工時／備註修改，再調整核定金額。';
    return;
  }
  if (costGrid.adjustmentDraft?.dirty && !confirm('目前核定金額尚未保存，確定放棄修改？')) return;
  const sheet = costGrid.data?.sheets.find(item => item.label === costGrid.label);
  const row = sheet?.rows.find(item => item.day === day);
  if (!row) return;
  costGrid.adjustmentDraft = {day, dirty: false, reviewed: null};
  document.getElementById('costGridAdjustmentTitle').textContent = `${sheet.label}｜${day} 核定金額調整`;
  document.getElementById('costGridAdjustmentFormula').textContent =
    `原 Excel 公式未稅 ${Number(row.total).toLocaleString('zh-TW')} 元；目前核定 ${Number(row.effective_total ?? row.total).toLocaleString('zh-TW')} 元。${row.adjustment_stale ? '原公式已變動，舊調整暫不採用，請重新核對。' : ''}`;
  document.getElementById('costGridAdjustmentAmount').value = row.manual_total_amount ?? '';
  document.getElementById('costGridAdjustmentReason').value = row.amount_adjustment_reason || '';
  document.getElementById('costGridAdjustmentReview').hidden = true;
  document.getElementById('costGridAdjustmentSave').disabled = true;
  document.getElementById('costGridAdjustment').hidden = false;
  document.getElementById('costGridAdjustment').scrollIntoView({block: 'nearest'});
}

function editCostGridAdjustment() {
  const draft = costGrid.adjustmentDraft;
  if (!draft) return;
  const sheet = costGrid.data?.sheets.find(item => item.label === costGrid.label);
  const row = sheet?.rows.find(item => item.day === draft.day);
  const rawAmount = document.getElementById('costGridAdjustmentAmount').value;
  const amount = rawAmount === '' ? null : Number(rawAmount);
  const reason = amount === null ? '' : document.getElementById('costGridAdjustmentReason').value.trim();
  draft.dirty = Boolean(row?.adjustment_stale) || amount !== (row?.manual_total_amount ?? null) || reason !== (row?.amount_adjustment_reason || '');
  draft.reviewed = null;
  document.getElementById('costGridAdjustmentReview').hidden = true;
  document.getElementById('costGridAdjustmentSave').disabled = true;
  window.DraftRecovery?.captureGrid();
}

function cancelCostGridAdjustment() {
  if (costGrid.adjustmentDraft?.dirty && !confirm('核定金額尚未保存，確定放棄修改？')) return;
  costGrid.adjustmentDraft = null;
  document.getElementById('costGridAdjustment').hidden = true;
}

function costGridAdjustmentPayload(previewOnly) {
  const draft = costGrid.adjustmentDraft;
  if (!draft || !costGrid.data || !draft.dirty) throw new Error('請先修改核定金額或原因。');
  if (costGrid.edits.size) throw new Error('請先保存或取消工時／備註修改。');
  const amountInput = document.getElementById('costGridAdjustmentAmount');
  if (!amountInput.checkValidity()) { amountInput.focus(); throw new Error('核定金額無效，最多兩位小數。'); }
  const manualAmount = amountInput.value === '' ? null : Number(amountInput.value);
  const reason = manualAmount === null ? '' : document.getElementById('costGridAdjustmentReason').value.trim();
  if (manualAmount !== null && !reason) throw new Error('請填寫核定金額的調整原因。');
  const sheet = costGrid.data.sheets.find(item => item.label === costGrid.label);
  const row = sheet.rows.find(item => item.day === draft.day);
  return {document_id: costGrid.data.document_id, day: draft.day, label: costGrid.label,
    expected_sha256: costGrid.data.sha256, expected_revision: row.adjustment_revision || 0,
    manual_total_amount: manualAmount, reason, preview_only: previewOnly};
}

async function previewCostGridAdjustment() {
  const status = document.getElementById('costGridStatus');
  try {
    const payload = costGridAdjustmentPayload(true);
    const result = await api('/api/cost-hour-imports/grid/amount-adjustment', {method: 'POST', body: JSON.stringify(payload)});
    const review = document.getElementById('costGridAdjustmentReview');
    review.textContent = `${result.day}｜原 Excel 公式 ${Number(result.formula_amount).toLocaleString('zh-TW')} 元\n目前採用 ${Number(result.before).toLocaleString('zh-TW')} → 核定後 ${Number(result.after).toLocaleString('zh-TW')} 元\n調整原因：${result.reason || '恢復公式'}。原 Excel 公式與工時不會改寫。`;
    review.hidden = false;
    costGrid.adjustmentDraft.reviewed = JSON.stringify(payload);
    document.getElementById('costGridAdjustmentSave').disabled = !result.changed;
    status.textContent = result.changed ? '僅預覽，尚未保存。請核對金額與原因後確認。' : '內容未變更，不需保存。';
  } catch (error) { status.textContent = `核定金額預覽失敗：${error.message}`; }
}

async function saveCostGridAdjustment() {
  if (costGrid.adjustmentBusy) return;
  const status = document.getElementById('costGridStatus');
  try {
    const payload = costGridAdjustmentPayload(false);
    if (JSON.stringify({...payload, preview_only: true}) !== costGrid.adjustmentDraft.reviewed) {
      throw new Error('內容已變更，請重新預覽。');
    }
    if (!confirm(`${payload.day} ${payload.label} 核定金額調整：確定保存並留下稽核紀錄嗎？`)) return;
    costGrid.adjustmentBusy = true;
    window.DraftRecovery?.formal('cost-grid','saving');
    document.getElementById('costGridAdjustmentSave').disabled = true;
    const result = await api('/api/cost-hour-imports/grid/amount-adjustment',
      {method: 'POST', body: JSON.stringify({...payload, confirmed: true})});
    await window.DraftRecovery?.close('cost-grid',false);
    window.DraftRecovery?.formal('cost-grid','saved',result.backup_status);
    costGrid.adjustmentDraft = null;
    document.getElementById('costGridAdjustment').hidden = true;
    const savedLabel = costGrid.label;
    await loadCostGrid();
    if (costGrid.data?.sheets.some(sheet => sheet.label === savedLabel)) {
      costGrid.label = savedLabel;
      renderCostGrid();
    }
    status.textContent = result.saved
      ? `${result.day} 核定金額已保存。${result.backup_status === 'saved' ? 'Google Drive 資料庫備份成功。' : '雲端備份未成功，請先重試備份，勿重新部署。'}`
      : '核定金額沒有變更。';
  } catch (error) {
    status.textContent = `保存未確認：${error.message}。請先重新載入核對，不要直接重複送出。`;
    window.DraftRecovery?.formal('cost-grid','failed');
  } finally { costGrid.adjustmentBusy = false; }
}

window.addEventListener('beforeunload', event => {
  if (costGridDirty()) { event.preventDefault(); event.returnValue = ''; }
});
