"""视图⑤：数据健康。

这一屏回答一个问题：**我现在看到的结论有多可信？**

数据源挂掉的时候，用户最需要的恰恰是这一页——所以它必须在任何数据状态下都能打开、
都能给出东西（没有失败轮次就显示"没有失败"，而不是空白或报错）。
这也是它没有资格跟着其它视图一起白屏的原因（`safeLoad()` 的隔离在 layout 层）。

## 价格可信度曲线为什么要专门配一段文案

`net_price` 为 NULL 的唯一含义是「当轮促销接口没成功，价格不可知」，
不是「价格是 0」。这两件事在图上看起来都像"掉到底"，但含义完全相反：
前者要去看数据源，后者是要立刻处理的定价事故。
所以这张图旁边必须写清楚，否则它自己会造出一个假警报。

## 阈值来源为什么放在"数据健康"里

因为「结论不对」这件事有两个来源：数据不对，或者尺子不对。
前四个区块看数据，最后一块看尺子——它直接告诉你每个键当前是从哪一层来的，
公司层覆盖了哪些键、产品层覆盖了哪些键。改阈值之前先看这里，
就知道自己的改动会和谁的覆盖打架。
"""
from __future__ import annotations

SECTION = """
<section class="view" id="view-health">
  <div class="view-body">
    <div class="grid4">
      <div class="stat"><div class="k">采集轮次</div>
        <div class="v" id="h-total">—</div><div class="n" id="h-total-n"></div></div>
      <div class="stat"><div class="k">成功率</div>
        <div class="v" id="h-rate">—</div><div class="n" id="h-rate-n"></div></div>
      <div class="stat"><div class="k">失败轮次</div>
        <div class="v" id="h-fail">—</div><div class="n" id="h-fail-n">失败不一定是数据源的问题</div></div>
      <div class="stat"><div class="k">价格可知比例（最近一天）</div>
        <div class="v" id="h-trust">—</div><div class="n" id="h-trust-n"></div></div>
    </div>

    <h2 class="sec">价格可信度（按天）</h2>
    <div class="card">
      <div id="h-bars"><div class="empty">加载中…</div></div>
      <div class="note">
        柱高 = 当天快照里 <b>价格可知</b> 的比例（<code>net_price</code> 非空）。
        <b>价格不可知表示当轮促销接口没成功，不是价格是 0。</b>
        补一个 0 会把「不知道」变成「降价到 0」，降价检测、均价统计会全被污染且不报错——
        所以这里宁可留空，也不填数。
      </div>
    </div>

    <h2 class="sec">最近的失败轮次</h2>
    <div class="card" id="h-fails"><div class="empty">加载中…</div></div>

    <h2 class="sec">数据新鲜度</h2>
    <div class="card" id="h-fresh"><div class="empty">加载中…</div></div>

    <h2 class="sec">当前阈值来源</h2>
    <div class="card" id="h-thr"><div class="empty">加载中…</div></div>
  </div>
</section>
"""


