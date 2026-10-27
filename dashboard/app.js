/* 蜂巢 Hive 多机任务看板 —— 原生 ES2020，零依赖。
 * 数据只来自同源的 /api/status（或拖拽 / 选择本地 JSON 作为降级）。
 */
(function () {
  'use strict';

  // ---------- 常量 ----------
  var REFRESH_MS = 15000;   // 自动刷新间隔
  var TICK_MS = 30000;      // 相对时间（已领取 / 租约剩余）的自行重算间隔
  var STORE_THEME = 'hive.theme';
  var STORE_AUTO = 'hive.autoRefresh';
  var RECENT_WINDOW_MS = 14 * 24 * 3600 * 1000;  // 「最近完成」窗口：完成于近 14 天
  var MAX_RECENT = 15;                            // 「最近完成」最多展示条数，超出留在「已关闭」

  // 六列，从左到右；判定顺序：已关闭 → 异常 → 待合并 → 进行中 → 待领取
  // recent（最近完成）从 closed 里捞出「近 14 天完成」的卡，默认展开，不用翻归档
  var COLUMNS = [
    { key: 'ready', title: '待领取', hint: 'state = ready 且无人抢锁' },
    { key: 'running', title: '进行中', hint: '已抢锁 / assigned / running，暂无结果' },
    { key: 'merging', title: '待合并', hint: 'result_status = done，state 还不是 done' },
    { key: 'abnormal', title: '异常', hint: 'failed / blocked / 租约过期' },
    { key: 'recent', title: '最近完成', hint: 'state = done 且完成于近 14 天（按完成时间倒序）' },
    { key: 'closed', title: '已关闭', hint: 'state = done 且完成超过 14 天（默认折叠）', collapsible: true }
  ];
  var COL_TITLE = {};
  COLUMNS.forEach(function (c) { COL_TITLE[c.key] = c.title; });

  var dtf = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'medium', hour12: false });
  var rtf = new Intl.RelativeTimeFormat('zh-CN', { numeric: 'auto' });

  // ---------- 状态 ----------
  var state = {
    tasks: [],        // 归一化后的全量任务
    filtered: [],     // 经过筛选后的任务
    repo: '',
    owner: '',
    query: '',
    autoRefresh: true,
    closedOpen: false,
    loading: true,
    error: '',
    lastUpdated: null,
    source: '/api/status',
    inflight: false
  };

  var el = {};        // 缓存的 DOM 引用
  var cols = {};      // key -> { root, count, body, empty, toggle }
  var lastFocus = null;
  var drawerId = null;

  // ---------- 小工具 ----------
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function storeGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function storeSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 隐私模式下忽略 */ } }

  function parseTime(iso) {
    if (!iso) return null;
    var t = Date.parse(iso);
    return isNaN(t) ? null : t;
  }

  /** 毫秒 → 「8 分钟」/「2 小时 52 分钟」，永不返回负数文本 */
  function humanDuration(ms) {
    var min = Math.max(0, Math.round(Math.abs(ms) / 60000));
    if (min < 1) return '不到 1 分钟';
    var h = Math.floor(min / 60), m = min % 60;
    if (h === 0) return m + ' 分钟';
    if (m === 0) return h + ' 小时';
    return h + ' 小时 ' + m + ' 分钟';
  }

  /** 秒 → 「32 秒」/「13 分 32 秒」 */
  function formatDuration(s) {
    if (!s) return '';
    if (s < 60) return Math.round(s) + ' 秒';
    var m = Math.floor(s / 60), r = Math.round(s % 60);
    return r ? m + ' 分 ' + r + ' 秒' : m + ' 分';
  }

  /** 相对时间：「刚刚」/「3 分钟前」；兼容 Date 对象或数字时间戳（毫秒） */
  function relTime(date) {
    if (date == null) return '';
    var ms = (date instanceof Date) ? date.getTime() : Number(date);
    if (!isFinite(ms)) return '';
    var diff = (ms - Date.now()) / 1000;
    var abs = Math.abs(diff);
    if (abs < 45) return rtf.format(Math.round(diff), 'second');
    if (abs < 3600) return rtf.format(Math.round(diff / 60), 'minute');
    if (abs < 86400) return rtf.format(Math.round(diff / 3600), 'hour');
    return rtf.format(Math.round(diff / 86400), 'day');
  }

  // ---------- 数据归一化与判定 ----------
  function normalize(t) {
    t = t || {};
    return {
      id: t.id || '',
      title: t.title || '',
      repo: t.repo || '',
      priority: t.priority || '',
      state: t.state || '',
      owner: t.owner || '',
      claim_agent: t.claim_agent || '',
      claimed_at: t.claimed_at || '',
      lease_until: t.lease_until || '',
      stale: !!t.stale,
      depends_on: Array.isArray(t.depends_on) ? t.depends_on.slice() : [],
      result_status: t.result_status || '',
      finished_at: t.finished_at || '',
      duration_s: Number(t.duration_s) || 0,
      head: t.head || '',
      verify: Array.isArray(t.verify) ? t.verify.slice() : [],
      result_summary: t.result_summary || '',
      summary: t.summary || ''
    };
  }

  function ownerOf(t) { return t.owner || t.claim_agent || ''; }

  /** 租约过期：服务端判定为 true，或租约已到点且任务还没有结果 */
  function isStale(t) {
    if (t.stale) return true;
    var lease = parseTime(t.lease_until);
    if (lease === null || t.result_status) return false;
    return lease < Date.now();
  }

  /** 列的单向判定，一张卡只落一列 */
  function classify(t) {
    if (t.state === 'done') {
      // 近 14 天完成的归「最近完成」，更早的落到「已关闭」归档；无完成时间一律进归档
      var fin = parseTime(t.finished_at);
      if (fin !== null && (Date.now() - fin) <= RECENT_WINDOW_MS) return 'recent';
      return 'closed';
    }
    if (t.result_status === 'failed' || t.state === 'blocked' || isStale(t)) return 'abnormal';
    if (t.result_status === 'done') return 'merging';
    if (t.claim_agent || t.state === 'assigned' || t.state === 'running') return 'running';
    return 'ready';
  }

  /** 「最近完成」列排序：按完成时间倒序（最近的在前），无完成时间沉底，再按 id */
  function sortRecent(a, b) {
    var ta = parseTime(a.finished_at), tb = parseTime(b.finished_at);
    if (ta === null && tb === null) return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0);
    if (ta === null) return 1;
    if (tb === null) return -1;
    return tb - ta;
  }

  /** P1 优先，其次按 id */
  function sortTasks(a, b) {
    var pa = a.priority || 'P9', pb = b.priority || 'P9';
    if (pa !== pb) return pa < pb ? -1 : 1;
    return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0);
  }

  // ---------- DOM 构建 ----------
  function cacheEls() {
    el.stats = document.getElementById('stats');
    el.meta = document.getElementById('meta');
    el.source = document.getElementById('source');
    el.banner = document.getElementById('banner');
    el.panel = document.getElementById('state-panel');
    el.columns = document.getElementById('columns');
    el.repo = document.getElementById('repo-filter');
    el.owner = document.getElementById('owner-filter');
    el.search = document.getElementById('search');
    el.auto = document.getElementById('auto-refresh');
    el.refresh = document.getElementById('refresh-now');
    el.loadFile = document.getElementById('load-file');
    el.fileInput = document.getElementById('file-input');
    el.theme = document.getElementById('theme-toggle');
    el.overlay = document.getElementById('overlay');
    el.drawer = document.getElementById('drawer');
  }

  function buildColumns() {
    COLUMNS.forEach(function (col) {
      var root = document.createElement('section');
      root.className = 'column';
      root.dataset.key = col.key;
      root.innerHTML =
        '<header class="col-head">' +
          '<div class="col-title"><span class="dot"></span><h2>' + esc(col.title) + '</h2>' +
            '<span class="count">0</span></div>' +
          '<p class="col-hint">' + esc(col.hint) + '</p>' +
        '</header>' +
        '<div class="col-body"><div class="col-empty">暂无任务</div></div>';

      var body = root.querySelector('.col-body');
      var count = root.querySelector('.count');
      var empty = root.querySelector('.col-empty');
      var toggle = null;
      var more = null;

      if (col.collapsible) {
        toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'col-toggle';
        toggle.setAttribute('aria-expanded', 'false');
        toggle.addEventListener('click', function () {
          state.closedOpen = !state.closedOpen;
          render();
        });
        root.querySelector('.col-head').appendChild(toggle);
      }

      if (col.key === 'recent') {
        more = document.createElement('div');
        more.className = 'col-more';
        more.hidden = true;
        body.appendChild(more);
      }

      cols[col.key] = { root: root, body: body, count: count, empty: empty, toggle: toggle, more: more, overflow: 0 };
      el.columns.appendChild(root);
    });
  }

  // ---------- 卡片渲染 ----------
  function prioBadge(t) {
    var p = (t.priority || '').toUpperCase();
    var cls = p === 'P1' ? 'p1' : (p === 'P2' ? 'p2' : (p === 'P3' ? 'p3' : 'p3'));
    return '<span class="badge ' + cls + '">' + esc(t.priority || 'P?') + '</span>';
  }

  function resultBadge(t) {
    var rs = t.result_status;
    if (rs === 'done') return '<span class="badge ok">done</span>';
    if (rs === 'failed') return '<span class="badge danger">failed</span>';
    return '';
  }

  /** 已领取 / 租约剩余 / 已过期 */
  function timeLine(t) {
    var now = Date.now();
    var claimed = parseTime(t.claimed_at);
    var lease = parseTime(t.lease_until);
    var stale = isStale(t);
    var parts = [];
    if (claimed !== null) parts.push('已领取 ' + humanDuration(now - claimed));
    if (lease !== null) {
      if (stale) parts.push('<span class="over">已过期 ' + humanDuration(now - lease) + '</span>');
      else parts.push('租约剩余 ' + humanDuration(lease - now));
    } else if (stale) {
      parts.push('<span class="over">租约过期</span>');
    }
    return parts.join(' · ');
  }

  /** verify 通过数 / 总数 + 耗时 */
  function resultLine(t) {
    var parts = [];
    if (t.verify.length) {
      var pass = t.verify.filter(function (v) { return v && v.code === 0; }).length;
      var all = t.verify.length;
      var cls = pass === all ? 'ok-text' : 'danger-text';
      parts.push('<span class="' + cls + '">通过 ' + pass + '/' + all + '</span>');
    }
    var d = formatDuration(t.duration_s);
    if (d) parts.push('耗时 ' + d);
    return parts.join(' · ');
  }

  function cardInner(t) {
    var stale = isStale(t);
    var col = classify(t);
    var h = '';

    h += '<div class="card-row">';
    h += '<span class="tid">' + esc(t.id) + '</span>';
    h += prioBadge(t);
    if (stale) h += '<span class="badge danger">租约过期</span>';
    h += resultBadge(t);
    h += '</div>';

    h += '<h3 class="card-title">' + esc(t.title) + '</h3>';

    h += '<div class="card-row">';
    h += '<span class="chip">' + esc(t.repo || '—') + '</span>';
    h += '<span class="chip">' + (ownerOf(t) ? esc(ownerOf(t)) : '未分派') + '</span>';
    h += '</div>';

    if (t.state === 'done' && t.finished_at) {
      var fin = parseTime(t.finished_at);
      if (fin !== null) h += '<div class="card-time">完成于 ' + relTime(fin) + '</div>';
    } else {
      var tt = (t.claimed_at || t.lease_until) ? timeLine(t) : '';
      if (tt) h += '<div class="card-time">' + tt + '</div>';
    }

    if (col === 'merging' || col === 'abnormal' || t.result_status) {
      var rl = resultLine(t);
      if (rl) h += '<div class="card-meta">' + rl + '</div>';
    }

    if (t.depends_on.length) {
      h += '<div class="dep">依赖 ' + t.depends_on.map(function (d) {
        return '<span class="chip">' + esc(d) + '</span>';
      }).join(' ') + '</div>';
    }
    return h;
  }

  function createCard(t) {
    var card = document.createElement('article');
    card.className = 'card is-new';
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.dataset.id = t.id;
    card.addEventListener('click', function () { openDrawer(t.id); });
    card.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault();
        openDrawer(t.id);
      }
    });
    // 入场动画播完就摘掉标记，避免后续重排时重复播放
    card.addEventListener('animationend', function () { card.classList.remove('is-new'); }, { once: true });
    fillCard(card, t);
    return card;
  }

  function fillCard(card, t) {
    var stale = isStale(t);
    card.classList.toggle('is-stale', stale);
    card.dataset.col = classify(t);
    card.innerHTML = cardInner(t);
    card.setAttribute('aria-label', t.id + '：' + t.title + '（' + COL_TITLE[classify(t)] + '）');
  }

  /** 按 id 做键的增量更新：复用已有节点，只给新卡片加入场动画 */
  function syncList(col) {
    var list = col.list;
    var body = col.body;
    var existing = Object.create(null);
    Array.prototype.forEach.call(body.children, function (node) {
      if (node.dataset && node.dataset.id) existing[node.dataset.id] = node;
    });

    var desired = list.map(function (t) {
      var card = existing[t.id];
      if (card) { delete existing[t.id]; fillCard(card, t); return card; }
      return createCard(t);
    });

    Object.keys(existing).forEach(function (id) { existing[id].remove(); });
    desired.forEach(function (card) { body.appendChild(card); }); // 复用节点会被移动而非重建
    col.empty.hidden = list.length > 0;
  }

  // ---------- 顶部统计 / 元信息 ----------
  function renderStats(counts) {
    var html = '<span class="stat">共 <b>' + state.filtered.length + '</b> 个任务</span>';
    COLUMNS.forEach(function (c) {
      html += '<span class="stat' + (c.key === 'abnormal' ? ' is-abnormal' : '') + '">' +
        esc(c.title) + ' <b>' + (counts[c.key] || 0) + '</b></span>';
    });
    el.stats.innerHTML = html;
  }

  function renderMeta() {
    if (!state.lastUpdated) { el.meta.textContent = '最后更新：—'; return; }
    el.meta.textContent = '最后更新 ' + relTime(state.lastUpdated) + '（' + dtf.format(state.lastUpdated) + '）';
    el.source.textContent = '数据源：' + state.source;
  }

  function renderFilters() {
    fillSelect(el.repo, uniq(state.tasks.map(function (t) { return t.repo; }).filter(Boolean)), state.repo, '全部仓库');
    fillSelect(el.owner, uniq(state.tasks.map(ownerOf).filter(Boolean)), state.owner, '全部 owner');
  }

  function uniq(arr) {
    var seen = Object.create(null), out = [];
    arr.forEach(function (v) { if (!seen[v]) { seen[v] = 1; out.push(v); } });
    return out.sort();
  }

  function fillSelect(sel, values, current, allLabel) {
    var html = '<option value="">' + esc(allLabel) + '</option>';
    values.forEach(function (v) {
      html += '<option value="' + esc(v) + '"' + (v === current ? ' selected' : '') + '>' + esc(v) + '</option>';
    });
    if (sel.dataset.sig !== html) { sel.innerHTML = html; sel.dataset.sig = html; }
    sel.value = current;
  }

  // ---------- 状态面板 / 横幅 ----------
  function showPanel(kind, msg) {
    var html = '';
    if (kind === 'loading') {
      html = '<div class="spinner"></div><h2>正在加载任务状态…</h2>';
    } else if (kind === 'error') {
      html = '<h2>没能读到任务状态</h2><p class="msg">' + esc(msg || '未知错误') + '</p>' +
        '<button type="button" class="btn primary" id="panel-retry">重试</button>' +
        '<p class="msg">也可以把 hive status --json 导出的文件拖到页面上。</p>';
    } else {
      html = '<h2>还没有任务</h2><p class="msg">当前筛选条件下没有任何任务卡。</p>';
    }
    el.panel.innerHTML = html;
    el.panel.hidden = false;
    el.columns.hidden = true;
    if (kind === 'error') {
      document.getElementById('panel-retry').addEventListener('click', function () { loadStatus(); });
    }
  }

  function hidePanel() {
    el.panel.hidden = true;
    el.columns.hidden = false;
  }

  function renderBanner() {
    if (state.error && state.tasks.length) {
      el.banner.innerHTML = '<span>刷新失败：' + esc(state.error) + '（展示的是上一次成功的数据）</span>';
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn';
      btn.textContent = '重试';
      btn.addEventListener('click', function () { loadStatus(); });
      el.banner.appendChild(btn);
      el.banner.hidden = false;
    } else {
      el.banner.hidden = true;
      el.banner.innerHTML = '';
    }
  }

  // ---------- 主渲染 ----------
  function applyFilters() {
    var q = state.query.trim().toLowerCase();
    state.filtered = state.tasks.filter(function (t) {
      if (state.repo && t.repo !== state.repo) return false;
      if (state.owner && ownerOf(t) !== state.owner) return false;
      if (!q) return true;
      var hay = (t.id + ' ' + t.title + ' ' + t.summary + ' ' + t.result_summary).toLowerCase();
      return hay.indexOf(q) !== -1;
    });
  }

  function render() {
    applyFilters();
    renderFilters();
    renderBanner();

    if (state.loading && !state.tasks.length) { showPanel('loading'); renderStats({}); return; }
    if (!state.tasks.length && state.error) { showPanel('error', state.error); renderStats({}); return; }
    if (!state.tasks.length) { showPanel('empty'); renderStats({}); return; }
    hidePanel();

    var groups = {};
    COLUMNS.forEach(function (c) { groups[c.key] = []; });
    state.filtered.forEach(function (t) { groups[classify(t)].push(t); });

    var counts = {};
    COLUMNS.forEach(function (c) {
      var list = groups[c.key];
      var overflow = 0;
      if (c.key === 'recent') {
        list = list.slice().sort(sortRecent);
        if (list.length > MAX_RECENT) { overflow = list.length - MAX_RECENT; list = list.slice(0, MAX_RECENT); }
      } else {
        list = list.sort(sortTasks);
      }
      // 统计与角标按「完整归属」计（含被截断的近期完成），避免数字对不上
      counts[c.key] = groups[c.key].length;
      cols[c.key].count.textContent = groups[c.key].length;
      cols[c.key].list = list;
      cols[c.key].overflow = overflow;

      if (cols[c.key].toggle) {
        cols[c.key].toggle.textContent = state.closedOpen
          ? '折叠（' + list.length + '）'
          : '展开（' + list.length + '）';
        cols[c.key].toggle.setAttribute('aria-expanded', String(state.closedOpen));
        cols[c.key].body.hidden = !state.closedOpen;
      }
      if (!cols[c.key].body.hidden) {
        syncList(cols[c.key]);
        var m = cols[c.key].more;
        if (m) {
          m.hidden = overflow <= 0;
          if (overflow > 0) m.textContent = '还有 ' + overflow + ' 条更早的近期完成（本列仅展示最近 ' + MAX_RECENT + ' 条）';
          cols[c.key].body.appendChild(m);  // 移到末尾，避免压在卡片前
        }
      }
    });

    renderStats(counts);
    renderMeta();
    if (drawerId) refreshDrawer();
  }

  // ---------- 数据加载 ----------
  function setTasks(list, source) {
    state.tasks = list.map(normalize);
    state.error = '';
    state.lastUpdated = new Date();
    state.source = source;
    state.loading = false;
    render();
  }

  function loadStatus() {
    if (state.inflight) return;
    state.inflight = true;
    state.loading = !state.tasks.length;   // 只有一条数据都没有时才显示整屏 loading
    render();

    fetch('/api/status', { cache: 'no-store', headers: { Accept: 'application/json' } })
      .then(function (r) {
        if (!r.ok) {
          return r.json().catch(function () { return null; }).then(function (body) {
            throw new Error((body && body.error) ? body.error : ('HTTP ' + r.status));
          });
        }
        return r.json();
      })
      .then(function (data) {
        if (!Array.isArray(data)) throw new Error('状态文件根节点不是数组');
        setTasks(data, '/api/status');
      })
      .catch(function (e) {
        state.error = (e && e.message) ? e.message : String(e);
        state.loading = false;
        render();
      })
      .then(function () { state.inflight = false; });
  }

  /** 拖拽 / 选择本地 JSON 的降级通道 */
  function readFile(file) {
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var data = JSON.parse(String(reader.result));
        if (!Array.isArray(data)) throw new Error('文件根节点不是数组');
        state.autoRefresh = false;      // 手动载入后停掉自动刷新，避免覆盖
        el.auto.checked = false;
        storeSet(STORE_AUTO, 'off');
        setTasks(data, file.name);
      } catch (e) {
        state.error = '本地文件解析失败：' + ((e && e.message) ? e.message : e);
        render();
      }
    };
    reader.onerror = function () {
      state.error = '本地文件读取失败';
      render();
    };
    reader.readAsText(file);
  }

  // ---------- 详情抽屉 ----------
  function findTask(id) {
    for (var i = 0; i < state.tasks.length; i++) if (state.tasks[i].id === id) return state.tasks[i];
    return null;
  }

  function kvBlock(title, rows) {
    var h = '<section class="block"><h3>' + esc(title) + '</h3><dl class="kv">';
    rows.forEach(function (r) {
      h += '<dt>' + esc(r[0]) + '</dt><dd' + (r[2] ? ' class="mono"' : '') + '>' +
        (r[1] === '' || r[1] == null ? '<span class="muted">—</span>' : esc(r[1])) + '</dd>';
    });
    return h + '</dl></section>';
  }

  function drawerHTML(t) {
    var stale = isStale(t);
    var col = classify(t);
    var h = '';

    h += '<div class="drawer-head"><div class="card-row">' +
      '<span class="tid big">' + esc(t.id) + '</span>' + prioBadge(t) +
      (stale ? '<span class="badge danger">租约过期</span>' : '') + resultBadge(t) +
      '<span class="badge state">' + esc(COL_TITLE[col]) + '</span>' +
      '</div><h2 id="drawer-title">' + esc(t.title) + '</h2>' +
      '<button type="button" class="btn icon close" id="drawer-close" aria-label="关闭详情">✕</button></div>';

    h += '<div class="drawer-body">';

    h += kvBlock('概览', [
      ['所在列', COL_TITLE[col]],
      ['state', t.state],
      ['仓库', t.repo],
      ['owner', ownerOf(t) || ''],
      ['抢锁 agent', t.claim_agent || ''],
      ['commit', t.head, true],
      ['耗时', formatDuration(t.duration_s)],
      ['依赖', t.depends_on.length ? t.depends_on.join('、') : '']
    ]);

    h += '<section class="block"><h3>结果摘要</h3>' +
      (t.result_summary
        ? '<pre class="summary">' + esc(t.result_summary) + '</pre>'
        : '<p class="muted">（worker 还没有写摘要）</p>') +
      '</section>';

    h += '<section class="block"><h3>验收命令（' + t.verify.length + '）</h3>';
    if (!t.verify.length) {
      h += '<p class="muted">（没有验收命令）</p>';
    } else {
      h += '<ol class="verify">';
      t.verify.forEach(function (v) {
        v = v || {};
        var ok = v.code === 0;
        h += '<li><div class="verify-cmd"><code>' + esc(v.cmd || '') + '</code>' +
          '<span class="badge ' + (ok ? 'ok' : 'danger') + '">退出码 ' + esc(String(v.code)) + '</span></div>' +
          '<pre class="tail">' +
          esc(((v.tail && v.tail.length) ? v.tail.join('\n') : '（无输出）')) +
          '</pre></li>';
      });
      h += '</ol>';
    }
    h += '</section>';

    var leaseText = t.lease_until
      ? dtf.format(new Date(t.lease_until)) + (stale ? '（已过期 ' + humanDuration(Date.now() - parseTime(t.lease_until)) + '）' : '（有效）')
      : '';
    var timeRows = [
      ['抢锁时间', t.claimed_at ? dtf.format(new Date(t.claimed_at)) : ''],
      ['租约到期', leaseText],
      ['租约状态', t.lease_until ? (stale ? '已过期' : '有效') : '无租约']
    ];
    if (t.state === 'done' && t.finished_at) {
      var finMs = parseTime(t.finished_at);
      timeRows.unshift(['完成时间', finMs ? dtf.format(new Date(finMs)) : '']);
    }
    h += kvBlock('时间线', timeRows);

    h += '</div>';
    return h;
  }

  function openDrawer(id) {
    var t = findTask(id);
    if (!t) return;
    drawerId = id;
    lastFocus = document.activeElement;
    el.drawer.innerHTML = drawerHTML(t);
    el.drawer.classList.add('open');
    el.drawer.setAttribute('aria-hidden', 'false');
    el.overlay.classList.add('open');
    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
    document.getElementById('drawer-close').focus();
  }

  /** 抽屉开着时数据刷新了，就地更新内容（不打断阅读位置以外的状态） */
  function refreshDrawer() {
    var t = findTask(drawerId);
    if (!t) return;
    // 焦点还在抽屉里（例如停留在关闭按钮上）时不要重建，免得把焦点弄丢
    if (el.drawer.contains(document.activeElement)) return;
    var scroll = el.drawer.querySelector('.drawer-body');
    var top = scroll ? scroll.scrollTop : 0;
    el.drawer.innerHTML = drawerHTML(t);
    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
    var body = el.drawer.querySelector('.drawer-body');
    if (body) body.scrollTop = top;
  }

  function closeDrawer() {
    if (!drawerId) return;
    drawerId = null;
    el.drawer.classList.remove('open');
    el.drawer.setAttribute('aria-hidden', 'true');
    el.overlay.classList.remove('open');
    if (lastFocus && lastFocus.focus) lastFocus.focus();
    lastFocus = null;
  }

  // ---------- 主题 ----------
  function applyTheme(mode) {
    document.documentElement.setAttribute('data-theme', mode);
    storeSet(STORE_THEME, mode);
  }

  function initTheme() {
    var saved = storeGet(STORE_THEME);
    if (saved !== 'dark' && saved !== 'light') {
      saved = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    }
    document.documentElement.setAttribute('data-theme', saved);
    el.theme.addEventListener('click', function () {
      var now = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      applyTheme(now);
    });
    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      var onChange = function (e) {
        if (storeGet(STORE_THEME) !== 'dark' && storeGet(STORE_THEME) !== 'light') {
          document.documentElement.setAttribute('data-theme', e.matches ? 'dark' : 'light');
        }
      };
      if (mq.addEventListener) mq.addEventListener('change', onChange);
      else if (mq.addListener) mq.addListener(onChange);
    }
  }

  // ---------- 事件绑定 ----------
  function bindEvents() {
    el.repo.addEventListener('change', function () { state.repo = el.repo.value; render(); });
    el.owner.addEventListener('change', function () { state.owner = el.owner.value; render(); });
    el.search.addEventListener('input', function () { state.query = el.search.value; render(); });
    el.refresh.addEventListener('click', function () { loadStatus(); });

    el.auto.addEventListener('change', function () {
      state.autoRefresh = el.auto.checked;
      storeSet(STORE_AUTO, state.autoRefresh ? 'on' : 'off');
    });

    el.loadFile.addEventListener('click', function () { el.fileInput.click(); });
    el.fileInput.addEventListener('change', function () {
      readFile(el.fileInput.files && el.fileInput.files[0]);
      el.fileInput.value = '';
    });

    el.overlay.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' || e.key === 'Esc') closeDrawer();
    });

    // 文件拖拽降级通道
    var dragDepth = 0;
    document.addEventListener('dragenter', function (e) {
      e.preventDefault(); dragDepth++; document.body.classList.add('dropping');
    });
    document.addEventListener('dragover', function (e) { e.preventDefault(); });
    document.addEventListener('dragleave', function () {
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) document.body.classList.remove('dropping');
    });
    document.addEventListener('drop', function (e) {
      e.preventDefault();
      dragDepth = 0;
      document.body.classList.remove('dropping');
      var dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length) readFile(dt.files[0]);
    });

    // 自动刷新：页面不可见时不打扰
    setInterval(function () {
      if (state.autoRefresh && !state.inflight && document.visibilityState === 'visible') loadStatus();
    }, REFRESH_MS);

    // 相对时间自行重算（不重新请求网络）
    setInterval(function () { if (!state.inflight) render(); }, TICK_MS);

    // CI 状态条
    setInterval(loadCI, CI_MS);
  }

  // ---------- CI 监控条 ----------
  var CI_MS = 30000;

  function ciStatusClass(st) {
    return ({ Waiting: 'ci-wait', Running: 'ci-run', Success: 'ci-ok',
              Failure: 'ci-bad', Canceled: 'ci-cancel', Blocked: 'ci-bad' })[st] || 'ci-unknown';
  }

  function ciBadge(st) {
    return '<span class="ci-dot ' + ciStatusClass(st) + '"></span>' + esc(st || '?');
  }

  function renderCI(data) {
    var root = document.getElementById('ci-strip');
    if (!root) return;
    if (!data) { root.hidden = true; root.innerHTML = ''; return; }
    var html = '';
    var rs = data.runners || [];
    if (rs.length) {
      html += '<div class="ci-group"><span class="ci-label">Runner</span>' +
        rs.map(function (r) {
          return '<span class="ci-chip"><span class="ci-dot ' +
            (r.status === 'online' ? 'ci-ok' : 'ci-bad') + '"></span>' +
            esc(r.name || '?') + ' <em>' + esc(r.status || '?') + '</em></span>';
        }).join('') + '</div>';
    }
    var repos = data.repos || {};
    var names = Object.keys(repos);
    if (names.length) {
      html += '<div class="ci-group"><span class="ci-label">CI</span>' +
        names.map(function (r) {
          var o = repos[r], l = o && o.latest;
          var tail = '';
          if (l && l.title) tail = '<em class="ci-title">' + esc(l.title.slice(0, 38)) + '</em>';
          var st = (o && o.error) ? '' : (l ? l.status : '');
          var body = o && o.error
            ? '<span class="ci-dot ci-bad"></span>错误'
            : ciBadge(st);
          return '<span class="ci-chip">' + esc(r.split('/').pop()) + ': ' + body +
            (l ? ' <em>#' + l.run + '</em>' : '') + tail + '</span>';
        }).join('') + '</div>';
    }
    html += '<span class="ci-time">' +
      esc(data.checked_at ? data.checked_at.slice(11, 19) : '') + '</span>';
    root.innerHTML = html;
    root.hidden = false;
  }

  function loadCI() {
    try {
      fetch('/ci.json', { cache: 'no-store', headers: { Accept: 'application/json' } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { renderCI(d); })
        .catch(function () { renderCI(null); });
    } catch (e) {
      renderCI(null);
    }
  }

  // ---------- 启动 ----------
  function init() {
    cacheEls();
    buildColumns();
    initTheme();
    bindEvents();

    state.autoRefresh = storeGet(STORE_AUTO) !== 'off';
    el.auto.checked = state.autoRefresh;

    loadStatus();
    loadCI();
  }

  init();
})();
