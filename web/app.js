/* 商场扶梯平台前端（原生 JS） */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

const STATE = { people: [], escalators: [], detours: [], items: [], now: null,
  currentPerson: null, currentView: 'floors', detailCache: {} };

const ROLE_LABEL = { DUTY: '值班员', INSPECTOR: '巡检员', REPAIR: '维修员',
  SUPERVISOR: '主管', SERVICE: '客服' };
const STATE_LABEL = { STOPPED: '停止', STARTING: '启动中', RUNNING: '运行中',
  STOPPING: '停梯中', LOCKED: '封控停梯' };
const FAULT_LABEL = {
  REPORTED: '已报修', ASSIGNED: '已派单', ENROUTE: '维修在途', ARRIVED: '维修到场',
  REPAIRING: '维修中', REPAIRED: '待复检审批', REINSPECT_REJECTED: '复检退回',
  APPROVED: '复检通过·待复开', CLOSED: '已关闭' };
const SRC_LABEL = { FAULT: '设备故障', INCIDENT: '客流事件', ESTOP: '急停失败', REPEAT: '反复跳停' };
const ALARM_EVENTS = new Set(['SEALED', 'TRIP', 'SCHEDULE_BLOCKED', 'REOPEN_BLOCKED',
  'BARRICADE_MISSING', 'SLA_ARRIVAL_LATE', 'SLA_REPAIR_OVERDUE', 'REINSPECT_REJECTED',
  'ESTOP_TEST_FAILED']);
const GOOD_EVENTS = new Set(['STARTED', 'REOPENED', 'REINSPECT_APPROVED', 'REPAIR_DONE',
  'BARRICADE_REMOVED', 'ESTOP_TEST_PASS']);

// ---------------- API ----------------
async function api(path, body) {
  const opt = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, person: STATE.currentPerson }) } : {};
  const res = await fetch(path, opt);
  const data = await res.json();
  if (!data.ok) { toast(`[${data.error_code}] ${data.error}`, true); throw new Error(data.error_code); }
  return data;
}

function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = ''; }, 3200);
}

function person() { return STATE.people.find(p => p.code === STATE.currentPerson); }
function role() { const p = person(); return p ? p.role : null; }
function escByCode(code) { return STATE.escalators.find(e => e.code === code); }
function escOpts() {
  return STATE.escalators.map(e =>
    `<option value="${e.code}">${e.code} ${e.name}（${e.floor_from}F→${e.floor_to}F）</option>`).join('');
}
function dirSymbol(dir) {
  const up = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>';
  const dn = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="M19 12l-7 7-7-7"/></svg>';
  return dir === 'UP' ? up : dn;
}

// ---------------- 顶栏 / 导航 ----------------
function initChrome() {
  $('#personSelect').addEventListener('change', e => {
    STATE.currentPerson = e.target.value;
    localStorage.setItem('esc_person', e.target.value);
    renderRole(); renderAll();
  });
  $$('#nav button').forEach(b => b.addEventListener('click', () => switchView(b.dataset.view)));
  $('#reopenCancel').onclick = () => $('#reopenModal').classList.remove('show');
}

function switchView(v) {
  STATE.currentView = v;
  $$('#nav button').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  $$('.view').forEach(s => s.classList.toggle('active', s.id === `view-${v}`));
  renderAll();
}

function renderRole() {
  const p = person();
  $('#roleBadge').textContent = p ? ROLE_LABEL[p.role] : '—';
}

function tickClock() {
  const d = new Date();
  $('#clock').textContent = d.toLocaleString('zh-CN', { hour12: false });
}

