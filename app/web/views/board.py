"""视图①：数据看板。

面板的主屏。回答三个问题：**我在盯谁、他们最近怎么了、哪些还没判定**。

## 为什么「待判定」必须解释缺什么

`unknown` 级是止血补丁引入的正常状态（证据不足 → 不判），不是错误。
但如果面板只显示「待判定」三个字，用户第一反应是"系统坏了"。
所以这一屏会把 `tier_missing`（缺流量词 / 缺类目路径）直接摊开写，
让人知道"等数据补齐就会自动判"——这是把内部状态翻译成用户能行动的句子。

## 为什么四张数字卡各自容错

「数据健康度」那张卡依赖 `/api/panel/health`。如果健康接口挂了，
不能让整屏跟着白屏——恰恰相反，数据源出问题时用户最需要看的就是这一屏。
所以那一张卡单独 catch，失败就显示「—」+ 一句原因，其它卡照常。
"""
from __future__ import annotations

SECTION = """
<section class="view" id="view-board">
  <div class="view-body">
    <div class="grid4">
      <div class="stat"><div class="k">竞品总数</div>
        <div class="v" id="b-total">—</div><div class="n" id="b-total-n"></div></div>
      <div class="stat"><div class="k">近 7 天变化</div>
        <div class="v" id="b-week">—</div><div class="n" id="b-week-n">P0 / P1 / P2</div></div>
      <div class="stat"><div class="k">待处理（24h P0+P1）</div>
        <div class="v" id="b-todo">—</div><div class="n" id="b-todo-n"></div></div>
      <div class="stat"><div class="k">采集成功率（7 天）</div>
        <div class="v" id="b-health">—</div><div class="n" id="b-health-n">加载中…</div></div>
    </div>

    <h2 class="sec">竞品</h2>
    <div class="card" id="b-table"><div class="empty">加载中…</div></div>
    <div class="note">点任意一行展开近期变化（快照 / 信号 / 动作明细）。</div>
  </div>
</section>
"""


