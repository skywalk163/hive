#!/usr/bin/env node
/* T-0354 同步健康度四档分支的纯函数行为测试（零依赖，不需要 jsdom / 浏览器）。
 *
 * app.js 是 IIFE，内部函数不外露。这里把
 *     // === SYNC HEALTH PURE BEGIN ===  ...  // === SYNC HEALTH PURE END ===
 * 之间那段**纯函数**（不碰 DOM / 网络 / 闭包变量）原样抠出来，在 node 的 vm 里
 * 求值后逐条断言。它和 tests/test_dashboard_hlsa.py 的静态断言互补：
 *   - Python 侧钉「四档文案在不在、旧的拉取提示是否清零」（不依赖 node）；
 *   - 这个脚本钉「给什么输入落哪个分支」（可选，装了 node 就能跑）。
 *
 * 用法：node dashboard/dev/hlsa_banner_test.js
 * 退出码 0 = 全过；1 = 有断言失败（会打印到 stderr）。
 */
'use strict';

var fs = require('fs');
var path = require('path');
var vm = require('vm');

var APP = path.join(__dirname, '..', 'app.js');
var BEGIN = '// === SYNC HEALTH PURE BEGIN ===';
var END = '// === SYNC HEALTH PURE END ===';

var failures = 0;
var checks = 0;

function ok(cond, msg) {
  checks += 1;
  if (!cond) {
    failures += 1;
    console.error('FAIL: ' + msg);
  }
}

function eq(actual, expected, msg) {
  ok(actual === expected, msg + '（期望 ' + JSON.stringify(expected) +
     '，实际 ' + JSON.stringify(actual) + '）');
}

function contains(haystack, needle, msg) {
  ok(String(haystack || '').indexOf(needle) !== -1,
     msg + '（应包含 ' + JSON.stringify(needle) + '，实际 ' + JSON.stringify(haystack) + '）');
}

function notContains(haystack, needle, msg) {
  ok(String(haystack || '').indexOf(needle) === -1,
     msg + '（不应包含 ' + JSON.stringify(needle) + '，实际 ' + JSON.stringify(haystack) + '）');
}

function loadPure() {
  var src = fs.readFileSync(APP, 'utf8');
  var start = src.indexOf(BEGIN);
  var stop = src.indexOf(END);
  if (start < 0 || stop < 0 || stop <= start) {
    console.error('FAIL: app.js 里找不到 SYNC HEALTH 纯函数区块的 BEGIN/END 标记');
    process.exit(1);
  }
  var code = src.slice(start + BEGIN.length, stop);
  var sandbox = {
    Date: Date, Math: Math, Number: Number, Object: Object,
    String: String, Array: Array, JSON: JSON,
    isFinite: isFinite, isNaN: isNaN, parseFloat: parseFloat
  };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: 'app.js#sync-health' });
  return sandbox;
}

function iso(msAgo) {
  return new Date(Date.now() - (msAgo || 0)).toISOString();
}

