/* Drafts are private recovery copies, never automatic business submissions. */
window.DraftRecovery = (() => {
  const kinds = ['sign-slip', 'cost-grid'];
  let actorCode = null, epoch = 0, paused = true;
  let entries = {}, gridLocks = [];
  const fields = ['signSlipId','signSlipNo','signSlipDate','signSlipCustomer','signSlipSiteCode',
    'signSlipLocation','signSlipV25','signSlipV30','signSlipV45','signSlipTruck','signSlipNormal',
    'signSlipOvertime','signSlipTotal','signSlipStart','signSlipEnd','signSlipAmount','signSlipDrivers',
    'signSlipSignature','signSlipContent','signSlipSource','signSlipNotes'];
  const id = kind => kind === 'sign-slip' ? 'signSlip' : 'costGrid';
  const element = (kind, suffix) => document.getElementById(id(kind) + suffix);
  function status(kind, text) { if (element(kind, 'DraftStatus')) element(kind, 'DraftStatus').textContent = text; }
  function controls(kind) {
    const item = entries[kind];
    if (!item) return;
    element(kind, 'DraftRestore').hidden = !item.remote?.active;
    element(kind, 'DraftDiscard').hidden = !item.remote?.active && !item.dirty;
  }
  function signPayload() {
    const raw = Object.fromEntries(fields.map(key => [key, document.getElementById(key).value]));
    const original = signSlipCache.find(row => String(row.id) === raw.signSlipId);
    return {fields:raw, vehicle_hours:signSlipVehicleDetails(), base_updated_at:original?.updated_at || null};
  }
  function capture(kind, payload) {
    const item = entries[kind];
    if (paused || !item) return;
    item.dirty = payload;
    clearTimeout(item.timer);
    if (item.remote?.active && !item.accepted) {
      status(kind, '有未處理的舊草稿；請先恢復或捨棄。畫面新輸入尚未存草稿。'); controls(kind); return;
    }
    status(kind, '尚未正式保存；正在等待自動存草稿…'); controls(kind);
    item.timer = setTimeout(() => flush(kind), 1800);
  }
  async function flush(kind) {
    const item = entries[kind];
    if (paused || !item || item.conflict || !item.dirty || item.remote?.active && !item.accepted) return;
    clearTimeout(item.timer);
    if (item.flight) { await item.flight; if (entries[kind] === item && item.dirty) return flush(kind); return; }
    const payload = item.dirty, token = epoch;
    item.flight = (async () => {
      try {
        const result = await api(`/api/editing-drafts/${kind}`, {method:'PUT',body:JSON.stringify({expected_revision:item.revision,payload})});
        if (token !== epoch) return;
        item.revision = result.revision; item.remote = result; item.accepted = true;
        if (JSON.stringify(item.dirty) === JSON.stringify(payload)) item.dirty = null;
        status(kind, `草稿已存系統（${formatTW(result.saved_at)}）；尚未入帳，雲端備份待排程或按「備份草稿」。`);
      } catch (error) {
        item.conflict = String(error.message).includes('分頁');
        status(kind, `草稿未確認保存：${error.message}。請保留此頁，勿重複送出正式資料。`);
      } finally { item.flight = null; if (token === epoch) controls(kind); }
    })();
    await item.flight;
    if (!paused && !item.conflict && item.dirty && JSON.stringify(item.dirty) !== JSON.stringify(payload)) await flush(kind);
  }
  function captureSign() { capture('sign-slip', signPayload()); }
  function captureGrid() {
    if (!costGrid.data || !costGridDirty()) return;
    const adjustmentRow = costGrid.adjustmentDraft?.dirty ? costGrid.data.sheets.find(row=>row.label===costGrid.label)?.rows.find(row=>row.day===costGrid.adjustmentDraft.day) : null;
    capture('cost-grid', {document_id:costGrid.data.document_id, year:costGrid.data.year,month:costGrid.data.month,
      label:costGrid.label,sha256:costGrid.data.sha256, edits:[...costGrid.edits],source_slip:costGrid.sourceSlip,
      reason:document.getElementById('costGridSlipReasonInput').value,
      adjustment:adjustmentRow ? {day:adjustmentRow.day,expected_revision:adjustmentRow.adjustment_revision || 0,
        manual_total_amount:document.getElementById('costGridAdjustmentAmount').value,
        reason:document.getElementById('costGridAdjustmentReason').value} : null});
  }
  function wipeEditors() {
    resetSignSlipForm();
    costGrid.edits.clear(); costGrid.data = null; costGrid.label = null; costGrid.sourceSlip = null;
    costGrid.adjustmentDraft = null; costGridReviewReset();
    document.getElementById('costGridTabs').innerHTML = '';
    document.getElementById('costGridSheet').innerHTML = '';
    document.getElementById('costGridAdjustment').hidden = true;
    document.getElementById('costGridSlipAmountReason').hidden = true;
  }
  function pause() {
    paused = true;
    epoch++;
    kinds.forEach(kind => { if(entries[kind]) clearTimeout(entries[kind].timer); });
    wipeEditors();
  }
  async function configure(actor) {
    const code = actor?.employee_code;
    if (!code) return;
    const same = code === actorCode, old = entries;
    paused = true; const token = ++epoch;
    kinds.forEach(kind => { if(old[kind]) clearTimeout(old[kind].timer); });
    if (!same) wipeEditors();
    actorCode = code; entries = {};
    await Promise.all(kinds.map(async kind => {
      try {
        const remote = await api(`/api/editing-drafts/${kind}`);
        if (token !== epoch) return;
        const retained = same ? old[kind]?.dirty : null;
        const sameRevision = old[kind]?.revision === remote.revision;
        entries[kind] = {revision:remote.revision,remote,dirty:retained,accepted:!remote.active || Boolean(retained && sameRevision)};
        status(kind, remote.active ? `找到未完成草稿（${formatTW(remote.saved_at)}），請選擇恢復或捨棄；不會自動入帳。` : '沒有待恢復草稿。輸入後會自動存草稿，不等於正式保存。');
        controls(kind);
      } catch(error) { if(token === epoch) status(kind,`草稿載入失敗：${error.message}；不要假設已有草稿。`); }
    }));
    if(token !== epoch) return;
    paused = false;
    kinds.forEach(kind => { if(entries[kind]?.dirty && entries[kind].accepted) flush(kind); });
  }
  async function restore(kind) {
    const item = entries[kind];
    if (!item?.remote?.active || paused) return;
    if (item.dirty && !confirm('恢復草稿會替換目前未保存的畫面輸入，確定嗎？')) return;
    const data = item.remote.payload;
    const token = epoch;
    try {
      if (kind === 'sign-slip') {
        if (data.fields.signSlipId) {
          const day = data.fields.signSlipDate;
          const rows = await api(`/api/sign-slips?year=${day.slice(0,4)}&month=${Number(day.slice(5,7))}`);
          if (token !== epoch || paused) return;
          const current = rows.find(row => String(row.id) === data.fields.signSlipId);
          if (!current || current.updated_at !== data.base_updated_at) throw new Error('正式簽單已變更，請先重新讀取原簽單，舊草稿不能直接套用');
          signSlipCache = rows;
        }
        fields.forEach(key => { document.getElementById(key).value = data.fields[key] || ''; });
        document.getElementById('signSlipVehicleHoursSection').hidden = true;
        ['signSlipNormal','signSlipOvertime','signSlipTotal'].forEach(key=>{ document.getElementById(key).readOnly = false; });
        if(data.vehicle_hours?.length) renderSignSlipVehicleHours(data.vehicle_hours);
        openNamedPanel('sign-slips');
      } else {
        if(costGrid.busy) throw new Error('月表正在保存，請稍後再恢復草稿');
        if(costGridDirty() && !confirm('恢復前須重新讀取計價來源，放棄目前未保存修改嗎？')) return;
        costGrid.edits.clear(); costGrid.adjustmentDraft = null;
        openPricingGridShortcut(false);
        document.getElementById('costGridDocument').value = String(data.document_id);
        document.getElementById('costGridMonth').value = `${data.year}-${String(data.month).padStart(2,'0')}`;
        await loadCostGrid();
        if (token !== epoch || paused) return;
        if(!costGrid.data || costGrid.data.document_id !== data.document_id || costGrid.data.year !== data.year || costGrid.data.month !== data.month || costGrid.data.sha256 !== data.sha256) throw new Error('計價來源版本已變或無法讀取；草稿保留，不覆蓋新資料');
        const sheet = costGrid.data.sheets.find(row=>row.label === data.label);
        if(!sheet || data.edits.some(([day])=>!sheet.rows.some(row=>row.day===day))) throw new Error('草稿日期或標別不存在');
        costGrid.label=data.label; costGrid.edits=new Map(data.edits); costGrid.sourceSlip=data.source_slip;
        document.getElementById('costGridSlipReasonInput').value=data.reason || '';
        document.getElementById('costGridSlipAmountReason').hidden=!data.source_slip;
        renderCostGrid(); costGridReviewReset();
        if(data.adjustment) {
          const row=sheet.rows.find(row=>row.day===data.adjustment.day);
          if(!row || (row.adjustment_revision || 0) !== data.adjustment.expected_revision) throw new Error('核定金額版號已變，請重新核對');
          openCostGridAdjustment(data.adjustment.day);
          document.getElementById('costGridAdjustmentAmount').value=data.adjustment.manual_total_amount ?? '';
          document.getElementById('costGridAdjustmentReason').value=data.adjustment.reason || '';
          editCostGridAdjustment();
        }
      }
      item.accepted=true; item.dirty=null; controls(kind);
      status(kind,'草稿已恢復到畫面，尚未正式保存；月表必須重新預覽與勾選確認。');
    } catch(error) { status(kind,`未恢復：${error.message}`); }
  }
  async function close(kind, ask=true) {
    const item=entries[kind];
    if(!item || paused) return;
    if(ask && !confirm('捨棄這份草稿？歷史修訂仍保留，不會刪除正式資料。')) return;
    clearTimeout(item.timer);
    const token = epoch;
    if(item.flight) await item.flight;
    if(token !== epoch || paused) return;
    try {
      const result=await api(`/api/editing-drafts/${kind}`,{method:'PUT',body:JSON.stringify({expected_revision:item.revision,active:false})});
      if(token !== epoch || paused) return;
      item.revision=result.revision; item.remote=result; item.dirty=null; item.accepted=true; item.conflict=false;
      status(kind,'草稿已關閉，歷史修訂保留；正式資料不受影響。'); controls(kind);
    } catch(error) { status(kind,`草稿尚未關閉：${error.message}；請勿因此重複送出正式資料。`); }
  }
  async function backup(kind) {
    if(paused) return;
    await flush(kind);
    if(entries[kind]?.dirty || entries[kind]?.conflict) { status(kind,'尚有未存草稿，請先排除錯誤再備份。'); return; }
    status(kind,'正在備份草稿與資料庫到 Google Drive…');
    try {
      const result=await api('/api/editing-drafts/backup/cloud',{method:'POST'});
      status(kind,result.backup_status==='saved' ? '資料庫雲端備份成功；草稿仍不是正式資料。' : '草稿已存系統，但雲端備份未完成；請勿重新部署。');
    } catch(error) { status(kind,`雲端備份未完成：${error.message}`); }
  }
  function formal(kind, phase, backupStatus) {
    if(kind==='cost-grid') {
      if(phase==='saving' && !gridLocks.length) {
        gridLocks=[...document.querySelectorAll('#costGridSection input,#costGridSection button,#costGridSection select')].map(el=>[el,el.disabled]);
        gridLocks.forEach(([el])=>{el.disabled=true;});
      } else if(phase!=='saving') { gridLocks.forEach(([el,disabled])=>{el.disabled=disabled;}); gridLocks=[]; }
    }
    const target=element(kind,'SaveStatus'); if(!target) return;
    const messages={saving:'正式資料保存中，請勿重複送出。',failed:'尚未確認正式保存成功；輸入與草稿保留，請先重新查詢。',
      saved:kind==='sign-slip' ? '簽單已存系統；計價月表仍須核對，不代表已入帳。' : '計價月表已寫入 Google Drive；系統稽核已保存。'};
    target.textContent=messages[phase] || phase;
    if(phase==='saved') target.textContent += backupStatus==='saved' ? ' 資料庫雲端備份完成。' : ' 資料庫雲端備份未完成，請重試。';
  }
  document.getElementById('signSlipForm').addEventListener('input',captureSign);
  document.getElementById('costGridSlipReasonInput').addEventListener('input',captureGrid);
  window.addEventListener('beforeunload',event=>{
    if(Object.values(entries).some(item=>item.dirty || item.flight)) { event.preventDefault(); event.returnValue=''; }
  });
  return {configure,pause,captureSign,captureGrid,restore,close,backup,formal};
})();
