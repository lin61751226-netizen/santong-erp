const $ = (id) => document.getElementById(id);
const FIELD_ORDER = [
  'buyer', 'tax_id', 'address', 'year', 'month', 'day',
  'description', 'quantity', 'unit_price', 'amount',
  'subtotal', 'tax', 'total', 'uppercase', 'note',
];
const FIELD_LABELS = {
  buyer: '買受人', tax_id: '統一編號', address: '買受人地址',
  year: '年（民國）', month: '月', day: '日',
  description: '品名', quantity: '數量', unit_price: '單價', amount: '金額',
  subtotal: '銷售額合計', tax: '營業稅', total: '總計',
  uppercase: '中文大寫', note: '備註（右側直欄）',
};
const ROW_DETAIL_KEYS = ['description', 'quantity', 'unit_price', 'amount'];
const NUMERIC_KEYS = new Set(['unit_price', 'amount', 'subtotal', 'tax', 'total', 'year', 'month', 'day']);
const MAX_ITEMS = 5;

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}
function money(value) {
  return Number(value || 0).toLocaleString('zh-Hant', { maximumFractionDigits: 0 });
}
export function taipeiDate(value = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Taipei', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(value);
  const map = {};
  parts.forEach((part) => { if (part.type !== 'literal') map[part.type] = part.value; });
  return `${map.year}-${map.month}-${map.day}`;
}
function rocParts(dateString) {
  const [year, month, day] = dateString.split('-').map(Number);
  return { year: year - 1911, month, day };
}
function roundedAmount(quantity, unitPrice) {
  const q = Number(quantity || 1);
  const p = Number(unitPrice || 0);
  return Math.round((q * p) * 100) / 100;
}

const state = {
  layout: null,
  calc: null,
  draftRevision: 0,
  layoutRevision: 0,
  testPrint: false,
  printing: false,
  selectedField: null,
  sequence: 0,
};

function gatherDraft() {
  return {
    buyer: $('buyer').value.trim(),
    tax_id: $('taxId').value.trim(),
    address: $('buyerAddress').value.trim(),
    invoice_date: $('invoiceDate').value,
    tax: Number($('tax').value || 0),
    note: $('note').value.trim(),
    items: [...document.querySelectorAll('.item-row')].map((row) => ({
      description: row.querySelector('.item-description').value.trim(),
      quantity: row.querySelector('.item-quantity').value.trim(),
      unit_price: row.querySelector('.item-price').value.trim(),
    })),
  };
}
function setFeedback(text, kind = 'info') {
  const node = $('globalMessage');
  node.textContent = text || '';
  node.className = kind === 'error' ? 'notice error' : 'notice';
}
export function backupMessage(backup) {
  if (!backup || backup.status === 'saved') return '已保存，Google Drive 資料庫備份成功。';
  if (backup.status === 'skipped_regression') return '已保存；Google Drive 尚未成功設定，目前只留在本機資料庫。';
  return `已保存，但雲端備份失敗：${backup.message || ''} 請稍後按「重試雲端備份」。`;
}

export function positionedFields(layout, draft, result) {
  const roc = rocParts(draft.invoice_date);
  const fields = layout.fields;
  const out = [];
  function add(key, text, rowIndex = null) {
    const field = fields[key];
    if (!field || !text) return;
    let y = field.y + Number(layout.offset_y || 0);
    if (rowIndex !== null && ROW_DETAIL_KEYS.includes(key)) {
      y += rowIndex * Number(layout.row_pitch || 6);
    }
    out.push({
      key, text,
      x: Number((field.x + Number(layout.offset_x || 0)).toFixed(2)),
      y: Number(y.toFixed(2)),
      width: field.width,
      height: field.height || 0,
      font_size: field.font_size || layout.font_size,
      numeric: NUMERIC_KEYS.has(key),
    });
  }
  add('buyer', draft.buyer);
  if (draft.tax_id) {
    draft.tax_id.split('').forEach((char, index) => {
      add('tax_id', char, null);
      const base = out[out.length - 1];
      base.key = 'tax_id';
      base.numeric = true;
      base.x = Number((fields.tax_id.x + Number(layout.offset_x || 0)
        + index * Number(layout.tax_id_spacing || 6)).toFixed(2));
      base.width = Number(layout.tax_id_spacing || 6);
    });
  }
  add('address', draft.address);
  add('year', String(roc.year));
  add('month', String(roc.month));
  add('day', String(roc.day));
  draft.items.forEach((item, index) => {
    add('description', item.description, index);
    add('quantity', item.quantity, index);
    add('unit_price', item.unit_price ? money(item.unit_price) : '', index);
    add('amount', result.amounts[index] ? money(result.amounts[index]) : '', index);
  });
  add('subtotal', money(result.subtotal));
  add('tax', money(result.tax));
  add('total', money(result.total));
  add('uppercase', (result.uppercase_digits || []).join(''));
  add('note', draft.note);
  return out;
}

