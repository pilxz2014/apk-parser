/* APK 工具箱 · 主控台前端（原生 JS，零依赖）
 *
 * 设计原则：**侧栏由能力注册表驱动**。
 * 新增一个后端能力 → 在 registry.py 里加一条声明 → 侧栏自动出现。
 * 只要它的 renderer 落在预置的几种里，这里就零改动。
 */
'use strict';

/* ================================================================ 工具 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function txt(s) { return document.createTextNode(s == null ? '' : String(s)); }

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'html') e.innerHTML = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(e.style, v);
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(e.dataset, v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    e.appendChild(typeof kid === 'object' && kid.nodeType ? kid : txt(kid));
  }
  return e;
}

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  const u = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toFixed(n >= 100 ? 0 : 1) + ' ' + u[i];
}

function fmtAgo(ts) {
  if (!ts) return '-';
  const d = Date.now() / 1000 - ts;
  if (d < 60) return Math.floor(d) + ' 秒前';
  if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
  if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
  return Math.floor(d / 86400) + ' 天前';
}

async function copyText(t) {
  try {
    await navigator.clipboard.writeText(t);
    toast('已复制', 'ok');
    return true;
  } catch (e) {
    // 非安全上下文下 clipboard API 不可用 → 回退 execCommand
    const ta = el('textarea', { style: { position: 'fixed', opacity: '0' } }, t);
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { }
    ta.remove();
    toast(ok ? '已复制' : '复制失败，请手动选择', ok ? 'ok' : 'bad');
    return ok;
  }
}

/* ================================================================ API */

async function api(path, opts = {}) {
  const { method = 'GET', body, params } = opts;
  let url = path;
  if (params) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== '' && v != null) q.append(k, v);
    }
    const s = q.toString();
    if (s) url += (url.includes('?') ? '&' : '?') + s;
  }
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const r = await fetch(url, init);
  const ct = r.headers.get('Content-Type') || '';
  if (!ct.includes('json')) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return await r.text();
  }
  const data = await r.json();
  if (!r.ok && data && (data.error || data.detail)) {
    const err = new Error(data.error || data.detail);
    err.payload = data;
    err.status = r.status;
    throw err;
  }
  return data;
}

/* ================================================================ 状态 */

const S = {
  device: localStorage.getItem('apk.device') || '',
  devices: [],
  adbAvailable: true,
  reg: null,          // /api/capabilities 的整张表
  hash: null,
  openApk: localStorage.getItem('apk.lastFile') || '',
  // 上次取到推送的时间戳（秒）。初值取**页面加载那一刻**，不是 0 ——
  // 用 0 的话，每打开一次页面就会把服务端留着的最近 20 条历史推送
  // 全部重放一遍（连弹 20 个提示 + 20 次跳页，最后停在最旧那条）。
  pushSince: Date.now() / 1000,
  // 「浏览…」能否走本机原生对话框。服务端探测（有 tkinter 的 Python → PowerShell），
  // 探测结果由 /api/sys/pick 回给我们。不可用时改用浏览器选择 + 上传。
  pickNative: true,
  pickInfo: null,
  // 最近一次"这条路径是从哪来的"（拖入定位 / 浏览选中）：
  // 用来解释 D 盘下载目录这种"看起来不像原本路径"的路径
  lastLocate: null,
};

function saveDevice(serial) {
  S.device = serial || '';
  if (serial) localStorage.setItem('apk.device', serial);
  else localStorage.removeItem('apk.device');
}

function devParam() { return S.device || ''; }

/* ================================================================ 提示与弹层 */

function toast(msg, kind = '') {
  const t = el('div', { class: 'toast ' + kind }, msg);
  $('#toasts').appendChild(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 5200 : 2600);
}

/**
 * 通用弹层。
 *
 * `onClose` 会在**任何一种**关闭方式下被调一次（点遮罩、`close()`、ESC）。
 * 用它兜住"返回 Promise 的弹层"—— 少了它，用户点遮罩关掉时 Promise 永远
 * 不 resolve，调用方就静静挂在那儿（`pickLocalDir` 正是这种）。
 */
function modal({ title, body, foot, wide, onClose }) {
  const box = el('div', { class: 'modal' + (wide ? ' wide' : '') });
  if (title) box.appendChild(el('div', { class: 'modal-head' }, title));
  if (body) box.appendChild(el('div', { class: 'modal-body' }, body));
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    mask.remove();
    if (onClose) { try { onClose(); } catch (_) { /* 清理失败不该再抛 */ } }
  };
  const mask = el('div', { class: 'mask', onclick: (e) => { if (e.target === mask) close(); } });
  box.appendChild(el('div', { class: 'modal-foot' }, foot || el('button', { class: 'btn', onclick: () => close() }, '关闭')));
  mask.appendChild(box);
  $('#modal-root').appendChild(mask);
  return { close, box };
}

/**
 * 危险操作二次确认：要求把包名原样敲一遍。
 * 后端也会校验（confirm 字段必须等于 pkg），前端这层只是别让人手滑。
 */
function confirmTyping({ title, message, expect, action = '继续', danger = true }) {
  return new Promise((resolve) => {
    let input;
    const okBtn = el('button', {
      class: 'btn ' + (danger ? 'danger' : 'primary'), disabled: true,
      onclick: () => { m.close(); resolve(true); },
    }, action);
    input = el('input', {
      type: 'text', class: 'mono', placeholder: expect,
      oninput: (e) => { okBtn.disabled = e.target.value !== expect; },
    });
    const m = modal({
      title,
      body: el('div', {},
        el('div', { class: 'notice ' + (danger ? 'bad' : 'warn') }, message),
        el('div', { class: 'field' },
          el('label', {}, '请输入 ', el('b', {}, expect), ' 以确认'),
          input),
      ),
      foot: el('div', { style: { display: 'flex', gap: '8px' } },
        el('button', { class: 'btn', onclick: () => { m.close(); resolve(false); } }, '取消'),
        okBtn),
    });
    setTimeout(() => input.focus(), 30);
  });
}

function ask({ title, message, placeholder = '', value = '', action = '确定' }) {
  return new Promise((resolve) => {
    let input = el('input', { type: 'text', class: 'mono', placeholder, value });
    const go = () => { m.close(); resolve(input.value.trim()); };
    const m = modal({
      title,
      body: el('div', {}, message ? el('div', { style: { marginBottom: '8px' } }, message) : null, input),
      foot: el('div', { style: { display: 'flex', gap: '8px' } },
        el('button', { class: 'btn', onclick: () => { m.close(); resolve(null); } }, '取消'),
        el('button', { class: 'btn primary', onclick: go }, action)),
    });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    setTimeout(() => input.focus(), 30);
  });
}

/* ================================================================ UI 原语 */

function card(title, sub, ...body) {
  const head = title || sub
    ? el('div', { class: 'card-head' + (body.length ? '' : ' hd-only') },
        title ? el('div', { class: 'card-title' }, title) : null,
        sub ? el('div', { class: 'card-sub' }, sub) : null)
    : null;
  const kids = [head];
  if (body.length) kids.push(el('div', { class: 'card-body' }, ...body));
  return el('div', { class: 'card' }, ...kids);
}

function pageHead(title, desc, ...actions) {
  return el('div', { class: 'page-head' },
    el('div', { style: { flex: '1 1 auto', minWidth: '0' } },
      el('h2', { class: 'page-title' }, title),
      desc ? el('p', { class: 'page-desc' }, desc) : null),
    actions.length ? el('div', { style: { display: 'flex', gap: '7px', flexWrap: 'wrap' } }, ...actions) : null);
}

function kvBlock(pairs, monoKeys = []) {
  const dl = el('dl', { class: 'kv' });
  for (const [k, v] of pairs) {
    if (v == null || v === '') continue;
    dl.appendChild(el('dt', {}, k));
    dl.appendChild(el('dd', { class: monoKeys.includes(k) ? 'mono' : '' }, v));
  }
  return dl;
}

function notice(level, msg) {
  return el('div', { class: 'notice ' + level }, msg);
}

function spinner(label = '处理中…') {
  return el('div', { class: 'empty' }, el('span', { class: 'spin' }), ' ' + label);
}

function emptyState(icon, text) {
  return el('div', { class: 'empty' }, el('span', { class: 'e-ico' }, icon), text);
}

function codeBlock(cmd, { label = '' } = {}) {
  return el('div', { class: 'cmd' },
    el('button', { class: 'c-copy', onclick: () => copyText(cmd) }, '复制'),
    (label ? label + '\n' : '') + cmd);
}

function tag(text, kind = '') {
  return el('span', { class: 'tag ' + kind }, text);
}

/**
 * 数据表。
 * @param cols  [{key,label,mono,num,width,render}]
 * @param rows  object[]
 * @param opts  {filter:bool, onRow, emptyText}
 */
function dataTable(cols, rows, opts = {}) {
  const wrap = el('div', { class: 'tbl-wrap' });
  const table = el('table', { class: 'tbl' });
  const thead = el('thead', {}, el('tr', {},
    cols.map((c) => el('th', { class: (c.mono ? 'mono ' : '') + (c.num ? 'num' : ''), style: c.width ? { width: c.width } : null }, c.label))));
  const tbody = el('tbody');

  function paint(list) {
    tbody.innerHTML = '';
    if (!list.length) {
      tbody.appendChild(el('tr', {}, el('td', { colspan: cols.length },
        el('div', { class: 'empty' }, opts.emptyText || '没有数据'))));
      return;
    }
    for (const row of list) {
      const tr = el('tr', { class: opts.onRow ? 'clickable' : '' });
      for (const c of cols) {
        const v = c.render ? c.render(row) : row[c.key];
        tr.appendChild(el('td', { class: (c.mono ? 'mono ' : '') + (c.num ? 'num' : '') },
          v == null || v === '' ? el('span', { style: { color: 'var(--dim)' } }, '-') : v));
      }
      if (opts.onRow) tr.addEventListener('click', (e) => {
        if (e.target.closest('button,a,input')) return;
        opts.onRow(row);
      });
      tbody.appendChild(tr);
    }
  }
  paint(rows);
  table.appendChild(thead);
  table.appendChild(tbody);
  wrap.appendChild(table);

  if (opts.filter) {
    const box = el('div', { style: { marginBottom: '9px' } });
    const inp = el('input', {
      type: 'search', placeholder: opts.filterPlaceholder || '过滤…',
      oninput: (e) => {
        const kw = e.target.value.trim().toLowerCase();
        paint(!kw ? rows : rows.filter((r) =>
          cols.some((c) => String(c.render ? '' : (r[c.key] ?? '')).toLowerCase().includes(kw))
          || JSON.stringify(r).toLowerCase().includes(kw)));
      },
    });
    box.appendChild(inp);
    return el('div', {}, box, wrap);
  }
  return wrap;
}

function cardList(items, cols) {
  // 小组件：胶囊式键值列表
  return el('div', { class: 'row' }, ...items.map(([k, v]) =>
    el('span', { class: 'tag' }, k + ' ' + v)));
}

/* ------------------------------------------------ 应用选择框（带模糊过滤）

 * 为什么不用 `<datalist>`：它只按 option 的 `value` 匹配，而我们的 value 是
 * **包名**。用户记得住的是"微信"，不是 `com.tencent.mm` ——
 * 设备上 170 个包的时候，输"微信"一个候选都不出来，等于没有过滤。
 *
 * 这里自己做一个浮层候选：**应用名和包名一起匹配**，还支持空格分词
 * （`微信 mm` 能命中 `com.tencent.mm`）。
 *
 * 关键设计：返回的就是那个 `<input>` 本身，浮层挂在 `document.body` 上
 * （不放在卡片里，免得被 overflow 裁掉）。这样 `pkgInput` 的调用方
 * **一行都不用改**，全站的包名输入框一起获得过滤能力。
 */

const PKG_LIST = { loaded: false, items: [], pending: null };

/** 拉一次设备应用列表并缓存（`all=true` 连系统应用一起）。 */
function loadPkgList(all, force) {
  const key = all ? 'all' : 'user';
  if (PKG_LIST.loaded === key && !force) return Promise.resolve(PKG_LIST.items);
  if (PKG_LIST.pending) return PKG_LIST.pending;
  PKG_LIST.pending = (async () => {
    try {
      const r = await api('/api/apps', {
        params: { device: devParam(), system: all ? '1' : '', refresh: force ? '1' : '' },
      });
      PKG_LIST.items = r.apps || [];
    } catch (e) {
      PKG_LIST.items = [];
    }
    PKG_LIST.loaded = key;
    PKG_LIST.pending = null;
    return PKG_LIST.items;
  })();
  return PKG_LIST.pending;
}

/** `微信 mm` 这种空格分词全命中才算过；再按"名字命中"优先排。 */
function matchApps(items, kw) {
  const toks = String(kw || '').toLowerCase().split(/\s+/).filter(Boolean);
  const out = [];
  for (const a of items) {
    const label = String(a.label || '');
    const pkg = String(a.package || '');
    const hay = (label + ' ' + pkg).toLowerCase();
    let ok = true;
    for (const t of toks) { if (hay.indexOf(t) < 0) { ok = false; break; } }
    if (!ok) continue;
    // 名字命中排前面：用户输的多半是名字
    const score = label.toLowerCase().indexOf(toks[0] || '') >= 0 ? 0
      : (pkg.toLowerCase().indexOf(toks[0] || '') >= 0 ? 1 : 2);
    out.push({ app: a, score });
  }
  out.sort((x, y) => (x.score - y.score)
    || String(x.app.label || '').localeCompare(String(y.app.label || '')));
  return out.map((x) => x.app);
}

function pkgCombo(placeholder, value, opts = {}) {
  const all = !!opts.all;
  const inp = el('input', {
    type: 'text', class: 'mono combo-input', autocomplete: 'off', spellcheck: 'false',
    placeholder: placeholder || 'com.example.app', value: value || '',
  });
  const panel = el('div', { class: 'combo-panel' });
  document.body.appendChild(panel);          // 挂 body：不被卡片 overflow 裁掉

  let items = [];
  let shown = [];
  let hi = -1;

  const close = () => { panel.classList.remove('open'); hi = -1; };

  function place() {
    const r = inp.getBoundingClientRect();
    panel.style.left = Math.max(4, r.left) + 'px';
    panel.style.top = (r.bottom + 2) + 'px';
    panel.style.width = Math.max(r.width, 260) + 'px';
  }

  function paint() {
    panel.innerHTML = '';
    if (!shown.length) {
      panel.appendChild(el('div', { class: 'combo-empty' },
        items.length ? '没有匹配的应用' : '（还没读到应用列表）'));
      return;
    }
    shown.forEach((a, i) => {
      panel.appendChild(el('div', {
        class: 'combo-item' + (i === hi ? ' hi' : ''),
        title: a.package,
        // mousedown 上 preventDefault：否则 input 先 blur、面板先关，点击就丢了
        onmousedown: (e) => e.preventDefault(),
        onclick: () => {
          inp.value = a.package;
          close();
          inp.dispatchEvent(new Event('input', { bubbles: true }));
          inp.dispatchEvent(new Event('change', { bubbles: true }));
        },
      },
        el('span', { class: 'ci-label' }, a.label || '（无应用名）'),
        el('span', { class: 'ci-pkg' }, a.package)));
    });
  }

  function refilter() {
    const kw = inp.value.trim();
    // 输入框里已经是一个完整包名时不弹面板（正在挑完/手输时的噪音）
    const exact = items.some((a) => a.package === kw);
    shown = kw && !exact ? matchApps(items, kw).slice(0, 60)
      : (kw ? [] : items.slice(0, 60));
    hi = -1;
    paint();
  }

  function open() {
    loadPkgList(all).then((list) => {
      items = list || [];
      if (document.activeElement !== inp) return;   // 已经走开了就别弹
      place();
      panel.classList.add('open');
      refilter();
    });
  }

  inp.addEventListener('focus', open);
  inp.addEventListener('input', () => {
    if (!panel.classList.contains('open')) {
      if (!items.length) { open(); return; }
      place();
      panel.classList.add('open');
    }
    refilter();
  });
  inp.addEventListener('blur', () => setTimeout(close, 160));
  inp.addEventListener('keydown', (e) => {
    if (!panel.classList.contains('open')) {
      if (e.key === 'ArrowDown') { e.preventDefault(); open(); }
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault(); hi = Math.min(hi + 1, shown.length - 1); paint();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault(); hi = Math.max(hi - 1, -1); paint();
    } else if (e.key === 'Enter') {
      if (hi >= 0 && shown[hi]) {
        e.preventDefault();
        inp.value = shown[hi].package;
        close();
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
      } else {
        close();
      }
    } else if (e.key === 'Escape') {
      close();
    }
  });
  // 页面滚动/改窗口大小时浮层会错位 —— 直接关掉比跟位算得准。
  // 但**面板自己滚动**（列表长了用滚轮翻）不该关 —— 捕获阶段能收到面板内部的
  // scroll 事件，必须按 target 排除，否则"滚动查看列表"等于"滚动关闭列表"。
  window.addEventListener('scroll', (e) => {
    if (panel.contains(e.target) || e.target === panel) return;
    close();
  }, true);
  window.addEventListener('resize', close);
  return inp;
}

/** 让一个输入框带"已装应用"候选。返回值就是 `<input>`，老调用方零改动。 */
async function pkgInput(placeholder = 'com.example.app', value = '') {
  return pkgCombo(placeholder, value);
}

/* ================================================================ SSE */

function sse(path, handlers, params) {
  let url = path;
  if (params) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== '' && v != null) q.append(k, v);
    const s = q.toString();
    if (s) url += (url.includes('?') ? '&' : '?') + s;
  }
  const es = new EventSource(url);
  for (const [ev, fn] of Object.entries(handlers || {})) {
    if (ev === 'error') continue;
    es.addEventListener(ev, (e) => {
      let d = null;
      try { d = JSON.parse(e.data); } catch (_) { d = e.data; }
      fn(d);
    });
  }
  es.addEventListener('error', () => {
    if (handlers && handlers.error) handlers.error();
  });
  return es;
}

/* ================================================================ 设备栏 */

async function loadDevices(silent) {
  const sel = $('#dev-pick');
  const st = $('#dev-state');
  try {
    const r = await api('/api/devices');
    S.devices = r.devices || [];
    S.adbAvailable = !!r.adbAvailable;
    sel.innerHTML = '';
    if (!S.adbAvailable) {
      sel.appendChild(el('option', { value: '' }, 'adb 不可用'));
      st.className = 'tag bad';
      st.textContent = '未找到 adb';
      return;
    }
    if (!S.devices.length) {
      sel.appendChild(el('option', { value: '' }, '（没有连接设备）'));
      st.className = 'tag warn';
      st.textContent = '无设备';
      return;
    }
    const online = S.devices.filter((d) => d.state === 'device');
    const pickable = online.length ? online : S.devices;
    for (const d of pickable) {
      sel.appendChild(el('option', { value: d.serial },
        d.model ? `${d.model} (${d.serial})` : d.serial));
    }
    if (!pickable.some((d) => d.serial === S.device)) {
      saveDevice(pickable[0].serial);
    }
    sel.value = S.device;
    const cur = pickable.find((d) => d.serial === S.device) || pickable[0];
    st.className = 'tag ' + (online.length ? 'ok' : 'warn');
    st.textContent = cur ? `${cur.androidVersion || '?'} · ${cur.resolution || '?'}` : '';
    if (S.devices.some((d) => d.state === 'unauthorized')) {
      st.className = 'tag warn';
      st.textContent = '有设备未授权';
    }
  } catch (e) {
    sel.innerHTML = '';
    sel.appendChild(el('option', { value: '' }, '枚举失败'));
    st.className = 'tag bad';
    st.textContent = e.message;
    if (!silent) toast('设备枚举失败：' + e.message, 'bad');
  }
}

/* ================================================================ 侧栏 / 路由

 * 侧栏是**三级**：域（本机 / 设备 / 系统）→ 功能组 → 能力。
 * 分组和名称都来自服务端注册表（`registry.py`），前端只负责排版与折叠，
 * 所以"加一个功能"依旧不用改这里。
 *
 * 折叠状态存 localStorage；导航到某项时**自动展开**它所在的域与组 ——
 * 否则用户点完链接会看不见自己在哪。
 */

const NAV_COLLAPSE_KEY = 'apk.nav.collapsed';
const NAV_DOMAINS = [
  { id: 'local', title: '本机', icon: '💻', desc: '处理本机 APK 文件，不需要设备' },
  { id: 'device', title: '设备', icon: '📱', desc: '通过 adb 操作手机/平板' },
  { id: 'system', title: '系统', icon: '⚙️', desc: '右键菜单 / 环境自检 / 配置 / 主控台' },
];

function navCollapsed() {
  try {
    const m = JSON.parse(localStorage.getItem(NAV_COLLAPSE_KEY) || '{}');
    return (m && typeof m === 'object') ? m : {};
  } catch (_) { return {}; }
}

function isCollapsed(key) { return !!navCollapsed()[key]; }

function setCollapsed(key, val) {
  const m = navCollapsed();
  if (val) m[key] = 1; else delete m[key];
  try { localStorage.setItem(NAV_COLLAPSE_KEY, JSON.stringify(m)); } catch (_) { /* 隐私模式 */ }
}

/** 收起/展开一个域或组；`.closed` 决定箭头方向与内容显隐（都交给 CSS）。 */
function toggleNavNode(node, key) {
  const now = !node.classList.contains('closed');
  node.classList.toggle('closed', now);
  setCollapsed(key, now);
}

/** 把某个节点往上所有收起的祖先展开（导航到某项时用）。 */
function expandAncestors(node) {
  let n = node.parentNode;
  while (n && n.nodeType === 1) {
    if (n.dataset && n.dataset.key && n.classList.contains('closed')) {
      n.classList.remove('closed');
      setCollapsed(n.dataset.key, false);
    }
    n = n.parentNode;
  }
}

function navItemEl(c) {
  return el('div', {
    class: 'nav-item',
    dataset: { cap: c.id },
    title: [c.description || c.title,
      c.cli ? 'CLI：apktool.py ' + c.cli : '',
      c.api ? 'API：' + c.api : ''].filter(Boolean).join('\n'),
    onclick: () => go(c.id),
  },
    el('span', { class: 'n-ico' }, c.icon || '·'),
    el('span', { class: 'n-txt' }, c.title),
    c.risk === 'dangerous' ? el('span', { class: 'n-risk', title: c.riskText }, '!') : null,
    c.needsDevice ? el('span', { class: 'n-dev', title: '需要设备' }, 'USB') : null);
}

