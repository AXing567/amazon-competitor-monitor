"""视图②：阈值配置（最核心的一屏）。

## 表单为什么由 schema 驱动

`GET /api/config/thresholds/schema` 返回 `THRESHOLD_SPEC` 里的每一项（中文标签、类型、
默认值、范围、help）。前端**不认识任何字段名**，只负责按 `type` 渲染控件。
所以以后加一个阈值参数，这一屏零改动——这是 `THRESHOLD_SPEC` 作为唯一真相的直接收益。

## 为什么只开两层（公司默认 + 产品覆盖）

个人层（user）在生产里**不生效**：定时采集与飞书推送都是"系统行为，没有自然人"，
所以它们只读 company + product。既然改了不影响推送，那让用户在面板上改就是骗人。
等下一轮做了 per-user 分发，三层一起开。UI 上就写这句话，不藏在注释里。

## 为什么试算必须分等级

QA 实测：把 `price_p0_pct` 从 0.03 收到 0.5，`total` 完全不变——因为价格变化只是从 P0
降级成 P1，信号条数没少。只显示 total 会让人以为"改了没用"，进而放弃调阈值。
所以这一屏永远把 **P0 / P1 / P2 三行分开摆**，并且同时给两个数：
**原始命中数**（L2 判出多少条）与**模拟冷却去重后的命中数**（实际会打扰你几次）。

## 文案为什么写「样本数」而不是「窗口内 N 次对比」

试算为了拿到变化，每个竞品会多算一对基线（窗口前一条 + 窗口内第一条）。
写"对比次数"会被人拿去和"快照数 - 1"核对，然后发现对不上而怀疑算错。
「样本数」是这一屏唯一不会引发这种误会的中性说法。

## 为什么每组下面有个「这段什么意思」折叠区

阈值页最容易犯的错不是算错，而是**用户不知道这串数字意味着什么**：
`price_p1_pct = 0.03` 到底是"降 3% 进日报"还是"降 3% 会推送飞书"。
`explain_thresholds` 就是为这件事存在的，但一个只有 REST / Agent 碰得到的能力，
对"只看面板"的主要用户等于不存在——所以把它挂到每一组下面。

三条设计约束：
- **懒加载**：`<details>` 展开时才请求。三个分组预拉等于每次打开这一屏多打三次接口，
  而多数人只关心自己正在改的那一组。
- **`my_product_id` 有就带**：它决定"当前**实际生效**的是哪一层"，与顶部
  "你要改哪一层"的芯片是两件事，所以不跟 `CFG.scope` 走。
- **`personal_overrides_not_applied` 单独渲染**：后端已经把它塞进 `text` 里了，
  但让人去一段文字里找那句"你设的 3% 没生效"是不现实的——这是这个人在这一屏
  最该看见的一件事，所以必须是一条独立的警示行。
"""
from __future__ import annotations

SECTION = """
<section class="view" id="view-config">
  <div class="view-body">
    <div class="layerbar" id="cfg-layers"></div>
    <div class="note" id="cfg-layer-note"></div>
    <div id="cfg-groups"><div class="empty">加载中…</div></div>
    <div id="cfg-sim"></div>
  </div>
</section>
"""


