/* 搁置面板的 jsdom 行为测试（可选：需 jsdom）。
 *
 * 跑法：NODE_PATH=<装了 jsdom 的 node_modules> node dev/shelf-ui-test.js
 *
 * 验证的核心是排版取舍：搁置卡**不占第 7 列**，平时只收成一条横条；
 * 但计数常驻、点开就能看到「为什么停」和三个出口，搜索命中时也会提示。
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
  stage: 'shelved', dispatched_to: '', dispatched_at: '', finished_at: '',
  duration_s: 0, head: '', verify: [], result_summary: '', summary: '',
  shelved_at: '2026-10-22T09:00:00+08:00',
  shelved_from: 'ready',
  shelve_reason: '上游 SDK 还没发版，等 12 月',
};
const data = [...base, SHELVED];

function assert(cond, msg) {
  if (!cond) { console.error('FAIL:', msg); process.exitCode = 1; }
  else { console.log('ok  -', msg); }
}

(async () => {
  const vc = new VirtualConsole();
  const jsErrors = [];
  vc.on('jsdomError', e => jsErrors.push(String(e)));
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
  w.fetch = async (url) => {
    if (String(url).indexOf('/api/status') === 0) {
      return { ok: true, json: async () => data, headers: { get: () => null } };
    }
    return { ok: false, json: async () => ({}) };
  };
  w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  w.eval(appJs);
  await new Promise(r => setTimeout(r, 300));

  const doc = w.document;
  const byId = id => doc.querySelector(`.card[data-id="${id}"]`);
  const colCards = key => [...doc.querySelectorAll(`.column[data-key="${key}"] .card`)];
  const shelf = doc.getElementById('shelf');
  const body = doc.getElementById('shelf-body');
  const toggle = doc.getElementById('shelf-toggle');

  // 1) 不占任何一列：六列的卡片集合里绝不能出现搁置卡
  const allColIds = [...doc.querySelectorAll('.column .card')].map(c => c.dataset.id);
  assert(!allColIds.includes('T-9001'), '搁置卡不落到任何看板列');
  assert(!colCards('abnormal').length || !byId('T-9001'), '搁置卡不进异常列');
  assert(doc.querySelectorAll('.column').length === 6, '看板仍是 6 列，没有第 7 列');

  // 2) 默认收起，但计数常驻（不能忘、平时又能忽略）
  assert(body.hidden === true, '搁置面板默认折叠');
  assert(doc.getElementById('shelf-count').textContent === '1', '横条上常驻计数 1');
  assert(toggle.getAttribute('aria-expanded') === 'false', '折叠态 aria-expanded=false');
  const statShelf = doc.querySelector('.stat-shelf');
  assert(statShelf && /搁置/.test(statShelf.textContent) && /1/.test(statShelf.textContent),
         '统计条里有「搁置 1」入口');
  const total = doc.querySelector('.stat-total').textContent;
  assert(/共\s*8\s*个任务/.test(total), '活跃总数不含搁置卡（共 8 个任务，实际：' + total + '）');

  // 3) 搜索命中搁置卡时，折叠态也要给提示，别让人以为卡丢了
  const search = doc.getElementById('search');
  search.value = 'T-9001';
  search.dispatchEvent(new w.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 50));
  assert(doc.getElementById('shelf-hit').hidden === false, '搜索命中搁置卡时有「有匹配」提示');
  assert([...doc.querySelectorAll('.column .card')].length === 0, '搜索时活跃列没有误命中');
  search.value = '';
  search.dispatchEvent(new w.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 50));
  assert(doc.getElementById('shelf-hit').hidden === true, '清空搜索后提示消失');

  // 4) 点开：卡片带现场 + 三个出口
  toggle.click();
  await new Promise(r => setTimeout(r, 50));
  assert(body.hidden === false && shelf.classList.contains('is-open'), '点击横条可展开');
  assert(toggle.getAttribute('aria-expanded') === 'true', '展开态 aria-expanded=true');
  const sc = doc.querySelector('.shelf-list .card[data-id="T-9001"]');
  assert(sc, '展开后搁置卡出现在搁置面板');
  assert(/上游 SDK 还没发版/.test(sc.textContent), '卡片显示搁置原因');
  assert(/搁置于/.test(sc.textContent) && /原状态 ready/.test(sc.textContent),
         '卡片显示搁置时间与原状态');
  for (const action of ['unshelve', 'respawn', 'close']) {
    assert(sc.querySelector(`[data-action="${action}"]`), `搁置卡有「${action}」出口`);
  }
  assert(sc.querySelector('.shelf-badge'), '卡片有低调的「搁置」徽标');
  assert(sc.querySelector('[data-copy]'), '复制按钮拿的是搁置说明');

  // 5) 活跃列有「搁置」入口，已关闭列没有
  assert(doc.querySelector('.column[data-key="ready"] [data-action="shelve"]'),
         '待领取列卡片有「搁置」按钮');
  assert(!doc.querySelector('.column[data-key="recent"] [data-action="shelve"]'),
         '已完成卡片不显示搁置按钮');

  // 6) 详情抽屉也要把「为什么停」讲清楚
  sc.click();
  await new Promise(r => setTimeout(r, 50));
  const drawer = doc.getElementById('drawer');
  assert(drawer.getAttribute('aria-hidden') === 'false', '点搁置卡可打开详情');
  assert(/上游 SDK 还没发版/.test(drawer.textContent), '抽屉里有搁置原因');
  assert(/日后处置/.test(drawer.textContent), '抽屉里写明三个出口');

  assert(jsErrors.length === 0, '没有 JS 运行错误（jsdomError=' + jsErrors.length + '）');
  dom.window.close();
  console.log(process.exitCode ? '\nSOME CHECKS FAILED' : '\nALL SHELF UI CHECKS PASSED');
})();
