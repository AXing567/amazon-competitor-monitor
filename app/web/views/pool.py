"""视图③：竞品池管理。

三件事：加、停（`paused`）、归档；外加把**分层理由**摊开给人看。

## 「停」为什么是 paused 而不是隐藏

`paused` = 停止采集、保留历史、面板灰显但仍在列表里。
隐藏会让人以为"这个竞品被删了"，而它其实只是暂时不盯了；
更糟的是下次想恢复时找不到入口。

## 为什么把四同法则逐项摆出来

分层如果只显示一个字母，用户没法判断"判得对不对"。
把 `price_ratio / keyword_overlap / node_match / functional_match` 逐项给出值与是否达标，
用户才能说"这条判错了"——而这句话正是改阈值的依据。
`unknown` 级还要额外说明缺哪几维证据（缺流量词 / 缺类目路径）。
"""
from __future__ import annotations

SECTION = """
<section class="view" id="view-pool">
  <div class="view-body">
    <h2 class="sec">加入竞品</h2>
    <div class="card">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <input id="p-asin" placeholder="竞品 ASIN，如 B0XXXXXXXX"
               style="font:inherit;font-size:13px;padding:7px 10px;border-radius:8px;
                      border:1px solid var(--line);background:var(--card);color:var(--text);width:260px">
        <label class="note" style="margin:0">
          <input type="checkbox" id="p-force"> 强行加入（未过四同法则也加，分层会是「待判定」）
        </label>
        <button class="btn primary" id="p-add">加入</button>
      </div>
      <div class="note">加入时会自动拉一次数据、跑四同法则、建基线快照，并自动建立对标绑定。</div>
      <div id="p-add-msg"></div>
    </div>

    <h2 class="sec">池子</h2>
    <div class="note" id="p-filter"></div>
    <div class="card" id="p-table"><div class="empty">加载中…</div></div>
  </div>
</section>
"""


