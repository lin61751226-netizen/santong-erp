(function(root) {
  "use strict";
  const el = id => document.getElementById(id);
  let config, plan = null, request = null, busy = false, stopped = false, initialized = false;
  const fields = ["photoTextFrom", "photoTextTo", "photoTextLegacy"];
  function invalidate() { plan = null; request = null; el("photoTextApply").disabled = true; }
  function setBusy(value) {
    busy = value; fields.forEach(id => { el(id).disabled = value; });
    el("photoTextPreview").disabled = value;
    el("photoTextApply").disabled = value || !plan?.count;
    el("photoTextStop").disabled = !value;
    el("photoTextBackup").disabled = value;
  }
  function table(id, rows) {
    const body = el(id); body.replaceChildren();
    rows.forEach(values => {
      const tr = document.createElement("tr");
      values.forEach(value => { const td = document.createElement("td"); td.textContent = String(value); tr.append(td); });
      body.append(tr);
    });
  }
  async function preview() {
    plan = await config.api("/api/photo-uploads/classify/preview", {method:"POST", body:JSON.stringify(request)});
    table("photoTextSummary", plan.summary.map(r => [r.site_name,r.work_date,r.count]));
    table("photoTextRows", plan.photos.map(r => [r.file_name,r.caption,r.legacy ? "舊紀錄：發送者與時間" : "群組ID＋發送者＋時間"]));
    return plan;
  }
  async function read(event) {
    event.preventDefault(); if (busy) return;
    invalidate(); request = {date_from:el("photoTextFrom").value, date_to:el("photoTextTo").value, allow_legacy:el("photoTextLegacy").checked};
    setBusy(true); el("photoTextStatus").textContent = "正在核對群組文字與未分類照片…";
    try {
      const p = await preview();
      el("photoTextStatus").textContent = `可分類 ${p.count} 張，其中舊紀錄配對 ${p.legacy_count} 張。請先核對每張照片依據的文字。`;
    } catch(e) { invalidate(); el("photoTextStatus").textContent=e.message; }
    finally { setBusy(false); }
  }
  async function apply() {
    if (busy || !plan?.count || !request) return;
    if (!confirm(`依預覽文字分類 ${plan.count} 張照片，並移動原檔到工地／月份／施工日？\n不重傳、不改檔名或分享權限；每張保留修改紀錄。`)) return;
    setBusy(true); stopped = false; let saved = 0;
    try {
      while (plan.count && !stopped) {
        el("photoTextStatus").textContent = `已歸檔 ${saved} 張，尚有 ${plan.count} 張；每批最多20張，請勿關閉頁面。`;
        const result = await config.api("/api/photo-uploads/classify/apply", {method:"POST", body:JSON.stringify({...request,digest:plan.digest})});
        saved += result.saved;
        const failed = result.results.filter(r => r.status === "failed");
        if (result.backup_status !== "saved" || failed.length || !result.saved) {
          throw Error(`已歸檔 ${saved} 張；` + (failed.length ? `${failed.length}張未完成：${failed[0].detail}。` : "") + (result.backup_status !== "saved" ? "雲端資料庫備份未確認。" : "") + "請重新預覽後接續，不需重傳照片。");
        }
        await preview();
      }
      el("photoTextStatus").textContent = `已歸檔 ${saved} 張，雲端資料庫備份成功。` + (stopped ? "已停止後續批次，可重新預覽接續。" : "原檔連結、上傳時間及LINE綁定均保留。");
      await config.refresh();
    } catch(e) { el("photoTextStatus").textContent=e.message; invalidate(); }
    finally { setBusy(false); }
  }
  function configure(options) {
    config=options; el("photoTextClassification").hidden=!["owner","admin"].includes(options.actor?.role);
    if (initialized) return; initialized=true;
    el("photoTextFrom").value=options.today; el("photoTextTo").value=options.today;
    el("photoTextForm").addEventListener("submit",read);
    el("photoTextApply").addEventListener("click",apply);
    el("photoTextStop").addEventListener("click",()=>{stopped=true;});
    el("photoTextBackup").addEventListener("click",async()=>{
      if(busy)return;setBusy(true);
      try { const r=await config.api("/api/photo-uploads/classify/backup",{method:"POST"});
        el("photoTextStatus").textContent=r.status==="saved"?"雲端資料庫備份成功。":"雲端資料庫備份仍未確認，請勿重新部署。";
      } catch(e) { el("photoTextStatus").textContent=e.message; } finally {setBusy(false);}
    });
    fields.forEach(id=>{ el(id).addEventListener("input",invalidate); el(id).addEventListener("change",invalidate); });
    window.addEventListener("beforeunload",event=>{if(busy){event.preventDefault();event.returnValue="";}});
  }
  root.PhotoTextClassification={configure};
})(typeof window!=="undefined"?window:globalThis);
