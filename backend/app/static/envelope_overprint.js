const $ = (id) => document.getElementById(id);
const FIELD_ORDER = [
  'receiver_postcode', 'receiver_company', 'receiver_address', 'receiver_name',
  'sender_postcode', 'sender_address', 'sender_name', 'sender_phone',
];
const FIELD_LABELS = {
  receiver_postcode: '收件人郵遞區號',
  receiver_company: '收件公司（最右欄）',
  receiver_address: '收件人地址',
  receiver_name: '收件人姓名',
  sender_postcode: '寄件人郵遞區號',
  sender_address: '寄件人地址',
  sender_name: '寄件人姓名',
  sender_phone: '寄件人電話（併入姓名直書，不另印）',
};
const VERTICAL_FIELDS = new Set([
  'receiver_company', 'receiver_address', 'receiver_name',
  'sender_address', 'sender_name',
]);
const POSTCODE_FIELDS = new Set(['receiver_postcode', 'sender_postcode']);

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

const state = {
  layout: null,
  draft: null,
  lines: null,
  draftRevision: 0,
  layoutRevision: 0,
  testPrint: false,
  printing: false,
  sequence: 0,
};

export function setFeedback(text, kind = 'info') {
  const node = $('globalMessage');
  node.textContent = text || '';
  node.className = kind === 'error' ? 'notice error' : 'notice';
}
export function backupMessage(backup) {
  if (!backup || backup.status === 'saved') return '已保存，Google Drive 資料庫備份成功。';
  if (backup.status === 'skipped_regression') return '已保存；Google Drive 尚未成功設定，目前只留在本機資料庫。';
  return `已保存，但雲端備份失敗：${backup.message || ''} 請稍後按「重試雲端備份」。`;
}

function gatherDraft() {
  return {
    receiver_postcode: $('receiverPostcode').value.trim(),
    receiver_address: $('receiverAddress').value.trim(),
    receiver_company: $('receiverCompany').value.trim(),
    receiver_name: $('receiverName').value.trim(),
    receiver_suffix: $('receiverSuffix').value.trim(),
    sender_postcode: $('senderPostcode').value.trim(),
    sender_address: $('senderAddress').value.trim(),
    sender_name: $('senderName').value.trim(),
    sender_suffix: $('senderSuffix').value.trim(),
    sender_phone: $('senderPhone').value.trim(),
  };
}

function gatherLayout() {
  const fields = {};
  document.querySelectorAll('[data-field]').forEach((input) => {
    const key = input.dataset.field;
    const dim = input.dataset.dim;
    fields[key] = fields[key] || { x: 0, y: 0, width: 0, height: 0 };
    fields[key][dim] = Number(input.value);
  });
  return {
    paper_width: Number($('paperWidth').value),
    paper_height: Number($('paperHeight').value),
    font: state.layout.font,
    font_size: Number($('fontSize').value),
    name_font_size: Number($('nameFontSize').value),
    small_font_size: Number($('smallFontSize').value),
    postcode_spacing: Number($('postcodeSpacing').value),
    postcode_gap: Number($('postcodeGap').value),
    offset_x: Number($('offsetX').value),
    offset_y: Number($('offsetY').value),
    fields,
  };
}

// Photo calibration. Receiver postcode is already on its squares and is not copied.
const PHOTO_LAYOUT_KEYS = [
  'sender_postcode', 'sender_address', 'sender_name', 'sender_phone',
  'receiver_company', 'receiver_address', 'receiver_name',
];

export function applySenderLayout(original, sample) {
  const fields = { ...original.fields };
  for (const key of PHOTO_LAYOUT_KEYS) {
    fields[key] = { ...sample.fields[key] };
  }
  return { ...original, fields };
}

