"""视图④：动作记录 + 复盘闭环。

## 这一屏解决的两个问题

**上半屏：到期了没人回看。**
`actions.verify_due_at` 从这一版之前就在填，但到期之后**没有任何东西提醒**——
系统记了「7 天后回看」，然后 7 天后什么也没发生。
于是「动作 → 结果」这一段永远是断的：库里躺着一堆承诺过要复盘的记录，
没有人回来填结论，半年后你无法回答「跟价到底有没有用」。

**下半屏：动作记下来就散了。**
真正记动作的地方是飞书卡片上的按钮：信号推过来，你点「跟价」，系统自动落库，
还带上 `signal_id`（于是"你当时为什么这么做"永远有据可查）。

但没有卡片就不能记动作的话，遇到「老板在电话里说『这个我们先不动』」这种场景，
这条决策就彻底丢了——而它恰恰是 7 天后要复盘的那条。
所以下半屏是**兜底入口**：手工补一条，来源记成 `panel`，
与卡片来的记录在列表里一眼可分（`source` 列）。两者写的是同一张表、同一套校验。

## 为什么复盘卡只摆数、不给结论

`changes` 里的每一行都是「动作前 → 现在」，差值**不上色**。
红涨绿跌是行情语义；这里是「对手降价 10%」——对要抢份额的人是机会，
对刚备完货的人是威胁。同一个数，好坏完全相反。
染色等于系统替人下了判断，而判定权归人（见 `action_log.diff_metrics`）。

## 为什么「当时的判断」是个按钮而不是直接展开

一次日报有十几条判断，每条都有理由和中间量。全展开会让这一屏变成
一份读不完的文档，而复盘的人 90% 的时候只想看「他降了多少、我销量变了没」。
需要追问「凭什么」的时候再点——那时候他在找一个具体答案，愿意读长文。
"""
from __future__ import annotations

SECTION = """
<section class="view" id="view-actions">
  <div class="view-body">

    <h2 class="sec">待复盘 <span class="dim" id="r-count"></span></h2>
    <div class="note" style="margin-top:-4px">
      动作记下来时填了「几天后回看」的，到期会出现在这里。
      系统只把<b>动作前后的指标</b>并排放好，有效/无效由人判——
      同一个「对手降价 10%」，对不同策略是好是坏完全相反，机器判不了。
    </div>
    <div id="r-list"><div class="empty">加载中…</div></div>

    <h2 class="sec">补记一条动作</h2>
    <div class="warnbox">
      <b>这是兜底入口。</b>主力入口是飞书卡片上的按钮——信号推过来时点一下，
      系统会自动带上信号 ID 与来源，不需要你回来填表。<br>
      手工记录的来源会标成 <code>panel</code>，和卡片来的（<code>feishu_card</code>）区分开，
      复盘时能看出"这条是人后补的"。
    </div>
    <div class="card">
      <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 16px">
        <div>
          <div class="note" style="margin:0 0 4px">竞品 ASIN（可留空 = 与竞品无关的动作，如调自有广告）</div>
          <input id="a-asin" placeholder="B0XXXXXXXX"
                 style="font:inherit;font-size:13px;width:100%;padding:7px 10px;border-radius:8px;
                        border:1px solid var(--line);background:var(--card);color:var(--text)">
        </div>
        <div>
          <div class="note" style="margin:0 0 4px">动作类型（可选下面的，也可以自己写）</div>
          <input id="a-type" list="a-types" placeholder="price_follow / price_hold / stock_alert …"
                 style="font:inherit;font-size:13px;width:100%;padding:7px 10px;border-radius:8px;
                        border:1px solid var(--line);background:var(--card);color:var(--text)">
          <datalist id="a-types"></datalist>
        </div>
        <div style="grid-column:1/-1">
          <div class="note" style="margin:0 0 4px">做了什么 / 依据是什么（这条最重要，7 天后复盘全靠它）</div>
          <input id="a-reason" placeholder="例：他降到 19.99 但已断货，不跟价，改为抢广告位"
                 style="font:inherit;font-size:13px;width:100%;padding:7px 10px;border-radius:8px;
                        border:1px solid var(--line);background:var(--card);color:var(--text)">
        </div>
        <div style="grid-column:1/-1">
          <div class="note" style="margin:0 0 4px">补充说明（可选）</div>
          <input id="a-desc" placeholder="例：已把 PAT 竞价提到 0.85"
                 style="font:inherit;font-size:13px;width:100%;padding:7px 10px;border-radius:8px;
                        border:1px solid var(--line);background:var(--card);color:var(--text)">
        </div>
        <div>
          <div class="note" style="margin:0 0 4px">几天后回看效果（留空 = 不设验证日期）</div>
          <input id="a-verify" type="number" min="1" max="90" value="7"
                 style="font:inherit;font-size:13px;width:140px;padding:7px 10px;border-radius:8px;
                        border:1px solid var(--line);background:var(--card);color:var(--text)">
        </div>
      </div>
      <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
        <button class="btn primary" id="a-add">记下来</button>
        <span class="note" style="margin:0">这个日期只用于提醒你回来复盘，不影响任何判定。</span>
      </div>
      <div id="a-msg"></div>
    </div>

    <h2 class="sec">动作时间线</h2>
    <div class="note" id="a-filter"></div>
    <div class="card" id="a-table"><div class="empty">加载中…</div></div>
  </div>
</section>
"""


