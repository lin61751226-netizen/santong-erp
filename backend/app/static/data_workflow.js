const dataWorkflowSources = {
  pricing: [['pricing', '已上傳堆高機計價檔'], ['sitepricing', '國泰慈文獨立計價']],
  finance: [['workbook', '已上傳 Excel 原表'], ['finance', '已匯入系統收支']],
  payroll: [['workbook', '已上傳 Excel 原表'], ['payroll', '已存全年管理修訂']],
  roster: [['workbook', '已上傳 Excel 原表'], ['roster', '已存全年管理修訂']],
};

function dataWorkflowKindForMode(mode) {
  return mode === 'workbook' ? document.getElementById('companyWorkbookKind').value :
    mode === 'sitepricing' || mode === 'pricing' ? 'pricing' : mode;
}

function syncDataWorkflowFromMode(mode) {
  const kind = dataWorkflowKindForMode(mode);
  const kindSelect = document.getElementById('dataWorkflowKind');
  if (!kindSelect) return;
  kindSelect.value = kind;
  const source = document.getElementById('dataWorkflowSource');
  source.innerHTML = dataWorkflowSources[kind].map(([value, label]) =>
    `<option value="${value}">${escapeHtml(label)}</option>`).join('');
  source.value = mode;
  const monthId = {
    pricing: 'costGridMonth', sitepricing: 'worksitePricingMonth',
    workbook: 'companyWorkbookMonth', finance: 'dataMonth',
    payroll: 'managementGridMonth', roster: 'managementGridMonth',
  }[mode];
  if (document.getElementById(monthId)?.value) {
    document.getElementById('dataWorkflowMonth').value = document.getElementById(monthId).value;
  }
  const hints = {
    pricing: '請在下方選計價檔與標別；國泰慈文使用獨立計價，不寫入舊 Excel。',
    sitepricing: '國泰慈文從 2026/11 起獨立計價；每列核對後保存。',
    workbook: '下方選已上傳檔案和工作表；修改原表資料欄後，預覽並保存為新版本。',
    finance: '下方選來源檔；只顯示已匯入系統的收支，不等於原 Excel 全部內容。',
    payroll: '下方選已存的全年管理版本；修訂留在資料庫，不覆蓋原 Excel。',
    roster: '下方選已存的全年管理版本；排休修訂不是正式請假核准。',
  };
  document.getElementById('dataWorkflowHint').textContent = hints[mode];
}

function initDataWorkflow() {
  const month = todayString().slice(0, 7);
  document.getElementById('dataWorkflowMonth').value = month;
  for (const id of ['companyWorkbookMonth', 'dataMonth', 'managementGridMonth']) {
    if (!document.getElementById(id).value) document.getElementById(id).value = month;
  }
  syncDataWorkflowFromMode(managementGrid.mode);
  renderUploadedDataCards();
}

function changeDataWorkflowKind() {
  const kind = document.getElementById('dataWorkflowKind').value;
  const hasAnnual = state.managedDocuments.some(item => item.category === '通訊錄與年度管理' &&
    item.original_file_name.toLowerCase().endsWith('.xlsx'));
  const source = kind === 'pricing' ? 'pricing' :
    kind === 'finance' ? (document.getElementById('companyWorkbookDocument').value ? 'workbook' : 'finance') :
    hasAnnual ? 'workbook' : kind;
  document.getElementById('dataWorkflowSource').innerHTML = dataWorkflowSources[kind].map(([value, label]) =>
    `<option value="${value}">${escapeHtml(label)}</option>`).join('');
  document.getElementById('dataWorkflowSource').value = source;
  applyDataWorkflow();
}

function changeDataWorkflowSource() { applyDataWorkflow(); }
function changeDataWorkflowMonth() { applyDataWorkflow(); }

function syncDataWorkflowMonthFrom(inputId) {
  const source = document.getElementById(inputId);
  if (source?.value) document.getElementById('dataWorkflowMonth').value = source.value;
}

async function applyDataWorkflow() {
  const kind = document.getElementById('dataWorkflowKind').value;
  const mode = document.getElementById('dataWorkflowSource').value;
  let month = document.getElementById('dataWorkflowMonth').value;
  if (!month || !dataWorkflowSources[kind]?.some(([value]) => value === mode)) return;
  if (mode === 'workbook' && managementGrid.mode === 'workbook' && companyWorkbookDirty() &&
      document.getElementById('companyWorkbookKind').value !== kind &&
      !confirm('目前 Excel 月表有未保存修改，確定切換工作表嗎？')) {
    syncDataWorkflowFromMode(managementGrid.mode);
    return;
  }
  if (showExcelMode(mode, {autoLoad: false}) === false) {
    syncDataWorkflowFromMode(managementGrid.mode);
    return;
  }
  if (mode === 'sitepricing' && month < '2026-11') {
    month = '2026-11';
    document.getElementById('dataWorkflowMonth').value = month;
  }
  if (mode === 'workbook') {
    document.getElementById('companyWorkbookKind').value = kind;
    if (kind === 'payroll' || kind === 'roster') {
      const file = document.getElementById('companyWorkbookDocument');
      const selected = state.managedDocuments.find(item => String(item.id) === file.value);
      if (selected?.category !== '通訊錄與年度管理') {
        const annual = [...state.managedDocuments].sort((a, b) => b.id - a.id).find(item =>
          item.category === '通訊錄與年度管理' && item.original_file_name.toLowerCase().endsWith('.xlsx'));
        if (annual) file.value = String(annual.id);
      }
    }
  }
  const monthInput = {
    pricing: 'costGridMonth', sitepricing: 'worksitePricingMonth',
    workbook: 'companyWorkbookMonth', finance: 'dataMonth',
    payroll: 'managementGridMonth', roster: 'managementGridMonth',
  }[mode];
  document.getElementById(monthInput).value = month;
  syncDataWorkflowFromMode(mode);
  document.getElementById('dataWorkflowMonth').value = month;
  if (mode === 'pricing') await loadCostGrid();
  else if (mode === 'sitepricing') await loadWorksitePricing();
  else if (mode === 'workbook') await loadCompanyWorkbook();
  else if (mode === 'finance') await loadDocumentData(false, 0);
  else await loadManagementGridSources();
  syncDataWorkflowMonthFrom(monthInput);
}