function buildNav() {
  const bar = $('#sidebar');
  bar.innerHTML = '';
  const reg = S.reg;
  if (!reg) return;

  const groups = (reg.groups || []).slice().sort((a, b) => (a.order || 0) - (b.order || 0));
  const caps = reg.capabilities || [];
  // 被合并进父页面的能力（`hidden`）不上侧栏 —— 它们只是父页面里的一个 tab。
  // 能力声明本身还在，所以 CLI / API / 老书签一个都不少。
  const capsOf = (gid) => caps.filter((c) => c.group === gid && !c.hidden);

  bar.appendChild(el('div', { class: 'nav-tools' },
    el('button', {
      class: 'btn sm', title: '展开全部',
      onclick: () => { try { localStorage.removeItem(NAV_COLLAPSE_KEY); } catch (_) {} buildNav(); },
    }, '全展开'),
    el('button', {
      class: 'btn sm', title: '折叠到只剩一级',
      onclick: () => {
        const m = {};
        for (const d of NAV_DOMAINS) m['dom:' + d.id] = 1;
        for (const g of groups) m['grp:' + g.id] = 1;
        try { localStorage.setItem(NAV_COLLAPSE_KEY, JSON.stringify(m)); } catch (_) {}
        buildNav();
      },
    }, '全折叠')));

  for (const dom of NAV_DOMAINS) {
    const mine = groups.filter((g) => g.domain === dom.id && capsOf(g.id).length);
    if (!mine.length) continue;
    const dkey = 'dom:' + dom.id;

    const dbody = el('div', { class: 'nav-dom-bd' });
    for (const g of mine) {
      const gkey = 'grp:' + g.id;
      const items = capsOf(g.id);
      const gbody = el('div', { class: 'nav-grp-bd' }, ...items.map(navItemEl));
      const gnode = el('div', {
        class: 'nav-grp' + (isCollapsed(gkey) ? ' closed' : ''),
        dataset: { key: gkey },
      },
        el('div', {
          class: 'nav-grp-hd', title: g.description || g.title,
          onclick: () => toggleNavNode(gnode, gkey),
        },
          el('span', { class: 'caret' }),
          el('span', { class: 'g-ico' }, g.icon || '·'),
          el('span', { class: 'g-txt' }, g.title),
          el('span', { class: 'n-count' }, String(items.length))),
        gbody);
      dbody.appendChild(gnode);
    }

    const dnode = el('div', {
      class: 'nav-dom' + (isCollapsed(dkey) ? ' closed' : ''),
      dataset: { key: dkey },
    },
      el('div', {
        class: 'nav-dom-hd', title: dom.desc,
        onclick: () => toggleNavNode(dnode, dkey),
      },
        el('span', { class: 'caret' }),
        el('span', { class: 'd-ico' }, dom.icon),
        el('span', { class: 'd-txt' }, dom.title),
        el('span', { class: 'n-count' }, String(mine.reduce((s, g) => s + capsOf(g.id).length, 0)))),
      dbody);
    bar.appendChild(dnode);
  }
  markActive();
}

function markActive() {
  const id = S.hash && S.hash.id;
  $$('.nav-item').forEach((n) => n.classList.toggle('active', n.dataset.cap === id));
  const cur = $(`.nav-item[data-cap="${id}"]`);
  if (cur) expandAncestors(cur);
}

function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  if (!h) return null;
  const [path, qs] = h.split('?');
  const parts = path.split('/').filter(Boolean);
  const id = parts[0] === 'cap' ? parts.slice(1).join('/') : parts.join('/');
  const q = {};
  if (qs) for (const [k, v] of new URLSearchParams(qs)) q[k] = v;
  return { id, q };
}

function go(id, q) {
  const qs = q ? '?' + new URLSearchParams(q).toString() : '';
  location.hash = '#/cap/' + id + qs;
}

function render() {
  S.hash = parseHash();
  markActive();
  const main = $('#main');
  main.innerHTML = '';
  PATH_BARS.length = 0;   // 旧页面的路径条已随 DOM 销毁，清掉引用
  // 应用选择框的浮层挂在 body 上（不被卡片 overflow 裁），所以换页面时要自己收
  $$('.combo-panel').forEach((e) => e.remove());
  if (!S.hash) { go('local.parse'); return; }
  const cap = (S.reg.capabilities || []).find((c) => c.id === S.hash.id);
  if (!cap) {
    main.appendChild(pageHead('未知功能', S.hash.id));
    main.appendChild(card(null, null, notice('bad', '注册表里没有这个能力：' + S.hash.id)));
    return;
  }

  // 指向一个**已被合并**的能力（老书签 / CLI 帮助里抄来的链接 / 老前端跳转）
  // → 平滑转到它父页面的那一格，而不是显示一个空壳页面。
  if (cap.hidden && cap.tabOf) {
    go(cap.tabOf, Object.assign({}, S.hash.q || {}, { tab: cap.tabKey || '' }));
    return;
  }

  main.appendChild(pageHead(cap.title, cap.description, cap.cli ? tag(cap.cli, 'plain') : null));

  if (cap.needsDevice && S.adbAvailable && !S.devices.some((d) => d.state === 'device')) {
    main.appendChild(notice('warn', '这个功能需要设备，但当前没有在线设备。请连上 USB 并打开调试。'));
  }

  const fn = VIEWS[cap.id] || viewGeneric;
  try {
    fn(main, cap, S.hash.q || {});
  } catch (e) {
    main.appendChild(notice('bad', '渲染失败：' + e.message));
  }
}

/* ================================================================ 视图 */

const VIEWS = {};

/* ------------------------------------------------ tab（合并页面的骨架）

 * 注册表里一条能力可以声明 `tabOf` —— 意思是"我不是一个独立页面，
 * 我是某个页面里的一格"。前端在这里把「父页面 + 它的子能力」拼成
 * 一条 tab 栏 + 下面一块内容区。
 *
 * **tab 清单不写死在前端**：从 `S.reg.capabilities` 现算，
 * 所以后端加一个 `tabOf` 就自动多一格，这一点和侧栏是同一套逻辑。
 */

/** 一个页面的 tab 清单（父自己排第一）。顺序 = 注册表里的声明顺序。 */
function tabsOf(capId) {
  const caps = (S.reg && S.reg.capabilities) || [];
  const me = caps.find((c) => c.id === capId);
  if (!me) return [];
  const out = [{ key: me.tabKey || 'main', label: me.tabLabel || me.title, cap: capId, own: true }];
  const seen = { [out[0].key]: 1 };
  for (const c of caps) {
    if (c.tabOf !== capId) continue;
    // 别名（alias）不占格：它和父页自己那一格是同一个东西
    if (c.alias || seen[c.tabKey]) continue;
    seen[c.tabKey] = 1;
    out.push({ key: c.tabKey, label: c.tabLabel || c.title, cap: c.id, own: false });
  }
  return out;
}

function tabsBar(tabs, cur, onPick) {
  return el('div', { class: 'tabs' }, ...tabs.map((t) => el('button', {
    class: 'tab' + (t.key === cur ? ' on' : ''),
    'data-tab': t.key,
    onclick: () => onPick(t.key),
  }, t.label)));
}

/**
 * 把父页面 + 子能力拼成一个带 tab 的视图。
 *
 * @param parentId 父能力 id（也是注册表里的页面 id）
 * @param ownFn    父页面**自己那一格**的渲染函数（就原来那个单页实现）
 */
function mergedPage(parentId, ownFn) {
  return async function (host, cap, q) {
    const tabs = tabsOf(parentId);
    if (tabs.length < 2) {          // 没合并出东西来 → 就当普通页渲染
      await ownFn(host, cap, q);
      return;
    }
    // 没指定 tab（或指定的 tab 已不存在）就落在第一格 —— 别显示一条空白
    let cur = q.tab;
    if (!tabs.some((t) => t.key === cur)) cur = tabs[0].key;

    host.appendChild(tabsBar(tabs, cur, (k) => {
      const nq = Object.assign({}, q, { tab: k });
      go(parentId, nq);
    }));

    const def = tabs.find((t) => t.key === cur);
    const fn = def.own ? ownFn : (VIEWS[def.cap] || viewGeneric);
    const box = el('div', { class: 'tab-body' });
    host.appendChild(box);
    try {
      await fn(box, cap, q);
    } catch (e) {
      box.appendChild(notice('bad', '渲染失败：' + e.message));
    }
  };
}

/* ------------------------------------------------ 拖拽载入 APK */

/**
 * 浏览器**不给**拖入文件的真实路径（`File.path` 是 Electron 私货），
 * 但服务端就在本机 —— 拿 name/size/mtime 去 `/api/apk/locate` 搜回来。
 */
const APK_EXT = /\.(apk|apks|xapk|aab)$/i;
const PATH_BARS = [];   // {inp, go, auto}，页面里的"APK 路径条"自己注册进来

function dragHasFiles(e) {
  const dt = e.dataTransfer;
  if (!dt) return false;
  const types = dt.types || [];
  for (const t of types) if (t === 'Files') return true;
  return false;
}

function dropMask(show, text) {
  let m = $('#drop-mask');
  if (!show) { if (m) m.remove(); return; }
  if (!m) {
    m = el('div', { id: 'drop-mask' },
      el('div', { class: 'dm-box' },
        el('div', { class: 'dm-ico' }, '📥'),
        el('div', { class: 'dm-t1' }, text || '松开鼠标载入 APK'),
        el('div', { class: 'dm-t2' }, '支持 .apk / .apks / .xapk / .aab')));
    document.body.appendChild(m);
  } else {
    const t1 = m.querySelector('.dm-t1');
    if (t1 && text) t1.textContent = text;
  }
}

/** 把路径铺到当前页面上所有的"路径条"；auto=true 时自动执行标记了 auto 的那条。 */
function applyPath(p, { auto = false, source = '' } = {}) {
  S.openApk = p;
  localStorage.setItem('apk.lastFile', p);
  const bars = PATH_BARS.filter((b) => document.contains(b.inp));
  bars.forEach((b) => {
    b.inp.value = p;
    b.inp.classList.add('filled');
    if (b.onPath) b.onPath(p);
  });
  renderPathNote();
  const target = auto && bars.find((b) => b.auto);
  if (target) { target.go(); return; }
  if (bars.length) { toast('已载入路径' + (source ? '（' + source + '）' : '') + '，点按钮继续', 'ok'); return; }
  // 当前页面没有路径条 → 直接跳到「解析 APK」并开解析
  go('local.parse', { path: p });
}

/* ------------------------------------------------ 路径归属说明

 * 用户看到 `D:\Backup\Downloads\xxx.apk` 常常以为"路径被工具改写了"。
 * 真相往往是：Windows 的「下载」文件夹被**重定向**到了 D 盘 ——
 * 那串 D 盘路径就是它的**真实位置**，资源管理器地址栏里也是这个。
 *
 * 服务端（`paths.folder_of()` 读注册表 `Shell Folders`）会把归属和
 * 重定向事实一起带回来，这里只负责讲清楚，并允许用户在同名多命中时自己挑。
 */

const PATH_NOTES = [];   // 页面里的"路径说明"容器

function locateMemo(info) {
  S.lastLocate = info || null;
  return info;
}

function locateNoticeText(info) {
  if (!info) return '';
  if (info.folderNote) return info.folderNote;
  if (info.folder) return '位于系统「' + info.folder + '」文件夹：' + (info.folderRoot || '');
  return '';
}

function renderPathNote() {
  const text = locateNoticeText(S.lastLocate);
  for (const b of PATH_NOTES) {
    if (!document.contains(b.host)) continue;
    b.host.innerHTML = '';
    if (text) {
      b.host.appendChild(notice(S.lastLocate.folderRedirected ? 'warn' : 'ok', text));
    }
  }
}

/** 同名文件命中多个时，让用户自己挑 —— 猜错比多问一句更糟。 */
function chooseLocatedCandidate(cands, cur) {
  return new Promise((resolve) => {
    const body = el('div');
    body.appendChild(el('p', { class: 'cand-hint' },
      '本机找到 ' + cands.length + ' 个同名文件。浏览器不提供拖入文件的真实路径，'
      + '只能按「文件名 + 体积」找，请确认要用哪一个：'));
    let m = null;
    for (const c of cands) {
      const meta = [fmtBytes(c.size || 0),
        c.mtime ? new Date(c.mtime * 1000).toLocaleString() : '',
        c.folder ? ('系统「' + c.folder + '」文件夹') : ''].filter(Boolean).join(' · ');
      body.appendChild(el('div', { class: 'cand' + (c.path === cur ? ' best' : '') },
        el('div', { class: 'c-main' },
          el('div', { class: 'c-path mono' }, c.path),
          el('div', { class: 'c-meta' }, meta)),
        el('button', {
          class: 'btn sm' + (c.path === cur ? ' primary' : ''),
          onclick: () => { m.close(); resolve(c); },
        }, c.path === cur ? '用这个（最像）' : '用这个')));
    }
    m = modal({
      title: '选择要解析的文件', body, wide: true,
      foot: el('button', { class: 'btn', onclick: () => { m.close(); resolve(null); } }, '取消'),
    });
  });
}

/** 把 `/api/apk/locate` 的响应收成一条"路径说明"。 */
function locateInfoFrom(r, cand, source) {
  const c = cand || {};
  return {
    path: c.path || r.path,
    folder: c.folder || r.folder,
    folderRoot: c.folderRoot || r.folderRoot,
    folderNote: c.path && c.path !== r.path ? '' : r.folderNote,
    folderRedirected: c.folderRedirected != null ? c.folderRedirected : r.folderRedirected,
    source: source || '',
  };
}

async function loadDroppedFile(file) {
  if (!APK_EXT.test(file.name)) {
    toast('只支持 .apk / .apks / .xapk / .aab，收到的是 ' + file.name, 'bad');
    return;
  }
  dropMask(true, '正在本机定位「' + file.name + '」…');
  try {
    const r = await api('/api/apk/locate', {
      method: 'POST',
      body: {
        name: file.name,
        size: file.size || 0,
        mtime: Math.floor((file.lastModified || 0) / 1000),
      },
    });
    if (!r || !r.ok) {
      toast((r && (r.hint || r.error)) || '没能定位到文件', 'bad');
      return;
    }
    let use = r.path;
    let cand = null;
    // 同名命中多个（不同目录/不同体积）→ 让人自己挑。
    // 浏览器只能给"文件名+体积"，猜错会让用户对着另一个文件排查半天。
    if ((r.candidates || []).length > 1) {
      cand = await chooseLocatedCandidate(r.candidates, r.path);
      if (!cand) { toast('已取消（没有载入任何文件）', 'warn'); return; }
      use = cand.path;
    }
    locateMemo(locateInfoFrom(r, cand, '拖入'));
    applyPath(use, { auto: true, source: '拖入' });
  } catch (e) {
    toast('定位失败：' + e.message + '（可用「浏览…」直接选）', 'bad');
  } finally {
    dropMask(false);
  }
}

function bindGlobalDrop() {
  let depth = 0;
  window.addEventListener('dragenter', (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    depth += 1;
    dropMask(true);
  });
  window.addEventListener('dragover', (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  window.addEventListener('dragleave', (e) => {
    if (!dragHasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) dropMask(false);
  });
  window.addEventListener('drop', (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    dropMask(false);
    const files = Array.from(e.dataTransfer.files || []);
    if (!files.length) { toast('没读到文件', 'bad'); return; }
    if (files.length > 1) {
      toast('一次拖一个：' + files.length + ' 个文件里先用「' + files[0].name + '」', 'warn');
    }
    loadDroppedFile(files[0]);
  });
}

/* ------------------------------------------------ 选 APK：原生对话框 → 浏览器兜底

 * 首选**服务端原生对话框**：服务端就在本机，能直接把真实路径回给页面，
 * 于是解析一个 APK 不必先复制一份。
 *
 * 但它依赖具体后端（见 platform/filedialog.py）：
 *   ① 某个带 tkinter 的 Python 解释器   —— 注意**不是**必然有，
 *      隔离环境里的 Python 常被裁掉 tkinter（本机 managed 3.13 就是）
 *   ② Windows 上退化到 PowerShell + WinForms
 * 两条都不可用时，改用浏览器 `<input type=file>`；浏览器不给真实路径，
 * 所以先拿 name/size 去本机定位（命中就不用复制），实在找不到才上传副本。
 */

/** 问服务端「原生对话框能不能用」。结果缓存，避免每次点都探测。 */
async function probePickBackend() {
  try {
    const r = await api('/api/sys/pick');
    if (r && typeof r === 'object') {
      S.pickInfo = r;
      S.pickNative = !!r.native;
      if (!r.native) console.warn('[apk-parser] 原生对话框不可用：', r.tried || r.detail);
    }
  } catch (_) { /* 探测失败就按"可用"试一次，失败再降级 */ }
}

/** 用浏览器选一个文件（拿不到真实路径，只有 File 对象）。 */
function chooseFileInBrowser() {
  return new Promise((resolve) => {
    const inp = el('input', {
      type: 'file', accept: '.apk,.apks,.xapk,.aab',
      style: { position: 'fixed', left: '-9999px' },
    });
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      setTimeout(() => inp.remove(), 0);
      resolve(v);
    };
    inp.addEventListener('change', () => finish((inp.files && inp.files[0]) || null));
    // 取消时 change 不触发 —— 窗口重新获得焦点后兜底判定为"取消"
    window.addEventListener('focus', () => setTimeout(() => finish(null), 1200), { once: true });
    const timer = setTimeout(() => finish(null), 300000);
    document.body.appendChild(inp);
    inp.click();
  });
}

/** 上传一个文件到本机临时目录，返回服务端落盘信息。 */
function uploadApkFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/apk/upload?name=' + encodeURIComponent(file.name || 'upload.apk'));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    if (xhr.upload && onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && e.total) onProgress(e.loaded / e.total);
      };
    }
    xhr.onload = () => {
      let d = {};
      try { d = JSON.parse(xhr.responseText || '{}'); } catch (_) { d = {}; }
      if (xhr.status >= 200 && xhr.status < 300 && d && d.ok) resolve(d);
      else reject(new Error((d && d.error) || ('上传失败：HTTP ' + xhr.status)));
    };
    xhr.onerror = () => reject(new Error('无法连接主控台'));
    xhr.ontimeout = () => reject(new Error('上传超时'));
    xhr.onabort = () => reject(new Error('上传已取消'));
    xhr.send(file);
  });
}

/** 浏览器给的是 File 对象 —— 先试着在本机定位原件，找不到才上传副本。 */
async function resolveLocalFile(file) {
  if (!APK_EXT.test(file.name || '')) {
    toast('不是 APK 文件：' + (file.name || ''), 'bad');
    return null;
  }
  // ① 本机定位：命中就完全不用复制文件
  try {
    const r = await api('/api/apk/locate', {
      method: 'POST',
      body: {
        name: file.name,
        size: file.size || 0,
        mtime: Math.floor((file.lastModified || 0) / 1000),
      },
    });
    if (r && r.ok && r.path) {
      let use = r.path;
      let cand = null;
      if ((r.candidates || []).length > 1) {
        cand = await chooseLocatedCandidate(r.candidates, r.path);
        if (!cand) return null;
        use = cand.path;
      }
      locateMemo(locateInfoFrom(r, cand, '本机定位'));
      return { path: use, name: file.name, bytes: file.size, via: 'locate' };
    }
  } catch (_) { /* 定位失败不致命，继续走上传 */ }

  // ② 上传副本
  const t = el('div', { class: 'toast' }, '正在上传 ' + file.name + ' …');
  $('#toasts').appendChild(t);
  try {
    const d = await uploadApkFile(file, (p) => {
      t.textContent = '正在上传 ' + file.name + ' · ' + Math.round(p * 100) + '%';
    });
    t.remove();
    toast('已上传副本到临时目录（' + fmtBytes(d.bytes) + '）', 'ok');
    locateMemo({ path: d.path, source: '上传副本',
      folderNote: '这是浏览器上传的副本（浏览器不给真实路径）—— 解析没问题，但它不是你磁盘上那个文件' });
    return { path: d.path, name: d.name || file.name, bytes: d.bytes, via: 'upload' };
  } catch (e) {
    t.remove();
    toast('上传失败：' + e.message, 'bad');
    return null;
  }
}

/**
 * 统一的"选一个 APK"入口。成功返回 {path, name, bytes, via}，取消返回 null。
 */
async function pickApk() {
  // ---- ① 原生对话框（服务端弹，直接拿真实路径）
  if (S.pickNative) {
    let r = null;
    try {
      r = await api('/api/apk/pick', { method: 'POST', body: {} });
    } catch (e) {
      r = { error: e.message };
    }
    if (r && r.path) {
      locateMemo({
        path: r.path, folder: r.folder, folderRoot: r.folderRoot,
        folderNote: r.folderNote, folderRedirected: r.folderRedirected,
        source: '浏览',
      });
      return { path: r.path, name: r.name, bytes: r.bytes, via: 'native' };
    }
    if (r && r.cancelled) return null;                  // 用户主动取消 —— 安静
    if (r && r.fallback === 'browser') {
      S.pickNative = false;                             // 记住，别再白试一次
      toast('没有可用的原生对话框，改用浏览器选择（找不到原件时会复制一份副本）', 'warn');
    } else if (r && r.error) {
      toast(r.error, 'bad');
      return null;
    }
  }

  // ---- ② 浏览器兜底（此刻通常仍在用户手势有效期内，能正常弹选择框）
  const f = await chooseFileInBrowser();
  if (!f) return null;
  return await resolveLocalFile(f);
}

/* ------------------------------------------------ 本机目录选择

 * 「导出到哪儿 / 扫哪个目录」以前只能**手打绝对路径**。让人默写自己磁盘的
 * 结构是很不友好的交互，所以做两条路：
 *   ① 原生目录对话框（服务端弹，一次点击到位）—— 首选
 *   ② 页面内的目录浏览器（列本机目录，点着进）—— 没有原生后端时兜底，
 *      也顺便给"我就想看看我 D 盘都放了啥"这种需求用
 */

/**
 * 弹一个"选择本机目录"。选到返回路径字符串，取消返回 null。
 */
async function pickLocalDir({ title = '选择本机目录', start = '' } = {}) {
  let r = null;
  try {
    r = await api('/api/sys/pickdir', { method: 'POST', body: { title, start } });
  } catch (e) {
    r = { error: e.message };
  }
  if (r && r.path) return r.path;
  if (r && r.cancelled) return null;                 // 用户主动取消 → 安静
  if (r && r.fallback !== 'inpage' && r.error) toast(r.error, 'warn');
  // 原生后端不可用 → 退页面内的目录浏览器（服务端在本机，列本机目录是安全的）
  return await localDirPicker({ title, start });
}