// ---------------- 视图 1：楼层设备图 ----------------
function renderFloors() {
  const floors = [...new Set(STATE.escalators.map(e => e.floor_from))].sort((a, b) => b - a);
  $('#floorMap').innerHTML = floors.map(fl => {
    const list = STATE.escalators.filter(e => e.floor_from === fl);
    const cards = list.map(e => cardFloor(e)).join('');
    return `<div class="floor-band">
        <span class="floor-tag">${fl}F</span>
        <span class="line"></span>
        <span class="zone-note">${list[0].zone} · 计划 ${list[0].schedule_start}–${list[0].schedule_end}</span>
      </div>
      <div class="grid cols-3">${cards}</div>`;
  }).join('');
  $$('#floorMap .esc-card').forEach(c => c.addEventListener('click', () => {
    $('#clEsc').value = c.dataset.code;
    switchView('checklist');
  }));
}

function cardFloor(e) {
  const locked = e.run_state === 'LOCKED';
  const detours = STATE.detours.filter(d => d.escalator_id === e.id);
  return `<div class="esc-card ${e.run_state}" data-code="${e.code}" style="cursor:pointer">
    <div class="esc-head">
      <div class="dir-arrow">${dirSymbol(e.desired_direction)}</div>
      <div>
        <div class="name">${e.name}
          <span class="state-pill state-${e.run_state}">${STATE_LABEL[e.run_state]}</span>
        </div>
        <div class="code">${e.code} · ${e.floor_from}F→${e.floor_to}F · 配置 v${e.config_version}
          ${e.lock_holder ? `· <b style="color:var(--amber)">${e.lock_holder} 启动占用中</b>` : ''}</div>
      </div>
    </div>
    ${locked ? `<div class="hazard-strip"></div>
      <div class="lock-reason">⛔ ${e.lock_reason || '封控中'}（24h 跳停 ${e.trip_count} 次）</div>` : ''}
    ${detours.length ? `<div class="small mt8" style="color:var(--safe-red-dark)">🧭 ${detours[0].message}</div>` : ''}
  </div>`;
}

// ---------------- 视图 2：开梯清单 ----------------
async function renderChecklist() {
  const sel = $('#clEsc');
  if (!sel.options.length) sel.innerHTML = escOpts();
  const code = sel.value || sel.options[0]?.value;
  if (!code) return;
  sel.value = code;
  const d = await fetch(`/api/detail?code=${code}`).then(r => r.json());
  STATE.detailCache[code] = d;
  const e = d.escalator;
  $('#clVersion').textContent = `当前配置版本 v${e.config_version} · 方向 ${e.desired_direction === 'UP' ? '▲上行' : '▼下行'}`;
  $('#clDirection').value = e.desired_direction;

  // 汇总条
  const items = d.items;
  const filled = items.filter(i => i.result);
  const failed = items.filter(i => i.result === 'FAIL');
  const missing = STATE.items.filter(ci => !items.find(i => i.item_key === ci.key && i.result));
  let sumCls = '', sumTxt = '';
  if (!d.inspection) { sumTxt = '今日尚未建立班前检查表'; }
  else if (failed.length) { sumCls = 'bad'; sumTxt = `❌ 不合格项：${failed.map(i => ({steps:'梯级',comb:'梳齿板',handrail:'扶手带',lighting:'照明',estop:'急停装置'})[i.item_key]).join('、')}，禁止启动`; }
  else if (missing.length) { sumCls = 'bad'; sumTxt = `⛔ 检查漏项：${missing.map(i => i.label).join('、')}（全部合格才允许启动）`; }
  else { sumCls = 'good'; sumTxt = '✅ 五项班前检查全部合格'; }
  const sum = $('#clSummary');
  sum.className = 'summary-bar ' + sumCls;
  sum.textContent = sumTxt;

  $('#clItems').innerHTML = STATE.items.map(ci => {
    const row = items.find(i => i.item_key === ci.key);
    const res = row ? row.result : null;
    return `<tr data-key="${ci.key}">
      <td><b>${ci.label}</b><span class="req-mark">必检</span></td>
      <td><div class="seg">
        <button data-r="PASS" class="${res === 'PASS' ? 'sel-pass' : ''}">✓ 通过</button>
        <button data-r="FAIL" class="${res === 'FAIL' ? 'sel-fail' : ''}">✗ 失败</button>
        <button data-r="NA" class="${res === 'NA' ? 'sel-na' : ''}" title="必检项不可选不适用">不适用</button>
      </div></td>
      <td><input type="text" class="flat remark" style="width:100%" value="${row?.remark || ''}" placeholder="备注（可选）"></td>
    </tr>`;
  }).join('');

  $$('#clItems tr').forEach(tr => {
    const key = tr.dataset.key;
    tr.querySelectorAll('.seg button').forEach(b => b.addEventListener('click', async () => {
      try {
        if (!d.inspection) { await api('/api/inspection/open', { code }); }
        const insId = d.inspection ? d.inspection.id :
          (await fetch(`/api/detail?code=${code}`).then(r => r.json())).inspection.id;
        const remark = $('.remark', tr).value;
        await api('/api/inspection/item', { inspection_id: insId, key, result: b.dataset.r, remark });
        renderChecklist();
      } catch (_) {}
    }));
  });

  // 复开按钮（封控且工单已审批通过时可用）
  const f = d.active_fault;
  const reopenBtn = $('#btnReopenChecklist');
  reopenBtn.disabled = !(f && f.status === 'APPROVED' && role() === 'DUTY');
  reopenBtn.textContent = f ? `撤围挡 · 复 开（工单 #${f.id}）` : '撤围挡 · 复 开';
  reopenBtn.onclick = () => {
    if (!f) return;
    $('#reopenModalText').textContent = `扶梯 ${e.code} ${e.name}，工单 #${f.id}：${f.reason}`;
    $('#reopenConfirm').onclick = async () => {
      $('#reopenModal').classList.remove('show');
      try { await api('/api/reopen', { fault_id: f.id, confirmed: true });
        toast('围挡已撤除，扶梯复开运行'); refresh(); } catch (_) {}
    };
    $('#reopenModal').classList.add('show');
  };
}

