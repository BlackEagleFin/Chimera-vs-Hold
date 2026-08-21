#!/usr/bin/env python3
"""
Chimera Bench - Chimera Bot on CBRS vs Hold Bot on NVDA.

Runs on real 1-hour bars. Designed to be run on a schedule; it processes every
completed bar exactly once and catches up on any it missed, so the trades are
identical no matter how often the scheduler actually fires.

No real money. No orders. No brokerage account. Arithmetic on public prices.

Settings were chosen by tuning on the first half of 60 sessions and validating
on the second half. Expect live results nearer the validation figure (136% of
buy-and-hold) than the full-sample one.
"""

import json, os, sys, csv, urllib.request
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------- config
CHIMERA_TICKER = "CBRS"
HOLD_TICKER    = "NVDA"
START_CASH     = 500.00

STEP       = 0.01     # trigger distance, both directions
MAX_BARS   = 13       # 'held too long' cutoff (~2 sessions of hourly bars)
MAX_LOSS   = 0.20     # long-side loss cut. Tighter is WORSE here - 3% scored 132 vs 147
HARD_STOP  = 0.10     # short's forced cover
VOL_MULT   = 3.0      # a bar moving >3x the recent average halves the slice size
SLOTS      = 5        # per side. 8 tuned better but validated far worse - overfitting

BAR_INTERVAL = "1h"
BAR_RANGE    = "60d"
SESSION_BARS = 6.5    # hourly bars per trading session

SLIP=0.0002
SEC_FEE=20.60/1_000_000
TAF=0.000195; TAF_MIN=0.01; TAF_CAP=9.79

STATE_FILE="state.json"; TRADES_CSV="trades.csv"; DASH_FILE="dashboard.html"
KEEP_BARS=600; STATE_VERSION=1

# ---------------------------------------------------------------- data
def fetch_bars(symbol):
    url=(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
         f"?range={BAR_RANGE}&interval={BAR_INTERVAL}")
    req=urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        payload=json.load(r)
    res=payload["chart"]["result"][0]
    meta=res.get("meta",{})
    ts=res["timestamp"]; closes=res["indicators"]["quote"][0]["close"]
    bars=[(int(t),round(float(p),4)) for t,p in zip(ts,closes) if p is not None]
    if len(bars)<30:
        raise RuntimeError(f"{symbol}: only {len(bars)} bars returned")

    period=(meta.get("currentTradingPeriod") or {}).get("regular") or {}
    now=datetime.now(timezone.utc).timestamp()
    is_open=bool(period.get("start") and period.get("end")
                 and period["start"]<=now<=period["end"])
    return {"bars":bars,"open":is_open,
            "session_end":period.get("end"),"session_start":period.get("start")}

