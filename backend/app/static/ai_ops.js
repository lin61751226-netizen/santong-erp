function aiOpsBackupMessage(status) {
  if (status === "saved" || status === "no_change") return "Google Drive 資料庫備份成功。";
  return "已存入目前資料庫，但雲端備份未完成。成功前不要重新部署，可稍後再操作一次以重試備份。";
}

function aiOpsErrorText(error) {
  const message = error && error.message ? error.message : "操作失敗";
  if (message.startsWith("[") || message.startsWith("{")) return "欄位不完整，請修正後再存。";
  return message;
}

function selectedAiSite() {
  const sites = state.worksiteJournals?.sites || [];
  return sites.find(site => journalSiteKey(site) === state.selectedJournalSiteKey) || null;
}

function aiOpsDraftQuery() {
  const site = selectedAiSite();
  const workDate = state.worksiteJournals?.date || document.getElementById("journalDate").value;
  if (!site?.site_id || !workDate) return null;
  return {worksite_id: site.site_id, work_date: workDate, site_name: site.site_name};
}

async function refreshAiOpsPanel() {
  const host = document.getElementById("aiOpsJournal");
  if (!host) return;
  const query = aiOpsDraftQuery();
  if (!query) {
    host.innerHTML = "";
    return;
  }
  try {
    const [journal, slip, status] = await Promise.all([
      api(`/api/ai-ops/journals/drafts?work_date=${query.work_date}&worksite_id=${query.worksite_id}`),
      api(`/api/ai-ops/sign-slips/drafts?work_date=${query.work_date}&worksite_id=${query.worksite_id}`),
      api("/api/ai-ops/status"),
    ]);
    host.innerHTML = renderAiJournal(journal, status) + renderAiSignSlip(slip);
  } catch (error) {
    host.innerHTML = `<div class="ai-ops-box">${escapeHtml(aiOpsErrorText(error))}</div>`;
  }
}

function renderSourceList(sourceRefs) {
  const snippets = sourceRefs?.snippets || {};
  const labels = {
    group_text: "群組文字",
    photo: "照片",
    attendance: "打卡",
    assignment: "派工",
    report: "工作回報",
    inspection: "點檢",
  };
  const items = Object.entries(labels).flatMap(([key, label]) => (snippets[key] || []).map(row => {
    const text = row.content || row.note || row.work_item || row.file_name || row.event_type || row.forklift_code || "";
    const link = row.drive_url ? ` <a href="${escapeAttr(row.drive_url)}" target="_blank" rel="noopener">開啟</a>` : "";
    return `<li>${label} #${row.id} ${escapeHtml(text)}${link}</li>`;
  }));
  if (!items.length) return "<p class=\"muted\">這份草稿沒有可對照的來源。</p>";
  return `<details open><summary>來源</summary><ul class="ai-ops-sources">${items.join("")}</ul></details>`;
}

function renderAiJournal(payload, status) {
  const draft = payload.draft;
  const flag = status.ai_ops_enabled
    ? "AI 草稿已啟用。核准不會寫入計價工時。"
    : "AI 未開啟。仍可用現有派工與群組資料整理草稿，手動日誌不受影響。";
  if (!draft) {
    return `<section class="ai-ops-box"><h3>工作日誌草稿</h3><p>${flag}</p><p class="muted">按「整理 AI 草稿」產生這天工地的草稿。</p></section>`;
  }
  const content = draft.content || {};
  const flags = (content.review_flags || []).map(item => `<p class="ai-ops-flag">${escapeHtml(item)}</p>`).join("");
  const locked = draft.status !== "draft";
  return `<section class="ai-ops-box">
    <h3>工作日誌草稿｜${escapeHtml(draft.status_label)}</h3>
    <p>${flag}</p>
    <p class="muted">${escapeHtml(draft.hours_basis || "")}</p>
    ${flags}
    <label>工作摘要<textarea id="aiJournalSummary" rows="4" ${locked ? "disabled" : ""}>${escapeHtml(content.work_summary || "")}</textarea></label>
    <div class="ai-ops-grid">
      <label>正常工時<input id="aiJournalNormal" type="number" min="0" step="0.5" value="${draft.normal_hours}" ${locked ? "disabled" : ""}></label>
      <label>加班工時<input id="aiJournalOvertime" type="number" min="0" step="0.5" value="${draft.overtime_hours}" ${locked ? "disabled" : ""}></label>
      <label>支援工時<input id="aiJournalSupport" type="number" min="0" step="0.5" value="${draft.support_hours}" ${locked ? "disabled" : ""}></label>
    </div>
    <p>人員：${escapeHtml((content.workers || []).join("、") || "沒有對到人員")}</p>
    <p>機具：${escapeHtml((content.equipment || []).join("、") || "沒有對到機具")}</p>
    <p>工作：${escapeHtml((content.work_items || []).join("、") || "沒有工作內容")}</p>
    ${renderSourceList(draft.source_refs)}
    <div class="button-row">
      ${locked ? "" : `<button type="button" onclick="saveAiJournal(${draft.id})">儲存草稿修改</button>
        <button type="button" onclick="approveAiJournal(${draft.id})">核准草稿</button>
        <button type="button" class="button-secondary" onclick="rejectAiJournal(${draft.id})">退回</button>`}
      ${draft.status === "approved" ? `<button type="button" class="button-secondary" onclick="draftAiJournal(true)">另開新草稿</button>` : ""}
    </div>
  </section>`;
}

