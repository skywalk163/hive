/* 计划面板的 jsdom 行为测试（可选：需 jsdom）。
 *
 * 跑法：NODE_PATH=<装了 jsdom 的 node_modules> node dev/plans-ui-test.js
 *
 * 验证的是「搁置与计划同区域 Tab 切换」这个取舍：切 Tab 不能影响搁置原有折叠、
 * 计数与卡片操作；计划区要按进行中/已完成分区、进度可见、操作按钮齐全、
 * 关闭与删除要二次确认（且取消时不发请求）。
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const DIR = path.resolve(__dirname, '..');
let html = fs.readFileSync(path.join(DIR, 'index.html'), 'utf8');
html = html.replace('<script src="app.js" defer></script>', '');
const appJs = fs.readFileSync(path.join(DIR, 'app.js'), 'utf8');
const base = JSON.parse(fs.readFileSync(path.join(DIR, 'sample-status.json'), 'utf8'));
const FIXED = Date.parse('2026-10-23T11:00:00+08:00');

const SHELVED = {
  id: 'T-9001', title: '等上游 SDK 发版，暂时做不了',
  state: 'shelved', repo: 'light', priority: 'P2', owner: '',
  claim_agent: '', lease_until: '', depends_on: [], result_status: '',
  stage: 'shelved', finished_at: '', duration_s: 0,
  shelved_at: '2026-10-22T09:00:00+08:00',
  shelved_from: 'ready', shelve_reason: '上游 SDK 还没发版，等 12 月',
};

const PLANS = [
  { id: 'P-0001', prefix: 'PAY', title: '支付链路改造', description: '拆成 3 张卡',
    status: 'active', status_label: '进行中', pinned: true,
    task_ids: ['T-0106', 'T-0105'],
    created_by: 'alice', created_at: '2026-10-20T09:00:00+08:00',
    updated_at: '2026-10-22T09:00:00+08:00', completed_at: '', closed_at: '',
    auto_complete: true,
    progress: { total: 2, done: 1, rate: 50.0, missing: [], known: 2 } },
  { id: 'P-0002', prefix: 'INFRA', title: '基础设施加固', description: '',
    status: 'active', status_label: '进行中', pinned: false,
    task_ids: ['T-9999'],
    created_by: 'bob', created_at: '2026-10-21T09:00:00+08:00',
    updated_at: '2026-10-21T09:00:00+08:00', completed_at: '', closed_at: '',
    auto_complete: true,
    progress: { total: 1, done: 0, rate: 0.0, missing: ['T-9999'], known: 0 } },
  { id: 'P-0003', prefix: 'DOC', title: '文档整理', description: '',
    status: 'completed', status_label: '已完成', pinned: false, task_ids: [],
    created_by: 'carol', created_at: '2026-10-18T09:00:00+08:00',
    updated_at: '2026-10-19T09:00:00+08:00',
    completed_at: '2026-10-23T10:00:00+08:00', closed_at: '',
    auto_complete: true, progress: { total: 0, done: 0, rate: 0, missing: [], known: 0 } },
];

function assert(cond, msg) {
  if (!cond) { console.error('FAIL:', msg); process.exitCode = 1; }
  else { console.log('ok  -', msg); }
}

(async () => {
  const vc = new VirtualConsole();
  const jsErrors = [];
  vc.on('jsdomError', e => jsErrors.push(String(e)));

  const calls = [];          // 发出的写请求
  let plans = PLANS.slice();
  const confirmations = [];
  let confirmAnswer = true;

  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8787/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
    virtualConsole: vc,
  });
  const w = dom.window;
  const RealDate = w.Date;
  class FakeDate extends RealDate {
    constructor(...args) { if (!args.length) super(FIXED); else super(...args); }
    static now() { return FIXED; }
  }
  w.Date = FakeDate;
  w.confirm = (msg) => { confirmations.push(msg); return confirmAnswer; };
  w.fetch = async (url, opts) => {
    const u = String(url);
    const method = (opts && opts.method) || 'GET';
    if (u.indexOf('/api/status') === 0) {
      return { ok: true, json: async () => [...base, SHELVED], headers: { get: () => null } };
    }
    if (u.indexOf('/api/plans') === 0 && method === 'GET') {
      const counts = { active: 0, completed: 0, closed: 0 };
      plans.forEach(p => { if (p.status in counts) counts[p.status] += 1; });
      return { ok: true, json: async () => ({ ok: true, plans, counts }), headers: { get: () => null } };
    }
    if (u.indexOf('/api/plans') === 0 && method === 'POST') {
      const body = JSON.parse(opts.body || '{}');
      calls.push(body);
      if (body.action === 'create') {
        const plan = { id: 'P-0009', prefix: body.prefix, title: body.title,
          description: body.description || '', status: 'active', status_label: '进行中',
          pinned: !!body.pinned, task_ids: body.task_ids || [], created_by: 'loopback',
          created_at: '2026-10-23T11:00:00+08:00', updated_at: '2026-10-23T11:00:00+08:00',
          completed_at: '', closed_at: '', auto_complete: body.auto_complete !== false,
          progress: { total: (body.task_ids || []).length, done: 0, rate: 0, missing: [], known: 0 } };
        plans = plans.concat([plan]);
        return { ok: true, json: async () => ({ ok: true, plan }) };
      }
      if (body.action === 'complete') {
        plans = plans.map(p => p.id === body.plan_id
          ? { ...p, status: 'completed', status_label: '已完成',
              completed_at: '2026-10-23T11:00:00+08:00', pinned: false } : p);
        return { ok: true, json: async () => ({ ok: true, plan: plans.find(p => p.id === body.plan_id) }) };
      }
      if (body.action === 'delete') {
        plans = plans.filter(p => p.id !== body.plan_id);
        return { ok: true, json: async () => ({ ok: true, plan: { id: body.plan_id, status: 'deleted' } }) };
      }
      return { ok: true, json: async () => ({ ok: true, plan: plans.find(p => p.id === body.plan_id) }) };
    }
    if (u.indexOf('/api/config') === 0) {
      return { ok: true, json: async () => ({ dashboard: {}, system: { agents: [] } }) };
    }
    if (u.indexOf('/api/chat') === 0) {
      return { ok: true, json: async () => ({ ok: true, messages: [], last_epoch: 0 }) };
    }
    return { ok: false, json: async () => ({}) };
  };
  w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  w.eval(appJs);
  await new Promise(r => setTimeout(r, 400));

  const doc = w.document;
  const shelfBody = doc.getElementById('shelf-body');
  const planBody = doc.getElementById('plan-body');
  const shelfTab = doc.getElementById('shelf-toggle');
  const planTab = doc.getElementById('plan-toggle');
  const planCards = sel => [...doc.querySelectorAll(sel + ' .plan-card')];

  // 1) 两个 Tab 在同一个 section 里，默认停在搁置且沿用原折叠行为
  assert(shelfTab && planTab, '搁置 / 计划 两个 Tab 都存在');
  assert(shelfTab.parentNode === planTab.parentNode, '两个 Tab 同属一个区域（同一 Tab 栏）');
  assert(doc.querySelectorAll('.zone-tab').length === 2, 'Tab 栏只有搁置与计划两个 Tab');
  assert(shelfTab.getAttribute('aria-selected') === 'true', '默认选中搁置');
  assert(planTab.getAttribute('aria-selected') === 'false', '默认未选中计划');
  assert(planBody.hidden === true, '计划面板默认隐藏');
  assert(shelfBody.hidden === true, '搁置面板默认仍折叠（原行为不变）');
  assert(doc.getElementById('shelf-count').textContent === '1', '搁置计数常驻 1');
  assert(doc.getElementById('plan-count').textContent === '2', '计划 Tab 上常驻进行中计数 2');

  // 2) 切到计划：搁置内容收起、计划内容出现，且搁置卡片不会被删掉
  //    （先展开搁置，让卡片进 DOM；再切 Tab，验证原卡片还在）
  shelfTab.click();
  await new Promise(r => setTimeout(r, 100));
  assert(shelfBody.hidden === false, '点搁置 Tab 可展开（原交互）');
  planTab.click();
  await new Promise(r => setTimeout(r, 300));
  assert(shelfTab.getAttribute('aria-selected') === 'false', '切到计划后搁置 Tab 取消选中');
  assert(planTab.getAttribute('aria-selected') === 'true', '计划 Tab 选中');
  assert(shelfBody.hidden === true, '切到计划后搁置面板隐藏');
  assert(planBody.hidden === false, '计划面板显示');
  assert(doc.querySelector('.shelf-list .card[data-id="T-9001"]'),
         '搁置卡片仍在 DOM 里（切 Tab 不销毁原功能）');

  // 3) 分区：进行中在上，已完成在下
  const activeCards = planCards('#plan-active-list');
  const doneCards = planCards('#plan-done-list');
  assert(activeCards.length === 2, '进行中区有 2 个计划（实际 ' + activeCards.length + '）');
  assert(doneCards.length === 1, '已完成区有 1 个计划（实际 ' + doneCards.length + '）');
  const groups = [...doc.querySelectorAll('.plan-group')].map(g => g.id);
  assert(groups[0] === 'plan-active-group' && groups[1] === 'plan-done-group',
         '进行中区排在已完成区之前（已完成在计划栏下端）');

  // 4) 卡片字段与进度
  const first = activeCards[0];
  assert(first.dataset.planId === 'P-0001', '置顶计划排在最前');
  assert(/PAY/.test(first.textContent) && /支付链路改造/.test(first.textContent),
         '卡片显示前缀与标题');
  assert(/P-0001/.test(first.textContent), '卡片显示 ID');
  assert(/alice/.test(first.textContent), '卡片显示创建人');
  assert(/1<\/b>\/2|1\/2/.test(first.innerHTML) || /1\/2/.test(first.textContent),
         '卡片显示已完成数 / 总数');
  assert(/50%/.test(first.textContent), '卡片显示完成率 50%');
  assert(first.querySelector('.plan-progress .bar i'), '卡片有进度条');
  assert(first.querySelector('[data-plan-task="T-0106"]'), '关联任务以胶囊列出');
  assert(first.querySelector('[data-plan-task="T-0106"]').classList.contains('is-done'),
         '已完成任务胶囊有 is-done 标记');
  assert(first.querySelector('.plan-pin'), '置顶计划有置顶标记');
  assert(/完成于/.test(doneCards[0].textContent), '已完成卡片显示完成时间');
  assert(second(activeCards).querySelector('.plan-warn'),
         '关联任务不在快照里时明确提示（不装作 0/1）');

  function second(list) { return list[1]; }

  // 5) 操作按钮：进行中=完成/置顶/编辑/关闭/删除；已完成=编辑/删除
  for (const a of ['complete', 'edit', 'close', 'delete']) {
    assert(first.querySelector('[data-plan-action="' + a + '"]'), '进行中卡片有「' + a + '」按钮');
  }
  // 置顶计划显示「取消置顶」，未置顶的显示「置顶」——同一个位置两个动作
  assert(first.querySelector('[data-plan-action="unpin"]'), '置顶卡片有「取消置顶」');
  assert(second(activeCards).querySelector('[data-plan-action="pin"]'),
         '未置顶卡片有「置顶」');
  assert(!doneCards[0].querySelector('[data-plan-action="complete"]'),
         '已完成卡片不再有「完成」按钮');
  assert(!doneCards[0].querySelector('[data-plan-action="pin"]'),
         '已完成卡片不再有「置顶」按钮');

  // 5b) 「已完成」卡不再出现「关闭」按钮；手动完成未做完时徽标写「手动完成」
  assert(!doneCards[0].querySelector('[data-plan-action="close"]'),
         '已完成卡片不显示「关闭」（对已结束的计划是空操作）');
  {
    // P-0002 只完成 1/2 个关联任务：手动完成后徽标必须写「手动完成」，
    // 否则「已完成」和 50% 的进度条会互相打脸。
    const half = activeCards.find(c => c.dataset.planId === 'P-0002');
    half.querySelector('[data-plan-action="complete"]').click();
    await new Promise(r => setTimeout(r, 250));
    const moved = planCards('#plan-done-list').find(c => c.dataset.planId === 'P-0002');
    assert(moved, 'P-0002 完成后出现在已完成区');
    assert(/手动完成/.test(moved.textContent), '未做完却手动完成时徽标写「手动完成」');
    assert(/手动标记完成，仍有 1 个关联任务未完成/.test(moved.textContent),
           '并明确提示还有未完成的关联任务');
    assert(/\/\d+ 关联任务完成/.test(moved.textContent),
           '完成后的进度文案写「关联任务完成」，不与徽标重复（实际：' +
           moved.textContent.replace(/\s+/g, ' ').slice(0, 120) + '）');
  }

  // 6) 完成 → 二次确认 → 请求完成 → 卡片移入已完成区
  confirmAnswer = true;
  activeCards[0].querySelector('[data-plan-action="complete"]').click();
  await new Promise(r => setTimeout(r, 300));
  assert(callOf('complete'), '点「完成」发出 complete 请求');
  assert(planCards('#plan-active-list').length === 1, '完成后进行中区少一个（实际 '
         + planCards('#plan-active-list').length + '）');
  assert(planCards('#plan-done-list').length === 2, '完成后已完成后区多一个');

  function callOf(action) { return calls.filter(c => c.action === action); }

  // 7) 关闭 / 删除都要二次确认；取消则不请求
  const before = calls.length;
  confirmAnswer = false;
  const card2 = planCards('#plan-active-list')[0];
  card2.querySelector('[data-plan-action="close"]').click();
  card2.querySelector('[data-plan-action="delete"]').click();
  await new Promise(r => setTimeout(r, 200));
  assert(confirmations.length >= 2, '关闭与删除都弹了确认框');
  assert(calls.length === before, '取消确认后没有发出任何写请求');
  confirmAnswer = true;

  // 8) 删除带 confirm=true（服务端据此区分「误触」与「确认」）
  planCards('#plan-active-list')[0].querySelector('[data-plan-action="delete"]').click();
  await new Promise(r => setTimeout(r, 300));
  const del = callOf('delete')[0];
  assert(del && del.confirm === true, '删除请求带 confirm=true');

  // 9) 新建：弹窗字段 → POST create
  doc.getElementById('plan-new').click();
  await new Promise(r => setTimeout(r, 50));
  assert(doc.getElementById('plan-modal').hidden === false, '点「新建计划」打开弹窗');
  doc.getElementById('plan-prefix').value = 'QA';
  doc.getElementById('plan-title-input').value = '回归测试补齐';
  doc.getElementById('plan-desc').value = '覆盖计划链路';
  doc.getElementById('plan-tasks').value = 'T-0105，T-0106';
  doc.getElementById('plan-pinned').checked = true;
  doc.getElementById('plan-form').dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
  await new Promise(r => setTimeout(r, 300));
  const created = callOf('create')[0];
  assert(created, '提交表单发出 create 请求');
  assert(created.prefix === 'QA' && created.title === '回归测试补齐', 'create 带上前缀与标题');
  assert(created.task_ids.length === 2 && created.task_ids[0] === 'T-0105',
         '中文逗号分隔的任务号被正确解析');
  assert(created.pinned === true, '置顶勾选被带上');
  assert(doc.getElementById('plan-modal').hidden === true, '保存后弹窗关闭');

  // 10) 空态：计划为空时两个区都有明确文案
  plans = [];
  shelfTab.click();
  await new Promise(r => setTimeout(r, 100));
  planTab.click();
  await new Promise(r => setTimeout(r, 400));
  assert(doc.getElementById('plan-active-empty').hidden === false, '无进行中计划时显示空态');
  assert(doc.getElementById('plan-done-empty').hidden === false, '无已完成计划时显示空态');

  // 11) 切回搁置：原有折叠/展开与搜索提示仍然有效
  shelfTab.click();
  await new Promise(r => setTimeout(r, 300));
  assert(shelfBody.hidden === false, '切回搁置会展开（保持原交互）');
  assert(planBody.hidden === true, '计划面板重新隐藏');
  shelfTab.click();
  await new Promise(r => setTimeout(r, 100));
  assert(shelfBody.hidden === true, '再点搁置可收起（原折叠行为未受影响）');
  assert(doc.getElementById('shelf-count').textContent === '1', '搁置计数不因切 Tab 变化');

  assert(jsErrors.length === 0, '没有 JS 运行错误（jsdomError=' + jsErrors.length + '）');
  dom.window.close();
  console.log(process.exitCode ? '\nSOME CHECKS FAILED' : '\nALL PLAN UI CHECKS PASSED');
})();
