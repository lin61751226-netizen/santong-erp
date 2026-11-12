function pricingPrintMoney(value) {
  const amount = Number(value || 0);
  return amount ? amount.toLocaleString('zh-TW', {maximumFractionDigits: 2}) : '';
}

function pricingPrintAmount(value) {
  return Number(value || 0).toLocaleString('zh-TW', {maximumFractionDigits: 2});
}

function pricingPrintHours(value) {
  return pricingPrintAmount(value);
}

function pricingPrintSum(rows, field) {
  return Math.round(rows.reduce((sum, row) => sum + Number(row[field] || 0), 0) * 100) / 100;
}

function pricingPrintRound(value) {
  return Math.round(Number(value || 0) * 100) / 100;
}

function pricingPrintWeekday(day) {
  const [year, month, date] = String(day).split('-').map(Number);
  return new Date(Date.UTC(year, month - 1, date)).getUTCDay();
}

function pricingPrintDay(day) {
  const weekday = pricingPrintWeekday(day);
  const [, month, date] = String(day).split('-').map(Number);
  return `${month}/${date}（${'日一二三四五六'[weekday]}）`;
}

function pricingPrintBillingDate(day) {
  const weekday = pricingPrintWeekday(day);
  const [, month, date] = String(day).split('-').map(Number);
  return `${month}月${date}日(週${'日一二三四五六'[weekday]})`;
}

function pricingPrintNonZero(value) {
  return Math.abs(Number(value || 0)) > 0.0001;
}

// 核定額是當日未稅合計，直接改寫金額欄，不寫進紙本備註。
// 只有一個金額欄有公式金額時，整筆核定額放進該欄；
// 正常與加班（或支援）同時有金額、無法拆開時，差額併入正常金額。
// 公式已變的舊核定不套用，避免印出過期金額。
function pricingPrintApprovedSplit(row) {
  const normal = Number(row.normal_amount || 0);
  const overtime = Number(row.overtime_amount || 0);
  const support = Number(row.support_amount || 0);
  const formula = pricingPrintRound(row.total ?? normal + overtime + support);
  const split = {normal, overtime, support};
  if (row.adjustment_stale) return split;
  if (row.manual_total_amount == null || row.manual_total_amount === '') return split;
  const approved = pricingPrintRound(row.effective_total ?? row.manual_total_amount);
  const delta = pricingPrintRound(approved - formula);
  if (Math.abs(delta) <= 0.005) return split;
  const buckets = [['normal', normal], ['overtime', overtime], ['support', support]]
    .filter(([, value]) => pricingPrintNonZero(value));
  if (buckets.length === 1) split[buckets[0][0]] = approved;
  else split.normal = pricingPrintRound(normal + delta);
  return split;
}

