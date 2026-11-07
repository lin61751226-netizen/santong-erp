const worksitePricing = {data: null, vehicle: null, edits: new Map(), pending: new Map(), busy: false, request: 0, journalHint: '', journalImport: null};
const worksitePricingFields = ['units', 'normal_hours', 'overtime_hours', 'normal_hours_detail', 'overtime_hours_detail', 'holiday_reason', 'external_customer', 'fuel_cost', 'payer', 'manual_total_amount', 'manual_amount_reason'];

function worksitePricingDirty() { return worksitePricing.edits.size > 0; }

async function loadWorksitePricing() {
  const month = document.getElementById('worksitePricingMonth').value;
  const status = document.getElementById('worksitePricingStatus');
  if (!month) return;
  if (month < '2026-11') { status.textContent = '國泰慈文自115年11月開始計價，之前月份不可登錄。'; return; }
  if (worksitePricingDirty() && !confirm('國泰慈文月表格有未保存修改，重新載入會捨棄，確定嗎？')) return;
  worksitePricing.edits.clear();
  worksitePricing.pending.clear();
  const request = ++worksitePricing.request;
  status.textContent = '正在讀取國泰慈文月表格…';
  try {
    const sites = await api('/api/worksites');
    const site = sites.find(item => item.code === '國泰慈文');
    if (!site) throw new Error('工地管理尚無國泰慈文；請先完成工地資料同步。');
    const params = new URLSearchParams({worksite_id: site.id, year: month.slice(0, 4), month: Number(month.slice(5))});
    const data = await api(`/api/worksite-pricing/month?${params}`);
    if (request !== worksitePricing.request) return;
    const prior = worksitePricing.vehicle;
    worksitePricing.data = data;
    worksitePricing.vehicle = data.sheets.find(item => item.vehicle_type === prior)?.vehicle_type || data.sheets[0]?.vehicle_type;
    renderWorksitePricing();
    renderWorksitePricingJournalImport();
    status.textContent = `${data.site_name}｜${month}｜未稅計價。每列先核對，再保存到系統資料庫。${worksitePricing.journalHint ? ` ${worksitePricing.journalHint}` : ''}`;
  } catch (error) { if (request === worksitePricing.request) status.textContent = `讀取失敗：${error.message}`; }
}

function selectWorksitePricingVehicle(index) {
  if (worksitePricingDirty() && !confirm('目前車型有未保存修改，切換會捨棄，確定嗎？')) return;
  worksitePricing.edits.clear();
  worksitePricing.pending.clear();
  worksitePricing.vehicle = worksitePricing.data?.sheets[index]?.vehicle_type;
  renderWorksitePricing();
}

function renderWorksitePricingJournalImport() {
  const source = worksitePricing.journalImport;
  const host = document.getElementById('worksitePricingJournalImport');
  if (!host) return;
  host.hidden = !source || source.day.slice(0, 7) !== document.getElementById('worksitePricingMonth').value;
  if (host.hidden) return;
  document.getElementById('worksitePricingJournalDate').textContent = `${source.day} 日誌工時（先選目前車型）`;
  document.getElementById('worksitePricingJournalNormal').value = source.normalHours;
  document.getElementById('worksitePricingJournalOvertime').value = source.overtimeHours;
}

