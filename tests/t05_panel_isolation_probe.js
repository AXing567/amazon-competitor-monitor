/**
 * T05 面板 · 单 tab 故障隔离 · 最小 DOM 探针（Node，零依赖）
 *
 * 目的：证明「某个 view 加载失败时，错误只落在它自己的 `.view-body` 里，
 * 不影响骨架 / 其它 view」，而不是靠读代码猜。
 *
 * 做法：
 *   1. 读 `curl http://127.0.0.1:8100/panel` 抓下来的真实 HTML（.qa_tmp/panel.html）；
 *   2. 用真实的 `<section class="view" id="view-*">` 结构造一棵最小 DOM 树
 *      （section > div.view-body > 该 section 里所有 id 的元素）；
 *   3. 把页面里唯一那段内联 `<script>` 原样丢进 `vm` 上下文执行
 *      —— 跑的是**线上那份代码**，不是复制品；
 *   4. 用假 `fetch` 控制哪个接口失败，然后调 `show()` / `window.__retry()`，
 *      检查 DOM 落在了哪里。
 *
 * 跑法（需要 node，两个输入文件都由下面两条命令生成）：
 *   curl -s http://127.0.0.1:8100/panel -o .qa_tmp/panel.html
 *   #   没有起服务时也可以离线渲染，结果与 HTTP 返回完全一致（render_panel() 是纯函数）：
 *   #   python -c "from app.web import render_panel;
 *   #               open('.qa_tmp/panel.html','w',encoding='utf-8').write(render_panel())"
 *   # payloads.json = 看板/健康/池子等端点的真实返回，供 [F][H] 段使用：
 *   #   GET /api/panel/{board,health,context,pool,actions} · /api/config/thresholds{,/schema}
 *   <node> tests/t05_panel_isolation_probe.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HTML = process.env.PANEL_HTML
  || path.join(__dirname, '..', '.qa_tmp', 'panel.html');

// ============================================================
// 最小 DOM
// ============================================================

class El {
  constructor(tag, attrs) {
    attrs = attrs || {};
    this.tagName = String(tag || 'div').toUpperCase();
    this.id = attrs.id || '';
    this._class = attrs.class || '';
    this.children = [];
    this.parent = null;
    this._text = '';
    this.innerHTML = '';
    this.dataset = attrs.dataset || {};
    this.style = {};
    this.title = '';
    this.disabled = false;
    this.value = '';
    this.checked = false;
    this.type = attrs.type || '';
    this.scrollIntoView = () => {};
    this.onclick = null;
    this.oninput = null;
    this.onchange = null;
    const self = this;
    this.classList = {
      contains: (c) => self._classList().indexOf(c) >= 0,
      add: (c) => { if (!self.classList.contains(c)) self._class = (self._class + ' ' + c).trim(); },
      remove: (c) => { self._class = self._classList().filter((x) => x !== c).join(' '); },
      toggle: (c, force) => {
        const on = force === undefined ? !self.classList.contains(c) : !!force;
        if (on) self.classList.add(c); else self.classList.remove(c);
        return on;
      },
    };
  }
  _classList() { return String(this._class || '').split(/\s+/).filter(Boolean); }
  get className() { return this._class; }
  set className(v) { this._class = String(v || ''); }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join('');
    return this._text;
  }
  set textContent(v) { this._text = String(v === null || v === undefined ? '' : v); this.children = []; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  appendChild(el) { el.parent = this; this.children.push(el); return el; }
  insertBefore(el, ref) {
    el.parent = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(el); else this.children.splice(i, 0, el);
    return el;
  }
  remove() {
    if (!this.parent) return;
    const i = this.parent.children.indexOf(this);
    if (i >= 0) this.parent.children.splice(i, 1);
    this.parent = null;
  }
  querySelector(sel) { return query(this, sel)[0] || null; }
  querySelectorAll(sel) { return query(this, sel); }
}

function walk(root, fn) {
  for (const c of root.children) { fn(c); walk(c, fn); }
}

function matchesSimple(el, sel) {
  if (sel.startsWith('#')) return el.id === sel.slice(1);
  if (sel.startsWith('.')) return el.classList.contains(sel.slice(1));
  return el.tagName === sel.toUpperCase();
}

/** 支持 `#id` / `.cls` / `tag` / `A B`（后代） / `:scope > .cls`。够本探针用。 */
function query(root, sel) {
  sel = String(sel || '').trim();
  if (!sel) return [];
  if (sel.startsWith(':scope > ')) {
    const part = sel.slice(':scope > '.length).trim();
    return root.children.filter((c) => matchesSimple(c, part));
  }
  const parts = sel.split(/\s+/).filter(Boolean);
  let pool = root.children.slice();
  let hits = [];
  for (let i = 0; i < parts.length; i += 1) {
    const last = i === parts.length - 1;
    hits = [];
    const desc = [];
    for (const node of pool) { desc.push(node); walk(node, (n) => desc.push(n)); }
    for (const node of desc) {
      if (matchesSimple(node, parts[i])) {
        if (last) hits.push(node); else hits.push(node);
      }
    }
    if (!last) {
      const next = [];
      for (const node of hits) walk(node, (n) => next.push(n));
      pool = next;
    }
  }
  return hits;
}