function pricingPrintLegacyHtml(data, sheet) {
  const safe = value => escapeHtml(value ?? '');
  const rocYear = Number(data.year) - 1911;
  const month = Number(data.month);
  const title = `${sheet.label}工地 × 三通工程行 ${rocYear}年${month}月 請款計價表`;
  const splits = sheet.rows.map(pricingPrintApprovedSplit);
  const showSupport = sheet.rows.some((row, index) => (
    pricingPrintNonZero(row.support_hours) || pricingPrintNonZero(splits[index].support)
  ));
  const columns = showSupport
    ? [['date', '18%'], ['hours', '9%'], ['money', '12%'], ['hours', '9%'], ['money', '12%'], ['hours', '9%'], ['money', '12%'], ['note', '19%']]
    : [['date', '20%'], ['hours', '13%'], ['money', '16%'], ['hours', '13%'], ['money', '16%'], ['note', '22%']];
  const headers = ['日期', '正常<br>時數', '正常<br>金額', '加班<br>時數', '加班<br>金額'];
  if (showSupport) headers.push('支援<br>時數', '支援<br>金額');
  headers.push('備註');
  const rows = sheet.rows.map((row, index) => {
    const off = pricingPrintWeekday(row.day) === 0 || pricingPrintWeekday(row.day) === 6 || Boolean(row.is_holiday);
    const split = splits[index];
    const cells = [
      ['date', pricingPrintBillingDate(row.day)],
      ['hours', pricingPrintHours(row.normal_hours)],
      ['money', pricingPrintMoney(split.normal)],
      ['hours', pricingPrintHours(row.overtime_hours)],
      ['money', pricingPrintMoney(split.overtime)],
    ];
    if (showSupport) {
      cells.push(['hours', pricingPrintHours(row.support_hours)], ['money', pricingPrintMoney(split.support)]);
    }
    cells.push(['note', '']);
    return `<tr${off ? ' class="off"' : ''}>${cells.map(([kind, value]) => `<td class="${kind}">${safe(value)}</td>`).join('')}</tr>`;
  }).join('');
  const subtotalCells = [
    ['label', '小計'],
    ['hours', pricingPrintHours(pricingPrintSum(sheet.rows, 'normal_hours'))],
    ['money', pricingPrintMoney(pricingPrintSum(splits, 'normal'))],
    ['hours', pricingPrintHours(pricingPrintSum(sheet.rows, 'overtime_hours'))],
    ['money', pricingPrintMoney(pricingPrintSum(splits, 'overtime'))],
  ];
  if (showSupport) {
    subtotalCells.push(
      ['hours', pricingPrintHours(pricingPrintSum(sheet.rows, 'support_hours'))],
      ['money', pricingPrintMoney(pricingPrintSum(splits, 'support'))],
    );
  }
  subtotalCells.push(['note', '']);
  const columnUntaxed = pricingPrintRound(splits.reduce((sum, item) => sum + item.normal + item.overtime + item.support, 0));
  const untaxed = sheet.totals?.untaxed != null ? Number(sheet.totals.untaxed) : columnUntaxed;
  const taxed = sheet.totals?.taxed != null
    ? Number(sheet.totals.taxed)
    : untaxed + Math.round(untaxed * Number(data.tax_rate || 0));
  const tail = '<td></td>'.repeat(columns.length - 3);
  const summary = `<tr class="subtotal">${subtotalCells.map(([kind, value]) => `<td class="${kind}">${safe(value)}</td>`).join('')}</tr>
    <tr class="grand"><td class="label" colspan="2">總計（未稅）</td><td class="boxed untaxed">${safe(pricingPrintAmount(untaxed))}</td>${tail}</tr>
    <tr class="grand"><td class="label" colspan="2">含稅請款金額 NT$</td><td class="boxed taxed">${safe(pricingPrintAmount(taxed))}</td>${tail}</tr>`;
  return `<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>${safe(title)}</title><style>
    @page { size: A4 portrait; margin: 9mm 11mm; }
    * { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    html, body { margin: 0; color: #000; font-family: "Microsoft JhengHei", "PingFang TC", "Noto Sans TC", "Noto Sans CJK TC", "WenQuanYi Micro Hei", sans-serif; }
    body { background: #e9edf1; }
    .toolbar { position: sticky; top: 0; background: #1f4e79; color: #fff; padding: 10px 16px; text-align: center; font-size: 14px; }
    .toolbar button { margin-left: 12px; padding: 8px 16px; border: 0; border-radius: 6px; background: #fff; color: #1f4e79; font: inherit; cursor: pointer; }
    .page { width: 188mm; margin: 12px auto; padding: 8mm 0 6mm; background: #fff; box-shadow: 0 8px 30px #9aa8b6; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 10pt; line-height: 1.2; }
    th, td { border: 1px solid #c8c8c8; padding: 0.45mm 1.1mm; vertical-align: middle; }
    tr.title th { background: #1f4e79; color: #fff; border-color: #1f4e79; font-size: 13pt; font-weight: 700; letter-spacing: 0.04em; text-align: center; padding: 2.4mm 2mm; }
    thead tr:last-child th { background: #2e75b6; color: #fff; border-color: #2e75b6; font-weight: 700; text-align: center; font-size: 9.5pt; padding: 1mm 0.4mm; }
    td.date { background: #d6e4f0; color: #1f4e79; text-align: left; padding-left: 1.4mm; }
    td.hours { background: #fff; color: #000; text-align: center; }
    td.money { background: #ebf5fb; color: #000; text-align: right; padding-right: 1.5mm; }
    td.note { background: #fff; color: #000; text-align: left; font-size: 8pt; }
    tbody tr.off td { background: #fff0f0; color: #c00000; }
    tr.subtotal td { background: #fff2cc; color: #1f4e79; font-weight: 700; }
    tr.subtotal td.label, tr.grand td.label { text-align: center; }
    tr.grand td { background: #fce4d6; color: #c00000; font-weight: 700; height: 8.2mm; border-color: #e4c8b8; }
    tr.grand td.label { font-size: 11pt; }
    tr.grand td.boxed { border: 2px solid #222; background: #fce4d6; color: #c00000; text-align: right; font-weight: 700; padding-right: 1.6mm; }
    tr.grand td.boxed.untaxed { font-size: 13pt; }
    tr.grand td.boxed.taxed { font-size: 15pt; }
    .billing-sign { display: flex; justify-content: space-between; gap: 8mm; margin: 4mm 1mm 0; font-size: 9pt; color: #000; }
    .screen-only { margin: 0; padding: 8px 16px; background: #fff4e5; color: #7a3e00; text-align: center; font-size: 13px; }
    @media print {
      html, body { background: #fff; }
      .toolbar, .screen-only { display: none; }
      .page { width: auto; margin: 0; padding: 0; box-shadow: none; }
      tr { break-inside: avoid; }
      .billing-sign { break-inside: avoid; }
    }
  </style></head><body><div class="toolbar">A4 直式列印預覽 · 請先核對內容，再選 A4／實際大小 100%<button id="pricingPrintButton" type="button">列印 A4</button></div>
    ${sheet.rows.some(row => row.adjustment_stale) ? `<p class="screen-only">舊核定未印上表：${sheet.rows.filter(row => row.adjustment_stale).map(row => safe(pricingPrintBillingDate(row.day))).join('、')}。公式已變，紙本暫採公式金額。</p>` : ''}
    <main class="page"><table class="billing"><colgroup>${columns.map(([kind, width]) => `<col class="${kind}" style="width:${width}">`).join('')}</colgroup>
      <thead><tr class="title"><th colspan="${columns.length}">${safe(title)}</th></tr><tr>${headers.map(label => `<th>${label}</th>`).join('')}</tr></thead>
      <tbody>${rows}</tbody><tfoot>${summary}</tfoot></table>
      <p class="billing-sign"><span>請款日期：${rocYear}年${month}月</span><span>承辦人：____________</span><span>審核：____________</span></p>
    </main></body></html>`;
}

