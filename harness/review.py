"""review — the learning loop: close trades, cost-model them, measure per setup.

The harness could open paper tickets but never close them, so pnl_usd stayed
NULL and equity never moved. Without closed, cost-adjusted trades there is
nothing to learn from and no way to tell a rule from a feel.

This module adds:
  - cost model (taker/maker fees + slippage + hourly funding)
  - close_trade()  -> gross / fees / funding / NET, plus equity mark
  - capture()      -> decision context snapshot at entry (setup, regime, ...)
  - report()       -> per-setup expectancy net of costs, with sample gates

Nothing here places orders. Read/write is journal-only.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
COSTS_PATH = Path(__file__).parent / "costs.json"
SETUPS_PATH = Path(__file__).parent / "setups.yaml"

# Placeholder defaults. VERIFY against your own fills (official client
# `hyperliquid_client.py review <address>` prints real fee totals) before
# trusting any expectancy number this produces.
DEFAULT_COSTS = {
    "taker_fee_bps": 4.5,
    "maker_fee_bps": 1.5,
    "enter_as": "taker",
    "exit_as": "taker",
    "slippage_bps": 1.0,
    "funding_bps_per_hour": 1.0,
    "fee_source": "placeholder — verify vs real fills",
}

MIN_N_FOR_PROMOTION = 30  # below this, treat expectancy as noise


def load_costs() -> dict:
    c = dict(DEFAULT_COSTS)
    if COSTS_PATH.exists():
        c.update(json.loads(COSTS_PATH.read_text()))
    return c


@dataclass
class Costs:
    fee_usd: float
    funding_usd: float
    hold_hours: float

    @property
    def total(self) -> float:
        return self.fee_usd + self.funding_usd


def estimate_costs(size_usd: float, hold_seconds: float, cfg: dict | None = None) -> Costs:
    """Round-trip fee + slippage + funding on the held notional."""
    cfg = cfg or load_costs()
    rate = {"taker": cfg["taker_fee_bps"], "maker": cfg["maker_fee_bps"]}
    per_side = rate.get(cfg["enter_as"], cfg["taker_fee_bps"]) + rate.get(
        cfg["exit_as"], cfg["taker_fee_bps"]
    )
    bps = per_side + cfg["slippage_bps"] * 2  # slip on both fills
    fee_usd = size_usd * bps / 10_000
    hours = max(hold_seconds, 0) / 3600.0
    funding_usd = size_usd * cfg["funding_bps_per_hour"] / 10_000 * hours
    return Costs(fee_usd=fee_usd, funding_usd=funding_usd, hold_hours=hours)


# ---------- schema ----------

_EXTRA_TRADE_COLS = {
    "setup_tag": "TEXT",
    "regime": "TEXT",
    "exit_ts": "REAL",
    "hold_seconds": "REAL",
    "gross_pnl_usd": "REAL",
    "fees_usd": "REAL",
    "funding_usd": "REAL",
    "net_pnl_usd": "REAL",
    "mae_pct": "REAL",
    "mfe_pct": "REAL",
    "excluded_from_report": "INTEGER DEFAULT 0",
    "orig_size_usd": "REAL",
    "tp1_ratio": "REAL DEFAULT 0.80",
    "tp1_size_usd": "REAL",
    "tp1_exit_price": "REAL",
    "tp1_exit_ts": "REAL",
    "tp1_gross_pnl_usd": "REAL",
    "tp1_fees_usd": "REAL",
    "tp1_funding_usd": "REAL",
    "tp1_net_pnl_usd": "REAL",
}


def ensure_schema(db: sqlite3.Connection) -> None:
    """Additive, idempotent. Never rewrites existing rows."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS trade_context (
            trade_id  INTEGER PRIMARY KEY,
            ts        REAL NOT NULL,
            setup_tag TEXT,
            regime    TEXT,
            timeframe TEXT,
            session   TEXT,
            conviction INTEGER,
            funding_bps_hr REAL,
            spread_bps REAL,
            depth_usd_5lv REAL,
            dist_24h_high_pct REAL,
            dist_24h_low_pct REAL,
            vol_24h_pct REAL,
            notes     TEXT,
            snapshot  TEXT,   -- free-form json blob for anything not yet promoted
            version   INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_tc_setup ON trade_context(setup_tag);
        """
    )
    have = {r[1] for r in db.execute("PRAGMA table_info(trades)")}
    for col, typ in _EXTRA_TRADE_COLS.items():
        if col not in have:
            db.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
    db.commit()


# ---------- close & TP1 ----------

def hit_tp1(
    db: sqlite3.Connection,
    trade_id: int,
    exit_price: float | None = None,
    exit_ts: float | None = None,
    note: str = "",
) -> dict:
    """Take partial profit at TP1 (default 80%), bank PnL, move SL to Breakeven."""
    ensure_schema(db)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if row is None:
        raise ValueError(f"no trade #{trade_id}")
    if row["status"] != "open":
        raise ValueError(f"trade #{trade_id} is '{row['status']}', must be 'open' to hit TP1")

    tp1_price = exit_price if exit_price is not None else row["tp1"]
    if tp1_price is None:
        raise ValueError(f"trade #{trade_id} has no TP1 price defined")

    ratio = row["tp1_ratio"] if row["tp1_ratio"] is not None else 0.80
    full_size = row["size_usd"]
    tranche_size = full_size * ratio
    runner_size = full_size - tranche_size
    entry = row["entry_price"]
    direction = 1.0 if row["side"] == "long" else -1.0
    pct = (tp1_price - entry) / entry * direction
    gross = tranche_size * pct

    now = exit_ts or time.time()
    costs = estimate_costs(tranche_size, now - row["ts"])
    net = gross - costs.total

    # Move SL to breakeven (entry_price) on the remaining runner
    db.execute(
        """UPDATE trades SET
           status = 'closed_tp1',
           orig_size_usd = COALESCE(orig_size_usd, ?),
           size_usd = ?,
           stop_loss = ?,
           tp1_size_usd = ?,
           tp1_exit_price = ?,
           tp1_exit_ts = ?,
           tp1_gross_pnl_usd = ?,
           tp1_fees_usd = ?,
           tp1_funding_usd = ?,
           tp1_net_pnl_usd = ?,
           pnl_usd = ?
           WHERE id = ?""",
        (full_size, runner_size, entry, tranche_size, tp1_price, now,
         gross, costs.fee_usd, costs.funding_usd, net, net, trade_id),
    )
    db.execute(
        "INSERT OR REPLACE INTO equity_marks (ts, equity_usd) VALUES (?,?)",
        (now, 500.0 + db.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) s FROM trades "
            "WHERE status IN ('closed','stopped','closed_tp1')"
        ).fetchone()["s"]),
    )
    db.execute(
        "INSERT INTO events (ts, kind, detail) VALUES (?,?,?)",
        (now, "tp1",
         f"TP1 #{trade_id} {row['side']} {row['symbol']} @ {tp1_price} "
         f"({int(ratio*100)}% size ${tranche_size:.2f}) "
         f"gross {gross:+.2f} fees {costs.fee_usd:.2f} funding {costs.funding_usd:.2f} "
         f"net {net:+.2f} [SL -> BE {entry:,.2f}] {note}"),
    )
    db.commit()
    return {
        "id": trade_id,
        "symbol": row["symbol"],
        "side": row["side"],
        "status": "closed_tp1",
        "tranche_size": tranche_size,
        "runner_size": runner_size,
        "ratio": ratio,
        "exit_price": tp1_price,
        "breakeven_sl": entry,
        "gross": gross,
        "net": net,
        "fees": costs.fee_usd,
        "funding": costs.funding_usd,
        "hold_hours": costs.hold_hours,
    }


def close_trade(
    db: sqlite3.Connection,
    trade_id: int,
    exit_price: float,
    exit_ts: float | None = None,
    note: str = "",
    artifact: bool = False,
) -> dict:
    """Close an open trade or the remaining runner of a closed_tp1 trade.

    artifact=True: a build/test row, not a decision. Closed at a real observed
    price for the record, but gross/net zeroed, costs not accrued and the row
    excluded from report().
    """
    ensure_schema(db)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if row is None:
        raise ValueError(f"no trade #{trade_id}")
    if row["status"] in ("closed", "stopped"):
        raise ValueError(f"trade #{trade_id} already {row['status']}")

    now = exit_ts or time.time()
    entry = row["entry_price"]
    direction = 1.0 if row["side"] == "long" else -1.0
    pct = (exit_price - entry) / entry * direction

    is_runner = (row["status"] == "closed_tp1")

    if is_runner:
        runner_size = row["size_usd"]
        runner_gross = runner_size * pct if not artifact else 0.0
        runner_costs = (
            estimate_costs(runner_size, now - row["ts"])
            if not artifact
            else Costs(0.0, 0.0, (now - row["ts"]) / 3600)
        )
        runner_net = runner_gross - runner_costs.total

        tp1_gross = row["tp1_gross_pnl_usd"] or 0.0
        tp1_fees = row["tp1_fees_usd"] or 0.0
        tp1_funding = row["tp1_funding_usd"] or 0.0
        tp1_net = row["tp1_net_pnl_usd"] or 0.0

        total_gross = tp1_gross + runner_gross
        total_fees = tp1_fees + runner_costs.fee_usd
        total_funding = tp1_funding + runner_costs.funding_usd
        total_net = tp1_net + runner_net

        stopped = (direction > 0 and exit_price <= row["stop_loss"]) or (
            direction < 0 and exit_price >= row["stop_loss"]
        )
        status = "stopped" if (stopped and total_net <= 0) else "closed"

        db.execute(
            """UPDATE trades SET status=?, exit_price=?, exit_ts=?, hold_seconds=?,
               gross_pnl_usd=?, fees_usd=?, funding_usd=?, net_pnl_usd=?, pnl_usd=?,
               excluded_from_report=?
               WHERE id=?""",
            (status, exit_price, now, now - row["ts"], total_gross, total_fees,
             total_funding, total_net, total_net, 1 if artifact else 0, trade_id),
        )
        db.execute(
            "INSERT OR REPLACE INTO equity_marks (ts, equity_usd) VALUES (?,?)",
            (now, 500.0 + db.execute(
                "SELECT COALESCE(SUM(pnl_usd),0) s FROM trades "
                "WHERE status IN ('closed','stopped','closed_tp1')"
            ).fetchone()["s"]),
        )
        db.execute(
            "INSERT INTO events (ts, kind, detail) VALUES (?,?,?)",
            (now, "close",
             f"CLOSE #{trade_id} [RUNNER] {row['side']} {row['symbol']} @ {exit_price} "
             f"runner_gross {runner_gross:+.2f} total_gross {total_gross:+.2f} "
             f"total_fees {total_fees:.2f} total_net {total_net:+.2f} [{status}] {note}"),
        )
        db.commit()
        return {
            "id": trade_id,
            "symbol": row["symbol"],
            "side": row["side"],
            "exit_price": exit_price,
            "status": status,
            "is_runner": True,
            "runner_gross": runner_gross,
            "runner_net": runner_net,
            "gross": total_gross,
            "net": total_net,
            "fees": total_fees,
            "funding": total_funding,
            "hold_hours": (now - row["ts"]) / 3600.0,
            "tp1": row["tp1"],
            "tp2": row["tp2"],
        }
    else:
        size = row["size_usd"]
        gross = size * pct
        if artifact:
            costs = Costs(fee_usd=0.0, funding_usd=0.0, hold_hours=(now - row["ts"]) / 3600)
            gross = 0.0
            status = "closed"
        else:
            costs = estimate_costs(size, now - row["ts"])
            stopped = (direction > 0 and exit_price <= row["stop_loss"]) or (
                direction < 0 and exit_price >= row["stop_loss"]
            )
            status = "stopped" if stopped else "closed"

        tp1 = row["tp1"]
        net = gross - costs.total
        db.execute(
            """UPDATE trades SET status=?, exit_price=?, exit_ts=?, hold_seconds=?,
               gross_pnl_usd=?, fees_usd=?, funding_usd=?, net_pnl_usd=?, pnl_usd=?,
               excluded_from_report=?
               WHERE id=?""",
            (status, exit_price, now, now - row["ts"], gross, costs.fee_usd,
             costs.funding_usd, net, net, 1 if artifact else 0, trade_id),
        )
        db.execute(
            "INSERT OR REPLACE INTO equity_marks (ts, equity_usd) VALUES (?,?)",
            (now, 500.0 + db.execute(
                "SELECT COALESCE(SUM(pnl_usd),0) s FROM trades "
                "WHERE status IN ('closed','stopped','closed_tp1')"
            ).fetchone()["s"]),
        )
        db.execute(
            "INSERT INTO events (ts, kind, detail) VALUES (?,?,?)",
            (now, "close",
             f"CLOSE #{trade_id} {row['side']} {row['symbol']} @ {exit_price} "
             f"gross {gross:+.2f} fees {costs.fee_usd:.2f} "
             f"funding {costs.funding_usd:.2f} net {net:+.2f} [{status}] {note}"),
        )
        db.commit()
        return {
            "id": trade_id,
            "symbol": row["symbol"],
            "side": row["side"],
            "exit_price": exit_price,
            "status": status,
            "is_runner": False,
            "gross": gross,
            "net": net,
            "fees": costs.fee_usd,
            "funding": costs.funding_usd,
            "hold_hours": costs.hold_hours,
            "tp1": tp1,
            "tp2": row["tp2"],
        }


# ---------- context capture ----------

def capture(
    db: sqlite3.Connection,
    trade_id: int,
    setup_tag: str | None = None,
    regime: str | None = None,
    timeframe: str | None = None,
    session: str | None = None,
    conviction: int | None = None,
    notes: str | None = None,
    snapshot: dict | None = None,
) -> None:
    """Record WHY, at decision time. The whole point of the exercise."""
    ensure_schema(db)
    if not regime:
        regime = _session_regime()
    db.execute(
        """INSERT OR REPLACE INTO trade_context
           (trade_id, ts, setup_tag, regime, timeframe, session, conviction,
            notes, snapshot, version)
           VALUES (?,?,?,?,?,?,?,?,?,1)""",
        (trade_id, time.time(), setup_tag, regime, timeframe,
         session or _session_name(), conviction, notes,
         json.dumps(snapshot or {})),
    )
    db.execute(
        "UPDATE trades SET setup_tag=?, regime=? WHERE id=?",
        (setup_tag, regime, trade_id),
    )
    db.commit()


def _session_name() -> str:
    h = datetime.now(timezone.utc).hour
    if h < 7:
        return "asia"
    if h < 13:
        return "london"
    if h < 21:
        return "us"
    return "late"


def _session_regime() -> str:
    """Placeholder until a real regime classifier exists — do not trust."""
    return "unclassified"


# ---------- report ----------

def report(db: sqlite3.Connection, min_n: int = MIN_N_FOR_PROMOTION) -> str:
    ensure_schema(db)
    db.row_factory = sqlite3.Row
    closed = db.execute(
        """SELECT t.*, tc.setup_tag AS ctx_setup
           FROM trades t LEFT JOIN trade_context tc ON tc.trade_id = t.id
           WHERE t.status IN ('closed','stopped','closed_tp1')
             AND COALESCE(t.excluded_from_report, 0) = 0"""
    ).fetchall()
    excluded = db.execute(
        "SELECT COUNT(*) c FROM trades WHERE COALESCE(excluded_from_report,0)=1"
    ).fetchone()["c"]

    lines = ["**Trade review — net of fees + funding**"]
    if excluded:
        lines.append(f"({excluded} build-artifact trade(s) excluded from these stats)")
    if not closed:
        lines.append("")
        lines.append("0 measured trades. Nothing to measure yet.")
        opens = db.execute("SELECT COUNT(*) c FROM trades WHERE status='open'").fetchone()["c"]
        if opens:
            lines.append(f"{opens} trade(s) still open — close them first.")
        lines.append("")
        lines.append("No edge claim is possible until n > 0. Log trades, close them, "
                     "then this report becomes the only honest scoreboard.")
        return "\n".join(lines)

    total_net = sum(r["net_pnl_usd"] or 0 for r in closed)
    total_fees = sum(r["fees_usd"] or 0 for r in closed)
    gross = sum(r["gross_pnl_usd"] or 0 for r in closed)
    lines.append(
        f"n={len(closed)} | gross {gross:+.2f} | costs -{total_fees:,.2f} | net {total_net:+.2f}"
    )
    drag = (total_fees / abs(gross) * 100) if gross else float("inf")
    lines.append(
        f"cost drag: {drag:.0f}% of gross" if gross else "cost drag: gross was 0"
    )

    groups: dict[str, list] = {}
    for r in closed:
        groups.setdefault(r["ctx_setup"] or "untagged", []).append(r)

    lines.append("")
    lines.append("**By setup**")
    for tag, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        nets = [r["net_pnl_usd"] or 0 for r in rows]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n <= 0]
        pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) else float("inf")
        exp = sum(nets) / len(nets)
        avg_hold = sum(r["hold_seconds"] or 0 for r in rows) / len(rows) / 3600
        flag = "" if len(rows) >= min_n else f" ⚠️ n<{min_n} = noise"
        lines.append(
            f"· **{tag}** n={len(rows)} | exp {exp:+.2f}/trade | "
            f"win {len(wins)/len(rows)*100:.0f}% | PF {pf:.2f} | "
            f"hold {avg_hold:.1f}h{flag}"
        )
    lines.append("")
    lines.append(f"Promotion gate: a setup needs n≥{min_n} AND PF>1.0 net of costs.")
    lines.append("Costs are modelled, not measured — verify against real fills.")
    return "\n".join(lines)
