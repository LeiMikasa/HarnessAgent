/**
 * snake.html 测试套件 —— 零依赖，使用 Node 内置 test runner 运行：
 *
 *   node --test tests/snake_core.test.mjs
 *
 * 分三层：
 *   1) 纯逻辑核心（snake.html 里 CORE:BEGIN/END 之间的代码）单元测试
 *   2) 页面静态检查（自包含、脚本语法、必需的 DOM 节点、编码约定）
 *   3) 无头冒烟集成测试（用最小 DOM 桩驱动真实 UI 脚本跑完整局）
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import assert from 'node:assert/strict';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SNAKE_HTML = path.join(ROOT, 'snake.html');
const HTML = readFileSync(SNAKE_HTML, 'utf8');

/* ------------------------------------------------------------------ 提取 */

const CORE_RE = /\/\* ==== CORE:BEGIN ==== \*\/([\s\S]*?)\/\* ==== CORE:END ==== \*\//;

function extractCore(html = HTML) {
  const m = html.match(CORE_RE);
  assert.ok(m, 'snake.html 必须包含 CORE:BEGIN / CORE:END 标记');
  return m[1];
}

function extractScripts(html = HTML) {
  return [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/gi)].map((m) => m[1]);
}

/** 在隔离的 vm 上下文里加载纯逻辑核心，返回 { core, sandbox }。 */
function loadCore() {
  const sandbox = { module: { exports: {} }, console };
  vm.createContext(sandbox);
  vm.runInContext(extractCore(), sandbox, { filename: 'snake-core.js' });
  const core = sandbox.module.exports;
  assert.ok(core && typeof core.createGame === 'function', '核心必须导出 createGame');
  return { core, sandbox };
}

const { core } = loadCore();

/* vm 上下文里创建的对象原型与本上下文不同，deepStrictEqual 会报「结构相同但非同源」，
   因此统一先把跨 realm 的对象归一化成本地对象再比较。 */
const plain = (o) => JSON.parse(JSON.stringify(o));
const assertCell = (actual, x, y) => assert.deepEqual(plain(actual), { x: x, y: y });
const assertEvent = (actual, expected) => assert.deepEqual(plain(actual), expected);

/* --------------------------------------------------------------- DOM 桩 */

function makeClassList() {
  const set = new Set();
  return {
    add(...c) { c.forEach((x) => set.add(x)); },
    remove(...c) { c.forEach((x) => set.delete(x)); },
    contains(c) { return set.has(c); },
    toggle(c, force) { ((force === undefined ? !set.has(c) : force) ? set.add(c) : set.delete(c)); },
    _set: set
  };
}

function makeCtx2d() {
  const base = {
    createLinearGradient: () => ({ addColorStop() {} }),
    createRadialGradient: () => ({ addColorStop() {} }),
    calls: []
  };
  return new Proxy(base, {
    get(target, prop) {
      if (prop in target) return target[prop];
      return (...args) => { target.calls.push(prop); void args; };
    },
    set(target, prop, value) { target[prop] = value; return true; }
  });
}

function makeElement(id) {
  const listeners = new Map();
  const attrs = new Map();
  const el = {
    id,
    tagName: id === 'board' ? 'CANVAS' : (id === 'overlayBtn' ? 'BUTTON' : 'DIV'),
    textContent: '',
    innerHTML: '',
    hidden: false,
    width: 0,
    height: 0,
    style: {},
    dataset: {},
    classList: makeClassList(),
    _ctx: null,
    getContext() {
      if (!el._ctx) el._ctx = makeCtx2d();
      return el._ctx;
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const list = listeners.get(type) || [];
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    },
    dispatch(type, event = {}) {
      const ev = Object.assign({ type, target: el, preventDefault() {}, stopPropagation() {} }, event);
      for (const fn of listeners.get(type) || []) fn(ev);
      return ev;
    },
    listenerCount(type) { return (listeners.get(type) || []).length; },
    setAttribute(k, v) { attrs.set(k, String(v)); if (k === 'data-phase') el.dataset.phase = String(v); },
    getAttribute(k) { return attrs.has(k) ? attrs.get(k) : null; },
    focus() {},
    appendChild() {},
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 600, height: 600 }; }
  };
  return el;
}