export function paperGeometry(layout) {
  const width = Number(layout.paper_width);
  const height = Number(layout.paper_height);
  if (!Number.isFinite(width) || !Number.isFinite(height)) throw new Error('紙張尺寸不正確');
  const mode = layout.feed_mode || 'portrait_cw';
  const portrait = mode === 'portrait_cw' || mode === 'portrait_ccw';
  const pageW = portrait ? height : width;
  const pageH = portrait ? width : height;
  if (!(width >= 80 && width <= 297 && height >= 80 && height <= 297)) throw new Error('紙張尺寸須為80至297mm。');
  return {
    width, height, mode, portrait,
    pageWidth: pageW, pageHeight: pageH,
    pageRule: `@page { size:${pageW}mm ${pageH}mm; margin:0; }`,
  };
}

function gatherLayout() {
  const fields = {};
  document.querySelectorAll('[data-field]').forEach((input) => {
    const key = input.dataset.field;
    const dim = input.dataset.dim;
    fields[key] = fields[key] || { x: 0, y: 0, width: 0, height: 0 };
    if (dim === 'font_size' && input.value === '') return;
    fields[key][dim] = Number(input.value);
  });
  return {
    paper_width: Number($('paperWidth').value),
    paper_height: Number($('paperHeight').value),
    feed_mode: $('feedMode').value,
    font: $('fontSize').dataset.font || state.layout.font,
    font_size: Number($('fontSize').value),
    row_pitch: Number($('rowPitch').value),
    tax_id_spacing: Number($('taxSpacing').value),
    offset_x: Number($('offsetX').value),
    offset_y: Number($('offsetY').value),
    fields,
  };
}

function renderFieldEditor() {
  const editor = $('fieldEditor');
  editor.innerHTML = FIELD_ORDER.map((key) => {
    const field = state.layout.fields[key];
    const height = field.height ? `<label>高<input type="number" step="0.1" data-field="${key}" data-dim="height" value="${field.height}"></label>` : '';
    const fontSize = field.font_size ?? '';
    return `<div class="field-row"><strong>${FIELD_LABELS[key]}</strong>
      <label>X<input type="number" step="0.1" data-field="${key}" data-dim="x" value="${field.x}"></label>
      <label>Y<input type="number" step="0.1" data-field="${key}" data-dim="y" value="${field.y}"></label>
      <label>寬<input type="number" step="0.1" data-field="${key}" data-dim="width" value="${field.width}"></label>
      <label>字級<input type="number" min="8" max="24" step="0.5" data-field="${key}" data-dim="font_size" value="${fontSize}" placeholder="全域"></label>
      ${height}</div>`;
  }).join('');
  editor.querySelectorAll('input').forEach((input) => input.addEventListener('input', liveRefresh));
}

function syncLayoutControls() {
  const layout = state.layout;
  $('paperWidth').value = layout.paper_width;
  $('paperHeight').value = layout.paper_height;
  $('feedMode').value = layout.feed_mode || 'portrait_cw';
  $('fontSize').value = layout.font_size;
  $('fontSize').dataset.font = layout.font;
  $('rowPitch').value = layout.row_pitch;
  $('taxSpacing').value = layout.tax_id_spacing;
  $('offsetX').value = layout.offset_x;
  $('offsetY').value = layout.offset_y;
}

function liveRefresh() {
  try {
    state.layout = gatherLayout();
    renderPreview();
    validatePaper();
  } catch (error) {
    $('paperWarning').hidden = false;
    $('paperWarning').textContent = error.message;
  }
}

