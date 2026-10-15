(function (root) {
  "use strict";
  let config, initialized = false, busy = false, preview = null, payload = null;
  const el = id => document.getElementById(id);
  const fields = ["drivePhotoFolder", "drivePhotoSite", "drivePhotoCode", "drivePhotoGroup"];
  function invalidate() {
    preview = null; payload = null;
    el("drivePhotoApply").disabled = true;
    el("drivePhotoDates").replaceChildren();
    el("drivePhotoExceptions").textContent = "";
  }
  function setBusy(value) {
    busy = value;
    fields.forEach(id => { el(id).disabled = value; });
    el("drivePhotoPreview").disabled = value;
    el("drivePhotoApply").disabled = value || !preview;
  }
  function renderPlan(data) {
    const body = el("drivePhotoDates"); body.replaceChildren();
    data.dates.forEach(item => {
      const tr = document.createElement("tr");
      [item.work_date, item.count].forEach(text => { const td = document.createElement("td"); td.textContent = String(text); tr.append(td); });
      body.append(tr);
    });
    el("drivePhotoExceptions").textContent = data.exception_count
      ? `共 ${data.exception_count} 項略過／衝突（下列最多顯示50項）：` + data.exceptions.map(item => `${item.name}：${item.reason}`).join("；")
      : "沒有日期或工地例外。";
  }
  async function readPreview(event) {
    event.preventDefault(); if (busy) return;
    invalidate();
    const request = {folder_url:el("drivePhotoFolder").value.trim(), site_id:Number(el("drivePhotoSite").value),
      filename_code:el("drivePhotoCode").value.trim(), source_group:el("drivePhotoGroup").value.trim()};
    if (!request.site_id) { el("drivePhotoFeedback").textContent = "請先選擇歸屬工地。"; return; }
    setBusy(true); el("drivePhotoFeedback").textContent = "正在讀取 Drive 原檔中繼資料，不會下載或修改照片，請稍候…";
    try {
      const data = await config.api("/api/photo-uploads/drive/preview", {method:"POST", body:JSON.stringify(request)});
      preview = data; payload = request; renderPlan(data);
      el("drivePhotoFeedback").textContent = `${data.site_name}：將新增 ${data.counts.new} 張、重複 ${data.counts.duplicate} 張、略過 ${data.counts.skipped} 項、衝突 ${data.counts.conflict} 項。請核對下方施工日期。`;
    } catch (error) { el("drivePhotoFeedback").textContent = error.message; }
    finally { setBusy(false); }
  }
  async function apply() {
    if (busy || !preview || !payload) return;
    if (!confirm(`確認依檔名施工日期，將 ${preview.counts.new} 張照片建立到「${preview.site_name}」？\n不搬移、不改名、不重傳照片。其他工地、日期不完整與衝突項目不會寫入。`)) return;
    setBusy(true); el("drivePhotoFeedback").textContent = "重新核對原檔、備份現有資料並建立紀錄，請勿關閉頁面…";
    try {
      const result = await config.api("/api/photo-uploads/drive/apply", {method:"POST", body:JSON.stringify({...payload, digest:preview.digest})});
      const backupOk = result.backup_status === "saved";
      el("drivePhotoFeedback").textContent = `已新增 ${result.added} 張照片紀錄；原 Drive 照片未異動。` + (backupOk
        ? "雲端資料庫備份成功。" : "資料已保存，但雲端備份未確認；請重新預覽並保存以重試備份，不需重傳。" );
      invalidate(); await config.refresh();
    } catch (error) { el("drivePhotoFeedback").textContent = error.message; invalidate(); }
    finally { setBusy(false); }
  }
  function configure(options) {
    config = options;
    el("drivePhotoIndex").hidden = !["owner","admin"].includes(options.actor?.role);
    if (busy) return;
    const select = el("drivePhotoSite"), old = select.value;
    select.replaceChildren(new Option("請選擇工地", ""));
    options.sites.filter(site => site.is_active !== false).forEach(site => select.add(new Option(site.name, site.id)));
    select.value = old;
    if (!initialized) {
      initialized = true;
      el("drivePhotoIndexForm").addEventListener("submit", readPreview);
      el("drivePhotoApply").addEventListener("click", apply);
      fields.forEach(id => { el(id).addEventListener("input", invalidate); el(id).addEventListener("change", invalidate); });
      window.addEventListener("beforeunload", event => { if (busy) { event.preventDefault(); event.returnValue = ""; } });
    }
  }
  root.DrivePhotoIndex = {configure};
})(typeof window !== "undefined" ? window : globalThis);