function main() {
  var api = loadPure();
  var mode = api.syncHealthMode;
  var banner = api.syncHealthBanner;
  var chip = api.syncHealthChip;
  var seqOf = api.syncSeqOf;
  var retry = api.syncRetryText;

  // ---------------- 1) 同步正常：横幅隐藏，顶栏有 seq / 最后同步 / 档位 ----------------
  var healthy = { lsdb_seq: 12, last_sync_at: iso(3 * 60 * 1000), tier: 'T1',
                  behind: 0, reason: 'synced' };
  eq(mode(healthy), 'ok', '同步正常应判 ok');
  eq(banner(healthy), null, '同步正常时横幅必须隐藏（返回 null）');
  var chipOk = chip(healthy, { nowMs: Date.now() });
  contains(chipOk.text, 'LSDB seq 12', '顶栏应显示 LSDB seq');
  contains(chipOk.text, '最后同步 3 分钟前', '顶栏应显示最后同步的相对时间');
  contains(chipOk.text, '档位 T1', '顶栏应显示档位');
  eq(seqOf(healthy), 12, 'syncSeqOf 应取到 lsdb_seq');

  // ---------------- 2) 同步滞后：不叫用户去 pull，只给自动重试倒计时 ----------------
  var behind = { lsdb_seq: 13, tier: 'T0', behind: 3, reason: 'syncing' };
  eq(mode(behind), 'behind', 'behind>0 应判 behind');
  var behindView = banner(behind, { retryInSeconds: 12 });
  eq(behindView.mode, 'behind', '滞后横幅 mode 应为 behind');
  contains(behindView.text, '状态同步中', '滞后横幅文案');
  contains(behindView.text, '已落后 3 条', '滞后横幅应给出落后条数');
  contains(behindView.text, '约 12 秒后自动重试', '滞后横幅应给自动重试倒计时');
  contains(banner(behind, { retryInSeconds: -1 }).text, '自动刷新已暂停',
           '自动刷新被关掉时不能承诺自动重试');
  eq(retry(0), '稍后自动重试', '倒计时未知时的兜底文案');

  // ---------------- 3) 本地有未提交改动：不是故障，也不提供一键拉取 ----------------
  var dirty = { lsdb_seq: 14, reason: 'dirty', local_dirty: true, behind: 2 };
  eq(mode(dirty), 'dirty', 'local_dirty 应判 dirty（脏优先于落后条数）');
  var dirtyView = banner(dirty);
  contains(dirtyView.text, '本地有未提交改动，自动同步已跳过', '本地脏的横幅文案');
  contains(dirtyView.text, '改动的主人', '本地脏应提示联系改动主人');

  // ---------------- 4) 本地与 origin 分叉：需人工裁决 + 两个 sha ----------------
  var diverged = {
    lsdb_seq: 15, reason: 'diverged', diverged: true,
    head_sha: 'abcdef1234567890', remote_sha: 'fedcba0987654321'
  };
  eq(mode(diverged), 'diverged', 'diverged 应判分叉');
  var divView = banner(diverged);
  eq(divView.level, 'danger', '分叉是唯一的危险档');
  contains(divView.text, '分叉', '分叉横幅文案');
  contains(divView.text, '人工裁决', '分叉必须写清「需人工裁决」');
  contains(divView.mono.join(' / '), 'head abcdef1', '分叉横幅应给 head_sha');
  contains(divView.mono.join(' / '), 'origin fedcba0', '分叉横幅应给 remote_sha');

  // ---------------- 5) 旧后端没有 sync_health：降级到 local_stale，不白屏 ----------------
  eq(mode(null), 'legacy', '没有 sync_health 应判 legacy');
  eq(mode({ degraded: true }), 'legacy', 'degraded 记录应判 legacy');
  eq(banner(null, { legacyLocalStale: false }), null,
     '旧后端且未落后时不该有横幅');
  var legacyView = banner(null, { legacyLocalStale: true });
  contains(legacyView.text, '同步健康度不可用（旧版后端）', '旧后端降级文案');
  contains(legacyView.text, '落后 origin/main', '旧后端降级要保留事实描述');
  contains(chip(null).text, '旧版后端', '旧后端顶栏降级文案');

  // ---------------- 6) 四档文案里不许再出现无差别的「去拉一下」 ----------------
  [behindView, dirtyView, divView, legacyView].forEach(function (view, i) {
    notContains(view.text, 'git pull', '第 ' + (i + 1) + ' 档文案不许出现 git pull');
    notContains(view.text, 'sync-repos', '第 ' + (i + 1) + ' 档文案不许出现 sync-repos');
  });

  // ---------------- 7) 显式 status/state 优先（给后端留前向兼容口） ----------------
  eq(mode({ status: 'ok', reason: 'dirty' }), 'ok', '显式 status=ok 优先于 reason');
  eq(mode({ status: 'dirty', diverged: true }), 'dirty', '显式 status 优先于启发式');

  if (failures) {
    console.error('\n' + failures + ' / ' + checks + ' 项断言失败');
    process.exit(1);
  }
  console.log('ALL SYNC HEALTH UI CHECKS PASSED (' + checks + ' 项断言)');
}

main();
