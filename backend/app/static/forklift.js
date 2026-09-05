// 堆高機出租管理系統 — 整合版（資料持久化至後端 API）
let currentView = 'dashboard';
let cache = { customers: [], equipment: [], rentals: [], stats: null };

// API 工具
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has('Content-Type') && options.body) headers.set('Content-Type', 'application/json');
  const response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) { window.location.href = '/'; throw new Error('尚未登入'); }
  if (!response.ok) throw new Error(data.detail || JSON.stringify(data));
  return data;
}

// 初始化
document.addEventListener('DOMContentLoaded', async () => {
  try {
    const me = await api('/api/auth/me');
    document.getElementById('userInfo').textContent = `${me.name}（${me.employee_code}）`;
    if (me.must_change_password) { window.location.href = '/'; return; }
    initializeApp();
  } catch (error) {
    window.location.href = '/';
  }
});

function initializeApp() {
  setupNavigation();
  setupModal();
  setupLogout();
  loadAllData().then(() => {
    document.getElementById('loading').style.display = 'none';
    renderView('dashboard');
    setActiveNav('dashboard');
  });
}

async function loadAllData() {
  const [customers, equipment, rentals, stats] = await Promise.all([
    api('/api/forklift/customers'),
    api('/api/forklift/equipment'),
    api('/api/forklift/rentals'),
    api('/api/forklift/stats'),
  ]);
  cache = { customers, equipment, rentals, stats };
}

function setupLogout() {
  document.getElementById('logoutBtn').addEventListener('click', async () => {
    await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
    window.location.href = '/';
  });
}

// 導航
function setupNavigation() {
  document.querySelectorAll('.nav-link[data-view]').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      const view = e.target.dataset.view;
      if (view && view !== currentView) {
        currentView = view;
        setActiveNav(view);
        renderView(view);
      }
    });
  });
}

function setActiveNav(activeView) {
  document.querySelectorAll('.nav-link[data-view]').forEach(link => {
    link.classList.remove('active');
    if (link.dataset.view === activeView) link.classList.add('active');
  });
}

// Modal
function setupModal() {
  const modal = document.getElementById('modalDialog');
  document.getElementById('closeModal').addEventListener('click', () => modal.close());
  document.getElementById('modalCancel').addEventListener('click', () => modal.close());
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.close(); });
}

function openModal(title, content, onSubmit) {
  const modal = document.getElementById('modalDialog');
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('modalBody').innerHTML = content;
  const submitBtn = document.getElementById('modalSubmit');
  const newBtn = submitBtn.cloneNode(true);
  submitBtn.parentNode.replaceChild(newBtn, submitBtn);
  if (onSubmit) newBtn.addEventListener('click', (e) => { e.preventDefault(); onSubmit(); });
  modal.showModal();
}

function closeModal() { document.getElementById('modalDialog').close(); }

// 視圖路由
function renderView(viewName) {
  const mainContent = document.getElementById('mainContent');
  switch (viewName) {
    case 'dashboard': renderDashboard(mainContent); break;
    case 'customers': renderCustomers(mainContent); break;
    case 'equipment': renderEquipment(mainContent); break;
    case 'rentals': renderRentals(mainContent); break;
    case 'financial': renderFinancial(mainContent); break;
    case 'reports': renderReports(mainContent); break;
    default: renderDashboard(mainContent);
  }
}