/** 页面内的本机目录浏览器（挑一个目录；面包屑 + 盘符 + 常用文件夹可跳）。 */
function localDirPicker({ title = '选择本机目录', start = '' } = {}) {
  return new Promise((resolve) => {
    let cur = start || '';
    let upPath = '';
    let done = false;
    let m = null;                     // 先建 body 再建 modal，所以这里用变量兜一下

    const finish = (v) => {
      if (done) return;
      done = true;
      if (m) m.close();
      resolve(v);
    };

    const crumbBox = el('div', { class: 'crumbs' });
    const rootBox = el('div', { class: 'row dirpick-roots' });
    const listBox = el('div', { class: 'dirpick-list' });
    const noteBox = el('div');
    const curEl = el('span', { class: 'mono cur-dir' }, '');
    const upBtn = el('button', {
      class: 'btn', disabled: true,
      onclick: () => { if (upPath && upPath !== cur) goDir(upPath); },
    }, '↑ 上级');
    const okBtn = el('button', { class: 'btn primary', onclick: () => finish(cur) }, '选择此目录');

    function paintCrumbs(r) {
      crumbBox.innerHTML = '';
      const crumbs = r.crumbs || [];
      crumbs.forEach((c, i) => {
        crumbBox.appendChild(el('span', {
          class: 'crumb' + (i === crumbs.length - 1 ? ' cur' : ''),
          onclick: () => goDir(c.path),
        }, c.name));
        if (i < crumbs.length - 1) crumbBox.appendChild(el('span', { class: 'crumb-sep' }, '›'));
      });
    }

    function paintRoots(r) {
      rootBox.innerHTML = '';
      const roots = r.roots || {};
      for (const d of (roots.drives || [])) {
        rootBox.appendChild(el('button', { class: 'btn sm', onclick: () => goDir(d.path) }, d.name));
      }
      for (const f of (roots.known || [])) {
        if (!f.path) continue;
        rootBox.appendChild(el('button', {
          class: 'btn sm', title: (f.note || '') + '　' + f.path,
          onclick: () => goDir(f.path),
        }, (f.icon ? f.icon + ' ' : '') + (f.title || f.name || f.path)));
      }
    }

    async function goDir(p) {
      listBox.innerHTML = '';
      listBox.appendChild(spinner('读取 ' + (p || '主目录') + ' …'));
      noteBox.innerHTML = '';
      let r = null;
      try {
        r = await api('/api/fs/ls', { params: { path: p || '', dirsOnly: '1' } });
      } catch (e) {
        listBox.innerHTML = '';
        listBox.appendChild(notice('bad', e.message));
        return;
      }
      listBox.innerHTML = '';
      if (r.roots) paintRoots(r);
      if (!r.ok) {
        // 打不开也把面包屑画出来，用户能点着往回走，不至于卡死在一个空框上
        paintCrumbs(r);
        upPath = r.parent || '';
        upBtn.disabled = !upPath;
        listBox.appendChild(notice('bad', (r.detail || '打不开这个目录')
          + (r.hint ? '（' + r.hint + '）' : '')));
        return;
      }
      cur = r.path;
      curEl.textContent = cur;
      upPath = r.parent || '';
      upBtn.disabled = !upPath || upPath === cur;
      paintCrumbs(r);
      listBox.appendChild(el('div', { class: 'dirpick-head' },
        el('span', {}, r.detail),
        el('span', { class: 'mono', title: cur }, cur)));
      if (r.isFile) {
        listBox.appendChild(notice('warn', r.detail));
        return;
      }
      const dirs = (r.entries || []).filter((e) => e.dir);
      if (!dirs.length) {
        listBox.appendChild(el('div', { class: 'ft-empty' },
          '（这里没有子目录 —— 要选就直接点「选择此目录」）'));
      } else {
        for (const e of dirs) {
          listBox.appendChild(el('div', {
            class: 'dirpick-item', title: e.path, onclick: () => goDir(e.path),
          },
            el('span', { class: 'dp-ico' }, '📁'),
            el('span', { class: 'dp-name' }, e.name),
            el('span', { class: 'dp-open' }, '进入 ›')));
        }
      }
      if (r.warning) noteBox.appendChild(notice('warn', r.warning));
    }

    const body = el('div', { class: 'dirpick' },
      el('div', { class: 'row' }, el('div', { class: 'grow' }, crumbBox), upBtn, okBtn),
      rootBox,
      listBox,
      noteBox,
      el('div', { class: 'dirpick-foot' },
        el('span', { style: { color: 'var(--dim)', fontSize: '11.5px' } }, '当前目录：'),
        curEl,
        el('button', { class: 'btn sm', onclick: () => finish(null) }, '取消')));

    m = modal({
      title, wide: true, body,
      // 点遮罩 / 点「关闭」也得把 Promise 结掉，否则调用方会一直等
      onClose: () => finish(null),
      foot: el('button', { class: 'btn', onclick: () => finish(null) }, '关闭'),
    });

    // 初始位置：调用方指定 → 上次导出过的目录 → 用户主目录
    (async () => {
      let startAt = start;
      if (!startAt) {
        try {
          const r = await api('/api/sys/config');
          startAt = (r && (r.values || {}).last_out_dir) || '';
        } catch (e) { /* 读不到就走主目录 */ }
      }
      await goDir(startAt);
    })();
  });
}

/* ------------------------------------------------ 本机 APK */

function apkPathBar(host, { value, onGo, actionLabel = '解析', autoOnDrop = false }) {
  const inp = el('input', {
    type: 'text', class: 'mono apk-path',
    placeholder: 'D:\\path\\to\\app.apk   （也可以把 APK 直接拖进页面）',
    value: value || S.openApk,
  });
  const go = () => {
    const v = inp.value.trim();
    if (!v) { toast('请填 APK 路径，或点「浏览…」选 / 把文件拖进来', 'bad'); return; }
    S.openApk = v;
    localStorage.setItem('apk.lastFile', v);
    onGo(v);
  };
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
  const browse = el('button', {
    class: 'btn',
    title: '优先用本机原生对话框（直接拿真实路径，不复制文件）；不可用时自动改用浏览器选择',
    onclick: async () => {
      const r = await pickApk();
      if (!r || !r.path) return;
      toast('已选中 ' + (r.name || '') + ' · ' + fmtBytes(r.bytes || 0), 'ok');
      const src = r.via === 'upload' ? '上传' : (r.via === 'locate' ? '本机定位' : '浏览');
      applyPath(r.path, { source: src });
      go();
    },
  }, '浏览…');
  PATH_BARS.push({ inp, go, auto: !!autoOnDrop });
  // 路径说明容器：拖入/浏览之后在这里解释"这条路径属于哪个文件夹、有没有被重定向"
  const noteHost = el('div', { class: 'path-note' });
  PATH_NOTES.push({ host: noteHost });
  renderPathNote();
  return el('div', {},
    el('div', { class: 'row', style: { marginBottom: '4px' } },
      el('div', { class: 'grow' }, inp),
      browse,
      el('button', { class: 'btn primary', onclick: go }, actionLabel),
      el('span', { class: 'drop-tip', title: '浏览器不给拖入文件的路径，服务端会按文件名+体积在本机常见目录里找' }, '可拖入')),
    noteHost);
}

function sigCard(sig) {
  if (!sig) return null;
  const body = [];
  if (sig.schemes && sig.schemes.length) {
    body.push(el('div', { style: { marginBottom: '8px' } },
      ...sig.schemes.map((s) => tag(s.toUpperCase(), 'brand'))));
  }
  if (sig.error) body.push(notice('warn', sig.error));
  const ident = sig.identity || sig.digestIdentity;   // 后端字段是 identity
  if (ident) {
    body.push(kvBlock([['签名标识', ident],
                       ['是否 debug', sig.isDebug ? '是（不可正式分发）' : '否']], ['签名标识']));
  }
  for (const [i, c] of (sig.certificates || []).entries()) {
    body.push(el('div', { style: { marginTop: '10px' } },
      el('div', { style: { fontWeight: '600', marginBottom: '5px' } },
        '签名者 #' + (i + 1) + (c.schemes && c.schemes.length ? '  [' + c.schemes.join(' · ') + ']' : '')),
      kvBlock([
        ['主体', c.subject], ['颁发者', c.issuer],
        ['SHA-256', c.sha256], ['SHA-1', c.sha1],
        ['有效期', (c.notBefore || '') + ' → ' + (c.notAfter || '')],
        ['公钥', (c.publicKeyAlgorithm || '') + ' ' + (c.publicKeyBits || '') + ' bits'],
        ['签名算法', c.signatureAlgorithm],
      ], ['SHA-256', 'SHA-1'])));
  }
  for (const w of (sig.warnings || [])) body.push(notice('warn', w));
  if (sig.verified) body.push(notice('ok', sig.verified));
  return card('签名', null, ...body);
}

/** 跳到某个合并页面的某一格（带上下文，比如 APK 路径 / 包名）。 */
function goTab(parentId, tabKey, extraQ) {
  go(parentId, Object.assign({}, extraQ || {}, { tab: tabKey }));
}

function apkInfoView(host, d, q) {
  if (d.error && !d.package) {
    host.appendChild(card('解析失败', null, notice('bad', d.error)));
    return;
  }
  const cnt = d.componentCounts || {};
  const head = el('div', { class: 'apk-head' },
    d.icon ? el('img', { class: 'apk-icon', src: d.icon }) : el('div', { class: 'apk-icon' }),
    el('div', { class: 'h-main' },
      el('div', { class: 'apk-pkg' }, d.package || '(无包名)'),
      el('div', { class: 'apk-ver' },
        el('span', { class: 'v' }, 'v' + (d.versionName || '-')),
        tag('build ' + (d.versionCode ?? '-')),
        d.channel ? tag('渠道 ' + d.channel, 'brand') : null,
        d.debuggable ? tag('debuggable', 'warn') : null,
        tag(d.fileName)),
      el('div', { style: { marginTop: '7px', color: 'var(--dim)', fontSize: '12px' } },
        '解析耗时 ' + (d.parseMs ?? '?') + ' ms · ' + fmtBytes(d.fileSize))));

  const acts = [
    el('button', { class: 'btn sm', onclick: () => copyText(d.package || '') }, '复制包名'),
    el('button', { class: 'btn sm', onclick: () => copyText(d.path || '') }, '复制路径'),
    // 「提取图标 / 体积构成」现在是同一页里的格子，不再跳出去 ——
    // 跳出去意味着"重新选一遍文件"，那正是这轮要消掉的别扭
    el('button', {
      class: 'btn sm', onclick: () => goTab('local.parse', 'icon', { path: d.path }),
    }, '提取图标'),
    el('button', {
      class: 'btn sm', onclick: () => goTab('local.parse', 'slim', { path: d.path }),
    }, '体积构成'),
    el('button', {
      class: 'btn sm', onclick: () => goTab('local.parse', 'sign', { path: d.path }),
    }, '看签名'),
    el('button', {
      class: 'btn sm', onclick: () => go('device.install.one', { apk: d.path }),
    }, '安装到设备'),
    el('button', {
      class: 'btn sm', onclick: () => go('device.install.one', { apk: d.path, tab: 'precheck' }),
    }, '安装预检'),
  ];
  if (d.package) {
    acts.push(el('button', {
      class: 'btn sm', onclick: () => go('device.app.detail', { pkg: d.package, tab: 'cmp', apk: d.path }),
    }, '与设备对比'));
  }
  host.appendChild(card('概览', null, head, el('div', { class: 'row', style: { marginTop: '12px' } }, ...acts)));

  host.appendChild(card('基本信息', null, kvBlock([
    ['文件大小', `${fmtBytes(d.fileSize)}（解压后 ${fmtBytes(Object.values(d.sizeBreakdown || {}).reduce((a, b) => a + (b || 0), 0))}）`],
    ['minSdk / targetSdk', `${d.minSdk ?? '-'} / ${d.targetSdk ?? '-'}${d.compileSdk ? '   compileSdk ' + d.compileSdk : ''}`],
    ['ABI', (d.abis || []).join(' / ') || '（无 native 库）'],
    ['Dex', `${(d.dexNames || []).length} 个   ${(d.dexNames || []).join(' ')}`],
    ['主 Activity', d.mainActivity],
    ['Application 类', d.applicationClass],
    ['组件', `Activity ${cnt.activity ?? 0} · Service ${cnt.service ?? 0} · Receiver ${cnt.receiver ?? 0} · Provider ${cnt.provider ?? 0}`],
    ['权限', `${(d.permissions || []).length} 项` + ((d.dangerousPermissions || []).length ? `（危险 ${d.dangerousPermissions.length}）` : '')],
    ['允许备份', d.allowBackup ? '是' : '否'],
    ['明文流量', d.usesCleartext ? '允许' : '禁止'],
    ['Feature', (d.features || []).join('、')],
  ], ['文件大小'])));

  const sig = sigCard(d.signature);
  if (sig) host.appendChild(sig);

  if ((d.dangerousPermissions || []).length) {
    host.appendChild(card('危险权限', d.dangerousPermissions.length + ' 项',
      el('div', { class: 'row' },
        ...d.dangerousPermissions.map((p) => tag(p.split('.').pop(), 'warn')))));
  }

  if ((d.permissions || []).length) {
    host.appendChild(card('全部权限', d.permissions.length + ' 项',
      el('div', { class: 'row' }, ...d.permissions.map((p) => tag(p.split('.').pop())))));
  }

  const comps = d.components || {};
  const compRows = [];
  for (const [t, list] of Object.entries(comps)) {
    for (const c of (list || [])) compRows.push({ type: t, ...c });
  }
  if (compRows.length) {
    host.appendChild(card('组件清单', compRows.length + ' 个',
      dataTable([
        { key: 'type', label: '类型', width: '76px' },
        { key: 'name', label: '组件', mono: true },
        { key: 'exported', label: '导出', width: '60px', render: (r) => r.exported === true ? tag('是', 'warn') : (r.exported === false ? tag('否') : '-') },
        { key: 'launchable', label: '可启动', width: '66px', render: (r) => r.launchable ? tag('✔', 'ok') : '' },
        { key: 'summary', label: '备注' },
        {
          key: '_a', label: '', width: '62px', render: (r) => el('button', {
            class: 'btn sm', onclick: (e) => { e.stopPropagation(); launchNow(compTarget(r, d.package), d.package); },
          }, '启动'),
        },
      ], compRows, { filter: true, filterPlaceholder: '过滤组件…', emptyText: '没有组件' })));
  }

  for (const w of (d.warnings || [])) host.appendChild(notice('warn', w));
}

/* 从 `pkg/.Cls` 里取包名（预览返回的 normalizedTarget 里那段）。 */
function pkgOfTarget(norm) {
  const s = String(norm || '');
  const i = s.indexOf('/');
  return i > 0 ? s.slice(0, i) : (s || '（未知）');
}

/* 组件的 `am` 组件名。优先用后端给的 `target`；没有就现场拼（兜底）。
   name 是全限定类名，直接丢给 `am -n` 是定不出组件的：
     - 类名以包名开头 → `pkg/.tail`
     - 否则 → `pkg/全限定类名`（**合法**写法；com.jxw.aizx.teacher 的类
       全在 com.jxw.teacher.* 下，只能这么拼） */
function compTarget(c, pkg) {
  if (c && c.target) return c.target;
  const name = (c && c.name) || '';
  if (!pkg || !name) return name;
  if (name.startsWith(pkg + '.')) return pkg + '/.' + name.slice(pkg.length + 1);
  return pkg + '/' + name;
}

async function launchNow(component, pkg) {
  try {
    const r = await api('/api/launch/start', {
      method: 'POST',
      // pkg 一定要带上：组件清单里的 name 是**全限定类名**（com.x.y.ui.MainActivity），
      // 光看字符串分不出包名和类名的边界。带上包名后端才能拼成 pkg/.Class，
      // 否则会得到一条空命令 `adb shell am start`。
      // 注意：代码包名和安装包名不同源时（com.jxw.aizx.teacher 的类在
      // com.jxw.teacher.* 下），拼出来的是 `pkg/全限定类名`，那同样是合法写法。
      body: { target: component, pkg: pkg || '', kind: 'activity', device: devParam() },
    });
    if (r.ok) toast('已启动 ' + (r.component || component), 'ok');
    else toast(r.detail || '启动失败', 'bad');
  } catch (e) {
    // 解析失败时后端会附带"设备上相关的包名"。toast 塞不下多行错误，
    // 这里至少把第一条候选带出来，用户知道该填什么。
    const cands = (e.payload && e.payload.candidates) || [];
    toast('启动失败：' + e.message + (cands.length ? '　试试包名 ' + cands[0] : ''), 'bad');
  }
}

/* 合并页「解析 APK」的**信息**那一格 —— 这就是原来那个单页实现本身。

 签名 / 图标 / 体积 三格各自是独立视图（`VIEWS['local.sign']` 等），
 由 `mergedPage` 按 tab 切换。它们**共用 q.path**，所以选定文件一次、
 四格之间随便翻，不用每格重新选一遍文件。 */
const parseOwnTab = async function (host, cap, q) {
  const bar = apkPathBar(host, {
    value: q.path,
    autoOnDrop: true,
    // 注意：doParse 只接受一个参数（路径）。早先这里写成了 doParse(host, p)，
    // 结果 p 收不到、doParse 拿到的是 DOM 元素 → JSON 里变成 {} →
    // 后端报「文件不存在：{}」。别把渲染用的 host 混进参数里。
    onGo: async (p) => { await doParse(p); },
  });
  host.appendChild(card(null, null, bar));

  // 结果区：全页**唯一**一块。每次解析都是「清掉旧的 → 填新的」，不在后面追加。
  // 追加的后果是连点几次之后堆出一摞过期结果，看图还得先猜哪张才是这一次的。
  const out = el('div', { class: 'dyn' });
  host.appendChild(out);

  let seq = 0;                 // 只认最后一次请求（慢响应回来时别覆盖新结果）
  if (q.path) await doParse(q.path);
  else showEmpty();

  function showEmpty() {
    out.innerHTML = '';
    out.appendChild(emptyState('📦', '选一个 APK / 把 APK 拖进页面 / 或直接把路径粘进上面的输入框'));
  }

  async function doParse(p) {
    const mine = ++seq;
    out.innerHTML = '';
    out.appendChild(el('div', { class: 'card' },
      el('div', { class: 'card-body' }, spinner('解析中…（含签名，首次稍慢）'))));
    try {
      const d = await api('/api/apk/parse', {
        method: 'POST',
        body: { path: p, signature: true, icon: true, components: true },
      });
      if (mine !== seq) return;   // 已经有更新的一次解析了，这份结果直接丢掉
      out.innerHTML = '';
      apkInfoView(out, d, q);
    } catch (e) {
      if (mine !== seq) return;
      out.innerHTML = '';
      out.appendChild(notice('bad', '解析失败：' + e.message));
    }
  }
};
VIEWS['local.parse'] = mergedPage('local.parse', parseOwnTab);

/* `local.popup`（信息卡片）不再单独注册视图：它是「信息」那一格的**别名**，
   注册表里 `alias=True`，`render()` 会把它重定向到 `local.parse?tab=info`。
   以前这里是 `VIEWS['local.popup'] = VIEWS['local.parse']` —— 现在那个位置上
   放的是合并页调度器，再指过去就会无限套自己。 */

/**
 * 合并页里某一格顶部的"当前文件"条。
 *
 * 有了它，格子就不用再摆一个"请选择 APK"的输入框 —— 那个框在 tab 语境下
 * 纯粹是噪音（文件早就选过了）。真没带 path（老书签直接进这一格）时，
 * 调用方仍旧退回到路径输入框。
 */
function curFileNote(q, backTab = 'info') {
  if (!q || !q.path) return null;
  return el('div', { class: 'cur-file' },
    el('span', { class: 'cf-label' }, '当前文件'),
    el('span', { class: 'cf-path mono', title: q.path }, q.path),
    el('button', {
      class: 'btn sm', title: '回到「信息」那一格换一个文件',
      onclick: () => goTab('local.parse', backTab, { path: q.path }),
    }, '换文件'));
}

VIEWS['local.icon'] = async function (host, cap, q) {
  const box = el('div');
  const run = async (p) => {
    box.innerHTML = '';
    box.appendChild(spinner('提取图标…'));
    try {
      const r = await api('/api/apk/icon', { method: 'POST', body: { path: p } });
      box.innerHTML = '';
      box.appendChild(card('图标已导出', r.source,
        el('div', { style: { textAlign: 'center' } },
          el('img', { src: r.url, style: { maxWidth: '200px', border: '1px solid var(--line)', borderRadius: '12px' } })),
        kvBlock([['文件', r.path], ['类型', r.mime], ['大小', fmtBytes(r.bytes)]], ['文件']),
        el('div', { class: 'row', style: { marginTop: '10px' } },
          el('a', { class: 'btn', href: r.url, download: '' }, '下载'),
          el('button', { class: 'btn', onclick: () => copyText(r.path) }, '复制路径'))));
    } catch (e) { box.innerHTML = ''; box.appendChild(notice('bad', e.message)); }
  };
  const note = curFileNote(q, 'icon');
  if (note) {
    host.appendChild(note);
    host.appendChild(box);
    await run(q.path);
  } else {
    host.appendChild(card(null, null, apkPathBar(host, {
      value: '', actionLabel: '提取图标', onGo: run,
    })));
    host.appendChild(box);
  }
};

VIEWS['local.sign'] = async function (host, cap, q) {
  const box = el('div');
  const run = async (p) => {
    box.innerHTML = '';
    box.appendChild(spinner('解析签名…'));
    try {
      const d = await api('/api/apk/signature', { params: { path: p } });
      box.innerHTML = '';
      box.appendChild(sigCard(d) || notice('warn', '没有解析到签名'));
    } catch (e) { box.innerHTML = ''; box.appendChild(notice('bad', e.message)); }
  };
  const note = curFileNote(q, 'sign');
  if (note) {
    host.appendChild(note);
    host.appendChild(box);
    await run(q.path);
  } else {
    host.appendChild(card(null, null, apkPathBar(host, {
      value: '', actionLabel: '看签名', onGo: run,
    })));
    host.appendChild(box);
  }
};

VIEWS['local.slim'] = async function (host, cap, q) {
  const box = el('div');
  const run = async (p) => {
    box.innerHTML = '';
    box.appendChild(spinner());
    try {
      const r = await api('/api/apk/analyze', { params: { path: p } });
      box.innerHTML = '';
      box.appendChild(card('体积构成 · ' + r.fileName, r.package + ' · ' + fmtBytes(
        (r.items || []).reduce((a, i) => a + (i.bytes || 0), 0)),
        el('div', { class: 'tbl-wrap' }, el('table', { class: 'tbl' },
          el('thead', {}, el('tr', {}, el('th', {}, '条目'), el('th', { class: 'num' }, '占比'), el('th', { class: 'num' }, '大小'))),
          el('tbody', {}, (r.items || []).map((i) => el('tr', {},
            el('td', {}, i.name),
            el('td', { class: 'num' }, i.percent),
            el('td', { class: 'num' }, fmtBytes(i.bytes))))))),
        (r.hints || []).length
          ? el('div', { style: { marginTop: '12px' } }, ...r.hints.map((h) => notice('warn', h)))
          : null));
    } catch (e) { box.innerHTML = ''; box.appendChild(notice('bad', e.message)); }
  };
  const note = curFileNote(q, 'slim');
  if (note) {
    host.appendChild(note);
    host.appendChild(box);
    await run(q.path);
  } else {
    host.appendChild(card(null, null, apkPathBar(host, {
      value: '', actionLabel: '看体积', onGo: run,
    })));
    host.appendChild(box);
  }
};

/* ------------------------------------------------ 双包对比

 * 两侧用的是**同一套**来源控件：本机 APK（浏览… 选真实文件）或设备上的已装应用。
 * 「设备应用」那一侧包名留空 = 用另一侧的包名，也就是**跟它的同名应用比** ——
 * 本地刚打了个包、想知道跟设备上装的那份差在哪，这是最常做的那一次，
 * 不该逼用户再去翻一遍包名。
 */
const DIFF_SRC_CN = {
  file: '本机文件',
  'device-remote': '设备·按需读取',
  'device-pulled': '设备·拉回本地',
};

