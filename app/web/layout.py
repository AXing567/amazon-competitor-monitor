"""L5B · 面板外壳：CSS、HTML 骨架、核心 JS。

## 为什么是「服务端拼成单文件」

面板要能在**断网、数据源熔断、客户没装任何东西**的情况下打开。
所以：没有构建链、没有 CDN、没有 `<script src>`、没有字体外链。
样式和脚本全部内联，服务端一次拼好返回一个 HTML——打开页面 = 一次 HTTP 请求，
不依赖任何第二个资源能加载成功。

## 分层

    layout.py        外壳：CSS、静态骨架、核心 JS（工具函数 + 路由 + 故障隔离）
    views/*.py       五个视图：各自的 section HTML + loadXxx() 加载函数
    panel.py         拼装 + lru_cache

## 面板的职责边界（红线）

面板**只做两件事**：把已有结论摆出来、把你的配置收进去。
任何"根据数据算出结论"的代码都属于越界——那属于 L2/L3。
所以前端不做加权、不做阈值比较、不判断"这个算不算异常"，
所有数字都来自 `/api/panel/*` 或 `/api/cap/*` 返回的字段。

## 故障隔离为什么写在这层

`safeLoad()` 是唯一的加载入口，它把每个视图的异常**收进该视图自己的 DOM 容器**。
放在这里而不是让每个视图自己 try/catch，是因为"忘记写 try"是会发生的，
而忘记了只有一处错误——用户看到的是整页白屏（数据源挂掉时最需要的那页也打不开）。
"""
from __future__ import annotations

# ============================================================
# CSS
# ============================================================