// ---- 儀表板 ----
function renderDashboard(container) {
  const s = cache.stats || {};
  container.innerHTML = `
    <div class="dashboard">
      <h1>營運儀表板</h1>
      <div class="stats-grid">
        <div class="card stat-card"><div class="stat-card__value">NT$ ${(s.total_income||0).toLocaleString()}</div><div class="stat-card__label">總收入</div></div>
        <div class="card stat-card"><div class="stat-card__value">NT$ ${(s.total_deposit||0).toLocaleString()}</div><div class="stat-card__label">押金總額</div></div>
        <div class="card stat-card"><div class="stat-card__value">${s.equipment_utilization||0}%</div><div class="stat-card__label">設備使用率</div></div>
        <div class="card stat-card"><div class="stat-card__value">NT$ ${(s.receivables||0).toLocaleString()}</div><div class="stat-card__label">應收帳款</div></div>
        <div class="card stat-card"><div class="stat-card__value">${s.rental_count||0}</div><div class="stat-card__label">出租筆數</div></div>
        <div class="card stat-card"><div class="stat-card__value">${s.customer_count||0}</div><div class="stat-card__label">客戶數</div></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:24px;">
        <div class="card"><div class="card__header"><h4>收入與押金</h4></div><div class="card__body"><div class="chart-container"><canvas id="incomeChart"></canvas></div></div></div>
        <div class="card"><div class="card__header"><h4>設備狀態分布</h4></div><div class="card__body"><div class="chart-container"><canvas id="equipmentChart"></canvas></div></div></div>
      </div>
    </div>`;
  setTimeout(() => {
    const ctx1 = document.getElementById('incomeChart');
    if (ctx1) new Chart(ctx1, { type: 'bar', data: { labels: ['總收入', '押金', '營業稅'], datasets: [{ data: [s.total_income||0, s.total_deposit||0, s.total_tax||0], backgroundColor: ['#1FB8CD','#FFC185','#B4413C'] }] }, options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } } });
    const ctx2 = document.getElementById('equipmentChart');
    if (ctx2) {
      const statusDist = s.equipment_status || {};
      new Chart(ctx2, { type: 'doughnut', data: { labels: Object.keys(statusDist), datasets: [{ data: Object.values(statusDist), backgroundColor: ['#1FB8CD','#FFC185','#B4413C'] }] }, options: { responsive: true, maintainAspectRatio: false } });
    }
  }, 100);
}

// ---- 客戶管理 ----
function renderCustomers(container) {
  container.innerHTML = `
    <div class="customers">
      <div class="flex justify-between items-center mb-8">
        <h1>客戶管理</h1>
        <div class="action-buttons">
          <button class="btn btn--primary" id="addCustomerBtn">新增客戶</button>
        </div>
      </div>
      <div class="card"><div class="card__body">
        <table class="data-table"><thead><tr>
          <th>客戶編號</th><th>公司名稱</th><th>統一編號</th><th>聯絡人</th><th>電話</th><th>信用額度</th><th>付款條件</th><th>等級</th><th>操作</th>
        </tr></thead><tbody>
          ${cache.customers.map(c => `
            <tr>
              <td>${c.customer_code}</td><td>${c.name}</td><td>${c.tax_id||''}</td><td>${c.contact||''}</td><td>${c.phone||''}</td>
              <td>NT$ ${(c.credit_limit||0).toLocaleString()}</td><td>${c.payment_terms||''}</td>
              <td><span class="status status--${c.grade==='A'?'success':'warning'}">${c.grade}級</span></td>
              <td>
                <button class="btn btn--sm btn--outline" onclick="editCustomer(${c.id})">編輯</button>
                <button class="btn btn--sm btn--outline" onclick="deleteCustomer(${c.id})">刪除</button>
              </td>
            </tr>`).join('')}
        </tbody></table>
      </div></div>
    </div>`;
  document.getElementById('addCustomerBtn').addEventListener('click', addCustomer);
}

function customerForm(c) {
  return `
    <div class="form-group"><label class="form-label">公司名稱</label><input type="text" class="form-control" id="f_name" value="${c?.name||''}" required></div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">統一編號</label><input type="text" class="form-control" id="f_taxId" value="${c?.tax_id||''}"></div>
      <div class="form-group"><label class="form-label">聯絡人</label><input type="text" class="form-control" id="f_contact" value="${c?.contact||''}"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">電話</label><input type="text" class="form-control" id="f_phone" value="${c?.phone||''}"></div>
      <div class="form-group"><label class="form-label">電子郵件</label><input type="email" class="form-control" id="f_email" value="${c?.email||''}"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">信用額度</label><input type="number" class="form-control" id="f_creditLimit" value="${c?.credit_limit||0}"></div>
      <div class="form-group"><label class="form-label">付款條件</label>
        <select class="form-control" id="f_paymentTerms">
          <option value="">請選擇</option>
          ${['現金','月結30天','月結45天','月結60天'].map(t => `<option value="${t}" ${c?.payment_terms===t?'selected':''}>${t}</option>`).join('')}
        </select>
      </div>
    </div>
    <div class="form-group"><label class="form-label">客戶等級</label>
      <select class="form-control" id="f_grade">
        ${['A','B','C'].map(g => `<option value="${g}" ${c?.grade===g?'selected':''}>${g}級</option>`).join('')}
      </select>
    </div>`;
}