function bindChecklistActions() {
  $('#clEsc').addEventListener('change', renderChecklist);
  $('#btnSetDir').onclick = async () => {
    const code = $('#clEsc').value;
    try { const r = await api('/api/direction', { code, direction: $('#clDirection').value });
      toast(`方向已更新，新版本 v${r.config_version}，启动请按此版本提交`); refresh(); } catch (_) {}
  };
  $('#btnOpenInspection').onclick = async () => {
    try { await api('/api/inspection/open', { code: $('#clEsc').value });
      toast('今日检查表已建立'); renderChecklist(); } catch (_) {}
  };
  $('#btnEstopPass').onclick = async () => {
    try { await api('/api/estop-test', { code: $('#clEsc').value, result: 'PASS', response_ms: 300 });
      toast('急停测试通过'); refresh(); } catch (_) {}
  };
  $('#btnEstopFail').onclick = async () => {
    try { await api('/api/estop-test', { code: $('#clEsc').value, result: 'FAIL', detail: '按下急停后梯级未在规定距离内停止' });
      toast('急停测试失败，已自动停梯封控', true); refresh(); } catch (_) {}
  };
  $('#btnAcquire').onclick = async () => {
    const e = escByCode($('#clEsc').value);
    try { await api('/api/start/acquire', { code: e.code, version: e.config_version });
      toast('启动权已占用（其他值班员此时无法启动）'); refresh(); } catch (_) {}
  };
  $('#btnRelease').onclick = async () => {
    try { await api('/api/start/release', { code: $('#clEsc').value });
      toast('已放弃启动'); refresh(); } catch (_) {}
  };
  $('#btnStart').onclick = async () => {
    const e = escByCode($('#clEsc').value);
    try { const r = await api('/api/start', { code: e.code, version: e.config_version });
      if (r.tripped) toast('控制柜启动后跳停，已自动封控', true);
      else toast('扶梯已启动'); refresh(); } catch (_) {}
  };
  $('#btnStop').onclick = async () => {
    try { await api('/api/stop', { code: $('#clEsc').value });
      toast('已停梯'); refresh(); } catch (_) {}
  };
}

