const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const DIR = path.resolve(__dirname, '..');
let html = fs.readFileSync(path.join(DIR, 'index.html'), 'utf8');
html = html.replace('<script src="app.js" defer></script>', '');
const appJs = fs.readFileSync(path.join(DIR, 'app.js'), 'utf8');
const sample = JSON.parse(fs.readFileSync(path.join(DIR, 'sample-status.json'), 'utf8'));
const FIXED = Date.parse('2026-10-23T11:00:00+08:00');

function assert(cond, msg) {
  if (!cond) { console.error('FAIL:', msg); process.exitCode = 1; }
  else { console.log('ok  -', msg); }
}

(async () => {
  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8787/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const w = dom.window;
  const RealDate = w.Date;
  class FakeDate extends RealDate {
    constructor(...args) { if (!args.length) super(FIXED); else super(...args); }
    static now() { return FIXED; }
  }
  w.Date = FakeDate;
  w.fetch = async () => ({ ok: true, json: async () => sample });
  w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  w.eval(appJs);
  await new Promise(r => setTimeout(r, 300));

  const doc = w.document;
  const cards = () => [...doc.querySelectorAll('.card')];
  const colCards = key => [...doc.querySelectorAll(`.column[data-key="${key}"] .card`)];
  const byId = id => doc.querySelector(`.card[data-id="${id}"]`);

  assert(colCards('running').length === 1 && byId('T-0101'), '进行中 1 张（T-0101）');
  assert(colCards('abnormal').length === 2 && byId('T-0102') && byId('T-0104'), '异常 2 张（T-0102/T-0104）');
  assert(colCards('merging').length === 1 && byId('T-0103'), '待合并 1 张（T-0103）');
  assert(colCards('ready').length === 1 && byId('T-0105'), '待领取 1 张（T-0105）');
  const closedCol = doc.querySelector('.column[data-key="closed"]');
  assert(closedCol.querySelector('.count').textContent === '1', '已关闭列计数 1');
  assert(closedCol.querySelector('.col-body').hidden === true, '已关闭列默认折叠');
  closedCol.querySelector('.col-head button').click();
  await new Promise(r => setTimeout(r, 50));
  assert(closedCol.querySelector('.col-body').hidden === false, '已关闭列可展开');
  assert(colCards('closed').length === 1 && byId('T-0106'), '展开后出现 T-0106');
  assert(cards().length === 6, '总共 6 张卡');

  assert(byId('T-0102').classList.contains('is-stale'), 'T-0102 有 is-stale 红边框类');
  assert(/已过期/.test(byId('T-0102').textContent), 'T-0102 文案是「已过期」');
  assert(/通过 0\/1/.test(byId('T-0104').textContent), 'T-0104 显示 0/1 通过');
  assert(/依赖/.test(byId('T-0105').textContent), 'T-0105 显示依赖');

  const search = doc.getElementById('search');
  search.value = 'lightmem';
  search.dispatchEvent(new w.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 50));
  assert(cards().length === 1 && byId('T-0103'), '搜索 lightmem 只剩 T-0103');

  search.value = '';
  search.dispatchEvent(new w.Event('input', { bubbles: true }));
  const repo = doc.getElementById('repo-filter');
  repo.value = 'light';
  repo.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 50));
  assert(cards().length === 4 && !byId('T-0103'), '仓库筛选 light 得到 4 张');

  repo.value = '';
  repo.dispatchEvent(new w.Event('change', { bubbles: true }));
  await new Promise(r => setTimeout(r, 50));
  byId('T-0104').click();
  await new Promise(r => setTimeout(r, 50));
  const drawer = doc.getElementById('drawer');
  assert(drawer.getAttribute('aria-hidden') === 'false', '点卡片打开详情抽屉');
  assert(/test_ps\.py::test_bsd_flags/.test(drawer.textContent), '抽屉里有失败用例名');
  doc.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await new Promise(r => setTimeout(r, 50));
  assert(drawer.getAttribute('aria-hidden') === 'true', 'Esc 关闭抽屉');

  assert(doc.querySelectorAll('a[href^="http"], img[src^="http"], script[src^="http"]').length === 0, '没有外链资源');
  dom.window.close();
  console.log(process.exitCode ? '\nSOME CHECKS FAILED' : '\nALL UI CHECKS PASSED');
})();