function addCustomer() {
  openModal('新增客戶', customerForm(), async () => {
    try {
      await api('/api/forklift/customers', { method: 'POST', body: JSON.stringify({
        name: document.getElementById('f_name').value,
        tax_id: document.getElementById('f_taxId').value,
        contact: document.getElementById('f_contact').value,
        phone: document.getElementById('f_phone').value,
        email: document.getElementById('f_email').value,
        credit_limit: parseInt(document.getElementById('f_creditLimit').value) || 0,
        payment_terms: document.getElementById('f_paymentTerms').value,
        grade: document.getElementById('f_grade').value,
      })});
      closeModal(); await loadAllData(); renderView('customers');
    } catch (e) { alert(e.message); }
  });
}

function editCustomer(id) {
  const c = cache.customers.find(x => x.id === id);
  openModal('編輯客戶', customerForm(c), async () => {
    try {
      await api(`/api/forklift/customers/${id}`, { method: 'PUT', body: JSON.stringify({
        name: document.getElementById('f_name').value,
        tax_id: document.getElementById('f_taxId').value,
        contact: document.getElementById('f_contact').value,
        phone: document.getElementById('f_phone').value,
        email: document.getElementById('f_email').value,
        credit_limit: parseInt(document.getElementById('f_creditLimit').value) || 0,
        payment_terms: document.getElementById('f_paymentTerms').value,
        grade: document.getElementById('f_grade').value,
      })});
      closeModal(); await loadAllData(); renderView('customers');
    } catch (e) { alert(e.message); }
  });
}

async function deleteCustomer(id) {
  if (!confirm('確定要刪除此客戶嗎？')) return;
  try { await api(`/api/forklift/customers/${id}`, { method: 'DELETE' }); await loadAllData(); renderView('customers'); }
  catch (e) { alert(e.message); }
}

// ---- 設備管理 ----
function renderEquipment(container) {
  container.innerHTML = `
    <div class="equipment">
      <div class="flex justify-between items-center mb-8">
        <h1>設備管理</h1>
        <div class="action-buttons"><button class="btn btn--primary" id="addEquipmentBtn">新增設備</button></div>
      </div>
      <div class="card"><div class="card__body">
        <table class="data-table"><thead><tr>
          <th>設備編號</th><th>設備名稱</th><th>品牌型號</th><th>承載</th><th>燃料</th><th>日租金</th><th>月租金</th><th>狀態</th><th>操作</th>
        </tr></thead><tbody>
          ${cache.equipment.map(e => `
            <tr>
              <td>${e.equipment_code}</td><td>${e.name}</td><td>${e.brand||''}</td><td>${e.capacity}T</td><td>${e.fuel_type||''}</td>
              <td>NT$ ${(e.daily_rate||0).toLocaleString()}</td><td>NT$ ${(e.monthly_rate||0).toLocaleString()}</td>
              <td><span class="status status--${e.status==='可租'?'success':e.status==='已租'?'warning':'error'}">${e.status}</span></td>
              <td>
                <button class="btn btn--sm btn--outline" onclick="editEquipment(${e.id})">編輯</button>
                <button class="btn btn--sm btn--outline" onclick="deleteEquipment(${e.id})">刪除</button>
              </td>
            </tr>`).join('')}
        </tbody></table>
      </div></div>
    </div>`;
  document.getElementById('addEquipmentBtn').addEventListener('click', addEquipment);
}

function equipmentForm(e) {
  return `
    <div class="form-group"><label class="form-label">設備名稱</label><input type="text" class="form-control" id="f_name" value="${e?.name||''}" required></div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">品牌型號</label><input type="text" class="form-control" id="f_brand" value="${e?.brand||''}"></div>
      <div class="form-group"><label class="form-label">承載能力 (噸)</label><input type="number" step="0.1" class="form-control" id="f_capacity" value="${e?.capacity||0}"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">燃料類型</label>
        <select class="form-control" id="f_fuelType">
          ${['','電動','柴油','汽油','LPG'].map(f => `<option value="${f}" ${e?.fuel_type===f?'selected':''}>${f||'請選擇'}</option>`).join('')}
        </select>
      </div>
      <div class="form-group"><label class="form-label">狀態</label>
        <select class="form-control" id="f_status">
          ${['可租','已租','維修中'].map(s => `<option value="${s}" ${e?.status===s?'selected':''}>${s}</option>`).join('')}
        </select>
      </div>
    </div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">日租金</label><input type="number" class="form-control" id="f_dailyRate" value="${e?.daily_rate||0}"></div>
      <div class="form-group"><label class="form-label">月租金</label><input type="number" class="form-control" id="f_monthlyRate" value="${e?.monthly_rate||0}"></div>
    </div>`;
}

