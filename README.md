# agent-trade-harness

<p align="left">
  <img src="https://img.shields.io/badge/python-3.11-0284c7?style=flat-square&logo=python&logoColor=white" alt="Python 3.11"/>
  <img src="https://img.shields.io/badge/tests-34%20passed-059669?style=flat-square" alt="Tests 34 Passed"/>
  <img src="https://img.shields.io/badge/venue-Hyperliquid%20L2-6366f1?style=flat-square" alt="Hyperliquid L2"/>
  <img src="https://img.shields.io/badge/risk%20unit-1R%20%3D%20%2450-b45309?style=flat-square" alt="1R = $50"/>
  <img src="https://img.shields.io/badge/performance-%2B5.38R%20%C2%B7%20PF%205.85-0284c7?style=flat-square" alt="Performance"/>
  <a href="https://wilsco.au/trade"><img src="https://img.shields.io/badge/live%20desk-wilsco.au%2Ftrade-b45309?style=flat-square" alt="Live Desk"/></a>
</p>

Autonomous systematic paper trading desk operating over live **Hyperliquid L2 orderbook feeds** and **perpetual funding rates**. 

Built to test autonomous agent supervision, empirical risk containment, and quantitative expectancy before real capital is committed. All performance is normalized into institutional **$R$-multiples (risk units)** with zero private dollar leakage.

---

<!-- SCOREBOARD_START -->
## Verified Performance (Unitized R)

![Cumulative Performance](assets/equity_curve.svg)

| Metric | Result | Metric | Result |
| :--- | :--- | :--- | :--- |
| **Cumulative Return** | **+5.38R** | **Max Drawdown** | **-1.11R** |
| **Expectancy** | **+1.35R / trade** | **Profit Factor** | **5.85** |
| **Win Rate** | **75.0%** (3W / 1L) | **Recovery Factor** | **4.85** |
| **Cost Drag** | **12.2% of gross** | **Payoff Ratio** | **1.95x** |

