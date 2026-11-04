"""视图④：动作记录（兜底入口）。

## 这一屏为什么必须存在，但不是主力

真正记动作的地方是**飞书卡片上的按钮**：信号推过来，你点「跟价」，系统自动落库，
还带上 `signal_id`（于是"你当时为什么这么做"永远有据可查）。
下一轮接那个。

但没有卡片就不能记动作的话，遇到「老板在电话里说『这个我们先不动』」这种场景，
这条决策就彻底丢了——而它恰恰是 7 天后要复盘的那条。
所以这一屏是**兜底入口**：手工补一条，来源记成 `panel`，与卡片来的记录在
列表里一眼可分（`source` 列）。两者写的是同一张表、同一套校验。

## 为什么动作类型既有下拉又有自由输入

`actions.action_type` 是自由字符串（库里已有 `price_follow` / `price_cut` 等写法）。
如果这一屏做成封闭枚举，历史数据里没在枚举内的值就显示不出来、也补不进去，
最后一定会有人去改库。所以：常用类型给数据列表（`<datalist>`）方便选，
但仍然允许手写；显示时按已知映射翻中文，不认识的照原样显示。

## 为什么「验证日期」单独一列

动作记下来不是终点：7 天后要回看"跟了价，销量掉没掉"。
`verify_due_at` 就是那个提醒日期，它由 L3 按动作类型算，面板只负责摆出来。
到期的行会标出来——这是这一屏唯一"主动"的地方，因为它是在提醒你一件已承诺的事。
"""
from __future__ import annotations

SECTION = """
<section class="view" id="view-actions">
  <div class="view-body">
    <div class="warnbox">
      <b>这是兜底入口。</b>主力入口是飞书卡片上的按钮——信号推过来时点一下，
      系统会自动带上信号 ID 与来源，不需要你回来填表。<br>
      手工记录的来源会标成 <code>panel</code>，和卡片来的（<code>feishu_card</code>）区分开，
      复盘时能看出"这条是人后补的"。
    </div>

    <h2 class="sec">补记一条动作</h2>
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
const SOURCE_LABEL = {panel:'面板手工', feishu_card:'飞书卡片', agent:'Agent 代录', system:'系统'};

register('actions', async () => {
  $('#a-types').innerHTML = ACT_TYPES.map(([v, t]) =>
    '<option value="' + v + '">' + t + '</option>').join('');
  $('#a-add').onclick = actSubmit;
  await actLoad();
});

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

  const now = Date.now();
  const rows = items.map(x => {
    const due = x.verify_due_at ? new Date(String(x.verify_due_at).replace(' ', 'T')) : null;
    const overdue = due && !isNaN(due.getTime()) && due.getTime() < now && !x.verdict;
    return `<tr class="row">
      <td class="dim" style="white-space:nowrap">${dt(x.at)}</td>
      <td>${esc(x.user || '—')}</td>
      <td>${x.asin ? '<b>' + esc(x.asin) + '</b>' : '<span class="dim">—</span>'}</td>
      <td style="white-space:nowrap">${esc(ACT_LABEL[x.action_type] || x.action_type || '—')}
          <div class="dim" style="font-size:11px">${esc(x.action_type || '')}</div></td>
      <td style="max-width:340px">${esc(x.reason || '')}
          ${x.description ? '<div class="dim" style="font-size:11.5px">' + esc(x.description) + '</div>' : ''}</td>
      <td class="dim" style="white-space:nowrap">${esc(SOURCE_LABEL[x.source] || x.source || '')}
          ${x.signal_id ? '<div style="font-size:11px">信号 #' + x.signal_id + '</div>' : ''}</td>
      <td style="white-space:nowrap">${x.verify_due_at ? dt(x.verify_due_at) : '—'}
          ${overdue ? '<div class="dim" style="font-size:11px;color:var(--p1)">已到期未复盘</div>' : ''}</td>
    </tr>`;
  }).join('');

  $('#a-table').innerHTML = `<table>
    <thead><tr><th>时间</th><th>谁</th><th>竞品</th><th>类型</th>
      <th>依据 / 说明</th><th>来源</th><th>验证日期</th></tr></thead>
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
    $('#a-msg').innerHTML = '<div class="warnbox"><b>已记下 #' + res.action_id + '</b>' +
      (res.verify_due_at ? '<br>验证日期：' + dt(res.verify_due_at) : '') +
      (res.unbound_warning ? '<br>提醒：' + esc(res.unbound_warning) : '') + '</div>';
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
