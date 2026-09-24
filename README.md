# Desk lab — Hyperliquid / Polymarket, read-only first

Always-on Studio computer + official Hermes info clients + paper tickets.
Not a live bot. Not Grok Bot. Do not merge with `local-quant` or the crypto ledger.

## What is live now

- Official Hermes skills (read-only): `hyperliquid`, `polymarket`
  installed for default, **Flash** (mouth), and 35b.
- `desk.py` — one CLI over those clients + the paper harness.
- Paper tickets + risk rails (`harness/`). Executor cannot send live orders.

Proven 2026-08-28: HL `markets` / `l2 ETH` / `funding ETH` against mainnet `/info`.
Polymarket `trending` via `python3.11` (system `python3` is 3.9 and cannot run the official poly script).

## Commands

```bash
cd ~/Dev/Projects/agent-trade-harness
./desk.py markets
./desk.py brief ETH
./desk.py l2 BTC
./desk.py poly
./desk.py poly "fed rate"
./desk.py ticket "long eth setup:vwap-reclaim tf:15m conv:4 entry 2500 sl 2400 size 200"
./desk.py close <trade_id> <exit_price>
./desk.py report
./desk.py status
```

`desk report` is the scoreboard: per-setup expectancy net of fees + funding,
with an n≥30 promotion gate. Costs are modelled (`harness/costs.json`) — verify
against real fills before trusting the numbers.

Rule-based conversion plan: `SPEC-rules-conversion.md`.

`desk state` needs a **view-only** address in `.env` (`HYPERLIQUID_USER_ADDRESS`).
Copy `.env.example`. Never a private key.

Telegram **Flash** is the desk mouth. Ask “ETH funding and L2 on Hyperliquid.”
No agent wallet on Flash (or any chat seat). Klud = escalate only.

## Next (not done)

1. You: confirm/edit the setup taxonomy in `SPEC-rules-conversion.md` §5.
2. You: verify `harness/costs.json` fee assumptions against real fills.
3. You: close the stale open BTC trade (#1) with a real price, or mark it a
   build artifact — it blocks the first honest `desk report`.
4. Then: tag 30 trades, read the report, decide with data.