JS = r"""
const ACT = {days: 30, asin: '', mine: true};

// 常用动作类型。库里允许自由字符串，这里只是「好填」用的候选。
const ACT_TYPES = [
  ['price_follow', '跟价'], ['price_hold', '不跟价（守住）'], ['price_raise', '提价'],
  ['price_cut', '降价'], ['stock_alert', '备货 / 补货'], ['listing_optimize', '改 listing'],
  ['coupon_test', '加券 / 试券'], ['ad_shift', '调广告'], ['no_action', '观察不动']
];
const ACT_LABEL = Object.fromEntries(ACT_TYPES);

// 判断中间量的中文名。只覆盖系统会算出来的那些键；
// 没收录的照原样显示英文 key——**不要**猜着翻译，翻错比不翻更误导。
const DEC_METRIC_LABEL = {
  current_price: '我的现价', new_price: '跟到的新价',
  current_unit_profit: '现在单均利润', new_unit_profit: '降价后单均利润',
  base_daily_profit: '现在日利润', current_daily_units: '现在日销',
  break_even_units: '盈亏平衡日销', required_growth_pct: '需要涨幅',
  viable: '可行', strategy: '策略', note: '说明',
  clearance_score: '清仓得分', hits: '命中信号', unknowns: '未采集到'
};

// 中间量的值类型。**必须和 DEC_METRIC_LABEL 一起维护**：
// 只给名字不给类型，前端就只能 `String(v)`，于是 `594.76` 显示成 594.76、
// `40` 显示成 40 ——看起来像"系统算出来的原始数字"，而人读到的是"594.76 什么"。
//
// `pct_raw` 和 `percent` 是两回事：`percent` 是 0~1 的比例（0.28 → 28%），
// `pct_raw` 已经是百分数（40 → 40%）。混用会让 40% 显示成 4000%。
const DEC_METRIC_KIND = {
  current_price: 'money', new_price: 'money',
  current_unit_profit: 'money', new_unit_profit: 'money',
  base_daily_profit: 'money',
  current_daily_units: 'float1', break_even_units: 'int',
  required_growth_pct: 'pct_raw', clearance_score: 'int',
  viable: 'bool'
};

const VERDICT_COLOR = {effective: 'var(--ok)', ineffective: 'var(--p0)', inconclusive: 'var(--muted)'};

register('actions', async () => {
  $('#a-types').innerHTML = ACT_TYPES.map(([v, t]) =>
    '<option value="' + v + '">' + t + '</option>').join('');
  $('#a-add').onclick = actSubmit;
  // 两块并行加载：复盘队列和动作时间线互不依赖，
  // 串行会让这一屏的等待时间变成两倍，而其中一块通常几毫秒就回来了。
  await Promise.all([rvLoad(), actLoad()]);
});

// ============================================================
// 复盘队列
// ============================================================

async function rvLoad(){
  const res = await api('/api/panel/reviews?limit=50');
  rvRender(res);
}

function rvRender(res){
  const items = (res && res.items) || [];
  const n = items.length;
  const over = (res && res.overdue) || 0;
  $('#r-count').textContent = n
    ? '（' + n + ' 条' + (over ? '，其中 ' + over + ' 条已超期' : '') + '）'
    : '';

  if (!n) {
    $('#r-list').innerHTML = '<div class="card"><div class="empty">' +
      '没有到期的复盘任务。<br>' +
      '这不是"系统没数据"——是确实没有动作承诺过要回看。' +
      '记动作时填了「几天后回看」，到期后才会出现在这里。</div></div>';
    return;
  }

  $('#r-list').innerHTML = items.map(rvCard).join('');
  $$('#r-list .rv').forEach(el => {
    $$('[data-v]', el).forEach(b => { b.onclick = () => rvSubmit(el, b.dataset.v); });
    const dec = $('[data-dec]', el);
    if (dec) dec.onclick = () => rvDecision(dec);
  });
}

function rvCard(it){
  const over = (it.overdue_days || 0) > 0;
  const dueText = over ? ('超期 ' + it.overdue_days + ' 天') : '今天到期';
  return '<div class="rv' + (over ? ' over' : '') + '" data-id="' + it.id + '">'
    + '<div class="h">'
    +   '<span class="ttl">' + esc(it.asin || '无竞品') + ' · ' + esc(it.action_label || it.action_type) + '</span>'
    +   '<span class="tag ' + (over ? 'unknown' : 'paused') + '">' + dueText + '</span>'
    +   '<span class="dim" style="font-size:12px">' + esc(it.user || '') + ' · ' + dt(it.at) + '</span>'
    + '</div>'
    + (it.reason ? '<div class="why">依据：' + esc(it.reason) + '</div>' : '')
    + rvChanges(it.comparison, it.changes)
    + (it.decision_id
        ? '<div style="margin-top:9px"><button class="btn sm" data-dec="' + it.decision_id + '">'
          + '当时的判断 #' + it.decision_id + '</button></div>'
          + '<div class="dec"></div>'
        : '<div class="dim" style="font-size:11.5px;margin-top:9px">'
          + '这条动作没挂信号，所以没有当时的系统判断可看（人工补记的动作通常如此）</div>')
    + '<div class="acts">'
    +   '<button class="btn sm" data-v="effective">有效</button>'
    +   '<button class="btn sm" data-v="ineffective">无效</button>'
    +   '<button class="btn sm" data-v="inconclusive">判不了</button>'
    +   '<input placeholder="为什么这么判（选填，但它比结论值钱）" data-note>'
    + '</div>'
    + '<div class="done"></div>'
    + '</div>';
}

function rvChanges(cmp, changes){
  cmp = cmp || {};
  changes = changes || cmp.changes || [];
  const stale = cmp.mine_stale
    ? '<div class="dim" style="font-size:11.5px;margin-top:4px">'
      + '自家锚点自那次动作以来没更新过 —— 「我的」那几个数不能当证据（是没更新，不是没变化）。</div>'
    : '';

  if (cmp.state === 'no_data' || (!changes.length && !cmp.state)) {
    // 这一句不能省。空着会被读成「没变化 → 动作无效」，
    // 而实际更常见的原因是「这几天没采到」或「锚点一直没填」。
    return '<div class="dim" style="font-size:12px;margin:6px 0;line-height:1.75">'
      + '没有可比的前后数据——可能这几天没采到，也可能锚点一直没人更新。'
      + '<b>这是缺数据，不是「没变化」。</b></div>' + stale;
  }

  if (!changes.length) {
    // 有数据、但一点没动。这本身是个结论：动作之后指标没反应。
    // 和「没数据」必须分开说，否则两种截然不同的处境会长得一模一样。
    return '<div class="dim" style="font-size:12px;margin:6px 0">'
      + '动作前后指标<b>没有任何变化</b>——这本身是个结论，不是缺数据。</div>' + stale;
  }

  return changes.slice(0, 6).map(ch => {
    let d = '';
    if (ch.delta_pct !== null && ch.delta_pct !== undefined && Math.abs(ch.delta_pct) >= 0.1) {
      d = (ch.delta_pct > 0 ? '+' : '') + ch.delta_pct + '%';
    } else if (ch.delta !== null && ch.delta !== undefined && ch.delta !== 0) {
      d = (ch.delta > 0 ? '+' : '') + num(ch.delta, 2);
    }
    return '<div class="chg">'
      + '<span class="k">' + esc(ch.label) + '</span>'
      + '<b>' + kindVal(ch.before, ch.kind) + '</b>'
      + '<span class="arrow">→</span>'
      + '<b>' + kindVal(ch.after, ch.kind) + '</b>'
      + '<span class="delta">' + (d || '—') + '</span>'
      + '</div>';
  }).join('') + stale;
}

async function rvSubmit(el, verdict){
  const id = parseInt(el.dataset.id, 10);
  const noteEl = $('[data-note]', el);
  const note = noteEl ? (noteEl.value || '').trim() : '';
  const btns = $$('[data-v]', el);
  btns.forEach(b => { b.disabled = true; });
  try {
    const res = await api('/api/panel/reviews', {
      action_id: id, verdict: verdict, note: note || null
    });
    const done = $('.done', el);
    done.innerHTML = '已记下：<b style="color:' + (VERDICT_COLOR[res.verdict] || 'var(--text)') + '">'
      + esc(res.verdict_label || verdict) + '</b>'
      + (res.reviewed_at ? `<span class="dim">　${dt(res.reviewed_at)}</span>` : '');
    // 不立刻删卡片：先让「已记下」露一下脸，人才能确认自己点对了。
    // 立刻消失会让人怀疑是不是没点上，然后去时间线里找——那一步纯属白费。
    setTimeout(() => {
      el.style.transition = 'opacity .25s';
      el.style.opacity = '0';
      setTimeout(() => { el.remove(); rvRecount(); }, 260);
    }, 900);
    await actLoad();      // 时间线里的「复盘」列要跟着变
  } catch (e) {
    btns.forEach(b => { b.disabled = false; });
    $('.done', el).innerHTML = '<span style="color:var(--p0)">没记上：' + esc(e.message) + '</span>';
  }
}

function rvRecount(){
  const left = $$('#r-list .rv').length;
  const over = $$('#r-list .rv.over').length;
  $('#r-count').textContent = left
    ? '（' + left + ' 条' + (over ? '，其中 ' + over + ' 条已超期' : '') + '）'
    : '';
  if (!left) rvLoad();   // 清空了就重新拉一次，把"没有到期任务"那句摆出来
}

async function rvDecision(btn){
  const box = btn.closest('.rv').querySelector('.dec');
  if (box.classList.contains('on')) { box.classList.remove('on'); return; }
  box.innerHTML = '加载中…';
  box.classList.add('on');
  try {
    const d = await api('/api/cap/get_decision', {decision_id: parseInt(btn.dataset.dec, 10)});
    const reasons = (d.reasons || []).map(r => '· ' + esc(r)).join('<br>');
    const metrics = decMetrics(d.metrics);
    box.innerHTML =
        '<b>' + esc(d.headline || '（当时没有给出一句话结论）') + '</b>'
      + (reasons ? '<div class="lab">为什么</div>' + reasons : '')
      + (metrics ? '<div class="lab">当时算出来的</div>' + metrics : '')
      + '<div class="lab">确定性 ' + (d.confidence === null || d.confidence === undefined
            ? '—' : Math.round(d.confidence * 100) + '%')
      + ' · ' + esc(d.origin_label || '') + ' · ' + dt(d.at) + '</div>'
      + ((d.actions || []).length
          ? '<div class="lab">当时建议的下一步</div>'
            + d.actions.map(a => '· ' + esc(a)).join('<br>')
          : '');
  } catch (e) {
    box.innerHTML = '<span style="color:var(--p0)">' + esc(e.message) + '</span>';
  }
}

// 判断中间量拍平成两列。只展开一层：`break_even` 这种嵌套字典是全部，
// 再深的层级目前不存在，写个递归只会让人以为以后会有。
function decMetrics(m){
  if (!m || !Object.keys(m).length) return '';
  const rows = [];
  Object.keys(m).forEach(k => {
    const v = m[k];
    if (v === null || v === undefined) return;
    if (typeof v === 'object' && !Array.isArray(v)) {
      Object.keys(v).forEach(k2 => {
        const v2 = v[k2];
        if (v2 === null || v2 === undefined || typeof v2 === 'object') return;
        rows.push([DEC_METRIC_LABEL[k2] || k2, decLeaf(v2, DEC_METRIC_KIND[k2])]);
      });
      return;
    }
    rows.push([DEC_METRIC_LABEL[k] || k, Array.isArray(v) ? v.join('、') : decLeaf(v, DEC_METRIC_KIND[k])]);
  });
  if (!rows.length) return '';
  return '<div class="kv" style="margin-top:4px">'
    + rows.map(([k, v]) => '<span class="k">' + esc(k) + '</span><span class="num">' + esc(v) + '</span>').join('')
    + '</div>';
}

// 按类型格式化一个中间量。**不要**退回 `String(v)`：
// 那会让「$5.00」「40%」「119 单」变成「5」「40」「119」，
// 而读者拿到的是一串无单位数字，只能靠 label 猜单位。
function decLeaf(v, kind){
  if (kind === 'bool' || typeof v === 'boolean') return v ? '是' : '否';
  if (kind === 'money') return '$' + Number(v).toFixed(2);
  if (kind === 'pct_raw') return num(v, 1) + '%';
  if (kind === 'float1') return num(v, 1);
  if (kind === 'int') return num(v, 0);
  if (typeof v === 'number') return num(v, 2);
  return String(v);
}

// ============================================================
// 动作时间线
// ============================================================

async function actLoad(){
  const chips = [[7, '近 7 天'], [30, '近 30 天'], [90, '近 90 天']];
  $('#a-filter').innerHTML = '时间范围：' + chips.map(([d, t]) =>
    '<button class="btn sm" data-d="' + d + '" style="' +
    (ACT.days === d ? 'border-color:var(--accent);color:var(--accent)' : '') + '">' + t + '</button>'
  ).join(' ') + '　'
  + [[true, '只看我'], [false, '全团队']].map(([m, t]) =>
      '<button class="btn sm" data-mine="' + m + '" style="' +
      (ACT.mine === m ? 'border-color:var(--accent);color:var(--accent)' : '') + '">' + t + '</button>'
    ).join(' ') + '　'
  + (ACT.asin ? '<button class="btn sm" id="a-clear">只看 ' + esc(ACT.asin) + ' ✕</button>' : '');
  $$('#a-filter [data-d]').forEach(b => {
    b.onclick = () => { ACT.days = parseInt(b.dataset.d, 10); actLoad(); };
  });
  // 「只看我 / 全团队」是**显式**切换，不是默认全团队：
  // 默认全团队时人会把别人的记录当成自己的，然后重复跟一次价——
  // 而这正是动作记录这一屏要防止的事。
  $$('#a-filter [data-mine]').forEach(b => {
    b.onclick = () => { ACT.mine = (b.dataset.mine === 'true'); actLoad(); };
  });
  const clr = $('#a-clear');
  if (clr) clr.onclick = () => { ACT.asin = ''; actLoad(); };

  const q = new URLSearchParams({days: String(ACT.days), limit: '200'});
  if (!ACT.mine) q.set('user_id', 'all');
  if (ACT.asin) q.set('asin', ACT.asin);
  const res = await api('/api/panel/actions?' + q.toString());
  const items = res.items || [];

  if (!items.length) {
    $('#a-table').innerHTML = '<div class="empty">这个范围内还没有动作记录。' +
      '<br>这不算异常——没人动过，也是一种结论。' +
      '<br>信号推来时在飞书卡片上点一下就会自动落在这里。</div>';
    return;
  }

  const rows = items.map(x => {
    // 超期天数由服务端算（`overdue_days`），前端不再自己比时间。
    // 两边各算一次的话，浏览器时区/时钟偏差会让人看到「面板说超期 1 天、
    // 复盘卡说 0 天」这种自相矛盾——而没人能判断该信哪个。
    const od = x.overdue_days;
    const odText = (od === null || od === undefined)
      ? ''
      : '<div style="font-size:11px;color:var(--p1)">'
        + (od > 0 ? '超期 ' + od + ' 天' : '今天到期') + '</div>';
    return `<tr class="row">
      <td class="dim" style="white-space:nowrap">${dt(x.at)}</td>
      <td>${esc(x.user || '—')}</td>
      <td>${x.asin ? '<b>' + esc(x.asin) + '</b>' : '<span class="dim">—</span>'}</td>
      <td style="white-space:nowrap">${esc(x.action_label || x.action_type || '—')}
          <div class="dim" style="font-size:11px">${esc(x.action_type || '')}</div></td>
      <td style="max-width:320px">${esc(x.reason || '')}
          ${x.description ? '<div class="dim" style="font-size:11.5px">' + esc(x.description) + '</div>' : ''}</td>
      <td class="dim" style="white-space:nowrap">${esc(x.source_label || x.source || '')}
          ${x.signal_id ? '<div style="font-size:11px">信号 #' + x.signal_id + '</div>' : ''}</td>
      <td style="white-space:nowrap">${x.verify_due_at ? dt(x.verify_due_at) : '—'}${odText}</td>
      <td style="white-space:nowrap">${rvVerdictCell(x)}</td>
    </tr>`;
  }).join('');

  $('#a-table').innerHTML = `<table>
    <thead><tr><th>时间</th><th>谁</th><th>竞品</th><th>类型</th>
      <th>依据 / 说明</th><th>来源</th><th>验证日期</th><th>复盘</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <div class="note">${ACT.mine ? '当前只看<b>你自己</b>记的动作（切到「全团队」看别人的）'
      : '当前看<b>全团队</b>的动作——注意别把别人已经做过的再重复做一次'}。
      共 ${num(items.length)} 条。「依据」为空的历史记录会显示成
      <code>（未填写依据）</code>——那是当时就没写，不是这一屏丢了数据。</div>`;

  $$('#a-table tr.row').forEach((tr, i) => {
    const asin = items[i].asin;
    if (!asin) return;
    tr.style.cursor = 'pointer';
    tr.title = '只看这个竞品的动作';
    tr.onclick = () => { ACT.asin = asin; actLoad(); };
  });
}

// 「复盘」列。没到期的写「未到期」，不是「待复盘」——
// 两者混起来会让人以为现在就该处理，然后天天看见一堆假待办。
function rvVerdictCell(x){
  if (x.verdict) {
    const c = VERDICT_COLOR[x.verdict] || 'var(--text)';
    return '<b style="color:' + c + '">' + esc(x.verdict_label || x.verdict) + '</b>'
      + (x.reviewed_at ? '<div class="dim" style="font-size:11px">' + dt(x.reviewed_at) + '</div>' : '');
  }
  if (x.verify_due_at) return '<span style="color:var(--p1)">待复盘</span>';
  return '<span class="dim">未设验证日</span>';
}

async function actSubmit(){
  const btn = $('#a-add');
  const type = ($('#a-type').value || '').trim();
  if (!type) { toast('先填动作类型', true); return; }
  const rawDays = ($('#a-verify').value || '').trim();
  const payload = {
    action_type: type,
    asin: ($('#a-asin').value || '').trim().toUpperCase() || null,
    reason: ($('#a-reason').value || '').trim() || null,
    description: ($('#a-desc').value || '').trim(),
    // 留空就传 null：由 L3 决定"不设验证日期"，而不是前端偷偷塞一个 0 或 7
    verify_days: rawDays ? parseInt(rawDays, 10) : null
  };
  btn.disabled = true;
  $('#a-msg').innerHTML = '';
  try {
    const res = await api('/api/panel/actions', payload);
    $('#a-msg').innerHTML = '<div class="warnbox"><b>已记下 #' + res.action_id + '</b>'
      + (res.verify_due_at ? '<br>验证日期：' + dt(res.verify_due_at) + '，到期会出现在上面的「待复盘」' : '')
      + (res.decision_id ? '<br>已挂上当时的判断 #' + res.decision_id + '，复盘时能点开看' : '')
      + (res.comparable === false
          ? '<br>⚠ 这个竞品一次都还没采过，到期会没有可比的前后数据'
          : '')
      + (res.unbound_warning ? '<br>提醒：' + esc(res.unbound_warning) : '') + '</div>';
    $('#a-asin').value = ''; $('#a-type').value = ''; $('#a-reason').value = ''; $('#a-desc').value = '';
    await actLoad();
  } catch (e) {
    // 兜底入口的错误必须说清楚下一步做什么，否则人只会重试同一个错
    $('#a-msg').innerHTML = '<div class="err"><div class="t">没记上</div><div class="m">' +
      esc(e.message) + '</div>' + (e.hint ? '<div class="h">建议：' + esc(e.hint) + '</div>' : '') +
      '</div>';
  } finally {
    btn.disabled = false;
  }
}
"""


def section() -> str:
    return SECTION


def js() -> str:
    return JS