CSS = """
:root{
  --bg:#f6f7f9; --card:#ffffff; --line:#e5e7eb; --text:#111827; --muted:#6b7280;
  --p0:#dc2626; --p1:#ea580c; --p2:#6b7280; --ok:#059669; --accent:#2563eb;
  --p0bg:#fef2f2; --p1bg:#fff7ed; --warnbg:#fffbeb;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0f1115; --card:#181b21; --line:#2a2f38; --text:#e8eaed; --muted:#9aa1ac;
    --p0:#f87171; --p1:#fb923c; --p2:#9ca3af; --ok:#34d399; --accent:#60a5fa;
    --p0bg:#241416; --p1bg:#241a14; --warnbg:#241f14;
  }
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
code{background:var(--line);padding:1px 5px;border-radius:4px;font-size:12px}

.top{position:sticky;top:0;z-index:20;background:var(--card);border-bottom:1px solid var(--line);
  display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:10px 20px}
.brand{display:flex;align-items:baseline;gap:10px;min-width:180px}
.brand b{font-size:15px;letter-spacing:-.2px}
.brand .sub{color:var(--muted);font-size:12px}
.tabs{display:flex;gap:4px;flex:1;flex-wrap:wrap}
.tabs a{padding:6px 12px;border-radius:8px;color:var(--muted);font-size:13px;font-weight:500}
.tabs a:hover{background:var(--bg);color:var(--text)}
.tabs a.on{background:var(--accent);color:#fff}
.who{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted)}
.who select{font:inherit;font-size:12.5px;padding:4px 8px;border-radius:7px;
  border:1px solid var(--line);background:var(--card);color:var(--text)}

main{max-width:1180px;margin:0 auto;padding:20px 20px 80px}
.view{display:none}
.view.on{display:block}
h2.sec{font-size:13px;font-weight:650;color:var(--muted);letter-spacing:.3px;
  margin:22px 0 10px;text-transform:uppercase}
h2.sec:first-child{margin-top:4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
@media(max-width:860px){.grid4{grid-template-columns:repeat(2,1fr)}}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.stat .k{font-size:12px;color:var(--muted);margin-bottom:6px}
.stat .v{font-size:24px;font-weight:700;letter-spacing:-.5px}
.stat .n{font-size:11.5px;color:var(--muted);margin-top:4px}

table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;white-space:nowrap}
tbody tr.row:hover{background:var(--bg)}
/* 已暂停 / 已归档的竞品是「灰显但不隐藏」：
   藏起来会让人以为它被删了，下次想恢复时找不到入口。 */
tbody tr.row.paused td{opacity:.55}
tbody tr.row.archived td{opacity:.38}
td.dim,.dim{color:var(--muted)}
.num{font-variant-numeric:tabular-nums}

.tag{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11.5px;font-weight:700}
.tag.S{background:#fee2e2;color:#991b1b}
.tag.A{background:#ffedd5;color:#9a3412}
.tag.B{background:#dbeafe;color:#1e40af}
.tag.C{background:#f3f4f6;color:#4b5563}
.tag.unknown{background:#fef3c7;color:#92400e}
.tag.active{background:#dcfce7;color:#166534}
.tag.paused{background:#f3f4f6;color:#4b5563}
.tag.archived{background:#e5e7eb;color:#6b7280}
@media (prefers-color-scheme: dark){
  .tag.S{background:#3f1d20;color:#fca5a5}
  .tag.A{background:#3d2716;color:#fdba74}
  .tag.B{background:#17293f;color:#93c5fd}
  .tag.C{background:#252a32;color:#9ca3af}
  .tag.unknown{background:#3a2f10;color:#fcd34d}
  .tag.active{background:#12321f;color:#6ee7b7}
  .tag.paused{background:#252a32;color:#9ca3af}
  .tag.archived{background:#20242b;color:#6b7280}
}

.lvl{font-size:11px;font-weight:800;padding:1px 6px;border-radius:4px;color:#fff}
.lvl.P0{background:var(--p0)} .lvl.P1{background:var(--p1)} .lvl.P2{background:var(--p2)}

.empty{color:var(--muted);font-size:13px;padding:14px 2px;line-height:1.8}
.err{background:var(--p0bg);border:1px solid var(--p0);border-radius:12px;padding:14px 16px;margin:8px 0}
.err .t{font-weight:650;font-size:13.5px;margin-bottom:6px}
.err .m{font-size:13px;line-height:1.75;white-space:pre-wrap}
.err .h{font-size:12.5px;color:var(--muted);margin-top:8px;line-height:1.75}
.toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:99;
  background:#111827;color:#fff;padding:9px 16px;border-radius:9px;font-size:13px;
  box-shadow:0 6px 24px rgba(0,0,0,.25);opacity:0;transition:opacity .18s;pointer-events:none}
.toast.on{opacity:1}
.toast.bad{background:var(--p0)}

.btn{font:inherit;font-size:12.5px;padding:5px 12px;border-radius:8px;cursor:pointer;
  border:1px solid var(--line);background:var(--card);color:var(--text)}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{opacity:.9;color:#fff}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.sm{padding:2px 9px;font-size:12px}
.btn.danger:hover{border-color:var(--p0);color:var(--p0)}

.field{display:grid;grid-template-columns:1fr 150px;gap:10px;align-items:start;
  padding:11px 0;border-bottom:1px dashed var(--line)}
.field:last-child{border-bottom:0}
.field .lab{font-size:13px}
.field .k{font-weight:600;margin-bottom:3px}
.field .h{font-size:11.5px;color:var(--muted);line-height:1.65}
.field .h b{color:var(--text);font-weight:600}
.field input,.field select{font:inherit;font-size:13px;width:100%;padding:6px 9px;border-radius:8px;
  border:1px solid var(--line);background:var(--card);color:var(--text)}
/* 文本类字段（词表、类目路径、标题、五点）用宽列：150px 的右列会把
   textarea 压成一条缝，一行只放得下三四个字，改起来像在猜。 */
.field.wide{grid-template-columns:1fr 340px}
.field textarea{font:inherit;font-size:12.5px;width:100%;padding:7px 9px;border-radius:8px;
  border:1px solid var(--line);background:var(--card);color:var(--text);
  line-height:1.6;resize:vertical}
@media(max-width:760px){.field.wide{grid-template-columns:1fr}}
.field input.changed{border-color:var(--accent);box-shadow:0 0 0 3px rgba(37,99,235,.12)}
.src{font-size:11px;color:var(--muted);margin-top:3px}
.layerbar{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 14px}
.layerbar .b{padding:5px 12px;border-radius:8px;border:1px solid var(--line);
  font-size:12.5px;cursor:pointer;color:var(--muted);background:var(--card)}
.layerbar .b.on{background:var(--accent);border-color:var(--accent);color:#fff}

.simrow{display:grid;grid-template-columns:96px repeat(3,1fr);gap:8px;align-items:center;
  padding:7px 0;border-bottom:1px dashed var(--line);font-size:13px}
.simrow:last-child{border-bottom:0}
.simrow .head{color:var(--muted);font-size:12px}
.big{font-size:19px;font-weight:700;font-variant-numeric:tabular-nums}
.up{color:var(--p0)} .down{color:var(--ok)}

.bars{display:flex;align-items:flex-end;gap:6px;height:96px;margin:10px 0 4px}
.bars .b{flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%}
.bars .b i{display:block;background:var(--accent);border-radius:3px 3px 0 0;min-height:2px}
.bars .b.bad i{background:var(--p0)}
.bars .x{display:flex;gap:6px;font-size:10.5px;color:var(--muted)}
.bars .x span{flex:1;text-align:center;overflow:hidden;white-space:nowrap}

details.exp>summary{cursor:pointer;color:var(--accent);font-size:12.5px;list-style:none}
details.exp>summary::-webkit-details-marker{display:none}
details.exp[open]>summary{margin-bottom:8px}
.sub{border-left:2px solid var(--line);padding:6px 0 6px 14px;margin:6px 0}

.warnbox{background:var(--warnbg);border:1px solid var(--p1);border-radius:10px;
  padding:10px 13px;font-size:12.5px;line-height:1.8;margin:8px 0}
.warnbox b{color:var(--p1)}
.note{font-size:12px;color:var(--muted);line-height:1.8;margin:6px 0}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:12.5px}
.kv .k{color:var(--muted)}

/* ── 数据模式切换（自家基线页） ── */
.modebox{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:760px){.modebox{grid-template-columns:1fr}}
.mode{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:13px 15px;cursor:pointer}
.mode:hover{border-color:var(--accent)}
.mode.on{border-color:var(--accent);box-shadow:0 0 0 3px rgba(37,99,235,.10)}
.mode .t{display:flex;align-items:center;gap:8px;font-weight:650;font-size:13.5px}
.mode .d{font-size:12px;color:var(--muted);line-height:1.7;margin-top:5px}
.mode .act{margin-top:9px;display:flex;gap:6px;flex-wrap:wrap}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;background:var(--line);flex:none}
.dot.ok{background:var(--ok)} .dot.bad{background:var(--p0)}

/* ── 锚点五维卡 ── */
.dimcard{background:var(--card);border:1px solid var(--line);border-radius:12px;
  margin-bottom:12px;overflow:hidden}
.dimcard .hd{display:flex;align-items:center;gap:10px;padding:12px 16px 9px}
.dimcard .hd .n{font-weight:650;font-size:13.5px}
.dimcard .hd .c{margin-left:auto;font-size:12px;color:var(--muted);
  font-variant-numeric:tabular-nums}
.dimcard .why{font-size:11.5px;color:var(--muted);padding:0 16px 9px;line-height:1.7}
.bar{height:3px;background:var(--line);border-radius:2px;overflow:hidden;margin:0 16px 11px}
.bar i{display:block;height:100%;background:var(--ok)}
.bar.part i{background:var(--p1)}
.bar.none i{background:var(--p0)}
.frow{display:grid;grid-template-columns:150px 1fr 76px;gap:10px;align-items:baseline;
  padding:8px 16px;border-top:1px dashed var(--line);font-size:12.5px}
.frow .l{color:var(--muted)}
.frow .v{min-width:0;overflow-wrap:anywhere}
.caps{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.cap{font-size:11.5px;padding:3px 9px;border-radius:6px;border:1px solid var(--line);
  color:var(--muted)}
.cap.yes{border-color:var(--ok);color:var(--ok)}
.assume-l{color:var(--p1);font-weight:600}

/* ── 复盘卡（动作记录页顶部） ── */
.rv{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:13px 15px;margin-bottom:10px}
/* 超期的左边加一道竖线。只用左边框、不用整框变色：
   整框变红会让「超期 2 天」和「超期 30 天」看起来一样严重。 */
.rv.over{border-left:3px solid var(--p1)}
.rv .h{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:7px}
.rv .ttl{font-weight:650;font-size:13.5px}
.rv .why{font-size:12.5px;color:var(--muted);line-height:1.75;margin:2px 0 8px}
/* 前后对比一行：用 flex 而不是 grid。
   grid 的 `1fr` 会把「22.99」和「21.99」各撑开半屏，中间的箭头漂在远处，
   读起来像四列无关的数字。这里要的是「一个短语」，不是一张表。 */
.rv .chg{display:flex;align-items:baseline;gap:9px;
  font-size:12.5px;padding:4px 0;border-top:1px dashed var(--line)}
.rv .chg:first-of-type{border-top:0}
.rv .chg .k{color:var(--muted);width:96px;flex:none}
/* 定宽 + 右对齐：多行之间数值能对成一列，扫一眼就能比 */
.rv .chg b{font-variant-numeric:tabular-nums;min-width:62px;text-align:right}
.rv .chg .arrow{color:var(--muted);flex:none}
/* 差值刻意**不上色**。红涨绿跌是行情语义；这里是「对手降价 10%」——
   对不同的人、不同的策略，同一件事是好是坏完全相反。
   染色等于系统替人下了判断，而这套系统的规矩是「只摆事实」。 */
.rv .delta{font-variant-numeric:tabular-nums;font-size:11.5px;padding:1px 7px;
  border-radius:6px;background:var(--bg);border:1px solid var(--line);color:var(--muted)}
.rv .acts{display:flex;gap:6px;align-items:center;margin-top:11px;flex-wrap:wrap}
.rv .acts input{font:inherit;font-size:12.5px;flex:1;min-width:180px;padding:5px 9px;
  border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text)}
.rv .done{font-size:12.5px;color:var(--ok);margin-top:9px}
.rv .dec{background:var(--bg);border-radius:9px;padding:9px 11px;margin-top:9px;
  font-size:12.5px;line-height:1.8;display:none}
.rv .dec.on{display:block}
.rv .dec .lab{color:var(--muted);font-size:11.5px;margin-top:6px}
"""