JS = r"""
let POOL_FILTER = 'active,paused';

register('pool', async () => {
  // 写操作一律走 act：失败时给 toast（带后端 hint），而不是静默无反应。
  $('#p-add').onclick = act(poolAdd, '加竞品');
  await poolLoad();
});

async function poolLoad(){
  const res = await api('/api/panel/pool?status=' + encodeURIComponent(POOL_FILTER));
  const items = res.items || [];

  const chips = [
    ['active', '在盯'], ['paused', '已暂停'], ['archived', '已归档']
  ];
  $('#p-filter').innerHTML = '当前显示：' + chips.map(([k, t]) => {
    const on = POOL_FILTER.split(',').indexOf(k) >= 0;
    return '<button class="btn sm" data-f="' + k + '" style="' +
      (on ? 'border-color:var(--accent);color:var(--accent)' : '') + '">' + t + '</button>';
  }).join(' ') + '　（点一下切换）';
  $$('#p-filter [data-f]').forEach(b => {
    b.onclick = () => {
      const set = POOL_FILTER.split(',').filter(x => x);
      const i = set.indexOf(b.dataset.f);
      if (i >= 0) set.splice(i, 1); else set.push(b.dataset.f);
      if (!set.length) set.push('active');
      POOL_FILTER = set.join(',');
      poolLoad();
    };
  });

  if (!items.length) {
    $('#p-table').innerHTML = '<div class="empty">这个筛选下没有竞品。</div>';
    return;
  }

  $('#p-table').innerHTML = '<table><thead><tr>' +
    '<th>ASIN</th><th>标题</th><th>状态</th><th>分层</th><th>判定理由</th>' +
    '<th>战场</th><th>操作</th></tr></thead><tbody>' +
    items.map(poolRow).join('') + '</tbody></table>';

  $$('#p-table [data-status]').forEach(b => {
    b.onclick = act(() => poolSetStatus(b.dataset.asin, b.dataset.status), '改状态');
  });
}

function poolRow(it){
  const r = it.tier_reasons || {};
  const reasons = [];
  if (r.price_ratio !== undefined && r.price_ratio !== null)
    reasons.push('价格比 ' + Number(r.price_ratio).toFixed(3) + (r.price_band_ok ? ' ✓' : ' ✗'));
  if (r.keyword_overlap !== undefined && r.keyword_overlap !== null)
    reasons.push('词重合 ' + pct(r.keyword_overlap, 0) + (r.keyword_pool_ok ? ' ✓' : ' ✗'));
  if (it.node_match !== undefined && it.node_match !== null)
    reasons.push('节点' + (it.node_match ? '一致 ✓' : '不一致 ✗'));
  if (it.functional_match !== undefined && it.functional_match !== null)
    reasons.push('功能' + (it.functional_match ? '相同 ✓' : '不同 ✗'));
  if (!reasons.length) reasons.push('<span class="dim">暂无判定记录</span>');

  // 缺失维度的说明必须分**三层**，不许把「暂定」与「待判定」混成一句：
  //   暂定（tier_provisional === true）   = 已经有字母档，只是定档所需的维度没测全；
  //   待判定（tier === 'unknown'）        = 能判维度 < 2，连字母档都还没有；
  //   其余                                = 正常结论，这一栏不显示。
  // 混成两层的后果：一个 B 级竞品下面挂一句「待判定是因为缺证据」，
  // 用户会以为分层没做出来，回头又去手工归档——这正是本轮要修掉的那种自相矛盾。
  //
  // 判据只用 API 给的 `tier_provisional` 布尔位，**不**去解析 `tier_label` 里的
  // 「（暂定：缺X）」括号：文案一改解析就静默失效，而失效的表现恰好就是两层混掉。
  const dimZh = {price_ratio: '价格带', keyword_overlap: '流量词重合', node_match: '类目节点'};
  const lack = (r.missing_dims || []).map(d => dimZh[d] || d);
  // 老数据没有 `missing_dims`（判定早于本轮），退回能力层算好的中文缺失项
  const lackText = lack.length ? lack.join('、') : (it.tier_missing || []).join('、');
  const missStyle = 'font-size:11.5px;line-height:1.65;margin-top:4px';
  let miss = '';
  if (it.tier_provisional === true) {
    miss = '<div class="dim" style="' + missStyle + '" title="' +
      esc((it.tier_missing || []).join('；')) + '">' +
      '分层 <b>' + esc(it.tier || '—') + '</b> · 暂定：缺的是' + esc(lackText || '关键维度') +
      '，这一档是按最能兜住的中间档给的。' +
      '<br>补齐之后池子维护会自动改判，不用手工干预。</div>';
  } else if (it.tier === 'unknown') {
    miss = '<div class="dim" style="' + missStyle + '">' +
      '待判定是因为缺证据：' + esc((it.tier_missing || []).join('、')) +
      '<br>数据补齐后池子维护会自动改判，不用手工干预。</div>';
  }

  // 用能力层已经算好的 `battlefield`（与看板同一份结构），
  // 不再单独拉一次绑定列表——两处各算一次，迟早会出现"看板说已绑定、池子说未绑定"。
  const bf = it.battlefield || {};
  const bfHtml = bf.binding_id
    ? esc(bf.my_asin || '—') + '<div class="dim" style="font-size:11px;max-width:220px;line-height:1.5" title="' +
      esc(bf.basis_summary || '') + '">' + esc((bf.basis_summary || '').slice(0, 46)) +
      ((bf.basis_summary || '').length > 46 ? '…' : '') + '</div>'
    : '<span class="dim">未绑定</span>';

  const act = [];
  if (it.status !== 'active')
    act.push('<button class="btn sm" data-status="active" data-asin="' + esc(it.asin) + '">恢复</button>');
  if (it.status !== 'paused')
    act.push('<button class="btn sm" data-status="paused" data-asin="' + esc(it.asin) + '">停</button>');
  if (it.status !== 'archived')
    act.push('<button class="btn sm danger" data-status="archived" data-asin="' + esc(it.asin) + '">归档</button>');

  return `<tr class="row ${esc(it.status || '')}">
    <td><b>${esc(it.asin)}</b></td>
    <td style="max-width:240px">${esc(it.title || '—')}</td>
    <td>${statusTag(it.status)}</td>
    <td>${tierTag(it.tier, it.tier_label)}
        <div class="dim" style="font-size:11px">${it.tier_updated_at ? dt(it.tier_updated_at) : ''}</div>${miss}</td>
    <td style="font-size:12px;line-height:1.8">${reasons.join('<br>')}</td>
    <td>${bfHtml}</td>
    <td style="white-space:nowrap">${act.join(' ')}</td>
  </tr>`;
}

async function poolAdd(){
  const asin = ($('#p-asin').value || '').trim().toUpperCase();
  if (!asin) { toast('先填 ASIN', true); return; }
  $('#p-add').disabled = true;
  $('#p-add-msg').innerHTML = '<div class="note">正在拉数据并判分层，可能要十几秒…</div>';
  try {
    const res = await api('/api/cap/add_competitor', {asin: asin, force: $('#p-force').checked});
    const tier = res.tier || res.effective_tier || '—';
    $('#p-add-msg').innerHTML = '<div class="warnbox"><b>已加入 ' + esc(asin) + '</b><br>' +
      '分层：' + tierTag(tier, res.tier_label) + ' ' +
      (res.decidable_dims !== undefined ? '（可判定维度 ' + res.decidable_dims + '/3）' : '') +
      (res.note ? '<br>' + esc(res.note) : '') + '</div>';
    $('#p-asin').value = '';
    await poolLoad();
  } catch (e) {
    $('#p-add-msg').innerHTML = '<div class="err"><div class="t">没加进去</div><div class="m">' +
      esc(e.message) + '</div>' + (e.hint ? '<div class="h">建议：' + esc(e.hint) + '</div>' : '') + '</div>';
  } finally {
    $('#p-add').disabled = false;
  }
}

async function poolSetStatus(asin, status){
  const res = await api('/api/panel/pool/' + encodeURIComponent(asin) + '/status', {status: status});
  toast(asin + '：' + (res.effect || status));
  await poolLoad();
}
"""


def section() -> str:
    return SECTION


def js() -> str:
    return JS