VIEWS['local.diff'] = async function (host, cap, q) {
  const st = {
    a: { kind: q.aPkg != null ? 'device' : 'file', file: q.a || S.openApk || '', pkg: q.aPkg || '' },
    b: { kind: q.bPkg != null ? 'device' : 'file', file: q.b || '', pkg: q.bPkg || '' },
  };
  const out = el('div');

  /* 一侧的来源编辑器（本机文件 / 设备应用 二选一） */
  function sideBox(label, s) {
    const fileInp = el('input', {
      type: 'text', class: 'mono apk-path', value: s.file,
      placeholder: 'D:\\path\\to\\app.apk   （也可以点「浏览…」选）',
      oninput: () => { s.file = fileInp.value; },
    });
    const pkgInp = pkgCombo('留空 = 用另一侧的包名（跟它的同名应用比）', s.pkg);
    pkgInp.addEventListener('input', () => { s.pkg = pkgInp.value; });

    const fileRow = el('div', { class: 'row' },
      el('div', { class: 'grow' }, fileInp),
      el('button', {
        class: 'btn',
        title: '优先用本机原生对话框（直接拿真实路径，不复制文件）；不可用时自动改用浏览器选择',
        onclick: async () => {
          const r = await pickApk();
          if (!r || !r.path) return;
          s.file = r.path;
          fileInp.value = r.path;
          toast('已选中 ' + (r.name || '') + ' · ' + fmtBytes(r.bytes || 0), 'ok');
        },
      }, '浏览…'));
    const devRow = el('div', { class: 'row' }, el('div', { class: 'grow' }, pkgInp));

    const paint = () => {
      fileRow.style.display = s.kind === 'file' ? '' : 'none';
      devRow.style.display = s.kind === 'device' ? '' : 'none';
    };
    const seg = el('div', { class: 'seg' },
      [['file', '本机 APK'], ['device', '设备应用']].map(([k, text]) => el('button', {
        class: k === s.kind ? 'on' : '',
        onclick: () => {
          s.kind = k;
          $$('button', seg).forEach((b) => b.classList.toggle('on', b.textContent === text));
          paint();
        },
      }, text)));
    paint();

    return el('div', { class: 'field' },
      el('label', {}, label + ' 侧'),
      el('div', { class: 'row' }, seg),
      fileRow, devRow);
  }

  host.appendChild(card('对比哪两个包', '每一侧都可以是「本机 APK」或「设备上已装的应用」',
    sideBox('A', st.a),
    sideBox('B', st.b),
    el('div', { class: 'row' },
      el('button', {
        class: 'btn primary',
        onclick: async () => {
          const body = { device: devParam() };
          for (const k of ['a', 'b']) {
            const s = st[k];
            if (s.kind === 'file') {
              if (!s.file.trim()) { toast(k.toUpperCase() + ' 侧还没选 APK', 'bad'); return; }
              body[k] = s.file.trim();
            } else {
              // 空串是**有意义**的：告诉服务端"这一侧是设备，包名请用另一侧推出来"
              body[k + 'Package'] = s.pkg.trim();
            }
          }
          out.innerHTML = '';
          out.appendChild(spinner('对比中…（设备侧要现读，约 1~2 秒）'));
          try {
            const d = await api('/api/apk/diff', { method: 'POST', body });
            out.innerHTML = '';
            renderDiff(out, d);
          } catch (e) {
            out.innerHTML = '';
            out.appendChild(notice('bad', e.message));
          }
        },
      }, '开始对比'),
      el('span', { style: { color: 'var(--dim)', fontSize: '11.5px' } },
        '设备侧走「按需读取」，不把整个 APK 拉下来'))));
  host.appendChild(out);
};

/** 渲染对比结果（两种来源模式共用）。 */
function renderDiff(box, d) {
  const src = d.sources || {};

  // ---- 两侧是谁、数据从哪来 ----
  const rows = [];
  for (const [k, w] of [['a', 'A'], ['b', 'B']]) {
    const s = src[k] || {};
    const side = (k === 'a' ? d.a : d.b) || {};
    rows.push(el('div', { class: 'row' },
      el('b', { style: { width: '16px' } }, w),
      el('span', { class: 'mono', style: { fontSize: '11.5px', flex: '1' }, title: side.path || '' },
        side.path || ''),
      s.kind === 'device' && d.device
        ? el('span', { class: 'mono', style: { fontSize: '11px', color: 'var(--dim)' } }, d.device) : null,
      tag(DIFF_SRC_CN[s.from] || s.from || '', s.from === 'file' ? '' : 'brand')));
  }
  const rs = d.remoteStats || {};
  if (rs.adbCalls) {
    rows.push(el('div', { style: { color: 'var(--dim)', fontSize: '11.5px', marginTop: '6px' } },
      '设备侧读取代价：' + rs.adbCalls + ' 次 adb 调用、' + fmtBytes(rs.fetched)
      + '（原包 ' + fmtBytes(rs.size) + '）—— 没有整包 pull'));
  }
  for (const n of (d.notes || [])) rows.push(notice('warn', n));
  box.appendChild(card('对比的两侧', null, ...rows));

  // ---- 字段差异 ----
  const fields = d.fields || [];
  const ndiff = fields.filter((r) => !r.same).length;
  box.appendChild(card('字段差异' + (ndiff ? '（' + ndiff + ' 处不同）' : ''), null,
    ndiff === 0 ? notice('ok', '逐字段一致 —— 两边是同一个版本') : null,
    dataTable([
      { key: 'label', label: '字段', width: '150px' },
      { key: 'a', label: 'A', mono: true },
      { key: 'b', label: 'B', mono: true },
      { key: 'same', label: '', width: '40px', render: (r) => (r.same ? '' : tag('≠', 'warn')) },
    ], fields, { emptyText: '无字段' })));

  const delta = (d.sizeDelta || {}).text;
  if (delta) {
    box.appendChild(el('div', { style: { margin: '8px 2px 0', fontSize: '12px', color: 'var(--dim)' } },
      '体积变化（A→B）：' + delta));
  }

  // ---- 签名 ----
  if (d.signature) {
    box.appendChild(card('签名', null,
      d.signature.verdict === 'same'
        ? notice('ok', '签名一致 —— ' + d.signature.reason)
        : notice('warn', d.signature.verdict + ' —— ' + d.signature.reason)));
  }

  // ---- 权限 ----
  const perms = d.permissions || {};
  if ((perms.onlyA || []).length || (perms.onlyB || []).length) {
    box.appendChild(card('权限差异', null,
      (perms.onlyA || []).length ? el('div', { style: { marginBottom: '7px' } },
        el('b', {}, '仅 A：'), ...perms.onlyA.map((p) => tag(p.split('.').pop(), 'warn'))) : null,
      (perms.onlyB || []).length ? el('div', {},
        el('b', {}, '仅 B：'), ...perms.onlyB.map((p) => tag(p.split('.').pop(), 'brand'))) : null));
  }

  // ---- 组件 ----
  for (const [t, cd] of Object.entries(d.components || {})) {
    if (!(cd.onlyA || []).length && !(cd.onlyB || []).length) continue;
    box.appendChild(card(t + ' 差异', null,
      (cd.onlyA || []).length ? el('div', { style: { marginBottom: '6px' } },
        el('b', {}, '仅 A：'),
        el('div', { class: 'mono', style: { fontSize: '11.5px' } }, (cd.onlyA || []).join('\n'))) : null,
      (cd.onlyB || []).length ? el('div', {},
        el('b', {}, '仅 B：'),
        el('div', { class: 'mono', style: { fontSize: '11.5px' } }, (cd.onlyB || []).join('\n'))) : null));
  }

  // ---- 设备侧的安装信息（只有设备模式才有）----
  const f = d.deviceFacts || {};
  const frows = [];
  const push = (label, v) => { if (v !== undefined && v !== null && v !== '') frows.push({ k: label, v: String(v) }); };
  push('包名', f.package);
  push('版本', f.versionName ? (f.versionName + '（build ' + f.versionCode + '）') : '');
  push('安装器', f.installer ? (f.installer + (f.installer === 'null' ? '（adb 装的）' : '')) : '');
  push('是否系统应用', f.system === undefined ? '' : (f.system ? '是' : '否'));
  push('首次安装', f.firstInstall);
  push('最后更新', f.lastUpdate);
  push('设备上的路径', f.apkPath);
  if (frows.length) {
    box.appendChild(card('设备侧安装信息', d.device || '', dataTable([
      { key: 'k', label: '项', width: '130px' },
      { key: 'v', label: '', mono: true },
    ], frows)));
  }
}


VIEWS['local.scan'] = async function (host, cap, q) {
  const dir = el('input', { type: 'text', class: 'mono', placeholder: 'D:\\apks', value: q.dir || '' });
  const out = el('div');
  const scan = async () => {
    const d = dir.value.trim();
    if (!d) { toast('请先选一个目录', 'bad'); return; }
    out.innerHTML = '';
    out.appendChild(spinner('批量解析中…'));
    try {
      const r = await api('/api/apk/scan', { method: 'POST', body: { dir: d, limit: 300 } });
      out.innerHTML = '';
      out.appendChild(card(`共 ${r.count} / ${r.total} 个`, null, dataTable([
        { key: 'appLabel', label: '应用名' },
        { key: 'package', label: '包名', mono: true },
        { key: 'versionName', label: '版本', width: '90px' },
        { key: 'bytes', label: '大小', num: true, width: '90px', render: (r2) => fmtBytes(r2.bytes) },
        { key: 'path', label: '路径', mono: true },
      ], r.items || [], {
        filter: true, filterPlaceholder: '按包名/应用名过滤…',
        onRow: (row) => go('local.parse', { path: row.path, tab: 'info' }),
      })));
    } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
  };
  host.appendChild(card('扫目录', '递归找 *.apk 并批量解析（不含签名，快）',
    el('div', { class: 'row' },
      el('div', { class: 'grow' }, dir),
      // 手打目录路径同样不该被要求 —— 点一下选
      el('button', {
        class: 'btn', onclick: async () => {
          const d = await pickLocalDir({ title: '要扫描哪个目录', start: dir.value.trim() });
          if (d) { dir.value = d; scan(); }
        },
      }, '浏览目录…'),
      el('button', { class: 'btn primary', onclick: scan }, '开始扫描'))));
  host.appendChild(out);
};

/* ------------------------------------------------ 设备 */

VIEWS['device.devices'] = async function (host) {
  const out = el('div');
  host.appendChild(card('设备操作', null, el('div', { class: 'row' },
    el('button', { class: 'btn', onclick: async () => { await loadDevices(); render(); } }, '重新枚举'),
    el('button', {
      class: 'btn', onclick: async () => {
        try { await api('/api/devices/restart-adb', { method: 'POST', body: {} }); toast('已重启 adb server', 'ok'); await loadDevices(); render(); }
        catch (e) { toast(e.message, 'bad'); }
      },
    }, '重启 adb'),
    el('button', {
      class: 'btn', onclick: async () => {
        const t = await ask({ title: '无线调试连接', message: '填 ip:port（先在同一 WiFi 下用 adb tcpip 5555 打开）', placeholder: '192.168.1.10:5555', action: '连接' });
        if (!t) return;
        try { await api('/api/devices/restart-adb', { method: 'POST', body: {} }); } catch (_) { }
        toast('请用命令行执行：adb connect ' + t, '');
      },
    }, '无线连接'))));
  host.appendChild(out);
  const list = el('div');
  host.appendChild(list);
  list.appendChild(spinner());
  try {
    const r = await api('/api/devices');
    list.innerHTML = '';
    list.appendChild(card(`设备 ${(r.devices || []).length} 台`, 'adb: ' + (r.adbPath || '未找到'),
      dataTable([
        { key: 'serial', label: '序列号', mono: true },
        { key: 'state', label: '状态', width: '100px', render: (d) => d.state === 'device' ? tag('在线', 'ok') : tag(d.state, 'warn') },
        { key: 'model', label: '型号' },
        { key: 'androidVersion', label: 'Android', width: '80px' },
        { key: 'resolution', label: '分辨率', width: '110px' },
        { key: 'abi', label: 'ABI' },
      ], r.devices || [], {
        emptyText: '没有检测到设备。检查 USB 调试是否打开。',
        onRow: (d) => showDeviceDetail(d.serial),
      })));
  } catch (e) {
    list.innerHTML = '';
    list.appendChild(notice('bad', e.message));
  }
};

async function showDeviceDetail(serial) {
  const body = el('div', {}, spinner());
  modal({ title: '设备 ' + serial, body, wide: true });
  try {
    const d = await api(`/api/devices/${encodeURIComponent(serial)}/info`);
    body.innerHTML = '';
    const rows = Object.entries(d).filter(([k]) => !['error'].includes(k));
    body.appendChild(kvBlock(rows));
  } catch (e) { body.innerHTML = ''; body.appendChild(notice('bad', e.message)); }
}

/* ------------------------------------------------ 字段标签（后端给中文名）

 后端 `stack.FIELDS` 里存着每个字段的**中文标签和说明**，响应里一起带过来。
 前端不再自己翻译一遍 —— 加字段只改后端一处，界面跟着变。
 */

function fLabel(fields, key) {
  const f = (fields || {})[key];
  return (f && f.label) || key;
}

function fHint(fields, key) {
  const f = (fields || {})[key];
  return (f && f.hint) || '';
}

/** 一格「中文标签 + 值」。标签带 title —— 悬停就看这字段到底是啥。 */
function fCell(fields, key, value, mono) {
  if (value === '' || value == null || value === false) return null;
  return el('div', { class: 'stack-field' },
    el('div', { class: 'stack-fk', title: fHint(fields, key) }, fLabel(fields, key)),
    el('div', { class: 'stack-fv' + (mono ? ' mono' : '') }, String(value)));
}

function yn(b) { return b ? '是' : '否'; }

function stateTag(a) {
  const s = (a.state || '').toUpperCase();
  const kind = s === 'RESUMED' ? 'ok' : (s === 'PAUSED' ? 'warn' : 'plain');
  return tag(a.stateText || a.state || '-', kind);
}

VIEWS['device.stack'] = async function (host, cap, q) {
  const kw = el('input', { type: 'search', placeholder: '按包名 / 类名过滤…', value: q.filter || '' });
  const visOnly = el('input', { type: 'checkbox' });
  const out = el('div');
  let data = null;

  host.appendChild(card('当前设备堆栈',
    '把 dumpsys activity activities 拆成「屏幕 → 任务 → Activity」三层；每个字段的中文名悬停可看说明',
    el('div', { class: 'row' },
      el('div', { class: 'grow' }, kw),
      el('label', { class: 'check' }, visOnly, '只看可见任务'),
      el('button', { class: 'btn primary', onclick: () => load(false) }, '刷新'),
      el('button', { class: 'btn', onclick: () => load(true) }, '强制重读'))));
  host.appendChild(out);

  kw.addEventListener('input', () => { if (data) paint(); });
  visOnly.addEventListener('change', () => { if (data) paint(); });

  async function load(force) {
    out.innerHTML = '';
    out.appendChild(spinner('读取设备堆栈…'));
    try {
      data = await api('/api/device/stack', {
        params: { device: devParam(), refresh: force ? '1' : '' },
      });
      paint();
    } catch (e) {
      data = null;
      out.innerHTML = '';
      out.appendChild(notice('bad', e.message));
    }
  }

  function hits(text) {
    const k = kw.value.trim().toLowerCase();
    if (!k) return true;
    return String(text || '').toLowerCase().indexOf(k) >= 0;
  }

  function paint() {
    const r = data;
    out.innerHTML = '';
    if (!r || !r.ok) {
      out.appendChild(notice('bad', (r && r.error) || '读取设备堆栈失败'));
      return;
    }
    const F = r.fields || {};
    const top = r.top || {};
    const cts = r.counts || {};

    // ---- 当前界面 ----
    const acts = el('div', { class: 'row' });
    if (top.component) {
      acts.appendChild(el('button', { class: 'btn', onclick: () => copyText(top.component) }, '复制组件'));
    }
    if (top.package) {
      acts.appendChild(el('button', {
        class: 'btn',
        onclick: () => go('device.app.detail', { pkg: top.package }),
      }, '应用详情'));
      acts.appendChild(el('button', {
        class: 'btn',
        onclick: () => go('device.launch.start', { pkg: top.package, component: top.component }),
      }, '用启动器打开'));
    }
    out.appendChild(card('当前界面',
      `任务 ${cts.tasks || 0} 个（可见 ${cts.visibleTasks || 0}）/ `
      + `Activity ${cts.activities || 0} 个 / 涉及 ${cts.packages || 0} 个包`,
      el('div', { class: 'stack-fields' },
        fCell(F, 'package', top.package, true),
        fCell(F, 'className', top.className, true),
        fCell(F, 'fullClass', top.fullClass, true),
        fCell(F, 'component', top.component, true),
        fCell(F, 'taskId', top.taskId, false)),
      acts));

    // ---- 任务 / Activity ----
    let shown = 0;
    for (const d of (r.displays || [])) {
      for (const t of (d.tasks || [])) {
        if (visOnly.checked && !t.visible) continue;
        const all = t.activities || [];
        // 任务名命中 → 把它下面的 Activity 全列出来；否则只列命中的那些
        const byTask = hits(t.affinityPackage) && kw.value.trim() !== '';
        const list = kw.value.trim() === '' ? all : (byTask ? all : all.filter(
          (a) => hits(a.component) || hits(a.package) || hits(a.className) || hits(a.fullClass)));
        if (kw.value.trim() !== '' && list.length === 0) continue;
        shown += 1;
        out.appendChild(taskCard(F, t, list, d.id));
      }
    }
    if (shown === 0) {
      out.appendChild(notice('warn', '没有匹配的任务。换个关键词，或取消「只看可见任务」。'));
    }

    out.appendChild(card('这条命令',
      `原始输出 ${r.rawLines || 0} 行 / ${r.rawBytes || 0} 字节 —— 界面里只列结构化结果，命令原样留在这儿可复制`,
      codeBlock(r.command || '')));
  }

  function taskCard(F, t, list, displayId) {
    const head = el('div', { class: 'stack-task-hd' },
      el('span', { class: 'stack-id mono' }, '#' + t.id),
      el('span', { class: 'stack-aff mono' }, t.affinityPackage || '(无归属)'),
      t.visible ? tag('可见', 'ok') : tag('不可见', 'plain'),
      tag(t.modeText || t.mode || '-', 'plain'));
    if (t.type) head.appendChild(tag(t.type, 'plain'));

    const body = el('div', { class: 'stack-fields' },
      fCell(F, 'taskId', t.id),
      fCell(F, 'affinity', t.affinityPackage, true),
      fCell(F, 'mode', t.modeText || t.mode, false),
      fCell(F, 'visible', yn(t.visible)),
      fCell(F, 'size', t.size != null ? t.size : ''),
      fCell(F, 'bounds', t.bounds, true));

    for (const a of list) {
      body.appendChild(actBlock(F, a, displayId));
    }
    return card(head, null, body);
  }

  function actBlock(F, a, displayId) {
    const hd = el('div', { class: 'stack-act-hd' },
      el('span', { class: 'stack-idx mono' }, '#' + a.index),
      el('span', { class: 'stack-comp mono' }, a.component || '-'),
      stateTag(a));
    if (a.isFocused) hd.appendChild(tag('当前焦点', 'brand'));
    if (a.isTop) hd.appendChild(tag('栈顶', 'plain'));
    if (a.appLabel) hd.appendChild(el('span', { class: 'stack-dim' }, a.appLabel));
    hd.appendChild(el('span', { class: 'stack-sp' }));
    if (a.component) {
      hd.appendChild(el('button', { class: 'btn', onclick: () => copyText(a.component) }, '复制'));
    }
    if (a.package) {
      hd.appendChild(el('button', {
        class: 'btn',
        onclick: () => go('device.app.detail', { pkg: a.package }),
      }, '应用详情'));
      hd.appendChild(el('button', {
        class: 'btn',
        onclick: () => go('device.launch.start', { pkg: a.package, component: a.component }),
      }, '启动'));
    }

    return el('div', { class: 'stack-act' + (a.isResumed ? ' on' : '') }, hd,
      el('div', { class: 'stack-fields' },
        fCell(F, 'package', a.package, true),
        fCell(F, 'appLabel', a.appLabel),
        fCell(F, 'className', a.className, true),
        fCell(F, 'fullClass', a.fullClass, true),
        fCell(F, 'component', a.component, true),
        fCell(F, 'state', a.stateText || a.state),
        fCell(F, 'visible', a.visible != null ? yn(a.visible) : ''),
        fCell(F, 'processName', a.processName, true),
        fCell(F, 'pid', a.pid),
        fCell(F, 'userId', a.userId),
        fCell(F, 'taskId', a.taskId),
        fCell(F, 'displayId', a.displayId || displayId),
        fCell(F, 'launchedFromPackage', a.launchedFromPackage, true),
        fCell(F, 'launchCount', a.launchCount),
        fCell(F, 'lastLaunchTime', a.lastLaunchTime, true),
        fCell(F, 'intent', a.intent, true)));
  }

  load(false);
};

VIEWS['device.apps'] = async function (host, cap, q) {
  const kw = el('input', { type: 'search', placeholder: '按应用名 / 包名过滤（空格分词）', value: q.filter || '' });
  const sys = el('input', { type: 'checkbox' });
  const out = el('div');
  let timer = null;

  host.appendChild(card('应用列表', '应用名是解析 APK 拿到的（服务端只读两个条目，不复制整包）',
    el('div', { class: 'row' },
      el('div', { class: 'grow' }, kw),
      el('label', { class: 'check' }, sys, '包含系统应用'),
      el('button', { class: 'btn primary', onclick: () => load(true) }, '查询'),
      el('button', { class: 'btn', onclick: () => load(false, true) }, '强制刷新'))));
  host.appendChild(out);

  kw.addEventListener('keydown', (e) => { if (e.key === 'Enter') load(false); });

  async function load(refreshOnly, force) {
    out.innerHTML = '';
    out.appendChild(spinner('读取应用列表…'));
    try {
      const r = await api('/api/apps', {
        params: {
          filter: kw.value.trim(), device: devParam(),
          system: sys.checked ? '1' : '', refresh: force ? '1' : '',
        },
      });
      out.innerHTML = '';
      if (!r.ok) { out.appendChild(notice('bad', r.error || '读取失败')); return; }
      paint(r);
      // 进度可能已经在跑（比如从别的页面起的），接上
      const st = await api('/api/apps/labels/state').catch(() => null);
      if (st && st.running) startPolling();
    } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
  }

  function paint(r) {
    const apps = r.apps || [];
    const named = apps.filter((a) => a.label).length;
    const missing = apps.length - named;

    const bar = el('div', { class: 'lbl-progress', style: { display: 'none' } });
    const body = [];

    if (missing > 0) {
      body.push(el('div', { class: 'row', style: { marginBottom: '10px', alignItems: 'center' } },
        el('button', {
          class: 'btn', onclick: () => startSync(apps),
        }, '补齐应用名（' + missing + ' 个）'),
        el('span', { style: { fontSize: '11.5px', color: 'var(--dim)' } },
          '从设备上的 APK 里读应用名：只取 Manifest + 资源表两个条目，约 1 秒/个，不复制整包；结果会缓存'),
        bar));
    }

    body.push(dataTable([
      {
        key: 'label', label: '应用名',
        render: (a) => a.label
          ? el('span', { style: { fontWeight: '600' } }, a.label)
          : el('span', { style: { color: 'var(--dim)' } }, '（未知）'),
      },
      { key: 'package', label: '包名', mono: true },
      {
        key: 'versionName', label: '版本', width: '104px',
        render: (a) => a.versionName || (a.versionCode ? 'build ' + a.versionCode : ''),
      },
      {
        key: 'system', label: '类型', width: '70px',
        render: (a) => a.system ? tag('系统') : tag('三方', 'brand'),
      },
    ], apps, {
      filter: true, filterPlaceholder: '本页再过滤…',
      emptyText: '没有匹配的应用',
      onRow: (a) => appDetailModal(a.package),
    }));

    out.appendChild(card('共 ' + apps.length + ' 个',
      (r.cached ? '来自缓存' : '刚刚读取') + ' · 应用名 ' + named + '/' + apps.length,
      ...body));

    function startSync(list) {
      const pkgs = list.map((a) => ({
        package: a.package, versionCode: a.versionCode || 0, apkPath: a.apkPath || '',
      }));
      api('/api/apps/labels/sync', {
        method: 'POST', body: { device: devParam(), packages: pkgs },
      }).then(() => startPolling()).catch((e) => toast(e.message, 'bad'));
    }

    function startPolling() {
      if (timer) return;
      bar.style.display = '';
      const tick = async () => {
        let st = null;
        try { st = await api('/api/apps/labels/state'); } catch (_) { return; }
        if (!st) return;
        const pct = st.total ? Math.round((st.done / st.total) * 100) : 0;
        bar.innerHTML = '';
        bar.appendChild(el('div', { class: 'lb-line' },
          el('div', { class: 'lb-bar' }, el('div', { class: 'lb-fill', style: { width: pct + '%' } })),
          el('span', { class: 'lb-txt' },
            st.done + '/' + st.total + ' · 成功 ' + (st.succeeded || 0) + ' · 失败 ' + (st.failed || 0)
            + (st.running && st.current ? ' · ' + st.current : ''))));
        if (st.running) {
          bar.appendChild(el('button', {
            class: 'btn sm', onclick: () => api('/api/apps/labels/stop', { method: 'POST' }).catch(() => {}),
          }, '停止'));
        } else {
          clearInterval(timer); timer = null;
          toast('应用名补齐结束：成功 ' + (st.succeeded || 0) + ' / 失败 ' + (st.failed || 0),
            st.failed ? 'warn' : 'ok');
          load(false);
        }
      };
      timer = setInterval(tick, 800);
      tick();
    }
  }

  load();
};

