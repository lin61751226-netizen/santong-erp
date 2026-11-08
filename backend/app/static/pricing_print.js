function pricingPrintMoney(value) {
  const amount = Number(value || 0);
  return amount ? amount.toLocaleString('zh-TW', {maximumFractionDigits: 2}) : '';
}

function pricingPrintSum(rows, field) {
  return Math.round(rows.reduce((sum, row) => sum + Number(row[field] || 0), 0) * 100) / 100;
}

function pricingPrintDay(day) {
  const [year, month, date] = day.split('-').map(Number);
  const weekday = new Date(Date.UTC(year, month - 1, date)).getUTCDay();
  return `${month}/${date}（${'日一二三四五六'[weekday]}）`;
}

function pricingPrintHtml(kind, data, sheet) {
  const safe = value => escapeHtml(value ?? '');
  const isLegacy = kind === 'legacy';
  const month = `${data.year} 年 ${data.month} 月`;
  const title = `三通工程行 ${data.year - 1911} 年 ${data.month} 月 ${isLegacy ? sheet.label : data.site_name} 計價表`;
  const rowClass = row => row.is_holiday || new Date(`${row.day}T12:00:00`).getDay() === 0 ? 'holiday' : '';
  let header;
  let rows;
  let summary;
  let notes = '';
  if (isLegacy) {
    header = '<tr><th>日期</th><th>正常<br>時數</th><th>正常<br>金額</th><th>加班<br>時數</th><th>加班<br>金額</th><th>支援<br>時數</th><th>支援<br>金額</th></tr>';
    rows = sheet.rows.map(row => `<tr class="${rowClass(row)}"><td>${pricingPrintDay(row.day)}</td><td>${safe(pricingPrintMoney(row.normal_hours))}</td><td>${safe(pricingPrintMoney(row.normal_amount))}</td><td>${safe(pricingPrintMoney(row.overtime_hours))}</td><td>${safe(pricingPrintMoney(row.overtime_amount))}</td><td>${safe(pricingPrintMoney(row.support_hours))}</td><td>${safe(pricingPrintMoney(row.support_amount))}</td></tr>`).join('');
    const normal = pricingPrintSum(sheet.rows, 'normal_amount');
    const overtime = pricingPrintSum(sheet.rows, 'overtime_amount');
    const support = pricingPrintSum(sheet.rows, 'support_amount');
    const subtotal = Number(sheet.totals?.untaxed ?? Math.round((normal + overtime + support) * 100) / 100);
    const taxRate = Number(data.tax_rate || 0);
    const tax = Number(sheet.totals?.tax ?? Math.round(subtotal * taxRate));
    const taxed = Number(sheet.totals?.taxed ?? subtotal + tax);
    summary = `<tr class="total"><th>合計（未稅）</th><td>${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'normal_hours')))}</td><td>${safe(pricingPrintMoney(normal))}</td><td>${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'overtime_hours')))}</td><td>${safe(pricingPrintMoney(overtime))}</td><td>${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'support_hours')))}</td><td>${safe(pricingPrintMoney(support))}</td></tr>
      <tr class="total"><th colspan="5">未稅計價合計</th><td colspan="2" class="amount">${safe(pricingPrintMoney(subtotal))}</td></tr>
      <tr class="total"><th colspan="5">營業稅試算（${safe((taxRate * 100).toLocaleString('zh-TW'))}%）</th><td colspan="2" class="amount">${safe(pricingPrintMoney(tax))}</td></tr>
      <tr class="total grand"><th colspan="5">含稅計價合計</th><td colspan="2" class="amount">${safe(pricingPrintMoney(taxed))}</td></tr>`;
    notes = '金額依已保存原計價檔公式計算；正式請款仍須核對原計價檔與簽單。油錢另記，不併入請款。';
  } else {
    header = '<tr><th>日期</th><th>台數</th><th>正常工時<br>逐台明細</th><th>加班工時<br>逐台明細</th><th>總台時<br>正／加</th><th>正常金額</th><th>加班金額</th><th>實際未稅</th><th>外叫客戶</th></tr>';
    rows = sheet.rows.map(row => {
      const normal = row.normal_hours_detail || (row.units ? `${row.normal_hours}×${row.units}` : '');
      const overtime = row.overtime_hours_detail || (row.overtime_hours ? `${row.overtime_hours}×${row.units}` : '');
      const hours = row.units ? `${row.normal_total_hours || 0}／${row.overtime_total_hours || 0}` : '';
      return `<tr class="${rowClass(row)}"><td>${pricingPrintDay(row.day)}</td><td>${safe(pricingPrintMoney(row.units))}</td><td class="note">${safe(normal)}</td><td class="note">${safe(overtime)}</td><td>${safe(hours)}</td><td>${safe(pricingPrintMoney(row.normal_amount))}</td><td>${safe(pricingPrintMoney(row.overtime_amount))}</td><td>${safe(pricingPrintMoney(row.total_amount))}</td><td class="note">${safe(row.external_customer)}</td></tr>`;
    }).join('');
    summary = `<tr class="total grand"><th>合計（未稅）</th><td>${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'units')))}</td><td></td><td></td><td>${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'normal_total_hours')))}／${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'overtime_total_hours')))}</td><td>${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'normal_amount')))}</td><td>${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'overtime_amount')))}</td><td>${safe(pricingPrintMoney(pricingPrintSum(sheet.rows, 'total_amount')))}</td><td></td></tr>`;
    const adjustments = sheet.rows.filter(row => row.manual_total_amount !== null && row.manual_total_amount !== undefined);
    notes = `本表未稅，油錢另記。${adjustments.length ? `人工核定：${adjustments.map(row => `${pricingPrintDay(row.day)} ${pricingPrintMoney(row.manual_total_amount)} 元（${row.manual_amount_reason || '未註明'}）`).join('；')}` : '金額依逐台工時與工地費率計算。'}`;
  }
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
    <div class="meta"><span>期間：${safe(month)}</span><span>來源：${safe(isLegacy ? data.document_name : data.site_name)}</span><span>已保存月表</span></div>
    <table><thead>${header}</thead><tbody>${rows}</tbody><tfoot>${summary}</tfoot></table>
    <p class="footnote">${safe(notes)}</p><div class="signatures"><span>製表：</span><span>核對：</span><span>客戶確認：</span></div>
    <p class="stamp">三通工程行｜月計價表｜列印日期：${new Date().toLocaleDateString('zh-TW')}</p></main></body></html>`;
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