function validatePaper() {
  const warning = $('paperWarning');
  const layout = state.layout;
  const problems = [];
  Object.entries(layout.fields).forEach(([key, field]) => {
    if (field.x + field.width > layout.paper_width + 0.1) {
      problems.push(`${FIELD_LABELS[key] || key} 超出右邊界`);
    }
    if (field.y + (field.height || 0) > layout.paper_height + 0.1) {
      problems.push(`${FIELD_LABELS[key] || key} 超出下邊界`);
    }
  });
  warning.hidden = problems.length === 0;
  warning.textContent = problems.join('；');
  return problems.length === 0;
}

function buildOneRow() {
  const tr = document.createElement('tr');
  tr.className = 'item-row';
  tr.innerHTML = `
    <td><input class="item-description" maxlength="40"></td>
    <td><input class="item-quantity" inputmode="decimal" style="width:80px"></td>
    <td><input class="item-price" inputmode="decimal" style="width:100px"></td>
    <td class="item-amount">0</td>
    <td><button type="button" class="button-secondary item-remove">刪</button></td>`;
  tr.querySelector('.item-remove').addEventListener('click', () => {
    if (document.querySelectorAll('.item-row').length === 1) return;
    tr.remove();
    refresh();
  });
  tr.querySelectorAll('input').forEach((input) => input.addEventListener('input', () => {
    updateRowAmount(tr);
    refresh();
  }));
  return tr;
}

function renderItems() {
  const tbody = $('itemRows');
  tbody.innerHTML = '';
  for (let i = 0; i < MAX_ITEMS; i += 1) tbody.appendChild(buildOneRow());
}

function updateRowAmount(row) {
  const quantity = row.querySelector('.item-quantity').value;
  const price = row.querySelector('.item-price').value;
  const amount = roundedAmount(quantity, price);
  row.querySelector('.item-amount').textContent = price ? money(amount) : '0';
  return amount;
}

function ensureItemRows() {
  if (!document.querySelector('.item-row')) {
    renderItems();
  }
}

function refresh() {
  ensureItemRows();
  const draft = gatherDraft();
  fetch('/api/invoice-print/calculate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(draft),
  }).then(async (response) => {
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      $('validation').textContent = data.detail || '金額或日期不正確';
      $('validation').hidden = false;
      state.calc = null;
      return;
    }
    const calculated = await response.json();
    if (!Array.isArray(calculated.uppercase_digits) || calculated.uppercase_digits.length !== 9) {
      state.calc = null;
      $('validation').textContent = '大寫金額九個位數資料不完整，請重新整理後再試；本次不可套印。';
      $('validation').hidden = false;
      renderPreview();
      return;
    }
    state.calc = calculated;
    $('validation').hidden = true;
    $('subtotalText').textContent = money(state.calc.subtotal);
    $('taxText').textContent = money(state.calc.tax);
    $('totalText').textContent = money(state.calc.total);
    $('uppercaseText').textContent = state.calc.uppercase;
    renderPreview();
  });
}

function renderPreview() {
  if (!state.layout) return;
  const sheet = $('sheet');
  const content = $('sheetContent');
  const paper = paperGeometry(state.layout);
  document.documentElement.style.setProperty('--paper-width', `${paper.width}mm`);
  document.documentElement.style.setProperty('--paper-height', `${paper.height}mm`);
  document.documentElement.style.setProperty('--page-width', `${paper.pageWidth}mm`);
  document.documentElement.style.setProperty('--page-height', `${paper.pageHeight}mm`);
  sheet.style.width = `${paper.width}mm`;
  sheet.style.height = `${paper.height}mm`;
  $('referencePhoto').hidden = state.testPrint || !$('showPhoto').checked;
  sheet.classList.toggle('feed-portrait', paper.portrait);
  sheet.classList.toggle('feed-cw', paper.mode === 'portrait_cw');
  sheet.classList.toggle('feed-ccw', paper.mode === 'portrait_ccw');

  let styleNode = document.getElementById('paperStyle');
  if (!styleNode) {
    styleNode = document.createElement('style');
    styleNode.id = 'paperStyle';
    document.head.appendChild(styleNode);
  }
  styleNode.textContent = paper.pageRule;

  if (state.testPrint) {
    content.innerHTML = '';
    $('testGrid').hidden = false;
    $('coordinateOverlay').hidden = true;
    $('coordinateLegend').hidden = true;
    renderTestGrid(paper);
    return;
  }
  $('testGrid').hidden = true;
  renderCoordinates(paper);
  const draft = gatherDraft();
  if (!state.calc) { content.innerHTML = ''; return; }
  content.innerHTML = positionedFields(state.layout, draft, state.calc).map((field) => {
    const size = field.font_size;
    const heightStyle = field.height ? `height:${field.height}mm;` : '';
    const cls = field.key === 'note' ? 'sheet-field field-note'
      : field.key === 'uppercase' ? 'sheet-field field-uppercase' : 'sheet-field';
    const text = field.key === 'uppercase'
      ? [...field.text].map((digit) => `<span>${esc(digit)}</span>`).join('')
      : esc(field.text);
    return `<div class="${cls}" data-key="${esc(field.key)}"
      style="left:${field.x}mm;top:${field.y}mm;${heightStyle}
      width:${field.width}mm;font-size:${size}pt;
      justify-content:${field.numeric ? 'flex-end' : 'flex-start'}">${text}</div>`;
  }).join('');
}

