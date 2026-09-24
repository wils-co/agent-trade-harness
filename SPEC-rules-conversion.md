# SPEC — discretionary → rule-based (HL perps)

Status: draft, 2026-09-14. Author: flash. Venue: Hyperliquid perpetuals.
Scope: convert Wilson's discretionary HL perps day trading into rules that can be
programmed and validated. **Not** a live trading mandate.

---

## 1. Where things actually stand

| Piece | State |
|---|---|
| Market read (`desk.py`, official HL client) | works, read-only |
| Risk gate (`harness/risk.py`) | works — deterministic, non-bypassable, 19 tests pass |
| Paper fill path (`context`→`ticket`→`confirm`) | works |
| Close path + realised PnL | **was missing** — added 2026-09-14 |
| Cost model (fees/slippage/funding) | **was missing** — added 2026-09-14 |
| Decision context capture | **was missing** — added 2026-09-14 |
| Per-setup expectancy report | **was missing** — added 2026-09-14 |
| Entry logic (the actual signal) | does not exist, and should not yet |

Journal reality before this spec: **1 trade, opened 2026-08-26, never closed, 0 equity marks.**
The harness was a build test. It has a good gate and no memory.

## 2. The premise (read this before planning anything)

You cannot program discretion you have not measured.

The instinct — sit down, write 10 rules, code them — produces a system that is not
your edge. It is a guess about your edge. It will get tested by the market against
cost, and HL day trading has real cost. The order that works is:

> **instrument → measure → codify → validate mechanically → only then automate**

Every phase below is gated. If a gate fails, the project stops, not continues.

## 3. Hard constraints (perps, day-trading timeframe)

- **Fees are not noise.** Round-trip taker ≈ 4.5bps × 2. On a $200 trade that is
  $0.18 per round trip before funding, against a $10 risk cap. At day-trading
  frequency that is a fixed tax on every decision. Paper PnL now models this.
  `harness/costs.json` values are **placeholders — verify against real fills**
  (`hyperliquid_client.py review <address>` prints actual totals per coin).
- **Funding is hourly.** Holds that cross funding hours pay (or receive) it. The
  cost model accrues it per hour held. Intraday scalps mostly dodge it — scalps
  pay in fees instead. There is no free timeframe.
- **Fee drag is the first thing to measure.** In sandbox validation, a gross
  +1.25 result became +0.70 net — a 44% drag. Any strategy whose edge is thinner
  than its round-trip cost is not a strategy.
- **Liquidation is not a risk input.** `max_leverage: 1` means the stop is the
  only loss vector. Keep it. Leverage raises fee drag and liquidation risk at the
  same time; it does not raise edge.
- **Sample size.** n<30 per setup is noise. Promotion needs n≥30 **and** profit
  factor >1.0 net of cost. Day trading reaches 30 trades per setup in weeks, not
  days, if you are honest about tagging.

## 4. Phases

### Phase 0 — instrument (now, ~10 min per trade, no new decisions)
Log every trade with a **setup tag**. Nothing else changes.
Ticket format adds optional tags:

```
desk ticket "long eth setup:vwap-reclaim regime:trend tf:15m conv:4 entry 4000 sl 3900 size 400"
desk confirm <id>
desk close <id> <exit_price>
desk report
```

**Gate 0:** tags are being applied. `desk report` shows `untagged` shrinking.
An untagged trade is a trade that taught you nothing. Confirm prints a warning
when you forget.

### Phase 1 — measure (n≥30 per setup)
`desk report` gives expectancy, win rate, profit factor and avg hold per setup,
net of modelled cost. The output is the only honest scoreboard in this repo.

**Gate 1:** at least one setup reaches n≥30 with PF>1.0 net. If nothing does
after ~100 trades, the finding is real and valuable: your current discretion does
not survive cost. Do not proceed to Phase 2 hoping it will.

### Phase 2 — codify the survivors only
For each Phase-1 survivor, write the entry as a predicate over observable state:

