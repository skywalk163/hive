/* 环境性假红 / 人工判绿的看板徽标与详情（T-0342 / T-0344 回归）。
 * 与 ui-test.js 同骨架：jsdom 加载真实 index.html + app.js，喂自定义 status。
 * 由 tests/test_dashboard_env_badge_ui.py 调起；没装 jsdom 时该测试会跳过。
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const DIR = path.resolve(__dirname, '..');
let html = fs.readFileSync(path.join(DIR, 'index.html'), 'utf8');
html = html.replace('<script src="app.js" defer></script>', '');
const appJs = fs.readFileSync(path.join(DIR, 'app.js'), 'utf8');
const FIXED = Date.parse('2026-11-13T15:00:00+08:00');

const status = [
  {
    id: 'T-9001', title: '缺 torch 导致 pytest 收集为空', state: 'ready',
    repo: 'hive', priority: 'P1', owner: '', claim_agent: '',
    result_status: 'failed', failure_kind: 'environment',
    env_error_hint: '请到目标机复跑；结果仍按 failed 归档，绝不自动判绿',
    verify_env_errors: [{ cmd: 'python3 -m pytest -q tests/', code: 4,
                          kind: 'no_tests_collected', reason: 'pytest 收集阶段就结束了' }],
    verify: [{ cmd: 'python3 -m pytest -q tests/', code: 4,
               tail: ['ERROR: found no collectors'] }],
    result_summary: '目标机上两条用例全绿', land_ready: null
  },
  {
    id: 'T-9002', title: '人工判绿收口', state: 'done',
    repo: 'hive', priority: 'P1', owner: '', claim_agent: '',
    result_status: 'failed', verdict_mismatch: true,
    result_error: 'verify 未通过', verify: [],
    result_summary: '人工裁决通过', land_ready: null
  }
];

function assert(cond, msg) {
  if (!cond) { console.error('FAIL:', msg); process.exitCode = 1; }
  else { console.log('ok  -', msg); }
}

(async () => {
  const vc = new VirtualConsole();
  const jsErrors = [];
  vc.on('jsdomError', e => jsErrors.push(String(e)));
  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8787/', runScripts: 'outside-only',
    pretendToBeVisual: true, virtualConsole: vc
  });
  const w = dom.window;
  const RealDate = w.Date;
  class FakeDate extends RealDate {
    constructor(...args) { if (!args.length) super(FIXED); else super(...args); }
    static now() { return FIXED; }
  }
  w.Date = FakeDate;
  w.fetch = async () => ({ ok: true, json: async () => status });
  w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  w.eval(appJs);
  await new Promise(r => setTimeout(r, 300));

  const doc = w.document;
  const byId = id => doc.querySelector(`.card[data-id="${id}"]`);
  const colCards = key => [...doc.querySelectorAll(`.column[data-key="${key}"] .card`)];

  // normalize 是白名单：新字段漏一个，这里就会退回普通 failed 徽标
  assert(!!byId('T-9001') && !!byId('T-9002'), '两张卡都渲染出来');
  assert(colCards('abnormal').length === 1 && colCards('abnormal')[0].dataset.id === 'T-9001',
    '环境假红仍留在异常列（不判绿、不隐藏）');
  assert(/env-failed/.test(byId('T-9001').textContent), 'T-9001 显示 env-failed 徽标');
  assert(/人工判绿·台账 failed/.test(byId('T-9002').textContent),
    'T-9002 显示人工判绿徽标，不再是一枚裸 failed');
  assert(!/badge danger">failed/.test(byId('T-9002').innerHTML),
    '人工判绿卡不该再挂红色 failed 徽标');

  byId('T-9001').click();
  await new Promise(r => setTimeout(r, 50));
  const drawer = doc.getElementById('drawer');
  assert(drawer.getAttribute('aria-hidden') === 'false', '点卡片打开详情抽屉');
  assert(/环境性假红（未自动判绿）/.test(drawer.textContent), '详情里有环境假红说明');
  assert(/请到目标机复跑/.test(drawer.textContent), '详情里有复跑建议');
  assert(/绝不自动判绿/.test(drawer.textContent), '详情里明确不判绿');
  assert(/pytest 收集阶段就结束了/.test(drawer.textContent), '详情里有逐条原因');

  assert(jsErrors.length === 0, '没有 jsdom 脚本错误：' + jsErrors.join(' | '));
  // app.js 里有自动刷新定时器，不关 window 的话 node 事件循环永远不退出
  dom.window.close();
  console.log(process.exitCode ? '\nSOME CHECKS FAILED'
                               : '\nALL ENV BADGE UI CHECKS PASSED');
})().catch(e => { console.error('FAIL: 脚本异常', e); process.exitCode = 1; });