function addEquipment() {
  openModal('新增設備', equipmentForm(), async () => {
    try {
      await api('/api/forklift/equipment', { method: 'POST', body: JSON.stringify({
        name: document.getElementById('f_name').value,
        brand: document.getElementById('f_brand').value,
        capacity: parseFloat(document.getElementById('f_capacity').value) || 0,
        fuel_type: document.getElementById('f_fuelType').value,
        status: document.getElementById('f_status').value,
        daily_rate: parseInt(document.getElementById('f_dailyRate').value) || 0,
        monthly_rate: parseInt(document.getElementById('f_monthlyRate').value) || 0,
      })});
      closeModal(); await loadAllData(); renderView('equipment');
    } catch (e) { alert(e.message); }
  });
}

function editEquipment(id) {
  const e = cache.equipment.find(x => x.id === id);
  openModal('編輯設備', equipmentForm(e), async () => {
    try {
      await api(`/api/forklift/equipment/${id}`, { method: 'PUT', body: JSON.stringify({
        name: document.getElementById('f_name').value,
        brand: document.getElementById('f_brand').value,
        capacity: parseFloat(document.getElementById('f_capacity').value) || 0,
        fuel_type: document.getElementById('f_fuelType').value,
        status: document.getElementById('f_status').value,
        daily_rate: parseInt(document.getElementById('f_dailyRate').value) || 0,
        monthly_rate: parseInt(document.getElementById('f_monthlyRate').value) || 0,
      })});
      closeModal(); await loadAllData(); renderView('equipment');
    } catch (e) { alert(e.message); }
  });
}

async function deleteEquipment(id) {
  if (!confirm('確定要刪除此設備嗎？')) return;
  try { await api(`/api/forklift/equipment/${id}`, { method: 'DELETE' }); await loadAllData(); renderView('equipment'); }
  catch (e) { alert(e.message); }
}

// ---- 出租管理 ----
function renderRentals(container) {
  container.innerHTML = `
    <div class="rentals">
      <div class="flex justify-between items-center mb-8">
        <h1>出租管理</h1>
        <div class="action-buttons"><button class="btn btn--primary" id="addRentalBtn">新增出租記錄</button></div>
      </div>
      <div class="card"><div class="card__body">
        <table class="data-table"><thead><tr>
          <th>出租編號</th><th>客戶</th><th>設備</th><th>開始</th><th>結束</th><th>天數</th><th>日租金</th><th>總金額</th><th>押金</th><th>稅</th><th>狀態</th><th>操作</th>
        </tr></thead><tbody>
          ${cache.rentals.map(r => `
            <tr>
              <td>${r.rental_code}</td><td>${r.customer_name}</td><td>${r.equipment_name}</td>
              <td>${r.start_date}</td><td>${r.end_date}</td><td>${r.days}</td>
              <td>NT$ ${(r.daily_rate||0).toLocaleString()}</td><td>NT$ ${(r.total_amount||0).toLocaleString()}</td>
              <td>NT$ ${(r.deposit||0).toLocaleString()}</td><td>NT$ ${(r.tax||0).toLocaleString()}</td>
              <td><span class="status status--${r.status==='已結清'?'success':r.status==='進行中'?'warning':'info'}">${r.status}</span></td>
              <td>
                <button class="btn btn--sm btn--outline" onclick="editRental(${r.id})">編輯</button>
                <button class="btn btn--sm btn--outline" onclick="deleteRental(${r.id})">刪除</button>
              </td>
            </tr>`).join('')}
        </tbody></table>
      </div></div>
    </div>`;
  document.getElementById('addRentalBtn').addEventListener('click', addRental);
}