function renderAiSignSlip(payload) {
  const draft = payload.draft;
  if (!draft) {
    return `<section class="ai-ops-box"><h3>簽單預填</h3><p class="muted">按「預填簽單」。不確定的司機、客戶或金額會標出來，不會猜，也不會直接建立正式簽單。</p></section>`;
  }
  const locked = draft.status !== "draft";
  const vehicles = draft.vehicles || {};
  const flags = (draft.uncertainties || []).map(item =>
    `<p class="ai-ops-flag ${item.level === "block" ? "is-block" : ""}">${escapeHtml(item.message)}</p>`).join("");
  const warnings = (draft.uncertainties || []).some(item => item.level === "warn");
  return `<section class="ai-ops-box">
    <h3>簽單預填｜${escapeHtml(draft.status_label)}</h3>
    <p class="muted">${escapeHtml(draft.amount_basis || "")}</p>
    ${flags}
    <div class="ai-ops-grid">
      <label>客戶<input id="aiSlipCustomer" value="${escapeAttr(draft.customer_name || "")}" ${locked ? "disabled" : ""}></label>
      <label>司機<input id="aiSlipDrivers" value="${escapeAttr(draft.driver_names || "")}" ${locked ? "disabled" : ""}></label>
      <label>金額（元）<input id="aiSlipAmount" type="number" min="0" step="1" value="${draft.amount ?? ""}" ${locked ? "disabled" : ""}></label>
      <label>開始<input id="aiSlipStart" value="${escapeAttr(draft.start_time || "")}" placeholder="08:00" ${locked ? "disabled" : ""}></label>
      <label>結束<input id="aiSlipEnd" value="${escapeAttr(draft.end_time || "")}" placeholder="17:00" ${locked ? "disabled" : ""}></label>
      <label>正常工時<input id="aiSlipNormal" type="number" min="0" step="0.5" value="${draft.normal_hours}" ${locked ? "disabled" : ""}></label>
      <label>加班工時<input id="aiSlipOvertime" type="number" min="0" step="0.5" value="${draft.overtime_hours}" ${locked ? "disabled" : ""}></label>
      <label>2.5噸<input id="aiSlipV25" type="number" min="0" step="1" value="${vehicles.twoPointFive || 0}" ${locked ? "disabled" : ""}></label>
      <label>3.0噸<input id="aiSlipV30" type="number" min="0" step="1" value="${vehicles.threePointZero || 0}" ${locked ? "disabled" : ""}></label>
      <label>4.5噸<input id="aiSlipV45" type="number" min="0" step="1" value="${vehicles.fourPointFive || 0}" ${locked ? "disabled" : ""}></label>
      <label>貨車<input id="aiSlipTruck" type="number" min="0" step="1" value="${vehicles.truck || 0}" ${locked ? "disabled" : ""}></label>
    </div>
    <label>工作內容<textarea id="aiSlipContent" rows="4" ${locked ? "disabled" : ""}>${escapeHtml(draft.work_content || "")}</textarea></label>
    ${locked ? `<p>已對應正式簽單編號 ${draft.sign_slip_id || "—"}。</p>` : `<div class="ai-ops-grid">
      <label>紙本單號<input id="aiSlipNo" placeholder="0002761"></label>
      ${warnings ? `<label><input id="aiSlipAck" type="checkbox"> 我已核對上方待確認項目</label>` : ""}
    </div>`}
    <div class="button-row">
      ${locked ? "" : `<button type="button" class="button-secondary" onclick="saveAiSignSlip(${draft.id})">儲存預填修改</button>
        <button type="button" onclick="confirmAiSignSlip(${draft.id})">確認並建立簽單</button>`}
    </div>
  </section>`;
}