function appDetailModal(pkg) {
  const body = el('div', {}, spinner());
  const foot = el('div', { style: { display: 'flex', gap: '7px', flexWrap: 'wrap' } },
    el('button', { class: 'btn sm', onclick: () => { go('device.app.detail', { pkg }); m.close(); } }, '打开详情页'),
    el('button', { class: 'btn sm', onclick: () => { go('device.app.sign', { pkg }); m.close(); } }, '看签名'),
    el('button', { class: 'btn sm', onclick: () => { go('device.launch.start', { pkg }); m.close(); } }, '启动组件'),
    el('button', { class: 'btn sm', onclick: () => { go('device.install.one', { pkg }); m.close(); } }, '安装'),
    el('button', { class: 'btn sm', onclick: async () => {
      const p = await ask({ title: '导出到本机', message: '留空则存到 output/ 目录', placeholder: 'D:\\out.apk' });
      if (p === null) return;
      try {
        const r = await api(`/api/apps/${encodeURIComponent(pkg)}/export`, { method: 'POST', body: { out: p, device: devParam() } });
        toast(r.ok ? ('已导出 → ' + r.path) : (r.detail || '失败'), r.ok ? 'ok' : 'bad');
      } catch (e) { toast(e.message, 'bad'); }
    } }, '导出 APK'),
  );
  const m = modal({ title: pkg, body, foot, wide: true });
  api(`/api/apps/${encodeURIComponent(pkg)}`, { params: { device: devParam() } }).then((d) => {
    body.innerHTML = '';
    if (d.error) { body.appendChild(notice('bad', d.error)); return; }
    body.appendChild(kvBlock([
      ['应用名', d.label], ['包名', d.package], ['版本', `v${d.versionName} (${d.versionCode})`],
      ['UID', d.uid], ['minSdk / targetSdk', `${d.minSdk ?? '-'} / ${d.targetSdk ?? '-'}`],
      ['首次安装', d.firstInstallTime], ['最后更新', d.lastUpdateTime],
      ['APK 路径', d.apkPath], ['数据目录', d.dataDir], ['安装来源', d.installer],
      ['系统应用', d.isSystem ? '是' : '否'], ['ABI', d.abi],
    ], ['包名', 'APK 路径', '数据目录']));
  }).catch((e) => { body.innerHTML = ''; body.appendChild(notice('bad', e.message)); });
}

/**
 * 合并页里某一格顶部的"当前应用"条。
 *
 * 和 `curFileNote` 同一个道理：包名已经定了，再摆一个可编辑的输入框
 * 只会让人以为改它有用。真要换包，回「详情」那一格 —— 那里改了会
 * 把整个页面的上下文一起带过去（`go` 出去，所有 tab 跟着变）。
 */
function curPkgNote(q, backTab = 'info') {
  if (!q || !q.pkg) return null;
  return el('div', { class: 'cur-file' },
    el('span', { class: 'cf-label' }, '当前应用'),
    el('span', { class: 'cf-path mono', title: q.pkg }, q.pkg),
    el('button', {
      class: 'btn sm', title: '回到「详情」那一格换一个应用',
      onclick: () => goTab('device.app.detail', backTab, { pkg: q.pkg, apk: q.apk }),
    }, '换应用'));
}

/* 「应用详情」合并页的**详情**那一格 */
const appDetailOwnTab = async function (host, cap, q) {
  const inp = await pkgInput('com.example.app', q.pkg || '');
  const out = el('div');

  // 查一个包 = 换整个页面的上下文（不是只刷新这一格）：
  // 用 go() 重进本页，这样签名/导出/操作几格指向的也是同一个包
  const query = (pkg) => {
    const p = (pkg || '').trim();
    if (!p) { toast('请填包名', 'bad'); return; }
    go('device.app.detail', { pkg: p, tab: 'info' });
  };

  host.appendChild(card('应用详情', null, el('div', { class: 'row' },
    el('div', { class: 'grow' }, inp),
    el('button', { class: 'btn primary', onclick: () => query(inp.value) }, '查询'))));
  host.appendChild(out);
  if (!q.pkg) return;

  const pkg = q.pkg;
  out.appendChild(spinner());
  try {
    const d = await api(`/api/apps/${encodeURIComponent(pkg)}`, { params: { device: devParam() } });
    out.innerHTML = '';
    if (d.error) { out.appendChild(notice('bad', d.error)); return; }
    out.appendChild(card(d.label || d.package, null, kvBlock([
      ['包名', d.package], ['版本', `v${d.versionName} (${d.versionCode})`],
      ['UID', d.uid], ['minSdk / targetSdk', `${d.minSdk ?? '-'} / ${d.targetSdk ?? '-'}`],
      ['首次安装', d.firstInstallTime], ['最后更新', d.lastUpdateTime],
      ['APK 路径', d.apkPath], ['数据目录', d.dataDir], ['安装来源', d.installer],
      ['系统应用', d.isSystem ? '是' : '否'], ['ABI', d.abi],
    ], ['包名', 'APK 路径', '数据目录']),
      el('div', { class: 'row', style: { marginTop: '12px' } },
        // 这几步以前是独立页面，现在就在同一页的下一格
        el('button', { class: 'btn sm', onclick: () => goTab('device.app.detail', 'sign', { pkg }) }, '签名'),
        el('button', { class: 'btn sm', onclick: () => goTab('device.app.detail', 'export', { pkg }) }, '导出'),
        el('button', { class: 'btn sm', onclick: () => goTab('device.app.detail', 'actions', { pkg }) }, '操作'),
        el('button', { class: 'btn sm', onclick: () => go('device.launch.start', { pkg, tab: 'comps' }) }, '组件列表'),
        el('button', { class: 'btn sm', onclick: () => go('device.launch.start', { pkg, tab: 'start' }) }, '启动'),
      )));
  } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
};
VIEWS['device.app.detail'] = mergedPage('device.app.detail', appDetailOwnTab);

VIEWS['device.app.sign'] = async function (host, cap, q) {
  const box = el('div');
  const note = curPkgNote(q, 'sign');
  if (note) {
    host.appendChild(note);
  } else {
    const inp = await pkgInput('com.example.app', '');
    host.appendChild(card('已安装应用签名', '免 root 两级加载：先 dumpsys 摘要（约 0.3s），后拉包完整解析',
      el('div', { class: 'row' },
        el('div', { class: 'grow' }, inp),
        el('button', { class: 'btn primary', onclick: () => load((inp.value || '').trim()) }, '查询'),
        el('button', {
          class: 'btn', onclick: () => load((inp.value || '').trim(), true),
        }, '忽略缓存重拉'))));
  }
  host.appendChild(box);
  if (note && q.pkg) load(q.pkg);

  async function load(pkg, refresh) {
    if (!pkg) { toast('请填包名', 'bad'); return; }
    box.innerHTML = '';
    box.appendChild(spinner('快路径：读取 dumpsys 签名摘要…'));
    let fast = null;
    try {
      fast = await api(`/api/apps/${encodeURIComponent(pkg)}/signature`, {
        params: { device: devParam(), fast: '1', refresh: refresh ? '1' : '' },
      });
    } catch (e) { /* 快路径失败就等全量 */ }
    box.innerHTML = '';
    if (fast && fast.ok) box.appendChild(signBlock(pkg, fast, '快路径结果（dumpsys）'));
    else if (fast && !fast.ok) box.appendChild(notice('warn', '快路径没拿到：' + (fast.reason || '未知')));

    const fullBox = el('div', {}, spinner('完整路径：拉取 base.apk 并本地解析签名…'));
    box.appendChild(fullBox);
    try {
      const full = await api(`/api/apps/${encodeURIComponent(pkg)}/signature`, {
        params: { device: devParam(), fast: '', refresh: refresh ? '1' : '' },
      });
      fullBox.innerHTML = '';
      if (full.ok) {
        fullBox.appendChild(signBlock(pkg, full, '完整结果（拉包本地解析）'));
        if (fast && fast.ok && full.identity && fast.identity && fast.identity !== full.identity) {
          fullBox.appendChild(notice('warn', '快慢两条路径的签名标识不一致，值得留意'));
        }
      } else {
        fullBox.appendChild(card('完整路径失败', full.source,
          notice('warn', full.reason || '取不到'),
          (full.commands || []).length ? codeBlock((full.commands || []).join('\n')) : null,
          el('div', { style: { color: 'var(--dim)', fontSize: '12px' } },
            '没有 root 权限时，签名只能靠"把 APK 拉回本机"这条路。若设备拒绝 adb pull，就到此为止。')));
      }
    } catch (e) {
      fullBox.innerHTML = '';
      fullBox.appendChild(notice('bad', e.message));
    }
  }

  function signBlock(pkg, res, title) {
    const body = [
      kvBlock([
        ['签名方案', (res.schemes || []).map((s) => s.toUpperCase()).join(' · ')],
        ['签名标识', res.identity],
        ['来源', res.source],
        ['粒度', res.full ? '完整证书' : '仅摘要'],
      ], ['签名标识']),
    ];
    for (const [i, c] of (res.certificates || []).entries()) {
      body.push(el('div', { style: { marginTop: '10px' } },
        el('div', { style: { fontWeight: '600', marginBottom: '5px' } }, '签名者 #' + (i + 1)),
        kvBlock([['主体', c.subject], ['颁发者', c.issuer], ['SHA-256', c.sha256],
                 ['SHA-1', c.sha1],
                 ['有效期', (c.notBefore || '') + ' → ' + (c.notAfter || '')]],
        ['SHA-256', 'SHA-1'])));
    }
    if (res.cached) body.push(el('div', { style: { marginTop: '8px', color: 'var(--dim)', fontSize: '12px' } }, '（来自本地缓存）'));
    return card(title, pkg, ...body);
  }
};

VIEWS['device.app.signcmp'] = async function (host, cap, q) {
  const apk = el('input', { type: 'text', class: 'mono', placeholder: 'D:\\app.apk', value: q.apk || '' });
  const pkg = await pkgInput('com.example.app（留空则用 APK 里的包名）', q.pkg || '');
  const out = el('div');
  host.appendChild(card('签名比对', '已安装应用 ⟷ 本机 APK',
    curPkgNote(q, 'cmp'),
    el('div', { class: 'field' }, el('label', {}, '本机 APK'),
      el('div', { class: 'row' },
        el('div', { class: 'grow' }, apk),
        // 手打 APK 路径同样是这轮要消掉的交互 —— 原生文件对话框直接给真路径
        el('button', {
          class: 'btn', onclick: async () => {
            const r = await pickApk();
            if (r && r.path) { apk.value = r.path; toast('已选中 ' + (r.name || ''), 'ok'); }
          },
        }, '浏览…'))),
    el('div', { class: 'field' }, el('label', {}, '包名（可留空）'), pkg),
    el('button', {
      class: 'btn primary', onclick: async () => {
        const a = apk.value.trim();
        if (!a) { toast('请先选一个本机 APK', 'bad'); return; }
        out.innerHTML = ''; out.appendChild(spinner('比对中（可能要拉设备上的包）…'));
        try {
          const d = await api('/api/apk/signature/compare', {
            method: 'POST',
            body: { apk: a, pkg: pkg.value.trim(), device: devParam() },
          });
          out.innerHTML = '';
          const same = d.verdict === 'same';
          out.appendChild(card('结论', null,
            notice(same ? 'ok' : 'warn', `${d.verdict} —— ${d.reason || ''}`),
            kvBlock([['设备端', d.installedSha256 || d.deviceSignature || ''],
                     ['本机 APK', d.localSha256 || d.apkSignature || '']],
            ['设备端', '本机 APK']),
            el('div', { style: { marginTop: '8px', fontSize: '12px', color: 'var(--dim)' } },
              same ? '签名一致 → 可以直接覆盖安装' : '签名不一致 → 覆盖安装会被 INSTALL_FAILED_UPDATE_INCOMPATIBLE 拒绝')));
        } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
      },
    }, '开始比对')));
  host.appendChild(out);
};

VIEWS['device.app.export'] = async function (host, cap, q) {
  const note = curPkgNote(q, 'export');
  const pkg = note ? null : await pkgInput('com.example.app', q.pkg || '');
  const out = el('input', { type: 'text', class: 'mono', placeholder: '留空 → output/ 目录（建议点右边「选择目录…」）' });
  const res = el('div');

  // 「保存到」以前只有一个手打路径的输入框 —— 让人默写自己磁盘的绝对路径
  // 是这轮明确要消掉的交互。原生目录对话框优先，不可用就退页面内的目录浏览器。
  const pickDir = el('button', {
    class: 'btn', title: '选一个本机目录作为导出目标（不用手打路径）',
    onclick: async () => {
      const p = await pickLocalDir({ title: '导出到哪个目录', start: out.value.trim() });
      if (!p) return;
      out.value = p;
      toast('导出到 ' + p, 'ok');
    },
  }, '选择目录…');

  const pkgField = el('div', { class: 'grow' }, el('div', { class: 'field' }, el('label', {}, '包名'), pkg));
  if (note) host.appendChild(note);
  host.appendChild(card('导出已安装 APK', '把设备上的 base.apk 拉回本机（与签名共用缓存）',
    el('div', { class: 'row' },
      note ? null : pkgField,
      el('div', { class: 'grow' }, el('div', { class: 'field' }, el('label', {}, '保存到'), out))),
    el('div', { class: 'row', style: { marginTop: '4px' } }, pickDir),
    el('button', {
      class: 'btn primary', onclick: async () => {
        const p = (note ? q.pkg : (pkg.value || '')).trim();
        if (!p) { toast('请填包名', 'bad'); return; }
        res.innerHTML = ''; res.appendChild(spinner('拉取中…'));
        try {
          const r = await api(`/api/apps/${encodeURIComponent(p)}/export`, {
            method: 'POST', body: { out: out.value.trim(), device: devParam() },
          });
          res.innerHTML = '';
          res.appendChild(r.ok
            ? card('导出成功', null, kvBlock([['文件', r.path], ['大小', fmtBytes(r.bytes)]], ['文件']),
                el('div', { class: 'row' },
                  el('button', { class: 'btn sm', onclick: () => copyText(r.path) }, '复制路径'),
                  el('button', {
                    class: 'btn sm',
                    onclick: () => go('local.parse', { path: r.path, tab: 'info' }),
                  }, '解析它'),
                  el('button', {
                    class: 'btn sm', title: '打开它所在的目录（下次导出会默认落在那儿）',
                    onclick: () => copyText(String(r.path || '').replace(/[\\/][^\\/]*$/, '')),
                  }, '复制所在目录')))
            : notice('bad', r.detail || '失败'));
        } catch (e) { res.innerHTML = ''; res.appendChild(notice('bad', e.message)); }
      },
    }, '导出')));
  host.appendChild(res);
};

/* 「操作」那一格：停止 / 清除数据 / 卸载 / 启用 / 禁用 / 重启 全在一屏。
 *
 * 合并不是为了省地方，是因为这三个动作**永远是针对同一个包**做选择 ——
 * 拆成三个页面时，用户想"先停掉再清数据"得来回跳两次、每次重新填一遍包名。
 */
const APP_ACTIONS = [
  { op: 'stop', label: '强制停止', danger: false, tip: 'force-stop' },
  { op: 'restart', label: '重启', danger: false, tip: '先 force-stop 再拉起首页' },
  { op: 'clear', label: '清除数据', danger: true, tip: 'pm clear —— 应用数据全部丢失，不可恢复' },
  { op: 'enable', label: '启用', danger: false, tip: 'pm enable' },
  { op: 'disable', label: '禁用', danger: false, tip: 'pm disable-user' },
  { op: 'uninstall', label: '卸载', danger: true, tip: 'pm uninstall —— 应用与数据一并删除' },
];

const appActionsView = async function (host, cap, q) {
  const note = curPkgNote(q, 'actions');
  const pkg = note ? null : await pkgInput('com.example.app', q.pkg || '');
  const out = el('div');

  const pkgOf = () => (note ? q.pkg : (pkg.value || '')).trim();

  async function doAction(op, danger) {
    const p = pkgOf();
    if (!p) { toast('请填包名', 'bad'); return; }
    if (danger) {
      const ok = await confirmTyping({
        title: op === 'clear' ? '确认清除数据' : '确认卸载',
        message: op === 'clear'
          ? `缓存清掉；**应用数据会全部丢失且不可恢复**。`
          : `应用会被卸载，数据一并删除。`,
        expect: p, action: op === 'clear' ? '清除数据' : '卸载',
      });
      if (!ok) return;
    }
    out.innerHTML = ''; out.appendChild(spinner('执行中…'));
    try {
      const r = await api(`/api/apps/${encodeURIComponent(p)}/action`, {
        method: 'POST', body: { op, device: devParam(), confirm: danger ? p : '' },
      });
      out.innerHTML = '';
      out.appendChild(r.ok ? notice('ok', r.detail || '完成') : notice('bad', r.detail || '失败'));
      if ((r.commands || []).length) out.appendChild(codeBlock(r.commands.join('\n')));
    } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
  }

  if (note) host.appendChild(note);
  host.appendChild(card('应用操作', '危险动作要输入包名二次确认（后端也会校验一次）',
    note ? null : el('div', { class: 'row' }, el('div', { class: 'grow' }, pkg)),
    el('div', { class: 'row', style: { marginTop: '6px' } },
      ...APP_ACTIONS.map((a) => el('button', {
        class: 'btn ' + (a.danger ? 'danger' : ''), title: a.tip,
        onclick: () => doAction(a.op, a.danger),
      }, a.label)))));
  host.appendChild(out);
};

VIEWS['device.app.stop'] = appActionsView;
// 这两个 ID 还留着（CLI / API / 老书签指着它们），但视图是同一个 ——
// 侧栏和 tab 栏里它们都归到「操作」那一格。
VIEWS['device.app.clear'] = appActionsView;
VIEWS['device.app.uninstall'] = appActionsView;

/* ------------------------------------------------ 启动器 */

VIEWS['device.launch.list'] = async function (host, cap, q) {
  const pkg = await pkgInput('com.example.app', q.pkg || '');
  const out = el('div');
  const run = async (refresh) => {
    const p = pkg.value.trim();
    if (!p) { toast('请填包名', 'bad'); return; }
    out.innerHTML = '';
    out.appendChild(spinner('解析组件（首次会拉一次 base.apk）…'));
    try {
      const r = await api('/api/launch/components', {
        params: { pkg: p, device: devParam(), refresh: refresh ? '1' : '' },
      });
      out.innerHTML = '';
      const rows = r.components || [];
      out.appendChild(card(`共 ${rows.length} 个组件`, null, dataTable([
        { key: 'type', label: '类型', width: '76px' },
        { key: 'name', label: '组件', mono: true },
        { key: 'exported', label: '导出', width: '62px', render: (c) => c.exported === true ? tag('是', 'warn') : (c.exported === false ? tag('否') : '-') },
        { key: 'source', label: '来源', width: '66px' },
        { key: 'summary', label: '备注' },
        {
          key: '_a', label: '', width: '120px', render: (c) => el('div', { style: { display: 'flex', gap: '5px' } },
            el('button', {
              class: 'btn sm', onclick: (e) => {
                e.stopPropagation();
                // 用后端给的 target（pkg/.Class）；老接口没有 target 字段时现场拼，
                // 但一定要把 pkg 带上，否则后端分不出包名/类名边界。
                go('device.launch.start', { component: compTarget(c, p), pkg: p, kind: c.type });
              },
            }, '编辑'),
            el('button', {
              class: 'btn sm primary', onclick: (e) => { e.stopPropagation(); launchNow(compTarget(c, p), p); },
            }, '直接启动')),
        },
      ], rows, { filter: true, filterPlaceholder: '过滤组件…', emptyText: '没解析到组件' })));
    } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
  };
  host.appendChild(card('组件列表', 'APK 解析为主路径（比 dumpsys 更全），失败自动回退',
    el('div', { class: 'row' },
      el('div', { class: 'grow' }, pkg),
      el('button', { class: 'btn primary', onclick: () => run(false) }, '列出组件'),
      el('button', { class: 'btn', onclick: () => run(true) }, '忽略缓存'))));
  host.appendChild(out);
  if (q.pkg) run(false);
};

/* 从「我的入口」跳回「启动」那一格时要带过去的整条 Intent。
   走 URL 放不下（extras 是数组、还有 flags/categories），所以用一个
   一次性的模块级变量交接：`launchOwnTab` 取走后就置空。 */
let LAUNCH_SEED = null;

