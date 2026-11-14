let worksiteBoardRequest = 0;

function initWorksiteBoard() {
  const select = document.getElementById('worksiteBoardSite');
  const previous = select.value;
  const sites = state.actor?.role === 'site_manager'
    ? state.worksites.filter(site => site.id === state.actor.home_site_id)
    : state.worksites;
  select.innerHTML = '<option value="">請選擇工地</option>' + sites.map(site =>
    `<option value="${Number(site.id)}">${escapeHtml(site.name)}（${escapeHtml(site.code)}）</option>`
  ).join('');
  if (sites.some(site => String(site.id) === previous)) select.value = previous;
  document.getElementById('worksiteBoardStatus').textContent = '選擇日期與工地後查看；不會修改正式紀錄。';
}

async function loadWorksiteBoard() {
  const date = document.getElementById('worksiteBoardDate').value;
  const siteId = document.getElementById('worksiteBoardSite').value;
  const status = document.getElementById('worksiteBoardStatus');
  const cards = document.getElementById('worksiteBoardCards');
  const request = ++worksiteBoardRequest;
  if (!date || !siteId) {
    cards.replaceChildren();
    status.textContent = '請先選擇日期與工地。';
    return;
  }
  status.textContent = '正在核對派工、打卡、點檢、照片、日誌、簽單與計價紀錄…';
  try {
    const params = new URLSearchParams({target_date: date, site_id: siteId});
    const data = await api(`/api/worksite-board?${params}`);
    if (request !== worksiteBoardRequest) return;
    renderWorksiteBoard(data);
    status.textContent = `${data.date}｜${data.site.name}。僅依目前紀錄顯示；沒有資料不代表員工未工作。計價匯入狀態仍須核對 Google Drive 原檔。`;
  } catch (error) {
    if (request !== worksiteBoardRequest) return;
    cards.replaceChildren();
    status.textContent = `工作台讀取失敗：${error.message}`;
  }
}

function renderWorksiteBoard(data) {
  const a = data.assignments, t = data.attendance, i = data.inspections;
  const p = data.photos, j = data.journal, s = data.sign_slips, c = data.pricing;
  const list = values => values.length ? `<ul>${values.map(value => `<li>${escapeHtml(value)}</li>`).join('')}</ul>` : '';
  const card = (title, badge, tone, detail, target) =>
    `<article class="worksite-board-card ${tone}"><div class="worksite-board-card-head"><h3>${title}</h3><span>${escapeHtml(badge)}</span></div><div class="worksite-board-detail">${detail}</div><button type="button" class="button-secondary" onclick="openWorksiteBoardItem('${target}')">前往處理</button></article>`;
  const cards = [
    card('派工', a.missing ? '未查到' : `${a.rows.length} 筆`, a.missing ? 'is-warning' : '',
      a.missing ? '<p>這天此工地尚未查到有效派工。</p>' : list(a.rows.map(row => `${row.work_item}｜${row.members.join('、') || '未指定人員'}${row.completed ? '｜已完成' : ''}`)), 'assignment-list'),
    card('打卡', t.missing ? `${t.missing_names.length} 人待核對` : `${t.rows.length} 筆`, t.missing ? 'is-warning' : '',
      `<p>此工地記錄 ${t.rows.length} 筆；以當日派工人員比對。</p>${t.missing ? list(t.missing_names.map(name => `${name}：此工地未見上班打卡，請核對其他工地或補登`)) : ''}`, 'attendance'),
    card('點檢', i.abnormal ? `${i.abnormal} 筆異常` : `${i.rows.length} 筆`, i.abnormal || i.missing ? 'is-warning' : '',
      `<p>正常 ${i.rows.length - i.abnormal}、異常 ${i.abnormal}。</p>${i.missing ? list(i.missing_machines.map(code => `${code}：今日此工地未見點檢`)) : '<p>歷史日期不以車輛目前位置推定缺漏。</p>'}`, 'inspections'),
    card('照片', `${p.count} 張`, '',
      p.count ? list(p.recent.map(row => row.name)) : '<p>尚無此施工日的工作照片；可查詢或批次匯入。</p>', 'photos'),
    card('日誌', j.saved_hours ? '工時已存' : '工時未存', j.saved_hours ? '' : 'is-warning',
      `<p>群組文字 ${j.group_text_count} 則；${j.saved_hours ? `已存工時：正常 ${j.saved_hours.normal}、加班 ${j.saved_hours.overtime}、支援 ${j.saved_hours.support}` : '尚無已存日誌工時。文字需人工篩選。'}</p>`, 'journal'),
    card('簽單', s.missing ? '待製作／核對' : `${s.rows.length} 張`, s.missing ? 'is-warning' : '',
      s.rows.length ? list(s.rows.map(row => `${row.number}｜正常 ${row.normal}、加班 ${row.overtime} 小時`)) : '<p>此日期工地尚無已保存簽單。</p>', 'sign-slips'),
    card('計價', c.state, c.state.includes('待核對') || c.state.includes('重新核對') ? 'is-warning' : '',
      `<p>資料庫匯入紀錄 ${c.import_count} 筆；不代表原 Excel 已由本畫面核實。</p>`, 'excel-editor'),
  ];
  document.getElementById('worksiteBoardCards').innerHTML = cards.join('');
}