```
setup: vwap-reclaim
  when  15m close > session VWAP, previous 4h was below, volume > 20-bar mean
  entry reclaim of VWAP ± 2bps
  stop  below the reclaim swing low, max 1.25% from entry
  exit  TP1 at 1R, trail rest under 5m swing lows
  skip  if |funding| > <threshold>/hr, or spread > <x> bps
```

Rules are constraints on the **stop and exit first** — those are what Phase 1
measured. Entry conditions are the last thing to fix, because entry quality is
the least measurable part of a small sample.

**Gate 2:** each rule is expressed without adjectives. "Strong momentum" fails.
"3 consecutive 15m closes above VWAP on rising volume" passes.

### Phase 3 — validate mechanically, side by side
Emit the mechanical system's trades as tickets in the same journal, tagged
`setup:<name>-mech`, running in parallel with your discretionary version on the
same market. Same gate, same costs, same report.

**Gate 3:** mechanical version is within noise of (or better than) the
discretionary version on n≥30. If mechanical is clearly worse, your discretion
contains something you have not yet named — go back to Phase 1 with the
difference as a new tag, do not tune parameters.

### Phase 4 — execution (only if Gate 3 passes)
Testnet agent wallet, human-confirm per order, no unattended submit. Unchanged
from standing rules. This phase is optional; a rule-based system you execute
manually is a valid end state and is where most of the value sits.

## 5. Setup taxonomy (Wilson's input required)

Tags must be defined **before** they are used, or they drift into synonyms and
the measurement is worthless. Starter vocabulary for HL perps day trading —
edit, delete, add:

- `vwap-reclaim` — mean reversion back through session VWAP
- `momentum-break` — break of range high/low with volume
- `range-fade` — fade the edge of a defined range in chop
- `trend-pullback` — continuation entry on a pullback in an established trend
- `liq-sweep` — entry after a liquidation cascade / wick reversal
- `funding-flush` — position when funding is stretched to one side
- `news-spike` — reaction to a scheduled or unscheduled catalyst

`regime` values: `trend` | `chop` | `range` | `volatile`.
`conviction`: 1–5, your own before-the-fact confidence. It is a **testable
variable** — if conviction doesn't correlate with outcome, your gut is not
adding information and the mechanical version should ignore it.

**Decision needed:** which of these do you actually trade, and are there setups
not on this list? One word each is fine.

## 6. Stop conditions (write these down now, not later)

- No setup reaches n≥30 within ~6 weeks of disciplined tagging → the bottleneck
  is not the software, it is consistency. Fix that first.
- All setups show PF<1.0 net after n≥30 → your current day-trading timeframe is
  not profitable against cost on this venue. Shift timeframe up (fewer, longer
  holds pay less fee drag) or stop. Both are wins over losing slowly.
- Two consecutive phases fail to produce a measurable change → close the
  project. Do not keep building harness features instead of measuring.

## 7. Division of labour

- **Flash (this seat):** spec, review of your sessions, journal queries, the
  per-setup report read, rule drafting from measured data, code changes to the
  harness.
- **Pool / cron:** any long-loop data collection or unattended monitoring. This
  seat does not run 24/7.
- **Wilson:** tags every trade, closes every trade with a real price, decides
  which setups are in scope.
- Standing rules unchanged: no agent key on any chat seat, no unattended
  `--submit`, no overnight agent trading, view-only address or nothing.

## 8. Immediate next actions

- [x] Close path, cost model, context capture, per-setup report — 2026-09-14
- [x] Live journal migrated additively (backups at `journal/harness.db.bak-*`)
- [x] Trade #1 (stale BTC build test) closed 2026-09-15 at live HL mid 77,967.50
      as an **artifact** — gross/costs/net zeroed, `excluded_from_report=1`.
      Build tests never enter the scoreboard: 20 days of placeholder funding on a
      test fill would be a fabricated number. Use `desk close <id> <price> artifact`
      for these.
- [ ] Wilson: confirm/edit the setup taxonomy (§5)
- [ ] Wilson: verify `harness/costs.json` fee assumptions against real fills
- [ ] Then: tag 30 trades and read the first report