/* 「启动组件」合并页的**启动**那一格 */
const launchOwnTab = async function (host, cap, q) {
  let catalog = { flags: [], commonFlags: [], commonActions: [], extrasTypes: ['string', 'int', 'bool', 'float', 'long', 'uri'] };
  try { catalog = await api('/api/launch/catalog'); } catch (e) { }

  // 从「我的入口」那一格点"编辑参数"过来时，把整条 Intent 塞进表单 ——
  // 只带组件名是不够的：收藏的正是那些 extras / flags
  const seed = LAUNCH_SEED;
  LAUNCH_SEED = null;
  const st = {
    kind: q.kind === 'service' ? 'service' : (seed && seed.kind === 'service' ? 'service' : 'activity'),
    target: (seed && seed.target) || q.component || '',
    // 从「组件清单」跳过来时会带上所属包名。它的作用是让后端能把
    // 全限定类名（com.x.y.ui.MainActivity）确定地拆成 pkg/.Class ——
    // 光靠字符串是拆不出来的（com.android.settings.homepage.SettingsHomepageActivity
    // 的真包名只是 com.android.settings）。
    pkg: (seed && (seed.package || seed.pkg)) || q.pkg || '',
    action: (seed && seed.action) || '',
    data: (seed && seed.data) || '',
    mimeType: (seed && seed.mimeType) || '',
    categories: (seed && seed.categories) ? seed.categories.slice() : [],
    flags: (seed && seed.flags) ? seed.flags.slice() : [],
    extras: (seed && seed.extras) ? seed.extras.map((x) => ({ key: x.key, value: x.value, type: x.type })) : [],
    user: (seed && seed.user) || '',
    wait: !!(seed && seed.wait),
  };

  const target = el('input', { type: 'text', class: 'mono', placeholder: 'com.example/.ui.MainActivity', value: st.target });
  // 「所属包名」以前是**隐藏**的（只从组件清单跳转时带过来），手动输入类名时
  // 没人能告诉后端包名边界 —— 而安装包名(applicationId)和代码包名经常不一样：
  // com.jxw.aizx.teacher 里所有类都在 com.jxw.teacher.* 下，
  // 光看 `com.jxw.teacher.ui.activity.LoginActivity` 是拆不出包名的。
  const pkgI = el('input', { type: 'text', class: 'mono',
    placeholder: 'com.example.app（选填；类名和包名不同源时必填）', value: st.pkg });
  const action = el('input', { type: 'text', class: 'mono', list: 'dl-actions', placeholder: 'android.intent.action.MAIN', value: st.action });
  const dataI = el('input', { type: 'text', class: 'mono', placeholder: 'content://… 或 https://…', value: st.data });
  const mime = el('input', { type: 'text', class: 'mono', placeholder: 'image/*', value: st.mimeType });
  const userI = el('input', { type: 'text', class: 'mono', placeholder: '0', style: { width: '90px' }, value: st.user });
  const waitC = el('input', { type: 'checkbox', checked: st.wait });
  const catI = el('input', { type: 'text', class: 'mono', placeholder: '输入后回车加入' });

  if (!$('#dl-actions')) {
    const dl = el('datalist', { id: 'dl-actions' });
    for (const a of (catalog.commonActions || [])) dl.appendChild(el('option', { value: a }));
    document.body.appendChild(dl);
  }

  const catBox = el('div', { class: 'row' });
  function paintCats() {
    catBox.innerHTML = '';
    for (const c of st.categories) {
      catBox.appendChild(el('span', {
        class: 'tag brand', style: { cursor: 'pointer' },
        onclick: () => { st.categories = st.categories.filter((x) => x !== c); paintCats(); preview(); },
      }, c + ' ✕'));
    }
    if (!st.categories.length) catBox.appendChild(el('span', { style: { color: 'var(--dim)', fontSize: '12px' } }, '（无）'));
  }
  paintCats();
  catI.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && catI.value.trim()) {
      st.categories.push(catI.value.trim()); catI.value = ''; paintCats(); preview();
    }
  });

  const flagBox = el('div', { class: 'row' });
  const allFlags = catalog.flags || [];
  function paintFlags(showAll) {
    flagBox.innerHTML = '';
    const list = showAll ? allFlags : allFlags.filter((f) => f.common);
    for (const f of list) {
      const on = st.flags.includes(f.name);
      flagBox.appendChild(el('span', {
        class: 'tag ' + (on ? 'brand' : 'plain'),
        style: { cursor: 'pointer' },
        title: f.value,
        onclick: () => {
          st.flags = on ? st.flags.filter((x) => x !== f.name) : st.flags.concat([f.name]);
          paintFlags(showAll); preview();
        },
      }, f.name.replace('ACTIVITY_', '')));
    }
    if (!showAll) {
      flagBox.appendChild(el('span', {
        class: 'tag plain', style: { cursor: 'pointer' },
        onclick: () => paintFlags(true),
      }, '更多…'));
    }
  }
  paintFlags(false);

  const extraBox = el('div');
  function paintExtras() {
    extraBox.innerHTML = '';
    if (!st.extras.length) {
      extraBox.appendChild(el('div', { style: { color: 'var(--dim)', fontSize: '12px' } }, '（无）'));
      return;
    }
    st.extras.forEach((x, i) => {
      const k = el('input', { type: 'text', class: 'mono', placeholder: 'key', value: x.key,
        oninput: (e) => { x.key = e.target.value; preview(); } });
      const v = el('input', { type: 'text', class: 'mono', placeholder: 'value', value: x.value,
        oninput: (e) => { x.value = e.target.value; preview(); } });
      const t = el('select', { style: { width: '110px' },
        onchange: (e) => { x.type = e.target.value; paintExtras(); preview(); } },
        (catalog.extrasTypes || []).map((tt) => el('option', { value: tt, selected: tt === x.type }, tt)));
      extraBox.appendChild(el('div', { class: 'row', style: { marginBottom: '6px' } },
        el('div', { style: { width: '150px' } }, k),
        el('div', { style: { width: '110px' } }, t),
        el('div', { class: 'grow' }, v),
        el('button', { class: 'btn sm', onclick: () => { st.extras.splice(i, 1); paintExtras(); preview(); } }, '✕')));
    });
  }
  paintExtras();

  const previewBox = el('div');
  let timer = null;
  function preview() {
    clearTimeout(timer);
    timer = setTimeout(doPreview, 220);
  }
  async function doPreview() {
    sync();
    previewBox.innerHTML = '';
    previewBox.appendChild(spinner('生成命令…'));
    try {
      const r = await api('/api/launch/preview', {
        method: 'POST', body: { ...bodyOf(), device: devParam() },
      });
      previewBox.innerHTML = '';
      if (r.problem) previewBox.appendChild(notice('warn', r.problem));
      // 解析不出来时，后端会把"设备上可能属于这个类名"的包名列出来 ——
      // 点一下就等于把「所属包名」填对，不用用户自己去猜 applicationId。
      const cands = (r.candidates && r.candidates.length)
        ? r.candidates : ((r.intent && r.intent.resolveCandidates) || []);
      if (cands.length) {
        previewBox.appendChild(el('div', { class: 'row', style: { marginTop: '8px', alignItems: 'center' } },
          el('span', { style: { color: 'var(--dim)', fontSize: '12px', marginRight: '6px' } },
            '设备上相关的包名（点一下填入「所属包名」）：'),
          ...cands.map((c) => el('span', {
            class: 'tag brand', style: { cursor: 'pointer' },
            title: '点一下填入「所属包名」',
            onclick: () => { pkgI.value = c; sync(); preview(); },
          }, c))));
      }
      previewBox.appendChild(codeBlock(r.command || '(空)'));
      // 包名是后端按类名反查出来的（不是我们明确知道的）→ 必须说清楚，
      // 用户才知道"命令里这串包名我没填过，是系统帮我推的"。
      if (r.intent && r.intent.pkgInferred) {
        previewBox.appendChild(notice('warn',
          '「所属包名」是自动推断的：' + pkgOfTarget(r.intent.normalizedTarget)
          + '（按类名在设备上反查得到 —— 安装包名和代码包名不同源）。不对就手动改。'));
      }
      previewBox.appendChild(el('div', { style: { color: 'var(--dim)', fontSize: '11.5px' } },
        'Flag 合并值：' + (r.intent && r.intent.flagValue ? r.intent.flagValue : '（无）')
        + (r.intent && r.intent.normalizedTarget ? '　有效组件：' + r.intent.normalizedTarget : '')));
    } catch (e) { previewBox.innerHTML = ''; previewBox.appendChild(notice('bad', e.message)); }
  }

  function sync() {
    st.target = target.value.trim();
    st.pkg = pkgI.value.trim();
    st.action = action.value.trim();
    st.data = dataI.value.trim();
    st.mimeType = mime.value.trim();
    st.user = userI.value.trim();
    st.wait = waitC.checked;
  }
  function bodyOf() {
    return {
      kind: st.kind, target: st.target, pkg: st.pkg, action: st.action, data: st.data,
      mimeType: st.mimeType, categories: st.categories, flags: st.flags,
      extras: st.extras, user: st.user, wait: st.wait,
    };
  }

  [target, pkgI, action, dataI, mime, userI].forEach((i) => i.addEventListener('input', preview));
  waitC.addEventListener('change', preview);

  const kindSeg = el('div', { class: 'seg' },
    ['activity', 'service'].map((k) => el('button', {
      class: st.kind === k ? 'on' : '',
      onclick: (e) => {
        st.kind = k;
        $$('button', kindSeg).forEach((b) => b.classList.toggle('on', b.textContent === k));
        preview();
      },
    }, k)));

  const runOut = el('div');

  host.appendChild(card('Intent 参数', '命令预览与实际执行走同一段代码，不可能不一致',
    el('div', { class: 'field' }, el('label', {}, '目标组件 / 包名'), target,
      el('div', { style: { color: 'var(--dim)', fontSize: '11.5px', marginTop: '4px' } },
        '三种写法：com.foo/.ui.MainActivity（组件）· com.foo（自动查启动入口）· '
        + '只填 Action + Data 走 URI 方式（VIEW + https://…）')),
    el('div', { class: 'field' }, el('label', {}, '所属包名（选填）'), pkgI,
      el('div', { style: { color: 'var(--dim)', fontSize: '11.5px', marginTop: '4px' } },
        '只在「类名和包名不同源」时才需要：安装包名(applicationId)和代码包名经常不一样，'
        + '比如 com.jxw.aizx.teacher 里所有类都在 com.jxw.teacher.* 下 —— '
        + '这时上面那串全限定类名光靠字符串是拆不出边界的。留空也行，系统会拿设备上已装的包反查。')),
    el('div', { class: 'field' }, el('label', {}, 'Intent 类型'), kindSeg),
    el('div', { class: 'row' },
      el('div', { class: 'grow' }, el('div', { class: 'field' }, el('label', {}, 'Action'), action)),
      el('div', { class: 'grow' }, el('div', { class: 'field' }, el('label', {}, 'MIME'), mime))),
    el('div', { class: 'field' }, el('label', {}, 'Data URI'), dataI),
    el('div', { class: 'field' }, el('label', {}, 'Categories'), catI, catBox),
    el('div', { class: 'field' }, el('label', {}, 'Flags'), flagBox),
    el('div', { class: 'field' },
      el('label', {}, 'Extras'),
      extraBox,
      el('button', { class: 'btn sm', onclick: () => { st.extras.push({ key: '', value: '', type: 'string' }); paintExtras(); } }, '+ 加一个')),
    el('div', { class: 'row' },
      el('div', { class: 'field' }, el('label', {}, '--user'), userI),
      el('label', { class: 'check' }, waitC, '等待启动完成（-W）')),
    el('div', { class: 'row', style: { marginTop: '10px' } },
      el('button', {
        class: 'btn primary', onclick: async () => {
          sync();
          runOut.innerHTML = ''; runOut.appendChild(spinner('执行中…'));
          try {
            const r = await api('/api/launch/start', { method: 'POST', body: { ...bodyOf(), device: devParam() } });
            runOut.innerHTML = '';
            runOut.appendChild(r.ok ? notice('ok', r.detail || '已发送启动请求') : notice('bad', r.detail || '启动失败'));
            if (r.command) runOut.appendChild(codeBlock(r.command));
            if (r.warning) runOut.appendChild(notice('warn', r.warning));
            if (r.totalTime != null) runOut.appendChild(kvBlock([['TotalTime', r.totalTime + ' ms'], ['ThisTime', (r.thisTime ?? 0) + ' ms']]));
          } catch (e) {
            runOut.innerHTML = '';
            runOut.appendChild(notice('bad', e.message));
            // 解析不出来时后端会带上"设备上相关的包名"，做成可点的标签 ——
            // 用户在「所属包名」里填对一次，这条命令就通了。
            const cands = (e.payload && e.payload.candidates) || [];
            if (cands.length) {
              runOut.appendChild(el('div', { class: 'row', style: { marginTop: '8px', alignItems: 'center' } },
                el('span', { style: { color: 'var(--dim)', fontSize: '12px', marginRight: '6px' } },
                  '点一下填入「所属包名」：'),
                ...cands.map((c) => el('span', {
                  class: 'tag brand', style: { cursor: 'pointer' },
                  onclick: () => { pkgI.value = c; sync(); preview(); },
                }, c))));
            }
          }
        },
      }, '执行'),
      el('button', {
        class: 'btn', onclick: async () => {
          sync();
          try {
            const r = await api('/api/launch/preview', { method: 'POST', body: { ...bodyOf(), device: devParam() } });
            runOut.innerHTML = '';
            runOut.appendChild(codeBlock(r.command || ''));
          } catch (e) { toast(e.message, 'bad'); }
        },
      }, '只看命令'),
      el('button', { class: 'btn', onclick: () => copyText($('.cmd', previewBox) ? $('.cmd', previewBox).textContent.replace(/^复制/, '') : '') }, '复制命令'),
      // 「配好一条 → 下次还想用」是启动器最真实的用法。以前只能靠"启动历史"
      // 翻出来再手抄一遍参数，等于没有。收藏存的是**整条 Intent**，不是裸组件名。
      el('button', {
        class: 'btn', title: '把当前这条 Intent 存成「我的入口」，下次一点就走',
        onclick: () => saveFavorite(),
      }, '☆ 收藏到我的入口'))));

  async function saveFavorite() {
    sync();
    if (!st.target && !st.action) {
      toast('先填目标组件或 Action，再收藏', 'bad'); return;
    }
    // 收藏前先让后端解析一遍：这样存下来的是**已解析的组件**（pkg/.Class），
    // 下次不必再依赖"设备上正好有这个包"才认得出来
    let resolved = null;
    try {
      resolved = await api('/api/launch/preview', {
        method: 'POST', body: { ...bodyOf(), device: devParam() },
      });
    } catch (e) { /* 解析不了也允许存，至少参数留着 */ }
    if (resolved && resolved.problem && !resolved.intent) {
      toast('这条还配不起来：' + resolved.problem, 'bad'); return;
    }

    const it = bodyOf();
    if (resolved && resolved.intent) {
      if (resolved.intent.normalizedTarget) it.target = resolved.intent.normalizedTarget;
      if (resolved.intent.package) it.pkg = resolved.intent.package;
    }
    const comp = it.target || it.action || '';
    // 默认名字取组件末段（`…ui.LoginActivity` → `LoginActivity`）——
    // 直接回车就走，不用现编一个名字
    const tail = String(comp).split('/').pop().split('.').pop() || '入口';
    const name = await ask({
      title: '收藏到我的入口',
      message: '给它起个名字（下次在「我的入口」里直接点，也可以随时改名）',
      value: tail, placeholder: tail,
    });
    if (!name) return;
    try {
      const r = await api('/api/launch/shortcut', {
        method: 'POST',
        body: { op: 'add', name, component: comp, intent: it, device: devParam() },
      });
      if (r.ok) toast(r.detail || '已收藏', 'ok');
      else toast(r.detail || '收藏失败', 'bad');
    } catch (e) { toast('收藏失败：' + e.message, 'bad'); }
  }

  host.appendChild(card('命令预览', null, previewBox));
  host.appendChild(runOut);
  preview();
};
VIEWS['device.launch.start'] = mergedPage('device.launch.start', launchOwnTab);

VIEWS['device.launch.broadcast'] = async function (host, cap, q) {
  const action = el('input', { type: 'text', class: 'mono', placeholder: 'com.example.ACTION', value: q.action || '' });
  const pkg = el('input', { type: 'text', class: 'mono', placeholder: '限定包（-p）' });
  const out = el('div');
  host.appendChild(card('发送广播', 'am broadcast',
    el('div', { class: 'field' }, el('label', {}, 'Action'), action),
    el('div', { class: 'field' }, el('label', {}, '限定包'), pkg),
    el('button', {
      class: 'btn primary', onclick: async () => {
        if (!action.value.trim()) { toast('请填 Action', 'bad'); return; }
        out.innerHTML = ''; out.appendChild(spinner());
        try {
          const r = await api('/api/launch/broadcast', {
            method: 'POST',
            body: { kind: 'broadcast', action: action.value.trim(), package: pkg.value.trim(), device: devParam() },
          });
          out.innerHTML = '';
          out.appendChild(r.ok ? notice('ok', r.detail || '已发送') : notice('bad', r.detail || '失败'));
          if (r.command) out.appendChild(codeBlock(r.command));
        } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
      },
    }, '发送')));
  host.appendChild(out);
};

VIEWS['device.launch.history'] = async function (host) {
  const out = el('div');
  host.appendChild(card('启动历史', null, el('button', {
    class: 'btn', onclick: async () => { await api('/api/launch/history/clear', { method: 'POST', body: {} }); toast('已清空'); load(); },
  }, '清空历史')));
  host.appendChild(out);
  async function load() {
    out.innerHTML = ''; out.appendChild(spinner());
    try {
      const r = await api('/api/launch/history', { params: { limit: 80 } });
      out.innerHTML = '';
      out.appendChild(card(`共 ${(r.history || []).length} 条`, null, dataTable([
        { key: 'time', label: '时间', width: '150px' },
        { key: 'component', label: '组件', mono: true },
        { key: 'result', label: '结果' },
      ], r.history || [], {
        filter: true,
        emptyText: '还没有启动记录',
        onRow: (h) => go('device.launch.start', { component: h.component }),
      })));
    } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
  }
  load();
};

VIEWS['device.launch.shortcut'] = async function (host, cap, q) {
  const out = el('div');

  async function post(body) {
    try {
      const r = await api('/api/launch/shortcut', { method: 'POST', body });
      if (r.ok) toast(r.detail || '完成', 'ok');
      else toast(r.detail || '操作失败', 'bad');
      load();
      return r.ok;
    } catch (e) { toast(e.message, 'bad'); return false; }
  }

  /** 直接把收藏的整条 Intent 启动掉（不必先跳回「启动」那一格）。 */
  async function runNow(s) {
    const body = Object.assign({}, s.intent || {});
    if (!body.target && !body.action && s.component) body.target = s.component;
    body.device = devParam();
    if (!body.kind) body.kind = 'activity';
    try {
      const r = await api('/api/launch/start', { method: 'POST', body });
      toast(r.ok ? ('已启动 ' + (r.component || s.name)) : (r.detail || '启动失败'),
        r.ok ? 'ok' : 'bad');
    } catch (e) { toast('启动失败：' + e.message, 'bad'); }
  }

  host.appendChild(card('我的入口', '配好的启动命令存下来，下次一点就走；名字随时可改',
    el('div', { class: 'row' },
      el('button', {
        class: 'btn primary', onclick: async () => {
          const name = await ask({ title: '新建入口', message: '给它起个名字', placeholder: '登录页' });
          if (!name) return;
          const comp = await ask({
            title: '目标组件', message: '包名/.类名 或 完整类名（也可以只在「启动」那一格配好再点 ☆ 收藏）',
            placeholder: 'com.example/.ui.Main',
          });
          if (!comp) return;
          await post({ op: 'add', name, component: comp });
        },
      }, '+ 新建入口'),
      el('button', {
        class: 'btn', title: '去「启动」那一格配一条带参数的，再点 ☆ 收藏',
        onclick: () => goTab('device.launch.start', 'start', {}),
      }, '去配一条带参数的'))));

  host.appendChild(out);
  await load();

  async function load() {
    out.innerHTML = ''; out.appendChild(spinner());
    let list = [];
    try {
      const r = await api('/api/launch/shortcut');
      list = r.shortcuts || [];
    } catch (e) {
      out.innerHTML = ''; out.appendChild(notice('bad', e.message));
      return;
    }
    out.innerHTML = '';
    if (!list.length) {
      out.appendChild(card(null, null, emptyState('⭐',
        '还没有「我的入口」—— 在「启动」那一格配好一条，点「☆ 收藏到我的入口」')));
      return;
    }

    const cols = [
      {
        key: 'name', label: '名称', width: '190px', render: (s) => el('span', {
          class: 'fav-name', title: '点一下直接启动',
          onclick: () => runNow(s),
        }, s.name),
      },
      {
        key: 'component', label: '组件 / Action', mono: true,
        render: (s) => s.component
          || ((s.intent && s.intent.action) ? '（Action）' + s.intent.action : ''),
      },
      {
        key: '_d', label: '参数', width: '150px',
        render: (s) => {
          const it = s.intent || {};
          const bits = [];
          if ((it.extras || []).length) bits.push('extras ' + it.extras.length);
          if ((it.flags || []).length) bits.push('flags ' + it.flags.length);
          if (it.data) bits.push('data');
          if ((it.categories || []).length) bits.push('cat ' + it.categories.length);
          if (it.user) bits.push('user ' + it.user);
          return bits.length
            ? el('span', { style: { color: 'var(--dim)', fontSize: '11.5px' } }, bits.join(' · '))
            : el('span', { style: { color: 'var(--dim)' } }, '-');
        },
      },
      {
        key: '_a', label: '', width: '250px', render: (s) => el('div', { style: { display: 'flex', gap: '5px' } },
          el('button', {
            class: 'btn sm primary', title: '用存下来的这条 Intent 启动',
            onclick: (e) => { e.stopPropagation(); runNow(s); },
          }, '启动'),
          el('button', {
            class: 'btn sm', title: '改名字（组件和参数不动）',
            onclick: async (e) => {
              e.stopPropagation();
              const name = await ask({
                title: '重命名入口', message: '只改显示名，组件和参数都保留',
                value: s.name, placeholder: s.name,
              });
              if (!name || name === s.name) return;
              await post({ op: 'edit', id: s.id, name });
            },
          }, '改名'),
          el('button', {
            class: 'btn sm', title: '把整条 Intent（含 extras/flags）载入「启动」那一格去改',
            onclick: (e) => {
              e.stopPropagation();
              LAUNCH_SEED = s.intent && Object.keys(s.intent).length
                ? s.intent : { target: s.component, kind: 'activity' };
              goTab('device.launch.start', 'start', {});
            },
          }, '编辑参数'),
          el('button', {
            class: 'btn sm danger', title: '删除这个入口',
            onclick: async (e) => {
              e.stopPropagation();
              const ok = await ask({
                title: '删除入口', message: '确定删除「' + s.name + '」？（只删这个快捷方式，不影响应用）',
                value: s.name, action: '删除',
              });
              if (!ok) return;
              await post({ op: 'del', id: s.id });
            },
          }, '删除')),
      },
    ];
    out.appendChild(card(`共 ${list.length} 个`, '点名称=直接启动 · 改名只动名字 · 编辑参数会载入整条 Intent',
      dataTable(cols, list, { filter: true, filterPlaceholder: '按名称/包名过滤…', emptyText: '没有入口' })));
  }
};

/* ------------------------------------------------ 捕获 */