async function draftAiJournal(openNew) {
  const feedback = document.getElementById("journalFeedback");
  const query = aiOpsDraftQuery();
  if (!query) {
    feedback.textContent = "請先整理工作日誌並選擇工地。";
    return;
  }
  feedback.textContent = `正在整理 ${query.site_name} 的工作日誌草稿。`;
  try {
    const data = await api("/api/ai-ops/journals/drafts", {
      method: "POST",
      body: JSON.stringify({work_date: query.work_date, worksite_id: query.worksite_id, open_new: openNew}),
    });
    feedback.textContent = `${data.message} ${aiOpsBackupMessage(data.backup_status)}`;
    await refreshAiOpsPanel();
  } catch (error) {
    feedback.textContent = aiOpsErrorText(error);
  }
}

function aiJournalBody(draftId) {
  return {
    work_summary: document.getElementById("aiJournalSummary").value,
    workers: [],
    equipment: [],
    work_items: [],
    quantities: [],
    issues: [],
    normal_hours: Number(document.getElementById("aiJournalNormal").value || 0),
    overtime_hours: Number(document.getElementById("aiJournalOvertime").value || 0),
    support_hours: Number(document.getElementById("aiJournalSupport").value || 0),
  };
}

async function saveAiJournal(draftId) {
  const feedback = document.getElementById("journalFeedback");
  const current = await api(`/api/ai-ops/journals/drafts?${new URLSearchParams(aiOpsDraftQuery())}`);
  const content = current.draft?.content || {};
  const body = aiJournalBody(draftId);
  body.workers = content.workers || [];
  body.equipment = content.equipment || [];
  body.work_items = content.work_items || [];
  body.quantities = content.quantities || [];
  body.issues = content.issues || [];
  try {
    const data = await api(`/api/ai-ops/journals/drafts/${draftId}`, {method: "PUT", body: JSON.stringify(body)});
    feedback.textContent = `${data.message} ${aiOpsBackupMessage(data.backup_status)}`;
    await refreshAiOpsPanel();
  } catch (error) {
    feedback.textContent = aiOpsErrorText(error);
  }
}

async function approveAiJournal(draftId) {
  const feedback = document.getElementById("journalFeedback");
  try {
    const data = await api(`/api/ai-ops/journals/drafts/${draftId}/approve`, {method: "POST"});
    feedback.textContent = `${data.message} ${aiOpsBackupMessage(data.backup_status)}`;
    await refreshAiOpsPanel();
  } catch (error) {
    feedback.textContent = aiOpsErrorText(error);
  }
}

async function rejectAiJournal(draftId) {
  const feedback = document.getElementById("journalFeedback");
  try {
    const data = await api(`/api/ai-ops/journals/drafts/${draftId}/reject`, {method: "POST"});
    feedback.textContent = `${data.message} ${aiOpsBackupMessage(data.backup_status)}`;
    await refreshAiOpsPanel();
  } catch (error) {
    feedback.textContent = aiOpsErrorText(error);
  }
}

async function draftAiSignSlip(openNew) {
  const feedback = document.getElementById("journalFeedback");
  const query = aiOpsDraftQuery();
  if (!query) {
    feedback.textContent = "請先整理工作日誌並選擇工地。";
    return;
  }
  const documentId = Number(document.getElementById("costMonthDocument")?.value || document.getElementById("costImportDocument")?.value || 0);
  feedback.textContent = `正在預填 ${query.site_name} 的簽單。`;
  try {
    const data = await api("/api/ai-ops/sign-slips/drafts", {
      method: "POST",
      body: JSON.stringify({
        work_date: query.work_date,
        worksite_id: query.worksite_id,
        document_id: documentId || null,
        open_new: openNew,
      }),
    });
    feedback.textContent = `${data.message} ${aiOpsBackupMessage(data.backup_status)}`;
    await refreshAiOpsPanel();
  } catch (error) {
    feedback.textContent = aiOpsErrorText(error);
  }
}

function aiSlipBody() {
  const amountText = document.getElementById("aiSlipAmount").value;
  return {
    customer_name: document.getElementById("aiSlipCustomer").value,
    work_content: document.getElementById("aiSlipContent").value,
    driver_names: document.getElementById("aiSlipDrivers").value,
    normal_hours: Number(document.getElementById("aiSlipNormal").value || 0),
    overtime_hours: Number(document.getElementById("aiSlipOvertime").value || 0),
    support_hours: 0,
    start_time: document.getElementById("aiSlipStart").value,
    end_time: document.getElementById("aiSlipEnd").value,
    amount: amountText === "" ? null : Number(amountText),
    vehicles: {
      twoPointFive: Number(document.getElementById("aiSlipV25").value || 0),
      threePointZero: Number(document.getElementById("aiSlipV30").value || 0),
      fourPointFive: Number(document.getElementById("aiSlipV45").value || 0),
      truck: Number(document.getElementById("aiSlipTruck").value || 0),
    },
  };
}

