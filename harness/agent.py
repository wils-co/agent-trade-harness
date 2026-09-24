"""agent — the interface Hermes (or any seat) calls.

Commands:
  ticket "<ticket text>" [chart_image_path]  → parse + risk-check + echo for confirm
  confirm <pending_id>                       → execute pending order
  status                                     → equity, open trades, rails
  flatten                                    → mark all open closed (paper) / Phase4 real
  kill [reason]                              → activate kill switch
  unkill                                     → deactivate kill switch
  brief                                      → daily summary

Every command returns plain text suitable for a Telegram reply.
"""

from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

from .risk import RiskEngine, load_config
from .ticket import parse_ticket, derive_size, format_ticket, TicketError, fetch_live_price
from .executor import get_executor, TESTNET
from . import review, notify

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "journal" / "harness.db"
PENDING_PATH = ROOT / "journal" / "pending.json"



# ---------- liq-aware sizing ----------

# HL base-tier maintenance margin: half of initial margin at max leverage.
# ETH maxLeverage=25 -> mmr 2%. BTC maxLeverage=40 -> mmr 1.25%.
MMR = {"BTC": 0.0125, "ETH": 0.02}
LIQ_BUFFER_PCT = 0.02  # liq must sit at least this far BEYOND the stop


def liq_price_long(entry: float, leverage: int, mmr: float) -> float:
    """Isolated long liq: entry * (1 - 1/L + mmr)."""
    return entry * (1 - 1 / leverage + mmr)


def liq_price_short(entry: float, leverage: int, mmr: float) -> float:
    """Isolated short liq: entry * (1 + 1/L - mmr)."""
    return entry * (1 + 1 / leverage - mmr)


def liq_aware_sizing(ticket: dict, mmr: float) -> dict:
    """Given entry/sl and an explicit risk_usd, derive:
    - notional = risk / stop_distance_pct
    - leverage as a MAXIMUM: isolated liq = entry*(1 -/+ 1/L -/+ mmr), so
      higher leverage pulls liq toward entry; leverage must stay LOW enough
      that liq lands beyond the stop with a LIQ_BUFFER_PCT cushion.
    Returns info dict; updates ticket in place. Raises TicketError if impossible.
    """
    entry, sl = ticket["entry"], ticket["sl"]
    side = ticket["side"]
    risk = ticket["risk_usd"]
    dist = abs(entry - sl)
    if dist <= 0 or entry <= 0:
        raise TicketError("SL equals entry")
    dist_pct = dist / entry
    notional = round(risk / dist_pct, 2)
    b = LIQ_BUFFER_PCT

    if side == "long":
        need = 1 + mmr - (sl / entry) * (1 - b)  # required 1/L
        if need <= 0:
            raise TicketError("stop must be below entry for a long")
        l_max = 1 / need

        def liq(L):
            return entry * (1 - 1 / L + mmr)

        target = sl * (1 - b)
    else:
        need = (sl / entry) * (1 + b) - 1 + mmr  # required 1/L
        if need <= 0:
            raise TicketError("stop must be above entry for a short")
        l_max = 1 / need

        def liq(L):
            return entry * (1 + 1 / L - mmr)

        target = sl * (1 + b)

    lev = int(l_max)  # floor: never round UP past the liq ceiling
    if lev < 1:
        raise TicketError(
            f"liq-as-stop impossible: even 1x liq sits inside the stop zone "
            f"(stop {dist_pct*100:.1f}% away, maintenance {mmr*100:.1f}%)"
        )
    requested = ticket.get("leverage") or 0
    if requested > 1:
        lev = min(lev, int(requested))
    if (side == "long" and liq(lev) > target) or (side == "short" and liq(lev) < target):
        raise TicketError("could not place liq beyond the stop")

    ticket["size_usd"] = notional
    ticket["leverage"] = lev
    return {
        "notional": notional,
        "leverage": lev,
        "liq_price": round(liq(lev), 2),
        "stop": sl,
    }