// One upright cell per character. writing-mode mixes digit and CJK advances,
// which is what stacked 3 onto 路 and 6 onto 巷. Cell height is the only gap.
export const GLYPH_EM = 1.15;
const PHONE_GAP_EM = 0.45;
// Sender squares measured on the printed envelope. Do not reuse the receiver pitch.
export const SENDER_POSTCODE_SPACING = 6.4;
export const SENDER_POSTCODE_GAP = 3;
export const SENDER_POSTCODE_BOXES = [
  [9.10, 182.4, 5.20, 7.8],
  [15.60, 182.4, 5.08, 7.8],
  [21.99, 182.4, 5.08, 7.8],
  [31.44, 182.4, 5.08, 7.8],
  [37.83, 182.4, 5.08, 7.8],
  [44.21, 182.4, 5.08, 7.8],
];
export const STAMP_GUIDE = [6.7, 15.5, 20.9, 18.4];

function isHyphen(ch) {
  return ch === '-' || ch === '－' || ch === '—' || ch === '–';
}

function normalizeSenderText(text) {
  return String(text ?? '').replace(/電話\s*[:：]/g, '電話：');
}

export function columnParts(text, { sidewaysPhone = false } = {}) {
  const source = normalizeSenderText(text);
  if (!sidewaysPhone) return { glyphs: Array.from(source), phone: '' };
  const match = source.match(/^(.*?)([0-9０-９][0-9０-９\-－—–]*)$/);
  if (!match || !match[2]) return { glyphs: Array.from(source), phone: '' };
  return { glyphs: Array.from(match[1]), phone: match[2] };
}

function phoneAdvanceEm(phone) {
  let em = PHONE_GAP_EM + 0.25;
  for (const ch of phone) em += isHyphen(ch) ? 0.45 : 0.62;
  return em;
}

export function verticalAdvanceEm(text, options = {}) {
  const { glyphs, phone } = columnParts(text, options);
  let em = glyphs.length * GLYPH_EM;
  if (phone) em += phoneAdvanceEm(phone);
  return em;
}

function glyphSpan(ch) {
  const shown = ch === ' ' ? '\u00a0' : ch;
  return `<span class="glyph">${esc(shown)}</span>`;
}

// Shared by the on-screen preview and print (print shows this same DOM).
export function verticalColumnHtml(text, options = {}) {
  const { glyphs, phone } = columnParts(text, options);
  let html = glyphs.map(glyphSpan).join('');
  if (phone) {
    const height = phoneAdvanceEm(phone) - PHONE_GAP_EM;
    html += `<span class="phone-run" style="height:${height.toFixed(2)}em"><span class="phone-text">${esc(phone)}</span></span>`;
  }
  return html;
}

// Shrink the whole column by one ratio when it would pass the field height.
export function verticalFontSize(text, heightMm, maxPt, options = {}) {
  const em = verticalAdvanceEm(text, options);
  const cap = Number(maxPt);
  if (!em || !(Number(heightMm) > 0) || !Number.isFinite(cap)) return cap;
  const usable = Math.max(1, Number(heightMm) - 1);
  const fitted = usable / (em * 0.3528);
  return Math.round(Math.min(cap, fitted) * 10) / 10;
}

function fontSizeFor(key, layout) {
  if (key === 'receiver_name') return layout.name_font_size;
  if (key.startsWith('sender') || key === 'sender_postcode') return layout.small_font_size;
  return layout.font_size;
}