JS = r"""
const HEALTH = {days: 7};

register('health', async () => {
  await healthLoad();
});

async function healthLoad(){
  // 三块一起请求：健康度、上下文（拿 my_product_id）、生效阈值（拿 layers）
  // 阈值这一块单独容错——它挂了不该影响"数据到底新不新鲜"的判断
  const ctx = await api('/api/panel/context');
  const [h, thr] = await Promise.all([
    api('/api/panel/health?days=' + HEALTH.days),
    api('/api/config/thresholds' +
        (ctx.my_product_id ? '?my_product_id=' + encodeURIComponent(ctx.my_product_id) : ''))
      .catch(e => ({__err: e}))
  ]);

  const r = h.rounds || {};
  $('#h-total').textContent = num(r.total);
  $('#h-total-n').textContent = '窗口 ' + num(h.window_days || HEALTH.days) + ' 天，成功 ' + num(r.ok) +
    (r.partial ? '（其中不完整 ' + num(r.partial) + '）' : '');

  $('#h-rate').textContent = (r.success_rate === null || r.success_rate === undefined)
    ? '—' : pct(r.success_rate);
  $('#h-rate-n').textContent = r.total ? '按「这一轮拿到数据」算，不按「没报错」算'
                                       : '窗口内还没有采集记录';

  $('#h-fail').textContent = num(r.failed);
  $('#h-fail-n').textContent = (r.failed ? '下面列出最近 ' + num((h.failures || []).length) + ' 条原因'
                                        : '窗口内没有失败轮次');

  // ---- 价格可信度曲线 ----
  const days = h.price_trust || [];
  if (!days.length) {
    $('#h-bars').innerHTML = '<div class="empty">窗口内还没有快照，暂时算不出可信度。</div>';
    $('#h-trust').textContent = '—';
    $('#h-trust-n').textContent = '没有数据';
  } else {
    const last = days[days.length - 1];
    $('#h-trust').textContent = (last.trust_ratio === null || last.trust_ratio === undefined)
      ? '—' : pct(last.trust_ratio, 0);
    $('#h-trust-n').textContent = last.date + '：可知 ' + num(last.price_known) +
      ' / 共 ' + num(last.rounds) + ' 条';

    const bars = days.map(d => {
      const ratio = d.trust_ratio === null || d.trust_ratio === undefined ? 0 : d.trust_ratio;
      const hgt = Math.max(2, Math.round(ratio * 100));
      const bad = d.price_unknown > 0;
      return '<div class="b' + (bad ? ' bad' : '') + '" title="' + esc(d.date) +
        '：可知 ' + num(d.price_known) + ' / 共 ' + num(d.rounds) +
        '，不可知 ' + num(d.price_unknown) + ' 条">' +
        '<i style="height:' + hgt + '%"></i></div>';
    }).join('');
    const xs = days.map(d => '<span>' + esc(d.date.slice(5)) + '</span>').join('');
    $('#h-bars').innerHTML = '<div class="bars">' + bars + '</div><div class="bars x">' + xs + '</div>' +
      '<div class="note" style="margin-top:8px">共 ' + num(days.length) + ' 天；' +
      '红色柱 = 当天有价格不可知的快照（不是价格跌了）。</div>';
  }

  // ---- 失败轮次 ----
  const fails = h.failures || [];
  if (!fails.length) {
    $('#h-fails').innerHTML = '<div class="empty">窗口内没有失败轮次。' +
      '<br>没有记录也是一种信号：如果同时「采集轮次」也很少，那说明采集根本没跑起来，' +
      '而不是"跑得很好"。</div>';
  } else {
    $('#h-fails').innerHTML = '<table><thead><tr><th>时间</th><th>竞品</th><th>触发方式</th>' +
      '<th>原因</th><th>耗时</th></tr></thead><tbody>' +
      fails.map(f => `<tr>
        <td class="dim" style="white-space:nowrap">${dt(f.at)}</td>
        <td>${esc(f.asin || '—')}</td>
        <td class="dim">${esc({schedule:'定时', panel:'面板触发', agent:'Agent 触发',
                              manual:'手工', retry:'重试'}[f.trigger] || f.trigger || '—')}</td>
        <td style="max-width:380px">${esc(f.error || '未记录原因')}</td>
        <td class="num dim">${f.duration_ms === null || f.duration_ms === undefined
          ? '—' : num(f.duration_ms) + ' ms'}</td>
      </tr>`).join('') + '</tbody></table>';
  }

  // ---- 数据新鲜度 ----
  const fresh = h.snapshot_freshness || [];
  if (!fresh.length) {
    $('#h-fresh').innerHTML = '<div class="empty">池子里还没有竞品。</div>';
  } else {
    const stale = fresh.filter(x => x.stale);
    $('#h-fresh').innerHTML = '<table><thead><tr><th>竞品</th><th>状态</th>' +
      '<th>最后一次数据</th><th>距今</th><th>质量</th></tr></thead><tbody>' +
      fresh.map(x => `<tr>
        <td><b>${esc(x.asin)}</b></td>
        <td>${statusTag(x.status)}</td>
        <td class="dim">${x.last_at ? dt(x.last_at) : '从未采到'}</td>
        <td>${x.hours_ago === null || x.hours_ago === undefined
          ? '<span class="dim">—</span>' : num(x.hours_ago, 1) + ' 小时'}</td>
        <td>${x.stale ? '<span class="tag S">过期</span>' : '<span class="tag active">新鲜</span>'}
            <span class="dim" style="font-size:11.5px">${esc(x.quality || '')}</span></td>
      </tr>`).join('') + '</tbody></table>' +
      '<div class="note">超过 36 小时没有新快照就算过期（与日报里的口径一致，' +
      '`health.STALE_HOURS`）。从没采到过数据也算过期——那是「还没开始」，不是「一切正常」。</div>' +
      (stale.length ? '<div class="warnbox"><b>' + num(stale.length) + ' 个竞品数据过期。</b>' +
        '<br>如果全都过期，先看上面的失败轮次与采集轮次：' +
        '可能是数据源熔断、也可能是定时任务没起来（<code>SCHEDULER_ENABLED</code>）。</div>' : '');
  }

  // ---- 阈值来源 ----
  if (thr && thr.__err) {
    $('#h-thr').innerHTML = '<div class="err"><div class="t">阈值来源没加载出来</div><div class="m">' +
      esc(thr.__err.message) + '</div>' +
      (thr.__err.hint ? '<div class="h">建议：' + esc(thr.__err.hint) + '</div>' : '') +
      '<div class="h">这一块只影响"尺子从哪来"，上面的数据健康结论不受影响。</div></div>';
  } else {
    const layers = thr.layers || [];
    const srcCount = {company: 0, user: 0, product: 0, default: 0};
    Object.keys(thr.sources || {}).forEach(g => {
      Object.keys(thr.sources[g] || {}).forEach(k => {
        const s = thr.sources[g][k];
        srcCount[s] = (srcCount[s] || 0) + 1;
      });
    });
    const layerHtml = layers.length
      ? '<table><thead><tr><th>作用域</th><th>分组</th><th>版本</th><th>覆盖了哪些键</th>' +
        '<th>最后修改</th></tr></thead><tbody>' +
        layers.map(l => `<tr>
          <td>${esc(l.scope_title || l.scope)}${l.scope_id ? '<div class="dim" style="font-size:11px">' +
            esc(l.scope_id) + '</div>' : ''}</td>
          <td class="dim">${esc(GROUP_TITLES[l.group_name] || l.group_name)}</td>
          <td class="num">v${esc(l.version)}</td>
          <td style="font-size:12px">${Object.keys(l.values || {}).map(k =>
            '<code>' + esc(k) + '</code>').join(' ') || '<span class="dim">空</span>'}</td>
          <td class="dim" style="white-space:nowrap">${dt(l.updated_at)}</td>
        </tr>`).join('') + '</tbody></table>'
      : '<div class="empty">两层都没有覆盖值，全部按系统默认跑。</div>';

    $('#h-thr').innerHTML = layerHtml + `<div class="note">
      当前生效值的来源分布：系统默认 <b>${num(srcCount.default || 0)}</b> 项 ·
      公司层 <b>${num(srcCount.company || 0)}</b> 项 ·
      产品层 <b>${num(srcCount.product || 0)}</b> 项 ·
      个人层 <b>${num(srcCount.user || 0)}</b> 项（本轮不生效，定时任务与推送不读它）。<br>
      改阈值之前先看这张表：同一个键如果产品层已经覆盖，改公司层对它不起作用。
    </div>`;
  }
}
"""


def section() -> str:
    return SECTION


def js() -> str:
    return JS