class Agent:
    def __init__(self):
        self.risk = RiskEngine(str(DB_PATH))
        self.executor = get_executor(self.risk)

    # ---------- ticket flow ----------

    def cmd_ticket(self, text: str, chart_image: str | None = None) -> str:
        try:
            t = parse_ticket(text)
        except TicketError as e:
            self.risk.log("reject", f"TICKET PARSE FAIL: {e} | raw: {text!r}")
            return f"❌ Ticket rejected: {e}"

        cfg = load_config()
        liq_note = ""
        if t.get("risk_usd") is not None:
            # explicit $ risk: size from stop distance, leverage so liq stays past the stop
            if t["risk_usd"] > cfg["max_risk_per_trade_usd"]:
                return (f"❌ risk ${t['risk_usd']:.0f} exceeds per-trade cap "
                        f"${cfg['max_risk_per_trade_usd']:.0f}")
            mmr = MMR.get(t["symbol"], 0.02)
            try:
                info = liq_aware_sizing(t, mmr)
            except TicketError as e:
                return f"❌ {e}"
            liq_note = (f"\nliq {info['liq_price']:,.0f} ({t['leverage']}x isolated) "
                        f"— stop {t['sl']:,.0f} fires first")
        elif t["size_usd"] is None:
            try:
                t["size_usd"] = derive_size(t, cfg["max_risk_per_trade_usd"])
            except TicketError as e:
                return f"❌ {e}"

        # THE gate — nothing bypasses this
        d = self.risk.check_order(
            symbol=t["symbol"], side=t["side"], entry_price=t["entry"],
            size_usd=t["size_usd"], stop_loss=t["sl"],
            leverage=t["leverage"], tp1=t["tp1"], tp2=t["tp2"],
        )
        if not d.approved:
            return f"🛑 BLOCKED by risk engine: {d.reason}"
        if d.clipped_size_usd is not None and d.clipped_size_usd != t["size_usd"]:
            t["size_usd"] = d.clipped_size_usd
            note = f"\n⚠️ size clipped to ${t['size_usd']:.0f} by rail"
        else:
            note = ""

        risk_usd = abs(t["entry"] - t["sl"]) / t["entry"] * t["size_usd"]

        pending = {
            "id": int(time.time()),
            "ticket": t,
            "thesis": text.strip(),
            "ctx": {k: t.get(k) for k in
                    ("setup", "regime", "timeframe", "conviction")},
            "chart_image": chart_image,
            "created": time.time(),
        }
        PENDING_PATH.write_text(json.dumps(pending, indent=2))

        mode = "TESTNET" if TESTNET else "LIVE"
        msg = (
            f"📋 TICKET [{mode}] #{pending['id']}\n{format_ticket(t)}\n"
            f"risk: ${risk_usd:.2f} of ${cfg['max_risk_per_trade_usd']} cap"
        )
        if liq_note:
            msg += liq_note
        msg += f"\n{note}\nReply 'confirm {pending['id']}' to execute."
        return msg

    def cmd_confirm(self, pending_id: int) -> str:
        if not PENDING_PATH.exists():
            return "❌ no pending order"
        p = json.loads(PENDING_PATH.read_text())
        if p["id"] != pending_id:
            return f"❌ pending order is #{p['id']}, not #{pending_id}"

        # re-run the gate at execution time in case state changed
        t = p["ticket"]
        d = self.risk.check_order(
            symbol=t["symbol"], side=t["side"], entry_price=t["entry"],
            size_usd=t["size_usd"], stop_loss=t["sl"],
            leverage=t["leverage"], tp1=t["tp1"], tp2=t["tp2"],
        )
        if not d.approved:
            PENDING_PATH.unlink(missing_ok=True)
            return f"🛑 re-check failed at execution: {d.reason}"

        fill = self.executor.market_order(
            t["symbol"], t["side"], t["size_usd"], t["entry"]
        )
        if not fill.ok:
            return f"❌ execution failed: {fill.detail}"

        orig_sz = t["size_usd"]
        ratio = t.get("tp1_ratio", 0.80)
        cur = self.risk.db.execute(
            """INSERT INTO trades (ts, symbol, side, entry_price, size_usd,
               stop_loss, tp1, tp2, leverage, thesis, chart_image, orig_size_usd, tp1_ratio)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), t["symbol"], t["side"], t["entry"], t["size_usd"],
             t["sl"], t["tp1"], t["tp2"], t["leverage"],
             p.get("thesis"), p.get("chart_image"), orig_sz, ratio),
        )
        self.risk.db.commit()

        ctx = p.get("ctx") or {}
        review.capture(
            self.risk.db,
            cur.lastrowid,
            setup_tag=ctx.get("setup"),
            regime=ctx.get("regime"),
            timeframe=ctx.get("timeframe"),
            conviction=ctx.get("conviction"),
            notes=p.get("thesis"),
        )
        untagged = "" if ctx.get("setup") else (
            "\n⚠️ no setup tag — this trade cannot be measured by strategy. "
            "Add 'setup:<tag>' next time."
        )
        PENDING_PATH.unlink()
        return (f"✅ FILLED: {format_ticket(t)}\nJournal updated (id #{cur.lastrowid})."
                f"{untagged}")

    # ---------- ops ----------

    def cmd_status(self) -> str:
        eq = self.risk.current_equity()
        dd = self.risk.current_drawdown_pct()
        max_dd = self.risk.max_drawdown_pct()
        today = self.risk.realized_pnl_today()
        total = self.risk.total_realized_pnl()
        killed = "🚨 KILLED" if self.risk.kill_switch_active() else "🟢 live"
        opens = self.risk.db.execute(
            "SELECT id, symbol, side, entry_price, size_usd, stop_loss, status, tp1, tp2, orig_size_usd FROM trades WHERE status IN ('open', 'closed_tp1')"
        ).fetchall()
        lines = [
            f"**Account** ({'testnet' if TESTNET else 'LIVE'}): {killed}",
            f"equity ${eq:.2f} | today {today:+.2f} | all-time {total:+.2f}",
            f"drawdown {dd:.1f}% / {load_config()['max_drawdown_pct']}% cap (worst {max_dd:.1f}%)",
            f"active positions: {len(opens)}",
        ]
        for o in opens:
            if o["status"] == "closed_tp1":
                tp2_str = f" tp2 {o['tp2']:,.2f}" if o['tp2'] else ""
                lines.append(f"  · #{o['id']} [RUNNER 20%] {o['side']} {o['symbol']} ${o['size_usd']:.0f} @ {o['entry_price']:,.2f} sl (BE) {o['stop_loss']:,.2f}{tp2_str}")
            else:
                tp_info = f" tp1 {o['tp1']:,.2f}" if o['tp1'] else ""
                tp_info += f" tp2 {o['tp2']:,.2f}" if o['tp2'] else ""
                lines.append(f"  · #{o['id']} {o['side']} {o['symbol']} ${o['size_usd']:.0f} @ {o['entry_price']:,.2f} sl {o['stop_loss']:,.2f}{tp_info}")
        return "\n".join(lines)

    def cmd_kill(self, reason: str = "") -> str:
        self.risk.activate_kill_switch(reason or "manual via TG")
        return "🚨 KILL SWITCH ON. All orders blocked. Flatten manually if needed."

    def cmd_unkill(self) -> str:
        self.risk.deactivate_kill_switch()
        return "🟢 kill switch off. Trading re-enabled."

    def cmd_tp1(self, trade_id: int, exit_price: float | None = None, note: str = "") -> str:
        try:
            r = review.hit_tp1(self.risk.db, trade_id, exit_price, note=note)
        except ValueError as e:
            return f"❌ {e}"

        # Alert Xox on Telegram
        notify.notify_tp1(r)

        pct_label = int(r["ratio"] * 100)
        runner_pct = 100 - pct_label
        return (
            f"🎯 TP1 HIT #{r['id']} {r['side'].upper()} {r['symbol']} @ {r['exit_price']:,.2f}\n"
            f"Banked {pct_label}% (${r['tranche_size']:.2f}): gross {r['gross']:+.2f} | "
            f"fees -{r['fees']:.2f} | **net {r['net']:+.2f}**\n"
            f"🏃 Remaining runner: {runner_pct}% (${r['runner_size']:.2f})\n"
            f"🛡️ Stop Loss moved to Breakeven @ {r['breakeven_sl']:,.2f} (risk-free)"
        )

    def cmd_close(self, trade_id: int, exit_price: float, note: str = "",
                  artifact: bool = False) -> str:
        try:
            r = review.close_trade(self.risk.db, trade_id, exit_price, note=note,
                                   artifact=artifact)
        except ValueError as e:
            return f"❌ {e}"
        if artifact:
            return (
                f"✅ CLOSED #{r['id']} [artifact] @ {exit_price:,.2f}\n"
                f"build test, not a decision — gross/costs/net zeroed, "
                f"excluded from report. held {r['hold_hours']:.1f}h"
            )

        # Alert Xox on Telegram
        notify.notify_close(r)

        if r.get("is_runner"):
            return (
                f"✅ CLOSED #{r['id']} [RUNNER {r['status']}] @ {exit_price:,.2f}\n"
                f"Runner: gross {r['runner_gross']:+.2f} | net {r['runner_net']:+.2f}\n"
                f"Total trade: gross {r['gross']:+.2f} | fees -{r['fees']:.2f} | "
                f"funding -{r['funding']:.2f} | **net {r['net']:+.2f}**\n"
                f"held {r['hold_hours']:.2f}h"
            )
        return (
            f"✅ CLOSED #{r['id']} [{r['status']}] @ {exit_price:,.2f}\n"
            f"gross {r['gross']:+.2f} | fees -{r['fees']:.2f} | "
            f"funding -{r['funding']:.2f} | **net {r['net']:+.2f}**\n"
            f"held {r['hold_hours']:.2f}h"
        )

    def cmd_flatten(self, exit_price: float | None, note: str = "flatten") -> str:
        opens = self.risk.db.execute(
            "SELECT id, symbol, entry_price FROM trades WHERE status IN ('open', 'closed_tp1')"
        ).fetchall()
        if not opens:
            return "no open trades"
        if exit_price is None:
            return ("❌ flatten needs a price in paper mode — I have no live tick here. "
                    "Use: close <id> <price>   or   flatten <price>")
        out = [self.cmd_close(o["id"], exit_price, note) for o in opens]
        return "\n".join(out)

    def cmd_report(self) -> str:
        return review.report(self.risk.db)

    def cmd_brief(self) -> str:
        day_trades = self.risk.db.execute(
            "SELECT COUNT(*) c, SUM(pnl_usd) s FROM trades WHERE ts >= "
            "(SELECT MAX(ts) FROM trades) - 86400 AND status != 'open'"
        ).fetchone()
        events = self.risk.db.execute(
            "SELECT kind, COUNT(*) c FROM events WHERE ts > strftime('%s','now')-86400 "
            "GROUP BY kind ORDER BY c DESC"
        ).fetchall()
        ev = ", ".join(f"{r['kind']}:{r['c']}" for r in events) or "quiet"
        winrate = ""
        wr = self.risk.db.execute(
            "SELECT AVG(CASE WHEN pnl_usd>0 THEN 1.0 ELSE 0 END) w, COUNT(*) n "
            "FROM trades WHERE status IN ('closed','closed_tp1','stopped')"
        ).fetchone()
        if wr["n"]:
            winrate = f" | 30d-ish win rate {wr['w']*100:.0f}% over {wr['n']} trades"
        return (
            f"**Daily brief**\n{self.cmd_status()}\n"
            f"closed last 24h: {day_trades['c'] or 0}, pnl {day_trades['s'] or 0:+.2f}{winrate}\n"
            f"events 24h: {ev}"
        )

    def cmd_check(self) -> str:
        """Check all active trades against live Hyperliquid prices.
        Triggers TP1, TP2, or Stop Loss as price crosses thresholds.
        """
        opens = self.risk.db.execute(
            "SELECT id, symbol, side, entry_price, size_usd, stop_loss, status, tp1, tp2 "
            "FROM trades WHERE status IN ('open', 'closed_tp1')"
        ).fetchall()
        if not opens:
            return "No active positions to monitor."

        symbols = {o["symbol"] for o in opens}
        prices = {}
        for sym in symbols:
            px = fetch_live_price(sym)
            if px is not None:
                prices[sym] = px

        actions = []
        statuses = []
        for o in opens:
            sym = o["symbol"]
            mid = prices.get(sym)
            if mid is None:
                statuses.append(f"#{o['id']} {sym}: live price unavailable")
                continue

            side = o["side"].lower()
            tid = o["id"]
            status = o["status"]
            sl = o["stop_loss"]
            tp1 = o["tp1"]
            tp2 = o["tp2"]

            if side == "long":
                if status == "open":
                    if tp1 and mid >= tp1:
                        actions.append(self.cmd_tp1(tid, mid, note="hit tp1 live check"))
                    elif sl and mid <= sl:
                        actions.append(self.cmd_close(tid, mid, note="hit sl live check"))
                    else:
                        tp_str = f" | tp1 {tp1:,.2f}" if tp1 else ""
                        statuses.append(f"#{tid} LONG {sym}: mid {mid:,.2f} | sl {sl:,.2f}{tp_str}")
                elif status == "closed_tp1":
                    if tp2 and mid >= tp2:
                        actions.append(self.cmd_close(tid, mid, note="hit tp2 live check"))
                    elif sl and mid <= sl:
                        actions.append(self.cmd_close(tid, mid, note="runner hit BE stop live check"))
                    else:
                        tp2_str = f" | tp2 {tp2:,.2f}" if tp2 else ""
                        statuses.append(f"#{tid} [RUNNER] LONG {sym}: mid {mid:,.2f} | sl(BE) {sl:,.2f}{tp2_str}")
            else:  # short
                if status == "open":
                    if tp1 and mid <= tp1:
                        actions.append(self.cmd_tp1(tid, mid, note="hit tp1 live check"))
                    elif sl and mid >= sl:
                        actions.append(self.cmd_close(tid, mid, note="hit sl live check"))
                    else:
                        tp_str = f" | tp1 {tp1:,.2f}" if tp1 else ""
                        statuses.append(f"#{tid} SHORT {sym}: mid {mid:,.2f} | sl {sl:,.2f}{tp_str}")
                elif status == "closed_tp1":
                    if tp2 and mid <= tp2:
                        actions.append(self.cmd_close(tid, mid, note="hit tp2 live check"))
                    elif sl and mid >= sl:
                        actions.append(self.cmd_close(tid, mid, note="runner hit BE stop live check"))
                    else:
                        tp2_str = f" | tp2 {tp2:,.2f}" if tp2 else ""
                        statuses.append(f"#{tid} [RUNNER] SHORT {sym}: mid {mid:,.2f} | sl(BE) {sl:,.2f}{tp2_str}")

        if actions:
            return "\n\n".join(actions)
        return "All positions active & within bands:\n  " + "\n  ".join(statuses)

    def cmd_watch(self, interval: int = 10) -> str:
        """Poll active positions in a continuous loop."""
        print(f"👀 Watching positions every {interval}s... Press Ctrl+C to exit.", flush=True)
        try:
            while True:
                out = self.cmd_check()
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] {out}\n", flush=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            return "\nStopped watch loop."


def run_command(argv: list[str]) -> str:
    if not argv:
        return Agent().cmd_status()
    cmd, *rest = argv
    agent = Agent()
    if cmd == "ticket":
        text = rest[0] if rest else ""
        img = rest[1] if len(rest) > 1 else None
        return agent.cmd_ticket(text, img)
    if cmd == "confirm":
        if not rest:
            return "usage: confirm <pending_id>  (see desk ticket output)"
        return agent.cmd_confirm(int(rest[0]))
    if cmd == "status":
        return agent.cmd_status()
    if cmd == "tp1":
        if not rest:
            return "usage: tp1 <trade_id> [exit_price]"
        tid = int(rest[0])
        px = float(rest[1]) if len(rest) > 1 else None
        return agent.cmd_tp1(tid, px)
    if cmd == "close":
        if len(rest) < 2:
            return "usage: close <trade_id> <exit_price> [artifact]"
        art = "artifact" in [a.lower() for a in rest[2:]]
        note = " ".join(a for a in rest[2:] if a.lower() != "artifact")
        return agent.cmd_close(int(rest[0]), float(rest[1]), note, artifact=art)
    if cmd == "check":
        return agent.cmd_check()
    if cmd == "watch":
        sec = int(rest[0]) if rest else 10
        return agent.cmd_watch(sec)
    if cmd == "flatten":
        return agent.cmd_flatten(float(rest[0]) if rest else None,
                                 " ".join(rest[1:]) or "flatten")
    if cmd == "report":
        return agent.cmd_report()
    if cmd == "kill":
        return agent.cmd_kill(" ".join(rest))
    if cmd == "unkill":
        return agent.cmd_unkill()
    if cmd == "brief":
        return agent.cmd_brief()
    return f"unknown command: {cmd}"


if __name__ == "__main__":
    import sys
    print(run_command(sys.argv[1:]))