/** 构造最小 DOM / window 环境，并加载真实的 core + ui 脚本。 */
function bootPage() {
  const registry = new Map();
  const windowTarget = makeElement('window');

  const document = {
    hidden: false,
    addEventListener: (...a) => windowTarget.addEventListener(...a),
    removeEventListener: (...a) => windowTarget.removeEventListener(...a),
    dispatch: (...a) => windowTarget.dispatch(...a),
    getElementById(id) {
      if (!registry.has(id)) registry.set(id, makeElement(id));
      return registry.get(id);
    },
    createElement: (tag) => makeElement('created:' + tag),
    querySelectorAll: () => []
  };

  const store = new Map();
  const localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
    _store: store
  };

  const rafQueue = [];
  let rafId = 0;
  const sandbox = {
    console,
    module: { exports: {} },
    document,
    localStorage,
    devicePixelRatio: 1,
    requestAnimationFrame(cb) { rafQueue.push(cb); return ++rafId; },
    cancelAnimationFrame() {},
    performance: { now: () => 0 }
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);

  const scripts = extractScripts();
  assert.equal(scripts.length, 2, '期望恰好两个内联 script（core + ui）');
  vm.runInContext(scripts[0], sandbox, { filename: 'snake-core.js' });
  vm.runInContext(scripts[1], sandbox, { filename: 'snake-ui.js' });

  const api = sandbox.__SNAKE__;
  assert.ok(api, 'UI 脚本必须暴露 window.__SNAKE__ 调试句柄');

  return { sandbox, api, document, localStorage, els: (id) => document.getElementById(id), rafQueue };
}

/** 按固定步长推进若干帧；首帧只用于校准时钟（dt = 0）。 */
function advance(api, ms, step = 50, start = 1000) {
  let ts = start;
  api.frame(ts);
  const frames = Math.ceil(ms / step);
  for (let i = 0; i < frames; i++) {
    ts += step;
    api.frame(ts);
  }
  return ts;
}

/* ==================================================================== 1. 核心逻辑 */

test('createGame：初始状态正确', () => {
  const s = core.createGame({ cols: 20, rows: 20 });
  assert.equal(s.cols, 20);
  assert.equal(s.rows, 20);
  assert.equal(s.snake.length, 3);
  assertCell(s.snake[0], 10, 10);
  assertCell(s.snake[1], 9, 10);
  assert.equal(s.dir, 'right');
  assert.equal(s.score, 0);
  assert.equal(s.level, 1);
  assert.equal(s.steps, 0);
  assert.equal(s.over, false);
  assert.ok(s.food, '初始必须有食物');
  assert.ok(!core.containsCell(s.snake, s.food.x, s.food.y), '食物不能落在蛇身上');
});

test('createGame：起始长度被限制在合法范围', () => {
  assert.equal(core.createGame({ cols: 10, rows: 10, startLength: 1 }).snake.length, 2);
  assert.equal(core.createGame({ cols: 5, rows: 5, startLength: 99 }).snake.length, 5);
});

test('freeCells：只返回空格子，数量守恒', () => {
  const s = core.createGame({ cols: 6, rows: 4 });
  const free = core.freeCells(s);
  assert.equal(free.length, 6 * 4 - s.snake.length);
  for (const c of free) assert.ok(!core.containsCell(s.snake, c.x, c.y));
});

test('step：普通一步只移动，不吃不变长', () => {
  const s = core.createGame({ cols: 20, rows: 20, rng: core.createRng(1) });
  const len = s.snake.length;
  const ev = core.step(s);
  assertEvent(ev, { moved: true, ate: false, over: false, reason: null });
  assertCell(s.snake[0], 11, 10);
  assert.equal(s.snake.length, len);
  assert.equal(s.steps, 1);
});