// ---------------- 视图 3：运行状态 ----------------
async function renderRunning() {
  const boxes = await Promise.all(STATE.escalators.map(async e => {
    const d = await fetch(`/api/detail?code=${e.code}`).then(r => r.json());
    const cab = d.cabinet, crowd = d.crowd;
    return `<div class="esc-card ${e.run_state}">
      <div class="esc-head">
        <div class="dir-arrow">${dirSymbol(e.desired_direction)}</div>
        <div><div class="name">${e.name}
          <span class="state-pill state-${e.run_state}">${STATE_LABEL[e.run_state]}</span></div>
          <div class="code">${e.code}</div></div>
      </div>
      <div class="stat-row">
        <span class="chip ${cab.running ? 'ok' : ''}">控制柜：<b>${cab.running ? `运行 ${e.desired_direction === 'UP' ? '▲' : '▼'}` : '停机'}</b></span>
        <span class="chip ${cab.fault_code ? 'bad' : ''}">故障码：<b>${cab.fault_code || '无'}</b></span>
        <span class="chip ${crowd.people_per_5min > 120 ? 'warn' : ''}">客流：<b>${crowd.people_per_5min}/5min</b></span>
        ${e.lock_holder ? `<span class="chip warn">启动占用：<b>${e.lock_holder}</b></span>` : ''}
      </div>
      <div class="btn-row">
        <button class="btn sm" data-act="trip">模拟：下次启动跳停</button>
        <button class="btn sm" data-act="fault">模拟：运行中故障</button>
        <button class="btn sm" data-act="incident">模拟：客流事件</button>
        <button class="btn sm" data-act="load">模拟：高客流</button>
      </div>
    </div>`;
  }));
  $('#runningGrid').innerHTML = boxes.join('');
  $$('#runningGrid .esc-card').forEach((c, i) => {
    const code = STATE.escalators[i].code;
    c.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', async () => {
      const act = b.dataset.act;
      try {
        if (act === 'trip') { await api('/api/mock/trip', { code }); toast('已注入：下一次启动将跳停'); }
        if (act === 'fault') { await api('/api/mock/fault', { code }); toast('已注入运行中故障，等待轮询封控'); }
        if (act === 'incident') { await api('/api/mock/incident', { code }); toast('已注入客流事件'); }
        if (act === 'load') { await api('/api/mock/load', { code, people: 180 }); toast('客流读数设为 180/5min'); }
        setTimeout(refresh, 400);
      } catch (_) {}
    }));
  });
}

// ---------------- 视图 4：封控计时 ----------------
async function renderLockdowns() {
  const { lockdowns } = await fetch('/api/lockdowns').then(r => r.json());
  if (!lockdowns.length) {
    $('#lockdownList').innerHTML = '<div class="card muted">当前无封控扶梯 🎉</div>';
    return;
  }
  $('#lockdownList').innerHTML = lockdowns.map(f => lockCard(f)).join('');
  bindLockdownCards();
}

function fmtMin(mins) {
  if (mins < 60) return `${mins} 分钟`;
  return `${Math.floor(mins / 60)} 小时 ${mins % 60} 分`;
}

