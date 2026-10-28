"""只读看板（单文件 HTML，无构建步骤、无外部依赖）。

定位很明确：**看板是用来「扫一眼」的，不是用来操作的**。
要操作就去飞书跟 Agent 说话——那才是主入口。

所以这里只做三件事：今日变化、自家锚点、竞品池。
"""
from __future__ import annotations

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>竞品监控看板</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#ffffff; --line:#e5e7eb; --text:#111827;
    --muted:#6b7280; --p0:#dc2626; --p1:#ea580c; --ok:#059669;
    --p0bg:#fef2f2; --p1bg:#fff7ed; --accent:#2563eb;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0f1115; --card:#181b21; --line:#2a2f38; --text:#e8eaed;
      --muted:#9aa1ac; --p0:#f87171; --p1:#fb923c; --ok:#34d399;
      --p0bg:#241416; --p1bg:#241a14; --accent:#60a5fa;
    }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;}
  .wrap{max-width:1080px;margin:0 auto;padding:24px 20px 64px}
  header{display:flex;align-items:baseline;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:20px}
  h1{font-size:20px;margin:0;font-weight:650;letter-spacing:-.2px}
  .sub{color:var(--muted);font-size:12.5px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px}
  @media(max-width:720px){.grid{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
  .card h2{font-size:13px;font-weight:600;color:var(--muted);margin:0 0 10px;letter-spacing:.4px;text-transform:uppercase}
  .big{font-size:26px;font-weight:700;letter-spacing:-.5px}
  .row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 0;border-bottom:1px dashed var(--line)}
  .row:last-child{border-bottom:0}
  .tag{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11.5px;font-weight:600}
  .tag.S{background:#fee2e2;color:#991b1b}
  .tag.A{background:#ffedd5;color:#9a3412}
  .tag.B{background:#dbeafe;color:#1e40af}
  .tag.C{background:#f3f4f6;color:#4b5563}
  @media (prefers-color-scheme: dark){
    .tag.S{background:#3f1d20;color:#fca5a5}
    .tag.A{background:#3d2716;color:#fdba74}
    .tag.B{background:#17293f;color:#93c5fd}
    .tag.C{background:#252a32;color:#9ca3af}
  }
  .item{border-radius:10px;padding:12px 14px;margin-bottom:10px;border:1px solid var(--line)}
  .item.p0{background:var(--p0bg);border-color:var(--p0)}
  .item.p1{background:var(--p1bg);border-color:var(--p1)}
  .item .hd{display:flex;gap:8px;align-items:center;font-weight:600;font-size:13.5px;margin-bottom:4px}
  .item .txt{font-size:13px;color:var(--text)}
  .item .act{font-size:12.5px;color:var(--muted);margin-top:6px;line-height:1.75}
  .lvl{font-size:11px;font-weight:700;padding:1px 6px;border-radius:4px;color:#fff}
  .lvl.P0{background:var(--p0)} .lvl.P1{background:var(--p1)}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
  th{color:var(--muted);font-weight:600;font-size:12px}
  td.dim{color:var(--muted)}
  .empty{color:var(--muted);font-size:13px;padding:12px 0}
  .bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden;margin-top:8px}
  .bar > i{display:block;height:100%;background:var(--accent);border-radius:3px}
  .warn{font-size:12.5px;color:var(--p1);margin-top:8px;line-height:1.7}
  .foot{color:var(--muted);font-size:12px;margin-top:22px;line-height:1.9}
  code{background:var(--line);padding:1px 5px;border-radius:4px;font-size:12px}
  .q{display:inline-block;margin:3px 6px 0 0;padding:4px 10px;border-radius:14px;
     border:1px solid var(--line);font-size:12.5px;cursor:default;color:var(--muted)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>竞品监控看板</h1>
      <div class="sub" id="sub">加载中…</div>
    </div>
    <div class="sub">日常操作请直接在飞书里跟 Agent 说</div>
  </header>

  <div class="grid">
    <div class="card">
      <h2>自家锚点</h2>
      <div class="big" id="readiness">—</div>
      <div class="bar"><i id="rbar" style="width:0%"></i></div>
      <div class="sub" id="anchorDetail" style="margin-top:10px">—</div>
      <div class="warn" id="anchorWarn"></div>
    </div>
    <div class="card">
      <h2>竞品池</h2>
      <div class="big" id="poolTotal">—</div>
      <div class="sub" id="poolTiers" style="margin-top:6px">—</div>
      <div class="sub" id="poolSig" style="margin-top:10px">—</div>
    </div>
  </div>

  <div class="card" style="margin-bottom:16px">
    <h2>今日变化</h2>
    <div id="changes"><div class="empty">加载中…</div></div>
  </div>

  <div class="card">
    <h2>监控列表</h2>
    <div id="table"><div class="empty">加载中…</div></div>
  </div>

  <div class="foot">
    数据源：卖家精灵转接桥 · 采集节奏 1 小时/轮（旺季 15 分钟）<br>
    拿不到的：对手广告预算、出价、ACOS、真实库存、成本结构、补货计划
    <div style="margin-top:8px">
      <span class="q">他降到 19.99 我要不要跟</span>
      <span class="q">这个 ASIN 是不是在清仓</span>
      <span class="q">盯一下 B0XXXXXXXX</span>
      <span class="q">今天有什么要处理的</span>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const money = v => v == null ? '—' : '$' + Number(v).toFixed(2);
const num = v => v == null ? '—' : Number(v).toLocaleString();
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const INV = {alert:['偏紧','var(--p0)'], normal:['正常','var(--ok)'], surplus:['充裕','var(--accent)'], unknown:['未知','var(--muted)']};

async function load(){
  try{
    const [brief, comps] = await Promise.all([
      fetch('/api/brief?hours=24').then(r=>r.json()),
      fetch('/api/competitors').then(r=>r.json())
    ]);
    renderAnchor(brief.anchor || {});
    renderPool(comps.items || [], brief);
    renderChanges(brief);
    renderTable(comps.items || []);
    $('sub').textContent = '数据截至 ' + new Date().toLocaleString('zh-CN');
  }catch(e){
    $('sub').textContent = '加载失败：' + e.message;
  }
}

function renderAnchor(a){
  const r = a.readiness ?? 0;
  $('readiness').textContent = r + '%';
  $('rbar').style.width = Math.min(r,100) + '%';
  const dos = a.days_of_supply;
  const inv = INV[a.inventory_status || 'unknown'];
  $('anchorDetail').innerHTML =
    '分级 <b>' + (a.grade || '—') + '</b> · 可售 <b>' + (dos ? Math.round(dos)+' 天' : '未知') +
    '</b> <span style="color:'+inv[1]+'">（'+inv[0]+'）</span>';
  const w = (a.warnings || []).slice(0,2);
  $('anchorWarn').innerHTML = w.map(x=>'⚠️ '+esc(x)).join('<br>');
}

function renderPool(items, brief){
  const c = {};
  items.forEach(i => c[i.tier] = (c[i.tier]||0)+1);
  $('poolTotal').textContent = items.length;
  $('poolTiers').innerHTML = ['S','A','B','C']
    .map(t => '<span class="tag '+t+'">'+t+' 级 '+(c[t]||0)+'</span>').join(' ');
  const lv = {p0:(brief.p0||[]).length, p1:(brief.p1||[]).length, p2:brief.p2_count||0};
  $('poolSig').textContent = '近 24 小时：P0 ' + lv.p0 + ' 条 · P1 ' + lv.p1 + ' 条 · P2 ' + lv.p2 + ' 条归档';
}

function itemHtml(it){
  const acts = (it.actions||[]).slice(0,3).map(a=>'· '+esc(a)).join('<br>');
  return '<div class="item '+it.level.toLowerCase()+'">' +
    '<div class="hd"><span class="lvl '+it.level+'">'+it.level+'</span>' +
    '<span class="tag '+it.tier+'">'+it.tier+'</span>' +
    '<span>'+esc(it.asin)+'</span>' +
    '<span class="dim" style="font-weight:400;color:var(--muted);font-size:12px">'+esc((it.title||'').slice(0,26))+'</span></div>' +
    '<div class="txt">'+esc(it.description)+'</div>' +
    (it.headline ? '<div class="txt" style="font-weight:600;margin-top:4px">→ '+esc(it.headline)+'</div>' : '') +
    (acts ? '<div class="act">'+acts+'</div>' : '') +
  '</div>';
}

function renderChanges(b){
  const list = [...(b.p0||[]), ...(b.p1||[])];
  if(!list.length){
    $('changes').innerHTML = '<div class="empty">今日无 P0 / P1 级变化，池子安静。</div>';
    return;
  }
  $('changes').innerHTML = list.map(itemHtml).join('');
}

function renderTable(items){
  if(!items.length){
    $('table').innerHTML = '<div class="empty">池子还是空的。在飞书里发「盯一下 B0XXXXXXXX」就能加。</div>';
    return;
  }
  const rows = items.map(i =>
    '<tr><td><span class="tag '+i.tier+'">'+i.tier+'</span></td>' +
    '<td><b>'+esc(i.asin)+'</b><br><span class="dim" style="font-size:11.5px">'+esc((i.title||'').slice(0,30))+'</span></td>' +
    '<td>'+money(i.net_price ?? i.price)+'</td>' +
    '<td>'+num(i.rank_sub)+'</td>' +
    '<td>'+num(i.review_count)+'</td>' +
    '<td>'+(i.rating==null?'—':Number(i.rating).toFixed(1))+'</td>' +
    '<td>'+(i.in_stock===false?'<span style="color:var(--p0)">断货</span>':(i.in_stock===true?'在售':'—'))+'</td>' +
    '<td class="dim">'+(i.keyword_overlap==null?'—':Math.round(i.keyword_overlap*100)+'%')+'</td>' +
    '<td class="dim">'+esc(i.data_quality||'')+'</td></tr>'
  ).join('');
  $('table').innerHTML =
    '<table><thead><tr><th>层</th><th>ASIN</th><th>净价</th><th>BSR</th><th>评论</th><th>星级</th><th>库存</th><th>词重合</th><th>数据</th></tr></thead>' +
    '<tbody>'+rows+'</tbody></table>';
}

load();
setInterval(load, 120000);
</script>
</body>
</html>
"""
