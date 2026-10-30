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
  var MAX_RECENT = 50;                              // 「最近完成」硬上限，防止误设过大
  var DEFAULT_RECENT_COUNT = 8;                     // 「最近完成」默认展示条数
  var STORE_SETTINGS = 'hive.settings';             // 个人设置（稀疏对象，仅存用户改过的项）

  // 六列，从左到右
  // recent（最近完成）= 已完成任务里按 finished_at 倒序的前 N 个（默认 8，可在设置里改）；
  // 其余已完成任务留在 closed（已关闭）归档。done 任务各归一列，不会重复。
  var COLUMNS = [
    { key: 'ready', title: '待领取', hint: 'state = ready 且无人抢锁' },
    { key: 'running', title: '进行中', hint: '已抢锁 / assigned / running，暂无结果' },
    { key: 'merging', title: '待合并', hint: 'result_status = done，state 还不是 done' },
    { key: 'abnormal', title: '异常', hint: 'failed / blocked / 租约过期' },
    { key: 'recent', title: '最近完成', hint: '已完成中按完成时间倒序的前 N 个（数量可在设置里改）' },
    { key: 'closed', title: '已关闭', hint: '其余已完成的任务（默认折叠）', collapsible: true }
  ];
  var COL_TITLE = {};
  COLUMNS.forEach(function (c) { COL_TITLE[c.key] = c.title; });

  // ---------- 设置：个人 local（稀疏，仅存改过的项）＋ 服务端共享默认（个人优先） ----------
  function loadPersonalRaw() {
    try {
      var o = JSON.parse(storeGet(STORE_SETTINGS) || '{}');
      return (o && typeof o === 'object') ? o : {};
    } catch (e) { return {}; }
  }
  function savePersonal() { storeSet(STORE_SETTINGS, JSON.stringify(personal)); }

  /** 合并优先级：个人覆盖（personal）> 服务端默认（server）> 内置默认 */
  function computeEffective() {
    function pick(k, dflt) { return (k in personal) ? personal[k] : ((k in server) ? server[k] : dflt); }
    function pickCol(k) {
      var p = personal.columns, s = server.columns;
      if (p && (k in p)) return !!p[k];
      if (s && (k in s)) return !!s[k];
      return true;
    }
    function clampInt(v, lo, hi, dflt) {
      v = Number(v);
      return (isFinite(v) && v >= lo && v <= hi) ? Math.round(v) : dflt;
    }
    var s = {
      recentCount: clampInt(pick('recentCount', DEFAULT_RECENT_COUNT), 1, MAX_RECENT, DEFAULT_RECENT_COUNT),
      refreshMs: clampInt(pick('refreshMs', REFRESH_MS), 5000, 600000, REFRESH_MS),
      compact: !!pick('compact', false),
      showCI: pick('showCI', true),
      showVerify: pick('showVerify', true),
      columns: {}
    };
    COLUMNS.forEach(function (c) { s.columns[c.key] = pickCol(c.key); });
    return s;
  }

  var personal = loadPersonalRaw();   // 个人覆盖（稀疏）
  var server = {};                    // 服务端共享默认（GET /api/config 拉取）
  var settings = computeEffective();  // 生效设置
  var refreshTimer = null;

  /** 把生效设置落到 DOM（主题/紧凑/CI/验收/自动刷新间隔） */
  function applyEffective() {
    document.body.classList.toggle('compact', !!settings.compact);
    document.body.classList.toggle('no-verify', !settings.showVerify);
    var ci = document.getElementById('ci-strip');
    if (ci) ci.hidden = !settings.showCI;
    startAutoRefresh();
  }

  function startAutoRefresh() {
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
    refreshTimer = setInterval(function () {
      if (state.autoRefresh && !state.inflight && document.visibilityState === 'visible') loadStatus();
    }, settings.refreshMs);
  }

  /** 拉取服务端共享设置与系统只读信息 */
  function loadServerConfig() {
    try {
      fetch('/api/config', { cache: 'no-store', headers: { Accept: 'application/json' } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) return;
          if (d.dashboard) { server = d.dashboard || {}; settings = computeEffective(); applyEffective(); render(); }
          if (d.system) state.system = d.system;
          if (state.settingsOpen) renderSettings();
        })
        .catch(function () {});
    } catch (e) {}
  }

  var dtf = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'medium', hour12: false });
  var rtf = new Intl.RelativeTimeFormat('zh-CN', { numeric: 'auto' });

  // ---------- 状态 ----------
  var state = {
    tasks: [],        // 归一化后的全量任务
    filtered: [],     // 经过筛选后的任务
    recentIds: {},    // 当前「最近完成」集合（id -> true），每轮 render 重算
    system: null,     // 服务端系统只读信息（GET /api/config 填充）
    settingsOpen: false,
    repo: '',
    owner: '',
    prio: '',         // 优先级筛选（P1/P2/P3）
    unowned: false,   // 仅看无主（未分派）任务
    col: '',          // 顶部统计点击后的「只看该列」筛选，空表示全部
    query: '',
    autoRefresh: true,
    closedOpen: false,
    loading: true,
    error: '',
    lastUpdated: null,
    source: '/api/status',
    snapshotAt: null,   // 快照生成时刻（毫秒），来自 X-Snapshot-Mtime；null = 服务端没给
    cluster: null,      // /api/cluster 的集群视图（leader/term/在线节点）
    board: null,        // /api/status 附带的 board 摘要（stage 分桶）；旧快照里没有 → null
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
      // —— 派发阶段（T-0188 的派生字段）：旧快照没有 stage，取空串后由徽标处静默跳过 ——
      stage: t.stage || '',
      dispatched_to: t.dispatched_to || '',
      dispatched_at: t.dispatched_at || '',
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
      summary: t.summary || '',
      // —— agent 主动上报的进度（status/<tid> git 主通道 + HTTP 加速通道）——
      progress: (t.progress === null || t.progress === undefined || t.progress === '')
        ? null : (Number(t.progress) || 0),
      push_state: t.push_state || '',
      push_step: t.push_step || '',
      push_message: t.push_message || '',
      pushed_at: t.pushed_at || '',
      push_source: t.push_source || ''
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
    if (t.state === 'done' || t.state === 'closed') {
      // done=真完成（最近完成的归 recent），closed=关闭作废（没产出结果就关了）
      // 两者都归档到「已关闭」。少判 closed 会让作废的卡掉进「待领取」，看着像还要人干。
      if (t.state === 'done' && state.recentIds && state.recentIds[t.id]) return 'recent';
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
    el.cluster = document.getElementById('cluster');
    el.board = document.getElementById('board');
    el.banner = document.getElementById('banner');
    el.panel = document.getElementById('state-panel');
    el.columns = document.getElementById('columns');
    el.repo = document.getElementById('repo-filter');
    el.owner = document.getElementById('owner-filter');
    el.prio = document.getElementById('prio-filter');
    el.unowned = document.getElementById('unowned-filter');
    el.search = document.getElementById('search');
    el.auto = document.getElementById('auto-refresh');
    el.refresh = document.getElementById('refresh-now');
    el.loadFile = document.getElementById('load-file');
  el.login = document.getElementById('login-btn');
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

  // ---------- 派发阶段（T-0189）----------
  // 与 dispatch_tracking.board_stage 的取值一一对应；查不到的中文名回落到阶段原名，
  // 保证旧/新快照都不会渲染出 undefined。
  var STAGE_LABEL = {
    pending: '待分配', dispatched: '已推送', claimed: '已领取',
    running: '在跑', done: '已完成', failed: '失败',
    conflict: '冲突', blocked: '挂起'
  };
  var STAGE_ORDER = ['pending', 'dispatched', 'claimed', 'running',
                     'done', 'failed', 'conflict', 'blocked'];

  function stageLabel(s) { return STAGE_LABEL[s] || s || ''; }

  /** 阶段徽标；dispatched 卡额外挂一枚「→ agent」。
   *  dispatched_to 的来源两路都认：卡上的 `dispatched_to`，以及 `stage === 'dispatched'`。 */
  function stageBadge(t) {
    var s = t.stage || '';
    var to = t.dispatched_to || '';
    var h = '';
    if (s) {
      h += '<span class="badge stage-badge" data-stage="' + esc(s) +
        '" title="派发阶段：' + esc(stageLabel(s)) + '">' + esc(stageLabel(s)) + '</span>';
    }
    if (to && (!s || s === 'dispatched')) {
      h += '<span class="badge stage-badge stage-to" data-stage="dispatched"' +
        ' title="已推送给 ' + esc(to) + '">→ ' + esc(to) + '</span>';
    }
    return h;
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
    if (t.duration_s) {
      // 耗时分级着色：秒级=快(绿)，超过 1 小时=慢(琥珀/红)，中间正常
      var dcls = t.duration_s < 60 ? 'dur-fast'
               : (t.duration_s > 3600 ? 'dur-slow' : 'dur-mid');
      parts.push('耗时 <span class="' + dcls + '">' + formatDuration(t.duration_s) + '</span>');
    }
    return parts.join(' · ');
  }

  // ---------- 卡片辅助判定 ----------
  var LEASE_URGENT_MS = 10 * 60 * 1000;   // 租约剩余低于这个阈值就告警

  /** 租约快到期（还没过期、也还没结果）：让运维一眼看出这里要超时了 */
  function isLeaseUrgent(t) {
    if (isStale(t) || t.result_status) return false;
    var lease = parseTime(t.lease_until);
    if (lease === null) return false;
    var left = lease - Date.now();
    return left > 0 && left < LEASE_URGENT_MS;
  }

  /** 执行模式：按 owner 查 agent 登记（exec / verify / manual），用于卡片左侧色点 */
  function modeOf(t) {
    var owner = ownerOf(t);
    var agents = (state.system && state.system.agents) || [];
    if (!owner || !agents.length) return '';
    for (var i = 0; i < agents.length; i++) {
      var a = agents[i];
      if (a && a.name === owner) return a.mode || (a.manual ? 'manual' : '') || '';
    }
    return '';
  }

  function actBtn(action, tid, label, cls) {
    return '<button type="button" class="act ' + cls + '" data-action="' + action +
      '" data-tid="' + esc(tid) + '">' + esc(label) + '</button>';
  }

  function cardInner(t) {
    var stale = isStale(t);
    var col = classify(t);
    var h = '';
    var mode = modeOf(t);

    h += '<div class="card-row card-head-row">';
    if (mode) {
      h += '<span class="mode-dot" data-mode="' + esc(mode) +
           '" title="执行模式：' + esc(mode) + '"></span>';
    }
    h += '<span class="tid strong">' + esc(t.id) + '</span>';
    h += prioBadge(t);
    if (stale) h += '<span class="badge danger">租约过期</span>';
    else if (isLeaseUrgent(t)) h += '<span class="badge warn">租约将到期</span>';
    h += resultBadge(t);
    h += stageBadge(t);
    h += '</div>';

    h += '<h3 class="card-title">' + esc(t.title) + '</h3>';

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

    // —— agent 主动上报的进度：进度条 + 当前阶段 + 一句话状态 + 最后通信时间 ——
    if (t.push_step || t.push_message || typeof t.progress === 'number') {
      h += '<div class="card-progress">';
      if (typeof t.progress === 'number') {
        var pc = Math.max(0, Math.min(100, Math.round(t.progress)));
        h += '<div class="bar"><i style="width:' + pc + '%"></i></div>' +
             '<span class="pct">' + pc + '%</span>';
      }
      if (t.push_step) h += '<span class="chip">' + esc(t.push_step) + '</span>';
      h += '</div>';
      if (t.push_message) h += '<div class="card-msg">' + esc(t.push_message) + '</div>';
    }
    if (t.pushed_at) {
      var seen = parseTime(t.pushed_at);
      if (seen !== null) {
        // 超过 10 分钟没上报就标红：这是「长时间没有通信」的直观信号
        var quiet = (Date.now() - seen) > 10 * 60 * 1000;
        h += '<div class="card-time">上报 ' +
             '<span class="' + (quiet ? 'over' : '') + '">' + relTime(seen) + '</span>' +
             (t.push_source ? ' · ' + esc(t.push_source) : '') + '</div>';
      }
    }

    // 底部标签区：repo / owner / 依赖——不跟标题抢视觉；依赖可点击定位到被依赖的卡
    h += '<div class="card-tags">';
    h += '<span class="pill pill-repo">' + esc(t.repo || '—') + '</span>';
    h += '<span class="pill' + (ownerOf(t) ? '' : ' pill-empty') + '">' +
         (ownerOf(t) ? esc(ownerOf(t)) : '未分派') + '</span>';
    t.depends_on.forEach(function (d) {
      h += '<button type="button" class="pill pill-dep" data-dep="' + esc(d) +
           '" title="点击定位到 ' + esc(d) + '">依赖 ' + esc(d) + '</button>';
    });
    h += '</div>';

    // 复制派发指令：每张卡都有（含已完成/已关闭），只复制文本，不领卡不改状态。
    h += '<div class="card-copy">' +
      '<button type="button" class="act act-copy" data-copy="' + esc(t.id) +
      '" title="把这张卡的派发指令复制到剪贴板，粘到 agent 对话框即可">复制派发指令</button>' +
      '</div>';

    // 操作按钮。
    // 「终止」曾经直接等同于 close，结果把没跑完的卡标成 done（T-0181 的教训），
    // 现在拆成两个语义清楚的动作：
    //   unassign=终止本次执行、放回待领取（想继续做就选这个）
    //   close   =这张卡不做了（有结果记 done，没结果记 closed 作废）
    if (col === 'abnormal' || col === 'running' || col === 'ready' ||
        col === 'closed' || col === 'recent') {
      h += '<div class="card-actions">';
      if (col === 'abnormal') {
        h += actBtn('unassign', t.id, '退回待领取', 'act-unassign');
        h += actBtn('reassign-open', t.id, '改派', 'act-reassign');
        h += actBtn('retry', t.id, '重试', 'act-retry');
        h += actBtn('close', t.id, '关闭作废', 'act-close');
      } else if (col === 'running') {
        h += actBtn('retry', t.id, '重试', 'act-retry');
        h += actBtn('unassign', t.id, '强制收回', 'act-unassign');
      } else if (col === 'ready') {
        h += actBtn('reassign-open', t.id, '派给…', 'act-reassign');
      } else {
        h += actBtn('reopen', t.id, '重开', 'act-reopen');
      }
      h += '</div>';
    }
    return h;
  }

  // ---------- 复制派发指令（T-0189）----------
  /**
   * 写剪贴板：优先异步 Clipboard API，失败（HTTP 非安全上下文 / 无权限 / 老浏览器）
   * 再走隐藏 textarea + execCommand("copy")。两条路径都返回 Promise。
   */
  function writeClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(function () {
        return legacyCopy(text);
      });
    }
    return legacyCopy(text);
  }

  function legacyCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.left = '-1000px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    var ok = false;
    try {
      ta.select();
      ta.setSelectionRange(0, ta.value.length);   // iOS Safari 只认 setSelectionRange
      ok = document.execCommand("copy");
    } catch (e) {
      ok = false;
    }
    document.body.removeChild(ta);
    return ok ? Promise.resolve() : Promise.reject(new Error('浏览器拒绝写入剪贴板'));
  }

  /** 按钮文字变「已复制」2 秒后复原；只改按钮，不刷新整页 */
  var COPY_TIMERS = Object.create(null);
  function flashCopied(btn) {
    if (!btn) return;
    if (!btn.dataset.label) btn.dataset.label = btn.textContent;
    var tid = btn.dataset.copy || '';
    btn.textContent = '已复制';
    btn.classList.add('is-copied');
    btn.disabled = true;
    if (COPY_TIMERS[tid]) clearTimeout(COPY_TIMERS[tid]);
    COPY_TIMERS[tid] = setTimeout(function () {
      btn.textContent = btn.dataset.label;
      btn.classList.remove('is-copied');
      btn.disabled = false;
      delete COPY_TIMERS[tid];
    }, 2000);
  }

  function copyDispatch(tid, btn) {
    fetch('/api/dispatch-prompt?task=' + encodeURIComponent(tid),
          { cache: 'no-store', headers: { Accept: 'application/json' } })
      .then(function (r) {
        return r.json().catch(function () { return null; }).then(function (body) {
          if (!r.ok) throw new Error((body && body.error) ? body.error : ('HTTP ' + r.status));
          return body || {};
        });
      })
      .then(function (body) {
        return writeClipboard(body.text || '').then(function () {
          flashCopied(btn);
          flash('已复制 ' + tid + ' 的派发指令，切到 agent 粘贴即可');
        });
      })
      .catch(function (e) {
        flash('复制失败：' + ((e && e.message) ? e.message : e), true);
      });
  }

  function createCard(t) {
    var card = document.createElement('article');
    card.className = 'card is-new';
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.dataset.id = t.id;
    // 复制按钮必须**先**注册并 stopImmediatePropagation：卡片自己也有 click → openDrawer，
    // 两个监听挂在同一个节点上，stopPropagation 拦不住同节点的后一个，只有 immediate 版有效。
    card.addEventListener('click', function (e) {
      var btn = e.target && e.target.closest ? e.target.closest('[data-copy]') : null;
      if (!btn || !card.contains(btn)) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      copyDispatch(btn.getAttribute('data-copy') || t.id, btn);
    });
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
    card.classList.toggle('is-urgent', isLeaseUrgent(t));   // 租约临期：呼吸灯告警
    var mode = modeOf(t);
    if (mode) card.dataset.mode = mode;
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
  // 各列的空状态文案：说清楚「什么时候这里会有东西」，比干巴巴的「暂无任务」有用
  var COL_EMPTY = {
    ready: '没有待领取的任务卡。',
    running: '没有进行中的任务。',
    merging: '当 Worker 完成并推送 res/ 分支后，这里会自动出现任务卡；请用 CLI 运行 hive land 合并。',
    abnormal: '没有异常任务。',
    recent: '最近还没有完成的任务。',
    closed: '暂无已关闭任务（点上方「展开」查看）'
  };

  /**
   * board 摘要的取值：优先用服务端给的 `board.counts`（dispatch_tracking 的权威口径），
   * 拿不到就按每张卡的 `stage` 现算；两个来源都没有（旧快照）→ 返回 null，
   * 调用方据此整条隐藏，绝不渲染 undefined。
   */
  function boardCounts() {
    var b = state.board && state.board.counts;
    if (b && typeof b === 'object') {
      return { counts: b, total: Number(state.board.total) || state.tasks.length };
    }
    var counts = Object.create(null);
    var seen = false;
    var total = 0;
    state.tasks.forEach(function (t) {
      if (!t.stage) return;
      seen = true;
      total++;
      counts[t.stage] = (counts[t.stage] || 0) + 1;
    });
    return seen ? { counts: counts, total: total } : null;
  }

  function renderBoard() {
    if (!el.board) return;
    var b = boardCounts();
    if (!b) {
      el.board.hidden = true;
      el.board.textContent = '';
      return;
    }
    var counts = b.counts || {};
    var parts = [];
    var used = Object.create(null);
    STAGE_ORDER.forEach(function (s) {
      var n = counts[s] || 0;
      used[s] = true;
      if (n > 0) parts.push(stageLabel(s) + ' ' + n);
    });
    // 服务端将来加新阶段时也别丢：不认识的键按原名排在后面
    Object.keys(counts).forEach(function (s) {
      if (used[s]) return;
      var n = Number(counts[s]) || 0;
      if (n > 0) parts.push(stageLabel(s) + ' ' + n);
    });
    if (!parts.length) parts.push('合计 ' + (b.total || 0));
    el.board.textContent = parts.join(' / ');
    el.board.title = '派发分组（全部任务，不受筛选影响）· 合计 ' + (b.total || 0);
    el.board.hidden = false;
  }

  function renderStats(counts) {
    // 统计条既是仪表盘也是快捷筛选器：点某列只看该列，再点「共 N 个」恢复全部
    var html = '<button type="button" class="stat stat-total' + (state.col ? '' : ' active') +
      '" data-col="" title="显示全部列">共 <b>' + state.filtered.length + '</b> 个任务</button>';
    COLUMNS.forEach(function (c) {
      if (!settings.columns[c.key]) return;   // 隐藏的面板不计入统计条
      html += '<button type="button" class="stat' +
        (c.key === 'abnormal' ? ' is-abnormal' : '') +
        (state.col === c.key ? ' active' : '') +
        '" data-col="' + esc(c.key) + '" title="只看「' + esc(c.title) + '」">' +
        esc(c.title) + ' <b>' + (counts[c.key] || 0) + '</b></button>';
    });
    el.stats.innerHTML = html;
  }

  /** 读服务端给的快照时刻（秒 → 毫秒）。拿不到返回 null，不影响主流程。 */
  function headerTime(r) {
    try {
      var v = r && r.headers && r.headers.get('X-Snapshot-Mtime');
      if (!v) return null;
      var sec = Number(v);
      return (isFinite(sec) && sec > 0) ? sec * 1000 : null;
    } catch (e) {
      return null;
    }
  }

  /**
   * 数据源文案 = 来源 + 快照有多旧。
   *
   * 刷新循环停摆时 /api/status 依旧 200，页面会安安静静地渲染一份几小时前的数据，
   * 看的人却以为它是实时的（2026-10-28 就因为一份 2h43m 的旧快照误判"退回没生效"）。
   * 所以这里把快照时刻显式摆出来，超过 10 分钟再给个警示。
   */
  function sourceLabel() {
    var s = state.source;
    if (!state.snapshotAt) return s;
    var ageMin = (Date.now() - state.snapshotAt) / 60000;
    var txt = relTime(state.snapshotAt);
    if (txt) s += '（快照 ' + txt + '）';
    if (ageMin >= 10) s = '⚠ ' + s + ' —— 快照可能已停更，检查看板服务';
    return s;
  }

  function renderMeta() {
    if (!state.lastUpdated) { el.meta.textContent = '最后更新：—'; return; }
    el.meta.textContent = '最后更新 ' + relTime(state.lastUpdated) + '（' + dtf.format(state.lastUpdated) + '）';
    el.source.textContent = '数据源：' + sourceLabel();
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
      if (state.prio && (t.priority || '').toUpperCase() !== state.prio) return false;
      if (state.unowned && ownerOf(t)) return false;   // 仅看无主
      if (!q) return true;
      var hay = (t.id + ' ' + t.title + ' ' + t.summary + ' ' + t.result_summary).toLowerCase();
      return hay.indexOf(q) !== -1;
    });
  }

  function render() {
    applyFilters();
    renderFilters();
    renderBanner();
    renderBoard();     // 早退分支（loading/error/empty）也要走，旧快照在这里静默隐藏

    if (state.loading && !state.tasks.length) { showPanel('loading'); renderStats({}); return; }
    if (!state.tasks.length && state.error) { showPanel('error', state.error); renderStats({}); return; }
    if (!state.tasks.length) { showPanel('empty'); renderStats({}); return; }
    hidePanel();

    // 计算「最近完成」集合：已完成任务按 finished_at 倒序取前 N 个，其余留在「已关闭」
    var doneSorted = state.filtered.filter(function (t) { return t.state === 'done'; })
      .slice().sort(sortRecent);
    state.recentIds = {};
    var n = Math.min(settings.recentCount, doneSorted.length);
    for (var i = 0; i < n; i++) state.recentIds[doneSorted[i].id] = true;

    var groups = {};
    COLUMNS.forEach(function (c) { groups[c.key] = []; });
    state.filtered.forEach(function (t) { groups[classify(t)].push(t); });

    var counts = {};
    COLUMNS.forEach(function (c) {
      var list = (c.key === 'recent')
        ? groups[c.key].slice().sort(sortRecent)
        : groups[c.key].slice().sort(sortTasks);
      counts[c.key] = list.length;
      cols[c.key].count.textContent = list.length;
      cols[c.key].empty.textContent = COL_EMPTY[c.key] || '暂无任务';
      cols[c.key].list = list;
      // 面板可见性：设置里关掉的，或被「只看该列」筛选排除的
      cols[c.key].root.hidden = !settings.columns[c.key] ||
                               (!!state.col && state.col !== c.key);

      if (cols[c.key].toggle) {
        cols[c.key].toggle.textContent = state.closedOpen
          ? '折叠（' + list.length + '）'
          : '展开（' + list.length + '）';
        cols[c.key].toggle.setAttribute('aria-expanded', String(state.closedOpen));
        cols[c.key].body.hidden = !state.closedOpen;
      }
      if (!cols[c.key].body.hidden) syncList(cols[c.key]);
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

  // ---------- 常驻集群状态（5s 一次，与任务快照解耦） ----------
  var CLUSTER_MS = 5000;

  function renderCluster(data) {
    if (!el.cluster) return;
    var running = !!(data && data.running);
    var snap = (data && data.state) || {};
    var leader = snap.leader || {};
    if (!running) {
      el.cluster.textContent = '集群：未启动';
      el.cluster.className = 'cluster off';
      el.cluster.title = '本机没有运行 hive cluster serve；用 hive cluster start 接入集群';
      return;
    }
    var term = snap.term || 0;
    var alive = snap.alive || 0;
    var known = snap.known || 0;
    var who = leader.name || '无主';
    var self = snap.self || {};
    var txt = '集群：' + who + ' · term ' + term + ' · ' + alive + '/' + known;
    if (snap.role === 'leader' || (self.id && leader.id === self.id)) {
      txt = '集群：本机主节点 · term ' + term + ' · ' + alive + '/' + known;
    }
    el.cluster.textContent = txt;
    el.cluster.className = 'cluster ' + (leader.id ? 'ok' : 'warn');
    el.cluster.title = (leader.url ? ('主节点 ' + leader.name + ' ' + leader.url) : '当前没有主节点') +
      '；节点 ' + alive + '/' + known + ' 在线；数据源 /api/cluster';
  }

  function loadCluster() {
    try {
      fetch('/api/cluster', { cache: 'no-store', headers: { Accept: 'application/json' } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { state.cluster = d; renderCluster(d); })
        .catch(function () { renderCluster(null); });
    } catch (e) { /* 集群是可选件，失败静默 */ }
  }

  function loadStatus() {
    if (state.inflight) return;
    state.inflight = true;
    state.loading = !state.tasks.length;   // 只有一条数据都没有时才显示整屏 loading
    render();

    fetch('/api/status', { cache: 'no-store', headers: { Accept: 'application/json' } })
      .then(function (r) {
        // 快照时刻（服务端文件 mtime）。拿不到就保持 null，只影响标签文案。
        state.snapshotAt = headerTime(r);
        if (!r.ok) {
          return r.json().catch(function () { return null; }).then(function (body) {
            throw new Error((body && body.error) ? body.error : ('HTTP ' + r.status));
          });
        }
        return r.json();
      })
      .then(function (data) {
        // 两种形态都吃：裸数组（旧快照）与 {tasks, board}（hive status --json --board）
        var list = Array.isArray(data) ? data
          : (data && Array.isArray(data.tasks) ? data.tasks : null);
        if (!list) throw new Error('状态文件既不是数组，也没有 tasks 数组');
        state.board = (!Array.isArray(data) && data && data.board) || null;
        setTasks(list, '/api/status');
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
        var list = Array.isArray(data) ? data
          : (data && Array.isArray(data.tasks) ? data.tasks : null);
        if (!list) throw new Error('文件根节点既不是数组，也没有 tasks 数组');
        state.board = (!Array.isArray(data) && data && data.board) || null;
        state.autoRefresh = false;      // 手动载入后停掉自动刷新，避免覆盖
        el.auto.checked = false;
        storeSet(STORE_AUTO, 'off');
        state.snapshotAt = null;        // 本地文件没有"快照时刻"，别拿上次 HTTP 的冒充
        setTasks(list, file.name);
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
      stageBadge(t) +
      '<span class="badge state">' + esc(COL_TITLE[col]) + '</span>' +
      '</div><h2 id="drawer-title">' + esc(t.title) + '</h2>' +
      '<button type="button" class="btn icon close" id="drawer-close" aria-label="关闭详情">✕</button></div>';

    h += '<div class="drawer-body">';

    h += '<div class="drawer-copy">' +
      '<button type="button" class="act act-copy" data-copy="' + esc(t.id) + '">复制派发指令</button>' +
      '</div>';

    h += kvBlock('概览', [
      ['所在列', COL_TITLE[col]],
      ['state', t.state],
      ['派发阶段', t.stage ? stageLabel(t.stage) : ''],
      ['已推送给', t.dispatched_to],
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

  /** 抽屉里的「复制派发指令」：抽屉不套在卡片里，单独绑一次即可 */
  function bindDrawerCopy() {
    var btn = el.drawer.querySelector('[data-copy]');
    if (!btn) return;
    btn.addEventListener('click', function () {
      copyDispatch(btn.getAttribute('data-copy') || drawerId, btn);
    });
  }

  function openDrawer(id) {
    var t = findTask(id);
    if (!t) return;
    drawerId = id;
    lastFocus = document.activeElement;
    el.drawer.innerHTML = drawerHTML(t);
    bindDrawerCopy();
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
    bindDrawerCopy();
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
    if (el.prio) el.prio.addEventListener('change', function () { state.prio = el.prio.value; render(); });
    if (el.unowned) el.unowned.addEventListener('change', function () {
      state.unowned = el.unowned.checked; render();
    });
    // 顶部统计条 = 快捷列筛选器：点某列只看该列，点「共 N 个」恢复
    if (el.stats) el.stats.addEventListener('click', function (e) {
      var b = e.target.closest('[data-col]');
      if (!b) return;
      state.col = b.getAttribute('data-col') || '';
      render();
    });
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

    // 登录：写操作需要 token 时，把 token 换成一个 HttpOnly cookie（不在页面里留明文）
    if (el.login) el.login.addEventListener('click', loginWithToken);

    el.overlay.addEventListener('click', closeDrawer);

    // 设置弹窗：齿轮按钮 + 遮罩点击关闭 + Esc 关闭
    var settingsBtn = document.getElementById('settings-btn');
    if (settingsBtn) settingsBtn.addEventListener('click', openSettings);
    var settingsOverlay = document.getElementById('settings-overlay');
    if (settingsOverlay) settingsOverlay.addEventListener('click', closeSettings);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' || e.key === 'Esc') {
        if (state.settingsOpen) closeSettings(); else closeDrawer();
      }
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

    // 自动刷新已由 startAutoRefresh()（applyEffective 内）按 settings.refreshMs 管理，这里不再单独建定时器

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

  // ---------- 设置弹窗 ----------
  function openSettings() {
    state.settingsOpen = true;
    initEditModel();   // 每次打开都用服务端最新系统配置初始化可编辑模型
    renderSettings();
    var m = document.getElementById('settings-modal');
    m.hidden = false;
    m.setAttribute('aria-hidden', 'false');
    document.getElementById('settings-overlay').classList.add('open');
  }
  function closeSettings() {
    state.settingsOpen = false;
    var m = document.getElementById('settings-modal');
    m.hidden = true;
    m.setAttribute('aria-hidden', 'true');
    document.getElementById('settings-overlay').classList.remove('open');
  }

  /** 生成一个下拉选项串 */
  function opts(values, current) {
    return values.map(function (v) {
      return '<option value="' + v + '"' + (Number(current) === Number(v) ? ' selected' : '') + '>' + esc(String(v)) + '</option>';
    }).join('');
  }

  function optsMode(cur) {
    return ['exec', 'verify', 'manual'].map(function (m) {
      return '<option value="' + m + '"' + (m === cur ? ' selected' : '') + '>' + m + '</option>';
    }).join('');
  }

  function fieldRow(label, control) {
    return '<div class="setting-row"><div class="setting-label">' + esc(label) + '</div>' + control + '</div>';
  }

  /** 把服务端系统配置复制成可编辑模型（repos / agent.repos 由对象转成 [name,path] 数组） */
  function initEditModel() {
    var s = state.system || {};
    state.edit = {
      hub: s.hub || '',
      repos: Object.keys(s.repos || {}).map(function (k) { return { name: k, path: s.repos[k] }; }),
      agents: (s.agents || []).map(function (a) {
        return {
          name: a.name || '',
          host: a.host || '',
          mode: a.mode || 'exec',
          command: a.command || '',
          launch: a.launch || '',
          worktrees: a.worktrees || '',
          max_concurrency: a.max_concurrency || 1,
          abilities: (a.abilities || []).slice(),
          capabilities: (a.capabilities || []).slice(),
          repos: Object.keys(a.repos || {}).map(function (k) { return { name: k, path: a.repos[k] }; })
        };
      })
    };
  }

  function blankAgent() {
    return { name: '', host: '', mode: 'exec', command: '', launch: '', worktrees: '',
      max_concurrency: 1, abilities: [], capabilities: [], repos: [] };
  }

  /** 把可编辑模型序列化回服务端期望的对象结构（repos 数组 → 对象） */
  function serializeEdit() {
    var reposObj = {};
    (state.edit.repos || []).forEach(function (r) { if (r.name) reposObj[r.name] = r.path; });
    var agents = (state.edit.agents || []).map(function (a) {
      var ra = {};
      (a.repos || []).forEach(function (r) { if (r.name) ra[r.name] = r.path; });
      return {
        name: a.name, host: a.host, mode: a.mode, command: a.command || '',
        manual: a.mode === 'manual',
        abilities: (a.abilities || []).slice(), capabilities: (a.capabilities || []).slice(),
        repos: ra, launch: a.launch || '', worktrees: a.worktrees || '',
        max_concurrency: Number(a.max_concurrency) || 1
      };
    });
    return { hub: state.edit.hub || '', repos: reposObj, agents: agents };
  }

  /** 系统设置 tab：Hub 地址 + 仓库列表 + Agent 注册 + 团队看板默认 */
  function systemTabHTML() {
    var s = state.system || {};
    var reposHTML = (state.edit.repos || []).map(function (r, i) {
      return '<div class="kv-row">' +
        '<input class="sys-input" data-f="name" data-ridx="' + i + '" placeholder="仓库名" value="' + esc(r.name) + '" />' +
        '<input class="sys-input" data-f="path" data-ridx="' + i + '" placeholder="/本地/绝对路径" value="' + esc(r.path) + '" />' +
        '<button type="button" class="btn mini danger" data-rm-repo data-ridx="' + i + '" aria-label="删除仓库">✕</button>' +
      '</div>';
    }).join('');

    var agentsHTML = (state.edit.agents || []).map(function (a, i) {
      var arepos = (a.repos || []).map(function (r, j) {
        return '<div class="kv-row">' +
          '<input class="sys-input" data-f="name" data-agen="' + i + '" data-ridx="' + j + '" placeholder="仓库名" value="' + esc(r.name) + '" />' +
          '<input class="sys-input" data-f="path" data-agen="' + i + '" data-ridx="' + j + '" placeholder="/本地/绝对路径" value="' + esc(r.path) + '" />' +
          '<button type="button" class="btn mini danger" data-rm-repo data-agen="' + i + '" data-ridx="' + j + '" aria-label="删除仓库">✕</button>' +
        '</div>';
      }).join('');
      return '<div class="agent-card">' +
        '<div class="agent-head"><b>' + esc(a.name || ('agent ' + (i + 1))) + '</b>' +
          '<button type="button" class="btn mini danger" data-rm-agent="' + i + '">移除</button></div>' +
        fieldRow('名称', '<input class="sys-input" data-f="name" data-agen="' + i + '" value="' + esc(a.name) + '" />') +
        fieldRow('Host', '<input class="sys-input" data-f="host" data-agen="' + i + '" value="' + esc(a.host) + '" />') +
        fieldRow('模式', '<select class="sys-input" data-f="mode" data-agen="' + i + '">' + optsMode(a.mode) + '</select>') +
        fieldRow('启动命令', '<input class="sys-input" data-f="command" data-agen="' + i + '" value="' + esc(a.command) + '" />') +
        fieldRow('launch(GUI)', '<input class="sys-input" data-f="launch" data-agen="' + i + '" value="' + esc(a.launch) + '" />') +
        fieldRow('worktrees', '<input class="sys-input" data-f="worktrees" data-agen="' + i + '" value="' + esc(a.worktrees) + '" />') +
        fieldRow('并发数', '<input class="sys-input" type="number" min="1" data-f="max_concurrency" data-agen="' + i + '" value="' + esc(String(a.max_concurrency)) + '" />') +
        fieldRow('能力 abilities', '<input class="sys-input" data-f="abilities" data-agen="' + i + '" value="' + esc((a.abilities || []).join(', ')) + '" />') +
        fieldRow('平台 capabilities', '<input class="sys-input" data-f="capabilities" data-agen="' + i + '" value="' + esc((a.capabilities || []).join(', ')) + '" />') +
        '<div class="setting-label sub">repos</div>' +
        '<div class="kvlist">' + arepos +
          '<button type="button" class="btn mini" data-add-repo data-agen="' + i + '">+ 仓库</button></div>' +
      '</div>';
    }).join('');

    return '' +
      '<p class="muted">hive 核心属性。保存后写入本地 hive-config.json 与各 agent 配置文件（不自动 git 提交，需要同步时自行 push）。</p>' +
      '<div class="sys-section"><h3 class="sys-h">系统信息（只读）</h3>' +
        '<dl class="kv"><dt>状态文件</dt><dd>' + esc(s.status_file || '—') + '</dd>' +
        '<dt>看板目录</dt><dd>' + esc(s.dashboard_dir || '—') + '</dd>' +
        '<dt>Agent 数</dt><dd>' + (state.edit.agents || []).length + '</dd></dl></div>' +
      '<div class="sys-section"><h3 class="sys-h">Hub 地址</h3>' +
        '<input class="sys-input wide" id="sys-hub" data-f="hub" placeholder="例如 http://hub-host:8080（可留空）" value="' + esc(state.edit.hub || '') + '" />' +
        '<div class="hint">总控 / 协调中心地址，持久化到 hive-config.json 的 hub 字段。</div></div>' +
      '<div class="sys-section"><h3 class="sys-h">仓库列表（集中登记）</h3>' +
        '<div class="kvlist">' + reposHTML + '</div>' +
        '<button type="button" class="btn mini" id="sys-repo-add">+ 新增仓库</button>' +
        '<div class="hint">name = 本地绝对路径；任务按 repo 字段匹配这里的 name。</div></div>' +
      '<div class="sys-section"><h3 class="sys-h">Agent 注册</h3>' +
        (agentsHTML || '<p class="muted">（暂无已注册 agent）</p>') +
        '<button type="button" class="btn mini" id="sys-agent-add">+ 注册新 Agent</button></div>' +
      '<hr class="sep" />' +
      '<h3 class="sys-h">团队看板默认（服务端共享）</h3>' +
      teamDefaultsHTML();
  }

  /** 团队看板默认（服务端共享）区块，含保存按钮 */
  function teamDefaultsHTML() {
    var sv = (server && typeof server === 'object') ? server : {};
    function sval(k, dflt) { return (k in sv) ? sv[k] : (k in settings ? settings[k] : dflt); }
    return '' +
      fieldRow('默认刷新间隔', '<select id="sys-refresh">' + opts([5, 10, 15, 30, 60].map(function (s) { return s * 1000; }), sval('refreshMs', REFRESH_MS)) + '</select>') +
      fieldRow('默认紧凑模式', '<label class="switch"><input type="checkbox" id="sys-compact"' + (sval('compact', false) ? ' checked' : '') + ' /><span>新成员默认紧凑</span></label>') +
      fieldRow('默认显示 CI 条', '<label class="switch"><input type="checkbox" id="sys-ci"' + (sval('showCI', true) ? ' checked' : '') + ' /><span>新成员默认显示</span></label>') +
      fieldRow('默认显示验收输出', '<label class="switch"><input type="checkbox" id="sys-verify"' + (sval('showVerify', true) ? ' checked' : '') + ' /><span>新成员默认显示</span></label>') +
      fieldRow('最近完成默认数量', '<select id="sys-recent">' + opts([5, 8, 10, 15, 20], sval('recentCount', DEFAULT_RECENT_COUNT)) + '</select>') +
      '<div class="setting-row"><button type="button" class="btn primary" id="sys-save">保存系统设置</button>' +
        '<span class="hint" id="sys-msg"></span></div>';
  }

  /** 绑定系统设置 tab 内的动态行（增删 + 字段输入），避免逐键重渲染丢焦点 */
  function bindSystemTab(m) {
    Array.prototype.forEach.call(m.querySelectorAll('[data-f]'), function (inp) {
      var f = inp.getAttribute('data-f');
      var agen = inp.getAttribute('data-agen');
      var ridx = inp.getAttribute('data-ridx');
      function onInput() {
        var val = (inp.type === 'checkbox') ? inp.checked : inp.value;
        if (agen !== null) {
          var ag = state.edit.agents[Number(agen)];
          if (!ag) return;
          if (ridx !== null) {
            var rr = ag.repos[Number(ridx)];
            if (rr) rr[f] = val;
          } else {
            if (f === 'abilities' || f === 'capabilities') val = val.split(',').map(function (x) { return x.trim(); }).filter(Boolean);
            else if (f === 'max_concurrency') val = Number(val) || 1;
            ag[f] = val;
          }
        } else if (ridx !== null) {
          var r0 = state.edit.repos[Number(ridx)];
          if (r0) r0[f] = val;
        } else if (f === 'hub') {
          state.edit.hub = val;
        }
      }
      inp.addEventListener('input', onInput);
      inp.addEventListener('change', onInput);
    });

    var ra = m.querySelector('#sys-repo-add');
    if (ra) ra.addEventListener('click', function () { state.edit.repos.push({ name: '', path: '' }); renderSettings(); });
    var aa = m.querySelector('#sys-agent-add');
    if (aa) aa.addEventListener('click', function () { state.edit.agents.push(blankAgent()); renderSettings(); });

    Array.prototype.forEach.call(m.querySelectorAll('[data-rm-repo]'), function (btn) {
      btn.addEventListener('click', function () {
        var agen = btn.getAttribute('data-agen');
        var ridx = Number(btn.getAttribute('data-ridx'));
        if (agen !== null) {
          if (state.edit.agents[Number(agen)]) state.edit.agents[Number(agen)].repos.splice(ridx, 1);
        } else {
          state.edit.repos.splice(ridx, 1);
        }
        renderSettings();
      });
    });
    Array.prototype.forEach.call(m.querySelectorAll('[data-add-repo]'), function (btn) {
      btn.addEventListener('click', function () {
        var agen = Number(btn.getAttribute('data-agen'));
        if (state.edit.agents[agen]) state.edit.agents[agen].repos.push({ name: '', path: '' });
        renderSettings();
      });
    });
    Array.prototype.forEach.call(m.querySelectorAll('[data-rm-agent]'), function (btn) {
      btn.addEventListener('click', function () {
        var i = Number(btn.getAttribute('data-rm-agent'));
        var nm = (state.edit.agents[i] && state.edit.agents[i].name) || ('第 ' + (i + 1) + ' 个');
        if (window.confirm('确定移除 agent「' + nm + '」？该 agent 的配置文件将被删除。')) {
          state.edit.agents.splice(i, 1);
          renderSettings();
        }
      });
    });
  }

  function renderSettings() {
    var m = document.getElementById('settings-modal');
    var colChecks = COLUMNS.map(function (c) {
      return '<label class="switch"><input type="checkbox" data-col="' + c.key + '"' +
        (settings.columns[c.key] ? ' checked' : '') + ' /><span>' + esc(c.title) + '</span></label>';
    }).join('');

    // 系统设置 tab（Hub / 仓库列表 / Agent 注册 / 团队默认）由 systemTabHTML() 构建

    m.innerHTML =
      '<div class="modal-card">' +
        '<div class="modal-head"><h2>设置</h2>' +
          '<button type="button" class="btn icon" id="settings-close" aria-label="关闭">✕</button></div>' +
        '<div class="tabs">' +
          '<button type="button" class="tab active" data-tab="board">看板展示</button>' +
          '<button type="button" class="tab" data-tab="system">系统设置</button>' +
        '</div>' +

        // —— 看板展示（个人）——
        '<div class="tab-body" data-tab-body="board">' +
          '<div class="setting-row"><div class="setting-label">最近完成显示数量</div>' +
            '<select id="set-recent">' + opts([5, 8, 10, 15, 20], settings.recentCount) + '</select>' +
            '<div class="hint">取「已关闭」里离当前时间最近的 N 个；其余留在「已关闭」。</div></div>' +
          '<div class="setting-row"><div class="setting-label">面板可见性</div>' +
            '<div class="checks">' + colChecks + '</div>' +
            '<div class="hint">取消勾选即隐藏该面板（数据仍保留）。</div></div>' +
          '<div class="setting-row"><div class="setting-label">自动刷新间隔</div>' +
            '<select id="set-refresh">' + opts([5, 10, 15, 30, 60].map(function (s) { return s * 1000; }), settings.refreshMs) + '</select>' +
            '<div class="hint">单位秒；页面不可见时暂停刷新。</div></div>' +
          '<div class="setting-row"><div class="setting-label">紧凑模式</div>' +
            '<label class="switch"><input type="checkbox" id="set-compact"' + (settings.compact ? ' checked' : '') + ' /><span>更紧凑的卡片间距</span></label></div>' +
          '<div class="setting-row"><div class="setting-label">显示 CI 状态条</div>' +
            '<label class="switch"><input type="checkbox" id="set-ci"' + (settings.showCI ? ' checked' : '') + ' /><span>顶部显示 Runner / CI 状态</span></label></div>' +
          '<div class="setting-row"><div class="setting-label">显示验收输出</div>' +
            '<label class="switch"><input type="checkbox" id="set-verify"' + (settings.showVerify ? ' checked' : '') + ' /><span>任务详情里展示验收命令输出</span></label></div>' +
          '<p class="muted">以上为「个人设置」，仅保存在本浏览器；团队共享默认值在「系统设置」里配置。</p>' +
        '</div>' +

        // —— 系统设置（服务端共享）——
        '<div class="tab-body" data-tab-body="system" hidden>' +
          systemTabHTML() +
        '</div>' +
      '</div>';

    // —— 绑定：看板展示（个人）——
    m.querySelector('#settings-close').addEventListener('click', closeSettings);
    m.querySelector('#set-recent').addEventListener('change', function (e) {
      personal.recentCount = Number(e.target.value); savePersonal(); settings = computeEffective(); render();
    });
    m.querySelector('#set-refresh').addEventListener('change', function (e) {
      personal.refreshMs = Number(e.target.value); savePersonal(); settings = computeEffective(); applyEffective();
    });
    m.querySelector('#set-compact').addEventListener('change', function (e) {
      personal.compact = e.target.checked; savePersonal(); settings = computeEffective(); applyEffective();
    });
    m.querySelector('#set-ci').addEventListener('change', function (e) {
      personal.showCI = e.target.checked; savePersonal(); settings = computeEffective(); applyEffective();
    });
    m.querySelector('#set-verify').addEventListener('change', function (e) {
      personal.showVerify = e.target.checked; savePersonal(); settings = computeEffective(); applyEffective();
    });
    Array.prototype.forEach.call(m.querySelectorAll('[data-col]'), function (cb) {
      cb.addEventListener('change', function () {
        personal.columns = personal.columns || {};
        personal.columns[cb.getAttribute('data-col')] = cb.checked;
        savePersonal(); settings = computeEffective(); render();
      });
    });

    // —— 绑定：系统设置 tab 动态行（Hub / 仓库 / Agent 增删与字段）——
    bindSystemTab(m);

    // —— 绑定：标签页切换 ——
    Array.prototype.forEach.call(m.querySelectorAll('.tab'), function (t) {
      t.addEventListener('click', function () {
        Array.prototype.forEach.call(m.querySelectorAll('.tab'), function (x) { x.classList.remove('active'); });
        t.classList.add('active');
        var name = t.getAttribute('data-tab');
        Array.prototype.forEach.call(m.querySelectorAll('[data-tab-body]'), function (b) {
          b.hidden = b.getAttribute('data-tab-body') !== name;
        });
      });
    });

    // —— 绑定：系统设置保存（hive 核心属性 + 团队看板默认）——
    var sysSave = m.querySelector('#sys-save');
    if (sysSave) sysSave.addEventListener('click', function () {
      var d = {
        system: serializeEdit(),
        dashboard: {
          refreshMs: Number(m.querySelector('#sys-refresh').value),
          compact: m.querySelector('#sys-compact').checked,
          showCI: m.querySelector('#sys-ci').checked,
          showVerify: m.querySelector('#sys-verify').checked,
          recentCount: Number(m.querySelector('#sys-recent').value)
        }
      };
      fetch('/api/config', {
        method: 'POST', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(d)
      })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (resp) {
          var msg = m.querySelector('#sys-msg');
          if (resp && resp.system) {
            server = resp.dashboard || {};
            state.system = resp.system || state.system;
            initEditModel();              // 用服务端归一化后的值刷新可编辑模型
            settings = computeEffective(); applyEffective(); render();
            if (state.settingsOpen) renderSettings();
            var m2 = document.getElementById('sys-msg');
            if (m2) m2.textContent = '已保存到服务端';
          } else if (msg) {
            msg.textContent = '保存失败';
          }
        })
        .catch(function () { var msg = m.querySelector('#sys-msg'); if (msg) msg.textContent = '保存失败（网络）'; });
    });
  }

  // ---------- 异常卡操作按钮（终止 / 改派 / 退回未分配）----------
  var TOAST_TIMER = null;
  function flash(msg, isError) {
    var t = document.getElementById('toast');
    if (!t) return;
    t.textContent = msg;
    t.className = 'toast' + (isError ? ' is-error' : '');
    t.hidden = false;
    if (TOAST_TIMER) clearTimeout(TOAST_TIMER);
    TOAST_TIMER = setTimeout(function () { t.hidden = true; }, isError ? 5000 : 2500);
  }

  function _verb(action, override) {
    return override ||
      ({ close: '关闭作废', retry: '重试', unassign: '退回待领取',
         reopen: '重开' })[action] || action;
  }

  /** 登录：token → POST /api/login → 服务端写 HttpOnly cookie，后续写操作自动带上。 */
  function loginWithToken() {
    var token = window.prompt(
      '写操作需要 token（hive agent enroll <controller> 生成，或用 hive_whoami 里那份）。\n' +
      'token 只会换成 cookie，不在页面里存明文。\n\n粘贴 token：', '');
    if (token === null) return;
    token = token.trim();
    if (!token) return;
    flash('正在校验 token …');
    fetch('/api/login', {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ token: token })
    })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, j: j }; }); })
      .then(function (res) {
        if (res.j && res.j.ok) {
          flash('已登录：' + (res.j.agent || '') + '（' + (res.j.role || '') + '）', false);
          if (el.login) {
            el.login.textContent = '已登录：' + (res.j.agent || '');
            el.login.title = '角色：' + (res.j.role || '') + '（换 token 再点一次）';
          }
        } else {
          flash('登录失败：' + ((res.j && res.j.error) || ('HTTP ' + res.status)), true);
        }
      })
      .catch(function (e) { flash('登录请求失败：' + e.message, true); });
  }

  // 调用后端 /api/task（后端再调用 hive CLI 真正执行）
  function callTaskAction(action, tid, agent, label, note) {
    var body = { action: action, tid: tid };
    if (agent) body.agent = agent;
    if (note) body.note = note;
    flash('正在' + _verb(action, label) + ' ' + tid + ' …');
    fetch('/api/task', {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, j: j }; }); })
      .then(function (res) {
        if (res.j && res.j.ok) {
          flash('操作成功：' + tid + ' 已' + _verb(action, label), false);
          loadStatus();
        } else {
          flash('操作失败：' + ((res.j && res.j.error) || ('HTTP ' + res.status)), true);
        }
      })
      .catch(function (e) { flash('请求失败：' + e.message, true); });
  }

  function taskById(tid) {
    for (var i = 0; i < state.tasks.length; i++) {
      if (state.tasks[i].id === tid) return state.tasks[i];
    }
    return null;
  }

  /** 写操作的原因默认值：把现场带进输入框，省得人对着空白框发呆 */
  function defaultReason(action, t) {
    var bits = [];
    if (action === 'reopen') {
      bits.push('误操作/需要继续执行');
    } else {
      if (t && t.state) bits.push('原状态 ' + t.state);
      if (t && t.stale) bits.push('租约过期');
      if (t && t.result_status) bits.push('结果 ' + t.result_status);
      if (t && t.push_message) bits.push(String(t.push_message).slice(0, 80));
    }
    return bits.join('；');
  }

  /** 收集原因；返回 null 表示用户取消。reason 会写进卡面备注（可追溯）。 */
  function askReason(action, tid) {
    var t = taskById(tid);
    var verb = _verb(action);
    var head = verb + ' ' + tid;
    if (action === 'close') {
      head += '\n\n注意：这是「这张卡不做了」，不是「标记完成」。有 result 的记 done，没有的记 closed（作废）。';
    } else if (action === 'unassign') {
      head += '\n\n卡会回到待领取，谁都能再领。';
    } else if (action === 'reopen') {
      head += '\n\n会把卡拉回待领取，并撤掉归档结果（原文仍在 git 历史里）。';
    }
    var r = window.prompt(head + '\n\n请写清原因（会写进卡面备注）：', defaultReason(action, t));
    if (r === null) return null;
    r = r.trim();
    return r || '（未填原因）';
  }

  // 改派：把按钮区替换成 agent 下拉 + 确认/取消
  function openReassign(btn) {
    var tid = btn.getAttribute('data-tid');
    var agents = (state.system && state.system.agents) || [];
    var wrap = btn.parentNode;
    var opts = agents.map(function (a) {
      var nm = (a && a.name) || '';
      return '<option value="' + esc(nm) + '">' + esc(nm) + '</option>';
    }).join('');
    if (!opts) opts = '<option value="">（无可用 agent）</option>';
    wrap.innerHTML =
      '<select class="agent-pick">' + opts + '</select>' +
      '<button type="button" class="act act-ok" data-action="reassign-confirm" data-tid="' + esc(tid) + '">确认</button>' +
      '<button type="button" class="act act-cancel" data-action="reassign-cancel" data-tid="' + esc(tid) + '">取消</button>';
  }

  // 事件委托：捕获阶段处理，命中操作按钮时阻止冒泡，避免触发卡片打开抽屉
  function bindTaskActions() {
    el.columns.addEventListener('click', function (e) {
      // 依赖胶囊：点击直接定位到被依赖的任务（把搜索框填成那个 id）
      var dep = e.target.closest('[data-dep]');
      if (dep) {
        e.stopPropagation();
        e.preventDefault();
        state.query = dep.getAttribute('data-dep') || '';
        el.search.value = state.query;
        state.col = '';
        render();
        flash('已定位到 ' + state.query, false);
        return;
      }
      var btn = e.target.closest('[data-action]');
      if (!btn) return;            // 非操作按钮，照常打开抽屉
      e.stopPropagation();
      e.preventDefault();
      var action = btn.getAttribute('data-action');
      var tid = btn.getAttribute('data-tid');
      if (action === 'close' || action === 'unassign' || action === 'reopen') {
        var reason = askReason(action, tid);
        if (reason === null) return;            // 用户取消
        callTaskAction(action, tid, null, null, reason);
      } else if (action === 'retry') {
        // retry 不带 agent：保留原 owner、复用原分支重跑
        if (!window.confirm(_verb(action) + '任务 ' + tid + ' ？')) return;
        callTaskAction(action, tid);
      } else if (action === 'reassign-open') {
        openReassign(btn);
      } else if (action === 'reassign-confirm') {
        var sel = btn.parentNode.querySelector('select.agent-pick');
        var agent = sel ? sel.value : '';
        if (!agent) { flash('请选择目标 agent', true); return; }
        callTaskAction('retry', tid, agent, '改派');
      } else if (action === 'reassign-cancel') {
        render();
      }
    }, true);
  }

  // ---------- 启动 ----------
  function init() {
    cacheEls();
    buildColumns();
    initTheme();
    bindEvents();
    bindTaskActions();         // 异常卡操作按钮：终止 / 改派 / 退回未分配
    applyEffective();          // 落地主题/紧凑/CI/刷新间隔等生效设置

    state.autoRefresh = storeGet(STORE_AUTO) !== 'off';
    el.auto.checked = state.autoRefresh;

    loadStatus();
    loadCI();
    loadCluster();
    setInterval(loadCluster, CLUSTER_MS);
    loadServerConfig();        // 拉取服务端共享默认与系统只读信息
  }

  init();
})();