function pricingPrintWorksiteHtml(data, sheet) {
  const safe = value => escapeHtml(value ?? '');
  const month = `${data.year} 年 ${data.month} 月`;
  const title = `三通工程行 ${data.year - 1911} 年 ${data.month} 月 ${data.site_name} 計價表`;
  const rowClass = row => row.is_holiday || new Date(`${row.day}T12:00:00`).getDay() === 0 ? 'holiday' : '';
  const header = '<tr><th>日期</th><th>台數</th><th>正常工時<br>逐台明細</th><th>加班工時<br>逐台明細</th><th>總台時<br>正／加</th><th>正常金額</th><th>加班金額</th><th>實際未稅</th><th>外叫客戶</th></tr>';
  const rows = sheet.rows.map(row => {
    const normal = row.normal_hours_detail || (row.units ? `${row.normal_hours}×${row.units}` : '');
    const overtime = row.overtime_hours_detail || (row.overtime_hours ? `${row.overtime_hours}×${row.units}` : '');
    const hours = row.units ? `${row.normal_total_hours || 0}／${row.overtime_total_hours || 0}` : '';
    return `<tr class="${rowClass(row)}"><td>${pricingPrintDay(row.day)}</td><td>${safe(pricingPrintMoney(row.units))}</td><td class="note">${safe(normal)}</td><td class="note">${safe(overtime)}</td><td>${safe(hours)}</td><td>${safe(pricingPrintMoney(row.normal_amount))}</td><td>${safe(pricingPrintMoney(row.overtime_amount))}</td><td>${safe(pricingPrintMoney(row.total_amount))}</td><td class="note">${safe(row.external_customer)}</td></tr>`;
  }).join('');
  const summary = `<tr class="total grand"><th>合計（未稅）</th><td>${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'units')))}</td><td></td><td></td><td>${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'normal_total_hours')))}／${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'overtime_total_hours')))}</td><td>${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'normal_amount')))}</td><td>${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'overtime_amount')))}</td><td>${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'total_amount')))}</td><td></td></tr>`;
  const adjustments = sheet.rows.filter(row => row.manual_total_amount !== null && row.manual_total_amount !== undefined);
  const notes = `本表未稅，油錢另記。${adjustments.length ? `人工核定：${adjustments.map(row => `${pricingPrintDay(row.day)} ${pricingPrintMoney(row.manual_total_amount)} 元（${row.manual_amount_reason || '未註明'}）`).join('；')}` : '金額依逐台工時與工地費率計算。'}`;
  return `<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>${safe(title)}</title><style>
    @page{size:A4 portrait;margin:10mm}
    *{box-sizing:border-box}html,body{margin:0;color:#17334a;font-family:"Microsoft JhengHei","Noto Sans TC",sans-serif}
    body{background:#e9edf1}.toolbar{position:sticky;top:0;background:#173e5d;color:white;padding:10px 16px;text-align:center;font-size:14px}
    .toolbar button{margin-left:12px;padding:8px 16px;border:0;border-radius:6px;background:#fff;color:#173e5d;font:inherit;cursor:pointer}
    .page{width:210mm;min-height:297mm;margin:18px auto;padding:10mm;background:white;box-shadow:0 8px 30px #9aa8b6}
    h1{text-align:center;font-size:16pt;letter-spacing:.08em;margin:0 0 4mm}h2{text-align:center;font-size:10pt;margin:0 0 5mm;font-weight:500}
    .meta{display:flex;justify-content:space-between;gap:10px;margin-bottom:3mm;font-size:8.5pt}
    table{width:100%;border-collapse:collapse;table-layout:fixed;font-size:8.5pt;line-height:1.25}
    th,td{border:1px solid #7c9bb3;padding:.65mm .7mm;text-align:center;vertical-align:middle;overflow-wrap:anywhere}
    thead th{background:#17618a;color:white;font-weight:700}tbody tr{height:5.4mm}tbody tr:nth-child(even){background:#f2f7fa}
    tbody tr.holiday{color:#ad2f31;background:#fff2ef}.note{text-align:left;font-size:8pt}.total{background:#e4f0f5;font-weight:700}.grand{background:#f8e9d5;font-size:10pt}
    .amount{text-align:right;padding-right:3mm}.footnote{font-size:8pt;line-height:1.5;margin:4mm 0 6mm}
    .signatures{display:flex;gap:12mm;font-size:9pt;margin-top:5mm}.signatures span{flex:1;border-bottom:1px solid #677d8e;padding-bottom:5mm}
    .stamp{font-size:7.5pt;color:#667b8b;margin-top:3mm}
    @media print{html,body{background:white}.toolbar{display:none}.page{width:auto;min-height:0;margin:0;padding:0;box-shadow:none}thead{display:table-header-group}tr{break-inside:avoid}.signatures{break-inside:avoid}}
  </style></head><body><div class="toolbar">A4 直式列印預覽 · 請先核對內容，再選 A4／實際大小 100%<button id="pricingPrintButton" type="button">列印 A4</button></div>
    <main class="page"><h1>${safe(title)}</h1><h2>${safe(sheet.worksheet_name || sheet.vehicle_type || '')}</h2>
    <div class="meta"><span>期間：${safe(month)}</span><span>來源：${safe(data.site_name)}</span><span>已保存月表</span></div>
    <table><thead>${header}</thead><tbody>${rows}</tbody><tfoot>${summary}</tfoot></table>
    <p class="footnote">${safe(notes)}</p><div class="signatures"><span>製表：</span><span>核對：</span><span>客戶確認：</span></div>
    <p class="stamp">三通工程行｜月計價表｜列印日期：${new Date().toLocaleDateString('zh-TW')}</p></main></body></html>`;
}