JS = r"""
const CFG = {schema: null, ctx: null, scope: 'company', eff: null, draft: {}, sim: null};

// GROUP_TITLES（分组中文名）定义在 layout 的核心 JS 里：健康页也要用。

register('config', async () => {
  const [schema, ctx] = await Promise.all([
    api('/api/config/thresholds/schema'),
    api('/api/panel/context')
  ]);
  CFG.schema = schema;
  CFG.ctx = ctx;
  await cfgLoadValues();
  cfgRender();
});

async function cfgLoadValues(){
  const q = CFG.scope === 'product' ? ('?my_product_id=' + encodeURIComponent(CFG.ctx.my_product_id)) : '';
  CFG.eff = await api('/api/config/thresholds' + q);
  CFG.draft = {};
  CFG.sim = null;
}

function cfgScopeId(){
  return CFG.scope === 'product' ? String(CFG.ctx.my_product_id) : '';
}

function cfgRender(){
  // ---- 层选择 ----
  const layers = [
    ['company', '公司默认', '整租户生效，采集与推送都按它跑'],
    ['product', '产品覆盖', '只对这一个自家产品生效，优先级高于公司默认']
  ];
  // 空库/新装时 `ctx.my_product_id` 是 null。这时"产品覆盖"这一层没有对象可覆盖，
  // 点它只会发出 `?my_product_id=null` 然后静默失败。所以先把芯片标灰、说清原因，
  // 并且点它时**主动抛一句人话**（由 act 转成 toast），而不是让它看起来能点。
  const hasProduct = !!(CFG.ctx && CFG.ctx.my_product_id);
  $('#cfg-layers').innerHTML = layers.map(([k, t]) => {
    const off = (k === 'product' && !hasProduct);
    return '<div class="b' + (CFG.scope === k ? ' on' : '') + '" data-scope="' + k + '"' +
      (off ? ' style="opacity:.55;cursor:not-allowed" title="还没有自家产品锚点，产品层无从覆盖"' : '') +
      '>' + t + (off ? '<span class="dim" style="font-size:11px">（先设锚点）</span>' : '') + '</div>';
  }).join('');
  $$('#cfg-layers .b').forEach(el => {
    // 用 act 包一层：作用域切换要重新拉值，拉不到时不能"点了没反应"。
    el.onclick = act(async () => {
      if (el.dataset.scope === 'product' && !hasProduct) {
        throw new ApiError('还没有自家产品锚点，产品层无从覆盖',
          '先在「自家数据」里填一次 ASIN / 售价 / 保本价，再回来改产品层；公司默认层现在就能改。');
      }
      if (CFG.scope === el.dataset.scope) return;
      CFG.scope = el.dataset.scope;
      await cfgLoadValues();
      cfgRender();
    }, '切换作用域');
  });
  $('#cfg-layer-note').innerHTML =
    '本轮<b>只开放两层</b>：公司默认 → 产品覆盖。<b>个人层不开放</b>——' +
    '你的个人覆盖目前只影响"你自己看面板时的生效值"，定时采集与飞书推送仍按公司层跑；' +
    '让你改了却不影响推送，等于骗人，等下一轮做完个人分发再一起开。' +
    (CFG.scope === 'product'
      ? '<br>当前编辑：<code>产品覆盖（自家 ' + esc(CFG.ctx.my_asin || '—') + '）</code>'
      : '<br>当前编辑：<code>公司默认</code>');

  // ---- 各分组表单 ----
  const groups = CFG.schema.groups || [];
  $('#cfg-groups').innerHTML = groups.map(g => {
    const items = g.items || [];
    const rows = items.map(it => cfgFieldRow(g.group, it)).join('');
    return `<h2 class="sec">${esc(GROUP_TITLES[g.group] || g.title || g.group)}</h2>
      <div class="card">
        ${rows}
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
          <button class="btn primary" data-act="save" data-group="${esc(g.group)}">保存这一组</button>
          <button class="btn" data-act="sim" data-group="${esc(g.group)}"
            ${g.group === 'diff' ? '' : 'disabled title="试算只影响变化判定组"'}>试算影响</button>
          <button class="btn" data-act="reset" data-group="${esc(g.group)}">放弃改动</button>
          <button class="btn" data-act="versions" data-group="${esc(g.group)}">版本历史 / 回滚</button>
        </div>
        <details class="exp" data-explain="${esc(g.group)}">
          <summary>这段什么意思？</summary>
          <div id="cfg-expl-${esc(g.group)}"></div>
        </details>
        <div id="cfg-warn-${esc(g.group)}"></div>
        <div id="cfg-ver-${esc(g.group)}"></div>
      </div>`;
  }).join('') || '<div class="empty">没有可配置项。</div>';

  $$('#cfg-groups [data-act]').forEach(btn => {
    // 保存 / 试算 / 版本历史三个动作都走 cfgAction，这里统一兜异常。
    btn.onclick = act(() => cfgAction(btn.dataset.act, btn.dataset.group));
  });
  $$('#cfg-groups details[data-explain]').forEach(d => {
    // 懒加载：只在**展开**时请求。收起时什么都不发。
    // 用 toggle 事件而不是 onclick：点 summary、按回车、键盘空格都能触发，
    // 而 onclick 只覆盖鼠标点击——键盘用户会打不开说明。
    d.addEventListener('toggle', () => {
      if (d.open) cfgExplain(d.dataset.explain);
    });
  });
  $$('#cfg-groups input').forEach(inp => {
    inp.oninput = () => {
      const g = inp.dataset.group, k = inp.dataset.key;
      const orig = CFG.eff.effective[g][k];
      const changed = String(inp.value) !== String(orig);
      inp.classList.toggle('changed', changed);
      CFG.draft[g] = CFG.draft[g] || {};
      if (changed) CFG.draft[g][k] = inp.value; else delete CFG.draft[g][k];
    };
  });
}

function cfgFieldRow(group, it){
  const val = CFG.eff.effective[group][it.key];
  const src = (CFG.eff.sources[group] || {})[it.key] || 'default';
  const srcText = {company:'公司默认', user:'个人覆盖', product:'产品覆盖', default:'系统默认'}[src] || src;
  const step = it.type === 'int' ? '1' : 'any';
  const range = '范围 ' + (it.minimum === null ? '—' : it.minimum) + ' ~ ' +
                (it.maximum === null ? '—' : it.maximum) + (it.unit ? ' ' + it.unit : '');
  return `<div class="field">
    <div class="lab">
      <div class="k">${esc(it.label)}</div>
      <div class="h">${esc(it.help || '')}<br>${esc(range)} · 默认 ${esc(it.default)}</div>
    </div>
    <div>
      <input type="number" step="${step}" data-group="${esc(group)}" data-key="${esc(it.key)}"
             value="${val === null || val === undefined ? '' : esc(val)}">
      <div class="src">当前来自 ${esc(srcText)}</div>
    </div>
  </div>`;
}

// 「这段什么意思」：把 explain_thresholds 翻译好的那几行铺出来。
// 展开才请求，收起不请求；同一个 details 反复展开就反复取——阈值随时可能被别人改，
// 缓存一份只会让这段说明变成"上一次的答案"。
async function cfgExplain(group){
  const out = $('#cfg-expl-' + group);
  if (!out) return;
  // my_product_id 只要有锚点就带：这段说明讲的是「当前**实际生效**的是哪一层」，
  // 与顶部"你要改哪一层"的芯片无关——跟着那个芯片走，会让只看公司层的人
  // 看不到自己产品上的覆盖，说明就成了半截话。
  // （测试里对此有防回退断言：`cfgExplain` 的函数体不许出现那个芯片变量。）
  const pid = CFG.ctx && CFG.ctx.my_product_id;
  let url = '/api/config/thresholds/explain?group_name=' + encodeURIComponent(group);
  if (pid) url += '&my_product_id=' + encodeURIComponent(pid);

  out.innerHTML = '<div class="dim">正在取当前生效的阈值…</div>';
  let res;
  try {
    res = await api(url);
  } catch (e) {
    // 这一块的加载失败不该顶掉整屏（其余表单是好的）：就地说明，再展开一次即重试。
    const hint = (e && e.hint) ? '（' + e.hint + '）' : '';
    out.innerHTML = '<div class="note">这段说明没取到：' +
      esc((e && e.message) ? e.message : String(e)) + esc(hint) +
      '<br>其它区域不受影响；把这段收起再展开可以重试。</div>';
    return;
  }

  // 个人覆盖**单独一条**，不要只指望用户去 text 里发现那句 ⚠。
  // "我设的 3% 没生效"是这个人在这一屏最该看见的一件事。
  const notApplied = res.personal_overrides_not_applied || [];
  const labels = {};
  ((CFG.schema.groups.find(g => g.group === group) || {}).items || []).forEach(it => {
    labels[it.key] = it.label;
  });
  const SRC_TITLE = {company: '公司默认', product: '产品覆盖', user: '个人覆盖', default: '系统默认'};
  const effective = res.effective || {}, sources = res.sources || {};
  const personalWarn = notApplied.length
    ? '<div class="warnbox"><b>⚠ 你有 ' + num(notApplied.length) + ' 项个人覆盖，在本组不生效</b><br>' +
      notApplied.map(o => '· ' + esc(labels[o.key] || o.key) + '：你设的是 <code>' +
        esc(o.value) + '</code>，实际判定用 <code>' + esc(effective[o.key]) + '</code>' +
        '（来自' + esc(SRC_TITLE[sources[o.key]] || '系统默认') + '）').join('<br>') +
      '<br>原因：<code>' + esc(group) + '</code> 这一组对所有人生效的是<b>同一套阈值</b>' +
      '（一轮采集只跑一遍，不可能按人各跑一套），个人层只影响「这条信号要不要推给你」。' +
      '想让你的值真正生效，请改<b>产品层或公司层</b>。</div>'
    : '';

  // 口径一句话：diff / tiering 不含个人层，notify 含——这就是 D5 的裁定。
  const judgmentGroup = (group === 'diff' || group === 'tiering');
  const scopeLine = judgmentGroup
    ? '<div class="note">口径：本组只算<b>公司默认 + 产品覆盖</b>——它对所有人是同一套值，' +
      '个人层不参与判定。</div>'
    : '<div class="note">口径：本组<b>计入个人覆盖</b>——它决定「这条信号要不要推给你」，' +
      '属于个人差异。</div>';

  // 逐行渲染（后端已经排好版），保留空行当分段。
  // 不整段塞进一个 div 靠 white-space 硬撑：窄屏上会变成一大坨没法扫读的文字墙。
  const body = String(res.text || '').split('\n').map(line => line.trim()
    ? '<div style="padding:2px 0;white-space:pre-wrap">' + esc(line) + '</div>'
    : '<div style="height:8px"></div>').join('');

  out.innerHTML = scopeLine + personalWarn + body;
}

async function cfgAction(act, group){
  if (act === 'reset') {
    await cfgLoadValues(); cfgRender(); toast('已放弃改动'); return;
  }
  if (act === 'save') return cfgSave(group);
  if (act === 'sim') return cfgSimulate(group);
  if (act === 'versions') return cfgVersions(group);
}

async function cfgSave(group){
  const values = cfgDraftFor(group);
  if (!Object.keys(values).length) { toast('这一组没有改动'); return; }
  const btn = $('[data-act="save"][data-group="' + group + '"]');
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/config/thresholds', {
      scope: CFG.scope,
      scope_id: cfgScopeId(),
      group_name: group,
      values: values,
      note: '面板修改'
    });
    const warn = (res.warnings || []).length
      ? '<div class="warnbox"><b>保存成功，但要提醒你：</b><br>' +
        res.warnings.map(w => '· ' + esc(w)).join('<br>') + '</div>'
      : '';
    $('#cfg-warn-' + group).innerHTML = warn;
    toast(res.action === 'noop' ? '值与当前一致，未产生新版本'
        : '已保存 v' + res.version + '（改了 ' + (res.changed_keys || []).length + ' 个键）');
    await cfgLoadValues();
    cfgRender();
    if (warn) $('#cfg-warn-' + group).innerHTML = warn;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function cfgDraftFor(group){
  const out = {};
  const d = CFG.draft[group] || {};
  Object.keys(d).forEach(k => {
    const spec = (CFG.schema.groups.find(g => g.group === group).items).find(i => i.key === k);
    const raw = d[k];
    out[k] = (spec && spec.type === 'int') ? parseInt(raw, 10) : parseFloat(raw);
  });
  return out;
}

async function cfgSimulate(group){
  const values = cfgDraftFor(group);
  const changed = Object.keys(values).length;
  const box = $('#cfg-sim');
  box.innerHTML = '<div class="card dim">正在用历史快照跑一遍…</div>';
  const payload = {
    values: {diff: values},
    days: 7,
    my_product_id: CFG.scope === 'product' ? CFG.ctx.my_product_id : null
  };
  const res = await api('/api/config/thresholds/simulate', payload);
  const w = res.window || {};
  const cur = res.current || {}, cand = res.candidate || {}, d = res.delta || {};
  const cool = res.after_cooldown || {current: {}, candidate: {}};

  const sign = v => (v > 0 ? '+' + v : String(v));
  const cls = v => (v > 0 ? 'up' : (v < 0 ? 'down' : 'dim'));
  const row = (label, a, b, delta) => `<div class="simrow">
      <div>${esc(label)}</div>
      <div class="big num">${num(a)}</div>
      <div class="big num">${num(b)}</div>
      <div class="big num ${cls(delta)}">${sign(delta)}</div>
    </div>`;

  // 个人覆盖只提示、不参与：面板试算与定时采集同口径（公司 + 产品两层）。
  // 不写这句的话，用户会以为"我改了个人层，试算怎么没变"——最后去怀疑算法。
  const po = res.personal_overrides || [];
  const personalNote = po.length
    ? '<div class="warnbox"><b>你有 ' + num(po.length) + ' 项个人覆盖，未参与本次试算</b><br>' +
      po.map(p => '· ' + esc(GROUP_TITLES[p.group_name] || p.group_name) + '：' +
        (p.keys || []).map(k => '<code>' + esc(k) + '</code>').join(' ')).join('<br>') +
      '<br>原因：试算与定时采集、飞书推送走同一口径（公司默认 + 产品覆盖），' +
      '个人覆盖本轮只影响"你自己看面板时的生效值"，所以这里刻意不算它。' +
      '要验证个人层效果，等 per-user 分发上线后再看。</div>'
    : '';

  // 试算口径自证：这个数字会被拿去做「要不要改阈值」的决策，所以口径不能靠猜。
  // 正常态永远是「公司层 + 产品层」，含个人层只在 API 显式传 user_id 时出现——
  // 那种结果与生产采集不同口径，必须用警示色标出来，否则用户会拿它预估线上频率。
  const SCOPE_LAYER_TITLE = {
    default: '系统默认', company: '公司层', user: '个人层', product: '产品层'
  };
  const sc = res.scope || {};
  const scLayers = sc.layers || [];
  const scHasUser = scLayers.indexOf('user') >= 0;
  const scTitles = scLayers.map(l => SCOPE_LAYER_TITLE[l] || l).join(' + ');
  const scopeNote = scHasUser
    ? '<div class="warnbox"><b>⚠ 本次试算含个人层覆盖（user #' + num(sc.user_id) + '）</b><br>' +
      '口径：' + esc(scTitles || '——') + '。定时采集与飞书推送<b>永远不含个人层</b>，' +
      '所以这个结果与生产不一致，只能用来单独验证个人覆盖的影响，' +
      '别拿它预估线上的触发频率。</div>'
    : '<div class="note">试算口径：' + esc(scTitles || '公司层 + 产品层') +
      '（无个人层覆盖）' + (sc.note ? '<br>' + esc(sc.note) : '') + '</div>';

  box.innerHTML = `<h2 class="sec">试算结果（${changed} 个键改动，未保存）</h2>
    <div class="card">
      <div class="note">
        样本数 <b>${num(w.pairs)}</b> 对（${num(w.competitors)} 个竞品 / ${num(w.snapshots)} 条快照，
        窗口 ${w.days} 天，季节阶段 <code>${esc(w.phase || '—')}</code>）。
        本屏只做试算，<b>不会写库</b>。
      </div>
      ${scopeNote}
      <div class="note">
        「当前」这一列是拿<b>现在生效的阈值</b>把历史重算一遍，<b>不等于看板上那个数</b>。
        两者条数不同是正常的：看板显示的是<b>当时</b>那条信号被判成的等级，而阈值可能在这之后被改过，
        也可能这条信号是在旧规则下产生的。这是两个时间视角，不是算错。
        想核对某个具体竞品，去它的变化历史里看当时的分级。
      </div>
      <div class="simrow"><div class="head">等级</div><div class="head">当前</div>
        <div class="head">改完</div><div class="head">变化</div></div>
      ${row('P0 立即处理', cur.p0, cand.p0, d.p0)}
      ${row('P1 今日关注', cur.p1, cand.p1, d.p1)}
      ${row('P2 归档', cur.p2, cand.p2, d.p2)}
      ${row('合计', cur.total, cand.total, d.total)}
      <div class="note" style="margin-top:12px">
        <b>原始命中</b>：当前 ${num((res.hit_counts || {}).current_raw)} 条 → 改完
        ${num((res.hit_counts || {}).candidate_raw)} 条（L2 判出来的条数）。<br>
        <b>冷却去重后</b>：当前 ${num((res.hit_counts || {}).current_after_cooldown)} 条 → 改完
        ${num((res.hit_counts || {}).candidate_after_cooldown)} 条
        （P0 ${num((cool.current || {}).p0)}→${num((cool.candidate || {}).p0)}，
         P1 ${num((cool.current || {}).p1)}→${num((cool.candidate || {}).p1)}）。
        这才是<b>实际会打扰你几次</b>——同一条信号在冷却窗口内只推一次。
      </div>
      ${personalNote}
      <div class="note">
        注意：<b>合计不变不代表没影响</b>。价格类变化一旦过了噪声门槛就一定产生信号，
        收紧阈值只会改变它的<b>等级</b>（P0 降成 P1），所以请看上面 P0/P1 两行的变化。
      </div>
      ${(res.samples || []).length ? '<div class="note"><b>几条变了的例子：</b><br>' +
        res.samples.slice(0, 6).map(s =>
          '· ' + esc(s.asin) + ' ' + esc(s.signal_type) + ' ' +
          esc(s.before_level || '（原本没有）') + ' → ' + esc(s.after_level || '（不再产生）') +
          (s.description ? '　' + esc(s.description) : '')
        ).join('<br>') + '</div>' : ''}
    </div>`;
  box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

async function cfgVersions(group){
  const q = new URLSearchParams({
    scope: CFG.scope, scope_id: cfgScopeId(), group_name: group, limit: '20'
  });
  const res = await api('/api/config/thresholds/versions?' + q.toString());
  const items = res.items || [];
  const box = $('#cfg-ver-' + group);
  if (!items.length) { box.innerHTML = '<div class="note">这个作用域还没有版本记录。</div>'; return; }
  box.innerHTML = '<h2 class="sec" style="margin-top:16px">版本历史（新 → 旧）</h2>' +
    '<table><thead><tr><th>版本</th><th>动作</th><th>操作人</th><th>时间</th><th>说明</th><th></th></tr></thead><tbody>' +
    items.map(v => `<tr>
      <td class="num">v${v.version}</td>
      <td>${esc({create:'新建', update:'修改', rollback:'回滚', noop:'无变化'}[v.action] || v.action || '')}</td>
      <td>${esc(v.operator || '—')}</td>
      <td class="dim">${dt(v.created_at)}</td>
      <td>${esc(v.note || '')}</td>
      <td><button class="btn sm" data-rollback="${v.version}" data-group="${esc(group)}">回滚到此版</button></td>
    </tr>`).join('') + '</tbody></table>' +
    '<div class="note">回滚不是覆盖历史：它会<b>追加一条新版本</b>，所以"谁在什么时候把它改回去了"永远有痕迹。</div>';
  $$('#cfg-ver-' + group + ' [data-rollback]').forEach(b => {
    // `disabled` 必须在 finally 里还原：回滚失败时如果按钮一直是灰的，
    // 用户连"再试一次"都做不到，而失败原因只在 toast 里闪 2.6 秒。
    b.onclick = act(async () => {
      b.disabled = true;
      try {
        const r = await api('/api/config/thresholds/rollback', {
          scope: CFG.scope, scope_id: cfgScopeId(), group_name: group,
          version: parseInt(b.dataset.rollback, 10), note: '面板回滚'
        });
        toast('已回滚，新版本 v' + r.new_version);
        await cfgLoadValues();
        cfgRender();
        cfgVersions(group);
      } finally {
        b.disabled = false;
      }
    }, '回滚');
  });
}
"""


def section() -> str:
    return SECTION


def js() -> str:
    return JS