async function persistAiSignSlip(draftId) {
  const query = aiOpsDraftQuery();
  const current = await api(`/api/ai-ops/sign-slips/drafts?work_date=${query.work_date}&worksite_id=${query.worksite_id}`);
  const body = aiSlipBody();
  body.support_hours = Number(current.draft?.support_hours || 0);
  return api(`/api/ai-ops/sign-slips/drafts/${draftId}`, {method: "PUT", body: JSON.stringify(body)});
}

async function saveAiSignSlip(draftId) {
  const feedback = document.getElementById("journalFeedback");
  try {
    const data = await persistAiSignSlip(draftId);
    feedback.textContent = `${data.message} ${aiOpsBackupMessage(data.backup_status)}`;
    await refreshAiOpsPanel();
  } catch (error) {
    feedback.textContent = aiOpsErrorText(error);
  }
}

async function confirmAiSignSlip(draftId) {
  const feedback = document.getElementById("journalFeedback");
  const slipNo = document.getElementById("aiSlipNo").value;
  const acknowledged = Boolean(document.getElementById("aiSlipAck")?.checked);
  try {
    const saved = await persistAiSignSlip(draftId);
    const data = await api(`/api/ai-ops/sign-slips/drafts/${draftId}/confirm`, {
      method: "POST",
      body: JSON.stringify({slip_no: slipNo, acknowledge_uncertainties: acknowledged}),
    });
    feedback.textContent = `${saved.message} ${data.message} ${aiOpsBackupMessage(data.backup_status)}`;
    await refreshAiOpsPanel();
    if (state.worksiteJournals?.date) document.getElementById("signSlipMonth").value = state.worksiteJournals.date.slice(0, 7);
    if (typeof loadSignSlips === "function") await loadSignSlips();
    if (typeof openDocumentTask === "function") openDocumentTask(4);
  } catch (error) {
    feedback.textContent = aiOpsErrorText(error);
  }
}

async function runAiBillingCheck() {
  const feedback = document.getElementById("aiOpsBillingFeedback");
  const host = document.getElementById("aiOpsBilling");
  const month = document.getElementById("costMonthMonth").value;
  const documentId = Number(document.getElementById("costMonthDocument").value || 0);
  if (!month) {
    feedback.textContent = "請先選擇作業月份。";
    return;
  }
  feedback.textContent = "正在核對這個月的日誌、簽單、打卡與已寫入工時。";
  try {
    const data = await api("/api/ai-ops/billing/checks", {
      method: "POST",
      body: JSON.stringify({month, document_id: documentId || null}),
    });
    feedback.textContent = `${data.message} ${aiOpsBackupMessage(data.backup_status)}`;
    host.innerHTML = renderBillingCheck(data.check);
  } catch (error) {
    feedback.textContent = aiOpsErrorText(error);
  }
}

function renderBillingCheck(check) {
  if (!check) return "";
  const report = check.report || {};
  const rows = report.rows || [];
  const body = rows.map(row => {
    const text = row.ai_explanation || row.explanation;
    const fix = row.ai_suggested_fix || row.suggested_fix;
    return `<tr>
      <td>${escapeHtml(row.work_date)}</td>
      <td>${escapeHtml(row.site_name || "")}${row.label ? `<br><span class="muted">${escapeHtml(row.label)}</span>` : ""}</td>
      <td>${escapeHtml(row.severity)}</td>
      <td>${escapeHtml(text)}</td>
      <td>${escapeHtml(fix)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="5">沒有差異。</td></tr>`;
  return `<p>${escapeHtml(report.summary || "")} ${escapeHtml(report.workbook_note || "")}</p>
    <div class="table-wrap"><table>
      <thead><tr><th>日期</th><th>工地</th><th>程度</th><th>說明</th><th>建議</th></tr></thead>
      <tbody>${body}</tbody>
    </table></div>`;
}

const aiOpsLoadJournals = loadWorksiteJournals;
loadWorksiteJournals = async function() {
  await aiOpsLoadJournals();
  await refreshAiOpsPanel();
};

const aiOpsRenderJournal = renderSelectedWorksiteJournal;
renderSelectedWorksiteJournal = function() {
  aiOpsRenderJournal();
  refreshAiOpsPanel();
};