async function openWorksiteBoardItem(target) {
  const date = document.getElementById('worksiteBoardDate').value;
  const siteId = document.getElementById('worksiteBoardSite').value;
  const site = state.worksites.find(row => String(row.id) === siteId);
  const put = (id, value) => { const input = document.getElementById(id); if (input) input.value = value; };
  if (target === 'assignment-list') { put('assignmentFilterDate', date); openNamedPanel(target); await loadAssignments(); return; }
  if (target === 'attendance') { put('attendanceFilterDate', date); put('attendanceEmployee', ''); openNamedPanel(target); await Promise.all([loadAttendance(), loadWorkReports()]); return; }
  if (target === 'inspections') { put('inspectionPeriod', 'day'); toggleInspectionPeriod(); put('inspectionFilterDate', date); put('inspectionSite', siteId); openNamedPanel(target); await loadForkliftInspections(); return; }
  if (target === 'photos') {
    put('photoFilterDateFrom', date); put('photoFilterDateTo', date);
    put('photoImportDate', date); put('photoImportSite', siteId);
    openNamedPanel(target); await loadPhotoUploads(); return;
  }
  if (target === 'journal') {
    put('journalDate', date); openNamedPanel(target); await loadWorksiteJournals();
    const option = [...document.getElementById('journalWorksite').options].find(row => row.value === siteId);
    if (option) { document.getElementById('journalWorksite').value = option.value; await changeJournalWorksite(false); }
    return;
  }
  if (target === 'sign-slips') {
    put('signSlipMonth', date.slice(0, 7)); openNamedPanel(target); await loadSignSlips();
    // Do not overwrite a dirty sign-slip editor; date/site prefill is only for a new blank form.
    if (!document.getElementById('signSlipNo').value) { put('signSlipDate', date); put('signSlipSiteCode', site?.code || ''); }
    return;
  }
  if (target === 'excel-editor') {
    openPricingGridShortcut(false);
    if (typeof costGridDirty === 'function' && costGridDirty()) {
      document.getElementById('costGridStatus').textContent = '計價月表有未保存修改；已保留原選擇，請先核對或保存，再切換日期。';
      return;
    }
    put('costGridMonth', date.slice(0, 7));
    if (document.getElementById('costGridDocument').value) await loadCostGrid();
    const matches = costGrid.data?.sheets.filter(sheet => sheet.label === site?.code) || [];
    if (matches.length === 1) { costGrid.label = matches[0].label; renderCostGrid(); }
    else document.getElementById('costGridStatus').textContent = `請核對 ${date} ${site?.name || ''} 的計價檔與標別；此工作台不會自動保存或覆寫月表。`;
  }
}