function renderCoordinates(paper) {
  const overlay = $('coordinateOverlay');
  const legend = $('coordinateLegend');
  const visible = $('showCoordinates').checked;
  overlay.hidden = !visible;
  legend.hidden = !visible;
  if (!visible) return;
  const offsetX = Number(state.layout.offset_x || 0);
  const offsetY = Number(state.layout.offset_y || 0);
  const fields = FIELD_ORDER.filter((key) => state.layout.fields[key]);
  let html = '<span class="coordinate-origin">0,0</span>';
  for (let x = 20; x < paper.width; x += 20) {
    html += `<span class="coordinate-x" style="left:${x}mm">X ${x}</span>`;
  }
  for (let y = 20; y < paper.height; y += 20) {
    html += `<span class="coordinate-y" style="top:${y}mm">Y ${y}</span>`;
  }
  overlay.innerHTML = html + fields.map((key, index) => {
    const field = state.layout.fields[key];
    const x = Number(field.x) + offsetX;
    const y = Number(field.y) + offsetY;
    return `<span class="coordinate-point" data-key="${key}" style="left:${x}mm;top:${y}mm" title="${esc(FIELD_LABELS[key])} X ${x} / Y ${y}">${index + 1}</span>`;
  }).join('');
  legend.innerHTML = `<strong>欄位起點座標（mm，左上角為 0,0）</strong><span>已含全域位移 X ${offsetX}、Y ${offsetY}；只供畫面校正，不會列印。</span><div class="coordinate-list">${fields.map((key, index) => {
    const field = state.layout.fields[key];
    return `<span><b>${index + 1}</b> ${esc(FIELD_LABELS[key])}：X ${Number(field.x) + offsetX}／Y ${Number(field.y) + offsetY}</span>`;
  }).join('')}</div>`;
}

function renderTestGrid(paper) {
  const grid = $('testGrid');
  grid.style.width = `${paper.width}mm`;
  grid.style.height = `${paper.height}mm`;
  const cellsX = Math.round(paper.width / 2);
  const cellsY = Math.round(paper.height / 2);
  grid.style.backgroundSize = '2mm 2mm';
  let html = `<div class="test-title">三通工程行｜${paper.width} × ${paper.height} mm 白紙校正 v3</div>`;
  html += `<div class="test-subtitle">${paper.mode === 'landscape' ? '橫式直出' : '直式103×195（網頁旋轉）'}／非正式發票／不可請款</div>`;
  for (let x = 0; x <= cellsX; x += 5) {
    html += `<span class="grid-x" style="left:${x * 2}mm">${x}</span>`;
  }
  for (let y = 0; y <= cellsY; y += 5) {
    html += `<span class="grid-y" style="top:${y * 2}mm">${y}</span>`;
  }
  html += '<span class="corner tl"></span><span class="corner tr"></span><span class="corner bl"></span><span class="corner br"></span>';
  grid.innerHTML = html;
}