function applyJournalToWorksitePricing() {
  const source = worksitePricing.journalImport;
  const active = worksitePricing.data?.sheets.find(item => item.vehicle_type === worksitePricing.vehicle);
  const row = active?.rows.find(item => item.day === source?.day);
  const status = document.getElementById('worksitePricingStatus');
  if (!row) { status.textContent = '這個日期不在目前月份或車型計價表內。'; return; }
  const units = Number(document.getElementById('worksitePricingJournalUnits').value);
  const normalTotal = Number(document.getElementById('worksitePricingJournalNormal').value);
  const overtimeTotal = Number(document.getElementById('worksitePricingJournalOvertime').value);
  const normalDetail = document.getElementById('worksitePricingJournalDetail')?.value.trim().replaceAll('，', ',') || '';
  const overtimeDetail = document.getElementById('worksitePricingJournalOvertimeDetail')?.value.trim().replaceAll('，', ',') || '';
  const parsed = value => value.split(',').map(item => item.trim() === '' ? NaN : Number(item.trim()));
  const normalUnits = normalDetail ? parsed(normalDetail) : [];
  const overtimeUnits = overtimeDetail ? parsed(overtimeDetail) : [];
  const validDetail = (items, limit) => items.length === units && items.every(value =>
    Number.isFinite(value) && value >= 0 && value <= limit && Number.isInteger(value * 2));
  if (!Number.isInteger(units) || units < 1 || units > 100 || !Number.isFinite(normalTotal) || normalTotal < 0 ||
      !Number.isFinite(overtimeTotal) || overtimeTotal < 0 ||
      (overtimeDetail && !normalDetail) ||
      (normalDetail ? !validDetail(normalUnits, 8) || normalUnits.reduce((a, b) => a + b, 0) !== normalTotal :
        normalTotal / units > 8 || !Number.isInteger(normalTotal / units * 2)) ||
      (normalDetail ? (overtimeTotal > 0 && !overtimeDetail) ||
        (overtimeDetail && (!validDetail(overtimeUnits, 24) || overtimeUnits.reduce((a, b) => a + b, 0) !== overtimeTotal)) :
        overtimeTotal / units > 24 || !Number.isInteger(overtimeTotal / units * 2))) {
    status.textContent = '請核對台數、總工時與逐台明細：明細加總須等於日誌工時；每台正常最多 8 小時、加班最多 24 小時，單位為半小時。'; return;
  }
  if ((worksitePricing.edits.has(row.day) || row.revision > 0) &&
      !confirm('此日期已有保存或未保存資料；帶入會覆蓋目前車型的台數、工時及人工金額，其他欄位保留。繼續嗎？')) return;
  const values = {...worksitePricingValues(row), units,
    normal_hours: normalDetail ? 0 : normalTotal / units,
    overtime_hours: normalDetail ? 0 : overtimeTotal / units,
    normal_hours_detail: normalDetail, overtime_hours_detail: normalDetail ? overtimeDetail : '',
    manual_total_amount: null, manual_amount_reason: ''};
  worksitePricing.edits.set(row.day, values);
  worksitePricing.pending.delete(row.day);
  renderWorksitePricing();
  status.textContent = `${row.day} ${active.vehicle_type}：日誌總工時 ${normalTotal}/${overtimeTotal}，${normalDetail ? `逐台 ${normalDetail} 小時` : `平均每台 ${values.normal_hours}/${values.overtime_hours} 小時`}，僅帶入待核對欄位。請核對公式與核定金額，再按該列「核對並保存」。`;
}

function worksitePricingValues(row) {
  return worksitePricing.edits.get(row.day) || Object.fromEntries(worksitePricingFields.map(field => [field, row[field]]));
}