test('queueDirection：拒绝反向、重复与非法方向，队列最多缓存 2 个', () => {
  const s = core.createGame({ cols: 20, rows: 20 });
  assert.equal(core.queueDirection(s, 'left'), false, '不能立刻反向');
  assert.equal(core.queueDirection(s, 'right'), false, '不能与当前方向重复');
  assert.equal(core.queueDirection(s, 'diagonal'), false, '非法方向被忽略');
  assert.equal(core.queueDirection(s, 'up'), true);
  assert.equal(core.queueDirection(s, 'down'), false, '队列里上一个是 up，down 属于反向');
  assert.equal(core.queueDirection(s, 'left'), true);
  assert.equal(core.queueDirection(s, 'up'), false, '队列已满（上限 2）');
  assert.equal(s.pending.length, 2);
});

test('step：每步只消费一个转向指令', () => {
  const s = core.createGame({ cols: 20, rows: 20 });
  core.queueDirection(s, 'up');
  core.queueDirection(s, 'left');
  core.step(s);
  assert.equal(s.dir, 'up');
  assertCell(s.snake[0], 10, 9);
  core.step(s);
  assert.equal(s.dir, 'left');
  assertCell(s.snake[0], 9, 9);
});

test('step：吃到食物加分、变长、重新生成食物', () => {
  const s = core.createGame({ cols: 20, rows: 20, rng: core.createRng(3) });
  const len = s.snake.length;
  s.food = { x: 11, y: 10 };
  const ev = core.step(s);
  assert.equal(ev.ate, true);
  assert.equal(s.score, core.FOOD_SCORE);
  assert.equal(s.snake.length, len + 1);
  assertCell(s.snake[0], 11, 10);
  assert.ok(s.food, '吃完必须刷新食物');
  assert.ok(!core.containsCell(s.snake, s.food.x, s.food.y), '新食物不能落在蛇身上');
});

test('等级随分数提升，速度随等级变快并有下限', () => {
  const s = core.createGame({ cols: 60, rows: 60, rng: core.createRng(5) });
  for (let i = 0; i < 25; i++) {
    s.food = { x: s.snake[0].x + 1, y: s.snake[0].y };
    core.step(s);
  }
  assert.equal(s.score, core.FOOD_SCORE * 25);
  assert.equal(s.level, 1 + Math.floor(s.score / 50));
  assert.equal(core.speedForLevel(1), core.BASE_SPEED);
  assert.ok(core.speedForLevel(2) < core.speedForLevel(1));
  assert.equal(core.speedForLevel(999), core.MIN_SPEED);
  assert.equal(core.speedForLevel(0), core.BASE_SPEED);
});

test('撞墙：立即结束，且棋盘保持不变、之后不再移动', () => {
  const s = core.createGame({ cols: 5, rows: 5, startLength: 2 });
  let ev;
  for (let i = 0; i < 10; i++) ev = core.step(s);
  assert.equal(ev.over, true);
  assert.equal(ev.reason, 'wall');
  assert.equal(s.over, true);
  assertCell(s.snake[0], 4, 2);
  const after = JSON.stringify(s.snake);
  const idle = core.step(s);
  assert.equal(idle.moved, false);
  assert.equal(JSON.stringify(s.snake), after, '结束后 step 应为空操作');
});

test('撞自己：检测身体碰撞，但允许跟随尾巴移动', () => {
  const s = core.createGame({ cols: 8, rows: 8 });
  s.snake = [{ x: 2, y: 2 }, { x: 3, y: 2 }, { x: 3, y: 3 }, { x: 2, y: 3 }];
  s.dir = 'right';
  s.food = { x: 7, y: 7 };
  const ev = core.step(s);
  assert.equal(ev.over, true);
  assert.equal(ev.reason, 'self');
  assertCell(s.snake[0], 2, 2);

  const ok = core.createGame({ cols: 8, rows: 8 });
  ok.snake = [{ x: 2, y: 2 }, { x: 3, y: 2 }, { x: 3, y: 3 }, { x: 2, y: 3 }];
  ok.dir = 'down';
  ok.food = { x: 7, y: 7 };
  const ev2 = core.step(ok);
  assert.equal(ev2.over, false, '尾巴同步让位，不算碰撞');
  assertCell(ok.snake[0], 2, 3);
  assert.equal(ok.snake.length, 4);
});