export function moveFieldWithinPaper(field, deltaX, deltaY, paper) {
  const round = (value) => Math.round(value * 10) / 10;
  return {
    x: round(Math.max(0, Math.min(paper.width - field.width, field.x + deltaX))),
    y: round(Math.max(0, Math.min(paper.height - (field.height || 1.5), field.y + deltaY))),
  };
}

function selectField(key) {
  if (!state.layout?.fields[key]) return;
  state.selectedField = key;
  $('selectionToolbar').hidden = false;
  $('selectedFieldName').textContent = FIELD_LABELS[key];
  $('selectedFontSize').value = state.layout.fields[key].font_size ?? state.layout.font_size;
  $('setUppercaseReference').hidden = key !== 'uppercase';
  document.querySelectorAll('.sheet-field').forEach((node) => {
    node.classList.toggle('selected', node.dataset.key === key);
  });
}

function bindPhotoEditing() {
  const sheet = $('sheet');
  let drag = null;
  sheet.addEventListener('pointerdown', (event) => {
    if (state.testPrint || !$('showPhoto').checked) return;
    const target = event.target.closest('.sheet-field, .coordinate-point');
    const key = target?.dataset.key;
    if (!key || !state.layout.fields[key]) return;
    selectField(key);
    drag = { key, pointerId: event.pointerId, x: event.clientX, y: event.clientY,
      original: { ...state.layout.fields[key] } };
    sheet.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  sheet.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const paper = paperGeometry(state.layout);
    const pxPerMm = sheet.getBoundingClientRect().width / paper.width;
    const next = moveFieldWithinPaper(drag.original,
      (event.clientX - drag.x) / pxPerMm, (event.clientY - drag.y) / pxPerMm, paper);
    Object.assign(state.layout.fields[drag.key], next);
    for (const dim of ['x', 'y']) {
      const input = document.querySelector(`[data-field="${drag.key}"][data-dim="${dim}"]`);
      if (input) input.value = next[dim];
    }
    renderPreview();
    selectField(drag.key);
  });
  const endDrag = (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    drag = null;
    liveRefresh();
  };
  sheet.addEventListener('pointerup', endDrag);
  sheet.addEventListener('pointercancel', endDrag);
  $('selectedFontSize').addEventListener('input', (event) => {
    const key = state.selectedField;
    if (!key || !event.target.value) return;
    const input = document.querySelector(`[data-field="${key}"][data-dim="font_size"]`);
    input.value = event.target.value;
    liveRefresh();
    selectField(key);
  });
  $('resetFieldFont').addEventListener('click', () => {
    const key = state.selectedField;
    if (!key) return;
    const input = document.querySelector(`[data-field="${key}"][data-dim="font_size"]`);
    input.value = '';
    liveRefresh();
    selectField(key);
  });
  $('setUppercaseReference').addEventListener('click', () => {
    const key = 'uppercase';
    for (const [dim, value] of Object.entries({ x: 33, y: 89, width: 90, font_size: 11 })) {
      document.querySelector(`[data-field="${key}"][data-dim="${dim}"]`).value = value;
    }
    liveRefresh();
    selectField(key);
    setFeedback('已在畫面套用大寫九格參考位置；請對照相片調整，確認後才按「保存定位」。');
  });
}

export function printAllowed(testPrint, paperOk, hasValidationError, hasVisibleWarning) {
  if (testPrint) return true;
  if (!paperOk) return false;
  if (hasValidationError) return false;
  if (hasVisibleWarning) return false;
  return true;
}

function restorePrint() {
  state.printing = false;
  document.body.classList.remove('print-armed');
  $('printBtn').disabled = false;
}