JS = r"""
register('board', async () => {
  // 健康度卡片单独容错：它挂了不影响这一屏的其它内容
  const healthP = api('/api/panel/health?days=7').catch(e => ({__err: e}));
  const board = await api('/api/panel/board');
  const health = await healthP;

  const s = board.summary || {};
  $('#b-total').textContent = num(s.total);
  const bt = s.by_tier || {};
  $('#b-total-n').textContent =
    ['S','A','B','C'].map(k => k + ' ' + (bt[k] || 0)).join(' · ')
    + (bt.unknown ? ' · 待判定 ' + bt.unknown : '');

  $('#b-week').textContent = num(s.week_changes);
  if (s.week_by_level) {
    $('#b-week-n').textContent = 'P0 ' + num(s.week_by_level.P0) +
      ' · P1 ' + num(s.week_by_level.P1) + ' · P2 ' + num(s.week_by_level.P2);
  }
  $('#b-todo').textContent = num((s.p0_24h || 0) + (s.p1_24h || 0));
  $('#b-todo-n').textContent = 'P0 ' + num(s.p0_24h) + ' · P1 ' + num(s.p1_24h) +
    (s.undecided ? '　·　待判定竞品 ' + s.undecided + ' 个' : '');

  if (health && health.__err) {
    $('#b-health').textContent = '—';
    $('#b-health-n').textContent = '健康接口没返回：' + health.__err.message;
  } else {
    const r = (health && health.rounds) || {};
    $('#b-health').textContent = (r.success_rate === null || r.success_rate === undefined)
      ? '—' : pct(r.success_rate);
    $('#b-health-n').textContent = '共 ' + num(r.total) + ' 轮，失败 ' + num(r.failed) + ' 轮';
  }

  const items = board.items || [];
  if (!items.length) {
    $('#b-table').innerHTML = '<div class="empty">池子里还没有竞品。' +
      '<br>去「竞品池」加一个，或在飞书里说「盯一下 B0XXXXXXXX」。</div>';
    return;
  }

  const rows = items.map(it => {
    const bf = it.battlefield || {};
    const lc = it.last_change;
    const miss = (it.tier_missing || []).length
      ? '<div class="dim" style="font-size:11.5px;line-height:1.6">缺：' +
        esc((it.tier_missing || []).join('、')) + '</div>'
      : '';
    // 暂停采集的竞品必须自己说出来。
    // 不标的话，它会安静地待在看板里、last_checked_at 越来越旧，而「最近变化」那一栏
    // 显示「近 7 天无 P0/P1」——用户读到的是「对手没动静」，实际是「我们没在采」。
    // 这两件事在业务上完全相反（一个是安全，一个是瞎），所以宁可整行压暗也要说清。
    const paused = it.status === 'paused';
    // 「战场」列必须按 `binding_id` 判断有没有绑定，不能按 `my_asin` 有没有值。
    // `battlefield.my_asin` 取的是租户的自家 ASIN——只要配过一次锚点，它对**每个**竞品
    // 都有值，哪怕这个竞品根本没绑定。按它判断会让 7 行里 5 行显示"已绑定"，
    // 而真实绑定数其实是 2。看板首页说谎比少一行数据严重得多。
    // 池子视图（pool.py）用的就是 binding_id，两处必须同一个判据。
    return `<tr class="row" data-asin="${esc(it.asin)}"${paused ? ' style="opacity:.62"' : ''}>
      <td><b>${esc(it.asin)}</b>${paused ? ' <span class="tag paused">已暂停</span>' : ''}<div class="dim" style="font-size:11.5px">${esc(it.brand || '')}</div></td>
      <td style="max-width:260px">${esc(it.title || '—')}</td>
      <td class="num">${money(it.net_price)}${it.price && it.net_price && it.price !== it.net_price
        ? '<div class="dim" style="font-size:11.5px">标价 ' + money(it.price) + '</div>' : ''}</td>
      <td class="num">${it.rank_sub ? num(it.rank_sub) : '—'}</td>
      <td>${lc ? '<span class="lvl ' + esc(lc.level) + '">' + esc(lc.level) + '</span> ' +
            '<span style="font-size:12.5px">' + esc(lc.signal_type || '') + '</span>' +
            '<div class="dim" style="font-size:11.5px">' + ago(lc.at) + '</div>'
          : (paused
              ? '<span class="dim">已暂停采集 · 最后 ' + ago(it.last_checked_at) + '</span>'
              : '<span class="dim">近 7 天无 P0/P1</span>')}</td>
      <td>${tierTag(it.tier, it.tier_label)}${miss}</td>
      <td>${bf.binding_id ? esc(bf.my_asin || '—') : '<span class="dim">未绑定</span>'}
          ${bf.basis_summary ? '<div class="dim" style="font-size:11px" title="' +
            esc(bf.basis_summary) + '">见依据</div>' : ''}</td>
      <td class="dim">${ago(it.last_checked_at)}</td>
    </tr>
    <tr class="detail" data-for="${esc(it.asin)}" style="display:none"><td colspan="8"></td></tr>`;
  }).join('');

  $('#b-table').innerHTML = `<table>
    <thead><tr>
      <th>ASIN</th><th>标题</th><th>净价</th><th>子类排名</th>
      <th>最近变化</th><th>分层</th><th>战场（自家产品）</th><th>最近采集</th>
    </tr></thead>
    <tbody>${rows}</tbody></table>`;

  $$('#b-table tr.row').forEach(tr => {
    tr.style.cursor = 'pointer';
    tr.onclick = () => toggleDetail(tr.dataset.asin);
  });
});

async function toggleDetail(asin){
  const d = $('tr.detail[data-for="' + asin + '"]');
  if (!d) return;
  if (d.style.display !== 'none') { d.style.display = 'none'; return; }
  d.style.display = '';
  const cell = d.firstElementChild;
  if (cell.dataset.loaded) return;
  cell.innerHTML = '<div class="sub dim">加载中…</div>';
  try {
    const res = await api('/api/panel/competitors/' + encodeURIComponent(asin) + '/changes?days=30');
    cell.dataset.loaded = '1';
    cell.innerHTML = renderChanges(res);
  } catch (e) {
    cell.innerHTML = '<div class="err"><div class="t">这个竞品的变化历史没加载出来</div>' +
      '<div class="m">' + esc(e.message) + '</div>' +
      (e.hint ? '<div class="h">建议：' + esc(e.hint) + '</div>' : '') + '</div>';
  }
}

function renderChanges(res){
  const snaps = (res.snapshots || []).slice(0, 30);
  const sigs = res.signals || [];
  const acts = res.actions || [];

  const snapRows = snaps.map(x => `<tr>
    <td class="dim">${dt(x.at)}</td>
    <td class="num">${money(x.net_price)}</td>
    <td class="num dim">${money(x.price)}</td>
    <td class="num">${x.rank ? num(x.rank) : '—'}</td>
    <td class="num">${x.rating === null || x.rating === undefined ? '—' : x.rating}★</td>
    <td class="num">${x.reviews ? num(x.reviews) : '—'}</td>
    <td>${x.in_stock === false ? '<span class="tag paused">断货</span>' : '在售'}</td>
    <td class="dim">${esc(x.data_quality || '')}</td>
  </tr>`).join('');

  const sigRows = sigs.map(x => `<tr>
    <td class="dim">${dt(x.at)}</td>
    <td><span class="lvl ${esc(x.level)}">${esc(x.level)}</span></td>
    <td>${esc(x.signal_type || '')}</td>
    <td>${esc(x.description || '')}</td>
  </tr>`).join('');

  const actRows = acts.map(x => `<tr>
    <td class="dim">${dt(x.at)}</td>
    <td>${esc(x.user || '—')}</td>
    <td>${esc(x.action_type || '')}</td>
    <td>${esc(x.description || '')}<div class="dim" style="font-size:11.5px">${esc(x.reason || '')}</div></td>
  </tr>`).join('');

  return `<div class="sub">
    <h2 class="sec" style="margin-top:6px">快照（${snaps.length} 条）</h2>
    ${snaps.length ? `<table><thead><tr><th>时间</th><th>净价</th><th>标价</th><th>排名</th>
      <th>星级</th><th>评论</th><th>库存</th><th>质量</th></tr></thead><tbody>${snapRows}</tbody></table>`
      : '<div class="empty">这个窗口内没有快照。</div>'}
    <h2 class="sec">信号（${sigs.length} 条）</h2>
    ${sigs.length ? `<table><thead><tr><th>时间</th><th>级别</th><th>类型</th><th>说明</th></tr></thead>
      <tbody>${sigRows}</tbody></table>` : '<div class="empty">没有信号。</div>'}
    <h2 class="sec">动作（${acts.length} 条）</h2>
    ${acts.length ? `<table><thead><tr><th>时间</th><th>谁</th><th>类型</th><th>做了什么 / 依据</th></tr></thead>
      <tbody>${actRows}</tbody></table>` : '<div class="empty">还没记过动作。</div>'}
  </div>`;
}
"""


def section() -> str:
    return SECTION


def js() -> str:
    return JS