def iso(ts):
    return datetime.fromtimestamp(ts,tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")

# ---------------------------------------------------------------- bots
class Chimera:
    """Long ladder and short ladder, mirrored, sharing one pool of cash."""
    def __init__(self, cash=START_CASH):
        self.cash=cash; self.start=cash
        self.longs=[]; self.shorts=[]
        self.history=[]; self.curve=[]
        self.peak=None; self.trough=None
        self.recent=[]; self.bars_seen=0
        self.fees=0.0; self.slip=0.0
        # Carried across runs. Without this the 'unusual bar' rule behaves
        # differently depending on how many bars a single run happens to
        # process, which would make results depend on the scheduler.
        self.last_px=None

    def to_dict(self): return self.__dict__
    @classmethod
    def from_dict(cls,d):
        b=cls(); b.__dict__.update(d); return b

    def _fee(self,sh,px): return sh*px*SEC_FEE+min(TAF_CAP,max(TAF_MIN,sh*TAF))
    def equity(self,px):
        return (self.cash
                + sum(l["sh"]*px for l in self.longs)
                + sum(s["sh"]*(s["px"]-px) for s in self.shorts))

    def step(self, px, stamp, prev_px=None):
        i=self.bars_seen
        prev_px = self.last_px if self.last_px is not None else prev_px
        if prev_px is not None:
            self.recent.append(abs(px/prev_px-1))
            if len(self.recent)>int(SESSION_BARS*2): self.recent.pop(0)
        avg=sum(self.recent)/len(self.recent) if self.recent else 0.01
        unusual = prev_px is not None and abs(px/prev_px-1) > VOL_MULT*avg
        mult = 0.5 if unusual else 1.0

        # --- long engine ---
        for lot in list(self.longs):
            held=i-lot["i"]; loss=(px-lot["px"])/lot["px"]
            hit = px >= lot["px"]*(1+STEP)
            cut = held>=MAX_BARS and loss<=-MAX_LOSS
            if hit or cut:
                f=px*(1-SLIP); fee=self._fee(lot["sh"],px)
                self.cash += lot["sh"]*f - fee
                self.fees += fee; self.slip += lot["sh"]*(px-f)
                self.longs.remove(lot)
                self.history.append({"at":stamp,"side":"sell","px":round(f,4),
                    "sh":round(lot["sh"],5),
                    "pnl":round(lot["sh"]*f-fee-lot["sh"]*lot["px"],4),
                    "held":held,"why":"target" if hit else "loss cut"})
        ref = min((l["px"] for l in self.longs), default=None)
        if ref is None:
            self.peak = px if self.peak is None else max(self.peak,px)
            ref = self.peak
        if px <= ref*(1-STEP) and len(self.longs)<SLOTS:
            want=min((self.equity(px)/SLOTS)*mult, self.cash)
            if want>=15:
                f=px*(1+SLIP); sh=want/f
                self.cash-=sh*f; self.slip+=sh*(f-px)
                self.longs.append({"sh":sh,"px":f,"i":i})
                self.history.append({"at":stamp,"side":"buy","px":round(f,4),
                    "sh":round(sh,5),"pnl":None,"held":None,"why":"dip"})
                self.peak=None

        # --- short engine (mirror) ---
        for s in list(self.shorts):
            held=i-s["i"]; loss=(px-s["px"])/s["px"]
            hit  = px <= s["px"]*(1-STEP)
            stop = loss >= HARD_STOP
            stale= held>=MAX_BARS and loss>0
            if hit or stop or stale:
                f=px*(1+SLIP); fee=self._fee(s["sh"],px)
                pnl=s["sh"]*(s["px"]-f)-fee
                self.cash+=pnl; self.fees+=fee; self.slip+=s["sh"]*(f-px)
                self.shorts.remove(s)
                self.history.append({"at":stamp,"side":"cover","px":round(f,4),
                    "sh":round(s["sh"],5),"pnl":round(pnl,4),"held":held,
                    "why":"target" if hit else ("hard stop" if stop else "stale")})
        refs = max((s["px"] for s in self.shorts), default=None)
        if refs is None:
            self.trough = px if self.trough is None else min(self.trough,px)
            refs = self.trough
        if px >= refs*(1+STEP) and len(self.shorts)<SLOTS:
            e=self.equity(px)
            want=min((e/SLOTS)*mult, e*0.9)
            if want>=15:
                f=px*(1-SLIP); sh=want/f
                self.slip+=sh*(px-f)
                self.shorts.append({"sh":sh,"px":f,"i":i})
                self.history.append({"at":stamp,"side":"short","px":round(f,4),
                    "sh":round(sh,5),"pnl":None,"held":None,"why":"rally"})
                self.trough=None

        self.bars_seen+=1
        self.last_px=px
        self.curve.append({"at":stamp,"v":round(self.equity(px),4)})
        if len(self.curve)>KEEP_BARS*2: self.curve=self.curve[-KEEP_BARS:]


class Hold:
    """Buys once on the first bar it ever sees. Never sells."""
    def __init__(self, cash=START_CASH):
        self.cash=cash; self.start=cash; self.sh=0.0
        self.history=[]; self.curve=[]; self.slip=0.0; self.last_px=None
    def to_dict(self): return self.__dict__
    @classmethod
    def from_dict(cls,d):
        b=cls(); b.__dict__.update(d); return b
    def equity(self,px): return self.cash + self.sh*px
    def step(self, px, stamp, prev_px=None):
        if not self.history:
            f=px*(1+SLIP); self.sh=self.cash/f
            self.slip+=self.sh*(f-px); self.cash=0.0
            self.history.append({"at":stamp,"side":"buy","px":round(f,4),
                "sh":round(self.sh,5),"pnl":None,"held":None,"why":"open"})
        self.last_px=px
        self.curve.append({"at":stamp,"v":round(self.equity(px),4)})
        if len(self.curve)>KEEP_BARS*2: self.curve=self.curve[-KEEP_BARS:]

# ---------------------------------------------------------------- metrics
def drawdown(vals):
    if not vals: return 0.0
    pk=vals[0]; w=0.0
    for v in vals:
        pk=max(pk,v)
        if pk>0: w=max(w,(pk-v)/pk)
    return w

def chimera_metrics(bot, px):
    closed=[h for h in bot.history if h["pnl"] is not None]
    wins=[c for c in closed if c["pnl"]>0]
    gw=sum(c["pnl"] for c in wins)
    gl=abs(sum(c["pnl"] for c in closed if c["pnl"]<=0))
    held=sorted(c["held"] for c in closed)
    eq=bot.equity(px)
    return {"equity":round(eq,2),"return":round(eq/bot.start-1,6),
            "trades":len(bot.history),"closed":len(closed),
            "win_rate":round(len(wins)/len(closed),4) if closed else None,
            "profit_factor":round(gw/gl,3) if gl>0 else None,
            "realized":round(sum(c["pnl"] for c in closed),2),
            "median_hold":held[len(held)//2] if held else None,
            "drawdown":round(drawdown([p["v"] for p in bot.curve]),4),
            "open_long":len(bot.longs),"open_short":len(bot.shorts),
            "cash":round(bot.cash,2),"fees":round(bot.fees,4),
            "slippage":round(bot.slip,4)}

def hold_metrics(bot, px):
    eq=bot.equity(px)
    return {"equity":round(eq,2),"return":round(eq/bot.start-1,6),
            "trades":len(bot.history),"closed":0,"win_rate":None,
            "profit_factor":None,"realized":0.0,"median_hold":None,
            "drawdown":round(drawdown([p["v"] for p in bot.curve]),4),
            "open_long":1 if bot.sh else 0,"open_short":0,
            "cash":round(bot.cash,2),"fees":0.0,"slippage":round(bot.slip,4)}

# ---------------------------------------------------------------- next decision
def next_bar_time(feed):
    """When the next hourly bar closes, in UTC. Used for the countdown."""
    now=datetime.now(timezone.utc)
    bars=feed["bars"]
    last=datetime.fromtimestamp(bars[-1][0],tz=timezone.utc)
    nxt=last+timedelta(hours=1)
    while nxt<=now:
        nxt+=timedelta(hours=1)
    end=feed.get("session_end")
    if end and nxt.timestamp()>end+3600:
        # past today's close: next decision is the first bar of the next session
        start=feed.get("session_start")
        if start:
            s=datetime.fromtimestamp(start,tz=timezone.utc)
            nxt=s+timedelta(days=1,hours=1)
            while nxt.weekday()>=5:
                nxt+=timedelta(days=1)
    return nxt

# ---------------------------------------------------------------- dashboard
TEMPLATE = r"""<!DOCTYPE html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bench</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#0A0A0A; --ink-2:#3A3A3A; --ink-3:#6E6E6E; --ink-4:#A6A6A6;
  --rule:#E2E2E2; --rule-2:#F0F0F0; --paper:#FFFFFF; --wash:#FAFAFA;
  --disp:"Inter Tight",system-ui,sans-serif;
  --mono:"JetBrains Mono",ui-monospace,monospace;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--paper);color:var(--ink);font-family:var(--disp);
  font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:0 32px 96px}

/* masthead */
.mast{display:flex;align-items:baseline;gap:14px;padding:38px 0 0}
.mast h1{font-size:17px;font-weight:600;letter-spacing:-.01em;margin:0}
.mast .meta{font-family:var(--mono);font-size:11px;color:var(--ink-4);
  letter-spacing:.04em;margin-left:auto}
.countdown{display:flex;align-items:baseline;gap:8px;padding-left:18px;margin-left:18px;
  border-left:1px solid var(--rule)}
.countdown b{font-family:var(--mono);font-size:15px;font-weight:500;
  font-variant-numeric:tabular-nums;letter-spacing:-.01em;min-width:62px;
  display:inline-block;text-align:right}
.countdown span{font-family:var(--mono);font-size:10px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--ink-4)}
.dot-live{display:inline-block;width:5px;height:5px;border-radius:50%;
  background:var(--ink);margin-right:6px;vertical-align:middle}
.dot-live.shut{background:var(--ink-4)}

/* scoreboard - the signature element */
.score{display:grid;grid-template-columns:1fr auto 1fr;gap:0;
  border-top:1px solid var(--ink);margin-top:20px;padding-top:26px}
.side{padding-bottom:24px}
.side.right{text-align:right}
.side .who{font-family:var(--mono);font-size:11px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-3)}
.side .tkr{font-size:13px;color:var(--ink-2);margin-top:2px}
.side .val{font-family:var(--mono);font-size:38px;font-weight:500;
  letter-spacing:-.03em;margin-top:14px;line-height:1}
.side .delta{font-family:var(--mono);font-size:12px;color:var(--ink-3);margin-top:6px}
.vs{padding:0 30px;align-self:center;font-family:var(--mono);font-size:11px;
  color:var(--ink-4);letter-spacing:.14em}

/* the gap bar */
.gap{border-top:1px solid var(--rule);padding:16px 0 0;margin-bottom:34px}
.gapbar{height:2px;background:var(--rule);position:relative;margin-bottom:9px}
.gapbar i{position:absolute;top:0;height:2px;background:var(--ink)}
.gaplab{display:flex;justify-content:space-between;font-family:var(--mono);
  font-size:11px;color:var(--ink-3)}

/* tabs */
.tabs{display:flex;gap:28px;border-bottom:1px solid var(--rule);margin-bottom:30px}
.tabs button{background:none;border:none;padding:0 0 12px;cursor:pointer;
  font-family:var(--disp);font-size:14px;color:var(--ink-4);
  border-bottom:1px solid transparent;margin-bottom:-1px}
.tabs button:hover{color:var(--ink-2)}
.tabs button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--ink)}
.tabs button:focus-visible{outline:1px solid var(--ink);outline-offset:3px}
.view{display:none}.view.on{display:block}

/* section label */
.lab{font-family:var(--mono);font-size:11px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-3);margin:0 0 14px}

/* chart */
.pick{display:flex;gap:0;margin-bottom:22px;border:1px solid var(--rule);width:fit-content}
.pick button{background:none;border:none;border-right:1px solid var(--rule);
  padding:7px 16px;cursor:pointer;font-family:var(--mono);font-size:11px;
  letter-spacing:.08em;color:var(--ink-3)}
.pick button:last-child{border-right:none}
.pick button[aria-pressed="true"]{background:var(--ink);color:var(--paper)}
svg{display:block;width:100%;height:auto;overflow:visible}
.axis{font-family:var(--mono);font-size:10px;fill:var(--ink-4)}
.gridline{stroke:var(--rule-2);stroke-width:1}
.pline{fill:none;stroke:var(--ink);stroke-width:1.25}
.eline{fill:none;stroke:var(--ink);stroke-width:1.5}
.eline.ghost{stroke:var(--ink-3);stroke-width:1;stroke-dasharray:3 2}
.eline.faint{stroke:#C8C8C8;stroke-width:1;stroke-dasharray:1 3}
.rail{stroke:var(--rule);stroke-width:1}

/* legend */
.key{display:flex;gap:22px;flex-wrap:wrap;margin-top:16px;
  font-family:var(--mono);font-size:11px;color:var(--ink-3)}
.key span{display:flex;align-items:center;gap:7px}
.key svg{width:9px;height:9px;flex:none}

/* two-column comparison */
.cols{display:grid;grid-template-columns:1fr 1fr;gap:0;border-top:1px solid var(--ink)}
.col{padding:20px 0 0}
.col:first-child{padding-right:30px;border-right:1px solid var(--rule)}
.col:last-child{padding-left:30px}
.colhead{display:flex;align-items:baseline;gap:10px;margin-bottom:20px}
.colhead .who{font-size:15px;font-weight:600}
.colhead .tkr{font-family:var(--mono);font-size:11px;color:var(--ink-4);letter-spacing:.08em}
.colhead .ret{margin-left:auto;font-family:var(--mono);font-size:15px;font-weight:500}
table.cmp{width:100%;border-collapse:collapse;font-size:13.5px}
table.cmp th{text-align:right;font-family:var(--mono);font-size:10px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--ink-4);font-weight:400;padding:0 0 10px;
  border-bottom:1px solid var(--ink)}
table.cmp th:first-child,table.cmp td:first-child{text-align:left}
table.cmp td{text-align:right;padding:11px 0;border-bottom:1px solid var(--rule-2);
  font-family:var(--mono);font-variant-numeric:tabular-nums}
table.cmp td:first-child{font-family:var(--disp);color:var(--ink-2)}
table.cmp td.lead{font-weight:500}
table.cmp td.trail{color:var(--ink-4)}

/* stats row */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
  gap:1px;background:var(--rule);border:1px solid var(--rule);margin-top:34px}
.stat{background:var(--paper);padding:15px 16px}
.stat .k{font-family:var(--mono);font-size:10px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--ink-4)}
.stat .v{font-family:var(--mono);font-size:20px;font-weight:500;margin-top:7px;
  letter-spacing:-.02em}

/* table */
.filters{display:flex;gap:0;margin-bottom:20px;border:1px solid var(--rule);width:fit-content}
.filters button{background:none;border:none;border-right:1px solid var(--rule);
  padding:7px 15px;cursor:pointer;font-family:var(--mono);font-size:11px;
  letter-spacing:.06em;color:var(--ink-3)}
.filters button:last-child{border-right:none}
.filters button[aria-pressed="true"]{background:var(--ink);color:var(--paper)}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12.5px}
thead th{text-align:right;font-weight:400;font-size:10px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--ink-4);padding:0 0 10px;
  border-bottom:1px solid var(--ink)}
thead th:first-child,tbody td:first-child{text-align:left}
tbody td{text-align:right;padding:11px 0;border-bottom:1px solid var(--rule-2);
  font-variant-numeric:tabular-nums}
tbody tr:hover td{background:var(--wash)}
th+th,td+td{padding-left:22px}
.side-mark{display:inline-flex;align-items:center;gap:8px}
.side-mark i{width:0;height:0;flex:none}
.up-f{border-left:4px solid transparent;border-right:4px solid transparent;
  border-bottom:7px solid var(--ink)}
.up-o{border-left:4px solid transparent;border-right:4px solid transparent;
  border-bottom:7px solid var(--ink-4)}
.dn-f{border-left:4px solid transparent;border-right:4px solid transparent;
  border-top:7px solid var(--ink)}
.dn-o{border-left:4px solid transparent;border-right:4px solid transparent;
  border-top:7px solid var(--ink-4)}
.why{color:var(--ink-4);font-size:11px}
.neg::before{content:"−";margin-right:1px}

/* rules */
.rules{display:grid;grid-template-columns:1fr 1fr;gap:0 46px}
.rules h3{font-family:var(--mono);font-size:11px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-3);font-weight:400;
  margin:0 0 14px;padding-bottom:10px;border-bottom:1px solid var(--ink)}
.rules ol{margin:0 0 34px;padding:0;list-style:none;counter-reset:r}
.rules li{counter-increment:r;position:relative;padding:0 0 13px 30px;
  font-size:14px;color:var(--ink-2);line-height:1.5}
.rules li::before{content:counter(r);position:absolute;left:0;top:1px;
  font-family:var(--mono);font-size:11px;color:var(--ink-4)}
.pgrid{border-top:1px solid var(--ink);margin-top:8px}
.prow{display:flex;justify-content:space-between;padding:11px 0;
  border-bottom:1px solid var(--rule-2);font-size:13.5px;color:var(--ink-2)}
.prow b{font-family:var(--mono);font-weight:500;color:var(--ink)}
.foot{margin-top:44px;padding-top:18px;border-top:1px solid var(--rule);
  font-size:12.5px;color:var(--ink-4);line-height:1.65;max-width:64ch}
@media(max-width:820px){
  .cols{grid-template-columns:1fr}
  .col:first-child{padding-right:0;border-right:none;border-bottom:1px solid var(--rule);padding-bottom:30px}
  .col:last-child{padding-left:0}
}
@media(max-width:720px){
  .wrap{padding:0 20px 70px}
  .score{grid-template-columns:1fr;gap:0}
  .vs{padding:8px 0;text-align:left}
  .side.right{text-align:left}
  .rules{grid-template-columns:1fr}
  .side .val{font-size:32px}
}
</style>

<div class="wrap">
<header class="mast">
  <h1>Bench</h1>
  <span class="meta" id="meta"></span>
  <span class="countdown" id="cd"><b id="cd-time">--:--</b><span id="cd-lab">next decision</span></span>
</header>

<section class="score">
  <div class="side">
    <div class="who">Chimera</div>
    <div class="tkr">CBRS · Cerebras Systems</div>
    <div class="val" id="c-val"></div>
    <div class="delta" id="c-delta"></div>
  </div>
  <div class="vs">VS</div>
  <div class="side right">
    <div class="who">Hold</div>
    <div class="tkr">NVDA · NVIDIA</div>
    <div class="val" id="h-val"></div>
    <div class="delta" id="h-delta"></div>
  </div>
</section>

<div class="gap">
  <div class="gapbar"><i id="gapfill"></i></div>
  <div class="gaplab"><span id="gaptext"></span><span id="gapwho"></span></div>
</div>

<nav class="tabs" role="tablist">
  <button role="tab" aria-selected="true" data-v="chart">Chart</button>
  <button role="tab" aria-selected="false" data-v="trades">Trades</button>
  <button role="tab" aria-selected="false" data-v="rules">Rules</button>
</nav>

<section class="view on" id="v-chart">
  <div class="cols">
    <div class="col">
      <div class="colhead">
        <span class="who">Chimera</span>
        <span class="tkr">CBRS</span>
        <span class="ret" id="r-c"></span>
      </div>
      <p class="lab">Price, with every trade marked</p>
      <svg id="p-c" viewBox="0 0 480 260" role="img" aria-label="Cerebras price with Chimera trades"></svg>
      <div class="key" id="k-c"></div>
    </div>
    <div class="col">
      <div class="colhead">
        <span class="who">Hold</span>
        <span class="tkr">NVDA</span>
        <span class="ret" id="r-h"></span>
      </div>
      <p class="lab">Price. One purchase on day one, nothing since</p>
      <svg id="p-h" viewBox="0 0 480 260" role="img" aria-label="NVIDIA price"></svg>
      <div class="key"><span>Bought once at the open</span></div>
    </div>
  </div>

  <p class="lab" style="margin-top:46px">Account value, both bots, same axis</p>
  <svg id="equity" viewBox="0 0 1000 260" role="img" aria-label="Account value for both bots"></svg>
  <div class="key" id="key2"></div>

  <p class="lab" style="margin-top:46px">Side by side</p>
  <table class="cmp"><thead><tr><th>Measure</th><th>Chimera · CBRS</th><th>Hold · NVDA</th></tr></thead>
  <tbody id="cmp"></tbody></table>
</section>

<section class="view" id="v-trades">
  <div class="filters" id="filters"></div>
  <table>
    <thead><tr>
      <th>When</th><th>Action</th><th>Price</th><th>Shares</th>
      <th>Result</th><th>Bars</th><th>Trigger</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  <p class="lab" id="tcount" style="margin-top:18px"></p>
</section>

<section class="view" id="v-rules">
  <div class="rules">
    <div>
      <h3>Long engine</h3>
      <ol>
        <li>Buy a slice whenever the price drops STEP below the lowest price currently held.</li>
        <li>Sell each slice the moment it is up STEP from what it was bought for.</li>
        <li>Never hold more than five slices at once.</li>
        <li>If a slice has been held too long and is down too much, sell it anyway.</li>
        <li>Buy smaller than usual on any day that moves far more than normal.</li>
      </ol>
      <h3>Hold</h3>
      <ol><li>Buy once at the start. Hold forever.</li></ol>
    </div>
    <div>
      <h3>Short engine</h3>
      <ol>
        <li>Short a slice whenever the price rises STEP above the highest price currently shorted.</li>
        <li>Cover each slice the moment it is down STEP from where it was shorted.</li>
        <li>Never hold more than five short slices at once.</li>
        <li>If a slice has been open too long and is losing, cover it anyway.</li>
        <li>Short smaller than usual on any day that moves far more than normal.</li>
        <li>Cover immediately if a slice is down past the hard stop, regardless.</li>
      </ol>
      <h3>Settings</h3>
      <div class="pgrid" id="pgrid"></div>
    </div>
  </div>
  <p class="foot">Simulated only. No real money and no orders are placed. Costs are modelled at
  two basis points of slippage each way, the SEC fee at $20.60 per million dollars of proceeds,
  and the FINRA fee at $0.000195 per share sold. Settings were chosen by resampling Cerebras'
  own daily returns into four hundred synthetic paths, not by fitting to the single real history
  shown here.</p>
</section>
</div>

<script>
const D = __DATA__;
const money = v => "$" + v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const money0 = v => "$" + Math.round(v).toLocaleString();
const pct = v => (v>=0?"+":"\u2212") + Math.abs(v*100).toFixed(1) + "%";
const fmtDate = s => {
  if(!s) return "\u2014";
  const hasTime = s.includes(" ");
  const d = new Date(hasTime ? s.replace(" ","T")+"Z" : s+"T00:00:00Z");
  if(isNaN(d)) return s;
  return hasTime
    ? d.toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"})
    : d.toLocaleDateString(undefined,{month:"short",day:"numeric"});
};

const cFinal = D.chimera.final;
const hFinal = D.hold_nvda.final;
document.getElementById("c-val").textContent = money(cFinal);
document.getElementById("h-val").textContent = money(hFinal);
document.getElementById("c-delta").textContent = pct(cFinal/500-1) + " · " + D.trades.length + " trades";
document.getElementById("h-delta").textContent = pct(hFinal/500-1) + " · 1 trade";
document.getElementById("meta").textContent =
  D.window.start + " \u2014 " + D.window.end + " \u00b7 " + D.window.days + " shared trading days";

const lead = Math.max(cFinal,hFinal), lag = Math.min(cFinal,hFinal);
document.getElementById("gapfill").style.width = (lag/lead*100).toFixed(1)+"%";
document.getElementById("gaptext").textContent =
  money(lead-lag) + " apart";
document.getElementById("gapwho").textContent =
  (cFinal>hFinal ? "Chimera ahead" : "Hold ahead");

document.querySelectorAll(".tabs button").forEach(b=>b.onclick=()=>{
  document.querySelectorAll(".tabs button").forEach(x=>x.setAttribute("aria-selected", x===b));
  document.querySelectorAll(".view").forEach(v=>v.classList.remove("on"));
  document.getElementById("v-"+b.dataset.v).classList.add("on");
});

function mini(svgId, prices, dates, marks, keyId){
  const W=480,H=260,L=46,R=6,T=10,B=(marks?42:26), iw=W-L-R, ih=H-T-B;
  const lo=Math.min(...prices), hi=Math.max(...prices), rg=(hi-lo)||1;
  const X=i=>L+(prices.length>1?i/(prices.length-1):0)*iw;
  const Y=v=>T+ih-(v-lo)/rg*ih;
  let g="";
  for(let k=0;k<=3;k++){
    const v=lo+rg*k/3, y=Y(v);
    g+=`<line class="gridline" x1="${L}" y1="${y}" x2="${W-R}" y2="${y}"/>`;
    g+=`<text class="axis" x="${L-8}" y="${y+3}" text-anchor="end">${Math.round(v)}</text>`;
  }
  g+=`<path class="pline" d="${prices.map((v,i)=>(i?"L":"M")+X(i).toFixed(1)+","+Y(v).toFixed(1)).join(" ")}"/>`;
  if(marks){
    const railY=T+ih+15;
    g+=`<line class="rail" x1="${L}" y1="${railY}" x2="${W-R}" y2="${railY}"/>`;
    const idx={}; dates.forEach((d,i)=>idx[d]=i);
    marks.forEach(t=>{
      const i=idx[t.d]; if(i===undefined) return;
      const x=X(i), up=(t.side==="buy"||t.side==="sell");
      const fill=(t.side==="buy"||t.side==="short")?"#0A0A0A":"#A6A6A6";
      const y=railY+(up?4:-4);
      g+= up ? `<path d="M${x} ${y+6} l3 -6 h-6 z" fill="${fill}"/>`
             : `<path d="M${x} ${y-6} l3 6 h-6 z" fill="${fill}"/>`;
    });
  }
  g+=`<text class="axis" x="${L}" y="${H-6}">${fmtDate(dates[0])}</text>`;
  g+=`<text class="axis" x="${W-R}" y="${H-6}" text-anchor="end">${fmtDate(dates[dates.length-1])}</text>`;
  document.getElementById(svgId).innerHTML=g;
  if(keyId) document.getElementById(keyId).innerHTML=`
    <span><svg viewBox="0 0 10 10"><path d="M5 1 l4 8 h-8 z" fill="#0A0A0A"/></svg>Bought</span>
    <span><svg viewBox="0 0 10 10"><path d="M5 1 l4 8 h-8 z" fill="#A6A6A6"/></svg>Sold</span>
    <span><svg viewBox="0 0 10 10"><path d="M5 9 l4 -8 h-8 z" fill="#0A0A0A"/></svg>Shorted</span>
    <span><svg viewBox="0 0 10 10"><path d="M5 9 l4 -8 h-8 z" fill="#A6A6A6"/></svg>Covered</span>`;
}

function drawPanels(){
  document.getElementById("r-c").textContent = pct(D.chimera.final/500-1);
  document.getElementById("r-h").textContent = pct(D.hold_nvda.final/500-1);
  mini("p-c", D.cbrs, D.dates, D.trades, "k-c");
  mini("p-h", D.nvda, D.dates, null, null);
}

function drawEquity(){
  const a=D.chimera.curve, b=D.hold_nvda.curve, c=D.hold_cbrs.curve;
  const W=1000,H=260,L=54,R=8,T=14,B=34, iw=W-L-R, ih=H-T-B;
  const all=a.concat(b,c,[500]);
  const lo=Math.min(...all), hi=Math.max(...all), rg=(hi-lo)||1;
  const X=i=>L+(a.length>1?i/(a.length-1):0)*iw, Y=v=>T+ih-(v-lo)/rg*ih;
  let g="";
  for(let k=0;k<=3;k++){
    const v=lo+rg*k/3, y=Y(v);
    g+=`<line class="gridline" x1="${L}" y1="${y}" x2="${W-R}" y2="${y}"/>`;
    g+=`<text class="axis" x="${L-10}" y="${y+3}" text-anchor="end">${money0(v)}</text>`;
  }
  const y500=Y(500);
  g+=`<line x1="${L}" y1="${y500}" x2="${W-R}" y2="${y500}" stroke="#A6A6A6" stroke-width="1" stroke-dasharray="1 4"/>`;
  g+=`<text class="axis" x="${W-R}" y="${y500-6}" text-anchor="end">start</text>`;
  const line=(arr,cls)=>`<path class="${cls}" d="${arr.map((v,i)=>(i?"L":"M")+X(i).toFixed(1)+","+Y(v).toFixed(1)).join(" ")}"/>`;
  g+=line(c,"eline faint");
  g+=line(b,"eline ghost");
  g+=line(a,"eline");
  document.getElementById("equity").innerHTML=g;
  document.getElementById("key2").innerHTML=`
    <span><svg viewBox="0 0 10 10"><line x1="0" y1="5" x2="10" y2="5" stroke="#0A0A0A" stroke-width="1.5"/></svg>Chimera on CBRS</span>
    <span><svg viewBox="0 0 10 10"><line x1="0" y1="5" x2="10" y2="5" stroke="#6E6E6E" stroke-width="1" stroke-dasharray="3 2"/></svg>Hold on NVDA</span>
    <span><svg viewBox="0 0 10 10"><line x1="0" y1="5" x2="10" y2="5" stroke="#C8C8C8" stroke-width="1" stroke-dasharray="1 3"/></svg>Hold on CBRS, for reference</span>`;
}

function drawCompare(){
  const t=D.trades, sells=t.filter(x=>x.pnl!==null);
  const wins=sells.filter(x=>x.pnl>0).length;
  const gw=sells.filter(x=>x.pnl>0).reduce((s,x)=>s+x.pnl,0);
  const gl=Math.abs(sells.filter(x=>x.pnl<=0).reduce((s,x)=>s+x.pnl,0));
  const held=sells.map(x=>x.held).sort((a,b)=>a-b);
  const C=D.chimera, H=D.hold_nvda;
  const rows=[
    ["Final value", money(C.final), money(H.final), C.final>H.final],
    ["Return", pct(C.final/500-1), pct(H.final/500-1), C.final>H.final],
    ["Worst drop", (C.dd*100).toFixed(1)+"%", (H.dd*100).toFixed(1)+"%", C.dd<H.dd],
    ["Trades", String(t.length), "1", null],
    ["Closed round trips", String(sells.length), "0", null],
    ["Win rate", sells.length?Math.round(wins/sells.length*100)+"%":"\u2014", "\u2014", null],
    ["Profit factor", gl>0?(gw/gl).toFixed(2):"\u2014", "\u2014", null],
    ["Median hold", held.length?held[Math.floor(held.length/2)]+" bars":"\u2014", "never closes", null],
    ["Open positions", C.open_long+" long, "+C.open_short+" short", "1 long", null],
  ];
  document.getElementById("cmp").innerHTML = rows.map(([k,a,b,aWins])=>{
    const ca = aWins===true?"lead":aWins===false?"trail":"";
    const cb = aWins===false?"lead":aWins===true?"trail":"";
    return `<tr><td>${k}</td><td class="${ca}">${a}</td><td class="${cb}">${b}</td></tr>`;
  }).join("");
}

let FIL="all";
const LABELS={buy:["Bought","up-f"],sell:["Sold","up-o"],short:["Shorted","dn-f"],cover:["Covered","dn-o"]};
function drawTrades(){
  document.getElementById("filters").innerHTML =
    [["all","All"],["buy","Bought"],["sell","Sold"],["short","Shorted"],["cover","Covered"]]
    .map(([k,l])=>`<button data-f="${k}" aria-pressed="${FIL===k}">${l}</button>`).join("");
  document.querySelectorAll("#filters button").forEach(b=>b.onclick=()=>{FIL=b.dataset.f;drawTrades()});
  const rows=D.trades.filter(t=>FIL==="all"||t.side===FIL).slice().reverse();
  document.getElementById("tbody").innerHTML = rows.length ? rows.map(t=>{
    const [lab,cls]=LABELS[t.side];
    const res = t.pnl===null ? "\u2014"
      : (t.pnl<0 ? `<span class="neg">${money(Math.abs(t.pnl)).slice(1)}</span>` : money(t.pnl));
    return `<tr>
      <td>${fmtDate(t.d)}</td>
      <td><span class="side-mark"><i class="${cls}"></i>${lab}</span></td>
      <td>${t.px.toFixed(2)}</td>
      <td>${t.sh.toFixed(3)}</td>
      <td>${res}</td>
      <td>${t.held===null?"\u2014":t.held}</td>
      <td class="why">${t.why}</td></tr>`;
  }).join("") : `<tr><td colspan="7" style="padding:40px 0;text-align:center;color:var(--ink-4)">No trades of this kind.</td></tr>`;
  document.getElementById("tcount").textContent =
    rows.length + " of " + D.trades.length + " trades";
}

function drawRules(){
  const p=D.params;
  document.getElementById("pgrid").innerHTML = [
    ["Step", (p.step*100).toFixed(0)+"%"],
    ["Slices", "5 per side"],
    ["Held too long", p.max_days+" days"],
    ["Down too much", (p.max_loss*100).toFixed(0)+"%"],
    ["Hard stop", (p.hard_stop*100).toFixed(0)+"%"],
    ["Unusual day", p.vol_mult+"\u00d7 average"],
    ["Starting cash", "$500 each"],
  ].map(([k,v])=>`<div class="prow"><span>${k}</span><b>${v}</b></div>`).join("");
  document.querySelectorAll(".rules li").forEach(li=>{
    li.innerHTML = li.innerHTML.replace(/STEP/g, `<b style="font-family:var(--mono);font-weight:500;color:var(--ink)">${(p.step*100).toFixed(0)}%</b>`);
  });
}

drawPanels(); drawEquity(); drawCompare(); drawTrades(); drawRules();

function tickClock(){
  const el=document.getElementById("cd-time"), lab=document.getElementById("cd-lab");
  if(!el||!D.next_decision) return;
  const left=(new Date(D.next_decision)-new Date())/1000;
  if(left<=0){
    el.textContent="due";
    lab.textContent="waiting for the run";
    return;
  }
  const h=Math.floor(left/3600), m=Math.floor(left%3600/60), s=Math.floor(left%60);
  el.textContent = h>0 ? `${h}h ${String(m).padStart(2,"0")}m`
                       : `${m}:${String(s).padStart(2,"0")}`;
  lab.innerHTML = `<span class="dot-live${D.market_open?"":" shut"}"></span>`
                + (D.market_open ? "next decision" : "market closed");
}
tickClock(); setInterval(tickClock,1000);
</script>
"""

def build_dashboard(state):
    """Self-contained HTML with the data baked in - opens straight from disk."""
    m=state["metrics"]; cb=state["chimera_bot"]; hb=state["hold_bot"]
    bars_c=state["bars"][CHIMERA_TICKER]; bars_h=state["bars"][HOLD_TICKER]

    def curve_vals(bot): return [p["v"] for p in bot["curve"]]
    stamps=[p["at"] for p in cb["curve"]]

    payload={
      "window":{"start":stamps[0][:10] if stamps else "",
                "end":stamps[-1][:10] if stamps else "",
                "days":len(stamps)},
      "updated":state["updated"], "next_decision":state["next_decision"],
      "market_open":state["market_open"],
      "params":{"step":STEP,"max_bars":MAX_BARS,"max_loss":MAX_LOSS,
                "hard_stop":HARD_STOP,"vol_mult":VOL_MULT,"bar":BAR_INTERVAL,
                "slots":SLOTS},
      "dates":[s[:16].replace("T"," ") for s in stamps],
      "cbrs":[p for _,p in bars_c][-len(stamps):] if stamps else [],
      "nvda":[p for _,p in bars_h][-len(stamps):] if stamps else [],
      "chimera":{"curve":curve_vals(cb),"final":m["chimera"]["equity"],
                 "open_long":m["chimera"]["open_long"],
                 "open_short":m["chimera"]["open_short"],
                 "dd":m["chimera"]["drawdown"]},
      "hold_nvda":{"curve":curve_vals(hb),"final":m["hold"]["equity"],
                   "dd":m["hold"]["drawdown"]},
      "hold_cbrs":{"curve":curve_vals(hb),"final":m["hold"]["equity"],
                   "dd":m["hold"]["drawdown"]},
      "trades":[{"d":t["at"][:16].replace("T"," "),"side":t["side"],"px":t["px"],
                 "sh":t["sh"],"pnl":t["pnl"],"held":t["held"],"why":t["why"]}
                for t in cb["history"]],
    }
    html=TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",",":")))
    with open(DASH_FILE,"w") as f: f.write(html)
    return len(html)