function renderWorksitePricing() {
  const data = worksitePricing.data;
  if (!data) return;
  const active = data.sheets.find(item => item.vehicle_type === worksitePricing.vehicle);
  document.getElementById('worksitePricingTabs').innerHTML = data.sheets.map((sheet, index) =>
    `<button type="button" role="tab" aria-selected="${sheet.vehicle_type === worksitePricing.vehicle}" class="${sheet.vehicle_type === worksitePricing.vehicle ? 'active' : ''}" onclick="selectWorksitePricingVehicle(${index})">${escapeHtml(sheet.vehicle_type)}</button>`).join('');
  if (!active) return;
  const money = value => Number(value || 0).toLocaleString('zh-TW');
  const rows = active.rows.map(row => {
    const values = worksitePricingValues(row);
    const weekday = new Date(`${row.day}T12:00:00`).getDay();
    const mandatory = weekday === 0 ? '週日' : row.day.slice(5) === '01-01' ? '元旦' : '';
    const number = (field, max, step) => `<input type="number" min="0" max="${max}" step="${step}" aria-label="${row.day} ${field}" data-day="${row.day}" data-field="${field}" value="${escapeAttr(values[field] ?? 0)}" oninput="editWorksitePricingCell(this)">`;
    const manualNumber = `<input type="number" min="0" max="1000000000" step="0.01" aria-label="${row.day} 人工核定未稅金額，留空自動計算" data-day="${row.day}" data-field="manual_total_amount" value="${escapeAttr(values.manual_total_amount ?? '')}" placeholder="自動" oninput="editWorksitePricingCell(this)">`;
    const text = field => `<input data-day="${row.day}" data-field="${field}" maxlength="200" value="${escapeAttr(values[field] || '')}" oninput="editWorksitePricingCell(this)">`;
    const detail = (field, placeholder) => `<input data-day="${row.day}" data-field="${field}" maxlength="1000" aria-label="${row.day} ${placeholder}" placeholder="${placeholder}" value="${escapeAttr(values[field] || '')}" oninput="editWorksitePricingCell(this)">`;
    const holiday = `<select data-day="${row.day}" data-field="holiday_reason" onchange="editWorksitePricingCell(this)" aria-label="${row.day} 假日類別">
      ${mandatory ? `<option value="${mandatory}">${mandatory}</option>` : `<option value="" ${!values.holiday_reason ? 'selected' : ''}>週一至六</option><option value="春節" ${values.holiday_reason === '春節' ? 'selected' : ''}>春節</option>`}</select>`;
    return `<tr class="${mandatory || values.holiday_reason ? 'cost-grid-holiday' : ''}"><th scope="row">${escapeHtml(row.day.slice(5))} ${'日一二三四五六'[weekday]}</th>
      <td>${number('units', 100, 1)}</td><td>${number('normal_hours', 8, 0.5)}</td><td>${number('overtime_hours', 24, 0.5)}</td>
      <td>${detail('normal_hours_detail', '例 2,3')}</td><td>${detail('overtime_hours_detail', '例 0,1')}</td><td>${money(row.normal_total_hours ?? row.units * row.normal_hours)}／${money(row.overtime_total_hours ?? row.units * row.overtime_hours)}</td>
      <td>${holiday}</td><td>${money(row.normal_amount)}</td><td>${money(row.overtime_amount)}</td><td>${money(row.calculated_total ?? row.normal_amount + row.overtime_amount)}</td>
      <td>${manualNumber}</td><td><input data-day="${row.day}" data-field="manual_amount_reason" maxlength="200" aria-label="${row.day} 金額調整理由" value="${escapeAttr(values.manual_amount_reason || '')}" placeholder="人工調整時必填" oninput="editWorksitePricingCell(this)"></td><td><strong>${money(row.total_amount)}</strong></td>
      <td>${text('external_customer')}</td><td>${number('fuel_cost', 1000000000, 0.01)}</td><td>${text('payer')}</td>
      <td><button type="button" class="button-compact" onclick="saveWorksitePricingRow('${row.day}')" ${worksitePricing.edits.has(row.day) ? '' : 'disabled'}>核對並保存</button></td></tr>`;
  }).join('');
  const rate = active.rate;
  document.getElementById('worksitePricingTable').innerHTML = `<div class="cost-grid-caption"><strong>${escapeHtml(active.vehicle_type)}</strong><span>週一至六 時價 ${money(rate.weekday_hourly)}／日價 ${money(rate.weekday_daily)} · 週日／元旦／春節 時價 ${money(rate.holiday_hourly)}／日價 ${money(rate.holiday_daily)} · 加班 ${money(rate.overtime_hourly)}／小時 · ${rate.effective_from} 起</span></div>
    <div class="cost-grid-scroll"><table><thead><tr><th>日期</th><th>台數</th><th>統一正常<br>時數／台</th><th>統一加班<br>時數／台</th><th>逐台正常<br>不同時數填</th><th>逐台加班<br>有加班時填</th><th>合計台時<br>正常／加班</th><th>費率</th><th>正常金額</th><th>加班金額</th><th>公式合計</th><th>核定未稅金額<br>留空採公式</th><th>調整理由</th><th>實際合計未稅</th><th>外叫客戶</th><th>油錢</th><th>付款人</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function editWorksitePricingCell(input) {
  const active = worksitePricing.data?.sheets.find(item => item.vehicle_type === worksitePricing.vehicle);
  const row = active?.rows.find(item => item.day === input.dataset.day);
  if (!row) return;
  const values = {...worksitePricingValues(row)};
  values[input.dataset.field] = input.dataset.field === 'manual_total_amount'
    ? (input.value === '' ? null : Number(input.value))
    : input.type === 'number' ? (input.value === '' ? 0 : Number(input.value)) : input.value.trim();
  if (input.dataset.field === 'normal_hours_detail' && values.normal_hours_detail) {
    values.normal_hours = 0;
    values.overtime_hours = 0;
    input.closest('tr').querySelector('[data-field="normal_hours"]').value = 0;
    input.closest('tr').querySelector('[data-field="overtime_hours"]').value = 0;
  }
  if (input.dataset.field === 'normal_hours_detail' && !values.normal_hours_detail) {
    values.overtime_hours_detail = '';
    input.closest('tr').querySelector('[data-field="overtime_hours_detail"]').value = '';
  }
  if (['normal_hours', 'overtime_hours'].includes(input.dataset.field) && values[input.dataset.field] > 0 && values.normal_hours_detail) {
    values.normal_hours_detail = '';
    values.overtime_hours_detail = '';
    input.closest('tr').querySelector('[data-field="normal_hours_detail"]').value = '';
    input.closest('tr').querySelector('[data-field="overtime_hours_detail"]').value = '';
  }
  if (worksitePricingFields.every(field => values[field] === row[field])) worksitePricing.edits.delete(row.day);
  else worksitePricing.edits.set(row.day, values);
  worksitePricing.pending.delete(row.day);
  input.closest('tr').querySelector('button').disabled = !worksitePricing.edits.has(row.day);
  document.getElementById('worksitePricingStatus').textContent = `未保存 ${worksitePricing.edits.size} 個日期；修改工時後金額須按「核對並保存」預覽，人工核定金額須填理由。`;
}

async function saveWorksitePricingRow(day) {
  if (worksitePricing.busy || !worksitePricing.edits.has(day)) return;
  const active = worksitePricing.data.sheets.find(item => item.vehicle_type === worksitePricing.vehicle);
  const row = active.rows.find(item => item.day === day);
  const values = worksitePricing.edits.get(day);
  const invalid = document.querySelector('#worksitePricingTable input:invalid');
  if (invalid) { invalid.focus(); document.getElementById('worksitePricingStatus').textContent = '有無效台數、時數或油錢，請先修正。'; return; }
  const signature = JSON.stringify(values);
  if (worksitePricing.pending.get(day)?.signature !== signature) worksitePricing.pending.set(day, {signature, requestId: crypto.randomUUID()});
  const payload = {worksite_id: worksitePricing.data.site_id, work_date: day, vehicle_type: active.vehicle_type,
    ...values, expected_revision: row.revision, request_id: worksitePricing.pending.get(day).requestId};
  const status = document.getElementById('worksitePricingStatus');
  worksitePricing.busy = true;
  try {
    const preview = await api('/api/worksite-pricing/row', {method: 'POST', body: JSON.stringify({...payload, preview_only: true})});
    if (preview.replayed) {
      Object.assign(row, values, {revision: preview.revision, normal_amount: preview.normal_amount,
        overtime_amount: preview.overtime_amount, calculated_total: preview.calculated_total, total_amount: preview.total_amount});
      worksitePricing.edits.delete(day);
      worksitePricing.pending.delete(day);
      renderWorksitePricing();
      status.textContent = `${day} 前一次請求已保存；請再次重新載入核對雲端紀錄。`;
      return;
    }
    if (!preview.changed) { worksitePricing.edits.delete(day); renderWorksitePricing(); status.textContent = '內容沒有變更。'; return; }
    if (!confirm(`${active.vehicle_type} ${day}：${values.normal_hours_detail ? `逐台正常工時 ${values.normal_hours_detail} 小時${values.overtime_hours_detail ? `、逐台加班 ${values.overtime_hours_detail} 小時` : ''}` : `${values.units} 台各 ${values.normal_hours} 小時`}。\n正常 ${Number(preview.normal_amount).toLocaleString()} + 加班 ${Number(preview.overtime_amount).toLocaleString()} = 公式 ${Number(preview.calculated_total).toLocaleString()} 元。\n核定未稅 ${Number(preview.total_amount).toLocaleString()} 元${values.manual_total_amount === null ? '' : `；理由：${values.manual_amount_reason}`}。\n油錢另記，不含稅。確定保存嗎？`)) return;
    const result = await api('/api/worksite-pricing/row', {method: 'POST', body: JSON.stringify({...payload, confirmed: true})});
    Object.assign(row, values, {revision: result.revision, normal_amount: result.normal_amount,
      overtime_amount: result.overtime_amount, calculated_total: result.calculated_total, total_amount: result.total_amount});
    worksitePricing.edits.delete(day);
    worksitePricing.pending.delete(day);
    renderWorksitePricing();
    status.textContent = `${day} 已保存未稅計價。${result.backup?.status === 'saved' ? 'Google Drive 資料庫備份成功。' : '雲端備份未成功；重試前請勿重新部署。'}`;
  } catch (error) { status.textContent = `保存未確認：${error.message}；請先重新載入核對，勿直接重複送出。`; }
  finally { worksitePricing.busy = false; }
}

window.addEventListener('beforeunload', event => {
  if (worksitePricingDirty()) { event.preventDefault(); event.returnValue = ''; }
});
