(function (root) {
  "use strict";
  const MAX_FILES = 100;
  const MAX_BYTES = 10 * 1024 * 1024;
  function validateFiles(files) {
    if (!files.length) return "請先選擇照片。";
    if (files.length > MAX_FILES) return "每批最多 100 張照片，請分批匯入。";
    if (files.some(file => !file.size || file.size > MAX_BYTES)) return "每張照片不可空白，且最多 10 MB。";
    if (files.some(file => !/\.(jpe?g|png|webp)$/i.test(file.name))) return "僅支援 JPG、PNG、WebP，請先轉換其他格式。";
    return "";
  }
  function retryable(item) { return ["pending", "failed", "backup-warning"].includes(item.status); }
  let config, queue = [], running = false, stopped = false, batch = null, initialized = false;
  const el = id => document.getElementById(id);
  const fields = ["photoImportSite", "photoImportDate", "photoImportGroup", "photoImportAlbum", "photoImportFiles"];

  function render() {
    const list = el("photoImportResults");
    list.replaceChildren();
    queue.forEach(item => {
      const row = document.createElement("li");
      row.textContent = `${item.file.name}：${item.message || "等待匯入"}`;
      row.dataset.status = item.status;
      list.append(row);
    });
    const count = status => queue.filter(item => item.status === status).length;
    el("photoImportStatus").textContent = queue.length
      ? `共 ${queue.length} 張；完成 ${count("imported")}、重複略過 ${count("duplicate")}、失敗 ${count("failed")}、備份待確認 ${count("backup-warning")}、等待 ${count("pending")}。${running ? "正在匯入，請勿關閉頁面。" : ""}`
      : "尚未選取照片。";
    fields.forEach(id => { el(id).disabled = running || Boolean(batch); });
    el("photoImportReset").disabled = running;
    el("photoImportStop").hidden = !running;
    el("photoImportStart").disabled = running || !queue.some(retryable);
    el("photoImportStart").textContent = batch ? "繼續／重試未完成項目" : "開始匯入 Google Drive";
  }

  async function start(event) {
    event.preventDefault();
    if (running || !queue.some(retryable)) return;
    if (!batch) {
      const siteId = el("photoImportSite").value;
      const workDate = el("photoImportDate").value;
      if (!siteId || !workDate) { el("photoImportStatus").textContent = "請選擇工地與原施工日期。"; return; }
      const site = config.sites.find(item => String(item.id) === siteId);
      if (!confirm(`將 ${queue.length} 張照片匯入「${site.name}」，施工日期 ${workDate}？\n47 與 76 工地請分批選擇，不會自動判斷照片內容。`)) return;
      batch = { site_id: siteId, work_date: workDate,
        source_group: el("photoImportGroup").value, album_name: el("photoImportAlbum").value };
    }
    running = true;
    stopped = false;
    render();
    try {
      for (const item of queue.filter(retryable)) {
        if (stopped) break;
        item.message = "上傳中…";
        render();
        const data = new FormData();
        Object.entries(batch).forEach(([key, value]) => data.append(key, value));
        data.append("file", item.file, item.file.name);
        try {
          const result = await config.api("/api/photo-uploads/import", { method: "POST", body: data });
          item.status = result.status;
          item.message = result.status === "duplicate" ? "已存在，略過重複" : "已存入 Google Drive，紀錄已保存";
          if (result.backup_status !== "saved") {
            item.status = "backup-warning";
            item.message = "照片與本機紀錄已保存，但雲端資料庫備份尚未確認；請重試本項確認備份";
          }
        } catch (error) {
          item.status = "failed";
          item.message = `未確認完成：${error.message}（可重試，不會重傳已完成項目）`;
          // Authentication/configuration failures affect the whole batch.
          if (/登入|權限|授權|尚未設定|儲存空間/.test(error.message)) stopped = true;
        }
        render();
      }
    } finally {
      running = false;
      render();
      await config.refresh();
    }
  }

  function configure(options) {
    config = options;
    el("photoAlbumImport").hidden = !["owner", "admin"].includes(options.actor?.role);
    if (batch || running) return;
    const select = el("photoImportSite"), previous = select.value;
    select.replaceChildren(new Option("請選擇工地（47／76 請分批）", ""));
    options.sites.filter(site => site.is_active !== false).forEach(site => select.add(new Option(site.name, site.id)));
    select.value = previous;
    el("photoImportDate").max = options.today;
    if (!initialized) {
      initialized = true;
      el("photoImportForm").addEventListener("submit", start);
      el("photoImportFiles").addEventListener("change", event => {
        const files = Array.from(event.target.files);
        const error = validateFiles(files);
        queue = error ? [] : files.map(file => ({ file, status: "pending", message: "等待匯入" }));
        render();
        if (error) el("photoImportStatus").textContent = error;
      });
      el("photoImportStop").addEventListener("click", () => { stopped = true; el("photoImportStop").textContent = "本張完成後停止"; });
      el("photoImportReset").addEventListener("click", () => {
        queue = []; batch = null; el("photoImportFiles").value = "";
        el("photoImportStop").textContent = "停止後續上傳";
        render();
      });
      window.addEventListener("beforeunload", event => {
        if (running || queue.some(retryable)) { event.preventDefault(); event.returnValue = ""; }
      });
    }
    render();
  }
  const exported = { configure, validateFiles, retryable };
  root.PhotoAlbumImport = exported;
  if (typeof module !== "undefined") module.exports = exported;
})(typeof window !== "undefined" ? window : globalThis);