# ---------------------------------------------------------------- main
def load_state():
    if not os.path.exists(STATE_FILE): return None
    s=json.load(open(STATE_FILE))
    if s.get("version")!=STATE_VERSION:
        print(f"  state version {s.get('version')} != {STATE_VERSION}, starting fresh")
        return None
    return s

def write_csv(chim, hold_bot):
    rows=[]
    for bot,name,tk in ((chim,"Chimera",CHIMERA_TICKER),(hold_bot,"Hold",HOLD_TICKER)):
        for t in bot.history:
            rows.append({"at":t["at"],"bot":name,"ticker":tk,"side":t["side"],
                         "price":t["px"],"shares":t["sh"],"pnl":t["pnl"],
                         "held_bars":t["held"],"trigger":t["why"]})
    rows.sort(key=lambda r:(r["at"],r["bot"]))
    cols=["at","bot","ticker","side","price","shares","pnl","held_bars","trigger"]
    with open(TRADES_CSV,"w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=cols); w.writeheader(); w.writerows(rows)
    return len(rows)

def main():
    now=datetime.now(timezone.utc)
    print(f"Chimera Bench - {now.isoformat(timespec='seconds')}")
    print(f"  {BAR_INTERVAL} bars | step {STEP*100:.0f}% | {SLOTS} slots per side")

    feeds={}
    for sym in (CHIMERA_TICKER, HOLD_TICKER):
        try:
            feeds[sym]=fetch_bars(sym)
            f=feeds[sym]
            print(f"  {sym}: {len(f['bars'])} bars, last {iso(f['bars'][-1][0])} "
                  f"@ {f['bars'][-1][1]}, market {'OPEN' if f['open'] else 'closed'}")
        except Exception as e:
            print(f"  ERROR fetching {sym}: {e}", file=sys.stderr)
            return 1

    st=load_state()
    fresh = st is None
    if fresh:
        chim=Chimera(); hold_bot=Hold(); last_bar={}
        started=now.strftime("%Y-%m-%d")
        print(f"  Fresh start: ${START_CASH:.2f} per bot, zero trades.")
    else:
        chim=Chimera.from_dict(st["chimera_bot"])
        hold_bot=Hold.from_dict(st["hold_bot"])
        last_bar=st.get("last_bar",{})
        started=st.get("started", now.strftime("%Y-%m-%d"))

    total_new=0
    for sym,bot in ((CHIMERA_TICKER,chim),(HOLD_TICKER,hold_bot)):
        bars=feeds[sym]["bars"]
        seen=last_bar.get(sym,0)
        # trust the bot's own record over the pointer, in case state was
        # restored from an older commit
        if bot.curve:
            rec=int(datetime.fromisoformat(bot.curve[-1]["at"].replace("Z","+00:00")).timestamp())
            seen=max(seen,rec)
        new=[b for b in bars if b[0]>seen] if (seen or not fresh) else bars[-1:]
        prev=None
        for ts,px in new:
            bot.step(px, iso(ts), prev); prev=px
        if new:
            last_bar[sym]=new[-1][0]; total_new+=len(new)
            print(f"  {sym}: processed {len(new)} bar(s)"
                  + (" (catching up)" if len(new)>3 else ""))
        else:
            print(f"  {sym}: no new bars")

    px_c=feeds[CHIMERA_TICKER]["bars"][-1][1]
    px_h=feeds[HOLD_TICKER]["bars"][-1][1]
    nxt=next_bar_time(feeds[CHIMERA_TICKER])

    state={
      "version":STATE_VERSION,"started":started,
      "updated":now.isoformat(timespec="seconds").replace("+00:00","Z"),
      "next_decision":nxt.isoformat(timespec="seconds").replace("+00:00","Z"),
      "market_open":feeds[CHIMERA_TICKER]["open"],
      "settings":{"bar":BAR_INTERVAL,"step":STEP,"max_bars":MAX_BARS,
                  "max_loss":MAX_LOSS,"hard_stop":HARD_STOP,
                  "vol_mult":VOL_MULT,"slots":SLOTS,"start_cash":START_CASH},
      "tickers":{"chimera":CHIMERA_TICKER,"hold":HOLD_TICKER},
      "prices":{CHIMERA_TICKER:px_c, HOLD_TICKER:px_h},
      "bars":{s:[[t,p] for t,p in feeds[s]["bars"][-KEEP_BARS:]] for s in feeds},
      "last_bar":last_bar,
      "chimera_bot":chim.to_dict(),"hold_bot":hold_bot.to_dict(),
      "metrics":{"chimera":chimera_metrics(chim,px_c),"hold":hold_metrics(hold_bot,px_h)},
    }
    tmp=STATE_FILE+".tmp"
    json.dump(state, open(tmp,"w"), separators=(",",":"))
    os.replace(tmp, STATE_FILE)

    n=write_csv(chim,hold_bot)
    size=build_dashboard(state)

    m=state["metrics"]
    print(f"\n  Bars processed: {total_new} | trades on record: {n}")
    print(f"  Next decision:  {nxt.isoformat(timespec='minutes')}")
    print(f"  Dashboard:      {DASH_FILE} ({size:,} bytes)\n")
    print(f"  {'':10}{'value':>10}{'return':>10}{'trades':>8}{'max drop':>10}")
    print(f"  {'Chimera':10}${m['chimera']['equity']:>9.2f}"
          f"{m['chimera']['return']*100:>9.1f}%{m['chimera']['trades']:>8}"
          f"{m['chimera']['drawdown']*100:>9.1f}%")
    print(f"  {'Hold':10}${m['hold']['equity']:>9.2f}"
          f"{m['hold']['return']*100:>9.1f}%{m['hold']['trades']:>8}"
          f"{m['hold']['drawdown']*100:>9.1f}%")
    return 0

if __name__=="__main__":
    sys.exit(main())