function printSheet() {
  if (state.printing) return;
  const paperOk = validatePaper();
  if (!state.testPrint) {
    refresh();
    if (!state.calc) {
      alert('金額或日期不正確，無法列印正式內容。');
      return;
    }
    if (!printAllowed(false, paperOk, !$('validation').hidden, false)) {
      alert('仍有欄位超出紙張，請先修正。');
      return;
    }
  }
  state.sequence += 1;
  const sequence = state.sequence;
  state.printing = true;
  $('printBtn').disabled = true;
  document.body.classList.add('print-armed');
  renderPreview();
  const timer = setTimeout(() => {
    if (sequence === state.sequence) window.print();
  }, 250);
  if (window.matchMedia) {
    const printMedia = window.matchMedia('print');
    const onChange = (event) => {
      if (!event.matches) {
        clearTimeout(timer);
        restorePrint();
        if (printMedia.removeEventListener) printMedia.removeEventListener('change', onChange);
      }
    };
    if (printMedia.addEventListener) printMedia.addEventListener('change', onChange);
    else printMedia.addListener(onChange);
  }
  const onAfter = () => {
    clearTimeout(timer);
    if (sequence !== state.sequence) return;
    window.removeEventListener('afterprint', onAfter);
    window.removeEventListener('focus', onFocus);
    restorePrint();
  };
  const onFocus = () => {
    if (sequence !== state.sequence) return;
    clearTimeout(timer);
    window.setTimeout(() => {
      if (state.printing && sequence === state.sequence) {
        window.removeEventListener('focus', onFocus);
        window.removeEventListener('afterprint', onAfter);
        restorePrint();
      }
    }, 500);
  };
  window.addEventListener('afterprint', onAfter);
  window.addEventListener('focus', onFocus);
}

export function applySampleLayout(original, sample) {
  const fields = { ...sample.fields };
  return {
    ...original,
    paper_width: sample.paper_width,
    paper_height: sample.paper_height,
    feed_mode: sample.feed_mode,
    tax_id_spacing: sample.tax_id_spacing,
    row_pitch: sample.row_pitch,
    fields,
  };
}

export function normalizeLayout(raw) {
  // Tolerate legacy v1/v2 layouts (with 單位, no 地址) using real-form defaults.
  const sample = window.__SAMPLE_LAYOUT__;
  if (!sample || !raw || !raw.fields) return raw;
  const known = new Set(FIELD_ORDER);
  const changed = Object.keys(raw.fields).some((key) => !known.has(key))
    || FIELD_ORDER.some((key) => !raw.fields[key]);
  if (!changed && raw.feed_mode) return raw;
  const fields = {};
  FIELD_ORDER.forEach((key) => {
    fields[key] = raw.fields[key] || sample.fields[key];
  });
  return {
    ...raw,
    feed_mode: raw.feed_mode || sample.feed_mode,
    tax_id_spacing: raw.tax_id_spacing || sample.tax_id_spacing,
    row_pitch: raw.row_pitch || sample.row_pitch,
    fields,
  };
}

