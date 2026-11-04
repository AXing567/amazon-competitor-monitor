"""视图①：自家基线。

## 为什么它是第一个标签

竞品监控的每一次判断都是「和谁比、比什么、比多少算异常」。
自家锚点缺一个数，后面就有一整类判断变成「判不了」——
而这个事实在别的视图里表现为"某个数字是破折号"，看不出根因。

所以这一页要回答的是**上游**问题：我现在站在什么位置上。

## 「看」和「改」放同一页

分开的话，人看到"缺保本价"还得跳到另一个页面去填，
而那个页面不知道缺的是哪一格——回来的路上信息就丢了。
这里每个字段一行，缺的那行标红，改完直接刷新。

## 数据模式为什么在这页

它不是这一页的局部状态，而是**整个系统**的全局状态（服务端按请求头切库）。
放在别处就得在导航栏再开一个位置；而这一页正是"哪个库的数据算数"
最该被说清楚的地方——顶上看一眼，就知道下面的数字该不该信。

## 面板不做判断（红线）

就绪度、能力边界、缺什么影响什么、哪些是假设值——全部来自
`/api/cap/get_baseline` 与 `/api/dataset`。这一层只做格式化与排版，
一处阈值比较都没有。`can_*` 四个布尔位直接来自 L2 的 `assess()`。
"""
from __future__ import annotations

SECTION = """
<section class="view" id="view-baseline">
  <div class="view-body">

    <h2 class="sec">数据模式</h2>
    <div class="note">
      两套数据物理隔离：<b>真实数据</b>是你采来的竞品，<b>Mock 数据</b>是一套可随时重建的演示集。
      切过去之后，本页和其余所有标签都跟着换库——不是只换这一屏。
    </div>
    <div class="modebox" id="bl-modes">
      <div class="empty">加载中…</div>
    </div>

    <h2 class="sec">自家锚点</h2>
    <div id="bl-anchor"><div class="empty">加载中…</div></div>

    <h2 class="sec">五维明细</h2>
    <div class="note" id="bl-dims-note"></div>
    <div id="bl-dims"></div>

    <h2 class="sec">改锚点</h2>
    <div class="card" id="bl-form"><div class="empty">加载中…</div></div>

  </div>
</section>
"""


