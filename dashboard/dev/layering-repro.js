/* T-0207 复现/验收：详情抽屉打开后是否盖住顶栏右侧的筛选区。
 *
 * 用法（与 ui-test.js 同套 jsdom；需要 npm i jsdom）：
 *   node dashboard/dev/layering-repro.js
 *
 * 注意：jsdom 不做真实布局，getBoundingClientRect 全为 0。要拿到「真实」
 * 交集面积，请用 headless Chromium/Edge 跑同一个 DOM 计算（本脚本的几何
 * 函数 intersectionArea / hitIsSelect 与浏览器里一致）。CI 里没有浏览器驱动
 * 时，pytest 的 UI 层会 skip（见 tests/test_dashboard_layering.py）。
 *
 * 输出一行 LAYERING_RESULT=<json>，字段：
 *   intersectionArea  .drawer 与 #repo-filter 的 getBoundingClientRect 交集 px²
 *   hitTag            elementFromPoint(#repo-filter 中心) 命中的标签/类
 *   hitIsSelect       命中的是不是 select 本体（true=筛选可用）
 */
const fs = require('fs');
const path = require('path');

const DIR = path.resolve(__dirname, '..');
let html = fs.readFileSync(path.join(DIR, 'index.html'), 'utf8');
html = html.replace('<script src="app.js" defer></script>', '');
const appJs = fs.readFileSync(path.join(DIR, 'app.js'), 'utf8');
const sample = JSON.parse(fs.readFileSync(path.join(DIR, 'sample-status.json'), 'utf8'));

function intersectionArea(a, b) {
  const x = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
  const y = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
  return x * y;
}

(async () => {
  const { JSDOM, VirtualConsole } = require('jsdom');
  const vc = new VirtualConsole();
  vc.on('jsdomError', () => {});
  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8787/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
    virtualConsole: vc,
  });
  const w = dom.window;
  w.fetch = async () => ({ ok: true, json: async () => sample });
  w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  w.eval(appJs);
  await new Promise(r => setTimeout(r, 300));

  const doc = w.document;
  // 打开一张卡的详情抽屉
  const card = doc.querySelector('.card[data-id="T-0104"]') || doc.querySelector('.card');
  if (card) card.click();
  await new Promise(r => setTimeout(r, 80));

  const drawer = doc.getElementById('drawer');
  const repoFilter = doc.getElementById('repo-filter');
  const dr = drawer.getBoundingClientRect();
  const rr = repoFilter.getBoundingClientRect();
  const area = intersectionArea(dr, rr);

  // 点 #repo-filter 本体中心，看实际命中谁
  const cx = rr.left + rr.width / 2;
  const cy = rr.top + rr.height / 2;
  const hit = doc.elementFromPoint(cx, cy);
  const hitIsSelect = !!hit && hit.tagName === 'SELECT';
  const hitTag = hit ? (hit.tagName + (hit.className ? '.' + String(hit.className).split(' ')[0] : '')) : null;

  const result = {
    drawerRect: { left: dr.left, top: dr.top, right: dr.right, bottom: dr.bottom },
    repoRect: { left: rr.left, top: rr.top, right: rr.right, bottom: rr.bottom },
    intersectionArea: area,
    hitTag,
    hitIsSelect,
    drawerOpen: drawer.getAttribute('aria-hidden') === 'false',
  };
  console.log('LAYERING_RESULT=' + JSON.stringify(result));
  dom.window.close();
})();
