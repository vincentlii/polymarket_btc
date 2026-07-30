"""Single-file, dependency-free page for the read-only BTC bot dashboard."""

from __future__ import annotations


def dashboard_html() -> str:
    return _DASHBOARD_HTML


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BTC 15m Bot · Control Room</title>
<style>
:root {
  color-scheme: dark;
  --bg:#0c100e;
  --surface:#121714;
  --surface-2:#171d19;
  --line:#29332c;
  --line-soft:#202923;
  --text:#edf1eb;
  --muted:#96a198;
  --quiet:#68736b;
  --ok:#8fd3a6;
  --ok-bg:#16281d;
  --warn:#e7be73;
  --warn-bg:#2a2315;
  --bad:#ec8c80;
  --bad-bg:#2d1917;
  --unknown:#98a19b;
  --accent:#c9df8a;
  --shadow:0 22px 60px rgba(0,0,0,.2);
}
* { box-sizing:border-box; }
[hidden] { display:none !important; }
html { background:var(--bg); }
body {
  margin:0;
  min-height:100vh;
  color:var(--text);
  background:
    radial-gradient(circle at 88% -10%, rgba(87,116,83,.18), transparent 28rem),
    radial-gradient(circle at -12% 36%, rgba(64,88,71,.12), transparent 24rem),
    var(--bg);
  font-family:"Avenir Next","Segoe UI Variable","Noto Sans SC",sans-serif;
  font-variant-numeric:tabular-nums;
}
button, input, select, table { font:inherit; }
main { width:min(1240px, calc(100% - 40px)); margin:0 auto; padding:26px 0 56px; }
.topbar { display:flex; align-items:center; justify-content:space-between; gap:24px; padding:8px 0 24px; }
.brand { display:flex; align-items:center; gap:12px; min-width:0; }
.mark { width:34px; height:34px; display:grid; place-items:center; border:1px solid #4d5d50; background:#172019; color:var(--accent); font:800 .72rem/1 ui-monospace,"Cascadia Code",monospace; letter-spacing:-.05em; }
.brand-copy strong { display:block; font-size:.98rem; letter-spacing:.01em; }
.brand-copy span { display:block; margin-top:2px; color:var(--muted); font-size:.74rem; }
.top-meta { display:flex; align-items:center; justify-content:flex-end; gap:10px; flex-wrap:wrap; }
.pill { display:inline-flex; align-items:center; gap:7px; min-height:30px; padding:6px 10px; border:1px solid var(--line); border-radius:999px; background:rgba(20,27,23,.78); color:var(--muted); font-size:.72rem; }
.dot { width:7px; height:7px; border-radius:50%; background:var(--unknown); box-shadow:0 0 0 4px rgba(152,161,155,.08); }
.pill.ok { color:var(--ok); border-color:#31513c; background:var(--ok-bg); }
.pill.warning { color:var(--warn); border-color:#554526; background:var(--warn-bg); }
.pill.error { color:var(--bad); border-color:#5d302b; background:var(--bad-bg); }
.pill.ok .dot { background:var(--ok); box-shadow:0 0 0 4px rgba(143,211,166,.1); }
.pill.warning .dot { background:var(--warn); box-shadow:0 0 0 4px rgba(231,190,115,.1); }
.pill.error .dot { background:var(--bad); box-shadow:0 0 0 4px rgba(236,140,128,.1); }
.hero { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:32px; align-items:end; padding:34px 0 30px; border-top:1px solid var(--line); }
.kicker,.section-kicker { color:var(--accent); font:700 .68rem/1.4 ui-monospace,"Cascadia Code",monospace; letter-spacing:.13em; text-transform:uppercase; }
h1 { margin:9px 0 12px; max-width:720px; font-size:clamp(2rem,4.2vw,3.65rem); line-height:1.02; letter-spacing:-.048em; font-weight:550; }
.hero p { margin:0; color:var(--muted); font-size:.9rem; }
.freshness { min-width:210px; text-align:right; }
.freshness strong { display:block; font-size:.95rem; font-weight:550; }
.freshness span { display:block; margin-top:5px; color:var(--quiet); font:500 .7rem/1.4 ui-monospace,"Cascadia Code",monospace; }
.alert-stack { display:grid; gap:8px; margin-bottom:16px; }
.alert { display:flex; align-items:flex-start; gap:10px; padding:11px 13px; border:1px solid var(--line); border-radius:10px; background:var(--surface); color:var(--muted); font-size:.78rem; }
.alert.warning { border-color:#4a3d25; background:var(--warn-bg); color:#ecd39f; }
.alert.error { border-color:#55302b; background:var(--bad-bg); color:#f1b0a7; }
.metric-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }
.metric { min-height:116px; padding:17px 18px 15px; border:1px solid var(--line); border-radius:12px; background:linear-gradient(145deg,rgba(24,31,27,.96),rgba(16,21,18,.96)); box-shadow:var(--shadow); animation:settle .45s both; }
.metric:nth-child(2) { animation-delay:.04s; }.metric:nth-child(3) { animation-delay:.08s; }.metric:nth-child(4) { animation-delay:.12s; }
.metric-label { color:var(--muted); font-size:.72rem; }
.metric-value { margin-top:14px; font-size:clamp(1.35rem,2.5vw,2rem); line-height:1; letter-spacing:-.04em; font-weight:570; }
.metric-note { margin-top:9px; color:var(--quiet); font-size:.67rem; }
.positive { color:var(--ok) !important; }.negative { color:var(--bad) !important; }.neutral { color:var(--text) !important; }
.main-grid { display:grid; grid-template-columns:minmax(0,1.75fr) minmax(300px,.85fr); gap:12px; margin-top:12px; }
.panel { border:1px solid var(--line); border-radius:12px; background:rgba(18,23,20,.95); box-shadow:var(--shadow); overflow:hidden; }
.panel-head { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; padding:18px 20px 14px; border-bottom:1px solid var(--line-soft); }
.panel-head h2 { margin:4px 0 0; font-size:1rem; font-weight:570; letter-spacing:-.015em; }
.panel-meta { color:var(--quiet); font-size:.67rem; text-align:right; }
.chart-wrap { min-height:276px; padding:12px 18px 5px; display:grid; align-items:center; }
#equity-chart { width:100%; height:228px; overflow:visible; }
.chart-grid { stroke:#253028; stroke-width:1; }
.chart-area { fill:url(#equity-fill); }
.chart-line { fill:none; stroke:var(--accent); stroke-width:2.4; stroke-linecap:round; stroke-linejoin:round; }
.chart-dot { fill:var(--accent); stroke:#253027; stroke-width:5; }
.chart-label { fill:var(--muted); font:500 11px ui-monospace,"Cascadia Code",monospace; }
.empty { min-height:170px; display:grid; place-items:center; color:var(--quiet); text-align:center; padding:28px; font-size:.78rem; line-height:1.7; }
.mini-stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); border-top:1px solid var(--line-soft); }
.mini-stat { padding:13px 17px; border-right:1px solid var(--line-soft); }
.mini-stat:last-child { border-right:0; }
.mini-stat span { display:block; color:var(--quiet); font-size:.65rem; }
.mini-stat strong { display:block; margin-top:5px; font-size:.85rem; font-weight:550; }
.variant-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; padding:16px 20px 20px; }
.variant-card { padding:15px; border:1px solid var(--line-soft); border-radius:10px; background:var(--surface-2); }
.variant-card.primary { border-color:#536346; box-shadow:inset 0 2px 0 var(--accent); }
.variant-title { display:flex; justify-content:space-between; gap:10px; color:var(--text); font-size:.82rem; }
.variant-policy { margin-top:5px; color:var(--quiet); font-size:.66rem; }
.variant-values { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:14px; }
.variant-values span { display:block; color:var(--quiet); font-size:.62rem; }
.variant-values strong { display:block; margin-top:4px; font-size:.82rem; font-weight:550; }
.variant-segments { margin-top:12px; padding-top:10px; border-top:1px solid var(--line-soft); color:var(--quiet); font-size:.62rem; line-height:1.55; }
.funnel-shell { padding:0 20px 20px; }
.funnel-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:9px; color:var(--quiet); font-size:.64rem; }
.funnel-track { display:grid; grid-template-columns:repeat(7,minmax(88px,1fr)); gap:7px; overflow-x:auto; padding-bottom:3px; }
.funnel-step { min-width:88px; padding:10px 11px; border:1px solid var(--line-soft); border-radius:8px; background:#111713; }
.funnel-step span { display:block; color:var(--quiet); font-size:.59rem; }
.funnel-step strong { display:block; margin-top:5px; color:var(--text); font-size:.9rem; font-weight:560; }
.lifecycle-body { padding:18px 20px 20px; }
.stage-track { display:grid; grid-template-columns:repeat(5,1fr); gap:4px; margin:2px 0 22px; }
.stage { position:relative; padding-top:13px; color:var(--quiet); font-size:.62rem; text-align:center; }
.stage::before { content:""; position:absolute; top:0; left:50%; width:7px; height:7px; transform:translateX(-50%); border-radius:50%; background:#354038; }
.stage::after { content:""; position:absolute; top:3px; left:calc(50% + 6px); right:calc(-50% + 6px); height:1px; background:#303b33; }
.stage:last-child::after { display:none; }
.stage.complete,.stage.active { color:var(--text); }
.stage.complete::before { background:#597761; }
.stage.active::before { background:var(--accent); box-shadow:0 0 0 5px rgba(201,223,138,.1); }
.cycle-title { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:13px; }
.cycle-title strong { font-size:1.18rem; font-weight:560; }
.cycle-copy { margin:0 0 18px; color:var(--muted); font-size:.76rem; line-height:1.65; }
.progress { height:6px; border-radius:99px; background:#242d27; overflow:hidden; }
.progress > span { display:block; width:0; height:100%; background:linear-gradient(90deg,#718d73,var(--accent)); border-radius:inherit; transition:width .35s ease; }
.progress-copy { display:flex; justify-content:space-between; gap:12px; margin-top:7px; color:var(--quiet); font-size:.64rem; }
.cycle-list { display:grid; gap:10px; margin:18px 0 0; }
.cycle-row { display:grid; grid-template-columns:112px minmax(0,1fr); gap:12px; font-size:.72rem; }
.cycle-row dt { color:var(--quiet); }.cycle-row dd { margin:0; color:#d4d9d3; overflow-wrap:anywhere; }
.section { margin-top:12px; }
.health-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; padding:12px; }
.health-card { min-height:102px; padding:14px; border:1px solid var(--line-soft); border-radius:9px; background:var(--surface-2); }
.health-top { display:flex; align-items:center; justify-content:space-between; gap:10px; }
.health-label { font-size:.76rem; font-weight:560; }
.health-state { display:flex; align-items:center; gap:6px; color:var(--muted); font-size:.62rem; text-transform:uppercase; letter-spacing:.08em; }
.health-state .dot { width:6px; height:6px; box-shadow:none; }
.health-card.ok .health-state { color:var(--ok); }.health-card.ok .dot { background:var(--ok); }
.health-card.warning .health-state { color:var(--warn); }.health-card.warning .dot { background:var(--warn); }
.health-card.error .health-state { color:var(--bad); }.health-card.error .dot { background:var(--bad); }
.health-detail { margin:12px 0 0; min-height:32px; color:var(--muted); font-size:.69rem; line-height:1.45; }
.health-foot { display:flex; justify-content:space-between; gap:12px; margin-top:10px; color:var(--quiet); font:500 .61rem/1.3 ui-monospace,"Cascadia Code",monospace; }
.table-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; min-width:850px; }
th,td { padding:12px 14px; border-bottom:1px solid var(--line-soft); text-align:left; white-space:nowrap; }
th { color:var(--quiet); font-size:.61rem; font-weight:650; letter-spacing:.08em; text-transform:uppercase; }
td { color:#cdd3cc; font-size:.7rem; }
tbody tr:last-child td { border-bottom:0; }
tbody tr:hover { background:rgba(255,255,255,.018); }
.market-cell { max-width:260px; overflow:hidden; text-overflow:ellipsis; }
.side { display:inline-flex; align-items:center; min-width:42px; justify-content:center; padding:4px 7px; border-radius:6px; background:#222c25; color:#dce3dc; font-size:.62rem; text-transform:uppercase; }
.table-empty { padding:30px !important; color:var(--quiet); text-align:center; }
.history-bar { display:flex; align-items:center; justify-content:space-between; gap:18px; padding:14px 20px; border-top:1px solid var(--line-soft); background:#101512; }
.history-copy strong { display:block; font-size:.72rem; font-weight:570; }
.history-copy span { display:block; margin-top:4px; color:var(--quiet); font-size:.65rem; }
.control { min-height:32px; padding:7px 11px; border:1px solid #455248; border-radius:7px; background:#1a211c; color:var(--text); font-size:.68rem; cursor:pointer; }
.control:hover { border-color:#6b7b6e; background:#202821; }
.control:disabled { opacity:.45; cursor:not-allowed; }
.history-panel { border-top:1px solid var(--line); background:#0f1411; }
.history-toolbar { display:flex; align-items:end; justify-content:space-between; gap:16px; padding:14px 20px; }
.history-filter { display:grid; gap:5px; color:var(--quiet); font-size:.62rem; }
.history-filter select { min-width:170px; cursor:pointer; }
.history-state { color:var(--quiet); font-size:.65rem; text-align:right; }
.history-more { display:flex; justify-content:center; padding:14px 20px 18px; border-top:1px solid var(--line-soft); }
footer { display:flex; align-items:center; justify-content:space-between; gap:20px; padding:20px 2px 0; color:var(--quiet); font-size:.65rem; }
@keyframes settle { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:none; } }
@media (prefers-reduced-motion:reduce) { * { animation:none !important; transition:none !important; } }
@media (max-width:930px) { .metric-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .main-grid { grid-template-columns:1fr; } .health-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .variant-grid { grid-template-columns:1fr; } }
@media (max-width:640px) { main { width:min(100% - 24px,1240px); padding-top:14px; } .topbar,.hero { align-items:flex-start; flex-direction:column; display:flex; } .top-meta { justify-content:flex-start; } .hero { gap:16px; padding:26px 0 22px; } .freshness { text-align:left; } .metric-grid { grid-template-columns:1fr 1fr; } .metric { min-height:104px; padding:15px; } .main-grid { gap:10px; } .panel-head { padding:16px; } .chart-wrap { padding-left:10px; padding-right:10px; } .mini-stats { grid-template-columns:1fr 1fr; } .mini-stat:nth-child(2) { border-right:0; } .mini-stat:nth-child(-n+2) { border-bottom:1px solid var(--line-soft); } .health-grid { grid-template-columns:1fr; } .cycle-row { grid-template-columns:96px minmax(0,1fr); } .history-bar,.history-toolbar { align-items:flex-start; flex-direction:column; } .history-state { text-align:left; } footer { align-items:flex-start; flex-direction:column; } }
</style>
</head>
<body>
<main>
  <header class="topbar">
    <div class="brand"><div class="mark">15m</div><div class="brand-copy"><strong>BTC Bot Control Room</strong><span>Polymarket · read-only</span></div></div>
    <div class="top-meta"><span id="run-mode" class="pill">模式未登记</span><span id="overall" class="pill"><span class="dot"></span>正在读取</span></div>
  </header>

  <section class="hero">
    <div><div class="kicker">OPERATIONS · PERFORMANCE · LIFECYCLE</div><h1>稳定运行，清楚知道每一分钱。</h1><p id="active-market">等待运行状态…</p></div>
    <div class="freshness"><strong id="freshness-title">状态加载中</strong><span id="freshness-time">—</span></div>
  </section>

  <div id="alerts" class="alert-stack" aria-live="polite"></div>

  <section class="metric-grid" aria-label="绩效总览">
    <article class="metric"><div class="metric-label">当前资金</div><div id="metric-equity" class="metric-value">—</div><div id="metric-equity-note" class="metric-note">尚无账户快照</div></article>
    <article class="metric"><div class="metric-label">累计 PnL</div><div id="metric-total-pnl" class="metric-value">—</div><div id="metric-total-note" class="metric-note">已实现 + 未实现</div></article>
    <article class="metric"><div class="metric-label">今日 PnL</div><div id="metric-today-pnl" class="metric-value">—</div><div class="metric-note">UTC 自然日</div></article>
    <article class="metric"><div class="metric-label">最大回撤</div><div id="metric-drawdown" class="metric-value">—</div><div class="metric-note">基于已上报资金曲线</div></article>
  </section>

  <section class="main-grid">
    <article class="panel">
      <div class="panel-head"><div><div class="section-kicker">CAPITAL</div><h2>资金曲线</h2></div><div id="curve-range" class="panel-meta">等待真实记录</div></div>
      <div id="chart-empty" class="empty">尚无 Paper / Canary / Live 资金记录。<br>研究 Proxy 不会被绘制成盈利曲线。</div>
      <div id="chart-wrap" class="chart-wrap" hidden><svg id="equity-chart" viewBox="0 0 800 228" role="img" aria-label="资金曲线"></svg></div>
      <div class="mini-stats"><div class="mini-stat"><span>胜率</span><strong id="stat-win-rate">—</strong></div><div class="mini-stat"><span>订单 / 成交</span><strong id="stat-orders">0 / 0</strong></div><div class="mini-stat"><span>可用资金</span><strong id="stat-available">—</strong></div><div class="mini-stat"><span>未平仓敞口</span><strong id="stat-exposure">—</strong></div></div>
    </article>

    <article class="panel">
      <div class="panel-head"><div><div class="section-kicker">STRATEGY</div><h2>策略生命周期</h2></div><div id="gate-state" class="pill">未登记</div></div>
      <div class="lifecycle-body">
        <div id="stage-track" class="stage-track" aria-label="策略阶段"></div>
        <div class="cycle-title"><strong id="stage-title">等待周期快照</strong></div>
        <p id="next-action" class="cycle-copy">发布流程尚未写入策略周期，页面不会自行猜测当前阶段。</p>
        <div class="progress"><span id="cycle-progress"></span></div><div class="progress-copy"><span id="progress-label">暂无进度目标</span><span id="progress-value">—</span></div>
        <dl class="cycle-list">
          <div class="cycle-row"><dt>Champion</dt><dd id="champion-model">—</dd></div>
          <div class="cycle-row"><dt>Challenger</dt><dd id="challenger-model">—</dd></div>
          <div class="cycle-row"><dt>下次训练</dt><dd id="next-challenge">—</dd></div>
          <div class="cycle-row"><dt>下次评审</dt><dd id="next-review">—</dd></div>
          <div class="cycle-row"><dt>数据截止</dt><dd id="data-cutoff">—</dd></div>
        </dl>
      </div>
    </article>
  </section>

  <section class="panel section">
    <div class="panel-head"><div><div class="section-kicker">EXECUTION RACE</div><h2>三种成交策略对比</h2></div><div id="execution-epoch" class="panel-meta">同一信号 · 独立模拟账本</div></div>
    <div id="variant-grid" class="variant-grid"><div class="table-empty">等待策略数据</div></div>
    <div class="funnel-shell">
      <div class="funnel-head"><span>主策略本进程决策漏斗</span><span id="funnel-scope">等待数据</span></div>
      <div id="decision-funnel" class="funnel-track"><div class="funnel-step"><span>尚未开始</span><strong>—</strong></div></div>
    </div>
  </section>

  <section class="panel section">
    <div class="panel-head"><div><div class="section-kicker">SYSTEM</div><h2>Bot 健康与延迟</h2></div><div id="quality-summary" class="panel-meta">未收到数据质量摘要</div></div>
    <div id="health-grid" class="health-grid"></div>
  </section>

  <section class="panel section">
    <div class="panel-head"><div><div class="section-kicker">ORDERS</div><h2>最近订单与逐单盈亏</h2></div><div class="panel-meta">仅展示最近 15 条 · 按下单时间倒序</div></div>
    <div class="table-wrap"><table><thead><tr><th>时间</th><th>策略</th><th>市场</th><th>方向</th><th>执行 / 结算</th><th>成交</th><th>Fair / Market</th><th>PnL</th><th>下单 RTT</th></tr></thead><tbody id="trade-rows"><tr><td colspan="9" class="table-empty">尚无订单记录</td></tr></tbody></table></div>
    <div class="history-bar">
      <div class="history-copy"><strong>首页固定显示最近 15 条</strong><span>完整模拟订单保存在只读账本，可按策略分页查看。</span></div>
      <button id="history-toggle" class="control" type="button" aria-expanded="false" aria-controls="history-panel">查看完整历史</button>
    </div>
    <div id="history-panel" class="history-panel" hidden>
      <div class="history-toolbar">
        <label class="history-filter">策略筛选<select id="history-variant" class="control"><option value="">全部策略</option></select></label>
        <div id="history-state" class="history-state" aria-live="polite">尚未读取完整账本</div>
      </div>
      <div class="table-wrap"><table><thead><tr><th>时间</th><th>策略</th><th>市场</th><th>方向</th><th>执行 / 结算</th><th>成交</th><th>Fair / Market</th><th>PnL</th><th>下单 RTT</th></tr></thead><tbody id="history-rows"><tr><td colspan="9" class="table-empty">点击“查看完整历史”后按页读取</td></tr></tbody></table></div>
      <div class="history-more"><button id="history-more" class="control" type="button" hidden>加载更多</button></div>
    </div>
  </section>

  <footer><span>只读看板 · 无下单、撤单或停机权限</span><span id="snapshot-source">Runtime status only</span></footer>
</main>
<script>
const byId = id => document.getElementById(id);
const stageOrder = ['research','challenge','shadow','canary','live'];
const stageLabels = {research:'研究',challenge:'挑战',shadow:'影子',canary:'小额',live:'实盘'};
const gateLabels = {pending:'待开始',running:'进行中',go:'GO',no_go:'NO-GO',blocked:'已阻断'};
const stateLabels = {ok:'正常',warning:'注意',error:'异常',unknown:'未上报'};
const runtimeServiceLabels = {forward_collector:'前瞻数据采集',opening_shadow:'开盘 Shadow',research_paper:'Research Paper'};
const runtimeModeLabels = {forward_collection:'实时采集',post_window_shadow:'开盘后 Shadow',paper:'实时模拟'};
const tradeSideLabels = {up:'看涨',down:'看跌'};
const tradeStatusLabels = {insert_pending:'等待生效',working:'挂单中',cancel_pending:'撤单中',fak_pending:'FAK 提交中',partially_filled:'部分成交',filled:'已成交',canceled:'已撤单',recovery_canceled:'重启撤单',rejected:'已拒绝',pending:'待结算',resolved:'已结算',void:'作废'};
const regimeLabels = {early_3s_to_30s:'3–30s',price_discovery_35s_to_90s:'35–90s',mid_early_95s_to_180s:'95–180s',core:'核心价 20–80%',tail_low:'低价尾部 <20%',tail_high:'高价尾部 >80%'};
const history = {cursor:null,variant:'',loaded:0,currency:'USDC',loading:false};

function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
function money(value,currency='USDC',signed=false) { if(!finite(value)) return '—'; const prefix=signed&&value>0?'+':''; return `${prefix}${value.toFixed(2)} ${currency}`; }
function percent(value) { return finite(value) ? `${(value*100).toFixed(1)}%` : '—'; }
function probability(value) { return finite(value) ? value.toFixed(3) : '—'; }
function latency(value) { return finite(value) ? `${value.toFixed(value>=100?0:1)} ms` : '—'; }
function localTime(value) { if(!value) return '—'; const date=new Date(value); return Number.isNaN(date.getTime())?'—':date.toLocaleString('zh-CN',{hour12:false}); }
function ageText(seconds) { if(!finite(seconds)) return '未上报'; if(seconds<1) return '< 1 秒前'; if(seconds<60) return `${Math.round(seconds)} 秒前`; return `${Math.round(seconds/60)} 分钟前`; }
function statusTone(value) { return ['ok','warning','error'].includes(value)?value:'unknown'; }
function pnlTone(value) { return !finite(value)?'neutral':value>0?'positive':value<0?'negative':'neutral'; }
function setMetric(id,value,tone='neutral') { const node=byId(id); node.textContent=value; node.className=`metric-value ${tone}`; }
function readableIdentifier(value) { return String(value||'—').replaceAll('_',' '); }
function runtimeServiceLabel(value) { return runtimeServiceLabels[value]||readableIdentifier(value); }
function runtimeModeLabel(value) { return runtimeModeLabels[value]||readableIdentifier(value); }
function tradeSideLabel(value) { return tradeSideLabels[value]||readableIdentifier(value); }
function tradeStatusLabel(value) { return tradeStatusLabels[value]||readableIdentifier(value); }
function orderStrategyLabel(item) { const variant=readableIdentifier(item.variant_id); return item.execution_epoch?`${variant} · ${readableIdentifier(item.execution_epoch)}`:variant; }

function renderSummary(snapshot) {
  const performance=snapshot&&snapshot.performance; const currency=(performance&&performance.currency)||'USDC';
  history.currency=currency;
  setMetric('metric-equity',money(performance&&performance.equity,currency));
  byId('metric-equity-note').textContent=performance&&finite(performance.starting_balance)?`起始 ${money(performance.starting_balance,currency)}`:'尚无账户快照';
  const total=performance&&finite(performance.realized_pnl)?performance.realized_pnl+(finite(performance.unrealized_pnl)?performance.unrealized_pnl:0):null;
  setMetric('metric-total-pnl',money(total,currency,true),pnlTone(total));
  byId('metric-total-note').textContent=performance?`已实现 ${money(performance.realized_pnl,currency,true)} · 未实现 ${money(performance.unrealized_pnl,currency,true)}`:'已实现 + 未实现';
  setMetric('metric-today-pnl',money(performance&&performance.today_pnl,currency,true),pnlTone(performance&&performance.today_pnl));
  setMetric('metric-drawdown',money(performance&&performance.max_drawdown,currency));
  byId('stat-win-rate').textContent=percent(performance&&performance.win_rate);
  byId('stat-orders').textContent=performance?`${performance.order_count||0} / ${performance.fill_count||0}`:'0 / 0';
  byId('stat-available').textContent=money(performance&&performance.available_balance,currency);
  byId('stat-exposure').textContent=money(performance&&performance.open_exposure,currency);
  renderEquity(performance&&performance.equity_curve,currency);
  renderVariants(performance&&performance.variant_summaries,currency);
  renderFunnel(performance&&performance.decision_funnel,performance&&performance.paper_execution_epoch);
  renderOrders(performance&&performance.recent_orders,currency);
}

function renderVariants(variants,currency) {
  const grid=byId('variant-grid'); grid.replaceChildren();
  if(!(variants||[]).length) { const empty=document.createElement('div'); empty.className='table-empty'; empty.textContent='当前快照没有并行执行策略。'; grid.append(empty); return; }
  for(const item of variants) { const card=document.createElement('article'); card.className=`variant-card ${item.primary?'primary':''}`; const title=document.createElement('div'); title.className='variant-title'; const name=document.createElement('strong'); name.textContent=item.label; const badge=document.createElement('span'); badge.textContent=item.primary?'主策略':'对照'; title.append(name,badge); const policy=document.createElement('div'); policy.className='variant-policy'; policy.textContent=readableIdentifier(item.policy); const values=document.createElement('div'); values.className='variant-values'; for(const [label,value,tone] of [['全样本 PnL',money(item.realized_pnl,currency,true),pnlTone(item.realized_pnl)],['核心 EV / 机会',money(item.core_paired_ev_per_opportunity,currency,true),pnlTone(item.core_paired_ev_per_opportunity)],['全样本 EV / 机会',money(item.paired_ev_per_opportunity,currency,true),pnlTone(item.paired_ev_per_opportunity)],['EV / 成交份额',money(item.conditional_ev_per_filled_share,currency,true),pnlTone(item.conditional_ev_per_filled_share)],['成交率',percent(item.fill_rate),'neutral'],['机会 / 成交',`${item.resolved_opportunity_count||0} / ${item.fill_count||0}`,'neutral'],['核心 / 尾部',`${item.core_resolved_opportunity_count||0} / ${item.tail_resolved_opportunity_count||0}`,'neutral'],['Taker fee',money(item.taker_fees,currency),'neutral']]) { const cell=document.createElement('div'); const caption=document.createElement('span'); caption.textContent=label; const strong=document.createElement('strong'); strong.textContent=value; strong.className=tone; cell.append(caption,strong); values.append(cell); } const segments=document.createElement('div'); segments.className='variant-segments'; const resolved=(item.segment_summaries||[]).filter(segment=>(segment.resolved_count||0)>0); segments.textContent=resolved.length?resolved.map(segment=>`${regimeLabels[segment.key]||readableIdentifier(segment.key)} ${segment.resolved_count}次 · EV ${money(segment.paired_ev_per_opportunity,currency,true)}`).join(' ｜ '):'阶段与价格分层将在机会结算后显示'; card.append(title,policy,values,segments); grid.append(card); }
}

function renderFunnel(funnel,epoch) {
  byId('execution-epoch').textContent=epoch?`同一信号 · epoch ${readableIdentifier(epoch)}`:'同一信号 · 独立模拟账本';
  const track=byId('decision-funnel'); track.replaceChildren(); byId('funnel-scope').textContent=funnel?readableIdentifier(funnel.scope):'等待数据';
  if(!funnel) { const node=document.createElement('div'); node.className='funnel-step'; node.innerHTML='<span>尚未开始</span><strong>—</strong>'; track.append(node); return; }
  const stages=[['决策 tick',funnel.decision_ticks],['有效预测',funnel.predictions],['候选信号',funnel.eligible_signal_ticks],['独立机会',funnel.opportunities],['已提交',funnel.placements],['被拒绝',funnel.rejected],['有成交',funnel.fills],['已结算',funnel.resolved]];
  for(const [label,value] of stages) { const node=document.createElement('div'); node.className='funnel-step'; const caption=document.createElement('span'); caption.textContent=label; const strong=document.createElement('strong'); strong.textContent=String(value||0); node.append(caption,strong); track.append(node); }
}

function svgNode(name,attributes={}) { const node=document.createElementNS('http://www.w3.org/2000/svg',name); for(const [key,value] of Object.entries(attributes)) node.setAttribute(key,String(value)); return node; }
function renderEquity(rawPoints,currency) {
  const points=(rawPoints||[]).filter(item=>item&&finite(item.equity)&&item.timestamp);
  const empty=byId('chart-empty'),wrap=byId('chart-wrap'),svg=byId('equity-chart'); svg.replaceChildren();
  if(points.length<2) { empty.hidden=false; wrap.hidden=true; byId('curve-range').textContent='等待结算记录'; return; }
  empty.hidden=true; wrap.hidden=false;
  const values=points.map(item=>item.equity), min=Math.min(...values), max=Math.max(...values), span=Math.max(max-min,Math.max(max,1)*.005);
  const left=35,right=775,top=20,bottom=192;
  const x=index=>left+(right-left)*(index/Math.max(points.length-1,1)); const y=value=>bottom-(bottom-top)*((value-min)/span);
  const defs=svgNode('defs'),gradient=svgNode('linearGradient',{id:'equity-fill',x1:'0',x2:'0',y1:'0',y2:'1'}); gradient.append(svgNode('stop',{offset:'0%','stop-color':'#c9df8a','stop-opacity':'.2'}),svgNode('stop',{offset:'100%','stop-color':'#c9df8a','stop-opacity':'0'})); defs.append(gradient); svg.append(defs);
  for(const ratio of [0,.5,1]) { const gy=top+(bottom-top)*ratio; svg.append(svgNode('line',{x1:left,x2:right,y1:gy,y2:gy,class:'chart-grid'})); }
  const line=points.map((item,index)=>`${index===0?'M':'L'} ${x(index).toFixed(2)} ${y(item.equity).toFixed(2)}`).join(' ');
  const area=`${line} L ${right} ${bottom} L ${left} ${bottom} Z`;
  svg.append(svgNode('path',{d:area,class:'chart-area'}),svgNode('path',{d:line,class:'chart-line'}),svgNode('circle',{cx:x(points.length-1),cy:y(points.at(-1).equity),r:4,class:'chart-dot'}));
  const maxLabel=svgNode('text',{x:left,y:12,class:'chart-label'}); maxLabel.textContent=money(max,currency);
  const minLabel=svgNode('text',{x:left,y:218,class:'chart-label'}); minLabel.textContent=money(min,currency);
  const lastLabel=svgNode('text',{x:right,y:218,'text-anchor':'end',class:'chart-label'}); lastLabel.textContent=localTime(points.at(-1).timestamp);
  svg.append(maxLabel,minLabel,lastLabel);
  byId('curve-range').textContent=`${localTime(points[0].timestamp)} → ${localTime(points.at(-1).timestamp)}`;
}

function renderLifecycle(strategy) {
  const track=byId('stage-track'); track.replaceChildren(); const activeIndex=strategy?stageOrder.indexOf(strategy.stage):-1;
  stageOrder.forEach((stage,index)=>{ const node=document.createElement('div'); node.className=`stage ${index<activeIndex?'complete':index===activeIndex?'active':''}`; node.textContent=stageLabels[stage]; track.append(node); });
  const gate=strategy?strategy.gate_state:'pending'; byId('gate-state').textContent=strategy?(gateLabels[gate]||gate):'未登记'; byId('gate-state').className=`pill ${gate==='go'?'ok':gate==='no_go'||gate==='blocked'?'error':gate==='running'?'warning':''}`;
  byId('stage-title').textContent=strategy?`${stageLabels[strategy.stage]||strategy.stage}阶段`:'等待周期快照';
  byId('next-action').textContent=strategy?strategy.next_action:'发布流程尚未写入策略周期，页面不会自行猜测当前阶段。';
  const current=strategy&&strategy.progress_current,target=strategy&&strategy.progress_target,ratio=finite(current)&&finite(target)&&target>0?Math.min(1,current/target):0;
  byId('cycle-progress').style.width=`${ratio*100}%`; byId('progress-label').textContent=(strategy&&strategy.progress_label)||'暂无进度目标'; byId('progress-value').textContent=finite(current)&&finite(target)?`${current} / ${target}`:'—';
  byId('champion-model').textContent=(strategy&&strategy.model_id)||'—'; byId('challenger-model').textContent=(strategy&&strategy.challenger_model_id)||'—';
  const challengeCadence=strategy&&strategy.challenger_interval_days?`（每 ${strategy.challenger_interval_days} 天）`:''; const reviewCadence=strategy&&strategy.review_interval_days?`（每 ${strategy.review_interval_days} 天）`:'';
  byId('next-challenge').textContent=strategy&&strategy.next_challenge_at?`${localTime(strategy.next_challenge_at)} ${challengeCadence}`:'—'; byId('next-review').textContent=strategy&&strategy.next_review_at?`${localTime(strategy.next_review_at)} ${reviewCadence}`:'—'; byId('data-cutoff').textContent=localTime(strategy&&strategy.data_cutoff_at);
}

function healthFromRuntime(payload) {
  const items=(payload.statuses||[]).map(item=>({key:`runtime-${item.service}`,label:runtimeServiceLabel(item.service),state:item.health&&item.health.healthy?'ok':'error',detail:`${runtimeModeLabel(item.mode)} · ${item.state==='running'?'运行中':readableIdentifier(item.state)}`,updated_at:item.updated_at,latency_ms:null,age_seconds:item.health&&item.health.age_seconds}));
  const projection=payload.snapshot_health, hasLive=(payload.statuses||[]).some(item=>item.service==='live_operations'), isPaper=payload.snapshot&&payload.snapshot.run_mode==='research_paper';
  if(payload.snapshot||hasLive) items.push({key:'performance-projection',label:isPaper?'模拟资金与盈亏快照':'账户与盈亏快照',state:projection&&projection.healthy?'ok':'error',detail:projection&&projection.healthy?(isPaper?'Research Paper 模拟账本新鲜':'真实账户账本投影新鲜'):`投影不可用 · ${readableIdentifier(projection&&projection.reason||'missing_snapshot')}`,updated_at:payload.snapshot&&payload.snapshot.generated_at||payload.generated_at,latency_ms:null,age_seconds:projection&&projection.age_seconds});
  const shadow=payload.shadow, coverage=shadow&&shadow.coverage;
  if(shadow) items.push({key:'shadow-evidence',label:'Shadow 证据',state:'ok',detail:`${shadow.market_slug} · 合格 ${coverage&&coverage.quality_eligible||0}/${coverage&&coverage.predictions||0} · 未提交订单`,updated_at:payload.generated_at,latency_ms:null,age_seconds:null});
  const collector=(payload.statuses||[]).find(item=>item.service==='forward_collector');
  for(const feed of (collector&&collector.details&&collector.details.feeds)||[]) items.push({key:`feed-${feed.key}`,label:readableIdentifier(feed.source),state:feed.state==='ok'?'ok':'error',detail:`${feed.instrument} · ${readableIdentifier(feed.state)} · gaps ${feed.gap_count||0}`,updated_at:feed.last_event_at||collector.updated_at,latency_ms:null,age_seconds:feed.age_seconds});
  const storage=collector&&collector.details&&collector.details.storage;
  for(const [key,disk] of Object.entries(storage||{})) { const used=disk&&disk.used_percent, free=disk&&disk.free_bytes; items.push({key:`disk-${key}`,label:`Disk ${readableIdentifier(key)}`,state:finite(used)&&used>=95?'error':finite(used)&&used>=85?'warning':'ok',detail:finite(used)&&finite(free)?`${used.toFixed(1)}% used · ${(free/1073741824).toFixed(1)} GiB free`:'disk usage unavailable',updated_at:collector.updated_at,latency_ms:null,age_seconds:null}); }
  return items;
}
function renderHealth(payload,snapshot) {
  const merged=new Map(); for(const item of healthFromRuntime(payload)) merged.set(item.key,item); for(const item of (snapshot&&snapshot.health)||[]) merged.set(item.key,item);
  const grid=byId('health-grid'); grid.replaceChildren();
  if(merged.size===0) { const empty=document.createElement('div'); empty.className='empty'; empty.textContent='尚未收到任何服务或连接状态。'; grid.append(empty); }
  for(const item of merged.values()) { const tone=statusTone(item.state); const card=document.createElement('article'); card.className=`health-card ${tone}`; const top=document.createElement('div'); top.className='health-top'; const label=document.createElement('div'); label.className='health-label'; label.textContent=item.label; const state=document.createElement('div'); state.className='health-state'; const dot=document.createElement('span'); dot.className='dot'; const stateText=document.createElement('span'); stateText.textContent=stateLabels[tone]; state.append(dot,stateText); top.append(label,state); const detail=document.createElement('p'); detail.className='health-detail'; detail.textContent=item.detail||'未提供说明'; const foot=document.createElement('div'); foot.className='health-foot'; const timing=document.createElement('span'); timing.textContent=finite(item.latency_ms)?latency(item.latency_ms):'—'; const age=document.createElement('span'); age.textContent=finite(item.age_seconds)?ageText(item.age_seconds):localTime(item.updated_at); foot.append(timing,age); card.append(top,detail,foot); grid.append(card); }
  const collector=(payload.statuses||[]).find(item=>item.service==='forward_collector'), quality=collector&&collector.details&&collector.details.quality;
  byId('quality-summary').textContent=quality?`接受 ${quality.accepted_events||0} · Gap ${quality.gap_events||0} · Stale ${quality.stale_events||0}`:'未收到数据质量摘要';
}

function renderOrders(orders,currency) {
  const records=[...(orders||[])].sort((a,b)=>String(b.placed_at).localeCompare(String(a.placed_at)));
  renderOrderRows(byId('trade-rows'),records,currency,{emptyText:'尚无订单记录；Shadow 信号不会伪装成成交。'});
}

function renderOrderRows(body,records,currency,{append=false,emptyText='尚无订单记录'}={}) {
  if(!append) body.replaceChildren();
  if(records.length===0&&!append) { const row=document.createElement('tr'),cell=document.createElement('td'); cell.colSpan=9; cell.className='table-empty'; cell.textContent=emptyText; row.append(cell); body.append(row); return; }
  for(const item of records) { const row=document.createElement('tr'); const pnl=item.filled_shares>0?(finite(item.realized_pnl)?item.realized_pnl:item.unrealized_pnl):null; const status=`${tradeStatusLabel(item.execution_status)} / ${tradeStatusLabel(item.settlement_status)}`; const values=[localTime(item.placed_at),orderStrategyLabel(item),item.market_slug,tradeSideLabel(item.side),status,`${finite(item.entry_price)?item.entry_price.toFixed(3):'—'} × ${finite(item.filled_shares)?item.filled_shares.toFixed(2):'—'}`,`${probability(item.p_fair)} / ${probability(item.market_price)}`,money(pnl,currency,true),latency(item.order_latency_ms)]; values.forEach((value,index)=>{ const cell=document.createElement('td'); if(index===2){cell.className='market-cell';cell.title=String(value);} if(index===3){const badge=document.createElement('span');badge.className='side';badge.textContent=String(value);cell.append(badge);}else{cell.textContent=String(value);} if(index===7) cell.classList.add(pnlTone(pnl)); row.append(cell); }); const details=[]; if(item.execution_route) details.push(`执行：${readableIdentifier(item.execution_route)}`); if(item.entry_regime) details.push(`阶段：${regimeLabels[item.entry_regime]||readableIdentifier(item.entry_regime)}`); if(item.price_bucket) details.push(`价格：${regimeLabels[item.price_bucket]||readableIdentifier(item.price_bucket)}${item.go_eligible===false?'（仅研究）':''}`); if(item.terminal_reason) details.push(`终止：${readableIdentifier(item.terminal_reason)}`); if((item.signal_observations||[]).length) details.push(`信号：${item.signal_observations.map(signal=>`#${signal.signal_number} edge ${finite(signal.taker_net_edge)?signal.taker_net_edge.toFixed(3):'—'}`).join(' → ')}`); if(details.length) row.title=details.join(' ｜ '); body.append(row); }
}

function updateHistoryVariants(variants) {
  const select=byId('history-variant'),current=select.value; select.replaceChildren(); const all=document.createElement('option'); all.value=''; all.textContent='全部策略'; select.append(all);
  for(const variant of variants||[]) { const option=document.createElement('option'); option.value=variant; option.textContent=readableIdentifier(variant); select.append(option); }
  if([...select.options].some(option=>option.value===current)) select.value=current;
}

async function loadHistory({reset=false}={}) {
  if(history.loading) return;
  history.loading=true; const more=byId('history-more'),variantSelect=byId('history-variant'); more.disabled=true; variantSelect.disabled=true; byId('history-state').textContent='正在读取完整账本…';
  if(reset) { history.cursor=null; history.loaded=0; }
  const params=new URLSearchParams({limit:'50'}); if(history.cursor) params.set('cursor',history.cursor); if(history.variant) params.set('variant',history.variant);
  try {
    const response=await fetch(`/api/orders?${params}`,{cache:'no-store'}); const payload=await response.json(); if(!response.ok) throw new Error(payload.message||'完整账本读取失败');
    renderOrderRows(byId('history-rows'),payload.items||[],history.currency,{append:!reset,emptyText:'当前筛选没有订单记录。'});
    history.loaded+=Array.isArray(payload.items)?payload.items.length:0; history.cursor=payload.next_cursor||null; updateHistoryVariants(payload.available_variants);
    byId('history-state').textContent=`已加载 ${history.loaded} / ${payload.total_records||0} 条 · 只读模拟账本`;
    more.hidden=!payload.has_more;
  } catch(error) {
    if(reset) renderOrderRows(byId('history-rows'),[],history.currency,{emptyText:'完整历史暂不可用。'});
    byId('history-state').textContent=`读取失败：${error instanceof Error?error.message:'未知错误'}`; more.hidden=true;
  } finally { history.loading=false; more.disabled=false; variantSelect.disabled=false; }
}

byId('history-toggle').addEventListener('click',()=>{ const panel=byId('history-panel'),opening=panel.hidden; panel.hidden=!opening; byId('history-toggle').textContent=opening?'收起完整历史':'查看完整历史'; byId('history-toggle').setAttribute('aria-expanded',String(opening)); if(opening&&history.loaded===0) loadHistory({reset:true}); });
byId('history-more').addEventListener('click',()=>loadHistory());
byId('history-variant').addEventListener('change',event=>{ history.variant=event.target.value; loadHistory({reset:true}); });

function renderAlerts(payload,snapshot) {
  const target=byId('alerts'); target.replaceChildren(); const items=[]; for(const error of payload.errors||[]) items.push({state:'error',message:error}); if(payload.stop_request) items.push({state:'warning',message:`已请求停止：${payload.stop_request.reason}`}); for(const alert of (snapshot&&snapshot.alerts)||[]) items.push(alert);
  for(const item of items.slice(0,4)) { const node=document.createElement('div'); const tone=statusTone(item.state); node.className=`alert ${tone}`; const dot=document.createElement('span'); dot.className='dot'; const text=document.createElement('span'); text.textContent=item.message; node.append(dot,text); target.append(node); }
}

function render(payload) {
  const snapshot=payload.snapshot||null; const snapshotHealth=(snapshot&&snapshot.health)||[]; const staleProjection=snapshot&&(!payload.snapshot_health||!payload.snapshot_health.healthy); const runtimeFailure=(payload.statuses||[]).some(item=>!item.health||!item.health.healthy); const overallState=!payload.health||!payload.health.healthy||runtimeFailure||(payload.errors||[]).length>0||staleProjection||snapshotHealth.some(item=>item.state==='error')?'error':payload.stop_request||snapshotHealth.some(item=>item.state==='warning'||item.state==='unknown')?'warning':'ok';
  const overall=byId('overall'); overall.className=`pill ${overallState}`; overall.replaceChildren(); const dot=document.createElement('span'); dot.className='dot'; const label=document.createElement('span'); label.textContent=overallState==='ok'?'Bot 运行正常':overallState==='warning'?'需要关注':'运行异常'; overall.append(dot,label);
  const mode=byId('run-mode'); mode.textContent=snapshot?String(snapshot.run_mode).replaceAll('_',' ').toUpperCase():payload.shadow?'POST-WINDOW SHADOW':'NO PERFORMANCE SNAPSHOT'; mode.className=`pill ${snapshot||payload.shadow?'ok':''}`;
  const collector=(payload.statuses||[]).find(item=>item.service==='forward_collector'); const market=collector&&collector.details&&(collector.details.active_market||collector.details.scheduled_market); byId('active-market').textContent=market?`当前市场 · ${market}`:'当前市场尚未上报';
  byId('freshness-title').textContent=payload.health&&payload.health.healthy?'运行状态新鲜':'运行状态不可用'; byId('freshness-time').textContent=`刷新 ${localTime(payload.generated_at)} · ${ageText(payload.health&&payload.health.age_seconds)}`;
  byId('snapshot-source').textContent=snapshot?`${snapshot.run_mode==='research_paper'?'Simulated ledger':'Account snapshot'} ${localTime(snapshot.generated_at)}${staleProjection?' · STALE':''}`:payload.shadow?'Shadow evidence · no account PnL':'Runtime status only';
  renderAlerts(payload,snapshot); renderSummary(snapshot); renderLifecycle(snapshot&&snapshot.strategy); renderHealth(payload,snapshot);
}

async function refresh() { try { const response=await fetch('/api/status',{cache:'no-store'}); render(await response.json()); } catch(error) { render({generated_at:new Date().toISOString(),health:{healthy:false,reason:'dashboard_fetch_failed'},statuses:[],errors:[`看板读取失败：${String(error)}`]}); } }
refresh(); setInterval(refresh,5000);
</script>
</body>
</html>
"""
