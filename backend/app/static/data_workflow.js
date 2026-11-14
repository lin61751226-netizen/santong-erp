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
  renderUploadedDataCards();
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
  renderUploadedDataCards();
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

const dataWorkflowKindLabels = {pricing: '計價', finance: '收支明細', payroll: '薪資', roster: '排休'};
let dataWorkflowOpenId = null;

function dataWorkflowDocumentId(item) {
  const id = Number(item?.id);
  return Number.isInteger(id) && id > 0 ? id : 0;
}

function dataWorkflowIsExport(item) {
  const name = String(item?.original_file_name || '');
  return item?.category === '線上資料匯出' || name.includes('線上資料匯出');
}

function dataWorkflowExportToken(item) {
  return (String(item?.original_file_name || '').match(/線上資料匯出_([A-Za-z0-9]+)/) || [])[1] || '';
}

function dataWorkflowNormalizeStem(name) {
  let stem = String(name || '').replace(/\.[^.]+$/i, '').trim();
  let previous = '';
  while (stem && stem !== previous) {
    previous = stem;
    stem = stem
      .replace(/_線上編輯_\d{8}_\d{6}_\d+$/i, '')
      .replace(/_\d{8}_\d{6}(?:_\d+)?$/, '')
      .replace(/[ _]*[（(]\d+[)）]$/, '')
      .replace(/_(?:最終修正版|修正版|修訂版|最新版)$/, '')
      .replace(/(計價)\d{4}$/, '$1');
  }
  return stem || String(name || '');
}

function dataWorkflowFamilyLabel(name) {
  const stem = dataWorkflowNormalizeStem(name);
  const parts = stem.split('_').filter(Boolean);
  if (parts.length > 1) return parts[parts.length - 1];
  const mark = stem.match(/(?:推|堆)高機計價|收支明細|全年管理|薪資|排休/);
  return mark ? mark[0] : stem;
}

function dataWorkflowDocumentMatchesKind(item, kind) {
  if (!item || dataWorkflowIsExport(item)) return false;
  const name = String(item.original_file_name || '');
  const category = String(item.category || '');
  if (kind === 'pricing') return category === '推高機計價' || (/計價/.test(name) && /[推堆]高機/.test(name));
  if (kind === 'finance') {
    return category === '收支明細'
      || name.startsWith('三通公司通訊錄_收支明細表_堆高機')
      || name.startsWith('三通公司收支明細表_六和')
      || (category === '通訊錄與年度管理' && name.includes('收支明細'));
  }
  if (kind === 'payroll' || kind === 'roster') {
    return category === '通訊錄與年度管理' && /\.xlsx$/i.test(name);
  }
  return false;
}

function dataWorkflowExportMatchesKind(item, kind) {
  if (!dataWorkflowIsExport(item)) return false;
  const name = String(item.original_file_name || '');
  const token = dataWorkflowExportToken(item).toLowerCase();
  if (token) {
    if (kind === 'finance') return token === 'finance' || token === 'contacts';
    if (kind === 'payroll') return token === 'payroll';
    if (kind === 'roster') return token === 'roster';
    if (kind === 'pricing') return token === 'pricing' || token === 'cost';
    return false;
  }
  if (kind === 'finance') return /收支|通訊錄/.test(name);
  if (kind === 'payroll') return /薪資/.test(name);
  if (kind === 'roster') return /排休/.test(name);
  return false;
}

function dataWorkflowPartition(documents, kind) {
  const current = [];
  const history = [];
  const exports = [];
  const families = new Map();
  for (const item of documents || []) {
    if (dataWorkflowIsExport(item)) {
      if (dataWorkflowExportMatchesKind(item, kind)) exports.push(item);
      continue;
    }
    if (!dataWorkflowDocumentMatchesKind(item, kind)) continue;
    const key = dataWorkflowNormalizeStem(item.original_file_name);
    if (!families.has(key)) families.set(key, []);
    families.get(key).push(item);
  }
  for (const items of families.values()) {
    const sorted = [...items].sort((a, b) => dataWorkflowDocumentId(b) - dataWorkflowDocumentId(a));
    current.push(sorted[0]);
    history.push(...sorted.slice(1));
  }
  current.sort((a, b) => dataWorkflowDocumentId(b) - dataWorkflowDocumentId(a));
  history.sort((a, b) => dataWorkflowDocumentId(b) - dataWorkflowDocumentId(a));
  exports.sort((a, b) => dataWorkflowDocumentId(b) - dataWorkflowDocumentId(a));
  return {current, history, exports};
}

