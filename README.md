# Chimera Bench

Chimera Bot trading Cerebras (CBRS) against Hold Bot holding NVIDIA (NVDA).
Real 1-hour bars, $500 each, no real money and no orders placed.

Each bot gets the stock its approach is built for. Chimera needs volatility with
no settled direction; Hold needs sustained growth. Whichever wins, wins on its
own terms.

---

## Setup

1. **New repository.** Public if you want a free dashboard URL. Tick "Add a
   README file" so the repo has a default branch — scheduled workflows only run
   from it.
2. **Upload `chimera_bench.py`** — Add file → Upload files → Commit.
3. **Create the workflow.** Add file → Create new file. Type
   `.github/workflows/bench.yml` as the filename; each `/` you type becomes a
   folder. Paste in the contents of `bench.yml` and commit.
4. **Settings → Actions → General → Workflow permissions → Read and write.**
   Skip this and the bots run but cannot save, so every run starts from scratch.
5. **Actions → Chimera Bench → Run workflow.** This first run starts the
   experiment from today with both bots flat.
6. *Optional:* **Settings → Pages → Deploy from a branch → main → / (root)**
   gives you a permanent dashboard at
   `USERNAME.github.io/REPO/dashboard.html`.

No API key is needed. Prices come from Yahoo Finance, which works server-side
without one.

---

## The rules

**Chimera, long engine**

1. Buy a slice whenever the price drops 1% below the lowest price currently held
2. Sell each slice the moment it is up 1% from what it was bought for
3. Never hold more than five slices
4. If a slice has been held 13 bars and is down more than 20%, sell it anyway
5. Buy a half slice on any bar moving more than 3x the recent average

**Chimera, short engine** — the mirror image

1. Short a slice whenever the price rises 1% above the highest price shorted
2. Cover each slice the moment it is down 1% from where it was shorted
3. Never hold more than five short slices
4. If a slice has been open 13 bars and is losing, cover it anyway
5. Short a half slice on unusually large bars
6. Cover immediately if a slice is down 10%, regardless — a short's loss has no
   ceiling the way a buy's does, so this one is a hard line

**Hold** — buy once, hold forever.

---

## Where the settings came from

Tuned on the first 30 of 60 sessions, then checked against the last 30 it had
never seen.

| Setting | Value | Why |
|---|---|---|
| Bar size | 1 hour | Best of 5min / 15min / 30min / 1h / 2h / daily |
| Step | 1% | The optimal step falls as bars slow down |
| Held too long | 13 bars (~2 sessions) | 3 bars scored 135, 13 bars 147 |
| Long loss cut | 20% | Tight cuts are *worse* — 3% scored 132 |
| Short hard stop | 10% | Mild peak, kept mainly as real protection |
| Unusual bar | 3x average | Marginal effect |
| Slots | 5 per side | 8 tuned better but validated far worse |

**Expect roughly 136% of buy-and-hold, not the higher backtest figures.** That
is what the settings scored on data they were not fitted to. The tuning half
scored 165%; the 29-point gap between them is the size of the fitting effect,
and it is the honest measure of how much of any backtest is real.

Two findings worth carrying forward:

- **Cutting losses tightly hurt.** On a stock this volatile a 3% move against a
  position is noise, and cutting there books losses on trades about to recover.
- **The slot count showed overfitting in real time.** Eight slots produced the
  single best tuning score in the whole grid and one of the worst validation
  scores. Choosing by tuning performance alone would have picked the setting
  that fails hardest live.

---

## How it runs

Every 30 minutes during market hours, plus once after the close. Each run reads
whichever 1-hour bars have completed since last time, makes one decision per
bar, and writes `state.json`, `trades.csv` and `dashboard.html`.

**Missed runs do not matter.** Tested directly: running on every single bar and
running on every seventh bar produce identical trades and an identical final
value to the cent. If GitHub delays or drops runs — it does, routinely — the
next successful run catches up.

The dashboard shows a live countdown to the next bar close, both stocks side by
side with every trade marked, both equity curves on one axis, a full trade log,
and the rules with their current values.

---

## Things that will eventually go wrong

**Yahoo is unofficial.** It is free and needs no key, but nobody guarantees it
stays available. If it changes, the run fails loudly and leaves `state.json`
untouched rather than corrupting it.

**GitHub does not email you when a scheduled run fails.** Glance at the Actions
tab occasionally. Catch-up handles the gap either way.

**Schedules stop after 60 days of repository inactivity.** The commits each run
makes should count, but worth checking if it ever goes quiet.

**Changing a setting mid-experiment invalidates the comparison,** since the bots
carry forward positions taken under the old rules. To change something, delete
`state.json` and note the date you restarted.

---

Simulated only. Costs are modelled at two basis points of slippage each way,
the SEC fee at $20.60 per $1M of proceeds, and the FINRA fee at $0.000195 per
share sold. Short positions are modelled without borrow costs or margin
interest, which real shorting would incur. This is a paper experiment, not
advice, and I am not a financial advisor.