function lockCard(f) {
  const arrLate = f.arrival_overdue;
  const repLate = f.repair_overdue;
  const barUp = f.barricade_status === 'UP';
  const barPending = f.barricade_status === 'PENDING';
  return `<div class="card fault-card SEALED-tone" data-fid="${f.fault_id}" data-code="${f.esc_code}">
    <div class="inline">
      <span class="tag-src src-${f.source}">${SRC_LABEL[f.source] || f.source}</span>
      <b>工单 #${f.fault_id}</b>
      <span class="muted small">${f.esc_code} ${f.esc_name}</span>
      <span class="state-pill state-LOCKED">${FAULT_LABEL[f.status] || f.status}</span>
      <span style="flex:1"></span>
      <span class="small muted">封控已持续</span>
      <span class="timer ${f.sealed_minutes > 30 ? 'over' : ''}" data-sealed="${f.sealed_at}">${fmtMin(f.sealed_minutes)}</span>
    </div>
    <div class="lock-reason mt8">⛔ ${f.reason}</div>
    <div class="stat-row">
      <span class="chip ${barUp ? 'ok' : 'bad'}">围挡：<b>${barUp ? '已就位（斜纹围挡）' : (barPending ? '未就位（10 分钟宽限）' : '已撤除')}</b></span>
      <span class="chip ${arrLate ? 'bad' : ''}">到场时限：<b>${f.arrival_due_at?.slice(11) || '—'}</b>${arrLate ? ' · 已超时' : ''}</span>
      <span class="chip ${repLate ? 'bad' : ''}">修复时限：<b>${f.repair_due_at?.slice(11) || '待到场'}</b>${repLate ? ' · 维修延期' : ''}</span>
      <span class="chip">维修员：<b>${f.repair_name || '未派单'}</b></span>
    </div>
    <div class="btn-row">
      ${barPending ? '<button class="btn sm danger" data-a="barricade">设置围挡</button>' : ''}
      <button class="btn sm" data-a="detour">客服发布绕行</button>
    </div>
    <div class="photos small mt8"></div>
  </div>`;
}

function bindLockdownCards() {
  $$('#lockdownList .card').forEach(card => {
    const fid = Number(card.dataset.fid), code = card.dataset.code;
    const bBar = $('[data-a="barricade"]', card);
    if (bBar) bBar.onclick = async () => {
      try { await api('/api/barricade/place', { fault_id: fid });
        toast('围挡已就位'); refresh(); } catch (_) {}
    };
    $('[data-a="detour"]', card).onclick = async () => {
      const msg = prompt('绕行提示内容（仅客服可发布）',
        `${code} 临时停用，请改乘相邻扶梯/直梯`);
      if (msg) { try { await api('/api/detour/publish', { code, message: msg });
        toast('绕行信息已发布'); refresh(); } catch (_) {} }
    };
    loadPhotos(fid, $('.photos', card));
  });
}

async function loadPhotos(fid, container) {
  const { photos } = await fetch(`/api/photos?fault_id=${fid}`).then(r => r.json());
  if (photos.length) container.innerHTML = '故障照片：' +
    photos.map(p => `<img src="${p.relpath}" title="${p.original_name || ''}">`).join('');
}

// ---------------- 视图 5：报修进度 ----------------
async function renderFaults() {
  $('#nfEsc').innerHTML = escOpts();
  const { faults } = await fetch('/api/faults').then(r => r.json());
  if (!faults.length) { $('#faultList').innerHTML = '<div class="card muted">暂无进行中的报修工单</div>'; return; }
  $('#faultList').innerHTML = faults.map(f => faultCard(f)).join('');
  bindFaultCards();
}

function flowSteps(f) {
  const steps = [
    ['REPORTED', '报修'], ['ASSIGNED', '派单'], ['ENROUTE', '在途'], ['ARRIVED', '到场'],
    ['REPAIRING', '维修'], ['REPAIRED', '待复检'], ['APPROVED', '审批通过']];
  const order = ['REPORTED', 'ASSIGNED', 'ENROUTE', 'ARRIVED', 'REPAIRING', 'REPAIRED', 'APPROVED', 'CLOSED'];
  const cur = f.status === 'REINSPECT_REJECTED'
    ? order.indexOf('REPAIRING') : order.indexOf(f.status);
  return steps.map(([key, label], i) => {
    const cls = i <= cur ? 'done' : '';
    return `<span class="step ${cls}">${label}</span>${i < steps.length - 1 ? '<span class="arrow">→</span>' : ''}`;
  }).join('') + (f.reject_count ? `<span class="step reject" style="margin-left:8px">退回 ×${f.reject_count}</span>` : '');
}

function repairPeople() {
  return STATE.people.filter(p => p.role === 'REPAIR')
    .map(p => `<option value="${p.code}">${p.code} ${p.name}</option>`).join('');
}