function rentalForm(r) {
  const availableEquip = cache.equipment.filter(e => e.status === '可租' || (r && e.id === r.equipment_id));
  return `
    <div class="form-row">
      <div class="form-group"><label class="form-label">客戶</label>
        <select class="form-control" id="f_customerId" required>
          <option value="">請選擇客戶</option>
          ${cache.customers.map(c => `<option value="${c.id}" ${r?.customer_id===c.id?'selected':''}>${c.customer_code} ${c.name}</option>`).join('')}
        </select>
      </div>
      <div class="form-group"><label class="form-label">設備</label>
        <select class="form-control" id="f_equipmentId" required onchange="updateRentalRates()">
          <option value="">請選擇設備</option>
          ${availableEquip.map(e => `<option value="${e.id}" data-daily-rate="${e.daily_rate}" ${r?.equipment_id===e.id?'selected':''}>${e.equipment_code} ${e.name}</option>`).join('')}
        </select>
      </div>
    </div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">開始日期</label><input type="date" class="form-control" id="f_startDate" value="${r?.start_date||''}" onchange="calculateRentalAmount()"></div>
      <div class="form-group"><label class="form-label">結束日期</label><input type="date" class="form-control" id="f_endDate" value="${r?.end_date||''}" onchange="calculateRentalAmount()"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">天數</label><input type="number" class="form-control" id="f_days" readonly value="${r?.days||''}"></div>
      <div class="form-group"><label class="form-label">日租金</label><input type="number" class="form-control" id="f_dailyRate" readonly value="${r?.daily_rate||''}"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">租金小計</label><input type="number" class="form-control" id="f_subtotal" readonly></div>
      <div class="form-group"><label class="form-label">營業稅 (5%)</label><input type="number" class="form-control" id="f_tax" readonly value="${r?.tax||''}"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label class="form-label">總金額</label><input type="number" class="form-control" id="f_totalAmount" readonly value="${r?.total_amount||''}"></div>
      <div class="form-group"><label class="form-label">押金</label><input type="number" class="form-control" id="f_deposit" value="${r?.deposit||''}"></div>
    </div>
    <div class="form-group"><label class="form-label">狀態</label>
      <select class="form-control" id="f_status">
        ${['進行中','已結清','逾期'].map(s => `<option value="${s}" ${r?.status===s?'selected':''}>${s}</option>`).join('')}
      </select>
    </div>`;
}

function updateRentalRates() {
  const sel = document.getElementById('f_equipmentId');
  if (sel && sel.value) {
    const rate = parseInt(sel.selectedOptions[0].dataset.dailyRate) || 0;
    document.getElementById('f_dailyRate').value = rate;
    document.getElementById('f_deposit').value = rate * 10;
    calculateRentalAmount();
  }
}

function calculateRentalAmount() {
  const start = document.getElementById('f_startDate')?.value;
  const end = document.getElementById('f_endDate')?.value;
  const dailyRate = parseInt(document.getElementById('f_dailyRate')?.value) || 0;
  if (start && end && dailyRate) {
    const days = Math.ceil(Math.abs(new Date(end) - new Date(start)) / (1000*60*60*24));
    document.getElementById('f_days').value = days;
    const subtotal = dailyRate * days;
    const tax = Math.round(subtotal * 0.05);
    document.getElementById('f_subtotal').value = subtotal;
    document.getElementById('f_tax').value = tax;
    document.getElementById('f_totalAmount').value = subtotal + tax;
  }
}

function addRental() {
  openModal('新增出租記錄', rentalForm(), async () => {
    try {
      await api('/api/forklift/rentals', { method: 'POST', body: JSON.stringify({
        customer_id: parseInt(document.getElementById('f_customerId').value),
        equipment_id: parseInt(document.getElementById('f_equipmentId').value),
        start_date: document.getElementById('f_startDate').value,
        end_date: document.getElementById('f_endDate').value,
        days: parseInt(document.getElementById('f_days').value) || 0,
        daily_rate: parseInt(document.getElementById('f_dailyRate').value) || 0,
        total_amount: parseInt(document.getElementById('f_totalAmount').value) || 0,
        deposit: parseInt(document.getElementById('f_deposit').value) || 0,
        tax: parseInt(document.getElementById('f_tax').value) || 0,
        status: document.getElementById('f_status').value,
      })});
      closeModal(); await loadAllData(); renderView('rentals');
    } catch (e) { alert(e.message); }
  });
}