function buildDom(html) {
  const doc = new El('#document');
  const body = new El('body');
  doc.appendChild(body);

  // 骨架：header.top（含 nav.tabs）+ main
  const header = new El('header', { class: 'top' });
  body.appendChild(header);
  const tabs = new El('nav', { class: 'tabs' });
  header.appendChild(tabs);
  const main = new El('main');
  body.appendChild(main);

  const order = [...html.matchAll(/<section class="view" id="(view-[a-z]+)"/g)].map((m) => m[1]);
  const starts = order.map((id) => html.indexOf(`id="${id}"`));
  order.forEach((id, idx) => {
    const from = starts[idx];
    const to = idx + 1 < starts.length ? starts[idx + 1] : html.indexOf('</main>');
    const slice = html.slice(from, to);
    const sec = new El('section', { id, class: idx === 0 ? 'view on' : 'view' });
    main.appendChild(sec);
    const vb = new El('div', { class: 'view-body' });
    sec.appendChild(vb);
    // 该 section 里所有 id（除了 section 自己的）都挂进 view-body：
    // 真实的层级更深，但隔离要验的是「有没有被删掉 / 有没有被别的 view 改到」。
    for (const m of slice.matchAll(/id="([^"]+)"/g)) {
      if (m[1] === id) continue;
      vb.appendChild(new El('div', { id: m[1] }));
    }
    tabs.appendChild(new El('a', { dataset: { view: id.replace('view-', '') } }));
  });

  doc.body = body;
  doc.getElementById = (id) => {
    let found = null;
    walk(doc, (n) => { if (!found && n.id === id) found = n; });
    return found;
  };
  doc.querySelector = (sel) => query(doc, sel)[0] || null;
  doc.querySelectorAll = (sel) => query(doc, sel);
  doc.createElement = (tag) => new El(tag);
  doc.addEventListener = () => {};
  return { doc, order };
}

// ============================================================
// 断言
// ============================================================

let pass = 0;
const fails = [];
function check(name, cond, detail) {
  if (cond) { pass += 1; console.log('  ✅ ' + name); }
  else { fails.push(name + (detail ? ' — ' + detail : '')); console.log('  ❌ ' + name + (detail ? ' — ' + detail : '')); }
}
const tick = () => new Promise((r) => setTimeout(r, 0));

// ============================================================
// 主流程
// ============================================================