function faultCard(f) {
  const barUp = f.barricade_status === 'UP';
  const barPending = f.barricade_status === 'PENDING';
  const barLabel = barUp ? '围挡已就位' : (barPending ? '设置围挡' : '围挡已撤除');
  return `<div class="card fault-card" data-fid="${f.id}" data-code="${f.esc_code}">
    <div class="inline">
      <span class="tag-src src-${f.source}">${SRC_LABEL[f.source] || f.source}</span>
      <b>工单 #${f.id}</b>
      <span class="muted small">${f.esc_code} ${f.esc_name}</span>
      <span class="state-pill state-LOCKED">${FAULT_LABEL[f.status]}</span>
      <span style="flex:1"></span>
      <span class="small muted">${f.created_at}</span>
    </div>
    <div class="mt8">${f.reason}</div>
    <div class="flow mt12">${flowSteps(f)}</div>
    <div class="btn-row" data-role-actions>
      ${barPending
        ? '<button class="btn sm danger" data-a="barricade">设置围挡</button>'
        : `<span class="chip ${barUp ? 'ok' : ''}">${barLabel}</span>`}
      <select class="flat sm" data-a="pick-repair" style="display:none">${repairPeople()}</select>
      <button class="btn sm" data-a="dispatch">派单</button>
      <button class="btn sm" data-a="enroute">维修出发</button>
      <button class="btn sm" data-a="arrive">到场登记</button>
      <button class="btn sm" data-a="begin">开始维修</button>
      <button class="btn sm" data-a="complete">完成维修</button>
      <button class="btn sm dark" data-a="reinspect">巡检提交复检(合格)</button>
      <label class="btn sm" style="cursor:pointer">上传故障照片<input type="file" accept="image/*" hidden data-a="photo"></label>
    </div>
    <div class="photos small mt8"></div>
  </div>`;
}

function bindFaultCards() {
  $$('#faultList .card').forEach(card => {
    const fid = Number(card.dataset.fid);
    const get = a => $(`[data-a="${a}"]`, card);
    if (get('barricade')) get('barricade').onclick = async () => {
      try { await api('/api/barricade/place', { fault_id: fid }); toast('围挡已就位'); refresh(); } catch (_) {}
    };
    get('dispatch').onclick = async () => {
      const rp = get('pick-repair').value;
      try { await api('/api/repair/dispatch', { fault_id: fid, repair: rp });
        toast(`已派单给 ${rp}`); refresh(); } catch (_) {}
    };
    get('enroute').onclick = async () => { try { await api('/api/repair/enroute', { fault_id: fid }); refresh(); } catch (_) {} };
    get('arrive').onclick = async () => {
      try { const r = await api('/api/repair/arrive', { fault_id: fid });
        toast(r.late ? '到场登记成功，但已超过到场时限（维修延期）' : '到场登记成功'); refresh(); } catch (_) {}
    };
    get('begin').onclick = async () => {
      const note = prompt('维修措施', '现场排查并处置');
      try { await api('/api/repair/begin', { fault_id: fid, note: note || '' }); refresh(); } catch (_) {}
    };
    get('complete').onclick = async () => {
      const note = prompt('维修结果', '故障已排除，试运转正常');
      try { await api('/api/repair/complete', { fault_id: fid, note: note || '' });
        toast('维修完成，等待巡检复检'); refresh(); } catch (_) {}
    };
    get('reinspect').onclick = async () => {
      const note = prompt('复检说明', '复检项目全部合格');
      try { await api('/api/reinspection/submit', { fault_id: fid, result: 'PASS', note: note || '' });
        toast('复检已提交，等待主管审批'); refresh(); } catch (_) {}
    };
    get('photo').addEventListener('change', async ev => {
      const file = ev.target.files[0];
      if (!file) return;
      const fd = new FormData();
      fd.append('fault_id', fid);
      fd.append('person', STATE.currentPerson);
      fd.append('file', file);
      const res = await fetch('/api/photo', { method: 'POST', body: fd });
      const data = await res.json();
      if (!data.ok) toast(`[${data.error_code}] ${data.error}`, true);
      else { toast('照片已保存到本地目录'); refresh(); }
    });
    loadPhotos(fid, $('.photos', card));
  });
}

