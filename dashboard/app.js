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
  // 登录态缓存：只存「上次登录的 agent 名」，用来首帧直接把按钮画成「已登录」。
  // token 本身从不落 localStorage（它在 HttpOnly cookie 里），所以这里最多泄露一个名字。
  var STORE_AUTH = 'hive.auth.agent';
  var MAX_RECENT = 50;                              // 「最近完成」硬上限，防止误设过大
  var DEFAULT_RECENT_COUNT = 8;                     // 「最近完成」默认展示条数
  var STORE_SETTINGS = 'hive.settings';             // 个人设置（稀疏对象，仅存用户改过的项）

  // 六列，从左到右
  // recent（最近完成）= 已完成任务里按 finished_at 倒序的前 N 个（默认 8，可在设置里改）；
  // 其余已完成任务留在 closed（已关闭）归档。done 任务各归一列，不会重复。
  var COLUMNS = [
    { key: 'ready', title: '待领取', hint: 'state = ready 且无人抢锁' },
    { key: 'running', title: '进行中', hint: '已抢锁 / assigned / running，暂无结果' },
    { key: 'merging', title: '待合入', hint: '结果 done、代码分支已核验到位（land_ready），可跑 hive land；分支缺失的卡会进异常列' },
    { key: 'abnormal', title: '异常', hint: 'failed / blocked / 租约过期 / 不可合入；卡片上带责任 agent 与时间预算' },
    { key: 'recent', title: '最近完成', hint: '已完成中按完成时间倒序的前 N 个（数量可在设置里改）' },
    { key: 'closed', title: '已关闭', hint: '其余已完成的任务（默认折叠）', collapsible: true }
  ];
  var COL_TITLE = {};
  COLUMNS.forEach(function (c) { COL_TITLE[c.key] = c.title; });
  // 搁置不是第 7 列，但也得有个名字给 aria-label / 详情用
  COL_TITLE.shelved = '搁置';

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
          // 后端代码比进程新（改了 serve.py 没重启）时横幅提示：这类「页面
          // 是新代码、接口是旧路由」的 404 光看页脚根本判断不出来。
          var svc = d.system && d.system.service;
          if (svc && svc.restart_hint && !state.staleWarned) {
            state.staleWarned = true;
            state.error = svc.restart_hint;
            render();
          }
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
    shelfOpen: false,     // 搁置面板默认收起：平时忽略，点开才看
    zone: 'shelf',        // 搁置 / 计划 同一区域的 Tab：shelf | plan
    planOpen: false,      // 计划面板是否展开（切到计划 Tab 时默认展开）
    plans: [],            // GET /api/plans 的计划数组（服务端已排序）
    planCounts: { active: 0, completed: 0, closed: 0 },
    planLoading: false,
    planError: '',
    planEditing: null,    // 正在编辑的计划 id；null = 新建
    staleWarned: false,   // 后端代码过期提示只弹一次
    loading: true,
    error: '',
    lastUpdated: null,
    source: '/api/status',
    snapshotAt: null,   // 快照生成时刻（毫秒），来自 X-Snapshot-Mtime；null = 服务端没给
    cluster: null,      // /api/cluster 的集群视图（leader/term/在线节点）
    clusterOpen: false, // 集群面板是否展开（默认折叠）
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
      // —— 搁置现场：旧快照没有这三个字段，空串即可，搁置面板会显示「未填原因」——
      shelved_at: t.shelved_at || '',
      shelved_from: t.shelved_from || '',
      shelve_reason: t.shelve_reason || '',
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
      push_source: t.push_source || '',
      // —— 状态一致性 / 异常定位 / 时间预算 ——
      // normalize 是白名单：漏一个字段，前端就永远拿到 undefined。
      // land_ready 必须保留三态（true/false/null），老后端没有该字段时按旧行为。
      land_ready: (t.land_ready === false) ? false
                  : (t.land_ready === true) ? true : null,
      land_block: t.land_block || '',
      local_stale: !!t.local_stale,
      stale_suspected: !!t.stale_suspected,
      result_agent: t.result_agent || '',
      abnormal_agent: t.abnormal_agent || '',
      timeout_minutes: Number(t.timeout_minutes) || 0,
      lease_minutes: Number(t.lease_minutes) || 0,
      budget_score: Number(t.budget_score) || 0,
      budget_timeout: Number(t.budget_timeout) || 0,
      budget_lease: Number(t.budget_lease) || 0,
      budget_note: t.budget_note || ''
    };
  }

  function ownerOf(t) { return t.owner || t.claim_agent || ''; }

  /** 租约过期：服务端判定为 true，或租约已到点且任务还没有结果 */
  function isStale(t) {
    if (t.stale) return true;   // 服务端已按心跳豁免过，这里直接采信
    var lease = parseTime(t.lease_until);
    if (lease === null || t.result_status) return false;
    // 前端兜底：worker 刚上报过进度就说明还在跑，租约刚过期也可能只是
    // 心跳/续租晚了一点。给 15 分钟宽限，别把在跑的任务丢进异常列。
    if (t.pushed_at) {
      var pushed = parseTime(t.pushed_at);
      if (pushed !== null && (Date.now() - pushed) < 15 * 60 * 1000) return false;
    }
    return lease < Date.now();
  }

  /** 列的单向判定，一张卡只落一列 */
  function classify(t) {
    // 搁置必须最先判：它不属于任何活跃列，也不能被「有结果 / 租约过期」
    // 拽进异常列。漏了这条会落到最后 return 'ready'，正是搁置要避免的。
    if (t.state === 'shelved') return 'shelved';
    if (t.state === 'done' || t.state === 'closed') {
      // done=真完成（最近完成的归 recent），closed=关闭作废（没产出结果就关了）
      // 两者都归档到「已关闭」。少判 closed 会让作废的卡掉进「待领取」，看着像还要人干。
      if (t.state === 'done' && state.recentIds && state.recentIds[t.id]) return 'recent';
      return 'closed';
    }
    if (t.result_status === 'failed' || t.state === 'blocked' || isStale(t)) return 'abnormal';
    if (t.result_status === 'done') {
      // 「结果 JSON 写了 done」不等于「代码分支到了协同服务器」。后端
      // land_ready=false 时若仍放进「待合入」，人会复制一条必然 blocked 的
      // 合入指令（T-0320 / T-0324 / T-0326 反复踩过）。land_ready 为 undefined
      // 表示老版本后端没给这个字段，按旧行为处理，避免整列消失。
      if (t.land_ready === false) return 'abnormal';
      return 'merging';
    }
    if (t.claim_agent || t.state === 'assigned' || t.state === 'running') return 'running';
    return 'ready';
  }

  /** 搁置排序：最近搁置的在前（捡起来时先看新的），无时间沉底，再按 id */
  function sortShelved(a, b) {
    var ta = parseTime(a.shelved_at), tb = parseTime(b.shelved_at);
    if (ta === null && tb === null) return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0);
    if (ta === null) return 1;
    if (tb === null) return -1;
    return tb - ta;
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
    el.upgradeBar = document.getElementById('upgrade-bar');
    el.upgradeVersion = document.getElementById('upgrade-version');
    el.upgradeCheck = document.getElementById('upgrade-check');
    el.upgradeApply = document.getElementById('upgrade-apply');
    el.upgradePublish = document.getElementById('upgrade-publish');
    el.banner = document.getElementById('banner');

    el.panel = document.getElementById('state-panel');
    el.columns = document.getElementById('columns');
    // 搁置面板（独立于 .columns：不占列宽，也不参与列显隐设置）
    el.shelf = document.getElementById('shelf');
    el.shelfToggle = document.getElementById('shelf-toggle');
    el.shelfBody = document.getElementById('shelf-body');
    el.shelfList = document.getElementById('shelf-list');
    el.shelfEmpty = document.getElementById('shelf-empty');
    el.shelfCount = document.getElementById('shelf-count');
    el.shelfHit = document.getElementById('shelf-hit');
    // 计划面板（与搁置同一区域的 Tab）
    el.planToggle = document.getElementById('plan-toggle');
    el.planBody = document.getElementById('plan-body');
    el.planCount = document.getElementById('plan-count');
    el.planActiveList = document.getElementById('plan-active-list');
    el.planDoneList = document.getElementById('plan-done-list');
    el.planActiveEmpty = document.getElementById('plan-active-empty');
    el.planDoneEmpty = document.getElementById('plan-done-empty');
    el.planActiveCount = document.getElementById('plan-active-count');
    el.planDoneCount = document.getElementById('plan-done-count');
    el.planLoading = document.getElementById('plan-loading');
    el.planError = document.getElementById('plan-error');
    el.planNew = document.getElementById('plan-new');
    el.planModal = document.getElementById('plan-modal');
    el.planOverlay = document.getElementById('plan-overlay');
    el.planForm = document.getElementById('plan-form');
    el.planFormError = document.getElementById('plan-form-error');
    el.repo = document.getElementById('repo-filter');
    el.owner = document.getElementById('owner-filter');
    el.prio = document.getElementById('prio-filter');
    el.unowned = document.getElementById('unowned-filter');
    el.search = document.getElementById('search');
    el.auto = document.getElementById('auto-refresh');
    el.refresh = document.getElementById('refresh-now');
    el.loadFile = document.getElementById('load-file');
    el.login = document.getElementById('login-btn');
    el.logout = document.getElementById('logout-btn');
    el.fileInput = document.getElementById('file-input');
    el.theme = document.getElementById('theme-toggle');
    el.overlay = document.getElementById('overlay');
    el.drawer = document.getElementById('drawer');
    // 留言面板
    el.chatFab = document.getElementById('chat-fab');
    el.chatBadge = document.getElementById('chat-badge');
    el.chatPanel = document.getElementById('chat-panel');
    el.chatClose = document.getElementById('chat-close');
    el.chatScope = document.getElementById('chat-scope');
    el.chatKind = document.getElementById('chat-kind');
    el.chatFeed = document.getElementById('chat-feed');
    el.chatForm = document.getElementById('chat-form');
    el.chatInput = document.getElementById('chat-input');
    el.chatSend = document.getElementById('chat-send');
    el.chatSrc = document.getElementById('chat-src');
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

  /**
   * 搁置面板做成「伪列」：复用 syncList 的增量渲染，但刻意不进 COLUMNS ——
   * 它不占列宽、不进设置里的列显隐，也不参与顶部「只看该列」筛选。
   *
   * 计划与搁置共用同一个 section，靠 Tab 切换显示哪一个：这样既满足「同一区域
   * 的 Tab」，又不动搁置原有的折叠语义（点搁置 Tab 仍是展开/收起，不是切走）。
   */
  function buildShelf() {
    cols.shelf = { root: el.shelf, body: el.shelfList, empty: el.shelfEmpty,
                   count: el.shelfCount, list: [], toggle: null, more: null,
                   overflow: 0 };
    if (el.shelfToggle) {
      el.shelfToggle.addEventListener('click', function () {
        if (state.zone !== 'shelf') { setZone('shelf'); return; }
        state.shelfOpen = !state.shelfOpen;
        render();
      });
    }
    if (el.planToggle) {
      el.planToggle.addEventListener('click', function () {
        if (state.zone !== 'plan') { setZone('plan'); return; }
        state.planOpen = !state.planOpen;
        render();
      });
    }
  }

  /** 切换 Tab：进计划顺手展开并拉一次数据；进搁置保持它原来的开合状态。 */
  function setZone(zone) {
    state.zone = zone === 'plan' ? 'plan' : 'shelf';
    if (state.zone === 'plan') {
      state.planOpen = true;
      // 每次进来都拉一次：计划进度依赖任务快照，页面可能已经放了很久。
      loadPlans({ quiet: true });
    } else {
      state.shelfOpen = true;
    }
    render();
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
    conflict: '冲突', blocked: '挂起', shelved: '搁置'
  };
  var STAGE_ORDER = ['pending', 'dispatched', 'claimed', 'running',
                     'done', 'failed', 'conflict', 'blocked', 'shelved'];

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

  /**
   * 搁置卡用紧凑布局：重点不是「谁在做什么」，而是「为什么停、什么时候停、
   * 现在能怎么捡起来」。不显示进度条 / 租约 / 验收结果（那些都已被释放）。
   */
  function shelfCardInner(t) {
    var h = '<div class="card-row card-head-row">' +
      '<span class="tid strong">' + esc(t.id) + '</span>' + prioBadge(t) +
      '<span class="badge shelf-badge">搁置</span></div>';
    h += '<h3 class="card-title">' + esc(t.title) + '</h3>';
    h += '<div class="shelf-why">' + esc(t.shelve_reason || '（未填原因）') + '</div>';

    h += '<div class="card-time">';
    var at = parseTime(t.shelved_at);
    h += at !== null ? ('搁置于 ' + relTime(at) + ' · ' + dtf.format(at)) : '搁置时间未知';
    if (t.shelved_from) h += ' · 原状态 ' + esc(t.shelved_from);
    h += '</div>';

    h += '<div class="card-tags"><span class="pill pill-repo">' +
         esc(t.repo || '—') + '</span></div>';

    // 「复制说明」拿到的不是派发指令，而是一段明确写着「不要领取」的搁置说明
    var chatN = chatCountFor(t.id);
    h += '<div class="card-copy">' +
      '<button type="button" class="act act-copy" data-copy="' + esc(t.id) +
      '" title="复制搁置说明：原因 + 三个出口 + 不要领取">复制说明</button>' +
      '<button type="button" class="act act-chat" data-chat="' + esc(t.id) +
      '" title="打开这张卡的留言线程">讨论' +
      (chatN ? '<span class="n">' + chatN + '</span>' : '') + '</button></div>';

    // 三个出口：放回待领取 / 变成另外的任务 / 废弃
    h += '<div class="card-actions">' +
      actBtn('unshelve', t.id, '放回待领取', 'act-ok') +
      actBtn('respawn', t.id, '另立新卡', 'act-reassign') +
      actBtn('close', t.id, '废弃', 'act-close') +
      '</div>';
    return h;
  }

  function cardInner(t) {
    if (classify(t) === 'shelved') return shelfCardInner(t);
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

    // 结果写了 done 但代码分支没到协同服务器时，直接把原因写在卡上，
    // 免得人拿着「复制合入指令」去跑一条必然 blocked 的命令。
    if (t.land_ready === false && t.land_block) {
      h += '<div class="card-meta">合入受阻：' + esc(t.land_block) + '</div>';
    }

    // 异常卡必须一眼看到「找谁」：优先抢锁 agent，其次 owner / 派单对象 /
    // 结果里记的交付 agent。没有就明说没登记，别让人对着空卡猜。
    if (col === 'abnormal') {
      var who = t.abnormal_agent || ownerOf(t) || t.result_agent || t.dispatched_to || '';
      h += '<div class="card-meta">责任 agent：' +
           (who ? esc(who) : '（未登记：卡面无 owner / claim / 派单对象）') +
           '</div>';
      if (t.timeout_minutes && t.budget_timeout > t.timeout_minutes) {
        h += '<div class="card-meta">超时偏紧：' + t.timeout_minutes +
             ' 分钟（复杂度建议 ' + t.budget_timeout + ' 分钟）</div>';
      }
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

    // 复制指令：每张卡都有（含已完成/已关闭），只复制文本，不领卡不改状态。
    // 待合入的卡复制的是「给主控 agent 的合入指令」（让它跑 hive land），
    // 其余状态复制派发指令 —— 文案跟着语义走，别再让主控收到一句「请领取任务」。
    var chatN = chatCountFor(t.id);
    var isMerging = col === 'merging';
    h += '<div class="card-copy">' +
      '<button type="button" class="act act-copy" data-copy="' + esc(t.id) +
      '" title="' + (isMerging
        ? '把合入指令复制到剪贴板，粘给主控 agent 执行 hive land 合入'
        : '把这张卡的派发指令复制到剪贴板，粘到 agent 对话框即可') + '">' +
      (isMerging ? '复制合入指令' : '复制派发指令') + '</button>' +
      '<button type="button" class="act act-chat" data-chat="' + esc(t.id) +
      '" title="打开这张卡的留言线程：留提示 / 看 agent 的疑问">讨论' +
      (chatN ? '<span class="n">' + chatN + '</span>' : '') + '</button>' +
      '</div>';

    // 操作按钮。
    // 「终止」曾经直接等同于 close，结果把没跑完的卡标成 done（T-0181 的教训），
    // 现在拆成两个语义清楚的动作：
    //   unassign=终止本次执行、放回待领取（想继续做就选这个）
    //   close   =这张卡不做了（有结果记 done，没结果记 closed 作废）
    var isActive = (col === 'abnormal' || col === 'running' ||
                    col === 'ready' || col === 'merging');
    if (isActive || col === 'closed' || col === 'recent') {
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
      } else if (col === 'closed' || col === 'recent') {
        h += actBtn('reopen', t.id, '重开', 'act-reopen');
      }
      // 搁置入口只给活跃列：当前做不了、退回待领取又会占位置时用。
      // 已关闭 / 搁置中的卡不需要这个按钮。
      if (isActive) h += actBtn('shelve', t.id, '搁置', 'act-shelve');
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
          // mode 由服务端按卡的状态定：merge=给主控的合入指令，abnormal=附带溯源块
          var msg = body.mode === 'shelved'
            ? '已复制 ' + tid + ' 的搁置说明（写着不要领取），别贴给 agent 派活'
            : (body.mode === 'merge'
              ? '已复制 ' + tid + ' 的合入指令，切到主控 agent 粘贴即可'
              : (body.mode === 'abnormal'
                ? '已复制 ' + tid + ' 的派发指令（含异常溯源信息），切到 agent 粘贴即可'
                : '已复制 ' + tid + ' 的派发指令，切到 agent 粘贴即可'));
          flash(msg);
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
    // 「讨论」同理：必须先于 openDrawer 拦下，否则点一下就变成打开详情
    card.addEventListener('click', function (e) {
      var btn = e.target && e.target.closest ? e.target.closest('[data-chat]') : null;
      if (!btn || !card.contains(btn)) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      openChat('task:' + (btn.getAttribute('data-chat') || t.id));
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
    merging: '当 Worker 完成并推送 res/ 分支后，这里会自动出现任务卡；请用 CLI 运行 hive land 合入。',
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

  function renderStats(counts, shelvedCount) {
    // 统计条既是仪表盘也是快捷筛选器：点某列只看该列，再点「共 N 个」恢复全部。
    // 搁置卡不计入「共 N 个任务」：它不算挂账，混进去会让活跃盘子看着虚高。
    var active = Math.max(0, state.filtered.length - (shelvedCount || 0));
    var html = '<button type="button" class="stat stat-total' + (state.col ? '' : ' active') +
      '" data-col="" title="显示全部列（不含搁置）">共 <b>' + active + '</b> 个任务</button>';
    COLUMNS.forEach(function (c) {
      if (!settings.columns[c.key]) return;   // 隐藏的面板不计入统计条
      html += '<button type="button" class="stat' +
        (c.key === 'abnormal' ? ' is-abnormal' : '') +
        (state.col === c.key ? ' active' : '') +
        '" data-col="' + esc(c.key) + '" title="只看「' + esc(c.title) + '」">' +
        esc(c.title) + ' <b>' + (counts[c.key] || 0) + '</b></button>';
    });
    // 搁置入口常驻但低调：不参与列筛选、不用告警色，只负责「别忘了」。
    html += '<button type="button" class="stat stat-shelf' +
      (state.zone !== 'plan' && state.shelfOpen ? ' active' : '') + '" data-shelf="1"' +
      ' title="切到搁置区（' + shelvedCount +
      ' 张，不计入活跃统计，也不会被 agent 领取）">' +
      '搁置 <b>' + shelvedCount + '</b></button>';
    // 计划入口同样常驻：点它切到计划的 Tab（不占列筛选）
    var planActive = (state.planCounts && state.planCounts.active) || 0;
    html += '<button type="button" class="stat stat-plan' +
      (state.zone === 'plan' ? ' active' : '') + '" data-plan="1"' +
      ' title="切到计划区（进行中 ' + planActive + ' 个）">计划 <b>' + planActive + '</b></button>';
    el.stats.innerHTML = html;
  }

  /** 搁置面板：默认只露一条横条，展开才渲染卡片列表 */
  function renderShelf(list) {
    var c = cols.shelf;
    // 浏览器缓存了旧 index.html（没有搁置区）时别把整页 JS 打崩
    if (!c || !el.shelf || !el.shelfBody || !c.count) return;
    c.list = list;
    c.count.textContent = list.length;
    c.empty.textContent = '没有搁置的任务。';
    var onShelf = state.zone !== 'plan';
    if (el.shelfToggle) {
      el.shelfToggle.setAttribute('aria-selected', String(onShelf));
      el.shelfToggle.setAttribute('aria-expanded', String(onShelf && !!state.shelfOpen));
      el.shelfToggle.classList.toggle('active', onShelf);
    }
    if (el.planToggle) {
      el.planToggle.setAttribute('aria-selected', String(!onShelf));
      el.planToggle.setAttribute('aria-expanded', String(!onShelf && !!state.planOpen));
      el.planToggle.classList.toggle('active', !onShelf);
    }
    if (el.shelfBody) el.shelfBody.hidden = !(onShelf && state.shelfOpen);
    if (el.planBody) el.planBody.hidden = !(!onShelf && state.planOpen);
    el.shelf.classList.toggle('is-open', !onShelf ? !!state.planOpen : !!state.shelfOpen);
    el.shelf.classList.toggle('has-items', list.length > 0);
    el.shelf.classList.toggle('is-plan', !onShelf);
    // 正在筛选 / 搜索，且命中了搁置卡：折叠时给个提示，别让人以为卡丢了
    var filtering = !!(state.query.trim() || state.repo || state.owner ||
                       state.prio || state.unowned);
    if (el.shelfHit) {
      el.shelfHit.hidden = !(onShelf && !state.shelfOpen && list.length > 0 && filtering);
    }
    // 沿用原行为：只有展开时才铺卡片（收起态不占 DOM，也避免与「卡在别的列」
    // 混淆）。切回搁置 Tab 会重新 sync，卡片即时回来。
    if (onShelf && state.shelfOpen) syncList(c);
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
    renderUpgrade();   // 版本条随状态刷新一起重绘，不额外发请求
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
    } else if (state.tasks.some(function (t) { return t.local_stale; })) {
      // status 读的是本地工作区卡面；本机落后 origin/main 时，看板会把旧的
      // done / closed 当成真相，多机显示互相打架（T-0324）。
      el.banner.innerHTML = '<span>本地协调仓落后 origin/main，卡面状态可能不是最新；'
        + '在协调仓执行 git pull（或 hive sync-repos）后刷新看板</span>';
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
      // 搁置原因也进搜索域：回忆一张封存卡时，往往只记得「为什么停」
      var hay = (t.id + ' ' + t.title + ' ' + t.summary + ' ' + t.result_summary +
                 ' ' + t.shelve_reason).toLowerCase();
      return hay.indexOf(q) !== -1;
    });
  }

  function render() {
    applyFilters();
    renderFilters();
    renderBanner();
    renderBoard();     // 早退分支（loading/error/empty）也要走，旧快照在这里静默隐藏
    renderChatScopes(); // 任务/agent 列表变了，留言频道下拉跟着重算（内部按签名去抖）

    // 计划与任务相互独立：任务空/加载失败时，计划 Tab 仍要能打开看。
    if (state.loading && !state.tasks.length) {
      showPanel('loading'); renderStats({}); renderShelf([]); renderPlans(); return;
    }
    if (!state.tasks.length && state.error) {
      showPanel('error', state.error); renderStats({}); renderShelf([]); renderPlans(); return;
    }
    if (!state.tasks.length) {
      showPanel('empty'); renderStats({}); renderShelf([]); renderPlans(); return;
    }
    hidePanel();

    // 计算「最近完成」集合：已完成任务按 finished_at 倒序取前 N 个，其余留在「已关闭」
    var doneSorted = state.filtered.filter(function (t) { return t.state === 'done'; })
      .slice().sort(sortRecent);
    state.recentIds = {};
    var n = Math.min(settings.recentCount, doneSorted.length);
    for (var i = 0; i < n; i++) state.recentIds[doneSorted[i].id] = true;

    var groups = {};
    COLUMNS.forEach(function (c) { groups[c.key] = []; });
    // 搁置卡留在 state.filtered 里（这样搜索能搜到、统计能算到），
    // 但不进任何一列 —— 分列时单独接走，交给折叠面板。
    var shelvedList = [];
    state.filtered.forEach(function (t) {
      var key = classify(t);
      if (key === 'shelved') shelvedList.push(t);
      else if (groups[key]) groups[key].push(t);
    });
    shelvedList.sort(sortShelved);

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

    renderShelf(shelvedList);
    renderPlans();       // 任务的 done 状态变了，进度条要跟着变
    renderStats(counts, shelvedList.length);
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
    // 计划进度是从任务快照派生的：任务变了就顺手刷一次计划（服务端会重算
    // 完成数并可能自动完成），否则进度条要等到下次切 Tab 才更新。
    loadPlans({ quiet: true });
    // 集群面板里的「任务数」来自 /api/status，任务刷新时同步重算（不额外请求）
    if (state.cluster) renderCluster(state.cluster);
  }

  // ---------- 常驻集群状态（5s 一次，与任务快照解耦） ----------
  var CLUSTER_MS = 5000;
  // 后端 snapshot 目前没下发 peer_timeout / dead_timeout；缺失时的估算档位。
  // 节点记录里已有后端算好的 status，优先用它，只有旧快照缺 status 才按这两个数估。
  var CLUSTER_EST_PEER_S = 15;
  var CLUSTER_EST_DEAD_S = 60;

  // —— 纯函数：不碰 DOM，tests/test_dashboard_cluster.py 用 node 直接断言渲染结果 ——
  function clusterAgeSeconds(epoch, now) {
    var seen = Number(epoch);
    var at = Number(now);
    if (!isFinite(seen) || seen <= 0 || !isFinite(at)) return null;
    return Math.max(0, Math.round(at - seen));
  }

  /** 心跳 age：3s / 2m / 1h；没有 last_seen_epoch 返回 —（不伪造时间） */
  function clusterAge(epoch, now) {
    var s = clusterAgeSeconds(epoch, now);
    if (s === null) return '—';
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    return Math.floor(s / 86400) + 'd';
  }

  function clusterThresholds(snap) {
    var peer = Number(snap && snap.peer_timeout);
    var dead = Number(snap && snap.dead_timeout);
    var guessed = !(isFinite(peer) && peer > 0 && isFinite(dead) && dead > peer);
    if (guessed) {
      peer = CLUSTER_EST_PEER_S;
      dead = CLUSTER_EST_DEAD_S;
    }
    return { peer: peer, dead: dead, guessed: guessed };
  }

  /** 优先用后端 status（已按 peer_timeout/dead_timeout 分档）；缺失时才按 age 估。 */
  function clusterNodeStatus(node, now, peer, dead) {
    var st = String((node && node.status) || '');
    if (st === 'alive' || st === 'suspect' || st === 'dead') return st;
    var age = clusterAgeSeconds(node && node.last_seen_epoch, now);
    if (age === null) return 'dead';
    if (age <= peer) return 'alive';
    if (age <= dead) return 'suspect';
    return 'dead';
  }

  function clusterStatusView(status) {
    if (status === 'alive') return { cls: 'alive', label: '在线' };
    if (status === 'suspect') return { cls: 'suspect', label: '可疑' };
    return { cls: 'dead', label: '离线' };
  }

  function clusterEndpoint(leader) {
    if (!leader) return '';
    if (leader.url) return String(leader.url);
    if (leader.host && leader.port) return 'http://' + leader.host + ':' + leader.port;
    return '';
  }

  /** 在飞卡 = owner 对得上，且还没 done / closed / blocked / failed。 */
  function clusterTaskCount(tasks, name) {
    if (!name || !Array.isArray(tasks)) return 0;
    var n = 0;
    tasks.forEach(function (t) {
      if (!t) return;
      if ((t.owner || t.claim_agent || '') !== name) return;
      if (t.state === 'done' || t.state === 'closed' || t.state === 'blocked') return;
      if (t.result_status === 'failed') return;
      n++;
    });
    return n;
  }

  function renderClusterSummary(snap, open) {
    snap = snap || {};
    var leader = snap.leader || {};
    var self = snap.self || {};
    var term = snap.term || 0;
    var alive = snap.alive || 0;
    var known = snap.known || 0;
    var txt = '集群：' + (leader.name || '无主') + ' · term ' + term + ' · ' + alive + '/' + known;
    if (snap.role === 'leader' || (self.id && leader.id === self.id)) {
      txt = '集群：本机主节点 · term ' + term + ' · ' + alive + '/' + known;
    }
    return '<button type="button" class="cluster-toggle" data-cluster-toggle ' +
      'aria-expanded="' + (open ? 'true' : 'false') + '" aria-controls="cluster-panel">' +
      esc(txt) + '<span class="cluster-caret" aria-hidden="true">' +
      (open ? '▾' : '▸') + '</span></button>';
  }

  function renderClusterPanel(snap, tasks, now) {
    snap = snap || {};
    var leader = snap.leader || {};
    var self = snap.self || {};
    var thresholds = clusterThresholds(snap);
    var endpoint = clusterEndpoint(leader);
    var html = '<div class="cluster-panel" id="cluster-panel" role="region" aria-label="集群节点">';

    html += '<div class="cluster-head">' +
      '<div class="cluster-leader"><span class="cluster-kicker">主节点</span>' +
      '<b class="cluster-leader-name">' + esc(leader.name || '无主') + '</b>';
    if (endpoint) {
      html += '<code class="cluster-addr mono">' + esc(endpoint) + '</code>' +
        '<button type="button" class="btn mini cluster-copy" data-cluster-copy="' +
        esc(endpoint) + '">复制</button>';
    } else {
      html += '<span class="cluster-addr muted">未上报地址</span>';
    }
    html += '</div><dl class="cluster-facts">' +
      '<div><dt>term</dt><dd>' + esc(snap.term || 0) + '</dd></div>' +
      '<div><dt>quorum</dt><dd>' + esc(snap.quorum || 0) + '</dd></div>' +
      '<div><dt>集群 ID</dt><dd>' + esc(snap.cluster_id || '—') + '</dd></div>' +
      '</dl></div>';

    html += '<table class="cluster-nodes"><thead><tr>' +
      '<th>节点</th><th>角色</th><th>状态</th><th>心跳</th><th>任务</th><th>版本</th>' +
      '</tr></thead><tbody>';
    var nodes = Array.isArray(snap.nodes) ? snap.nodes : [];
    if (!nodes.length) {
      html += '<tr><td colspan="6" class="cluster-empty">快照里没有节点</td></tr>';
    } else {
      nodes.forEach(function (n) {
        n = n || {};
        var status = clusterNodeStatus(n, now, thresholds.peer, thresholds.dead);
        var view = clusterStatusView(status);
        var role = n.role || (leader.id && n.id === leader.id ? 'leader' : 'follower');
        var isSelf = !!(self.id && n.id === self.id);
        var age = clusterAge(n.last_seen_epoch, now);
        html += '<tr class="cluster-node">' +
          '<td class="cluster-name"><b>' + esc(n.name || n.id || '?') + '</b>' +
          (isSelf ? '<span class="cluster-self">本机</span>' : '') + '</td>' +
          '<td class="cluster-role">' + esc(role) + '</td>' +
          '<td><span class="cluster-status ' + view.cls + '"><span class="cluster-lamp"></span>' +
          view.label + '</span></td>' +
          '<td><span class="cluster-age mono" title="最后心跳 ' + esc(age) + ' 前">' +
          esc(age) + '</span></td>' +
          '<td><span class="cluster-tasks">' +
          clusterTaskCount(tasks, n.name || n.id) + '</span></td>' +
          '<td class="cluster-version mono">' + esc(n.version || '—') + '</td>' +
          '</tr>';
      });
    }
    html += '</tbody></table><p class="cluster-note">';
    if (thresholds.guessed) {
      html += '后端未下发 peer_timeout / dead_timeout，缺失状态的节点按 15s / 60s 估。';
    } else {
      html += '状态灯按后端下发的 peer_timeout / dead_timeout 分档。';
    }
    html += '数据源 /api/cluster，任务数来自 /api/status。</p></div>';
    return html;
  }

  function renderCluster(data) {
    if (!el.cluster) return;
    var running = !!(data && data.running);
    var snap = (data && data.state) || {};
    var leader = snap.leader || {};
    if (!running) {
      state.clusterOpen = false;
      el.cluster.className = 'cluster off';
      el.cluster.innerHTML = '<span class="cluster-summary">集群：未启动</span>';
      el.cluster.title = '本机没有运行 hive cluster serve；用 hive cluster start 接入集群';
      return;
    }
    var open = !!state.clusterOpen;
    var alive = snap.alive || 0;
    var known = snap.known || 0;
    el.cluster.className = 'cluster ' + (leader.id ? 'ok' : 'warn') + (open ? ' is-open' : '');
    el.cluster.innerHTML = renderClusterSummary(snap, open) +
      (open ? renderClusterPanel(snap, state.tasks, Date.now() / 1000) : '');
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
      '<button type="button" class="act act-copy" data-copy="' + esc(t.id) + '">' +
      (col === 'shelved' ? '复制搁置说明'
        : (col === 'merging' ? '复制合入指令' : '复制派发指令')) + '</button>' +
      '<button type="button" class="act act-chat" data-chat="' + esc(t.id) + '">讨论' +
      (chatCountFor(t.id) ? '<span class="n">' + chatCountFor(t.id) + '</span>' : '') +
      '</button>' +
      '</div>';

    // 这张卡的留言线程：人给 agent 的指点、agent 提出的疑问都在这儿
    var tmsgs = chatMessagesFor('task:' + t.id);
    if (tmsgs.length) {
      h += '<section class="block"><h3>讨论（' + tmsgs.length + '）</h3>';
      h += tmsgs.slice(-8).map(chatMsgHTML).join('');
      h += '</section>';
    }

    h += kvBlock('概览', [
      ['所在列', COL_TITLE[col]],
      ['state', t.state],
      ['派发阶段', t.stage ? stageLabel(t.stage) : ''],
      ['已推送给', t.dispatched_to],
      ['仓库', t.repo],
      ['owner', ownerOf(t) || ''],
      ['抢锁 agent', t.claim_agent || ''],
      ['结果 agent', t.result_agent || ''],
      ['责任 agent', t.abnormal_agent || ownerOf(t) || t.result_agent || ''],
      ['commit', t.head, true],
      ['耗时', formatDuration(t.duration_s)],
      ['依赖', t.depends_on.length ? t.depends_on.join('、') : '']
    ]);

    h += kvBlock('时间预算', [
      ['执行超时', t.timeout_minutes ? t.timeout_minutes + ' 分钟' : ''],
      ['租约', t.lease_minutes ? t.lease_minutes + ' 分钟' : ''],
      ['复杂度评分', String(t.budget_score || 0)],
      ['按复杂度建议', t.budget_timeout
        ? t.budget_timeout + ' / ' + t.budget_lease + ' 分钟（超时 / 租约）' : ''],
      ['估算依据', t.budget_note || '']
    ]);

    // 搁置卡点开详情，第一眼就要看到「为什么停」和「以后怎么办」
    if (col === 'shelved') {
      h += kvBlock('搁置', [
        ['原因', t.shelve_reason || '（未填原因）'],
        ['搁置于', t.shelved_at],
        ['搁置前状态', t.shelved_from],
        ['运行时文件', '已释放（租约 / 结果 / 进度 / 收件箱信号）'],
        ['日后处置', '放回待领取 · 另立新卡 · 废弃']
      ]);
    }

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

  /** 抽屉里的「复制派发指令」/「讨论」：抽屉不套在卡片里，单独绑一次即可 */
  function bindDrawerCopy() {
    var btn = el.drawer.querySelector('[data-copy]');
    if (btn) {
      btn.addEventListener('click', function () {
        copyDispatch(btn.getAttribute('data-copy') || drawerId, btn);
      });
    }
    var chatBtn = el.drawer.querySelector('[data-chat]');
    if (chatBtn) {
      chatBtn.addEventListener('click', function () {
        openChat('task:' + (chatBtn.getAttribute('data-chat') || drawerId));
      });
    }
  }

  function openDrawer(id) {
    var t = findTask(id);
    if (!t) return;
    drawerId = id;
    lastFocus = document.activeElement;
    el.drawer.innerHTML = drawerHTML(t);
    bindDrawerCopy();
    el.drawer.classList.add('open');
    document.body.classList.add('drawer-open');   // 浮标让位，别压在详情上
    el.drawer.setAttribute('aria-hidden', 'false');
    el.overlay.classList.add('open');
    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
    document.getElementById('drawer-close').focus();
  }

  /** 抽屉开着时数据刷新了，就地更新内容（不打断阅读位置以外的状态）
   *
   *  `force`：留言变化时用。平时怕把焦点弄丢会跳过重建（焦点常在关闭按钮上），
   *  但讨论区必须跟着新留言更新，这时重建后把焦点放回关闭按钮即可。
   */
  function refreshDrawer(force) {
    var t = findTask(drawerId);
    if (!t) return;
    if (!force && el.drawer.contains(document.activeElement)) return;
    var scroll = el.drawer.querySelector('.drawer-body');
    var top = scroll ? scroll.scrollTop : 0;
    var hadFocus = el.drawer.contains(document.activeElement);
    el.drawer.innerHTML = drawerHTML(t);
    bindDrawerCopy();
    var close = document.getElementById('drawer-close');
    close.addEventListener('click', closeDrawer);
    if (hadFocus) close.focus();
    var body = el.drawer.querySelector('.drawer-body');
    if (body) body.scrollTop = top;
  }

  function closeDrawer() {
    if (!drawerId) return;
    drawerId = null;
    el.drawer.classList.remove('open');
    document.body.classList.remove('drawer-open');
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
    // 顶部统计条 = 快捷列筛选器：点某列只看该列，点「共 N 个」恢复；
    // 「搁置」不是列，点它只展开折叠面板并滚过去。
    if (el.stats) el.stats.addEventListener('click', function (e) {
      var s = e.target.closest('[data-shelf]');
      if (s) {
        state.zone = 'shelf';
        state.shelfOpen = true;
        render();
        if (el.shelf && el.shelf.scrollIntoView) {
          el.shelf.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
        return;
      }
      var pn = e.target.closest('[data-plan]');
      if (pn) {
        setZone('plan');
        if (el.shelf && el.shelf.scrollIntoView) {
          el.shelf.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
        return;
      }
      var b = e.target.closest('[data-col]');
      if (!b) return;
      state.col = b.getAttribute('data-col') || '';
      render();
    });
    el.search.addEventListener('input', function () { state.query = el.search.value; render(); });
    el.refresh.addEventListener('click', function () { loadStatus(); });

    // 集群升级三键：检查（只读）/ 升级（本机快进）/ 发布（主控）
    if (el.upgradeCheck) {
      el.upgradeCheck.addEventListener('click', function () {
        refreshUpgrade().then(function () {
          if (!upgradeInfo) { flash('没拿到升级信息', true); return; }
          flash(upgradeInfo.update_available
            ? ('发现新版本 v' + upgradeInfo.remote_version)
            : '已是最新版本');
        });
      });
    }
    if (el.upgradeApply) {
      el.upgradeApply.addEventListener('click', function () {
        var info = upgradeInfo || {};
        var q = '把本机从 v' + (info.local_version || '?') + ' 升级到 v' +
                (info.remote_version || '?') + '？\n\n' +
                '只做 git 快进；协调仓有未提交改动会被拒绝。\n' +
                '升级完成后需要重启 hived / 看板进程才会用上新代码。';
        if (!window.confirm(q)) return;
        runUpgrade('apply', { yes: true }, '升级完成');
      });
    }
    if (el.upgradePublish) {
      el.upgradePublish.addEventListener('click', function () {
        var notes = window.prompt('发布当前版本给集群？可填变更说明（留空也行）：', '');
        if (notes === null) return;
        runUpgrade('publish', { notes: notes }, '已发布给集群');
      });
    }
    refreshUpgrade();

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
    if (el.logout) el.logout.addEventListener('click', logoutSession);

    el.overlay.addEventListener('click', closeDrawer);

    // 集群面板：点标题展开/折叠；主节点地址按钮复制；点面板外关闭
    if (el.cluster) el.cluster.addEventListener('click', function (e) {
      var copyBtn = e.target.closest && e.target.closest('[data-cluster-copy]');
      if (copyBtn) {
        e.preventDefault();
        e.stopPropagation();
        var addr = copyBtn.getAttribute('data-cluster-copy') || '';
        writeClipboard(addr).then(function () {
          flashCopied(copyBtn);
          flash('已复制主节点地址 ' + addr);
        }).catch(function (err) {
          flash('复制失败：' + ((err && err.message) ? err.message : err), true);
        });
        return;
      }
      var toggle = e.target.closest && e.target.closest('[data-cluster-toggle]');
      if (!toggle) return;
      e.preventDefault();
      e.stopPropagation();
      state.clusterOpen = !state.clusterOpen;
      renderCluster(state.cluster);
      syncTopbarHeight();
    });
    document.addEventListener('click', function (e) {
      if (!state.clusterOpen) return;
      if (el.cluster && el.cluster.contains(e.target)) return;
      state.clusterOpen = false;
      renderCluster(state.cluster);
    });

    // 设置弹窗：齿轮按钮 + 遮罩点击关闭 + Esc 关闭
    var settingsBtn = document.getElementById('settings-btn');
    if (settingsBtn) settingsBtn.addEventListener('click', openSettings);
    var settingsOverlay = document.getElementById('settings-overlay');
    if (settingsOverlay) settingsOverlay.addEventListener('click', closeSettings);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' || e.key === 'Esc') {
        if (state.planEditing !== null || (el.planModal && !el.planModal.hidden)) closePlanForm();
        else if (state.settingsOpen) closeSettings();
        else if (state.clusterOpen) { state.clusterOpen = false; renderCluster(state.cluster); }
        else closeDrawer();
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
    return ['exec', 'verify', 'manual', 'accept'].map(function (m) {
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

  // ---------- 集群升级（本地版本 / 检索 / 升级 / 发布）----------
  var upgradeInfo = null;
  var upgradeCanPublish = false;

  function upgradeFetch(method, body) {
    var opts = { method: method, headers: { 'Accept': 'application/json' } };
    if (body) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch('/api/upgrade', opts).then(function (r) { return r.json(); });
  }

  function renderUpgrade() {
    if (!el.upgradeBar) return;
    if (!upgradeInfo) { el.upgradeBar.hidden = true; return; }
    el.upgradeBar.hidden = false;
    el.upgradeVersion.textContent = 'v' + (upgradeInfo.local_version || '—');
    el.upgradeVersion.title =
      '本地 ' + (upgradeInfo.local_version || '?') +
      (upgradeInfo.remote_version ? '；主控 ' + upgradeInfo.remote_version : '；主控未发布') +
      '；策略 ' + (upgradeInfo.policy || '?');
    var can = !!upgradeInfo.update_available;
    el.upgradeApply.hidden = !can;
    if (can) {
      el.upgradeApply.textContent = '升级到 v' + upgradeInfo.remote_version;
      el.upgradeApply.title = '把本机快进到主控版本（协调仓有未提交改动会被拒绝）';
    }
    el.upgradePublish.hidden = !upgradeCanPublish;
    el.upgradePublish.title = '把本机版本发布给集群（' +
      (upgradeCanPublish ? '当前是 controller' : '需要 controller 角色') + '）';
  }

  function refreshUpgrade() {
    return upgradeFetch('GET').then(function (res) {
      if (!res || !res.ok) throw new Error((res && res.error) || '查询失败');
      upgradeInfo = res.upgrade || null;
      upgradeCanPublish = !!res.can_publish;
      renderUpgrade();
    }).catch(function (err) {
      // 升级信息拿不到不影响看板主流程，留个 console 痕迹即可
      if (window.console) console.warn('upgrade: ' + err);
    });
  }

  function runUpgrade(action, body, okMsg) {
    var btns = [el.upgradeCheck, el.upgradeApply, el.upgradePublish];
    btns.forEach(function (b) { if (b) b.disabled = true; });
    flash(action === 'apply' ? '正在升级…' : '正在处理…');
    return upgradeFetch('POST', Object.assign({ action: action }, body || {}))
      .then(function (res) {
        if (res && res.ok) {
          flash(okMsg || '完成');
        } else {
          flash((res && (res.error || res.output)) || (action + ' 失败'), true);
        }
      })
      .catch(function (err) { flash(String(err), true); })
      .then(function () {
        btns.forEach(function (b) { if (b) b.disabled = false; });
        return refreshUpgrade();
      });
  }

  function _verb(action, override) {
    return override ||
      ({ close: '关闭作废', retry: '重试', unassign: '退回待领取',
         reopen: '重开', shelve: '搁置', unshelve: '取消搁置',
         respawn: '另立新卡' })[action] || action;
  }

  // ---------- 登录态：HttpOnly cookie 长效保存 + 启动自检 ----------
  // 以前登录成功后只改了按钮文字，刷新页面就退回「登录」——cookie 其实还在，
  // 用户却以为没登录，又粘一遍 token。现在启动时用 GET /api/session 反查身份，
  // 只要 cookie 没被清/没被轮换吊销，按钮直接显示「已登录：<agent>」。
  var auth = { authenticated: false, agent: '', role: '', mode: 'on', checked: false };

  function paintAuth() {
    if (!el.login) return;
    if (auth.authenticated) {
      el.login.textContent = auth.mode === 'off'
        ? '本机直连' : ('已登录：' + (auth.agent || '?'));
      el.login.title = auth.mode === 'off'
        ? '看板绑在回环地址，写操作免鉴权（登录态不适用）'
        : ('角色：' + (auth.role || '?') +
           (auth.session_days ? '；登录态保留 ' + auth.session_days + ' 天' : '') +
           '（点一下可换 token）');
      el.login.classList.add('logged-in');
      if (el.logout) el.logout.hidden = auth.mode === 'off';
    } else {
      el.login.textContent = '登录';
      el.login.title = '写操作需要 token 时在这里登录';
      el.login.classList.remove('logged-in');
      if (el.logout) el.logout.hidden = true;
    }
  }

  function applyAuth(info) {
    info = info || {};
    if (info.mode) auth.mode = info.mode;
    auth.authenticated = info.authenticated === undefined
      ? !!info.ok : !!info.authenticated;
    auth.agent = info.agent || '';
    auth.role = info.role || '';
    if (info.session_days !== undefined) auth.session_days = info.session_days;
    auth.checked = true;
    // token 不进 localStorage（在 HttpOnly cookie 里），这里只缓存名字做首帧渲染
    storeSet(STORE_AUTH, auth.authenticated ? auth.agent : '');
    paintAuth();
  }

  /** 用 cookie 反查当前身份；cookie 有效就不必再输 token。 */
  function refreshSession() {
    return fetch('/api/session', {
      cache: 'no-store', headers: { Accept: 'application/json' }
    })
      .then(function (r) { return r.json(); })
      .then(function (j) { if (j && j.ok) applyAuth(j); return j; })
      .catch(function () { return null; });
  }

  /** 写操作遇到 401：cookie 没了或被吊销，别再假装已登录。 */
  function noteAuthRequired() {
    if (auth.mode === 'off' || !auth.authenticated) return;
    auth.authenticated = false;
    storeSet(STORE_AUTH, '');
    paintAuth();
  }

  function logoutSession() {
    if (!window.confirm('退出登录？退出后写操作需要重新填 token。')) return;
    fetch('/api/logout', { method: 'POST', cache: 'no-store' })
      .then(function () {
        applyAuth({ authenticated: false, agent: '', role: '', mode: auth.mode });
        flash('已退出登录');
      })
      .catch(function (e) { flash('退出失败：' + e.message, true); });
  }

  /** 登录：token → POST /api/login → 服务端写 HttpOnly cookie，后续写操作自动带上。 */
  function loginWithToken() {
    var token = window.prompt(
      '写操作需要 token（hive agent enroll <controller> 生成，或用 hive_whoami 里那份）。\n' +
      'token 只会换成 cookie（默认保留 ' + (auth.session_days || 30) + ' 天），不在页面里存明文。\n\n粘贴 token：', '');
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
          applyAuth({ authenticated: true, agent: res.j.agent, role: res.j.role,
                      mode: 'on', session_days: res.j.expires_in
                        ? Math.round(res.j.expires_in / 86400) : auth.session_days });
          flash('已登录：' + (res.j.agent || '') + '（' + (res.j.role || '') + '）' +
                (res.j.persistent === false ? '；本次为会话 cookie，关浏览器失效' : '；下次打开无需再输'),
                false);
        } else {
          flash('登录失败：' + ((res.j && res.j.error) || ('HTTP ' + res.status)), true);
        }
      })
      .catch(function (e) { flash('登录请求失败：' + e.message, true); });
  }

  // 调用后端 /api/task（后端再调用 hive CLI 真正执行）
  function callTaskAction(action, tid, agent, label, note, extra) {
    var body = { action: action, tid: tid };
    if (agent) body.agent = agent;
    if (note) body.note = note;
    if (extra) Object.keys(extra).forEach(function (k) { body[k] = extra[k]; });
    flash('正在' + _verb(action, label) + ' ' + tid + ' …');
    fetch('/api/task', {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, j: j }; }); })
      .then(function (res) {
        if (res.j && res.j.ok) {
          var msg = '操作成功：' + tid + ' 已' + _verb(action, label);
          if (res.j.new_task) msg += '，新卡 ' + res.j.new_task + ' 已进待领取';
          flash(msg, false);
          // 另立新卡后把搁置面板保持展开，方便接着决定原卡要不要废弃
          if (action === 'respawn') state.shelfOpen = true;
          loadStatus();
        } else {
          if (res.status === 401) noteAuthRequired();
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
    } else if (action === 'shelve') {
      head += '\n\n卡会退出派发池：释放租约 / 结果 / 进度 / 收件箱信号，不再占待领取、也不算异常。'
            + '\n请写清「现在为什么做不了」—— 这是日后判断能不能捡起来的唯一依据。';
    } else if (action === 'unshelve') {
      head += '\n\n卡会回到待领取，谁都能再领。搁置时释放的文件不会自动恢复，等于重新排队。';
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

  // 事件委托：捕获阶段处理，命中操作按钮时阻止冒泡，避免触发卡片打开抽屉。
  // 同一套 handler 挂在六列容器和搁置面板上（搁置面板在 .columns 外面）。
  function taskActionHandler(e) {
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

    if (action === 'close' || action === 'unassign' || action === 'reopen' ||
        action === 'shelve' || action === 'unshelve') {
      var reason = askReason(action, tid);
      if (reason === null) return;            // 用户取消
      var extra = null;
      // 搁置卡的「废弃」强制作废：搁置时结果已被释放，别让旧快照里残留的
      // res 把它误判成 done（那就跟「真做完了」长得一样了）。
      if (action === 'close') {
        var tc = taskById(tid);
        if (tc && tc.state === 'shelved') extra = { done: false };
      }
      callTaskAction(action, tid, null, null, reason, extra);
    } else if (action === 'respawn') {
      var src = taskById(tid);
      var title = window.prompt(
        '以搁置卡 ' + tid + ' 为模板另立一张「待领取」新卡。\n' +
        '原卡会保持搁置（确认不再需要再点「废弃」）。\n\n' +
        '原标题：' + ((src && src.title) || '') + '\n' +
        '搁置原因：' + ((src && src.shelve_reason) || '未填') +
        '\n\n新卡标题：', (src && src.title) || '');
      if (title === null) return;
      title = String(title).trim();
      if (!title) { flash('新卡标题不能为空', true); return; }
      callTaskAction('respawn', tid, null, null,
                     '由搁置卡 ' + tid + ' 另立：' + title, { title: title });
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
  }

  function bindTaskActions() {
    el.columns.addEventListener('click', taskActionHandler, true);
    if (el.shelfList) el.shelfList.addEventListener('click', taskActionHandler, true);
  }

  // ---------- 留言（跨机聊天） ----------
  //
  // 场景：机器 A 的 agent 对某张卡有疑问、或想指点机器 B 上另一个 agent 的活，
  // 以前只能把文本贴到某个聊天窗口里再口口相传。现在统一走这里：
  //   频道 = 大厅 / task:T-0231 / agent:<名字>
  // 读取走 GET /api/chat（真身在集群，各机器 2 秒内同步）；写入要 token（与其它写操作同口径）。
  var CHAT_MS = 6000;                    // 面板打开时的轮询间隔
  var CHAT_LIMIT = 300;                  // 一次拉最近多少条
  var STORE_CHAT_SEEN = 'hive.chat.lastSeen';
  var STORE_CHAT_SCOPE = 'hive.chat.scope';
  var CHAT_KIND_LABEL = { note: '说明', question: '提问', answer: '回答' };

  var chat = {
    open: false,
    scope: 'hall',
    messages: [],
    source: '',
    busy: false,
    lastSeen: 0,
    timer: null,
    ticks: 0
  };

  function chatScopeLabel(scope) {
    if (!scope || scope === 'hall') return '大厅';
    if (scope.indexOf('task:') === 0) {
      var tid = scope.slice(5);
      var t = findTask(tid);
      return tid + (t && t.title ? ' · ' + t.title : '');
    }
    if (scope.indexOf('agent:') === 0) return scope.slice(6) + '（私信）';
    return scope;
  }

  function chatScopeShort(scope) {
    if (!scope || scope === 'hall') return '大厅';
    if (scope.indexOf('task:') === 0) return scope.slice(5);
    if (scope.indexOf('agent:') === 0) return '@' + scope.slice(6);
    return scope;
  }

  /** 下拉里的频道：大厅 → 智能体（私信）→ 任务卡讨论 */
  function chatScopeOptions() {
    var h = '<option value="hall">大厅（所有机器都看得到）</option>';
    var agents = [];
    if (state.system && state.system.agents) {
      state.system.agents.forEach(function (a) {
        if (a && a.name) agents.push(a.name);
      });
    }
    state.tasks.forEach(function (t) { if (t.owner) agents.push(t.owner); });
    agents = uniq(agents).sort();
    if (agents.length) {
      h += '<optgroup label="私信某个 agent">';
      agents.forEach(function (n) {
        h += '<option value="agent:' + esc(n) + '">' + esc(n) + '</option>';
      });
      h += '</optgroup>';
    }
    var tasks = state.tasks.slice(0).sort(function (a, b) {
      return a.id < b.id ? 1 : -1;
    });
    if (tasks.length) {
      h += '<optgroup label="某张卡的讨论">';
      tasks.forEach(function (t) {
        h += '<option value="task:' + esc(t.id) + '">' + esc(t.id) + ' · ' +
             esc((t.title || '').slice(0, 28)) + '</option>';
      });
      h += '</optgroup>';
    }
    return h;
  }

  function renderChatScopes() {
    var sel = el.chatScope;
    if (!sel) return;
    var html = chatScopeOptions();
    if (sel.dataset.sig !== html) {
      sel.innerHTML = html;
      sel.dataset.sig = html;
    }
    // 选中的频道可能来自卡片点击（任务不在下拉里就临时补一个）
    if (chat.scope && !sel.querySelector('option[value="' + chat.scope.replace(/"/g, '\\"') + '"]')) {
      var extra = document.createElement('option');
      extra.value = chat.scope;
      extra.textContent = chat.scope;
      sel.appendChild(extra);
    }
    sel.value = chat.scope;
  }

  function chatMessagesFor(scope) {
    return chat.messages.filter(function (m) { return m.scope === scope; });
  }

  function chatCountFor(tid) {
    var scope = 'task:' + tid;
    var n = 0;
    for (var i = 0; i < chat.messages.length; i++) {
      if (chat.messages[i].scope === scope) n++;
    }
    return n;
  }

  function chatUnread() {
    var n = 0;
    for (var i = 0; i < chat.messages.length; i++) {
      if (Number(chat.messages[i].epoch || 0) > chat.lastSeen) n++;
    }
    return n;
  }

  function renderChatBadge() {
    if (!el.chatBadge) return;
    var n = chatUnread();
    if (n > 0 && !chat.open) {
      el.chatBadge.textContent = n > 99 ? '99+' : String(n);
      el.chatBadge.hidden = false;
    } else {
      el.chatBadge.hidden = true;
    }
  }

  function chatMsgHTML(m) {
    var kind = m.kind || 'note';
    var mine = String(m.from || '').indexOf('human') === 0;
    var cls = 'chat-msg' + (mine ? ' is-mine' : '') +
              (kind === 'question' ? ' is-question' : '') +
              (kind === 'answer' ? ' is-answer' : '');
    var t = Number(m.epoch || 0) * 1000;
    var h = '<div class="' + cls + '" data-mid="' + esc(m.id) + '">';
    h += '<div class="chat-msg-meta">';
    h += '<span class="chat-msg-from">' + esc(m.from || '匿名') + '</span>';
    if (m.node_name) h += '<span class="chat-msg-node">@' + esc(m.node_name) + '</span>';
    if (kind !== 'note') h += '<span class="chat-msg-kind">' + esc(CHAT_KIND_LABEL[kind] || kind) + '</span>';
    h += '<span class="chat-msg-time">' + esc(relTime(t)) + '</span>';
    h += '<button type="button" class="chat-msg-copy" data-chat-copy="' + esc(m.id) +
         '" title="复制这段话，粘给任意 agent">复制</button>';
    h += '</div>';
    h += '<div class="chat-msg-text">' + esc(m.text || '') + '</div>';
    if (chat.scope === 'hall' && m.scope !== 'hall') {
      h += '<div class="chat-msg-scope">来自 ' + esc(chatScopeShort(m.scope)) + '</div>';
    }
    h += '</div>';
    return h;
  }

  function renderChatFeed() {
    var feed = el.chatFeed;
    if (!feed) return;
    var list = chatMessagesFor(chat.scope);
    if (!list.length) {
      feed.innerHTML = '<div class="chat-empty">这个频道还没有留言。<br>' +
        '在下面粘一段 prompt / 说明 / 疑问，点「发送」——<br>' +
        '其它机器上的 agent 用 <code>hive chat list</code> 或 MCP 的 ' +
        '<code>hive_chat_read</code> 就能读到。</div>';
      return;
    }
    var nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
    feed.innerHTML = list.map(chatMsgHTML).join('');
    if (nearBottom || chat.open) feed.scrollTop = feed.scrollHeight;
  }

  function renderChatSource() {
    if (!el.chatSrc) return;
    if (!chat.source) {
      el.chatSrc.textContent = '—';
      el.chatSrc.className = 'chat-src';
      return;
    }
    if (chat.source === 'cluster') {
      el.chatSrc.textContent = '集群同步';
      el.chatSrc.className = 'chat-src';
    } else {
      el.chatSrc.textContent = '本地（集群未运行）';
      el.chatSrc.className = 'chat-src is-local';
    }
  }

  function renderChat() {
    renderChatScopes();
    renderChatFeed();
    renderChatSource();
    renderChatBadge();
    if (el.chatFab) {
      el.chatFab.classList.toggle('is-open', chat.open);
      el.chatFab.setAttribute('aria-expanded', chat.open ? 'true' : 'false');
    }
    if (el.chatPanel) {
      el.chatPanel.classList.toggle('open', chat.open);
      el.chatPanel.setAttribute('aria-hidden', chat.open ? 'false' : 'true');
    }
  }

  function loadChat() {
    if (chat.busy) return;
    chat.busy = true;
    fetch('/api/chat?since=0&limit=' + CHAT_LIMIT, { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        var incoming = (data && data.messages) || [];
        var changed = incoming.length !== chat.messages.length;
        if (!changed) {
          for (var i = 0; i < incoming.length; i++) {
            if (incoming[i].id !== chat.messages[i].id) { changed = true; break; }
          }
        }
        chat.messages = incoming;
        chat.source = (data && data.source) || '';
        if (changed) {
          renderChat();
          // 卡片上的「讨论 n」、详情抽屉里的讨论区跟着变
          if (!state.inflight) render();
          if (drawerId) refreshDrawer(true);
          if (chat.open) {
            chat.lastSeen = incoming.reduce(function (a, m) {
              return Math.max(a, Number(m.epoch || 0));
            }, chat.lastSeen);
            storeSet(STORE_CHAT_SEEN, String(chat.lastSeen));
            renderChatBadge();
          }
        }
      })
      .catch(function () { /* 看板其它部分不受影响 */ })
      .then(function () { chat.busy = false; });
  }

  /** 懒轮询：面板开着每 CHAT_MS 拉一次；收起时降频到 5 倍，只为了角标 */
  function startChatPolling() {
    if (chat.timer) return;
    chat.timer = setInterval(function () {
      chat.ticks++;
      if (chat.open || chat.ticks % 5 === 0) loadChat();
    }, CHAT_MS);
  }

  function openChat(scope) {
    if (scope) chat.scope = scope;
    else chat.scope = storeGet(STORE_CHAT_SCOPE) || 'hall';
    chat.open = true;
    closeDrawer();
    renderChat();
    loadChat();
    chat.lastSeen = chat.messages.reduce(function (a, m) {
      return Math.max(a, Number(m.epoch || 0));
    }, chat.lastSeen);
    storeSet(STORE_CHAT_SEEN, String(chat.lastSeen));
    renderChatBadge();
    if (el.chatInput) setTimeout(function () { el.chatInput.focus(); }, 120);
  }

  function closeChat() {
    chat.open = false;
    renderChat();
  }

  function sendChat() {
    if (!el.chatInput) return;
    var text = (el.chatInput.value || '').trim();
    if (!text) { flash('内容不能为空', true); return; }
    var payload = {
      text: text,
      scope: chat.scope,
      kind: (el.chatKind && el.chatKind.value) || 'note'
    };
    if (el.chatSend) { el.chatSend.disabled = true; el.chatSend.textContent = '发送中…'; }
    fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          return { ok: r.ok, status: r.status, data: d };
        });
      })
      .then(function (res) {
        if (!res.ok) {
          if (res.status === 401) {
            noteAuthRequired();
            flash('发送需要登录：点右上角「登录」填 token', true);
          } else if (res.data && res.data.usage &&
                     /^\s*(?:\/plan|@command:plan)\b/i.test(text)) {
            // 计划指令写错时把正确格式原样给出来，而不是只说「发送失败」
            flash('计划指令格式不对：' + ((res.data && res.data.error) || res.status), true);
          } else {
            flash('发送失败：' + ((res.data && res.data.error) || res.status), true);
          }
          return;
        }
        el.chatInput.value = '';
        if (res.data && res.data.offline) {
          flash('已存到本机（集群没运行，别的机器暂时看不到）', true);
        }
        // 计划指令执行后，计划区要立刻反映（/api/plans 已在服务端跑过同一条指令）
        if (/^\s*(?:\/plan|@command:plan)\b/i.test(text)) {
          flash((res.data && res.data.message) || '计划指令已执行');
          state.zone = 'plan';
          state.planOpen = true;
          loadPlans();
        }
        loadChat();
      })
      .catch(function (err) {
        flash('发送失败：' + ((err && err.message) || err), true);
      })
      .then(function () {
        if (el.chatSend) { el.chatSend.disabled = false; el.chatSend.textContent = '发送'; }
      });
  }

  function bindChat() {
    if (el.chatFab) el.chatFab.addEventListener('click', function () {
      if (chat.open) closeChat(); else openChat();
    });
    if (el.chatClose) el.chatClose.addEventListener('click', closeChat);
    if (el.chatScope) el.chatScope.addEventListener('change', function () {
      chat.scope = el.chatScope.value || 'hall';
      storeSet(STORE_CHAT_SCOPE, chat.scope);
      renderChatFeed();
      if (el.chatInput) el.chatInput.focus();
    });
    if (el.chatFeed) el.chatFeed.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('[data-chat-copy]') : null;
      if (!btn) return;
      var mid = btn.getAttribute('data-chat-copy');
      var msg = null;
      for (var i = 0; i < chat.messages.length; i++) {
        if (chat.messages[i].id === mid) { msg = chat.messages[i]; break; }
      }
      if (!msg) return;
      writeClipboard(msg.text || '').then(function () {
        flash('已复制这条留言');
      }).catch(function () { flash('复制失败', true); });
    });
    if (el.chatForm) el.chatForm.addEventListener('submit', function (e) {
      e.preventDefault();
      sendChat();
    });
    if (el.chatInput) el.chatInput.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        sendChat();
      }
    });
    // Esc 关面板（设置/集群面板优先级更高，交给全局那个 handler）
    document.addEventListener('keydown', function (e) {
      if ((e.key === 'Escape' || e.key === 'Esc') && chat.open &&
          !state.settingsOpen && !state.clusterOpen && !drawerId) {
        closeChat();
      }
    });
    startChatPolling();
    loadChat();
  }

  // ---------- 任务计划（计划 Tab） ----------
  //
  // 与搁置共用一个 section，靠 Tab 切换。计划数据来自 /api/plans（服务端已按
  // 规则排序：进行中置顶在前、其余更新时间倒序；已完成按完成时间倒序），
  // 前端只做「分组 + 渲染」，不重复实现排序，避免两套规则漂移。
  var PLAN_STATUS_LABEL = { active: '进行中', completed: '已完成', closed: '已关闭', deleted: '已删除' };

  function planById(pid) {
    for (var i = 0; i < state.plans.length; i++) {
      if (state.plans[i].id === pid) return state.plans[i];
    }
    return null;
  }

  function loadPlans(opts) {
    if (state.planLoading) return;
    state.planLoading = true;
    state.planError = '';
    renderPlans();
    fetch('/api/plans', { cache: 'no-store', headers: { Accept: 'application/json' } })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          return { ok: r.ok, status: r.status, data: d };
        });
      })
      .then(function (res) {
        if (!res.ok || !res.data || res.data.ok === false) {
          state.planError = (res.data && res.data.error) || ('HTTP ' + res.status);
          state.planLoading = false;
          renderPlans();
          return;
        }
        state.plans = Array.isArray(res.data.plans) ? res.data.plans : [];
        state.planCounts = res.data.counts || { active: 0, completed: 0, closed: 0 };
        state.planLoading = false;
        renderPlans();
        if (!opts || !opts.quiet) renderMeta();
      })
      .catch(function (e) {
        state.planError = '计划请求失败：' + ((e && e.message) ? e.message : e);
        state.planLoading = false;
        renderPlans();
      });
  }

  /** 进度条 + 「已完成 3/8 · 38%」；缺任务时明确说「快照里没有」，不装作 0 */
  function planProgressHTML(p) {
    var g = p.progress || { total: 0, done: 0, rate: 0, missing: [] };
    if (!g.total) {
      return '<div class="plan-progress is-empty"><span class="plan-progress-text">' +
        '未关联任务</span></div>';
    }
    var rate = Math.max(0, Math.min(100, Math.round(g.rate)));
    // 手动提前完成时写「关联任务」而不是「已完成」：否则卡片上会同时出现
    // 「已完成」徽标和「1/2 已完成」，读起来像数据自相矛盾。
    var unit = (p.status === 'active') ? '已完成' : '关联任务完成';
    var h = '<div class="plan-progress">' +
      '<div class="bar"><i style="width:' + rate + '%"></i></div>' +
      '<span class="plan-progress-text"><b>' + g.done + '</b>/' + g.total +
      ' ' + unit + ' · ' + rate + '%</span></div>';
    h += '<div class="plan-tasks">';
    (p.task_ids || []).forEach(function (tid) {
      var t = findTask(tid);
      var cls = 'pill plan-task';
      var title = tid;
      // 任务还没加载完（首屏）时不判定「缺失」，否则会闪一片虚线胶囊
      if (t) {
        if (t.state === 'done' || t.result_status === 'done') cls += ' is-done';
      } else if (state.tasks.length) {
        cls += ' is-missing';
        title = tid + '（当前快照里没有）';
      }
      h += '<button type="button" class="' + cls + '" data-plan-task="' + esc(tid) +
        '" title="' + esc(title) + '">' + esc(tid) + '</button>';
    });
    h += '</div>';
    if (g.missing && g.missing.length) {
      h += '<div class="plan-warn">快照里找不到 ' + g.missing.length + ' 个关联任务：' +
        esc(g.missing.join(', ')) + '</div>';
    }
    return h;
  }

  function planCardHTML(p) {
    var isDone = p.status !== 'active';
    var h = '<article class="plan-card' + (p.pinned ? ' is-pinned' : '') +
      (isDone ? ' is-done' : '') + '" data-plan-id="' + esc(p.id) + '">';
    h += '<div class="plan-head">';
    if (p.pinned) h += '<span class="plan-pin" title="置顶">📌</span>';
    // 手动完成时任务可能还没全做完：徽标直接写「手动完成」，别让人对着
    // 一个 50% 的进度条怀疑「已完成」是不是坏了。
    var pg0 = p.progress || {};
    var manual = p.status === 'completed' && pg0.total && pg0.done < pg0.total;
    var statusText = manual ? '手动完成' : (PLAN_STATUS_LABEL[p.status] || p.status);
    h += '<span class="plan-id">' + esc(p.id) + '</span>' +
      '<span class="plan-prefix">' + esc(p.prefix || '—') + '</span>' +
      '<span class="badge plan-status" data-status="' + esc(p.status) + '"' +
      (manual ? ' title="手动标记完成：仍有 ' + (pg0.total - pg0.done) + ' 个关联任务未完成"' : '') +
      '>' + esc(statusText) + '</span>';
    h += '</div>';
    h += '<h4 class="plan-title">' + esc(p.title) + '</h4>';
    if (p.description) h += '<div class="plan-desc">' + esc(p.description) + '</div>';
    h += planProgressHTML(p);
    // 手动完成时任务可能还没全做完：把「完成」和「进度」的矛盾说清楚，
    // 而不是让人对着一个 50% 的进度条怀疑数据错了。
    var pg = p.progress || {};
    if (p.status !== 'active' && pg.total && pg.done < pg.total) {
      h += '<div class="plan-warn">手动标记' +
        (p.status === 'closed' ? '关闭' : '完成') + '，仍有 ' + (pg.total - pg.done) +
        ' 个关联任务未完成</div>';
    }

    var created = parseTime(p.created_at);
    var updated = parseTime(p.updated_at);
    var doneAt = parseTime(p.completed_at || p.closed_at);
    h += '<div class="plan-meta">';
    h += '<span>创建人 ' + esc(p.created_by || '—') + '</span>';
    if (created !== null) h += '<span>建于 ' + dtf.format(created) + '</span>';
    if (updated !== null) h += '<span>更新 ' + relTime(updated) + '</span>';
    if (doneAt !== null) h += '<span>' + (p.status === 'closed' ? '关闭于 ' : '完成于 ') +
      dtf.format(doneAt) + '</span>';
    h += '</div>';

    h += '<div class="plan-actions">';
    if (p.status === 'active') {
      h += '<button type="button" class="act act-ok" data-plan-action="complete" data-plan-id="' +
        esc(p.id) + '">完成</button>';
      h += '<button type="button" class="act" data-plan-action="' +
        (p.pinned ? 'unpin' : 'pin') + '" data-plan-id="' + esc(p.id) + '">' +
        (p.pinned ? '取消置顶' : '置顶') + '</button>';
    }
    h += '<button type="button" class="act act-reassign" data-plan-action="edit" data-plan-id="' +
      esc(p.id) + '">编辑</button>';
    // 关闭只对进行中有意义：已完成/已关闭的再「关闭」一次是空操作，徒增困惑
    if (p.status === 'active') {
      h += '<button type="button" class="act" data-plan-action="close" data-plan-id="' +
        esc(p.id) + '">关闭</button>';
    }
    h += '<button type="button" class="act act-close" data-plan-action="delete" data-plan-id="' +
      esc(p.id) + '">删除</button>';
    h += '</div></article>';
    return h;
  }

  function renderPlans() {
    if (!el.planBody) return;
    if (el.planCount) {
      var activeN = (state.planCounts && state.planCounts.active) || 0;
      el.planCount.textContent = activeN;
      var allN = state.plans.length;
      el.planCount.title = '进行中的计划 ' + activeN + ' 个（全部 ' + allN + ' 个：' +
        '已完成 ' + ((state.planCounts && state.planCounts.completed) || 0) +
        '，已关闭 ' + ((state.planCounts && state.planCounts.closed) || 0) + '）';
    }
    if (el.planLoading) el.planLoading.hidden = !state.planLoading;
    if (el.planError) {
      el.planError.hidden = !state.planError;
      el.planError.innerHTML = state.planError
        ? '<span>计划加载失败：' + esc(state.planError) + '</span>' +
          '<button type="button" class="btn" id="plan-retry">重试</button>'
        : '';
      var retry = document.getElementById('plan-retry');
      if (retry) retry.addEventListener('click', function () { loadPlans(); });
    }
    var active = [], done = [];
    state.plans.forEach(function (p) {
      if (p.status === 'active') active.push(p);
      else done.push(p);              // completed / closed 都进「已完成」区，徽标区分
    });
    if (el.planActiveCount) el.planActiveCount.textContent = active.length;
    if (el.planDoneCount) el.planDoneCount.textContent = done.length;
    if (el.planActiveList) {
      el.planActiveList.innerHTML = active.map(planCardHTML).join('');
      el.planActiveList.hidden = !active.length;
      if (el.planActiveEmpty) el.planActiveEmpty.hidden = !!active.length || state.planLoading;
    }
    if (el.planDoneList) {
      el.planDoneList.innerHTML = done.map(planCardHTML).join('');
      el.planDoneList.hidden = !done.length;
      if (el.planDoneEmpty) el.planDoneEmpty.hidden = !!done.length || state.planLoading;
    }
  }

  // ---- 新建 / 编辑弹窗 ----
  function openPlanForm(pid) {
    var p = pid ? planById(pid) : null;
    state.planEditing = p ? p.id : null;
    if (!el.planModal || !el.planForm) return;
    document.getElementById('plan-modal-title').textContent = p ? ('编辑计划 ' + p.id) : '新建计划';
    el.planForm.reset();
    document.getElementById('plan-prefix').value = p ? (p.prefix || '') : '';
    document.getElementById('plan-title-input').value = p ? (p.title || '') : '';
    document.getElementById('plan-desc').value = p ? (p.description || '') : '';
    document.getElementById('plan-tasks').value = p ? (p.task_ids || []).join(', ') : '';
    document.getElementById('plan-auto').checked = p ? p.auto_complete !== false : true;
    document.getElementById('plan-pinned').checked = p ? !!p.pinned : false;
    if (el.planFormError) { el.planFormError.hidden = true; el.planFormError.textContent = ''; }
    el.planModal.hidden = false;
    el.planModal.setAttribute('aria-hidden', 'false');
    if (el.planOverlay) el.planOverlay.classList.add('open');
    var first = document.getElementById('plan-prefix');
    if (first && first.focus) first.focus();
  }

  function closePlanForm() {
    state.planEditing = null;
    if (!el.planModal) return;
    el.planModal.hidden = true;
    el.planModal.setAttribute('aria-hidden', 'true');
    if (el.planOverlay) el.planOverlay.classList.remove('open');
  }

  function planFormError(msg) {
    if (!el.planFormError) { flash(msg, true); return; }
    el.planFormError.hidden = false;
    el.planFormError.textContent = msg;
  }

  /** 任务号：允许中英文逗号 / 空格 / 换行分隔，统一大写并去重。 */
  function parsePlanTasks(raw) {
    var seen = Object.create(null), out = [];
    String(raw || '').split(/[\s,，、;；]+/).forEach(function (s) {
      var t = s.trim().toUpperCase();
      if (!t) return;
      if (!/^T-\d{3,6}$/.test(t)) { throw new Error('任务号不合法：' + s.trim() + '（应形如 T-0231）'); }
      if (!seen[t]) { seen[t] = 1; out.push(t); }
    });
    return out;
  }

  function savePlanForm(e) {
    if (e && e.preventDefault) e.preventDefault();
    var prefix = (document.getElementById('plan-prefix').value || '').trim();
    var title = (document.getElementById('plan-title-input').value || '').trim();
    var desc = document.getElementById('plan-desc').value || '';
    var auto = document.getElementById('plan-auto').checked;
    var pinned = document.getElementById('plan-pinned').checked;
    if (!prefix) { planFormError('前缀不能为空（例：PAY）'); return; }
    if (!title) { planFormError('标题不能为空'); return; }
    var tasks;
    try { tasks = parsePlanTasks(document.getElementById('plan-tasks').value); }
    catch (err) { planFormError(err.message); return; }

    var editing = state.planEditing;
    var body = editing
      ? { action: 'update', plan_id: editing,
          fields: { prefix: prefix, title: title, description: desc,
                    task_ids: tasks, pinned: pinned, auto_complete: auto } }
      : { action: 'create', prefix: prefix, title: title, description: desc,
          task_ids: tasks, pinned: pinned, auto_complete: auto };
    var save = document.getElementById('plan-form-save');
    if (save) { save.disabled = true; save.textContent = '保存中…'; }
    fetch('/api/plans', {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          return { status: r.status, data: d };
        });
      })
      .then(function (res) {
        if (!res.data || !res.data.ok) {
          var msg = (res.data && res.data.error) || ('HTTP ' + res.status);
          if (res.status === 401) { noteAuthRequired(); msg = '需要登录：点右上角「登录」填 token'; }
          planFormError(msg);
          return;
        }
        flash((editing ? '已更新 ' : '已创建 ') + res.data.plan.id + ' · ' + res.data.plan.title);
        closePlanForm();
        loadPlans();
      })
      .catch(function (err) { planFormError('保存失败：' + ((err && err.message) || err)); })
      .then(function () {
        if (save) { save.disabled = false; save.textContent = '保存'; }
      });
  }

  // ---- 卡片操作 ----
  function callPlanAction(action, pid, confirmExtra) {
    var p = planById(pid);
    var body = { action: action, plan_id: pid };
    if (confirmExtra) Object.keys(confirmExtra).forEach(function (k) { body[k] = confirmExtra[k]; });
    var verb = { complete: '完成', close: '关闭', delete: '删除',
                 pin: '置顶', unpin: '取消置顶' }[action] || action;
    flash('正在' + verb + '计划 ' + pid + ' …');
    fetch('/api/plans', {
      method: 'POST', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          return { status: r.status, data: d };
        });
      })
      .then(function (res) {
        if (!res.data || !res.data.ok) {
          var msg = (res.data && res.data.error) || ('HTTP ' + res.status);
          if (res.status === 401) { noteAuthRequired(); msg = '需要登录：点右上角「登录」填 token'; }
          else if (res.status === 403) msg = '权限不足：' + msg;
          flash(verb + '失败：' + msg, true);
          return;
        }
        flash('计划 ' + pid + ' 已' + verb + (p ? '（' + p.title + '）' : ''));
        loadPlans();
      })
      .catch(function (err) { flash(verb + '失败：' + ((err && err.message) || err), true); });
  }

  /** 计划卡操作事件委托；捕获阶段拦下，避免冒泡到别处 */
  function planActionHandler(e) {
    var task = e.target.closest && e.target.closest('[data-plan-task]');
    if (task) {
      // 关联任务胶囊：和看板上的「依赖」一样，点一下就在搜索框里定位
      e.stopPropagation();
      e.preventDefault();
      state.query = task.getAttribute('data-plan-task') || '';
      if (el.search) el.search.value = state.query;
      state.col = '';
      render();
      flash('已定位到 ' + state.query);
      return;
    }
    var btn = e.target.closest && e.target.closest('[data-plan-action]');
    if (!btn) return;
    e.stopPropagation();
    e.preventDefault();
    var action = btn.getAttribute('data-plan-action');
    var pid = btn.getAttribute('data-plan-id');
    var p = planById(pid);
    var label = (p && p.title) || '';
    if (action === 'edit') { openPlanForm(pid); return; }
    if (action === 'complete') {
      if (!window.confirm('把计划 ' + pid + ' 标为已完成？\n' + label)) return;
      callPlanAction('complete', pid);
      return;
    }
    if (action === 'close') {
      if (!window.confirm('关闭计划 ' + pid + '？\n关闭不是完成：它会带着「已关闭」标记留在已完成区。\n'
                          + label)) return;
      callPlanAction('close', pid);
      return;
    }
    if (action === 'delete') {
      if (!window.confirm('删除计划 ' + pid + '？\n这是软删除：记录仍保留，但不再出现在列表里。\n'
                          + label)) return;
      callPlanAction('delete', pid, { confirm: true });
      return;
    }
    if (action === 'pin' || action === 'unpin') { callPlanAction(action, pid); return; }
  }

  function bindPlans() {
    if (el.planActiveList) el.planActiveList.addEventListener('click', planActionHandler, true);
    if (el.planDoneList) el.planDoneList.addEventListener('click', planActionHandler, true);
    if (el.planNew) el.planNew.addEventListener('click', function () { openPlanForm(null); });
    var close = document.getElementById('plan-modal-close');
    var cancel = document.getElementById('plan-form-cancel');
    if (close) close.addEventListener('click', closePlanForm);
    if (cancel) cancel.addEventListener('click', closePlanForm);
    if (el.planOverlay) el.planOverlay.addEventListener('click', closePlanForm);
    if (el.planForm) el.planForm.addEventListener('submit', savePlanForm);
  }

  // ---------- 启动 ----------
  // T-0207：把顶栏实际高度写进 --topbar-h，fixed 抽屉据此从顶栏下方起滑。
  // 顶栏会随窗口宽度换行（≤768px 筛选区换行、meta 行），固定 px 会错位，
  // 所以这里实测 offsetHeight；首帧 CSS 里的 84px 只是兜底。
  function syncTopbarHeight() {
    var bar = document.querySelector('.topbar');
    if (!bar) return;
    document.documentElement.style.setProperty('--topbar-h', bar.offsetHeight + 'px');
  }

  function init() {
    cacheEls();
    buildColumns();
    buildShelf();
    initTheme();
    bindEvents();
    bindChat();                // 留言面板：浮标 / 频道 / 发送 / 轮询
    bindTaskActions();         // 异常卡操作按钮：终止 / 改派 / 退回未分配
    bindPlans();               // 计划 Tab：新建/编辑弹窗 + 卡片操作
    applyEffective();          // 落地主题/紧凑/CI/刷新间隔等生效设置

    syncTopbarHeight();
    if (window.addEventListener) {
      window.addEventListener('resize', syncTopbarHeight);
      window.addEventListener('orientationchange', syncTopbarHeight);
    }

    state.autoRefresh = storeGet(STORE_AUTO) !== 'off';
    el.auto.checked = state.autoRefresh;

    loadStatus();
    loadCI();
    loadCluster();
    // 登录态自检：先按上次登录的 agent 名把按钮画好（首帧不闪「登录」），
    // 再用 cookie 向服务端核对——cookie 有效就继续是「已登录」，无需再输 token。
    var cachedAgent = storeGet(STORE_AUTH);
    if (cachedAgent) {
      applyAuth({ authenticated: true, agent: cachedAgent, mode: 'on' });
    }
    refreshSession();
    loadPlans({ quiet: true });   // 计划与任务解耦：任务快照失败也要能看计划
    setInterval(loadCluster, CLUSTER_MS);
    loadServerConfig();        // 拉取服务端共享默认与系统只读信息

    // 深链：#plan 直接停在计划 Tab（贴给同事「看这个计划」时省一次点击）
    if ((location.hash || '') === '#plan') setZone('plan');

    // 深链：#chat=<hall|task:T-0231|agent:名字> 直接摊开对应的留言线程，
    // 方便在群里甩一条链接让人「来这张卡的讨论里说」。
    var m = /^#chat(?:=([^&]+))?$/.exec(location.hash || '');
    if (m) {
      var scope = 'hall';
      try { scope = decodeURIComponent(m[1] || 'hall'); } catch (err) { scope = 'hall'; }
      openChat(scope);
    }
  }

  init();
})();