test('吃到尾巴所在格子同时算咬到自己', () => {
  const s = core.createGame({ cols: 8, rows: 8 });
  s.snake = [{ x: 2, y: 2 }, { x: 3, y: 2 }, { x: 3, y: 3 }, { x: 2, y: 3 }];
  s.dir = 'down';
  s.food = { x: 2, y: 3 };
  const ev = core.step(s);
  assert.equal(ev.over, true);
  assert.equal(ev.reason, 'self');
});

test('棋盘被填满：判定通关（won）且不再生成食物', () => {
  const s = core.createGame({ cols: 3, rows: 3, startLength: 2 });
  s.snake = [
    { x: 1, y: 0 }, { x: 0, y: 0 }, { x: 0, y: 1 }, { x: 1, y: 1 },
    { x: 0, y: 2 }, { x: 1, y: 2 }, { x: 2, y: 2 }, { x: 2, y: 1 }
  ];
  s.dir = 'right';
  s.food = { x: 2, y: 0 }; /* 全盘唯一空格 */
  const ev = core.step(s);
  assert.equal(ev.reason, 'win');
  assert.equal(s.won, true);
  assert.equal(s.over, true);
  assert.equal(s.food, null);
  assert.equal(s.snake.length, 9);
  assert.equal(core.randomFood(s), null, '满盘时 randomFood 返回 null');
});

test('createRng：确定性且输出落在 [0,1)', () => {
  const a = core.createRng(42);
  const b = core.createRng(42);
  const c = core.createRng(43);
  const seqA = [a(), a(), a()];
  const seqB = [b(), b(), b()];
  assert.deepEqual(plain(seqA), plain(seqB));
  assert.notDeepEqual(plain(seqA), plain([c(), c(), c()]));
  for (const v of seqA) assert.ok(v >= 0 && v < 1, '随机数必须在 [0,1)');
});

test('固定种子下食物序列可复现', () => {
  const run = () => {
    const s = core.createGame({ cols: 10, rows: 10, rng: core.createRng(2024) });
    const foods = [s.food];
    for (let i = 0; i < 5; i++) {
      s.food = { x: s.snake[0].x, y: s.snake[0].y + 1 };
      core.step(s);
      foods.push(s.food);
    }
    return foods.map(plain);
  };
  assert.deepEqual(run(), run());
});

test('cellKey / containsCell 基本行为', () => {
  assert.equal(core.cellKey(3, 4), '3,4');
  const cells = [{ x: 1, y: 1 }, { x: 2, y: 5 }];
  assert.equal(core.containsCell(cells, 2, 5), true);
  assert.equal(core.containsCell(cells, 5, 2), false);
  assert.equal(core.containsCell([], 0, 0), false);
});

/* ============================================================ 2. 页面静态检查 */