👉 **[View Full Verified Trade Ledger & Setup Attribution (TRADES.md)](TRADES.md)** · **[Live Desk Dashboard (wilsco.au/trade)](https://wilsco.au/trade)**
<!-- SCOREBOARD_END -->

---

## 🏛️ The Non-Negotiable Contract

The harness is engineered around four strict institutional rules:

```text
┌────────────────────────────────────────────────────────────────────────┐
│ 1. THE MODEL NEVER PLACES ORDERS                                       │
│    The LLM (Hermes / Claude / Gemini) parses intent, checks geometry,  │
│    and audits logs. Deterministic Python risk modules hold unbypassable│
│    veto power over position sizing, leverage caps, and daily loss caps.│
├────────────────────────────────────────────────────────────────────────┤
│ 2. DOLLAR AMOUNT ALWAYS MEANS RISK                                     │
│    A prompt like "short eth $50" strictly means $50.00 at risk (1R),   │
│    never 1x spot size. Leverage and notional sizing are mathematically │
│    derived from stop distance so max loss is fixed at exactly 1R.      │
├────────────────────────────────────────────────────────────────────────┤
│ 3. MICROSTRUCTURE OVER INDICATORS                                      │
│    Stops are never anchored to arbitrary indicators. A local daemon    │
│    (liq-tape on port 8791) streams live orderbook walls and liquidation│
│    pools so stops sit safely behind real market liquidity barriers.    │
├────────────────────────────────────────────────────────────────────────┤
│ 4. CENTRAL LIMIT THEOREM PROMOTION GATE (n ≥ 30)                       │
│    Small samples are random noise. Any strategy under 30 closed trades │
│    is strictly labeled "Incubating". A setup must survive taker fees,   │
│    slippage, and funding across 30+ trades before promotion to a rule. │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 📐 System Architecture & Data Air-Gap

The repository maintains a strict separation between private execution state and public verified reporting:

```text
Local Private SSOT (Studio M3 Ultra)           Public Verification (GitHub & Web)
────────────────────────────────────           ──────────────────────────────────
[Hyperliquid L2 / WS]
         │ (stream)
         ▼
[liq-tape :8791] ──► [harness.agent]
                           │
                           ▼
                  [journal/harness.db]
                  • Real Cash ($768.94)
                  • $3,835 Notional Size
                  • Exact Margin & Fees
                  [STRICTLY GITIGNORED]
                           │
                           │ python3.11 desk.py export
                           ▼
                  [harness/export.py] ────────► TRADES.md (Zero $ leaked)
                                      ────────► assets/equity_curve.svg
                                      ────────► README.md (Scoreboard)
                                      ────────► wilsco.au/trade
```

---

## ⚡ In-Flight Autonomous Lifecycle

Positions are monitored every 15 seconds against price boundaries:

```text
ENTRY (e.g. Short ETH @ 2,684.50 · Risk $50 · 18x Isolated)
  │
  ├──► TP1 Hit (+2.55% · 2,616.00):
  │       ├─ Bank 80% of position size (+$78.29 locked)
  │       ├─ Auto-trail stop on remaining 20% runner to Breakeven (2,684.50)
  │       └─ Position becomes a risk-free free-roll
  │
  ├──► Outcome A: TP2 Runner Hit (+4.53% · 2,563.00) ──► Realize full R:R 2.26+
  └──► Outcome B: Runner Stopped at Breakeven ──────────► Scratched at $0 loss
```

---

## 💻 CLI Commands

The harness is operated through `desk.py`, wrapping official Hyperliquid info clients and paper execution:

| Command | Action |
| :--- | :--- |
| `./desk.py markets` | Top market volume and 24h volatility across Hyperliquid. |
| `./desk.py brief ETH` | L2 orderbook top 5 levels + 24h hourly funding rates. |
| `./desk.py l2 <COIN>` | Deep 8-level orderbook bid/ask wall inspection. |
| `./desk.py ticket "<PROMPT>"` | Parse trade intent, derive leverage from stop, and log paper ticket. |
| `./desk.py check` | Check active positions against live mark and TP/SL bands. |
| `./desk.py watch [interval]` | Continuous background monitor with automatic TP1 partial banking. |
| `./desk.py export` | **One-command publishing:** scrubs $, generates SVG, and updates `TRADES.md`. |
| `./desk.py report` | Internal expectancy scoreboard grouped by setup tag. |
| `./desk.py status` | Current account equity, drawdown from peak, and open positions. |

---

## 📁 Repository Structure

```text
agent-trade-harness/
├── README.md               # Architecture overview + Verified Scoreboard
├── TRADES.md               # Public verified trade book (clean R:R, zero dollar amounts)
├── desk.py                 # Primary desk CLI
├── SPEC-rules-conversion.md# Rules conversion doctrine
├── assets/
│   └── equity_curve.svg    # Retina vector cumulative R + underwater profile
├── harness/
│   ├── export.py           # Public sanitizer, stats engine, and SVG generator
│   ├── ticket.py           # Ticket parser & leverage derivation
│   ├── risk.py             # Risk rails & stop distance constraints
│   ├── review.py           # Cost modeling (fees/funding) & expectancy review
│   ├── agent.py            # Local telemetry & alert daemon
│   └── notify.py           # Telegram alert dispatcher
├── tests/
│   ├── test_export.py      # Assertions verifying zero $ leakage & quant math
│   ├── test_risk.py        # Risk rail and cap validation
│   └── test_notify.py      # Alert formatting tests
└── journal/                # [GITIGNORED]
    ├── harness.db          # Private SQLite database with real $ balances
    └── harness.db.LOCKED-SSOT # Immutable local backup copy
```

---

## 🧪 Testing & Verification

Comprehensive unit tests enforce risk rails, leverage derivations, and zero-dollar leak assertions:

```bash
python3 -m pytest tests/
# ============================== 34 passed in 0.14s ==============================
```

---

## 🔗 Links

- **Live Desk Dashboard:** [wilsco.au/trade](https://wilsco.au/trade)
- **Verified Trade Ledger:** [TRADES.md](TRADES.md)
- **Author:** Wilson Chang ([@wilsco_](https://x.com/wilsco_) · [wilsco.au](https://wilsco.au))