function dataWorkflowHistoryNote(items) {
  const groups = [];
  for (const item of [...items].sort((a, b) => dataWorkflowDocumentId(a) - dataWorkflowDocumentId(b))) {
    const label = dataWorkflowFamilyLabel(item.original_file_name);
    let group = groups.find(row => row.label === label);
    if (!group) {
      group = {label, ids: []};
      groups.push(group);
    }
    group.ids.push(dataWorkflowDocumentId(item));
  }
  return groups.map(group => `${group.ids.map(id => `#${id}`).join('、')} ${group.label}`).join('，');
}

function dataWorkflowExportNote(items) {
  const tokens = [];
  for (const item of items) {
    const token = dataWorkflowExportToken(item);
    if (token && !tokens.includes(token)) tokens.push(token);
  }
  return tokens.length ? `線上資料匯出 ${tokens.join('／')}，不列入主清單` : '不列入主清單';
}

function dataWorkflowActiveDocumentId() {
  const kind = document.getElementById('dataWorkflowKind')?.value;
  const mode = document.getElementById('dataWorkflowSource')?.value;
  if (mode === 'sitepricing' || mode === 'finance' || mode === 'payroll' || mode === 'roster') return 0;
  const sourceId = kind === 'pricing' || mode === 'pricing'
    ? document.getElementById('costGridDocument')?.value
    : document.getElementById('companyWorkbookDocument')?.value;
  const id = Number(sourceId);
  return Number.isInteger(id) && id > 0 ? id : 0;
}

function dataWorkflowText(id, text) {
  const node = document.getElementById(id);
  if (node) node.textContent = text;
}

function dataWorkflowCard(item, {latest = false, active = false} = {}) {
  const id = dataWorkflowDocumentId(item);
  const opened = dataWorkflowOpenId === id;
  const actions = opened
    ? `<button type="button" class="button-secondary button-compact" onclick="openManagedDocument(${id})">預覽</button>
       <button type="button" class="button-compact" onclick="openUploadedDataForMonth(${id})">處理這個月</button>`
    : `<button type="button" class="button-secondary button-compact" onclick="openUploadedDataCard(${id})">開啟</button>`;
  const badge = latest
    ? `<em class="uploaded-data-badge">最新 #${id}</em>`
    : `<em class="uploaded-data-badge is-old">#${id}</em>`;
  return `<article class="uploaded-data-card${active ? ' is-current' : ''}" data-document-id="${id}">
    <span class="uploaded-data-mark" aria-hidden="true">X</span>
    <div class="uploaded-data-name"><strong title="${escapeAttr(item.original_file_name)}">${escapeHtml(item.original_file_name)}</strong>${badge}</div>
    <small class="uploaded-data-meta">${escapeHtml(formatTW(item.created_at))} 上傳${active ? ' · 目前使用中' : ''}</small>
    <div class="uploaded-data-actions">${actions}</div>
  </article>`;
}

function openUploadedDataCard(id) {
  const next = Number(id);
  dataWorkflowOpenId = dataWorkflowOpenId === next ? null : next;
  renderUploadedDataCards();
}

function renderUploadedDataCards() {
  const cards = document.getElementById('uploadedDataCards');
  if (!cards) return;
  const kind = document.getElementById('dataWorkflowKind')?.value || 'pricing';
  const label = dataWorkflowKindLabels[kind] || '資料';
  const {current, history, exports} = dataWorkflowPartition(state.managedDocuments, kind);
  const activeId = dataWorkflowActiveDocumentId();
  dataWorkflowText('uploadedDataHeading', `${label}的檔案`);
  dataWorkflowText('uploadedDataScope', `只列${label}相關檔案，每份只顯示最新版。`);
  const status = document.getElementById('uploadedDataStatus');
  if (status) {
    status.textContent = current.length
      ? `最新版 ${current.length} 份${history.length ? `，較舊版本 ${history.length} 份收在下方` : ''}。預覽在 APP 內讀取原檔，不會修改內容。`
      : `此類型尚無已上傳的 Excel。請展開「上傳新檔案」，或到公司 Excel 文件庫上傳；原檔會保留在 Google Drive。`;
  }
  cards.innerHTML = current.map(item => dataWorkflowCard(item, {
    latest: true, active: dataWorkflowDocumentId(item) === activeId,
  })).join('');
  const historyNode = document.getElementById('uploadedDataHistory');
  const exportNode = document.getElementById('uploadedDataExports');
  if (historyNode) historyNode.hidden = history.length === 0;
  if (exportNode) exportNode.hidden = exports.length === 0;
  dataWorkflowText('uploadedDataHistorySummary', `歷史版本（${history.length} 份）`);
  dataWorkflowText('uploadedDataHistoryNote', dataWorkflowHistoryNote(history));
  dataWorkflowText('uploadedDataExportsSummary', `系統匯出紀錄（${exports.length} 份）`);
  dataWorkflowText('uploadedDataExportsNote', dataWorkflowExportNote(exports));
  const historyBody = document.getElementById('uploadedDataHistoryBody');
  const exportBody = document.getElementById('uploadedDataExportsBody');
  if (historyBody) {
    historyBody.innerHTML = history.map(item => dataWorkflowCard(item, {
      active: dataWorkflowDocumentId(item) === activeId,
    })).join('');
  }
  if (exportBody) exportBody.innerHTML = exports.map(item => dataWorkflowCard(item)).join('');
}

async function openUploadedDataForMonth(id) {
  const item = state.managedDocuments.find(row => row.id === id);
  if (!item) return;
  const kind = document.getElementById('dataWorkflowKind')?.value;
  const month = document.getElementById('dataWorkflowMonth')?.value;
  if (item.category === '推高機計價') {
    if (showExcelMode('pricing', {autoLoad: false}) === false) return;
    document.getElementById('costGridDocument').value = String(id);
    if (month) document.getElementById('costGridMonth').value = month;
    await loadCostGrid();
  } else if (item.original_file_name.toLowerCase().endsWith('.xlsx') &&
             ['通訊錄與年度管理', '收支明細'].includes(item.category)) {
    if (showExcelMode('workbook', {autoLoad: false}) === false) return;
    const workbookKind = kind === 'payroll' || kind === 'roster' || kind === 'finance' ? kind :
      (item.category === '收支明細' ? 'finance' : document.getElementById('companyWorkbookKind').value);
    document.getElementById('companyWorkbookKind').value = workbookKind;
    document.getElementById('companyWorkbookDocument').value = String(id);
    if (month) document.getElementById('companyWorkbookMonth').value = month;
    syncDataWorkflowFromMode('workbook');
    await loadCompanyWorkbook();
  } else {
    openManagedDocument(id);
    return;
  }
  document.getElementById('panel-excel-editor')?.scrollIntoView?.({behavior: 'smooth', block: 'start'});
  renderUploadedDataCards();
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