test('页面：自包含、无外部资源、UTF-8 声明', () => {
  assert.match(HTML, /<meta charset="UTF-8">/i);
  assert.match(HTML, /<html lang="zh-CN">/);
  assert.ok(!/(?:src|href)\s*=\s*["']https?:/i.test(HTML), '不允许引用外部网络资源');
  assert.ok(!/<link[^>]+stylesheet/i.test(HTML), '不允许外部样式表');
  assert.ok(!/\bimport\s*\(/.test(HTML), '不允许动态 import');
  assert.ok(!/\bfetch\s*\(/.test(HTML), '不允许 fetch');
});

test('页面：编码约定（无 BOM + CRLF 换行）', () => {
  const raw = readFileSync(SNAKE_HTML);
  assert.notEqual(raw[0], 0xef, 'UTF-8 文件不应带 BOM');
  const text = raw.toString('utf8');
  const lf = (text.match(/\n/g) || []).length;
  const crlf = (text.match(/\r\n/g) || []).length;
  assert.ok(lf > 0, '文件应当有内容');
  assert.equal(lf, crlf, '所有换行都必须是 CRLF');
});

test('页面：每个内联脚本都能通过语法解析', () => {
  const scripts = extractScripts();
  assert.ok(scripts.length >= 2, '至少应有 core 与 ui 两个脚本块');
  scripts.forEach((code, i) => {
    assert.doesNotThrow(() => new vm.Script(code, { filename: `inline-script-${i}.js` }));
  });
});

test('页面：UI 依赖的 DOM 节点与方向键都在', () => {
  for (const id of ['board', 'overlay', 'overlayTitle', 'overlayText', 'overlayBtn', 'score', 'best', 'level', 'dpad']) {
    assert.match(HTML, new RegExp(`id="${id}"`), `缺少 #${id}`);
  }
  for (const dir of ['up', 'down', 'left', 'right']) {
    assert.match(HTML, new RegExp(`data-dir="${dir}"`), `缺少 data-dir="${dir}" 方向键`);
  }
});

test('核心代码不触碰 DOM / window，可独立测试', () => {
  const coreCode = extractCore();
  assert.ok(!/\bdocument\b/.test(coreCode), '核心不应引用 document');
  assert.ok(!/requestAnimationFrame/.test(coreCode), '核心不应引用 rAF');
  assert.ok(!/localStorage/.test(coreCode), '核心不应引用 localStorage');
});

/* ========================================================== 3. 无头冒烟集成 */

test('集成：页面初始化后处于 ready，循环不会自行开局', () => {
  const { api, els } = bootPage();
  assert.equal(api.phase(), 'ready');
  assert.equal(els('overlay').hidden, false);
  assert.equal(els('overlayTitle').textContent, '准备好了吗？');
  advance(api, 1000);
  assert.equal(api.state().steps, 0, '等待开始时不推进');
  assert.equal(api.phase(), 'ready');
});

test('集成：按钮开局 → 方向键转向 → 吃食物加分并刷新 HUD', () => {
  const { api, els, document } = bootPage();
  els('overlayBtn').dispatch('click');
  assert.equal(api.phase(), 'playing');
  assert.equal(els('overlay').hidden, true);

  document.dispatch('keydown', { key: 'ArrowDown' });
  assert.equal(api.state().pending[0], 'down');
  advance(api, 200);
  assert.equal(api.state().dir, 'down');

  const head = api.state().snake[0];
  api.setFood(head.x, head.y + 1);
  advance(api, 200, 50, 100000);
  assert.equal(api.state().score, core.FOOD_SCORE);
  assert.equal(els('score').textContent, String(core.FOOD_SCORE));
  assert.ok(els('score').classList.contains('pulse'), '得分时 HUD 应有反馈动效');
  assert.ok(!core.containsCell(api.state().snake, api.state().food.x, api.state().food.y));
});

test('集成：空格暂停/继续，页面隐藏自动暂停', () => {
  const { api, els, document } = bootPage();
  document.dispatch('keydown', { key: ' ' });
  assert.equal(api.phase(), 'playing');

  document.dispatch('keydown', { key: ' ' });
  assert.equal(api.phase(), 'paused');
  assert.equal(els('overlay').hidden, false);
  assert.equal(els('overlayTitle').textContent, '已暂停');
  const steps = api.state().steps;
  advance(api, 500, 50, 200000);
  assert.equal(api.state().steps, steps, '暂停时不应推进');

  document.dispatch('keydown', { key: 'p' });
  assert.equal(api.phase(), 'playing');

  document.hidden = true;
  document.dispatch('visibilitychange');
  assert.equal(api.phase(), 'paused', '切到后台应自动暂停');
});

test('集成：撞墙结束、记录最高分、R 键重开', () => {
  const { api, els, document, localStorage } = bootPage();
  document.dispatch('keydown', { key: 'Enter' });
  assert.equal(api.phase(), 'playing');

  const head = api.state().snake[0];
  api.setFood(head.x + 1, head.y);
  advance(api, 200, 50, 300000);
  const score = api.state().score;
  assert.ok(score > 0, '应当吃到那颗食物');

  advance(api, 4000, 50, 400000); /* 一路向右撞墙 */
  assert.equal(api.phase(), 'over');
  assert.equal(api.state().reason, 'wall');
  assert.match(els('overlayTitle').textContent, /撞墙/);
  assert.equal(els('overlay').getAttribute('data-phase'), 'over');
  assert.equal(api.best(), score);
  assert.equal(localStorage.getItem('snake.best.v1'), String(score), '最高分应写入本地存储');

  document.dispatch('keydown', { key: 'r' });
  assert.equal(api.phase(), 'playing');
  assert.equal(api.state().score, 0);
  assert.equal(api.state().steps, 0);
  assert.equal(els('score').textContent, '0');
});

test('集成：方向键状态机（先上后左，逐帧生效）', () => {
  const { api, document } = bootPage();
  document.dispatch('keydown', { key: 'ArrowUp' }); /* ready 状态下按方向键即开局 */
  assert.equal(api.phase(), 'playing');
  advance(api, 200, 50, 500000);
  assert.equal(api.state().dir, 'up');

  document.dispatch('keydown', { key: 'a' });
  advance(api, 200, 50, 600000);
  assert.equal(api.state().dir, 'left');

  document.dispatch('keydown', { key: 'd' }); /* 立刻反向应被忽略 */
  assert.equal(api.state().pending.length, 0);
});

test('集成：虚拟方向键与滑动手势可用', () => {
  const { api, els } = bootPage();
  els('overlayBtn').dispatch('click');

  const button = { getAttribute: (k) => (k === 'data-dir' ? 'down' : null) };
  els('dpad').dispatch('click', { target: button });
  assert.equal(api.state().pending[0], 'down');
  advance(api, 200, 50, 700000);
  assert.equal(api.state().dir, 'down');

  const canvas = els('board');
  canvas.dispatch('touchstart', { touches: [{ clientX: 300, clientY: 300 }] });
  canvas.dispatch('touchend', { changedTouches: [{ clientX: 90, clientY: 300 }] });
  assert.equal(api.state().pending[0], 'left', '向左滑动应转为 left');
  advance(api, 200, 50, 710000);
  assert.equal(api.state().dir, 'left');

  canvas.dispatch('touchstart', { touches: [{ clientX: 300, clientY: 300 }] });
  canvas.dispatch('touchend', { changedTouches: [{ clientX: 300, clientY: 120 }] });
  assert.equal(api.state().pending[0], 'up', '向上滑动应转为 up');
  advance(api, 200, 50, 720000);
  assert.equal(api.state().dir, 'up');

  /* 微小位移不视为滑动 */
  const pendingBefore = api.state().pending.length;
  canvas.dispatch('touchstart', { touches: [{ clientX: 300, clientY: 300 }] });
  canvas.dispatch('touchend', { changedTouches: [{ clientX: 306, clientY: 303 }] });
  assert.equal(api.state().pending.length, pendingBefore, '微小位移不应触发转向');
});

test('集成：真实 rAF 循环路径可运行且会重绘', () => {
  const { sandbox, rafQueue } = bootPage();
  assert.ok(rafQueue.length >= 1, 'UI 应当自行注册动画帧');
  const cb = rafQueue.shift();
  const before = rafQueue.length;
  cb(16);
  assert.ok(rafQueue.length > before, '循环应当继续注册下一帧');
  const ctx = sandbox.document.getElementById('board').getContext('2d');
  assert.ok(ctx.calls.length > 0, '应当发生绘制调用');
  assert.ok(ctx.calls.includes('fillRect'));
});

test('集成：__SNAKE__.setFood / setDirection 调试句柄行为正确', () => {
  const { api } = bootPage();
  api.start();
  api.setDirection('up');
  api.setFood(3, 4);
  assertCell(api.state().food, 3, 4);
  assert.equal(api.state().pending[0], 'up');
  api.pause();
  assert.equal(api.phase(), 'paused');
  api.restart();
  assert.equal(api.phase(), 'playing');
  assert.equal(api.state().steps, 0);
});