function bindFaultCreation() {
  $('#btnSeal').onclick = async () => {
    const code = $('#nfEsc').value, source = $('#nfSource').value;
    const reason = $('#nfReason').value || '人工触发停梯封控';
    try { const r = await api('/api/seal', { code, source, reason });
      toast(`已封控并生成工单 #${r.fault_id}，请尽快设置围挡`);
      $('#nfReason').value = ''; refresh(); } catch (_) {}
  };
}

// ---------------- 视图 6：复检审批 ----------------
async function renderApprove() {
  const pendingOnly = $('#apPendingOnly').checked;
  const { reinspections } = await fetch(`/api/reinspections?pending=${pendingOnly ? 1 : 0}`)
    .then(r => r.json());
  if (!reinspections.length) { $('#approveList').innerHTML = '<div class="card muted">暂无复检单</div>'; return; }
  $('#approveList').innerHTML = reinspections.map(r => `
    <div class="card" data-rid="${r.id}" data-fid="${r.fault_id}">
      <div class="inline">
        <b>复检单 #${r.id}</b>
        <span class="muted small">${r.esc_code} ${r.esc_name} · 工单 #${r.fault_id}</span>
        <span class="chip ${r.result === 'PASS' ? 'ok' : 'bad'}">复检结论：${r.result === 'PASS' ? '合格' : '不合格'}</span>
        <span class="chip ${r.approval === 'APPROVED' ? 'ok' : r.approval === 'REJECTED' ? 'bad' : 'warn'}">
          审批：${{ PENDING: '待审批', APPROVED: '已批准', REJECTED: '已退回' }[r.approval]}</span>
      </div>
      <div class="mt8 small">报修原因：${r.fault_reason}</div>
      <div class="mt8 small">巡检员：<b>${r.inspector_name}</b>；复检说明：${r.note || '—'}</div>
      ${r.approval === 'REJECTED' ? `<div class="lock-reason mt8">退回原因：${r.reject_reason}</div>` : ''}
      ${r.approval === 'PENDING' ? `<div class="btn-row">
          <button class="btn green sm" data-a="ok">主管批准</button>
          <button class="btn ghost-danger sm" data-a="no">主管退回</button>
        </div>` : `<div class="small muted mt8">审批人：${r.approver_name || '—'} · ${r.decided_at || ''}</div>`}
      <div class="reopen-zone">
        <span class="zone-label">复开操作区</span>
        <span class="small muted">仅复检批准后可执行；复开须撤除围挡并二次确认</span>
        <button class="btn reopen" data-a="reopen" ${r.approval !== 'APPROVED' ? 'disabled' : ''}>
          撤围挡 · 复 开（工单 #${r.fault_id}）
        </button>
      </div>
    </div>`).join('');
  $$('#approveList .card').forEach(card => {
    const rid = Number(card.dataset.rid), fid = Number(card.dataset.fid);
    const ok = $('[data-a="ok"]', card), no = $('[data-a="no"]', card), ro = $('[data-a="reopen"]', card);
    if (ok) ok.onclick = async () => { try { await api('/api/reinspection/approve',
      { id: rid, approve: true }); toast('已批准复检'); refresh(); } catch (_) {} };
    if (no) no.onclick = async () => {
      const reason = prompt('退回原因（必填）', '复检发现仍有异常项');
      if (!reason) return;
      try { await api('/api/reinspection/approve', { id: rid, approve: false, reason });
        toast('已退回，工单返回维修环节'); refresh(); } catch (_) {}
    };
    if (ro && !ro.disabled) ro.onclick = () => {
      $('#reopenModalText').textContent = `工单 #${fid} 复检已批准，即将撤除围挡并复开`;
      $('#reopenConfirm').onclick = async () => {
        $('#reopenModal').classList.remove('show');
        try { await api('/api/reopen', { fault_id: fid, confirmed: true });
          toast('围挡撤除，扶梯复开'); refresh(); } catch (_) {}
      };
      $('#reopenModal').classList.add('show');
    };
  });
}