# ============================================================
# 核心 JS
# ============================================================

JS_CORE = """
'use strict';
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

function esc(v){
  if (v === null || v === undefined) return '';
  return String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function money(v){ return (v === null || v === undefined) ? '—' : '$' + Number(v).toFixed(2); }
function num(v, d){ if (v === null || v === undefined) return '—';
  return Number(v).toLocaleString('en-US', {maximumFractionDigits: d === undefined ? 0 : d}); }
function pct(v, d){ if (v === null || v === undefined) return '—';
  return (Number(v) * 100).toFixed(d === undefined ? 1 : d) + '%'; }

// 按后端给的 kind 格式化一个指标值。
//
// 为什么不让后端直接给 "$22.99"：后端拼一次、这里再 money() 一次，
// 屏幕上就是 "$$22.99"——只有截图能发现，单测不会红。
// 所以契约是「后端说是什么类型，前端决定长什么样」（见 ARCHITECTURE）。
function kindVal(v, kind){
  if (v === null || v === undefined) return '—';
  switch (kind) {
    case 'money':  return money(v);
    case 'percent': return pct(v);
    case 'rank':   return num(v) + ' 名';
    case 'rating': return Number(v).toFixed(1) + ' 星';
    case 'keywords': return Array.isArray(v) ? (v.length + ' 个词') : String(v);
    case 'bool':   return v ? '有货' : '无货';
    case 'path':   return Array.isArray(v) ? v.join(' › ') : String(v);
    default:       return num(v, 2);
  }
}
function dt(v){
  if (!v) return '—';
  const s = String(v).replace(' ', 'T');
  const d = new Date(s);
  if (isNaN(d.getTime())) return String(v).slice(0, 16);
  const p = n => String(n).padStart(2, '0');
  return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}
function ago(v){
  if (!v) return '—';
  const d = new Date(String(v).replace(' ', 'T'));
  if (isNaN(d.getTime())) return '—';
  const h = (Date.now() - d.getTime()) / 3600000;
  if (h < 1) return Math.max(1, Math.round(h * 60)) + ' 分钟前';
  if (h < 48) return Math.round(h) + ' 小时前';
  return Math.round(h / 24) + ' 天前';
}
function tierTag(t, label){
  const k = (t || 'unknown');
  const text = label || (k === 'unknown' ? '待判定' : k + ' 级');
  return '<span class="tag ' + esc(k) + '">' + esc(text) + '</span>';
}
function statusTag(s){
  const m = {active:'在盯', paused:'已暂停', archived:'已归档', observing:'观察'};
  return '<span class="tag ' + esc(s || '') + '">' + esc(m[s] || s || '—') + '</span>';
}

// 阈值分组的中文名。放在外壳层而不是 config 视图里：
// 健康页也要显示分组名，视图之间互相 `const` 依赖会在拆文件时变成隐形耦合。
const GROUP_TITLES = {diff: '变化判定阈值', tiering: '分层（四同法则）阈值', notify: '推送与冷却'};

class ApiError extends Error {
  constructor(msg, hint, detail){ super(msg); this.hint = hint || ''; this.detail = detail || ''; }
}

let CURRENT_USER = localStorage.getItem('cm_user') || '';

// ── 数据模式（live / mock）─────────────────────────────────────
// 放在 localStorage 而不是内存变量：刷新页面后应该还停在原来那个模式。
// 一个人切到 Mock 看了一眼、再刷新变成真实数据，他会以为 Mock 数据丢了。
//
// 每个请求都带 `X-Data-Mode` —— 包括面板自己的 `/api/dataset`。
// 服务端把模式钉在请求上下文里（见 `app/api/middleware.py`），
// 所以这里漏带一个请求，那一个请求就会读到真实库，
// 而页面上其余部分还是 Mock —— 两套数字同屏，最难查的那种不一致。
let DATA_MODE = localStorage.getItem('cm_mode') || 'live';

function headers(extra){
  const h = Object.assign(
    {'Content-Type': 'application/json', 'X-Data-Mode': DATA_MODE},
    extra || {}
  );
  if (CURRENT_USER) h['X-User-Id'] = CURRENT_USER;
  return h;
}

// 切模式 = 换一个库看。已经加载过的视图必须全部作废重拉，
// 否则切过去看到的是上一个库的 DOM。
function setDataMode(mode){
  if (mode === DATA_MODE) return;
  DATA_MODE = mode;
  localStorage.setItem('cm_mode', mode);
  toast(mode === 'mock' ? '已切到 Mock 数据' : '已切到真实数据');
  reloadAll();
}

async function api(path, body, method){
  const opt = {method: method || (body === undefined ? 'GET' : 'POST'), headers: headers()};
  if (body !== undefined) opt.body = JSON.stringify(body);
  let res;
  try {
    res = await fetch(path, opt);
  } catch (e) {
    // 网络层失败（服务没起来 / 断网）：给一句能行动的话
    throw new ApiError('连不上服务（' + (e && e.message ? e.message : '网络错误') + '）',
                       '确认服务已启动、地址正确；本页面不依赖任何外部资源，断网也能渲染。');
  }
  let json = null;
  try { json = await res.json(); } catch (e) { json = null; }
  if (json === null) {
    throw new ApiError('接口返回的不是 JSON（HTTP ' + res.status + '）',
                       '可能是路由不存在或服务端异常；看服务端日志。');
  }
  if (json.ok !== true) {
    throw new ApiError(json.error || ('HTTP ' + res.status + ' 失败'), json.hint || '', json.detail || '');
  }
  return json;
}

function toast(msg, bad){
  let el = $('#toast');
  if (!el) { el = document.createElement('div'); el.id = 'toast'; el.className = 'toast'; document.body.appendChild(el); }
  el.textContent = msg;
  el.className = 'toast on' + (bad ? ' bad' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast' + (bad ? ' bad' : ''); }, 2600);
}

const RETRY = {};
function errCard(root, e, viewName){
  if (!root) return;
  const body = $('.view-body', root) || root;
  const msg = (e && e.message) ? e.message : String(e);
  const hint = (e && e.hint) ? e.hint : '';
  // 原始错误（多半带一段 SQL）默认折叠：它在，但不当主角
  const detail = (e && e.detail) ? e.detail : '';
  const old = $(':scope > .err', body);
  if (old) old.remove();
  const box = document.createElement('div');
  box.className = 'err';
  box.innerHTML =
    '<div class="t">这一屏没加载出来</div>' +
    '<div class="m">' + esc(msg) + '</div>' +
    (hint ? '<div class="h">建议：' + esc(hint) + '</div>' : '') +
    (detail ? '<details class="exp" style="margin-top:8px"><summary>原始错误（排查用）</summary>' +
      '<div class="h" style="white-space:pre-wrap;margin-top:6px">' + esc(detail) + '</div></details>' : '') +
    '<div class="h">只有这一屏受影响，其它标签页照常可用。</div>' +
    '<div style="margin-top:10px"><button class="btn sm" onclick="window.__retry(\\'' + esc(viewName) + '\\')">重试</button></div>';
  // 插在最前面，**不覆盖**这一屏原有骨架：
  // 覆盖掉的话「重试」按钮按下之后，视图找不到自己的容器（#b-total / #p-table …），
  // 于是第二次失败，而且错的原因变成了"Cannot set properties of null"——
  // 一个本该能自愈的按钮反而让状态更糟。
  body.insertBefore(box, body.firstChild);
  // 其它视图的 DOM 一概不碰 —— 这就是「单 tab 故障隔离」
}

window.__retry = function(name){ loaded[name] = false; safeLoad(name); };

// ── errCard 与 act 的分工 ──────────────────────────────────────────
// errCard：**加载**失败。整屏没东西可看，所以占位在页面顶部，带「重试」。
// act：**动作**失败。页面本身是好的，数据也还在，不该被一张红卡顶掉——
//      用 toast 报错，用户能接着改输入再点一次。
// 两者都只影响当前这一屏，互不传染。

// 写操作的统一包装。
// 为什么必须有：5 个 loader 由 safeLoad 兜住，但**按钮回调没人兜**——
// 异常直接抛进事件处理器，浏览器控制台里有红字，而页面上什么都没发生。
// 用户看到的是"点了没反应"，于是再点一次，于是重复提交。
// 这里统一 catch 一次，别在 5 个按钮里各写一份 try/catch——那种写法下
// 下一个新加的按钮必然漏，而漏掉的表现又和这里一模一样。
function act(fn, label){
  return async function(){
    try {
      return await fn.apply(this, arguments);
    } catch (e) {
      console.error(e);
      const msg = (e && e.message) ? e.message : String(e);
      const hint = (e && e.hint) ? '　' + e.hint : '';
      toast((label ? label + '失败：' : '操作失败：') + msg + hint, true);
    }
  };
}

const LOADERS = {};
const loaded = {};

//: 打开面板先落在哪一屏。选「自家基线」：它是所有判断的上游，
//: 锚点没配好时，看板和池子上的每个数字都只是破折号，而这一页会说清为什么。
const DEFAULT_VIEW = 'baseline';

function register(name, fn){ LOADERS[name] = fn; }

async function safeLoad(name){
  const fn = LOADERS[name];
  if (!fn) return;
  const root = document.getElementById('view-' + name);
  const body = root && $('.view-body', root);
  // 重试前先清掉上一次的错误卡片，否则成功了还挂着一张红牌
  if (body) { const old = $(':scope > .err', body); if (old) old.remove(); }
  try {
    // **不要**把 .view-body 清成"加载中…"：
    // 每个视图的 SECTION 里已经写好了自己的占位（"—"、空表格、"加载中…"），
    // 整体覆盖会把 #b-total / #p-table 这些容器一起删掉，
    // 于是加载函数取到 null 直接抛异常——表现就是"一打开就报错"。
    await fn();
  } catch (e) {
    errCard(root, e, name);
  }
}

async function show(name){
  const n = LOADERS[name] ? name : DEFAULT_VIEW;
  $$('.view').forEach(v => v.classList.toggle('on', v.id === 'view-' + n));
  $$('.tabs a').forEach(a => a.classList.toggle('on', a.dataset.view === n));
  if (!loaded[n]) { loaded[n] = true; await safeLoad(n); }
}

function reloadAll(){
  Object.keys(loaded).forEach(k => { loaded[k] = false; });
  const cur = (location.hash || '#' + DEFAULT_VIEW).slice(1);
  const n = LOADERS[cur] ? cur : DEFAULT_VIEW;
  loaded[n] = true;
  safeLoad(n);
}

// ---- 用户切换（X-User-Id 只是"以谁的身份操作"，不是鉴权）----
async function initUsers(){
  const sel = $('#user-picker');
  if (!sel) return;
  let data;
  try { data = await api('/api/users'); }
  catch (e) { sel.innerHTML = '<option>默认用户</option>'; return; }
  const users = data.items || [];
  sel.innerHTML = users.map(u =>
    '<option value="' + u.id + '">' + esc(u.name) + (u.role === 'owner' ? '（owner）' : '') + '</option>'
  ).join('') || '<option value="">默认</option>';
  if (!CURRENT_USER && data.default_user_id) CURRENT_USER = String(data.default_user_id);
  sel.value = CURRENT_USER;
  const who = $('#who-name');
  const cur = users.find(u => String(u.id) === String(CURRENT_USER));
  if (who) who.textContent = cur ? cur.name : '';
  sel.onchange = () => {
    CURRENT_USER = sel.value;
    localStorage.setItem('cm_user', CURRENT_USER);
    if (who) who.textContent = (sel.options[sel.selectedIndex] || {}).text || '';
    toast('已切换身份，正在按新身份重新加载');
    reloadAll();
  };
}

window.addEventListener('hashchange', () => show((location.hash || '#' + DEFAULT_VIEW).slice(1)));
document.addEventListener('DOMContentLoaded', async () => {
  initUsers();
  show((location.hash || '#' + DEFAULT_VIEW).slice(1));
});
"""