export function positionedElements(layout, draft, lines) {
  const out = [];
  const ox = Number(layout.offset_x || 0);
  const oy = Number(layout.offset_y || 0);
  const postcodeSpacing = Number(layout.postcode_spacing || 6.5);
  const postcodeGap = Number(layout.postcode_gap || 3);

  function postcodeChars(key, raw) {
    const chars = (raw || '').replace(/\D/g, '').slice(0, 5).split('');
    const field = layout.fields[key];
    const spacing = key === 'sender_postcode' ? SENDER_POSTCODE_SPACING : postcodeSpacing;
    const gap = key === 'sender_postcode' ? SENDER_POSTCODE_GAP : postcodeGap;
    chars.forEach((ch, i) => {
      out.push({
        key, text: ch,
        x: Number((field.x + ox + i * spacing + (i >= 3 ? gap : 0)).toFixed(2)),
        y: Number((field.y + oy).toFixed(2)),
        width: spacing,
        height: 0,
        postcode: true,
        fontSize: fontSizeFor(key, layout),
      });
    });
  }

  function addVertical(key, text, { oneColumn = false, sidewaysPhone = false } = {}) {
    const field = layout.fields[key];
    if (!text || !field) return;
    const fontSize = oneColumn
      ? verticalFontSize(text, field.height, fontSizeFor(key, layout), { sidewaysPhone })
      : fontSizeFor(key, layout);
    out.push({
      key, text,
      x: Number((field.x + ox).toFixed(2)),
      y: Number((field.y + oy).toFixed(2)),
      width: field.width,
      height: field.height,
      vertical: !oneColumn,
      oneColumn,
      sidewaysPhone,
      fontSize,
    });
  }

  postcodeChars('receiver_postcode', draft.receiver_postcode);
  addVertical('receiver_company', lines.receiver_company);
  addVertical('receiver_address', lines.receiver_address);
  addVertical('receiver_name', lines.receiver_name);
  postcodeChars('sender_postcode', draft.sender_postcode);
  // Address is one vertical column. Name and phone share the other column;
  // sender_phone is not drawn again.
  addVertical('sender_address', lines.sender_address, { oneColumn: true });
  addVertical('sender_name', lines.sender_name, { oneColumn: true, sidewaysPhone: true });
  return out;
}

function renderFieldEditor() {
  $('fieldEditor').innerHTML = FIELD_ORDER.map((key) => {
    const field = state.layout.fields[key];
    const vertical = VERTICAL_FIELDS.has(key);
    return `<div class="field-row"><strong>${FIELD_LABELS[key]}</strong>
      <label>X<input type="number" step="0.1" data-field="${key}" data-dim="x" value="${field.x}"></label>
      <label>${vertical ? 'Y頂' : 'Y'}<input type="number" step="0.1" data-field="${key}" data-dim="y" value="${field.y}"></label>
      <label>寬<input type="number" step="0.1" data-field="${key}" data-dim="width" value="${field.width}"></label>
      <label>${vertical ? '行高' : '高'}<input type="number" step="0.1" data-field="${key}" data-dim="height" value="${field.height}"></label>
      </div>`;
  }).join('');
  $('fieldEditor').querySelectorAll('input').forEach((input) => input.addEventListener('input', liveRefresh));
}

function syncControls() {
  const l = state.layout;
  $('paperWidth').value = l.paper_width;
  $('paperHeight').value = l.paper_height;
  $('fontSize').value = l.font_size;
  $('nameFontSize').value = l.name_font_size;
  $('smallFontSize').value = l.small_font_size;
  $('postcodeSpacing').value = l.postcode_spacing;
  $('postcodeGap').value = l.postcode_gap;
  $('offsetX').value = l.offset_x;
  $('offsetY').value = l.offset_y;
}

async function validateDraft() {
  const draft = gatherDraft();
  const response = await fetch('/api/envelope-print/validate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(draft),
  });
  const data = await response.json();
  if (!response.ok) {
    $('validation').textContent = data.detail || '內容不正確';
    $('validation').hidden = false;
    return null;
  }
  $('validation').hidden = true;
  state.draft = draft;
  state.lines = data.lines;
  return data;
}