async function init() {
  $('currentDate').textContent = `台北日期：${taipeiDate()}`;
  $('invoiceDate').value = taipeiDate();
  renderItems();
  const [template, sample] = await Promise.all([
    fetch('/api/invoice-print/template').then((r) => r.json()),
    fetch('/api/invoice-print/sample-layout').then((r) => r.json()),
  ]);
  window.__SAMPLE_LAYOUT__ = sample;
  state.layoutRevision = template.revision;
  const normalized = normalizeLayout(template.content);
  const legacy = JSON.stringify(normalized.fields) !== JSON.stringify(template.content?.fields)
    || !template.content?.feed_mode;
  state.layout = normalized;
  $('legacyWarning').hidden = !legacy;
  syncLayoutControls();
  renderFieldEditor();
  refresh();
  loadDrafts();

  document.querySelectorAll('input, textarea, select').forEach((input) => {
    if (input.id && ['buyer', 'taxId', 'buyerAddress', 'note', 'invoiceDate', 'tax'].includes(input.id)) {
      input.addEventListener('input', refresh);
    }
  });
  ['paperWidth', 'paperHeight', 'feedMode', 'fontSize', 'rowPitch', 'taxSpacing', 'offsetX', 'offsetY']
    .forEach((id) => $(id).addEventListener('input', liveRefresh));
  $('feedMode').addEventListener('change', liveRefresh);
  $('addItem').addEventListener('click', () => {
    if (document.querySelectorAll('.item-row').length >= MAX_ITEMS) return;
    const tr = document.createElement('tr');
    tr.className = 'item-row';
    tr.innerHTML = `
      <td><input class="item-description" maxlength="40"></td>
      <td><input class="item-quantity" inputmode="decimal" style="width:80px"></td>
      <td><input class="item-price" inputmode="decimal" style="width:100px"></td>
      <td class="item-amount">0</td>
      <td><button type="button" class="button-secondary item-remove">刪</button></td>`;
    $('itemRows').appendChild(tr);
    tr.querySelector('.item-remove').addEventListener('click', () => { tr.remove(); refresh(); });
    tr.querySelectorAll('input').forEach((input) => input.addEventListener('input', () => {
      updateRowAmount(tr);
      refresh();
    }));
    refresh();
  });
  $('calculateBtn').addEventListener('click', refresh);
  $('testPrint').addEventListener('change', (event) => {
    state.testPrint = event.target.checked;
    renderPreview();
  });
  $('showCoordinates').addEventListener('change', renderPreview);
  $('showPhoto').addEventListener('change', renderPreview);
  bindPhotoEditing();
  $('printBtn').addEventListener('click', printSheet);
  window.addEventListener('afterprint', restorePrint);
  window.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'p') {
      event.preventDefault();
      printSheet();
    }
  });
  $('sampleLayout').addEventListener('click', async () => {
    state.layout = applySampleLayout(state.layout, sample);
    syncLayoutControls();
    renderFieldEditor();
    $('legacyWarning').hidden = true;
    refresh();
    setFeedback('已套用真實發票195×103定位；請白紙試印，確認後再保存。');
  });
  $('saveLayout').addEventListener('click', async () => {
    const content = gatherLayout();
    const response = await fetch('/api/invoice-print/template', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_revision: state.layoutRevision, content }),
    });
    const data = await response.json();
    if (response.ok) {
      state.layoutRevision = data.revision;
      state.layout = content;
      setFeedback(backupMessage(data.backup));
    } else {
      setFeedback(data.detail || '保存失敗', 'error');
    }
  });
  $('saveDraft').addEventListener('click', async () => {
    const recordKey = prompt('請輸入草稿名稱（例如：2026-10-23 齊裕營造）', `${$('invoiceDate').value} ${$('buyer').value}`.trim());
    if (!recordKey) return;
    const response = await fetch('/api/invoice-print/drafts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ record_key: recordKey, expected_revision: state.draftRevision, content: gatherDraft() }),
    });
    const data = await response.json();
    if (response.ok) {
      state.draftRevision = data.revision;
      setFeedback(backupMessage(data.backup));
      loadDrafts();
    } else {
      setFeedback(data.detail || '草稿保存失敗', 'error');
    }
  });
  $('retryBackup').addEventListener('click', async () => {
    const response = await fetch('/api/invoice-print/backup', { method: 'POST' });
    const data = await response.json();
    setFeedback(response.ok ? 'Google Drive 備份成功。' : `備份失敗：${data.detail}`);
  });
  window.addEventListener('resize', renderPreview);
}

async function loadDrafts() {
  const data = await fetch('/api/invoice-print/drafts').then((r) => r.json());
  $('draftRows').innerHTML = data.items.map((item) => `
    <tr>
      <td><a href="#" data-key="${esc(item.record_key)}">${esc(item.record_key)}</a></td>
      <td>${esc(item.content.buyer || '')}</td>
      <td>${new Date(item.created_at).toLocaleString('zh-Hant')}</td>
      <td>${item.revision}</td>
    </tr>`).join('');
  $('draftRows').querySelectorAll('a').forEach((link) => link.addEventListener('click', async (event) => {
    event.preventDefault();
    const key = link.dataset.key;
    const drafts = await fetch('/api/invoice-print/drafts').then((r) => r.json());
    const item = drafts.items.find((entry) => entry.record_key === key);
    if (!item) return;
    state.draftRevision = item.revision;
    applyDraft(item.content);
  }));
}

function applyDraft(content) {
  $('buyer').value = content.buyer || '';
  $('taxId').value = content.tax_id || '';
  $('buyerAddress').value = content.address || '';
  $('invoiceDate').value = content.invoice_date || taipeiDate();
  $('tax').value = content.tax || 0;
  $('note').value = content.note || '';
  ensureItemRows();
  const rows = [...document.querySelectorAll('.item-row')];
  rows.forEach((row, index) => {
    const item = content.items?.[index] || {};
    row.querySelector('.item-description').value = item.description || '';
    row.querySelector('.item-quantity').value = item.quantity || '';
    row.querySelector('.item-price').value = item.unit_price || '';
    updateRowAmount(row);
  });
  refresh();
}

if (typeof document !== 'undefined') {
  init();
}