function editRental(id) {
  const r = cache.rentals.find(x => x.id === id);
  openModal('編輯出租記錄', rentalForm(r), async () => {
    try {
      await api(`/api/forklift/rentals/${id}`, { method: 'PUT', body: JSON.stringify({
        customer_id: parseInt(document.getElementById('f_customerId').value),
        equipment_id: parseInt(document.getElementById('f_equipmentId').value),
        start_date: document.getElementById('f_startDate').value,
        end_date: document.getElementById('f_endDate').value,
        days: parseInt(document.getElementById('f_days').value) || 0,
        daily_rate: parseInt(document.getElementById('f_dailyRate').value) || 0,
        total_amount: parseInt(document.getElementById('f_totalAmount').value) || 0,
        deposit: parseInt(document.getElementById('f_deposit').value) || 0,
        tax: parseInt(document.getElementById('f_tax').value) || 0,
        status: document.getElementById('f_status').value,
      })});
      closeModal(); await loadAllData(); renderView('rentals');
    } catch (e) { alert(e.message); }
  });
}

async function deleteRental(id) {
  if (!confirm('確定要刪除此出租記錄嗎？')) return;
  try { await api(`/api/forklift/rentals/${id}`, { method: 'DELETE' }); await loadAllData(); renderView('rentals'); }
  catch (e) { alert(e.message); }
}

// ---- 財務管理 ----
function renderFinancial(container) {
  const s = cache.stats || {};
  const totalIncome = s.total_income || 0;
  const totalTax = s.total_tax || 0;
  const totalDeposit = s.total_deposit || 0;
  const receivables = s.receivables || 0;
  container.innerHTML = `
    <div class="financial">
      <h1>財務管理</h1>
      <div class="stats-grid" style="grid-template-columns:repeat(4,1fr);">
        <div class="card stat-card"><div class="stat-card__value" style="color:var(--color-success)">NT$ ${totalIncome.toLocaleString()}</div><div class="stat-card__label">總收入</div></div>
        <div class="card stat-card"><div class="stat-card__value">NT$ ${totalTax.toLocaleString()}</div><div class="stat-card__label">營業稅</div></div>
        <div class="card stat-card"><div class="stat-card__value">NT$ ${totalDeposit.toLocaleString()}</div><div class="stat-card__label">押金總額</div></div>
        <div class="card stat-card"><div class="stat-card__value" style="color:var(--color-warning)">NT$ ${receivables.toLocaleString()}</div><div class="stat-card__label">應收帳款</div></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:24px;">
        <div class="card"><div class="card__header"><h4>收入明細</h4></div><div class="card__body">
          <table class="data-table"><thead><tr><th>項目</th><th>金額</th></tr></thead><tbody>
            <tr><td>租金收入</td><td>NT$ ${totalIncome.toLocaleString()}</td></tr>
            <tr><td>押金收入</td><td>NT$ ${totalDeposit.toLocaleString()}</td></tr>
            <tr><td>營業稅</td><td>NT$ ${totalTax.toLocaleString()}</td></tr>
          </tbody></table>
        </div></div>
        <div class="card"><div class="card__header"><h4>出租記錄明細</h4></div><div class="card__body">
          <table class="data-table"><thead><tr><th>編號</th><th>客戶</th><th>金額</th><th>狀態</th></tr></thead><tbody>
            ${cache.rentals.map(r => `<tr><td>${r.rental_code}</td><td>${r.customer_name}</td><td>NT$ ${(r.total_amount||0).toLocaleString()}</td><td>${r.status}</td></tr>`).join('')}
          </tbody></table>
        </div></div>
      </div>
    </div>`;
}

// ---- 報表功能 ----
function renderReports(container) {
  container.innerHTML = `
    <div class="reports">
      <h1>報表功能</h1>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:24px;margin-bottom:24px;">
        <div class="card"><div class="card__body" style="text-align:center;">
          <h4>客戶報表</h4><p class="mb-8">客戶資料統計分析</p>
          <button class="btn btn--primary btn--full-width" onclick="generateCustomerReport()">產生報表</button>
        </div></div>
        <div class="card"><div class="card__body" style="text-align:center;">
          <h4>出租報表</h4><p class="mb-8">設備出租統計分析</p>
          <button class="btn btn--primary btn--full-width" onclick="generateRentalReport()">產生報表</button>
        </div></div>
        <div class="card"><div class="card__body" style="text-align:center;">
          <h4>財務報表</h4><p class="mb-8">收支損益統計分析</p>
          <button class="btn btn--primary btn--full-width" onclick="generateFinancialReport()">產生報表</button>
        </div></div>
      </div>
      <div class="card"><div class="card__header"><div class="flex justify-between items-center">
        <h4>報表預覽</h4>
        <div class="action-buttons">
          <button class="btn btn--outline" onclick="window.print()">列印</button>
          <button class="btn btn--outline" onclick="exportReportCSV()">匯出CSV</button>
        </div>
      </div></div><div class="card__body" id="reportContent">
        <p style="text-align:center;color:var(--color-text-secondary);padding:40px;">請選擇要產生的報表類型</p>
      </div></div>
    </div>`;
}