function moveActiveDataTable(direction) {
  const mode = managementGrid.mode;
  const section = document.getElementById({
    pricing: 'costGridSection', sitepricing: 'worksitePricingSection',
    workbook: 'companyWorkbookSection', finance: 'dataEditorSection',
    payroll: 'managementMonthGridSection', roster: 'managementMonthGridSection',
  }[mode]);
  const scroller = section?.querySelector('.cost-grid-scroll, .data-editor-table');
  if (scroller) scroller.scrollLeft += direction * Math.max(260, Math.round(scroller.clientWidth * .7));
}

function renderUploadedDataCards() {
  const cards = document.getElementById('uploadedDataCards');
  if (!cards) return;
  const docs = [...(state.managedDocuments || [])].sort((a, b) => b.id - a.id).slice(0, 12);
  document.getElementById('uploadedDataStatus').textContent = docs.length ?
    `已上傳 ${state.managedDocuments.length} 份；下列顯示最新 ${docs.length} 份。預覽在 APP 內讀取原檔，不會修改內容。` :
    '尚無已上傳 Excel。請先到公司 Excel 文件庫上傳，原檔會保留在 Google Drive。';
  cards.innerHTML = docs.map(item => `<article class="uploaded-data-card">
    <span>${escapeHtml(item.category)}</span><strong title="${escapeAttr(item.original_file_name)}">${escapeHtml(item.original_file_name)}</strong>
    <small>版本 #${item.id} · ${formatTW(item.created_at)}</small>
    <div class="button-row"><button type="button" class="button-secondary button-compact" onclick="openManagedDocument(${item.id})">在 APP 預覽</button>
    <button type="button" class="button-compact" onclick="openUploadedDataForMonth(${item.id})">選月份處理</button></div>
  </article>`).join('');
}

async function openUploadedDataForMonth(id) {
  const item = state.managedDocuments.find(row => row.id === id);
  if (!item) return;
  if (item.category === '推高機計價') {
    if (showExcelMode('pricing', {autoLoad: false}) === false) return;
    document.getElementById('costGridDocument').value = String(id);
    await loadCostGrid();
  } else if (item.original_file_name.toLowerCase().endsWith('.xlsx') &&
             ['通訊錄與年度管理', '收支明細'].includes(item.category)) {
    if (showExcelMode('workbook', {autoLoad: false}) === false) return;
    if (item.category === '收支明細') document.getElementById('companyWorkbookKind').value = 'finance';
    document.getElementById('companyWorkbookDocument').value = String(id);
    syncDataWorkflowFromMode('workbook');
    await loadCompanyWorkbook();
  } else {
    openManagedDocument(id);
    return;
  }
  document.getElementById('panel-excel-editor').scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function loadMonthlyRevenueEstimate() {
  const amount = document.getElementById('monthlyRevenueAmount');
  const period = document.getElementById('monthlyRevenuePeriod');
  const detail = document.getElementById('monthlyRevenueDetail');
  amount.textContent = '核對中…';
  try {
    const data = await api('/api/monthly-revenue-estimate');
    period.textContent = `${data.month}｜截至 ${data.as_of}｜未稅、已登錄計價`;
    amount.textContent = `${data.complete ? 'NT$ ' : '已知小計 NT$ '}${Number(data.known_total).toLocaleString('zh-TW', {maximumFractionDigits: 2})}`;
    detail.textContent = `堆高機計價 ${data.legacy_amount === null ? '待核對' : 'NT$ ' + Number(data.legacy_amount).toLocaleString('zh-TW')}（${data.legacy_document || '無檔案'}）；國泰慈文 NT$ ${Number(data.site_amount).toLocaleString('zh-TW')}。${data.issue ? `未完整：${data.issue}。` : ''}不含未登錄工時、收支收入及稅，不是正式請款金額。`;
  } catch (error) {
    amount.textContent = '目前無法核對';
    detail.textContent = `預估營收讀取失敗：${error.message}；不顯示未核實的零元。`;
  }
}
