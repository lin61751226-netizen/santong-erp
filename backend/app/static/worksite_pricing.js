const worksitePricing = {data: null, vehicle: null, edits: new Map(), pending: new Map(), busy: false, request: 0};
const worksitePricingFields = ['units', 'normal_hours', 'overtime_hours', 'holiday_reason', 'external_customer', 'fuel_cost', 'payer'];

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
    status.textContent = `${data.site_name}｜${month}｜未稅計價。每列先核對，再保存到系統資料庫。`;
  } catch (error) { if (request === worksitePricing.request) status.textContent = `讀取失敗：${error.message}`; }
}

function selectWorksitePricingVehicle(index) {
  if (worksitePricingDirty() && !confirm('目前車型有未保存修改，切換會捨棄，確定嗎？')) return;
  worksitePricing.edits.clear();
  worksitePricing.pending.clear();
  worksitePricing.vehicle = worksitePricing.data?.sheets[index]?.vehicle_type;
  renderWorksitePricing();
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
    const text = field => `<input data-day="${row.day}" data-field="${field}" maxlength="200" value="${escapeAttr(values[field] || '')}" oninput="editWorksitePricingCell(this)">`;
    const holiday = `<select data-day="${row.day}" data-field="holiday_reason" onchange="editWorksitePricingCell(this)" aria-label="${row.day} 假日類別">
      ${mandatory ? `<option value="${mandatory}">${mandatory}</option>` : `<option value="" ${!values.holiday_reason ? 'selected' : ''}>週一至六</option><option value="春節" ${values.holiday_reason === '春節' ? 'selected' : ''}>春節</option>`}</select>`;
    return `<tr class="${mandatory || values.holiday_reason ? 'cost-grid-holiday' : ''}"><th scope="row">${escapeHtml(row.day.slice(5))} ${'日一二三四五六'[weekday]}</th>
      <td>${number('units', 100, 1)}</td><td>${number('normal_hours', 8, 0.5)}</td><td>${number('overtime_hours', 24, 0.5)}</td>
      <td>${holiday}</td><td>${money(row.normal_amount)}</td><td>${money(row.overtime_amount)}</td><td><strong>${money(row.total_amount)}</strong></td>
      <td>${text('external_customer')}</td><td>${number('fuel_cost', 1000000000, 0.01)}</td><td>${text('payer')}</td>
      <td><button type="button" class="button-compact" onclick="saveWorksitePricingRow('${row.day}')" ${worksitePricing.edits.has(row.day) ? '' : 'disabled'}>核對並保存</button></td></tr>`;
  }).join('');
  const rate = active.rate;
  document.getElementById('worksitePricingTable').innerHTML = `<div class="cost-grid-caption"><strong>${escapeHtml(active.vehicle_type)}</strong><span>週一至六 時價 ${money(rate.weekday_hourly)}／日價 ${money(rate.weekday_daily)} · 週日／元旦／春節 時價 ${money(rate.holiday_hourly)}／日價 ${money(rate.holiday_daily)} · 加班 ${money(rate.overtime_hourly)}／小時 · ${rate.effective_from} 起</span></div>
    <div class="cost-grid-scroll"><table><thead><tr><th>日期</th><th>台數</th><th>正常<br>時數／台</th><th>加班<br>時數／台</th><th>費率</th><th>正常金額</th><th>加班金額</th><th>合計未稅</th><th>外叫客戶</th><th>油錢</th><th>付款人</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function editWorksitePricingCell(input) {
  const active = worksitePricing.data?.sheets.find(item => item.vehicle_type === worksitePricing.vehicle);
  const row = active?.rows.find(item => item.day === input.dataset.day);
  if (!row) return;
  const values = {...worksitePricingValues(row)};
  values[input.dataset.field] = input.type === 'number' ? (input.value === '' ? 0 : Number(input.value)) : input.value.trim();
  if (worksitePricingFields.every(field => values[field] === row[field])) worksitePricing.edits.delete(row.day);
  else worksitePricing.edits.set(row.day, values);
  worksitePricing.pending.delete(row.day);
  input.closest('tr').querySelector('button').disabled = !worksitePricing.edits.has(row.day);
  document.getElementById('worksitePricingStatus').textContent = `未保存 ${worksitePricing.edits.size} 個日期；金額欄尚未重算，按「核對並保存」先看新金額。`;
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
        overtime_amount: preview.overtime_amount, total_amount: preview.total_amount});
      worksitePricing.edits.delete(day);
      worksitePricing.pending.delete(day);
      renderWorksitePricing();
      status.textContent = `${day} 前一次請求已保存；請再次重新載入核對雲端紀錄。`;
      return;
    }
    if (!preview.changed) { worksitePricing.edits.delete(day); renderWorksitePricing(); status.textContent = '內容沒有變更。'; return; }
    if (!confirm(`${active.vehicle_type} ${day}：正常 ${Number(preview.normal_amount).toLocaleString()} + 加班 ${Number(preview.overtime_amount).toLocaleString()} = 未稅 ${Number(preview.total_amount).toLocaleString()} 元。\n油錢另記，不含稅。確定保存嗎？`)) return;
    const result = await api('/api/worksite-pricing/row', {method: 'POST', body: JSON.stringify({...payload, confirmed: true})});
    Object.assign(row, values, {revision: result.revision, normal_amount: result.normal_amount,
      overtime_amount: result.overtime_amount, total_amount: result.total_amount});
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