async function main() {
  const html = fs.readFileSync(HTML, 'utf8');
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (scripts.length !== 1) throw new Error('期望恰好 1 段内联 script，实际 ' + scripts.length);
  const code = scripts[0][1];

  const { doc, order } = buildDom(html);
  console.log(`\n[DOM] 从真实 HTML 构造：${order.length} 个 view（${order.join(', ')}）`);

  // ---- 假 fetch：按路径返回预设 ----
  const calls = {};
  let mode = {};   // path -> {ok:false,...} | {ok:true,...}
  const fetchStub = async (p) => {
    calls[p] = (calls[p] || 0) + 1;
    const key = Object.keys(mode).sort((a, b) => b.length - a.length)
      .find((k) => p === k || p.startsWith(k));
    const payload = key ? mode[key] : { ok: true };
    return { status: 200, json: async () => payload };
  };

  const win = { addEventListener: () => {} };
  const ctx = {
    window: win,
    document: doc,
    localStorage: { getItem: () => '', setItem: () => {} },
    fetch: fetchStub,
    console,
    setTimeout,
    clearTimeout,
    URLSearchParams,
  };
  vm.createContext(ctx);
  vm.runInContext(code, ctx, { filename: 'panel-inline.js' });

  check('内联 script 可在最小 DOM 上无异常执行完（说明外壳 JS 不依赖任何外链/框架）', true);
  check('window.__retry 已挂上（错误卡片上的「重试」按钮有落点）', typeof win.__retry === 'function');
  check('safeLoad 是全局函数（唯一加载入口）', typeof ctx.safeLoad === 'function');
  check('safeLoad 对不存在的 view 名静默返回（不会抛）', (await ctx.safeLoad('nope')) === undefined);

  const boardBody = doc.getElementById('view-board').querySelector('.view-body');
  const healthBody = doc.getElementById('view-health').querySelector('.view-body');
  const poolBody = doc.getElementById('view-pool').querySelector('.view-body');
  const boardKidCount = boardBody.children.length;
  const healKidCount = healthBody.children.length;
  const poolKidCount = poolBody.children.length;
  const headerHTML = doc.querySelector('header').innerHTML;
  const mainKidCount = doc.querySelector('main').children.length;

  // ---------- A. board 接口失败 ----------
  console.log('\n[A] 让 /api/panel/board 返回 {ok:false}，加载 board');
  mode = {
    '/api/panel/board': { ok: false, error: '数据库访问失败：表结构或数据库文件有问题', hint: '确认 data/*.db 可读' },
    '/api/panel/health': { ok: true, rounds: { total: 0, ok: 0, failed: 0, success_rate: null }, price_trust: [], failures: [], snapshot_freshness: [] },
  };
  await ctx.show('board');
  await tick();

  const errInBoard = boardBody.querySelectorAll('.err');
  check('board 的 .view-body 里出现 1 张错误卡', errInBoard.length === 1, '实际 ' + errInBoard.length);
  check('错误卡是 view-body 的第一个子节点（插在最前面，不覆盖原骨架）',
    boardBody.children[0] === errInBoard[0]);
  check('board 原有 ' + boardKidCount + ' 个子节点一个没少（#b-total 等容器仍可取到）',
    boardBody.children.length === boardKidCount + 1 && !!doc.getElementById('b-total'));
  check('错误卡用了接口给的 hint 显示「建议」', /建议/.test(errInBoard[0].innerHTML) && /data\/\*\.db/.test(errInBoard[0].innerHTML));
  check('错误卡写明「只有这一屏受影响」', /只有这一屏受影响/.test(errInBoard[0].innerHTML));

  check('health 的 .view-body 未被动过（子节点数不变、无 .err）',
    healthBody.children.length === healKidCount && healthBody.querySelectorAll('.err').length === 0);
  check('pool 的 .view-body 未被动过',
    poolBody.children.length === poolKidCount && poolBody.querySelectorAll('.err').length === 0);
  check('顶部骨架 / 标签栏未被重写', doc.querySelector('header').innerHTML === headerHTML);
  check('main 下仍是 5 个 section（没有被整体替换成错误页）',
    doc.querySelector('main').children.length === mainKidCount && mainKidCount === 5);

  // ---------- B. 另一个 tab 照常可用 ----------
  console.log('\n[B] board 坏着的同时切到 health');
  mode['/api/panel/context'] = { ok: true, my_product_id: null, my_asin: null };
  mode['/api/config/thresholds'] = { ok: true, effective: {}, sources: {}, layers: [] };
  mode['/api/panel/health'] = {
    ok: true,
    window_days: 7,
    rounds: { total: 3, ok: 2, failed: 1, partial: 0, success_rate: 0.667 },
    price_trust: [], failures: [{ at: '2026-11-04T10:00:00', asin: 'B0X', trigger: 'schedule', error: '429', duration_ms: 30 }],
    snapshot_freshness: [],
  };
  await ctx.show('health');
  await tick();
  check('health 正常渲染（#h-total 拿到 3）', doc.getElementById('h-total').textContent === '3',
    '实际 ' + JSON.stringify(doc.getElementById('h-total').textContent));
  check('health 渲染成功且没有出现错误卡', healthBody.querySelectorAll('.err').length === 0);
  check('board 的错误卡仍在（切走再切回不会把它弄丢，也不会传染给 health）',
    boardBody.querySelectorAll('.err').length === 1);
  check('采集成功率按 2/3 显示为 66.7%', doc.getElementById('h-rate').textContent === '66.7%',
    '实际 ' + JSON.stringify(doc.getElementById('h-rate').textContent));

  // ---------- C. 重新进入失败的 tab：会不会自愈 ----------
  console.log('\n[C] 再切回 board（loaded 标记为 true，按代码不会重试）');
  const boardCallsBefore = calls['/api/panel/board'] || 0;
  await ctx.show('board');
  await tick();
  const boardCallsAfter = calls['/api/panel/board'] || 0;
  console.log(`     board 接口调用次数：切回前 ${boardCallsBefore} → 切回后 ${boardCallsAfter}`);
  check('切回失败的 tab 不会自动重发请求（结论：不自愈，需点「重试」）',
    boardCallsAfter === boardCallsBefore);
  check('切回后错误卡还在（不是白屏，也不是"看起来好了其实没数据"）',
    boardBody.querySelectorAll('.err').length === 1);

  // ---------- D. 点「重试」能自愈 ----------
  console.log('\n[D] 接口恢复后点「重试」（window.__retry）');
  mode['/api/panel/board'] = {
    ok: true,
    summary: { total: 7, by_tier: { S: 2, A: 2, B: 1, C: 1, unknown: 1 }, undecided: 1, week_changes: 11, week_by_level: { P0: 6, P1: 3, P2: 2 }, p0_24h: 1, p1_24h: 0 },
    items: [],
  };
  win.__retry('board');
  await tick(); await tick();
  check('重试后错误卡被清掉（成功之后不会还挂着一张红牌）', boardBody.querySelectorAll('.err').length === 0);
  check('重试后数据真的填进去了（#b-total = 7）', doc.getElementById('b-total').textContent === '7',
    '实际 ' + JSON.stringify(doc.getElementById('b-total').textContent));
  check('重试后 #b-total-n 按 by_tier 渲染出 5 段（含「待判定 1」）',
    /S 2 · A 2 · B 1 · C 1 · 待判定 1/.test(doc.getElementById('b-total-n').textContent),
    '实际 ' + JSON.stringify(doc.getElementById('b-total-n').textContent));
  check('重试过程中没有抛 "Cannot set properties of null"（容器都还在）', true);
  check('其它 view 依旧未被波及', healthBody.querySelectorAll('.err').length === 0);

  // ---------- E. 非 ApiError 也被收住 ----------
  console.log('\n[E] 让 loader 抛一个普通 TypeError');
  ctx.register('probe', async () => { throw new TypeError('Cannot set properties of null'); });
  // probe 没有对应 section，root 为 null → errCard 应静默 return，不抛
  let threw = false;
  try { await ctx.safeLoad('probe'); } catch (e) { threw = true; }
  check('没有 #view-probe 时 errCard 静默返回，异常不冒泡给调用方', threw === false);

  // ---------- F. 用真实接口数据渲染看板，扫描非法字面量 ----------
  console.log('\n[F] 用真实 /api/panel/board 的返回渲染表格，扫描 undefined / null / NaN');
  const payloadsPath = path.join(__dirname, '..', '.qa_tmp', 'payloads.json');
  if (!fs.existsSync(payloadsPath)) {
    console.log('  ⚠️ 跳过：缺 .qa_tmp/payloads.json（生成方式见文件头注释）');
  } else {
    const P = JSON.parse(fs.readFileSync(payloadsPath, 'utf8'));
    mode = {
      '/api/panel/board': P.board,
      '/api/panel/health': P.health,
    };
    win.__retry('board');
    await tick(); await tick();
    const table = doc.getElementById('b-table').innerHTML;
    const bad = [];
    for (const token of ['undefined', 'NaN', 'null', '[object Object]']) {
      if (table.includes(token)) bad.push(token);
    }
    check('看板表格里没有 undefined / NaN / null / [object Object]',
      bad.length === 0, '命中：' + bad.join(', '));
    check('7 个竞品全部渲染出行（data-asin 出现 7 次）',
      (table.match(/data-asin="/g) || []).length === 7,
      '实际 ' + (table.match(/data-asin="/g) || []).length);
    check('unknown 竞品显示成「待判定（证据不足）」而不是 C 或空白',
      table.includes('待判定（证据不足）') && !/tag unknown">C/.test(table));
    check('unknown 竞品把缺什么证据摊开写（缺流量词 / 缺类目路径）',
      table.includes('缺流量词') && table.includes('缺类目路径'));
    check('总数卡 = 7，且 by_tier 五段都渲染出来',
      doc.getElementById('b-total').textContent === '7'
      && /S 2 · A 2 · B 1 · C 1 · 待判定 1/.test(doc.getElementById('b-total-n').textContent));
    const wl = doc.getElementById('b-week-n').textContent;
    check('近 7 天变化卡：11 与 P0 6 / P1 3 / P2 2 同时出现在界面上（总数=分项之和）',
      doc.getElementById('b-week').textContent === '11' && /P0 6 · P1 3 · P2 2/.test(wl), '实际 ' + wl);
    const todoN = doc.getElementById('b-todo-n').textContent;
    check('待处理卡把「待判定竞品 1 个」写出来', /待判定竞品 1 个/.test(todoN), '实际 ' + todoN);

    // 「战场」列：同一份 battlefield，看板与竞品池的判据必须一致
    const brokenCells = (table.match(/<td>B0MINE0001/g) || []).length;
    console.log(`     看板「战场（自家产品）」列里出现自家 ASIN 的单元格：${brokenCells} 个（其中只有 2 个是真正已绑定的）`);
    check('看板「战场」列对未绑定竞品显示「未绑定」（与竞品池一致）',
      brokenCells <= 2, `实际 ${brokenCells} 个单元格直接写了 B0MINE0001，其中 5 个 binding_id 为 null`);
  }

  // ---------- G. 交互动作不在 safeLoad 隔离范围内 ----------
  console.log('\n[G] A/B：loader 失败 vs 写操作失败');
  mode = { '/api/config/thresholds': { ok: false, error: '数据库访问失败：表结构或数据库文件有问题', hint: '确认 data/*.db 可读' } };
  await ctx.show('config');
  await tick();
  const cfgBody = doc.getElementById('view-config').querySelector('.view-body');
  check('A) 走 safeLoad 的 loader 失败 → 出错误卡', cfgBody.querySelectorAll('.err').length === 1);

  mode = { '/api/panel/pool/B0DEMO0005/status': { ok: false, error: '写入被数据库拒绝：违反了一条数据约束', hint: '稍后重试' } };
  let rejected = false;
  try { await ctx.poolSetStatus('B0DEMO0005', 'paused'); } catch (e) { rejected = true; }
  const poolErrs = poolBody.querySelectorAll('.err').length;
  check('B) 池子的「停/归档」按钮失败 → 异常直接抛给 onclick（页面无任何错误提示）',
    rejected === true && poolErrs === 0, `rejected=${rejected} 错误卡=${poolErrs}`);
  console.log('     结论：改状态 / 试算 / 保存 / 版本 / 回滚 这些 onclick 里没有 try/catch，');
  console.log('           失败时用户看到的是「点了没反应」，不是错误卡。');

  // ---------- H. 看板里「健康度」那张卡单独容错 ----------
  console.log('\n[H] 看板加载时健康接口失败，其它三张卡照常');
  if (fs.existsSync(payloadsPath)) {
    const P = JSON.parse(fs.readFileSync(payloadsPath, 'utf8'));
    mode = {
      '/api/panel/board': P.board,
      '/api/panel/health': { ok: false, error: '数据库访问失败：表结构或数据库文件有问题', hint: '确认 data/*.db 可读' },
    };
    win.__retry('board');
    await tick(); await tick();
    check('看板没有因为健康接口失败而出错误卡（整屏仍在）',
      boardBody.querySelectorAll('.err').length === 0);
    check('竞品总数照样是 7（其它卡不受影响）', doc.getElementById('b-total').textContent === '7');
    check('健康度卡显示「—」而不是 0%（不知道就说不知道）',
      doc.getElementById('b-health').textContent === '—',
      '实际 ' + JSON.stringify(doc.getElementById('b-health').textContent));
    check('健康度卡写清原因（前端把 error 文案带上）',
      /健康接口没返回/.test(doc.getElementById('b-health-n').textContent),
      '实际 ' + doc.getElementById('b-health-n').textContent);
  }

  // ---------- 汇总 ----------
  console.log('\n' + '─'.repeat(56));
  if (fails.length) {
    console.log(`单 tab 隔离探针：通过 ${pass}/${pass + fails.length}，失败 ${fails.length}`);
    fails.forEach((f) => console.log('  ! ' + f));
    process.exitCode = 1;
  } else {
    console.log(`单 tab 隔离探针：全部通过 ${pass}/${pass}`);
  }
}

main().catch((e) => { console.error('探针自身出错：', e); process.exitCode = 2; });