JS = r"""
register('baseline', async () => {
  await blLoad();
});

// 五个「只能是拍的」字段：任何外部数据源都给不出，填了也只有两种身份——
// 你自己算过的真值，或者先拍一个数占位。清单写死在这里而不是从后端取，
// 是因为「哪些字段可疑」是领域知识，不该随请求变化。
const ASSUMABLE = [
  ['fba_available_units', 'FBA 可售库存'],
  ['daily_sales_avg', '日均销量'],
  ['lead_time_days', '补货周期'],
  ['break_even_price', '保本价'],
  ['target_margin_rate', '目标毛利率'],
];

let BL_STATE = null;

async function blLoad(){
  // 第二个参数传 `{}` 是必须的：`api(path)` 不传 body 时走 GET，
  // 而 `GET /api/cap/{name}` 返回的是**能力元数据**（参数表、描述），
  // 不执行能力。写成 `api('/api/cap/get_baseline')` 不会报错——
  // 它会拿到一份 `{ok:true, name, params...}`，里面没有 `exists`，
  // 于是页面永远显示"还没有自家锚点"，而库里明明有。
  const [ds, bl] = await Promise.all([
    api('/api/dataset'),
    api('/api/cap/get_baseline', {}),
  ]);
  blRenderModes(ds);
  BL_STATE = bl;
  if (!bl.exists) {
    $('#bl-anchor').innerHTML =
      '<div class="warnbox"><b>还没有自家锚点。</b><br>' +
      '没有它，系统无法判断任何一个竞品：算不出价格比、算不出词重合、' +
      '也说不出「该不该跟价」。先在下面填三个数——售价、保本价、日均销量。</div>';
    $('#bl-dims').innerHTML = '';
    $('#bl-dims-note').textContent = '';
    blRenderForm(null);
    return;
  }
  blRenderAnchor(bl);
  blRenderDims(bl.baseline);
  blRenderForm(bl.baseline);
}

// ── 数据模式 ──────────────────────────────────────────────────
function blRenderModes(ds){
  const hint = {
    live: '你采来的真实竞品。这里的结论可以被当真。',
    mock: '一套自洽的演示数据，可随时重建。结论只用于验证链路。',
  };
  $('#bl-modes').innerHTML = (ds.modes || []).map(m => {
    const on = m.mode === ds.current;
    const state = m.error ? '读不出来'
      : (!m.db_exists ? '库还没建'
      : (!m.ready ? '还没有自家锚点'
      : (m.competitors + ' 个竞品 · ' + m.snapshots + ' 条快照')));
    const dotCls = m.error ? 'bad' : (m.ready ? 'ok' : '');
    const acts = [];
    if (m.mode === 'mock') {
      acts.push('<button class="btn sm" data-rebuild="1">' +
        (m.ready ? '重建 Mock 数据' : '生成 Mock 数据') + '</button>');
      if (m.ready) acts.push('<button class="btn sm danger" data-clear="1">清空</button>');
    }
    return '<div class="mode' + (on ? ' on' : '') + '" data-mode="' + esc(m.mode) + '">' +
      '<div class="t"><span class="dot ' + dotCls + '"></span>' + esc(m.label) +
      (on ? '<span class="tag active" style="margin-left:auto">当前</span>' : '') + '</div>' +
      '<div class="d">' + esc(hint[m.mode] || '') + '<br>' +
      esc(state) + (m.my_asin ? ' · ' + esc(m.my_asin) : '') +
      (m.last_capture ? '<br>最后采集 ' + esc(dt(m.last_capture)) : '') +
      (m.error ? '<br><span class="assume-l">' + esc(m.error) + '</span>' : '') +
      '</div>' +
      (acts.length ? '<div class="act">' + acts.join('') + '</div>' : '') +
      '</div>';
  }).join('');

  $$('#bl-modes [data-mode]').forEach(el => {
    el.onclick = () => setDataMode(el.dataset.mode);
  });
  // 按钮的点击会冒泡到卡片上触发切模式——切到 Mock 再造数据是对的，
  // 但"清空"之后还留在 Mock 模式也很合理，所以这里只用 stopPropagation
  // 拦住的是"点按钮等于点卡片"这一件事，不是拦业务。
  $$('#bl-modes [data-rebuild]').forEach(b => {
    b.onclick = act(async (ev) => {
      ev.stopPropagation();
      b.disabled = true;
      const old = b.textContent;
      b.textContent = '正在造数…';
      try {
        const res = await api('/api/dataset/mock/rebuild', {}, 'POST');
        // 造完必须切到 mock，否则用户点完"生成"看不到任何变化
        DATA_MODE = 'mock';
        localStorage.setItem('cm_mode', 'mock');
        toast('Mock 数据已生成：' + (res.competitors || 0) + ' 个竞品');
        reloadAll();
      } finally {
        b.disabled = false;
        b.textContent = old;
      }
    }, '生成 Mock 数据');
  });
  $$('#bl-modes [data-clear]').forEach(b => {
    b.onclick = act(async (ev) => {
      ev.stopPropagation();
      await api('/api/dataset/mock/clear', {}, 'POST');
      toast('Mock 数据已清空');
      reloadAll();
    }, '清空 Mock 数据');
  });
}

// ── 锚点总览 ──────────────────────────────────────────────────
function blRenderAnchor(bl){
  const a = bl.assessment || {};
  const b = bl.baseline || {};
  const caps = a.capabilities || {};
  const capList = [
    ['can_tier', '分层（四同法则）'],
    ['can_judge_price_follow', '跟价判断'],
    ['can_judge_attack_window', '进攻窗口'],
    ['can_detect_listing_copy', '抄 listing 检测'],
  ];
  const gradeCls = a.readiness >= 90 ? '' : 'part';
  const dos = a.days_of_supply;
  const dosText = dos === null || dos === undefined ? '—'
    : (Number(dos).toFixed(1) + ' 天');

  $('#bl-anchor').innerHTML =
    '<div class="card">' +
      '<div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap">' +
        '<div><div class="dim" style="font-size:11.5px">自家 ASIN</div>' +
          '<div style="font-size:19px;font-weight:700;letter-spacing:-.3px">' +
          esc(b.asin || '—') + '</div></div>' +
        '<div style="margin-left:auto;text-align:right">' +
          '<div class="dim" style="font-size:11.5px">锚点就绪度</div>' +
          '<div class="big">' + (a.readiness === undefined ? '—' : a.readiness + '%') +
          ' <span class="dim" style="font-size:13px;font-weight:400">' + esc(a.grade || '') +
          ' 级</span></div></div>' +
      '</div>' +
      '<div class="bar ' + gradeCls + '" style="margin:12px 0 14px"><i style="width:' +
        (a.readiness || 0) + '%"></i></div>' +
      '<div class="kv">' +
        '<span class="k">数据来源</span><span>' + esc(b.source_label || '—') + '</span>' +
        '<span class="k">可售天数</span><span>' + esc(dosText) +
          ' <span class="dim">（' + esc(dosWord(a.inventory_status)) + '）</span></span>' +
        '<span class="k">假设值</span><span>' +
          (b.assumed_fields && b.assumed_fields.length
            ? '<span class="assume-l">' + b.assumed_fields.map(f => esc(fieldZh(f))).join('、') +
              '</span>'
            : '<span class="dim">无（全部字段都有实测或明确来源）</span>') +
        '</span>' +
      '</div>' +
      '<div class="caps">' + capList.map(([k, label]) =>
        '<span class="cap ' + (caps[k] ? 'yes' : '') + '">' +
        (caps[k] ? '✓ ' : '✗ ') + esc(label) + '</span>').join('') + '</div>' +
      ((a.warnings || []).length
        ? '<div class="warnbox" style="margin-top:12px">' +
          a.warnings.map(w => esc(w)).join('<br><br>') + '</div>'
        : '') +
    '</div>';
}

function dosWord(s){
  return {alert: '低于 20 天，可能断货', normal: '正常区间',
          surplus: '超过 60 天，有积压风险', unknown: '算不出来'}[s] || '—';
}

// 字段英文名 → 中文名。取不到就原样露出英文：宁可难看，也不许
// 悄悄把一个不认识的字段名渲染成空白（那会让告警里少一项而没人发现）。
function fieldZh(name){
  const map = {};
  const g = (BL_STATE && BL_STATE.baseline && BL_STATE.baseline.groups) || [];
  g.forEach(x => (x.fields || []).forEach(f => { map[f.name] = f.label; }));
  ((BL_STATE && BL_STATE.baseline && BL_STATE.baseline.extras) || []).forEach(f => {
    map[f.name] = f.label;
  });
  return map[name] || name;
}

// ── 五维明细 ──────────────────────────────────────────────────
function blRenderDims(b){
  const groups = b.groups || [];
  const total = groups.reduce((s, g) => s + g.total, 0);
  const filled = groups.reduce((s, g) => s + g.filled, 0);
  $('#bl-dims-note').textContent =
    '五大维度共 ' + total + ' 个字段，已填 ' + filled + ' 个。' +
    '带「假设值」标记的格子，由它推导出的结论只能用来验证链路，不能用来做决定。';

  $('#bl-dims').innerHTML = groups.map(g => {
    const ratio = g.total ? g.filled / g.total : 0;
    const cls = ratio >= 1 ? '' : (ratio > 0 ? 'part' : 'none');
    const rows = (g.fields || []).map(f =>
      '<div class="frow">' +
        '<div class="l">' + esc(f.label) + '</div>' +
        '<div class="v">' + blFieldValue(f) + '</div>' +
        '<div>' + (f.assumed
          ? '<span class="tag A" title="这不是实测值，是拍的或先占位的">假设值</span>'
          : (f.filled ? '' : '<span class="dim" style="font-size:11px">缺</span>')) +
        '</div>' +
      '</div>').join('');
    return '<div class="dimcard">' +
      '<div class="hd"><span class="n">' + esc(g.name) + '</span>' +
        '<span class="tag ' + (g.filled === g.total ? 'active' : 'B') + '">' +
          g.filled + '/' + g.total + '</span>' +
        (g.auto_fillable
          ? '<span class="dim" style="font-size:11px">可自动补</span>'
          : '<span class="dim" style="font-size:11px">只能人填</span>') +
        '<span class="c">' + (ratio >= 1 ? '齐了' : '差 ' + (g.total - g.filled) + ' 项') +
        '</span></div>' +
      '<div class="why">' + esc(g.why || '') + '</div>' +
      '<div class="bar ' + cls + '"><i style="width:' + Math.round(ratio * 100) + '%"></i></div>' +
      rows + '</div>';
  }).join('') +
  // `category_path` 不在五维里（不计入就绪度），但它决定了「节点」这一维能不能判。
  // 不显示的话，人会以为五维全绿就万事俱备——而节点维度其实还是瞎的。
  ((b.extras || []).length
    ? '<div class="dimcard"><div class="hd"><span class="n">补充字段</span>' +
      '<span class="dim" style="font-size:11px;margin-left:auto">不计入就绪度</span></div>' +
      '<div class="why">不进五维评分，但「类目节点」这一维只认它；缺了就有一整维判不了。</div>' +
      b.extras.map(f => '<div class="frow"><div class="l">' + esc(f.label) + '</div>' +
        '<div class="v">' + blFieldValue(f) + '</div><div>' +
        (f.filled ? '' : '<span class="dim" style="font-size:11px">缺</span>') +
        '</div></div>').join('') + '</div>'
    : '');
}

// 值的显示。**类型由后端给（kind），格式由这里定** —— 后端只管"这是金额还是比例"，
// 一旦它开始拼 `$`，前端再 money() 一次就会变成 $$22.99，而那种错只有截图能发现。
function blFieldValue(f){
  if (!f.filled) return '<span class="dim">—</span>';
  const v = f.value;
  switch (f.kind) {
    case 'money': return money(v);
    case 'percent': return pct(v, 1);
    case 'int': return num(v);
    case 'float1': return num(v, 1);
    case 'rank': return '#' + num(v);
    case 'rating': return Number(v).toFixed(1) + ' ★';
    case 'list':
      return (v || []).map((x, i) =>
        '<div class="dim" style="font-size:12px">' + (i + 1) + '. ' + esc(String(x)) +
        '</div>').join('');
    case 'path':
      return esc((v || []).join(' › '));
    case 'image':
      return '<a href="' + esc(v) + '" target="_blank" rel="noopener">查看主图</a>';
    case 'keywords': {
      const items = Object.entries(v || {}).sort((a, b) => b[1] - a[1]);
      if (!items.length) return '<span class="dim">—</span>';
      return items.map(([k, s]) =>
        '<div>' + esc(k) + ' <span class="dim" style="font-size:11.5px">' + pct(s, 1) +
        '</span></div>').join('');
    }
    default: {
      const s = String(v);
      return '<span title="' + esc(s) + '">' +
        esc(s.length > 110 ? s.slice(0, 110) + '…' : s) + '</span>';
    }
  }
}

// ── 录入表单 ──────────────────────────────────────────────────
// 只有这 12 个字段能通过 `set_baseline` 写。加字段时要同时改能力层的 params，
// 否则 `_coerce_params` 会把它静默丢掉——表单显示"保存成功"，库里什么都没变。
const FORM_NUM = [
  ['current_price', '当前售价', '0.01', '美元'],
  ['break_even_price', '保本价', '0.01', '美元；跌破就不该跟'],
  ['target_margin_rate', '目标毛利率', '0.01', '0~1 的小数，如 0.28'],
  ['daily_sales_avg', '日均销量', '0.1', '单/天'],
  ['fba_available_units', 'FBA 可售库存', '1', '件'],
  ['lead_time_days', '补货周期', '1', '天'],
  ['sub_category_rank', '子类目 BSR', '1', '名次'],
  ['rating', '星级', '0.1', '0~5'],
  ['review_count', '评论数', '1', '条'],
];

function blRenderForm(b){
  const val = (name, def) => {
    if (!b) return def === undefined ? '' : def;
    const g = (b.groups || []).concat([{ fields: b.extras || [] }]);
    for (const grp of g) {
      for (const f of (grp.fields || [])) if (f.name === name) return f.value;
    }
    return def === undefined ? '' : def;
  };
  // `asin` 不在五维里——它是这一行数据的主键，挂在 `baseline` 顶层。
  // 走 `val('asin')` 会一路找不到、静默返回空串，于是"改 ASIN"永远提交不上去。
  const asinVal = b ? (b.asin || '') : '';
  const assumed = new Set((b && b.assumed_fields) || []);

  $('#bl-form').innerHTML =
    '<div class="note">' + (b
      ? '只改你想改的格子——留空的字段不会被覆盖。'
      : '首次设置：<b>ASIN、当前售价、保本价、日均销量</b>四项填上就能开始判了。') + '</div>' +
    '<div class="field"><div class="lab"><div class="k">自家 ASIN</div>' +
      '<div class="h">' + (b ? '已有锚点，改 ASIN 等于换一个产品，慎改。' : '必填。') +
      '</div></div>' +
      '<div><input id="bl-f-asin" value="' + esc(asinVal) + '" ' +
      (b ? '' : 'placeholder="B0XXXXXXXX"') + '></div></div>' +

    FORM_NUM.map(([k, label, step, unit]) =>
      '<div class="field"><div class="lab"><div class="k">' + esc(label) +
      '</div><div class="h">' + esc(unit) + '</div></div>' +
      '<div><input type="number" step="' + step + '" id="bl-f-' + k + '" value="' +
      (val(k) === null || val(k) === undefined ? '' : esc(val(k))) + '"></div></div>'
    ).join('') +

    '<div class="field wide"><div class="lab"><div class="k">核心出单词</div>' +
      '<div class="h">一行一个，写成 <code>词 = 流量占比</code>。' +
      '「流量池」这一维只认它，缺了就判不了重合度。</div></div>' +
      '<div><textarea id="bl-f-core_keywords" rows="4" placeholder="roll up dish drying rack = 0.81">' +
      esc(kwToText(val('core_keywords'))) + '</textarea></div></div>' +

    '<div class="field wide"><div class="lab"><div class="k">自家类目路径</div>' +
      '<div class="h">从根到叶，一行一级。「节点」这一维只认它。</div></div>' +
      '<div><textarea id="bl-f-category_path" rows="5" placeholder="Home &amp; Kitchen">' +
      esc((val('category_path') || []).join('\n')) + '</textarea></div></div>' +

    '<div class="field wide"><div class="lab"><div class="k">标题</div>' +
      '<div class="h">对手抄你 listing 时的比对基准</div></div>' +
      '<div><input id="bl-f-title" value="' + esc(val('title', '')) + '"></div></div>' +

    '<div class="field wide"><div class="lab"><div class="k">主图链接</div>' +
      '<div class="h">可留空</div></div>' +
      '<div><input id="bl-f-image_url" value="' + esc(val('image_url', '')) + '"></div></div>' +

    '<div class="field wide"><div class="lab"><div class="k">五点描述</div>' +
      '<div class="h">一行一条</div></div>' +
      '<div><textarea id="bl-f-bullet_points" rows="5">' +
      esc(((val('bullet_points') || [])).join('\n')) + '</textarea></div></div>' +

    // 这一段**不能**用 `.field`：那一栏的右列固定 150px，塞进五个复选框会
    // 挤成一列竖排并溢出卡片（第一版就是这样，截图上标签跑到卡片外面去了）。
    '<div style="padding:11px 0;border-bottom:1px dashed var(--line)">' +
      '<div class="k" style="font-weight:600;margin-bottom:3px">哪些是拍的</div>' +
      '<div class="h" style="font-size:11.5px;color:var(--muted);line-height:1.65;margin-bottom:8px">' +
        '勾上的字段会在体检里触发告警：「由它们推导出的结论不可信」。填了真值就取消勾选。' +
      '</div>' +
      '<div style="display:flex;flex-wrap:wrap;gap:8px 20px;font-size:12.5px">' +
      ASSUMABLE.map(([k, label]) =>
        '<label style="display:inline-flex;align-items:center;gap:5px;white-space:nowrap">' +
        '<input type="checkbox" data-assume="' + k + '"' +
        (assumed.has(k) ? ' checked' : '') + '> ' + esc(label) + '</label>').join('') +
      '</div>' +
    '</div>' +

    '<div style="margin-top:14px;display:flex;gap:8px;align-items:center">' +
      '<button class="btn primary" id="bl-save">保存</button>' +
      '<span class="note" style="margin:0" id="bl-save-msg"></span>' +
    '</div>';
  $('#bl-save').onclick = act(blSave, '保存锚点');
}

function kwToText(v){
  if (!v || typeof v !== 'object') return '';
  return Object.entries(v).map(([k, s]) => k + ' = ' + s).join('\n');
}

function parseKw(text){
  const out = {};
  (text || '').split('\n').forEach(line => {
    const t = line.trim();
    if (!t) return;
    const i = t.lastIndexOf('=');
    if (i < 0) throw new Error('这一行看不懂：' + t + '　（要写成「词 = 0.81」）');
    const k = t.slice(0, i).trim();
    const n = Number(t.slice(i + 1).trim());
    if (!k) throw new Error('等号左边是空的：' + t);
    if (!isFinite(n) || n < 0) throw new Error('「' + k + '」的占比不是数字：' + t);
    out[k] = n;
  });
  return out;
}

function lines(text){
  return (text || '').split('\n').map(s => s.trim()).filter(Boolean);
}

async function blSave(){
  const body = {};
  const put = (k, v) => { if (v !== '' && v !== null && v !== undefined) body[k] = v; };

  put('asin', ($('#bl-f-asin').value || '').trim().toUpperCase());
  FORM_NUM.forEach(([k]) => {
    const raw = $('#bl-f-' + k).value;
    if (raw !== '') put(k, Number(raw));
  });
  // 文本类字段**允许清空**：`put` 的空串判断对它们是错的——
  // 「把这个字段删掉」是一个合法意图，而区分"没填"和"想删"只能靠
  // 「用户动没动过这个输入框」。这里用 `!== 原值` 判断更实在。
  const base = BL_STATE && BL_STATE.baseline;
  const before = name => {
    if (!base) return '';
    const all = (base.groups || []).reduce((a, g) => a.concat(g.fields || []), [])
      .concat(base.extras || []);
    const hit = all.find(f => f.name === name);
    return hit ? hit.value : '';
  };

  const kwRaw = $('#bl-f-core_keywords').value;
  const kw = parseKw(kwRaw);
  const kwBefore = before('core_keywords');
  if (kwRaw.trim() || (kwBefore && Object.keys(kwBefore).length)) put('core_keywords', kw);

  const cat = lines($('#bl-f-category_path').value);
  if (cat.length) put('category_path', cat);

  [['title', '#bl-f-title'], ['image_url', '#bl-f-image_url']].forEach(([k, sel]) => {
    const v = $(sel).value.trim();
    if (v) put(k, v);
  });
  const bullets = lines($('#bl-f-bullet_points').value);
  if (bullets.length) put('bullet_points', bullets);

  const assumed = $$('#bl-form [data-assume]').filter(c => c.checked).map(c => c.dataset.assume);
  body.assumed_fields = assumed;
  // 来源跟着「还有没有假设值」走：全填真值 = 手工录入；还有勾选 = 仍然有拍的。
  body.source = 'manual';

  if (!Object.keys(body).length) { toast('没有要改的东西', true); return; }

  $('#bl-save').disabled = true;
  try {
    const res = await api('/api/cap/set_baseline', body);
    $('#bl-save-msg').textContent = res.created ? '已新建锚点' : '已保存';
    toast('锚点已保存');
    await blLoad();
    // 锚点变了，池子的分层依据也变了——让其它视图下次进来自动重拉
    Object.keys(loaded).forEach(k => { if (k !== 'baseline') loaded[k] = false; });
  } finally {
    $('#bl-save').disabled = false;
  }
}
"""


def section() -> str:
    return SECTION


def js() -> str:
    return JS
