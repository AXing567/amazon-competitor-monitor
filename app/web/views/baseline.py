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

    <h2 class="sec">自家数据从哪来</h2>
    <div id="bl-source"><div class="empty">加载中…</div></div>

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
  blRenderSource(bl);
  if (!bl.exists) {
    $('#bl-anchor').innerHTML =
      '<div class="warnbox"><b>还没有自家锚点。</b><br>' +
      '没有它，系统无法判断任何一个竞品：算不出价格比、算不出词重合、' +
      '也说不出「该不该跟价」。<br>' +
      '两条路：① 配好 SP-API 后点上面的「从 SP-API 同步」；' +
      '② 直接在下面手工填——<b>售价、保本价、日均销量</b>三个数就能开始判了。</div>';
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

// ── 自家数据从哪来 ────────────────────────────────────────────
// 这一块回答的不是「值是多少」，而是「这个值谁给的、缺的该去哪拿」。
//
// 为什么非要有这一块：真实模式下自家数据只有两个合法来源——SP-API 与手工。
// 而卖家精灵反推同样能填出价格/星级/评价，页面上一视同仁地显示，看起来
// 和"接入完成"一模一样。**错的不是数值，是它的身份。**
const ORIGIN_META = {
  sp_api:       {cls: 'sp_api',       text: 'SP-API'},
  manual:       {cls: 'manual',       text: '手工'},
  sellersprite: {cls: 'sellersprite', text: '反推'},
  simulated:    {cls: 'simulated',    text: '模拟'},
};

function blOriginTag(origin){
  if (!origin) return '<span class="tag nokind" title="这一格的历史数据没有记录来源">来源未标注</span>';
  const m = ORIGIN_META[origin] || {cls: 'nokind', text: origin};
  return '<span class="tag ' + m.cls + '">' + esc(m.text) + '</span>';
}

function blRenderSource(bl){
  const d = bl.data_source || {};
  const sp = d.spapi || {};
  const cnt = d.counts || {};
  const live = d.mode === 'live';

  const countText = ['sp_api', 'manual', 'sellersprite', 'simulated', 'unknown']
    .filter(k => cnt[k])
    .map(k => (ORIGIN_META[k] ? ORIGIN_META[k].text : '未标注') + ' ' + cnt[k])
    .join(' · ') || '还没有任何字段';

  let html = '';

  if (live) {
    html += '<div class="note">真实模式下，自家数据只认两个来源：' +
      '<b>SP-API 同步</b>（库存/销量/到手价/标题/主图/类目）和<b>手工录入</b>' +
      '（保本价、成本、交期、星级评价这些接口给不了的）。' +
      '卖家精灵反推的数据只出现在 Mock 里。</div>';

    if (!sp.configured) {
      html += '<div class="warnbox" style="margin-top:10px">' +
        '<b>SP-API 还没配。</b>缺 ' +
        (sp.missing_env || []).map(e => '<code>' + esc(e) + '</code>').join('、') +
        '。<br>在配好之前，自家锚点只能手工填——手工填的数据一样能用，' +
        '只是会过时，需要你定期回来改。' +
        '<details style="margin-top:8px"><summary style="cursor:pointer">三项凭据去哪拿</summary>' +
        '<pre style="white-space:pre-wrap;font-size:12px;line-height:1.7;margin:8px 0 0">' +
        esc(sp.hint || '') + '</pre></details></div>';
    } else {
      html += '<div class="note" style="margin-top:8px">SP-API 已配置（region=' +
        esc(sp.region || '') + '）。同步只写接口真能给的字段，' +
        '拿不到的会逐条说明原因，不会覆盖成空。</div>';
    }

    html += '<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">' +
      '<button class="btn" id="bl-sync">从 SP-API 同步</button>' +
      '<button class="btn sm" id="bl-sync-dry">先预览（不写库）</button></div>';
  } else {
    html += '<div class="note">这是 Mock 数据，但<b>不是凭空编的</b>：' +
      '能反推的字段用卖家精灵的真实数据（价格、星级、评价数、日销估算、标题、' +
      '主图、类目），反推拿不到的才模拟补充（库存、保本价、交期、目标毛利、' +
      '子类排名、词表）。<br>这份锚点<b>只在 Mock 里有效</b>：' +
      '真实模式不接受反推值，切过去会看到「未接入」和接入指引。</div>';
  }

  html += '<div class="kv" style="margin-top:10px">' +
    '<span class="k">整批来源</span><span>' +
      (d.anchor_empty
        ? '<span class="dim">—（锚点还是空的，没有任何一批数据写进来过）</span>'
        : esc(d.anchor_source_label || '未标注') +
          (d.anchor_source ? ' <code>' + esc(d.anchor_source) + '</code>' : '')) +
      '</span>' +
    '<span class="k">逐格统计</span><span>' + esc(countText) + '</span>' +
    '</div>';

  if (d.blocked_reason) {
    html += '<div class="warnbox" style="margin-top:10px"><b>这份锚点在真实模式下已停用。</b><br>' +
      esc(d.blocked_reason) + '</div>';
  }
  if ((d.blocked_fields || []).length) {
    html += '<div class="warnbox" style="margin-top:10px">这几格在真实模式下不算数：' +
      d.blocked_fields.map(f => esc(f.label) + '（' + esc(f.origin_label || '') + '）').join('、') +
      '</div>';
  }

  html += '<div id="bl-sync-result"></div>';
  $('#bl-source').innerHTML = html;

  const onSync = (dry) => act(async (ev) => {
    const btn = ev.currentTarget;
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = dry ? '取数中…' : '同步中…';
    try {
      const res = await api('/api/panel/baseline/sync',
        {dry_run: dry}, 'POST');
      blRenderSyncResult(res, dry);
      if (res.ok && !dry) {
        toast('已同步 ' + Object.keys(res.written || {}).length + ' 个字段');
        reloadAll();
      }
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }, dry ? '预览 SP-API 数据' : '从 SP-API 同步');

  const b1 = $('#bl-sync'); if (b1) b1.onclick = onSync(false);
  const b2 = $('#bl-sync-dry'); if (b2) b2.onclick = onSync(true);
}

function blRenderSyncResult(res, dry){
  const box = $('#bl-sync-result');
  if (!box) return;

  if (!res.ok) {
    box.innerHTML = '<div class="err" style="margin-top:10px">' +
      '<div class="t">' + (dry ? '预览' : '同步') + '没成功</div>' +
      '<div class="m">' + esc(res.error || '') + '</div>' +
      ((res.missing_env || []).length
        ? '<div class="m">缺：' + res.missing_env.map(e => '<code>' + esc(e) + '</code>').join('、') + '</div>'
        : '') +
      (res.hint ? '<details style="margin-top:6px"><summary style="cursor:pointer">补法</summary>' +
        '<pre style="white-space:pre-wrap;font-size:12px;line-height:1.7">' + esc(res.hint) + '</pre></details>' : '') +
      '</div>';
    return;
  }

  const written = res.written || {};
  const changed = res.changed || [];
  const skipped = res.skipped || [];
  const notes = res.notes || [];
  const errors = res.errors || [];

  const w = Object.keys(written);
  box.innerHTML = '<div class="card" style="margin-top:10px">' +
    '<div style="font-weight:700;margin-bottom:6px">' +
      (dry ? '预览：能拿到这些（还没写库）' : '已写入 ' + w.length + ' 个字段') +
      ' · 数据质量 ' + esc(res.data_quality || '—') + '</div>' +
    (w.length
      ? '<div class="kv">' + w.map(k =>
          '<span class="k">' + esc(fieldZh(k)) + '</span><span>' +
          esc(blFmtRaw(written[k])) + '</span>').join('') + '</div>'
      : '<div class="dim">一个字段都没拿到。</div>') +
    (changed.length
      ? '<div class="note" style="margin-top:8px">覆盖了原来的值：' + changed.map(c =>
          esc(fieldZh(c.field)) + ' ' + esc(String(c.before)) + ' → ' + esc(String(c.after))
        ).join('；') + '</div>'
      : '') +
    (skipped.length
      ? '<div class="note" style="margin-top:8px"><b>没写的字段及原因：</b><br>' +
        skipped.map(s => esc(fieldZh(s.field)) + '：' + esc(s.reason)).join('<br>') + '</div>'
      : '') +
    (notes.length
      ? '<div class="warnbox" style="margin-top:8px">' + notes.map(n => esc(n)).join('<br><br>') + '</div>'
      : '') +
    (errors.length
      ? '<div class="warnbox" style="margin-top:8px"><b>这几段接口这轮失败了</b>' +
        '（不是没有数据）：<br>' + errors.map(e => esc(e)).join('<br>') + '</div>'
      : '') +
    '</div>';
}

// 同步结果里的原始值：不猜类型，数组/对象转成短文本。
function blFmtRaw(v){
  if (Array.isArray(v)) return v.join(' › ');
  if (v && typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

// ── 锚点总览 ──────────────────────────────────────────────────
function blRenderAnchor(bl){
  const a = bl.assessment || {};
  const b = bl.baseline || {};
  const caps = a.capabilities || {};
  // 空锚点（一行数据都没有）和「填了但都是假设值」是两种状态，措辞必须分开。
  const empty = !!(bl.data_source && bl.data_source.anchor_empty);
  // 整行被当前模式拒掉时，就绪度和能力勾都不能照常显示：
  // 「已停用」+「100% A 级 ✓ 跟价判断」同时出现在一张卡上，读的人只能挑一句信。
  const usable = a.usable !== false;
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
          (usable
            ? '<div class="big">' + (a.readiness === undefined ? '—' : a.readiness + '%') +
              ' <span class="dim" style="font-size:13px;font-weight:400">' + esc(a.grade || '') +
              ' 级</span></div>'
            : '<div class="big" style="font-size:16px">不可用</div>' +
              '<div class="dim" style="font-size:11.5px">当前模式下这份锚点被拒</div>') +
        '</div></div>' +
      '<div class="bar ' + (usable ? gradeCls : 'none') + '" style="margin:12px 0 14px"><i style="width:' +
        (usable ? (a.readiness || 0) : 0) + '%"></i></div>' +
      '<div class="kv">' +
        '<span class="k">数据来源</span><span>' +
          (empty
            ? '<span class="dim">—（锚点还没填，谈不上来源）</span>'
            : esc(b.source_label || '—')) + '</span>' +
        '<span class="k">可售天数</span><span>' + esc(usable ? dosText : '—') +
          ' <span class="dim">（' + esc(usable ? dosWord(a.inventory_status) : '锚点不可用，算出来也不作数') +
          '）</span></span>' +
        '<span class="k">假设值</span><span>' +
          (b.assumed_fields && b.assumed_fields.length
            ? '<span class="assume-l">' + b.assumed_fields.map(f => esc(fieldZh(f))).join('、') +
              '</span>'
            : (empty
                // 空锚点走这里如果写「全部字段都有实测」，就是拿一句漂亮话
                // 盖住了「一格都没填」——页面上最不该出现的就是这种句子。
                ? '<span class="dim">—（还没有任何字段，谈不上实测）</span>'
                : '<span class="dim">无（已填的都有实测或明确来源）</span>')) +
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
  const mode = (BL_STATE && BL_STATE.data_source && BL_STATE.data_source.mode) || 'live';
  $('#bl-dims-note').textContent =
    '五大维度共 ' + total + ' 个字段，已填 ' + filled + ' 个。' +
    '右边那一列是每一格的出身：' +
    (mode === 'live'
      ? 'SP-API 同步（后台来的，能重跑）· 手工（你填的，会过时）。反推值在真实模式下不出现。'
      : '反推（卖家精灵真实数据）· 模拟（反推拿不到，编的）· 手工。') +
    '带「假设值」的格子，由它推导出的结论只能用来验证链路，不能用来做决定。';

  $('#bl-dims').innerHTML = groups.map(g => {
    const ratio = g.total ? g.filled / g.total : 0;
    const cls = ratio >= 1 ? '' : (ratio > 0 ? 'part' : 'none');
    const live = ((BL_STATE && BL_STATE.data_source && BL_STATE.data_source.mode) || 'live') === 'live';
    const rows = (g.fields || []).map(f => {
      // 三个徽标互斥：假设值 > 来源 > 缺。
      // 为什么要「来源」这一列：一整排数字看起来都很确定，只有逐格标出
      // 「SP-API 同步 / 手工 / 反推 / 模拟」，读的人才分得清哪个能重跑、
      // 哪个是他上个月手填的、哪个是编的。
      const tag = f.filled
        ? (f.assumed
            ? '<span class="tag A" title="这不是实测值，是拍的或先占位的">假设值</span>'
            : blOriginTag(f.origin))
        : '<span class="dim" style="font-size:11px">缺</span>';
      const hint = (!f.filled && f.live_hint)
        ? '<div class="dim" style="font-size:11.5px;margin-top:2px">该从哪来：' + esc(f.live_hint) + '</div>'
        : '';
      return '<div class="frow">' +
        '<div class="l">' + esc(f.label) + '</div>' +
        '<div class="v">' + blFieldValue(f) + hint + '</div>' +
        '<div>' + tag + '</div>' +
      '</div>';
    }).join('');
    const spapiN = (g.fields || []).filter(f => f.spapi).length;
    return '<div class="dimcard">' +
      '<div class="hd"><span class="n">' + esc(g.name) + '</span>' +
        '<span class="tag ' + (g.filled === g.total ? 'active' : 'B') + '">' +
          g.filled + '/' + g.total + '</span>' +
        // 「SP-API 可给几格」从字段的来路表直接数出来，不用维度上的老布尔位：
        // 那个位是"能不能用卖家精灵自动补"时代的产物，现在会误导——
        // 它把「库存 SP-API 能给」说成「只能人填」。
        '<span class="dim" style="font-size:11px">' +
          (spapiN ? 'SP-API 可给 ' + spapiN + '/' + g.total : '只能人填') +
        '</span>' +
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
        '<div class="v">' + blFieldValue(f) +
        (!f.filled && f.live_hint
          ? '<div class="dim" style="font-size:11.5px;margin-top:2px">该从哪来：' +
            esc(f.live_hint) + '</div>'
          : '') +
        '</div><div>' +
        (f.filled ? blOriginTag(f.origin)
                  : '<span class="dim" style="font-size:11px">缺</span>') +
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