// ---------------- 视图 7：事件查询 ----------------
async function renderEvents() {
  const sel = $('#evEsc');
  if (sel.options.length <= 1) {
    sel.innerHTML = '<option value="">全部扶梯</option>' + escOpts();
    sel.value = '';
  }
  const code = sel.value;
  const kw = $('#evKeyword').value.trim().toLowerCase();
  const { events } = await fetch(`/api/events?${code ? `code=${code}&` : ''}limit=300`)
    .then(r => r.json());
  const rows = events.filter(ev => !kw ||
    ev.type.toLowerCase().includes(kw) || (ev.detail || '').toLowerCase().includes(kw));
  $('#eventList').innerHTML = rows.map(ev => {
    const cls = ALARM_EVENTS.has(ev.type) ? 'alarm' : GOOD_EVENTS.has(ev.type) ? 'good' : '';
    return `<li class="${cls}">
      <span class="t-time">${ev.created_at}</span>
      <span class="t-type">${ev.type}</span>
      <b>${ev.esc_code}</b>
      <span class="muted small">${ev.actor_name ? '· ' + ev.actor_name : '· 系统'}</span>
      ${ev.detail ? `<div class="small mt8">${ev.detail}</div>` : ''}
    </li>`;
  }).join('') || '<li class="muted">无匹配事件</li>';
}

function bindEventFilters() {
  $('#evRefresh').onclick = renderEvents;
  $('#evEsc').onchange = renderEvents;
  $('#evKeyword').addEventListener('keydown', e => { if (e.key === 'Enter') renderEvents(); });
  $('#apPendingOnly').onchange = renderApprove;
}

// ---------------- 绕行总横幅 ----------------
function renderDetourBanners() {
  $('#detourBanners').innerHTML = STATE.detours.map(d => `
    <div class="detour-banner" data-id="${d.id}">
      <div class="inner">
        <span style="font-size:18px">🧭</span>
        <b>客流绕行 · ${d.esc_code}</b>
        <span>${d.message}</span>
        <span style="flex:1"></span>
        <span class="small muted">${d.floor}F 发布 · ${d.created_at.slice(5)}</span>
        <button class="btn sm" data-a="retract">客服撤回</button>
      </div>
    </div>`).join('');
  $$('#detourBanners button').forEach(b => b.onclick = async () => {
    const id = Number(b.closest('.detour-banner').dataset.id);
    try { await api('/api/detour/retract', { id }); toast('绕行提示已撤回'); refresh(); } catch (_) {}
  });
}

// ---------------- 总刷新 ----------------
async function refresh() {
  const s = await fetch('/api/state').then(r => r.json());
  STATE.people = s.people; STATE.escalators = s.escalators;
  STATE.detours = s.detours; STATE.items = s.check_items; STATE.now = s.now;
  renderPersonSelect();
  renderDetourBanners();
  switch (STATE.currentView) {
    case 'floors': renderFloors(); break;
    case 'checklist': renderChecklist(); break;
    case 'running': renderRunning(); break;
    case 'lockdown': renderLockdowns(); break;
    case 'faults': renderFaults(); break;
    case 'approve': renderApprove(); break;
    case 'events': renderEvents(); break;
  }
}

function renderPersonSelect() {
  const sel = $('#personSelect');
  if (sel.options.length !== STATE.people.length) {
    sel.innerHTML = STATE.people.map(p =>
      `<option value="${p.code}">${p.code} ${p.name}（${ROLE_LABEL[p.role]}）</option>`).join('');
    sel.value = STATE.currentPerson || localStorage.getItem('esc_person') || STATE.people[0].code;
    STATE.currentPerson = sel.value;
    renderRole();
  }
}

function renderAll() { refresh(); }

// ---------------- 启动 ----------------
initChrome();
bindChecklistActions();
bindFaultCreation();
bindEventFilters();
tickClock();
setInterval(tickClock, 1000);
refresh();
setInterval(refresh, 5000);