const shotOwnTab = async function (host, cap, q) {
  const out = el('div');
  const live = el('div');
  host.appendChild(card('截图', 'exec-out 直取 PNG，不落设备临时文件',
    el('div', { class: 'row' },
      el('button', {
        class: 'btn primary', onclick: async () => {
          out.innerHTML = ''; out.appendChild(spinner('截图中…'));
          try {
            const r = await api('/api/capture/screenshot', { method: 'POST', body: { device: devParam() } });
            out.innerHTML = '';
            out.appendChild(card('已保存', `${(r.bytes / 1024).toFixed(1)} KB · ${r.ms} ms`,
              el('img', { src: r.url, style: { maxWidth: '100%', border: '1px solid var(--line)', borderRadius: '6px' } }),
              el('div', { class: 'row', style: { marginTop: '9px' } },
                el('button', { class: 'btn sm', onclick: () => copyText(r.path) }, '复制路径'),
                el('a', { class: 'btn sm', href: r.url, download: '', target: '_blank' }, '打开原图'))));
          } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
        },
      }, '截一张'),
      el('button', {
        class: 'btn', onclick: async () => {
          live.innerHTML = '';
          const img = el('img', { style: { maxWidth: '100%', border: '1px solid var(--line)', borderRadius: '6px' } });
          live.appendChild(img);
          const tick = async () => {
            try {
              const r = await fetch('/api/capture/screenshot/raw', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
              if (r.ok) { const b = await r.blob(); img.src = URL.createObjectURL(b); }
            } catch (_) { }
          };
          await tick();
          const iv = setInterval(tick, 1200);
          live.appendChild(el('button', { class: 'btn sm danger', style: { marginTop: '8px' }, onclick: () => clearInterval(iv) }, '停止刷新'));
        },
      }, '连续刷新（1.2s）'))));
  host.appendChild(out);
  host.appendChild(live);
};
VIEWS['device.shot'] = mergedPage('device.shot', shotOwnTab);

VIEWS['device.logcat'] = async function (host, cap, q) {
  // 包名**不手输**（手输等于让人去背包名），也**不用 <select>**：
  // 含系统应用 170 个的时候，下拉里翻是找不动的。用带模糊过滤的输入框 ——
  // 输"微信"或"mm"都能命中 com.tencent.mm，不选就是全机日志。
  const pkg = pkgCombo('全部应用（不选 = 整机日志）｜输应用名或包名过滤', q.pkg || '', { all: true });
  const pkgNote = el('span', { style: { fontSize: '11.5px', color: 'var(--dim)' } });
  const level = el('select', { style: { width: '120px' } },
    ['V', 'D', 'I', 'W', 'E'].map((l) => el('option', { value: l }, l)));
  const kw = el('input', { type: 'text', class: 'mono', placeholder: '关键字（输入即实时过滤，不用先停）' });
  const stat = el('span', { class: 'log-status' });

  /* ---------------- 缓冲 + 窗口化渲染（性能核心） ----------------
   * 全量日志每秒能来几百行。老实现**每行** appendChild + scrollTop=scrollHeight
   * （一次强制回流）→ 界面直接卡死。现在改成：
   *   1. 行先进内存缓冲 rows（上限 MAX_BUF，旧的丢，丢多少在状态条说清楚）
   *   2. 渲染合并：150ms 一批，一批一个 DocumentFragment
   *   3. DOM 里永远只挂最近 PAGE 行（窗口化）；往上翻按页加载更早的（分批加载）
   *   4. 跟随/回看两种模式：跟随=自动吸底；回看时新行**完全不碰 DOM**，
   *      只在「回到底部」按钮上报数 —— 翻旧日志时界面零抖动
   *   5. 关键字/级别是**本地实时过滤**：流不停、缓冲不变，只重算窗口 */
  const MAX_BUF = 20000;
  const PAGE = 500;
  const FLUSH_MS = 150;

  const wrap = el('div', { class: 'logwrap' });
  const box = el('div', { class: 'logbox' });
  wrap.appendChild(box);
  const jump = el('button', { class: 'btn sm log-jump', style: { display: 'none' }, onclick: goTail }, '');
  wrap.appendChild(jump);

  let es = null;
  let n = 0;                // 一共收到多少行（含被丢弃的）
  let dropped = 0;          // 被缓冲上限挤掉的行数
  let rows = [];            // {t, lv, cls}
  let view = [];            // 过滤后待显示的行（同一批对象引用）
  let follow = true;        // 是否吸底跟随
  let winStart = 0;         // DOM 里显示的是 view[winStart .. winStart+winLen)
  let winLen = 0;
  let pending = [];         // 已收到、还没渲染的行
  let flushTimer = null;
  let refilterTimer = null;
  let lock = false;         // 程序性改 scrollTop 时，别把 scroll 事件当用户操作
  let newSinceLook = 0;     // 回看期间新到的行数（缓冲满了以后 view.length 不再增长，必须单独记）

  function lvOf(t) {
    const m = t.match(/^\S+\s+\S+\s+\d+\s+\d+\s+([VDIWEF])\//);
    return m ? m[1] : '';
  }
  function clsOf(t) {
    return /E\//.test(t) || /FATAL/.test(t) ? 'l-error'
      : /W\//.test(t) ? 'l-warn' : /I\//.test(t) ? 'l-ok' : '';
  }
  function rowEl(r) { return el('div', { class: r.cls || '' }, r.t); }
  function passes(r) {
    const k = kw.value.trim().toLowerCase();
    const lv = level.value;
    if (r.lv && lv !== 'V' && r.lv !== lv) return false;   // V = 全部；非日志行恒显示
    if (k && r.t.toLowerCase().indexOf(k) < 0) return false;
    return true;
  }

  function updStatus() {
    // 「显示」必须是 winLen（DOM 里真实行数），不是 view.length —— 否则
    // 明明窗口化只挂了 500 行，状态条却说"显示 1502 行"，自相矛盾
    stat.textContent = '收到 ' + n + ' 行 · 显示 ' + winLen + ' 行'
      + (view.length > winLen ? ' / 共 ' + view.length + ' 行' : '')
      + (dropped ? '（缓冲只留最近 ' + MAX_BUF + ' 行，已丢 ' + dropped + '）' : '')
      + (follow ? ' · 跟随最新' : ' · 回看中');
    if (!follow && newSinceLook) {
      jump.style.display = '';
      jump.textContent = '↓ 已停下翻旧日志，有 ' + newSinceLook + ' 行新日志，点击回到底部';
    } else {
      jump.style.display = 'none';
    }
  }

  function renderSlice(start, keepScroll) {
    const st = keepScroll ? box.scrollTop : 0;
    winStart = Math.max(0, Math.min(start, Math.max(0, view.length - 1)));
    const end = Math.min(view.length, winStart + PAGE);
    const frag = document.createDocumentFragment();
    for (let i = winStart; i < end; i++) frag.appendChild(rowEl(view[i]));
    box.replaceChildren(frag);
    winLen = end - winStart;
    lock = true;
    box.scrollTop = keepScroll ? st : box.scrollHeight;
    setTimeout(() => { lock = false; }, 0);
    updStatus();
  }

  function goTail() {
    follow = true;
    newSinceLook = 0;
    renderSlice(Math.max(0, view.length - PAGE));
  }

  function applyFilter() {
    view = rows.filter(passes);
  }

  function flush() {
    flushTimer = null;
    if (!pending.length) return;
    const batch = pending; pending = [];
    let cut = 0;
    for (const r of batch) rows.push(r);
    if (rows.length > MAX_BUF) { cut = rows.length - MAX_BUF; dropped += cut; rows.splice(0, cut); }

    // 先把「这批过了滤的行」算出来：吸底时走追加路径，一行都不用重建
    const batchView = batch.filter(passes);
    if (cut) { applyFilter(); } else { view = view.concat(batchView); }

    if (follow && view.length) {
      const wasTail = !winLen || (winStart + winLen >= view.length - batchView.length);
      if (wasTail && !cut && batchView.length) {
        const frag = document.createDocumentFragment();
        for (const r of batchView) frag.appendChild(rowEl(r));
        box.appendChild(frag);
        winLen += batchView.length;
        while (winLen > PAGE) { box.removeChild(box.firstChild); winStart++; winLen--; }
        lock = true;
        box.scrollTop = box.scrollHeight;
        setTimeout(() => { lock = false; }, 0);
        updStatus();
      } else {
        renderSlice(Math.max(0, view.length - PAGE));
      }
    } else {
      newSinceLook += batchView.length;   // 回看期间：新行只记数，不碰 DOM
      updStatus();
    }
  }

  function refilterNow() {
    applyFilter();
    if (follow) renderSlice(Math.max(0, view.length - PAGE));
    else renderSlice(Math.min(winStart, Math.max(0, view.length - 1)), true);
  }
  kw.addEventListener('input', () => {
    clearTimeout(refilterTimer); refilterTimer = setTimeout(refilterNow, 250);
  });
  level.addEventListener('change', refilterNow);

  // 滚动：到底=恢复跟随；离开底部=进入回看；顶到头=往回加载更早的一页
  box.addEventListener('scroll', () => {
    if (lock) return;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    if (atBottom && !follow) { goTail(); return; }
    if (!atBottom && follow) { follow = false; updStatus(); return; }
    if (!follow && box.scrollTop <= 1 && winStart > 0) loadOlder();
  });
  function loadOlder() {
    const anchor = view[winStart] ? view[winStart].t : '';
    const start = Math.max(0, winStart - PAGE);
    renderSlice(start, true);
    // 新窗口顶部是更老的行，把视觉锚在"原来第一行"上，滚动不跳
    if (anchor) {
      const idx = Array.prototype.findIndex.call(
        box.children, (c) => c.textContent === anchor);
      if (idx > 0) { lock = true; box.scrollTop = box.children[idx].offsetTop; setTimeout(() => { lock = false; }, 0); }
    }
    updStatus();
  }

  function noteFor() {
    const cur = (pkg.value || '').trim();
    pkgNote.textContent = cur
      ? ('只看 ' + cur + '（不清空就是全机日志）')
      : '不选应用 = 输出整机日志';
  }
  pkg.addEventListener('input', noteFor);
  pkg.addEventListener('change', noteFor);
  noteFor();

  async function loadPkgs() {
    // 连系统应用一起列：日志最常见的排查对象恰恰是系统进程
    pkgNote.textContent = '正在读取应用列表…';
    const list = await loadPkgList(true, true);      // all=true, force=true
    pkgNote.textContent = list.length
      ? ('共 ' + list.length + ' 个应用可选（含系统）')
      : '读不到应用列表（仍可输出全机日志）';
    noteFor();
  }

  function line(text, cls) {
    const d = el('div', { class: cls || '' }, text);
    box.appendChild(d);
    if (box.childNodes.length > 3000) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
    n++;
  }

  function pushLine(t, cls) {
    t = String(t == null ? '' : t);
    if (t === '') return;
    pending.push({ t, lv: lvOf(t), cls: cls || clsOf(t) });
    n++;
    if (!flushTimer) flushTimer = setTimeout(flush, FLUSH_MS);
  }

  const startBtn = el('button', {
    class: 'btn primary', onclick: () => {
      if (es) return;
      n = 0; dropped = 0; rows = []; view = []; pending = []; newSinceLook = 0;
      follow = true; winStart = 0; winLen = 0; box.replaceChildren();
      pushLine('$ adb logcat -v time ' + ((pkg.value || '').trim()
        ? '--pid=$(' + pkg.value.trim() + ')' : '（全机）') + ' …', 'l-dim');
      // 级别/关键字**不再传给服务端**：过滤在本地做，改关键词立即生效
      // （服务端过滤是"启动时定死"的，改一次就得重开流）
      const params = { pkg: (pkg.value || '').trim(), device: devParam() };
      es = sse('/api/capture/logcat/stream', {
        log: (d) => pushLine(d.line),
        open: () => pushLine('· 已连接，开始输出', 'l-dim'),
      }, { ...params, duration: 0 });
      startBtn.disabled = true;
    },
  }, '▶ 开始');

  const stopBtn = el('button', {
    class: 'btn danger', disabled: true, onclick: () => {
      if (es) { es.close(); es = null; }
      pushLine('· 已停止（共 ' + n + ' 行）', 'l-dim');
      startBtn.disabled = false; stopBtn.disabled = true;
    },
  }, '■ 停止');

  // 让 stop 按钮的 disabled 跟随状态
  startBtn.addEventListener('click', () => { stopBtn.disabled = false; });

  host.appendChild(card('日志', 'SSE 实时流 · 不选应用 = 全机日志 · 往上翻自动按页加载更早的',
    el('div', { class: 'row', style: { alignItems: 'center' } },
      el('span', { style: { fontSize: '12px', color: 'var(--dim)' } }, '应用'),
      el('div', { class: 'grow', style: { maxWidth: '420px' } }, pkg),
      el('button', { class: 'btn sm', title: '重新拉取设备上的应用列表', onclick: loadPkgs }, '刷新列表'),
      el('div', { style: { width: '120px' } }, level),
      el('div', { class: 'grow' }, kw),
      startBtn, stopBtn,
      el('button', {
        class: 'btn', onclick: async () => {
          try {
            const r = await api('/api/capture/logcat', {
              params: { pkg: pkg.value || '', device: devParam(), lines: 400 },
            });
            for (const l of (r.lines || [])) pushLine(l);
            pushLine('· 快照 ' + ((r.lines || []).length) + ' 行（往上翻看更早的，每次加载 '
              + PAGE + ' 行）', 'l-dim');
          } catch (e) { pushLine('读取失败：' + e.message, 'l-error'); }
        },
      }, '抓快照'),
      el('button', {
        class: 'btn', title: '只清空界面显示，不影响设备与正在接收的流',
        onclick: () => {
          rows = []; view = []; pending = [];
          if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
          n = 0; dropped = 0; newSinceLook = 0; follow = true; winStart = 0; winLen = 0;
          box.replaceChildren();
          pushLine('· 已清屏（只清显示；日志流还在跑，新行会继续进来）', 'l-dim');
          flush();
          updStatus();
        },
      }, '清屏'),
      el('button', {
        class: 'btn', onclick: async () => {
          try { await api('/api/capture/logcat/clear', { method: 'POST', body: { device: devParam() } }); toast('已清空设备日志缓冲区', 'ok'); }
          catch (e) { toast(e.message, 'bad'); }
        },
      }, '清缓冲')),
    el('div', { class: 'row', style: { marginTop: '2px' } }, pkgNote)));
  host.appendChild(card(null, null, el('div', { class: 'row' }, stat), wrap));
  loadPkgs();
};

VIEWS['device.record'] = async function (host, cap, q) {
  const bitrate = el('select', { style: { width: '110px' } }, ['4M', '8M', '16M', '24M'].map((b) => el('option', { value: b, selected: b === '8M' }, b)));
  const tlimit = el('input', { type: 'number', value: '180', style: { width: '110px' } });
  const out = el('div');
  host.appendChild(card('录屏', 'screenrecord（设备端），可启停',
    el('div', { class: 'row' },
      el('div', { class: 'field' }, el('label', {}, '码率'), bitrate),
      el('div', { class: 'field' }, el('label', {}, '最长（秒）'), tlimit),
      el('button', {
        class: 'btn primary', onclick: async () => {
          out.innerHTML = ''; out.appendChild(spinner());
          try {
            const r = await api('/api/capture/record', {
              method: 'POST', body: { op: 'start', bitrate: bitrate.value, timeLimit: Number(tlimit.value) || 180, device: devParam() },
            });
            out.innerHTML = '';
            out.appendChild(r.ok ? notice('ok', r.detail || '已开始录屏') : notice('bad', r.detail || '失败'));
            if (r.command) out.appendChild(codeBlock(r.command));
          } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
        },
      }, '开始录屏'),
      el('button', {
        class: 'btn danger', onclick: async () => {
          out.innerHTML = ''; out.appendChild(spinner('收尾中…'));
          try {
            const r = await api('/api/capture/record', { method: 'POST', body: { op: 'stop', device: devParam() } });
            out.innerHTML = '';
            out.appendChild(r.ok ? card('已保存', null, kvBlock([['文件', r.path]], ['文件']))
              : notice('bad', r.detail || '失败'));
          } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
        },
      }, '停止并拉回'))));
  host.appendChild(out);
};

VIEWS['device.mirror'] = async function (host) {
  const out = el('div');
  host.appendChild(card('实时镜像', 'scrcpy（可选依赖，走了就是走了）',
    el('div', { class: 'row' },
      el('button', {
        class: 'btn primary', onclick: async () => {
          out.innerHTML = ''; out.appendChild(spinner());
          try {
            const r = await api('/api/capture/mirror', { method: 'POST', body: { device: devParam() } });
            out.innerHTML = '';
            out.appendChild(r.ok ? notice('ok', r.detail || '已启动 scrcpy（窗口应已弹出）') : notice('bad', r.detail || '失败'));
            if (r.command) out.appendChild(codeBlock(r.command));
          } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
        },
      }, '启动镜像'),
      el('button', {
        class: 'btn', onclick: async () => {
          out.innerHTML = ''; out.appendChild(spinner());
          try {
            const r = await api('/api/capture/mirror', { method: 'POST', body: { device: devParam(), record: 'auto' } });
            out.innerHTML = '';
            out.appendChild(r.ok ? notice('ok', r.detail || '已启动（并同时录制）') : notice('bad', r.detail || '失败'));
          } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
        },
      }, '镜像 + 录制'))));
  host.appendChild(out);
};

VIEWS['device.artifacts'] = async function (host) {
  const out = el('div');
  host.appendChild(card('产物管理', null, el('div', { class: 'row' },
    el('button', { class: 'btn', onclick: () => load() }, '刷新'),
    el('button', {
      class: 'btn', onclick: async () => {
        const d = await ask({ title: '清理旧产物', message: '删除 N 天前的截图/录屏', value: '7', action: '清理' });
        if (!d) return;
        try { const r = await api('/api/capture/artifacts/cleanup', { method: 'POST', body: { days: Number(d) || 7 } }); toast(r.detail, 'ok'); load(); }
        catch (e) { toast(e.message, 'bad'); }
      },
    }, '清理 7 天前'))));
  host.appendChild(out);

  async function load() {
    out.innerHTML = ''; out.appendChild(spinner());
    try {
      const r = await api('/api/capture/artifacts', { params: { limit: 200 } });
      out.innerHTML = '';
      const items = r.artifacts || [];
      out.appendChild(card(`共 ${items.length} 个 · ${fmtBytes((r.usage || {}).bytes)}`, (r.usage || {}).dir,
        items.length ? el('div', { class: 'gallery' }, ...items.map((a) => el('div', { class: 'shot' },
          a.kind === '录屏'
            ? el('video', { src: a.url, controls: '' })
            : el('img', { src: a.url, loading: 'lazy' }),
          el('div', { class: 's-meta' },
            el('div', { class: 's-name' }, a.name),
            el('div', {}, a.time + ' · ' + fmtBytes(a.bytes))),
          el('div', { class: 's-act' },
            el('a', { class: 'btn sm', href: a.url, download: '', target: '_blank' }, '下载'),
            el('button', { class: 'btn sm', onclick: () => copyText(a.path) }, '路径'),
            el('button', {
              class: 'btn sm danger', onclick: async () => {
                try { await api('/api/capture/artifacts/delete', { method: 'POST', body: { path: a.path } }); load(); }
                catch (e) { toast(e.message, 'bad'); }
              },
            }, '删除'))))) : emptyState('🗃', '还没有截图/录屏产物')));
    } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
  }
  load();
};

/* ------------------------------------------------ 安装 */

/* 「安装」合并页的**预检**那一格 */
const installPrecheckTab = async function (host, cap, q) {
  const apk = el('input', { type: 'text', class: 'mono', placeholder: 'D:\\app.apk', value: q.apk || '' });
  const deep = el('input', { type: 'checkbox' });
  const out = el('div');
  const run = async () => {
    const a = apk.value.trim();
    if (!a) { toast('请先选一个 APK', 'bad'); return; }
    out.innerHTML = ''; out.appendChild(spinner('预检中…'));
    try {
      const r = await api('/api/install/precheck', {
        method: 'POST', body: { apk: a, device: devParam(), deepSign: deep.checked },
      });
      out.innerHTML = '';
      const items = r.items || [];
      out.appendChild(card('预检结果', r.summary ? `${r.summary.pass ?? 0} 通过 · ${r.summary.warn ?? 0} 警告 · ${r.summary.fail ?? 0} 阻塞` : null,
        dataTable([
          { key: 'level', label: '', width: '46px', render: (i) => ({ pass: tag('✔', 'ok'), warn: tag('!', 'warn'), fail: tag('✖', 'bad'), info: tag('·') }[i.level] || tag('·')) },
          { key: 'title', label: '项', width: '150px' },
          { key: 'detail', label: '说明' },
          { key: 'code', label: '错误码', mono: true, width: '210px' },
        ], items, { emptyText: '没有检查项' })));
      out.appendChild(el('div', { class: 'row', style: { marginTop: '12px' } },
        el('button', {
          class: 'btn primary',
          onclick: () => goTab('device.install.one', 'install', { apk: a }),
        }, '继续安装')));
    } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
  };

  host.appendChild(card('安装前预检', 'minSdk / ABI / 签名 / 版本升降 —— 装之前先把坑找出来',
    el('div', { class: 'row' },
      el('div', { class: 'grow' }, apk),
      el('button', {
        class: 'btn', onclick: async () => {
          const r = await pickApk();
          if (r && r.path) { apk.value = r.path; toast('已选中 ' + (r.name || ''), 'ok'); }
        },
      }, '浏览…'),
      el('label', { class: 'check' }, deep, '深度（比对设备上的签名，慢）'),
      el('button', { class: 'btn primary', onclick: run }, '开始预检'))));
  host.appendChild(out);
  if (q.apk) await run();
};

VIEWS['device.install.precheck'] = installPrecheckTab;

/* 「安装」合并页的**安装**那一格 */
const installOwnTab = async function (host, cap, q) {
  const apk = el('textarea', { rows: 3, placeholder: '一行一个 APK 路径' }, q.apk || '');
  const noR = el('input', { type: 'checkbox' });
  const dg = el('input', { type: 'checkbox' });
  const tt = el('input', { type: 'checkbox' });
  const gr = el('input', { type: 'checkbox' });
  const logBox = el('div', { class: 'logbox' });
  const bar = el('div', { class: 'bar' }, el('i', { style: { width: '0%' } }));
  const status = el('div', { style: { fontSize: '12px', color: 'var(--dim)', marginTop: '6px' } }, '待命');
  let es = null;

  const apksOf = () => apk.value.split('\n').map((s) => s.trim()).filter(Boolean);

  function log(msg, cls) {
    logBox.appendChild(el('div', { class: cls || '' }, msg));
    logBox.scrollTop = logBox.scrollHeight;
  }

  host.appendChild(card('安装', '进度实时回传；失败自动翻成中文并给补救建议',
    el('div', { class: 'field' }, el('label', {}, 'APK 路径（一行一个）'), apk),
    // 手打路径是这轮要消掉的交互：原生文件对话框直接给真路径，选多次就叠加
    el('div', { class: 'row', style: { marginTop: '-4px', marginBottom: '10px' } },
      el('button', {
        class: 'btn sm', onclick: async () => {
          const r = await pickApk();
          if (!r || !r.path) return;
          const cur = apksOf();
          if (cur.indexOf(r.path) >= 0) { toast('已经在列表里了'); return; }
          apk.value = cur.concat([r.path]).join('\n');
          toast('已加入 ' + (r.name || ''), 'ok');
        },
      }, '+ 浏览…加入一个'),
      el('button', {
        class: 'btn sm', title: '把上次导出/用过的目录里的 APK 一网打尽',
        onclick: async () => {
          const d = await pickLocalDir({ title: '选一个目录，把里面的 APK 都加进来' });
          if (!d) return;
          try {
            const r = await api('/api/apk/scan', { method: 'POST', body: { dir: d, limit: 300 } });
            const found = (r.items || []).map((x) => x.path);
            if (!found.length) { toast('这个目录里没找到 APK', 'warn'); return; }
            const merged = Array.from(new Set(apksOf().concat(found)));
            apk.value = merged.join('\n');
            toast('加入 ' + found.length + ' 个（去重后共 ' + merged.length + ' 个）', 'ok');
          } catch (e) { toast(e.message, 'bad'); }
        },
      }, '+ 从目录批量加入'),
      el('button', { class: 'btn sm', onclick: () => { apk.value = ''; } }, '清空')),
    el('div', { class: 'row', style: { marginBottom: '10px' } },
      el('label', { class: 'check' }, noR, '不覆盖安装（不加 -r）'),
      el('label', { class: 'check' }, dg, '允许降级'),
      el('label', { class: 'check' }, tt, '允许 test 包'),
      el('label', { class: 'check' }, gr, '自动授予运行权限')),
    el('div', { class: 'row' },
      el('button', {
        class: 'btn primary', onclick: async () => {
          const apks = apksOf();
          if (!apks.length) { toast('请填至少一个 APK 路径', 'bad'); return; }
          logBox.innerHTML = ''; es && es.close();
          bar.className = 'bar'; $('i', bar).style.width = '0%';
          status.textContent = '准备中…';
          log('$ apk install one ' + apks.join(' '), 'l-dim');
          try {
            const r = await api('/api/install', {
              method: 'POST',
              body: {
                apks, device: devParam(),
                noReinstall: noR.checked, downgrade: dg.checked,
                test: tt.checked, grant: gr.checked,
              },
            });
            const tid = r.taskId;
            log('· 任务 ' + tid, 'l-dim');
            es = sse(`/api/tasks/${tid}/events`, {
              progress: (d) => {
                status.textContent = d.msg || d.stage || '';
                if (d.pct != null) $('i', bar).style.width = d.pct + '%';
                log('  ' + (d.msg || ''), 'l-ok');
              },
              log: (d) => log(d.msg || '', d.level === 'error' ? 'l-error' : (d.level === 'warn' ? 'l-warn' : '')),
              result: (res) => {
                $('i', bar).style.width = '100%';
                bar.className = 'bar ' + (res.ok ? 'done' : 'fail');
                status.textContent = res.detail || '';
                for (const r2 of (res.results || [])) {
                  log((r2.ok ? '✔ ' : '✖ ') + (r2.file || '') + '  ' + (r2.detail || ''), r2.ok ? 'l-ok' : 'l-error');
                  if (!r2.ok && r2.explain) {
                    log('   错误码：' + r2.explain.code, 'l-error');
                    log('   含义：' + r2.explain.meaning, 'l-warn');
                    log('   原因：' + r2.explain.cause, 'l-warn');
                    for (const f of (r2.explain.fixes || [])) log('   怎么办：' + f, 'l-ok');
                  }
                }
              },
              end: () => { es && es.close(); es = null; loadDevices(true); },
              error: () => { log('· 事件流断开', 'l-warn'); },
            });
          } catch (e) { log('✖ ' + e.message, 'l-error'); status.textContent = '失败'; bar.className = 'bar fail'; }
        },
      }, '开始安装'),
      el('button', {
        class: 'btn', onclick: () => {
          const a = apksOf()[0];
          goTab('device.install.one', 'precheck', a ? { apk: a } : {});
        },
      }, '先去预检'),
      el('button', { class: 'btn', onclick: () => { logBox.innerHTML = ''; } }, '清屏'))));
  host.appendChild(card('进度', null, bar, status, el('div', { style: { height: '10px' } }), logBox));
};
VIEWS['device.install.one'] = mergedPage('device.install.one', installOwnTab);

VIEWS['device.install.errors'] = async function (host) {
  const out = el('div');
  host.appendChild(card('安装失败诊断表', 'INSTALL_FAILED_* 的常见码'));
  host.appendChild(out);
  out.appendChild(spinner());
  try {
    const r = await api('/api/install/errors');
    out.innerHTML = '';
    out.appendChild(card(`共 ${(r.errors || []).length} 条`, null, dataTable([
      { key: 'code', label: '错误码', mono: true, width: '290px' },
      { key: 'meaning', label: '含义', width: '190px' },
      { key: 'cause', label: '常见原因' },
      {
        key: '_f', label: '怎么办', render: (e) => el('div', {}, ...(e.fixes || []).map((f) => el('div', { style: { fontSize: '12px' } }, '· ' + f))),
      },
    ], r.errors || [], { filter: true, filterPlaceholder: '搜错误码或含义…', emptyText: '没有词表' })));
  } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
};

/* ------------------------------------------------ 文件 */

VIEWS['device.files'] = async function (host, cap, q) {
  // 像文件管理器那样用：点目录展开下一层（懒加载），双击"进入"（换根）。
  // 之前只有一张平表、进目录得手改路径框 —— 那不像浏览，像填表。
  const pathInp = el('input', { type: 'text', class: 'mono', value: q.path || '/sdcard' });
  const crumbBox = el('div', { class: 'crumbs' });
  const aliasBox = el('div');
  const treeBox = el('div', { class: 'ftree-wrap' });

  let state = { path: '', parent: '/' };
  let want = q.path || '/sdcard';

  // ---------------------------------------------------------- 树

  function makeNode(ent, kids) {
    const node = el('div', { class: 'ft-node' });
    const carets = el('span', { class: 'ft-caret' + (ent.dir ? '' : ' leaf') }, ent.dir ? '▸' : '·');
    const nameCls = 'ft-name' + (ent.link ? ' is-link' : '');
    // 缩进交给 CSS 的 .ft-kids（每层 15px）；行内再写一遍会缩成两倍
    const name = el('div', { class: nameCls },
      carets,
      el('span', { class: 'ft-ico' }, ent.dir ? '📁' : '📄'),
      el('span', { class: 'ft-txt', title: ent.path }, ent.name),
      ent.link && ent.linkTarget
        ? el('span', { class: 'ft-link', title: '符号链接 → ' + ent.linkTarget }, '→ ' + ent.linkTarget)
        : null);

    const row = el('div', { class: 'ft-row', title: ent.path }, name,
      el('div', { class: 'ft-size' }, ent.dir || ent.size == null ? '' : fmtBytes(ent.size)),
      el('div', { class: 'ft-perm mono' }, ent.perm || ''),
      el('div', { class: 'ft-acts' },
        ent.dir ? el('button', {
          class: 'btn sm', title: '把这一层作为根，重新列',
          onclick: (e) => { e.stopPropagation(); goPath(ent.path); },
        }, '进入') : null,
        ent.dir ? null : el('button', {
          class: 'btn sm', title: '拉到本机',
          onclick: async (e) => {
            e.stopPropagation();
            const dst = await ask({ title: '拉回本机', message: '目标路径', value: 'D:\\' + ent.name });
            if (!dst) return;
            try {
              const r2 = await api('/api/files/pull', {
                method: 'POST', body: { src: ent.path, dst, device: devParam() },
              });
              toast(r2.ok ? '已拉到 ' + r2.dst : (r2.detail || '失败'), r2.ok ? 'ok' : 'bad');
            } catch (er) { toast(er.message, 'bad'); }
          },
        }, '拉回'),
        el('button', {
          class: 'btn sm', title: '复制完整路径',
          onclick: (e) => { e.stopPropagation(); copyText(ent.path); },
        }, '复制')));

    node.appendChild(row);
    const kidBox = el('div', { class: 'ft-kids' });
    node.appendChild(kidBox);
    kids.appendChild(node);
    return { node, row, kidBox, caret: carets };
  }

  async function expandDir(ent, ref, depth) {
    const open = ref.node.dataset.open === '1';
    if (open) {
      ref.node.dataset.open = '0';
      ref.kidBox.style.display = 'none';
      ref.caret.textContent = '▸';
      return;
    }
    if (ref.node.dataset.loaded === '1') {
      ref.node.dataset.open = '1';
      ref.kidBox.style.display = '';
      ref.caret.textContent = '▾';
      return;
    }
    ref.caret.textContent = '…';
    ref.kidBox.style.display = '';
    ref.kidBox.innerHTML = '';
    ref.kidBox.appendChild(spinner('读取 ' + ent.name + ' …'));
    let r = null;
    try {
      r = await api('/api/files/ls', { params: { path: ent.path, device: devParam() } });
    } catch (e) {
      ref.kidBox.innerHTML = '';
      ref.kidBox.appendChild(notice('bad', ent.name + '：' + e.message));
      ref.caret.textContent = '▸';
      return;
    }
    ref.kidBox.innerHTML = '';
    if (!r.ok) {
      ref.kidBox.appendChild(notice('bad', (r.detail || '读取失败') + (r.hint ? '（' + r.hint + '）' : '')));
      ref.caret.textContent = '▸';
      return;
    }
    if (!r.entries.length) {
      ref.kidBox.appendChild(el('div', { class: 'ft-empty' }, '（空目录）'));
    }
    for (const e of r.entries) {
      const sub = makeNode(e, ref.kidBox);
      bindRow(e, sub, depth + 1);
    }
    ref.node.dataset.loaded = '1';
    ref.node.dataset.open = '1';
    ref.caret.textContent = '▾';
    if (r.warning) {
      ref.kidBox.appendChild(el('div', { class: 'ft-empty' }, '⚠ ' + r.warning));
    }
  }

  function bindRow(ent, ref, depth) {
    const toggle = (e) => {
      if (e) e.stopPropagation();
      if (!ent.dir) { toast('这是文件：' + ent.path + '（可用「拉回」取到本机）'); return; }
      expandDir(ent, ref, depth);
    };
    ref.caret.addEventListener('click', toggle);
    ref.row.querySelector('.ft-name').addEventListener('click', toggle);
    ref.row.addEventListener('dblclick', () => { if (ent.dir) goPath(ent.path); });
  }

  // ---------------------------------------------------------- 顶部

  async function goPath(p) {
    want = p || pathInp.value.trim();
    await ls();
  }

  async function ls() {
    treeBox.innerHTML = '';
    treeBox.appendChild(spinner('读取 ' + want + ' …'));
    aliasBox.innerHTML = '';
    let r = null;
    try {
      r = await api('/api/files/ls', { params: { path: want, device: devParam() } });
    } catch (e) {
      treeBox.innerHTML = '';
      treeBox.appendChild(notice('bad', e.message));
      return;
    }
    treeBox.innerHTML = '';
    if (!r.ok) {
      treeBox.appendChild(notice('bad', (r.detail || '列目录失败')
        + (r.hint ? '（' + r.hint + '）' : '')));
      return;
    }
    state = { path: r.path, parent: r.parent || '/' };
    pathInp.value = r.path;
    paintCrumbs(r);

    if (r.isFile) {
      const f = r.file || {};
      treeBox.appendChild(notice('warn', r.detail + '：' + r.path
        + (f.linkTarget ? ' → ' + f.linkTarget : '')));
      if (r.hint) treeBox.appendChild(notice('ok', r.hint));
      const back = el('button', { class: 'btn', onclick: () => goPath(r.parent) }, '回到上级目录');
      treeBox.appendChild(el('div', { class: 'row', style: { marginTop: '8px' } }, back));
      return;
    }

    if (r.resolved && r.aliasNote) aliasBox.appendChild(notice('ok', r.aliasNote));
    if (r.warning) aliasBox.appendChild(notice('warn', r.warning + (r.warningHint ? '（' + r.warningHint + '）' : '')));

    const kids = el('div', {});
    treeBox.appendChild(el('div', { class: 'ft-head' },
      el('div', { class: 'ft-name' }, '名称'),
      el('div', { class: 'ft-size' }, '大小'),
      el('div', { class: 'ft-perm' }, '权限'),
      el('div', { class: 'ft-acts' }, r.detail)));
    treeBox.appendChild(kids);

    if (!r.entries.length) {
      treeBox.appendChild(el('div', { class: 'ft-empty' }, '（空目录）'));
      return;
    }
    for (const e of r.entries) {
      const ref = makeNode(e, kids);
      bindRow(e, ref, 0);
    }
  }

  function paintCrumbs(r) {
    crumbBox.innerHTML = '';
    const crumbs = r.crumbs || [];
    crumbs.forEach((c, i) => {
      crumbBox.appendChild(el('span', {
        class: 'crumb' + (i === crumbs.length - 1 ? ' cur' : ''),
        onclick: () => goPath(c.path),
      }, c.name));
      if (i < crumbs.length - 1) crumbBox.appendChild(el('span', { class: 'crumb-sep' }, '›'));
    });
    if (r.resolved && r.resolved !== r.path) {
      crumbBox.appendChild(el('span', {
        class: 'crumb-real', title: 'readlink -f 出来的规范路径（内容完全相同）',
        onclick: () => goPath(r.resolved),
      }, '≡ ' + r.resolved));
    }
  }

  pathInp.addEventListener('keydown', (e) => { if (e.key === 'Enter') goPath(); });
  host.appendChild(card('设备文件', '点目录展开下一层 · 双击进入 · 面包屑可点',
    el('div', { class: 'row' },
      el('div', { class: 'grow' }, pathInp),
      el('button', { class: 'btn primary', onclick: () => goPath() }, '列目录'),
      el('button', { class: 'btn', title: '回到上级目录', onclick: () => goPath(state.parent) }, '↑ 上级'),
      el('button', { class: 'btn', onclick: () => ls() }, '刷新')),
    el('div', { class: 'row', style: { marginTop: '7px', marginBottom: '2px' } },
      el('button', { class: 'btn sm', onclick: () => goPath('/sdcard') }, '/sdcard'),
      el('button', { class: 'btn sm', onclick: () => goPath('/storage/emulated/0') }, '/storage/emulated/0'),
      el('button', { class: 'btn sm', onclick: () => goPath('/data/local/tmp') }, '/data/local/tmp'),
      el('button', { class: 'btn sm', onclick: () => goPath('/') }, '/'),
      el('span', { class: 'drop-tip', title: '/sdcard 是符号链接，真身是 /storage/emulated/0；两个都能用' }, '/sdcard 是别名')),
    crumbBox,
    aliasBox));
  host.appendChild(card(null, null, treeBox));
  ls();
};

/* ------------------------------------------------ 系统 */

VIEWS['system.menu'] = async function (host) {
  const out = el('div');
  host.appendChild(card('右键菜单注入', 'Windows 走 HKCU（不需要管理员）；macOS 走 Automator；Linux 走 .desktop',
    el('div', { class: 'row' },
      el('button', {
        class: 'btn primary', onclick: async () => {
          try {
            const r = await api('/api/menu/apply', { method: 'POST', body: { op: 'install' } });
            toast(r.ok ? '已注册' : (r.error || '失败'), r.ok ? 'ok' : 'bad');
            load();
          } catch (e) { toast(e.message, 'bad'); }
        },
      }, '安装菜单'),
      el('button', {
        class: 'btn danger', onclick: async () => {
          try {
            const r = await api('/api/menu/apply', { method: 'POST', body: { op: 'uninstall' } });
            toast(r.ok ? '已卸载' : (r.error || '失败'), r.ok ? 'ok' : 'bad');
            load();
          } catch (e) { toast(e.message, 'bad'); }
        },
      }, '卸载菜单'))));
  host.appendChild(out);

  async function load() {
    out.innerHTML = ''; out.appendChild(spinner());
    try {
      const st = await api('/api/menu/state');
      out.innerHTML = '';
      if (!st.supported) { out.appendChild(notice('warn', '此平台不支持菜单注入')); return; }
      out.appendChild(card('菜单项', st.menuRoot || null, dataTable([
        { key: 'label', label: '菜单项' },
        { key: 'registered', label: '状态', width: '90px', render: (i) => i.registered ? tag('已注册', 'ok') : tag('未注册') },
        { key: 'command', label: '命令', mono: true },
      ], st.items || [], { emptyText: '没有菜单项' })));
      if (st.scripts) {
        out.appendChild(card('脚本文件', null, dataTable([
          { key: 'name', label: '文件', mono: true },
          { key: 'desc', label: '用途' },
          { key: 'exists', label: '存在', width: '70px', render: (s) => s.exists ? tag('✔', 'ok') : tag('缺失', 'bad') },
        ], st.scripts, { emptyText: '无' })));
      }
      if (st.note) out.appendChild(notice('warn', st.note));
    } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
  }
  load();
};

VIEWS['system.doctor'] = async function (host) {
  const out = el('div');
  host.appendChild(card('环境自检', null, el('button', { class: 'btn primary', onclick: () => load() }, '重新检测')));
  host.appendChild(out);
  async function load() {
    out.innerHTML = ''; out.appendChild(spinner());
    try {
      const d = await api('/api/sys/doctor');
      out.innerHTML = '';
      out.appendChild(card('工具', `Python ${d.python.version} · ${d.python.platform}`, dataTable([
        { key: 'name', label: '工具', width: '110px' },
        { key: 'required', label: '必需', width: '70px', render: (t) => t.required ? tag('必需', 'warn') : tag('可选') },
        { key: 'path', label: '路径', mono: true },
        { key: 'version', label: '版本', render: (t) => (t.version || '').split('\n')[0] },
        { key: 'hint', label: '说明', render: (t) => t.path ? '' : el('span', { style: { color: 'var(--warn)' } }, t.hint) },
      ], d.tools || [], { emptyText: '-' })));
      out.appendChild(card('目录', null, kvBlock([
        ['项目', d.dirs.project], ['配置', d.dirs.config],
        ['产物', d.dirs.output], ['缓存', d.dirs.cache],
      ], ['项目', '配置', '产物', '缓存'])));
    } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
  }
  load();
};

VIEWS['system.config'] = async function (host) {
  const out = el('div');
  out.appendChild(spinner());
  host.appendChild(out);
  try {
    const r = await api('/api/sys/config');
    out.innerHTML = '';
    const vals = r.values || {};
    const inputs = {};
    const form = el('div');
    for (const [k, v] of Object.entries(vals)) {
      const inp = el('input', { type: 'text', class: 'mono', value: v == null ? '' : String(v) });
      inputs[k] = inp;
      form.appendChild(el('div', { class: 'field' }, el('label', {}, k), inp));
    }
    out.appendChild(card('配置', r.path, form,
      el('div', { class: 'row' },
        el('button', {
          class: 'btn primary', onclick: async () => {
            const patch = {};
            for (const [k, inp] of Object.entries(inputs)) patch[k] = inp.value;
            try { const r2 = await api('/api/sys/config', { method: 'POST', body: { values: patch } }); toast('已保存', 'ok'); out.innerHTML = ''; VIEWS['system.config'](host); }
            catch (e) { toast(e.message, 'bad'); }
          },
        }, '保存'),
        el('button', {
          class: 'btn', onclick: async () => {
            try {
              const r2 = await api('/api/sys/config', { method: 'POST', body: { values: {} } });
              toast('已提交（空值=恢复默认）', 'ok');
            } catch (e) { toast(e.message, 'bad'); }
          },
        }, '清空全部'))));
  } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
};

VIEWS['system.server'] = async function (host) {
  const out = el('div');
  host.appendChild(card('主控台', null, el('div', { class: 'row' },
    el('button', { class: 'btn', onclick: () => load() }, '刷新状态'),
    el('button', {
      class: 'btn danger', onclick: async () => {
        const ok = await confirmTyping({
          title: '停止主控台',
          message: '主控台进程会被结束，本页面随即失联。重新启动：python apktool.py web start',
          expect: 'shutdown', action: '停止',
        });
        if (!ok) return;
        try { await api('/api/sys/shutdown', { method: 'POST', body: {} }); } catch (_) { }
        setTimeout(() => {
          document.body.innerHTML =
            '<div style="padding:40px;font-family:sans-serif;color:#667085">'
            + '主控台已停止。重新启动：<code>python apktool.py web start</code></div>';
        }, 900);
      },
    }, '停止主控台'))));
  host.appendChild(out);
  async function load() {
    out.innerHTML = ''; out.appendChild(spinner());
    try {
      const d = await api('/api/health');
      out.innerHTML = '';
      out.appendChild(card('运行中', null, kvBlock([
        ['版本', d.version], ['PID', d.pid],
        ['地址', location.origin],
        ['启动时间', new Date(d.time * 1000).toLocaleString()],
      ])));
    } catch (e) { out.innerHTML = ''; out.appendChild(notice('bad', e.message)); }
  }
  load();
};

/* ------------------------------------------------ 兜底 */

function viewGeneric(host, cap) {
  host.appendChild(card(cap.title, cap.description,
    kvBlock([
      ['能力 ID', cap.id],
      ['分组', cap.group],
      ['风险', cap.riskText],
      ['需要设备', cap.needsDevice ? '是' : '否'],
      ['渲染策略', cap.renderer],
      ['CLI', cap.cli],
      ['API', cap.api],
    ], ['能力 ID', 'CLI', 'API']),
    notice('warn', '这个能力还没有专属界面，但 CLI 与 API 都已可用。')));
}

/* ================================================================ 顶栏交互 */

function bindTopbar() {
  $('#dev-pick').addEventListener('change', async (e) => {
    saveDevice(e.target.value);
    toast('已切换设备');
    await loadDevices(true);
    render();
  });
  $('#dev-refresh').addEventListener('click', async () => {
    await loadDevices();
    render();
  });
  $('#btn-doctor').addEventListener('click', () => go('system.doctor'));
  $('#btn-apk-open').addEventListener('click', async () => {
    const r = await pickApk();
    if (!r || !r.path) return;
    toast('已选中 ' + (r.name || '') + ' · ' + fmtBytes(r.bytes || 0), 'ok');
    go('local.parse', { path: r.path });
  });
  $('#btn-shutdown').addEventListener('click', async () => {
    const s = await ask({ title: '停止主控台', message: '确定要停掉主控台进程吗？', placeholder: 'yes', action: '停止' });
    if (!s) return;
    try {
      await fetch('/api/sys/shutdown', { method: 'POST' });
    } catch (_) { }
    document.body.innerHTML = '<div style="padding:40px;font-family:sans-serif;color:#667085">'
      + '主控台已停止。重新启动：<code>python apktool.py web start</code></div>';
  });
}

/* 右键脚本推来的卡片 */
function pollPushes() {
  setInterval(async () => {
    try {
      const r = await api('/api/popup/pending', {
        // visible 是给服务端的心跳判据：右键脚本靠它决定"卡片推过来有没有人看得见"。
        // 不能只报"我在轮询"——被切走的标签页会被浏览器节流到 30~60 秒一次，
        // 偶尔也来一次，于是卡片被推进一个没人看的页面。
        params: {
          since: S.pushSince,
          visible: document.visibilityState === 'visible' ? 1 : 0,
        },
      });
      S.pushSince = r.now || S.pushSince;
      for (const p of (r.items || [])) {
        if (p.kind === 'apk-info' && p.data && p.data.path) {
          toast('收到推送：' + (p.data.package || p.data.fileName || ''), 'ok');
          go('local.parse', { path: p.data.path });
        } else {
          // 认不出来的推送必须**说出来**。以前这里直接跳过，于是后端
          // 字段名一改（比如 path 被写成 filePath），卡片就静静消失，
          // 前端一片安静、后端一片安静，只剩用户"点了没反应"。
          toast('收到一条无法识别的推送，已忽略', 'bad');
        }
      }
    } catch (_) { }
  }, 2500);
}

/* ================================================================ 启动 */

(async function boot() {
  try {
    const h = await api('/api/health');
    $('#ver').textContent = 'v' + (h.version || '?');
  } catch (e) {
    $('#ver').textContent = '（连不上后端）';
  }
  try {
    S.reg = await api('/api/capabilities');
  } catch (e) {
    S.reg = { groups: [], capabilities: [] };
    toast('能力注册表读取失败：' + e.message, 'bad');
  }
  bindTopbar();
  bindGlobalDrop();
  buildNav();
  probePickBackend();       // 后台探测原生对话框能力，别阻塞首屏
  window.addEventListener('hashchange', render);
  await loadDevices(true);
  if (!location.hash) go('local.parse');
  else render();
  pollPushes();
})();