function validatePaper() {
  const l = state.layout;
  const problems = [];
  Object.entries(l.fields).forEach(([key, field]) => {
    if (field.x + field.width > l.paper_width + 0.1) problems.push(`${FIELD_LABELS[key] || key} 超出右邊界`);
    if (field.y + field.height > l.paper_height + 0.1) problems.push(`${FIELD_LABELS[key] || key} 超出下邊界`);
  });
  if (!(l.paper_width >= 80 && l.paper_width <= 300 && l.paper_height >= 80 && l.paper_height <= 300)) {
    problems.push('紙張寬高必須介於80至300 mm');
  }
  $('paperWarning').hidden = problems.length === 0;
  $('paperWarning').textContent = problems.join('；');
  return problems.length === 0;
}

function liveRefresh() {
  state.layout = gatherLayout();
  validatePaper();
  renderPreview();
}

function renderPreview() {
  if (!state.layout) return;
  const l = state.layout;
  const sheet = $('sheet');
  document.documentElement.style.setProperty('--page-width', `${l.paper_width}mm`);
  document.documentElement.style.setProperty('--page-height', `${l.paper_height}mm`);
  const scale = Math.min(
    ($('sheet').parentElement.clientWidth - 40) / l.paper_width,
    4.2,
  );
  sheet.style.width = `${l.paper_width * scale}px`;
  sheet.style.height = `${l.paper_height * scale}px`;
  sheet.dataset.scale = scale;

  const styleNode = document.getElementById('paperStyle') || (() => {
    const node = document.createElement('style');
    node.id = 'paperStyle';
    document.head.appendChild(node);
    return node;
  })();
  styleNode.textContent = `@page { size:${l.paper_width}mm ${l.paper_height}mm; margin:0; }`;

  if (state.testPrint) {
    $('sheetContent').innerHTML = '';
    const guides = $('preprintGuides');
    if (guides) guides.hidden = true;
    $('testGrid').hidden = false;
    renderTestGrid(l, scale);
    return;
  }
  $('testGrid').hidden = true;
  const guides = $('preprintGuides');
  if (guides) {
    guides.hidden = false;
    guides.innerHTML = preprintGuideHtml();
  }
  const content = $('sheetContent');
  if (!state.lines) { content.innerHTML = ''; return; }
  content.innerHTML = positionedElements(l, state.draft, state.lines).map((el) => {
    if (el.postcode) {
      return `<div class="sheet-field postcode-char" data-key="${esc(el.key)}"
        style="left:${el.x}mm;top:${el.y}mm;width:${el.width}mm;font-size:${el.fontSize}pt">${esc(el.text)}</div>`;
    }
    const kind = el.oneColumn ? ' stack-column' : (el.vertical ? ' vertical' : '');
    const body = el.oneColumn
      ? verticalColumnHtml(el.text, { sidewaysPhone: !!el.sidewaysPhone })
      : esc(el.text);
    return `<div class="sheet-field${kind}" data-key="${esc(el.key)}"
      style="left:${el.x}mm;top:${el.y}mm;width:${el.width}mm;height:${el.height}mm;font-size:${el.fontSize}pt">${body}</div>`;
  }).join('');
}

function postcodeBoxesHtml(field, spacing, gap) {
  let html = '';
  for (let i = 0; i < 5; i += 1) {
    const x = field.x + i * spacing + (i >= 3 ? gap : 0);
    html += `<span class="guide-box" style="left:${x}mm;top:${field.y - 0.8}mm;width:${spacing - 0.4}mm;height:${spacing - 0.4}mm"></span>`;
  }
  return html;
}

function preprintGuideHtml() {
  const [x, y, w, h] = STAMP_GUIDE;
  let html = `<div class="guide-stamp" style="left:${x}mm;top:${y}mm;width:${w}mm;height:${h}mm">貼郵票處</div>`;
  for (const box of SENDER_POSTCODE_BOXES) {
    html += `<span class="guide-box" style="left:${box[0]}mm;top:${box[1]}mm;width:${box[2]}mm;height:${box[3]}mm"></span>`;
  }
  return html;
}