# ============================================================
# HTML 骨架
# ============================================================


def head(title: str) -> str:
    """`<head>` + 打开的 `<body>`。全部内联，无一个外链。"""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{title}</title>\n"
        f"<style>{CSS}</style>\n"
        "</head>\n<body>\n"
    )


def topbar(tenant_id: str, tabs: list[tuple[str, str]]) -> str:
    """顶部：品牌 + 五个标签 + 身份切换。

    标签由调用方从 `app.web.views.VIEWS` 生成（`panel.py` 负责），
    这样"标签顺序"和"section 顺序"只有一处定义。

    「身份切换」这一小块要说明它是**操作人标记**，不是登录：
    面板没有鉴权（单租户自用），这个下拉只是为了让人能回答"这条动作是谁记的"。
    """
    nav = "\n".join(
        f'    <a href="#{key}" data-view="{key}">{label}</a>' for key, label in tabs
    )
    return f"""
<header class="top">
  <div class="brand">
    <b>竞品监控</b>
    <span class="sub">{tenant_id}</span>
  </div>
  <nav class="tabs">
{nav}
  </nav>
  <div class="who">
    <span>操作人</span>
    <select id="user-picker" title="以谁的身份操作（不是登录，面板无鉴权）"></select>
    <span id="who-name" class="dim"></span>
  </div>
</header>
<main>
"""


def tail(views_js: str) -> str:
    """闭合 `<main>`，把核心 JS + 各视图 JS 内联，收尾。"""
    return (
        "</main>\n"
        "<script>\n"
        + JS_CORE
        + "\n// ===== 各视图 =====\n"
        + views_js
        + "\n</script>\n</body>\n</html>\n"
    )