function pricingPrintHtml(kind, data, sheet) {
  return kind === 'legacy' ? pricingPrintLegacyHtml(data, sheet) : pricingPrintWorksiteHtml(data, sheet);
}

function openPricingPrint(kind) {
  const legacy = kind === 'legacy';
  const source = legacy ? costGrid : worksitePricing;
  const status = document.getElementById(legacy ? 'costGridStatus' : 'worksitePricingStatus');
  const data = source.data;
  const sheet = data?.sheets.find(item => legacy ? item.label === source.label : item.vehicle_type === source.vehicle);
  if (!sheet) { status.textContent = '請先載入月份並選擇計價標別或車型。'; return; }
  if (legacy ? costGridDirty() : worksitePricingDirty()) {
    status.textContent = '有未保存的修改；請先保存或重新載入後再列印，避免紙本與正式資料不一致。'; return;
  }
  const popup = window.open('', '_blank');
  if (!popup) { status.textContent = '瀏覽器阻擋新分頁；請允許本站彈出視窗後重試。'; return; }
  popup.document.open();
  popup.document.write(pricingPrintHtml(kind, data, sheet));
  popup.document.close();
  popup.document.getElementById('pricingPrintButton').addEventListener('click', () => popup.print());
  status.textContent = '已開啟 A4 計價表預覽；未送印。請在預覽頁按「列印 A4」並核對紙張與 100% 比例。';
}