function renderTestGrid(l, scale) {
  const grid = $('testGrid');
  grid.style.width = `${l.paper_width}mm`;
  grid.style.height = `${l.paper_height}mm`;
  grid.style.backgroundSize = `${2 * scale}px ${2 * scale}px`;
  let html = `<div class="test-title">三通工程行｜直式信封 ${l.paper_width}×${l.paper_height} mm 白紙校正</div>`;
  html += preprintGuideHtml();
  html += postcodeBoxesHtml(l.fields.receiver_postcode, l.postcode_spacing, l.postcode_gap);
  const cellsX = Math.round(l.paper_width / 2);
  const cellsY = Math.round(l.paper_height / 2);
  for (let x = 0; x <= cellsX; x += 5) {
    html += `<span class="grid-x" style="left:${x * 2}mm">${x}</span>`;
  }
  for (let y = 0; y <= cellsY; y += 5) {
    html += `<span class="grid-y" style="top:${y * 2}mm">${y}</span>`;
  }
  html += '<span class="corner tl"></span><span class="corner tr"></span><span class="corner bl"></span><span class="corner br"></span>';
  grid.innerHTML = html;
}

function restorePrint() {
  state.printing = false;
  document.body.classList.remove('print-armed');
  $('printBtn').disabled = false;
}