function generateCustomerReport() {
  const gradeStats = cache.customers.reduce((acc, c) => { acc[c.grade] = (acc[c.grade]||0)+1; return acc; }, {});
  document.getElementById('reportContent').innerHTML = `
    <h3>客戶統計報表</h3><p>報表產生日期：${new Date().toLocaleDateString('zh-TW')}</p>
    <div style="margin:24px 0;"><h4>客戶概況</h4><ul>
      <li>總客戶數：${cache.customers.length} 家</li>
      <li>A級客戶：${gradeStats.A||0} 家</li><li>B級客戶：${gradeStats.B||0} 家</li><li>C級客戶：${gradeStats.C||0} 家</li>
    </ul></div>
    <table class="data-table"><thead><tr><th>編號</th><th>公司名稱</th><th>聯絡人</th><th>電話</th><th>信用額度</th><th>等級</th></tr></thead><tbody>
      ${cache.customers.map(c => `<tr><td>${c.customer_code}</td><td>${c.name}</td><td>${c.contact||''}</td><td>${c.phone||''}</td><td>NT$ ${(c.credit_limit||0).toLocaleString()}</td><td>${c.grade}級</td></tr>`).join('')}
    </tbody></table>`;
}

function generateRentalReport() {
  const totalRevenue = cache.rentals.reduce((s, r) => s + (r.total_amount||0), 0);
  const totalTax = cache.rentals.reduce((s, r) => s + (r.tax||0), 0);
  document.getElementById('reportContent').innerHTML = `
    <h3>出租統計報表</h3><p>報表產生日期：${new Date().toLocaleDateString('zh-TW')}</p>
    <div style="margin:24px 0;"><h4>出租概況</h4><ul>
      <li>出租筆數：${cache.rentals.length} 筆</li>
      <li>總營收：NT$ ${totalRevenue.toLocaleString()}</li>
      <li>總營業稅：NT$ ${totalTax.toLocaleString()}</li>
      <li>平均單筆金額：NT$ ${cache.rentals.length>0?Math.round(totalRevenue/cache.rentals.length).toLocaleString():0}</li>
    </ul></div>
    <table class="data-table"><thead><tr><th>編號</th><th>客戶</th><th>設備</th><th>租期</th><th>總金額</th><th>稅</th><th>狀態</th></tr></thead><tbody>
      ${cache.rentals.map(r => `<tr><td>${r.rental_code}</td><td>${r.customer_name}</td><td>${r.equipment_name}</td><td>${r.start_date} ~ ${r.end_date} (${r.days}天)</td><td>NT$ ${(r.total_amount||0).toLocaleString()}</td><td>NT$ ${(r.tax||0).toLocaleString()}</td><td>${r.status}</td></tr>`).join('')}
    </tbody></table>`;
}

function generateFinancialReport() {
  const s = cache.stats || {};
  document.getElementById('reportContent').innerHTML = `
    <h3>財務損益報表</h3><p>報表產生日期：${new Date().toLocaleDateString('zh-TW')}</p>
    <div style="margin:24px 0;"><h4>損益概況</h4><ul>
      <li>總收入：NT$ ${(s.total_income||0).toLocaleString()}</li>
      <li>營業稅：NT$ ${(s.total_tax||0).toLocaleString()}</li>
      <li>押金總額：NT$ ${(s.total_deposit||0).toLocaleString()}</li>
      <li>應收帳款：NT$ ${(s.receivables||0).toLocaleString()}</li>
      <li>設備使用率：${s.equipment_utilization||0}%</li>
    </ul></div>`;
}

function exportReportCSV() {
  const rows = [['編號','客戶','設備','開始','結束','天數','日租金','總金額','押金','稅','狀態']];
  cache.rentals.forEach(r => rows.push([r.rental_code, r.customer_name, r.equipment_name, r.start_date, r.end_date, r.days, r.daily_rate, r.total_amount, r.deposit, r.tax, r.status]));
  const csv = rows.map(row => row.map(c => `"${c}"`).join(',')).join('\n');
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = '出租記錄.csv'; a.click();
  URL.revokeObjectURL(url);
}
