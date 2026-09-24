"""Risk engine — deterministic, non-bypassable guardrails.

Every order passes through check_order() before reaching the exchange.
The agent (LLM) never calls the exchange directly; it can only propose
orders that this engine approves, clips, or rejects.
"""

from __future__ import annotations
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "risk_config.json"

DEFAULT_CONFIG = {
    "max_risk_per_trade_usd": 10.0,
    "max_position_notional_usd": 500.0,
    "daily_loss_cap_usd": 25.0,
    "max_drawdown_pct": 8.0,
    "max_leverage": 1,
    "require_stop_loss": True,
    "loss_streak_pause": 3,
    "cooldown_seconds": 86400,
    "allowed_symbols": ["BTC", "ETH"],
    "starting_equity_usd": 500.0,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


@dataclass
class Decision:
    approved: bool
    reason: str
    clipped_size_usd: float | None = None  # set when size was reduced


class RiskEngine:
    def __init__(self, db_path: str):
        self.cfg = load_config()
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                size_usd REAL NOT NULL,
                stop_loss REAL NOT NULL,
                tp1 REAL,
                tp2 REAL,
                leverage INTEGER NOT NULL DEFAULT 1,
                thesis TEXT,
                status TEXT NOT NULL DEFAULT 'open',  -- open|closed_tp1|closed|stopped|rejected
                exit_price REAL,
                pnl_usd REAL,
                chart_image TEXT,
                orig_size_usd REAL,
                tp1_ratio REAL DEFAULT 0.80,
                tp1_size_usd REAL,
                tp1_exit_price REAL,
                tp1_exit_ts REAL,
                tp1_gross_pnl_usd REAL,
                tp1_fees_usd REAL,
                tp1_funding_usd REAL,
                tp1_net_pnl_usd REAL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,       -- order|reject|rail_trip|kill|note
                detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS equity_marks (
                ts REAL PRIMARY KEY,
                equity_usd REAL NOT NULL
            );
        """)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(trades)")}
        extra_cols = {
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
        for col, typ in extra_cols.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
        self.db.commit()

    # ---------- helpers ----------

    def log(self, kind: str, detail: str):
        self.db.execute(
            "INSERT INTO events (ts, kind, detail) VALUES (?,?,?)",
            (time.time(), kind, detail),
        )
        self.db.commit()

    def kill_switch_active(self) -> bool:
        return Path(__file__).parent.joinpath("KILL").exists()

    def activate_kill_switch(self, reason: str = "manual"):
        Path(__file__).parent.joinpath("KILL").write_text(
            f"{datetime.now(timezone.utc).isoformat()} — {reason}\n"
        )
        self.log("kill", f"KILL SWITCH ACTIVATED: {reason}")

    def deactivate_kill_switch(self):
        p = Path(__file__).parent / "KILL"
        if p.exists():
            p.unlink()
            self.log("note", "kill switch deactivated")

    def realized_pnl_today(self) -> float:
        midnight = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        row = self.db.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) AS s FROM trades "
            "WHERE status IN ('closed','closed_tp1','stopped') AND ts >= ?",
            (midnight,),
        ).fetchone()
        return row["s"]

    def total_realized_pnl(self) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) AS s FROM trades "
            "WHERE status IN ('closed','closed_tp1','stopped')"
        ).fetchone()
        return row["s"]

    def current_equity(self) -> float:
        return self.cfg["starting_equity_usd"] + self.total_realized_pnl()

    def _equity_curve(self) -> list[float]:
        """Realized equity series: starting_equity, then one point per close.

        Peak/trough logic runs on this, NOT on cumulative PnL, so the
        high-water mark starts at starting_equity rather than 0.
        """
        rows = self.db.execute(
            "SELECT SUM(pnl_usd) OVER (ORDER BY ts, id) AS cum FROM trades "
            "WHERE status IN ('closed','closed_tp1','stopped') ORDER BY ts, id"
        ).fetchall()
        start = self.cfg["starting_equity_usd"]
        return [start] + [start + r["cum"] for r in rows]

    def max_drawdown_pct(self) -> float:
        """Worst peak-to-trough decline over all history, as a % of the peak
        that preceded that trough. Reporting stat — never falls once set."""
        peak = None
        max_dd = 0.0
        for eq in self._equity_curve():
            if peak is None or eq > peak:
                peak = eq
            if peak <= 0:
                return 100.0
            max_dd = max(max_dd, (peak - eq) / peak * 100.0)
        return max_dd

    def current_drawdown_pct(self) -> float:
        """How far below the running high-water mark we are RIGHT NOW,
        as a % of that high-water mark. 0.0 at a new equity high.
        This is the kill-switch input."""
        curve = self._equity_curve()
        peak = max(curve)
        if peak <= 0:
            return 100.0
        return max(0.0, (peak - curve[-1]) / peak * 100.0)

    def loss_streak(self) -> int:
        rows = self.db.execute(
            "SELECT pnl_usd FROM trades WHERE status IN ('closed','stopped') "
            "ORDER BY ts DESC LIMIT ?",
            (self.cfg["loss_streak_pause"],),
        ).fetchall()
        if len(rows) < self.cfg["loss_streak_pause"]:
            return 0
        return self.cfg["loss_streak_pause"] if all(
            r["pnl_usd"] < 0 for r in rows
        ) else 0

    def in_cooldown(self) -> tuple[bool, str]:
        n = self.loss_streak()
        if n >= self.cfg["loss_streak_pause"]:
            last = self.db.execute(
                "SELECT MAX(ts) AS t FROM trades WHERE pnl_usd < 0"
            ).fetchone()["t"]
            if last and time.time() - last < self.cfg["cooldown_seconds"]:
                hrs = self.cfg["cooldown_seconds"] / 3600
                return True, f"loss streak {n}: cooling down {hrs}h after last loss"
        return False, ""

    # ---------- THE gate ----------

    def check_order(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        size_usd: float,
        stop_loss: float,
        leverage: int = 1,
        tp1: float | None = None,
        tp2: float | None = None,
    ) -> Decision:
        """Returns Decision. This is the ONLY path to execution."""
        c = self.cfg

        # 0. kill switch — checked first, every time
        if self.kill_switch_active():
            self.log("rail_trip", f"REJECT {symbol}: KILL switch active")
            return Decision(False, "KILL switch active — trading locked")

        # 1. symbol allowlist
        base = symbol.split("-")[0].upper()
        if base not in [s.upper() for s in c["allowed_symbols"]]:
            self.log("rail_trip", f"REJECT {symbol}: not in allowlist")
            return Decision(False, f"{base} not in allowed symbols {c['allowed_symbols']}")

        # 2. leverage cap
        if leverage > c["max_leverage"]:
            self.log("rail_trip", f"REJECT {symbol}: leverage {leverage}x > cap")
            return Decision(False, f"Leverage {leverage}x exceeds cap {c['max_leverage']}x")

        # 3. required stop-loss + risk math
        if c["require_stop_loss"] and (stop_loss is None or stop_loss <= 0):
            self.log("rail_trip", f"REJECT {symbol}: no stop-loss")
            return Decision(False, "Stop-loss is mandatory")

        sl_dist_pct = abs(entry_price - stop_loss) / entry_price * 100
        risk_usd = size_usd * sl_dist_pct / 100
        if round(risk_usd, 2) > c["max_risk_per_trade_usd"]:
            self.log(
                "rail_trip",
                f"REJECT {symbol}: SL implies ${risk_usd:.2f} risk > ${c['max_risk_per_trade_usd']} cap "
                f"(SL dist {sl_dist_pct:.2f}% on ${size_usd:.0f})",
            )
            return Decision(
                False,
                f"Stop distance {sl_dist_pct:.2f}% implies ${risk_usd:.2f} risk; "
                f"cap is ${c['max_risk_per_trade_usd']}. Tighten SL or reduce size.",
            )

        # 4. side/SL sanity (long: SL below entry, TPs above; short: SL above entry, TPs below)
        if side == "long":
            if stop_loss >= entry_price:
                return Decision(False, "Long requires stop BELOW entry")
            if tp1 is not None and tp1 <= entry_price:
                return Decision(False, "Long TP1 must be above entry")
            if tp2 is not None:
                if tp1 is not None and tp2 <= tp1:
                    return Decision(False, "Long TP2 must be above TP1")
                elif tp2 <= entry_price:
                    return Decision(False, "Long TP2 must be above entry")
        elif side == "short":
            if stop_loss <= entry_price:
                return Decision(False, "Short requires stop ABOVE entry")
            if tp1 is not None and tp1 >= entry_price:
                return Decision(False, "Short TP1 must be below entry")
            if tp2 is not None:
                if tp1 is not None and tp2 >= tp1:
                    return Decision(False, "Short TP2 must be below TP1")
                elif tp2 >= entry_price:
                    return Decision(False, "Short TP2 must be below entry")
        else:
            return Decision(False, f"Unknown side: {side}")

        # 5. position notional cap (clip, don't reject)
        clipped = None
        if size_usd > c["max_position_notional_usd"]:
            clipped = c["max_position_notional_usd"]
            self.log("rail_trip", f"CLIP {symbol}: ${size_usd:.0f} -> ${clipped:.0f}")
            size_usd = clipped
            risk_usd = size_usd * sl_dist_pct / 100
            if risk_usd > c["max_risk_per_trade_usd"]:
                return Decision(
                    False,
                    f"Even at max size ${clipped:.0f}, risk ${risk_usd:.2f} exceeds cap",
                )

        # 6. daily loss cap
        today_pnl = self.realized_pnl_today()
        if today_pnl <= -c["daily_loss_cap_usd"]:
            self.log("rail_trip", f"REJECT {symbol}: daily loss cap hit (${today_pnl:.2f})")
            return Decision(
                False,
                f"Daily loss cap reached ({today_pnl:.2f} vs -{c['daily_loss_cap_usd']}). "
                "Locked until next UTC day.",
            )
        if today_pnl - risk_usd < -c["daily_loss_cap_usd"]:
            max_size = (
                (today_pnl + c["daily_loss_cap_usd"]) * 100 / sl_dist_pct
            )
            clipped = min(size_usd, max(0, max_size))
            self.log("rail_trip", f"CLIP {symbol}: daily-cap-aware size ${clipped:.0f}")
            size_usd = clipped

        # 7. current drawdown below high-water mark → full stop
        dd = self.current_drawdown_pct()
        if dd >= c["max_drawdown_pct"]:
            self.activate_kill_switch(f"drawdown {dd:.1f}% >= {c['max_drawdown_pct']}%")
            return Decision(False, f"Drawdown {dd:.1f}% below high-water mark — KILL switch engaged")

        # 8. loss-streak cooldown
        cooling, why = self.in_cooldown()
        if cooling:
            self.log("rail_trip", f"REJECT {symbol}: {why}")
            return Decision(False, why)

        return Decision(True, "approved", clipped_size_usd=size_usd)


if __name__ == "__main__":
    import sys
    eng = RiskEngine(sys.argv[1] if len(sys.argv) > 1 else "journal/harness.db")
    print(json.dumps(load_config(), indent=2))