async function downloadPdf(calibration) {
  const button = $(calibration ? 'downloadCalibrationPdfBtn' : 'downloadPdfBtn');
  const layout = gatherLayout();
  const body = { layout, calibration: Boolean(calibration) };
  if (!calibration) {
    const ok = await validateDraft();
    if (!ok) {
      alert('請先填齊收件人地址與姓名。');
      return;
    }
    if (!validatePaper()) {
      alert('仍有欄位超出紙張，請先修正。');
      return;
    }
    body.draft = gatherDraft();
  }
  button.disabled = true;
  try {
    const response = await fetch('/api/envelope-print/pdf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const detail = typeof data.detail === 'string' ? data.detail : 'PDF 產生失敗';
      setFeedback(detail, 'error');
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    const width = layout.paper_width || 100;
    const height = layout.paper_height || 200;
    link.download = calibration
      ? `信封白紙校正-${width}x${height}.pdf`
      : `信封套印-${width}x${height}.pdf`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setFeedback(calibration ? '已下載白紙校正 PDF。請用檢視器以實際大小列印。' : '已下載信封 PDF。請用檢視器以實際大小列印，紙張選 100×200。');
  } catch (error) {
    setFeedback('PDF 下載失敗，請稍後再試。', 'error');
  } finally {
    button.disabled = false;
  }
}

function printSheet() {
  if (state.printing) return;
  if (!state.testPrint) {
    validateDraft().then((ok) => {
      if (!ok) { alert('請先填齊收件人地址與姓名。'); return; }
      if (!validatePaper()) { alert('仍有欄位超出紙張，請先修正。'); return; }
      doPrint();
    });
    return;
  }
  doPrint();
}

function doPrint() {
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

async function init() {
  const [template, sample] = await Promise.all([
    fetch('/api/envelope-print/template').then((r) => r.json()),
    fetch('/api/envelope-print/sample-layout').then((r) => r.json()),
  ]);
  window.__SAMPLE_LAYOUT__ = sample;
  state.layoutRevision = template.revision;
  state.layout = template.content;
  syncControls();
  renderFieldEditor();
  validatePaper();
  const validated = await validateDraft();
  if (validated) renderPreview();
  loadDrafts();

  document.querySelectorAll('input, textarea').forEach((input) => {
    input.addEventListener('input', () => validateDraft().then((ok) => { if (ok) renderPreview(); }));
  });
  ['paperWidth', 'paperHeight', 'fontSize', 'nameFontSize', 'smallFontSize',
   'postcodeSpacing', 'postcodeGap', 'offsetX', 'offsetY']
    .forEach((id) => $(id).addEventListener('input', liveRefresh));
  $('testPrint').addEventListener('change', (event) => {
    state.testPrint = event.target.checked;
    renderPreview();
  });
  $('printBtn').addEventListener('click', printSheet);
  $('downloadPdfBtn').addEventListener('click', () => downloadPdf(false));
  $('downloadCalibrationPdfBtn').addEventListener('click', () => downloadPdf(true));
  window.addEventListener('afterprint', restorePrint);
  $('sampleLayout').addEventListener('click', () => {
    state.layout = { ...sample, offset_x: state.layout.offset_x, offset_y: state.layout.offset_y };
    syncControls();
    renderFieldEditor();
    validatePaper();
    renderPreview();
    setFeedback('已重設為100×200標準定位；請白紙試印後再保存。');
  });
  $('senderLayout').addEventListener('click', () => {
    state.layout = applySenderLayout(gatherLayout(), sample);
    syncControls();
    renderFieldEditor();
    validatePaper();
    renderPreview();
    setFeedback('已套用照片定位。寄件地址／姓名 X18／X6、Y52、高127；寄件郵遞區號 X8.5、Y184.5、每碼 6.4mm。收件地址 Y64、公司 Y54、姓名 Y60。上方收件郵遞區號未改。尚未保存。');
  });
  $('saveLayout').addEventListener('click', async () => {
    const content = gatherLayout();
    const response = await fetch('/api/envelope-print/template', {
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
    const ok = await validateDraft();
    if (!ok) { alert('請先填齊收件人地址與姓名。'); return; }
    const defaultName = `${state.draft.receiver_postcode || ''} ${state.draft.receiver_company || state.draft.receiver_name}`.trim();
    const recordKey = prompt('請輸入草稿名稱', defaultName);
    if (!recordKey) return;
    const response = await fetch('/api/envelope-print/drafts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ record_key: recordKey, expected_revision: state.draftRevision, content: state.draft }),
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
    const response = await fetch('/api/envelope-print/backup', { method: 'POST' });
    const data = await response.json();
    setFeedback(response.ok ? 'Google Drive 備份成功。' : `備份失敗：${data.detail}`);
  });
  window.addEventListener('resize', renderPreview);
}

async function loadDrafts() {
  const data = await fetch('/api/envelope-print/drafts').then((r) => r.json());
  $('draftRows').innerHTML = data.items.map((item) => `
    <tr>
      <td><a href="#" data-key="${esc(item.record_key)}">${esc(item.record_key)}</a></td>
      <td>${esc(item.content.receiver_company || item.content.receiver_name || '')}</td>
      <td>${new Date(item.created_at).toLocaleString('zh-Hant')}</td>
      <td>${item.revision}</td>
    </tr>`).join('') || '<tr><td colspan="4" class="muted">尚無草稿</td></tr>';
  $('draftRows').querySelectorAll('a').forEach((link) => link.addEventListener('click', async (event) => {
    event.preventDefault();
    const key = link.dataset.key;
    const drafts = await fetch('/api/envelope-print/drafts').then((r) => r.json());
    const item = drafts.items.find((entry) => entry.record_key === key);
    if (item) applyDraft(item.content);
  }));
}

function applyDraft(content) {
  $('receiverPostcode').value = content.receiver_postcode || '';
  $('receiverCompany').value = content.receiver_company || '';
  $('receiverName').value = content.receiver_name || '';
  $('receiverSuffix').value = content.receiver_suffix || '收';
  $('receiverAddress').value = content.receiver_address || '';
  $('senderPostcode').value = content.sender_postcode || '';
  $('senderName').value = content.sender_name || '';
  $('senderSuffix').value = content.sender_suffix || '緘';
  $('senderPhone').value = content.sender_phone || '';
  $('senderAddress').value = content.sender_address || '';
  validateDraft().then((ok) => { if (ok) renderPreview(); });
}

if (typeof document !== 'undefined') {
  init();
}
