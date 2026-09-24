"""harness.export — Unitized (R-Multiple) Public Ledger & Scoreboard Generator.

Strips all account equity, margin, and dollar sizing. Normalizes every trade
into institutional R-multiples (risk units), computes quantitative tear-sheet
metrics (Expectancy, Profit Factor, Recovery Factor, MAE/MFE, Hold Dynamics),
generates a retina-grade vector SVG equity curve (assets/equity_curve.svg),
and builds TRADES.md + updates README.md.

Run via:
  python3.11 desk.py export
"""
from __future__ import annotations

import html
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "journal/harness.db"
ASSETS_DIR = ROOT / "assets"
SVG_PATH = ASSETS_DIR / "equity_curve.svg"
TRADES_MD_PATH = ROOT / "TRADES.md"
README_MD_PATH = ROOT / "README.md"



def base_risk_usd() -> float:
    """1R = the per-trade risk cap from the (gitignored) local config."""
    from .risk import load_config
    return float(load_config()["max_risk_per_trade_usd"])


def load_trades(db_path: Path = DB_PATH) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    rows = c.execute(
        "SELECT * FROM trades ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def parse_initial_stop(trade: dict) -> float:
    thesis = trade.get("thesis") or ""
    m = re.search(r"(?:sl|stop)[\s:]*([0-9,.]+)", thesis, re.I)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return trade.get("stop_loss", 0.0)


def determine_trigger(trade: dict, r_mult: float) -> str:
    status = trade.get("status")
    if status == "open":
        return "OPEN"
    if status == "stopped" or r_mult < 0:
        return "SL"
    
    # Check if TP2 or TP1+BE
    exit_px = trade.get("exit_price")
    tp1_px = trade.get("tp1")
    tp2_px = trade.get("tp2")
    side = trade.get("side", "").lower()
    entry_px = trade.get("entry_price")

    if tp2_px and exit_px:
        if (side == "short" and exit_px <= tp2_px * 1.001) or (side == "long" and exit_px >= tp2_px * 0.999):
            return "TP2"
    if trade.get("tp1_exit_price"):
        if exit_px and entry_px and abs(exit_px - entry_px) / entry_px < 0.005:
            return "TP1 + BE"
        return "TP1"
    return "CLOSED"


def compute_metrics(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("status") in ("closed", "stopped", "closed_tp1") and not t.get("excluded_from_report")]
    
    processed = []
    cum_r = 0.0
    hwm = 0.0
    equity_curve = [{"trade": 0, "cum_r": 0.0, "hwm": 0.0, "drawdown": 0.0}]

    for t in closed:
        net_usd = t.get("net_pnl_usd") or 0.0
        r_mult = round(net_usd / base_risk_usd(), 2)
        cum_r = round(cum_r + r_mult, 2)
        if cum_r > hwm:
            hwm = cum_r
        dd = round(cum_r - hwm, 2)

        trigger = determine_trigger(t, r_mult)
        side_arrow = "▲" if t.get("side") == "long" else "▼"
        hold_hours = round((t.get("hold_seconds") or 0.0) / 3600.0, 1)

        processed.append({
            "id": t["id"],
            "ts": t.get("ts"),
            "date": datetime.fromtimestamp(t["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M") if t.get("ts") else "N/A",
            "side": side_arrow,
            "symbol": t.get("symbol", "ETH"),
            "setup": t.get("setup_tag") or "untagged",
            "entry": t.get("entry_price"),
            "stop": parse_initial_stop(t),
            "tp1": t.get("tp1"),
            "tp2": t.get("tp2"),
            "exit": t.get("exit_price"),
            "trigger": trigger,
            "r_multiple": r_mult,
            "cum_r": cum_r,
            "drawdown": dd,
            "hold_hours": hold_hours,
            "fees_funding_usd": (t.get("fees_usd") or 0.0) + (t.get("funding_usd") or 0.0),
            "gross_usd": t.get("gross_pnl_usd") or 0.0,
        })
        equity_curve.append({
            "trade": t["id"],
            "cum_r": cum_r,
            "hwm": hwm,
            "drawdown": dd,
        })

    n = len(processed)
    if n == 0:
        return {"n": 0, "processed": [], "equity_curve": equity_curve}

    wins = [p for p in processed if p["r_multiple"] > 0]
    losses = [p for p in processed if p["r_multiple"] <= 0]
    
    total_net_r = sum(p["r_multiple"] for p in processed)
    gross_won_r = sum(p["r_multiple"] for p in wins)
    gross_lost_r = abs(sum(p["r_multiple"] for p in losses))

    win_rate = (len(wins) / n) * 100.0 if n > 0 else 0.0
    avg_win_r = (gross_won_r / len(wins)) if wins else 0.0
    avg_loss_r = -(gross_lost_r / len(losses)) if losses else 0.0

    profit_factor = (gross_won_r / gross_lost_r) if gross_lost_r > 0 else float("inf")
    payoff_ratio = (avg_win_r / abs(avg_loss_r)) if avg_loss_r != 0 else float("inf")
    expectancy_r = total_net_r / n if n > 0 else 0.0

    max_dd_r = min((p["drawdown"] for p in processed), default=0.0)
    recovery_factor = (total_net_r / abs(max_dd_r)) if max_dd_r < 0 else float("inf")

    # SQN calculation: sqrt(N) * mean(R) / stdev(R)
    if n > 1:
        mean_r = total_net_r / n
        variance = sum((p["r_multiple"] - mean_r) ** 2 for p in processed) / (n - 1)
        stdev_r = math.sqrt(variance) if variance > 0 else 0.001
        sqn = math.sqrt(n) * (mean_r / stdev_r)
    else:
        sqn = 0.0

    # Holding times
    win_hold = sum(p["hold_hours"] for p in wins) / len(wins) if wins else 0.0
    loss_hold = sum(p["hold_hours"] for p in losses) / len(losses) if losses else 0.0

    # Cost drag (% of gross)
    total_gross = sum(p["gross_usd"] for p in processed)
    total_costs = sum(p["fees_funding_usd"] for p in processed)
    cost_drag_pct = (total_costs / total_gross * 100.0) if total_gross > 0 else 0.0

    # Streaks
    max_win_streak = 0
    max_loss_streak = 0
    cur_win = 0
    cur_loss = 0
    for p in processed:
        if p["r_multiple"] > 0:
            cur_win += 1
            cur_loss = 0
        else:
            cur_loss += 1
            cur_win = 0
        max_win_streak = max(max_win_streak, cur_win)
        max_loss_streak = max(max_loss_streak, cur_loss)

    # Setup attribution
    setups = {}
    for p in processed:
        s = p["setup"]
        if s not in setups:
            setups[s] = {"trades": 0, "wins": 0, "net_r": 0.0, "hours": 0.0}
        setups[s]["trades"] += 1
        if p["r_multiple"] > 0:
            setups[s]["wins"] += 1
        setups[s]["net_r"] += p["r_multiple"]
        setups[s]["hours"] += p["hold_hours"]

    setup_table = []
    for s, data in setups.items():
        st_n = data["trades"]
        st_wr = (data["wins"] / st_n) * 100.0 if st_n > 0 else 0.0
        st_exp = data["net_r"] / st_n if st_n > 0 else 0.0
        st_hold = data["hours"] / st_n if st_n > 0 else 0.0
        status_label = "Validated (n>=30)" if st_n >= 30 else f"Incubating (n={st_n}/30)"
        setup_table.append({
            "setup": s,
            "trades": st_n,
            "win_rate": st_wr,
            "net_r": data["net_r"],
            "expectancy": st_exp,
            "avg_hold": st_hold,
            "status": status_label,
        })

    # Active open trades
    open_trades = [t for t in trades if t.get("status") == "open"]

    return {
        "n": n,
        "processed": processed,
        "equity_curve": equity_curve,
        "total_net_r": total_net_r,
        "win_rate": win_rate,
        "wins_count": len(wins),
        "losses_count": len(losses),
        "avg_win_r": avg_win_r,
        "avg_loss_r": avg_loss_r,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff_ratio,
        "expectancy_r": expectancy_r,
        "max_dd_r": max_dd_r,
        "recovery_factor": recovery_factor,
        "sqn": sqn,
        "win_hold": win_hold,
        "loss_hold": loss_hold,
        "cost_drag_pct": cost_drag_pct,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "setup_table": setup_table,
        "open_trades": open_trades,
    }


SVG_THEMES = {
    # GitHub README: dark card that reads in both GitHub themes.
    "dark": {"bg": "#0d1117", "border": "#30363d", "grid": "#21262d", "axis": "#484f58",
             "muted": "#8b949e", "text": "#f0f6fc", "line": "#38bdf8", "hwm": "#64748b",
             "dd": "#f43f5e", "glow": "0.4", "font": "-apple-system, sans-serif"},
    # wilsco.au: paper/ink/gold. Drawdown is grey, not red, so it never relies on red-green.
    "light": {"bg": "#f7f4ee", "border": "#dcd8d0", "grid": "#e7e3db", "axis": "#b9b4aa",
              "muted": "#8a857b", "text": "#201d18", "line": "#b97f34", "hwm": "#8a857b",
              "dd": "#6b665d", "glow": "0.15", "font": "'IBM Plex Mono', ui-monospace, monospace"},
}


def generate_svg(metrics: dict, output_path: Path = SVG_PATH, theme: str = "dark") -> None:
    c = SVG_THEMES[theme]
    font = c["font"]
    eq = metrics.get("equity_curve", [])
    if not eq:
        return

    width = 760
    height = 360
    margin_l = 60
    margin_r = 30
    margin_t = 40
    margin_b = 40

    plot_w = width - margin_l - margin_r
    
    # 2 panels: Upper panel (Cumulative R, 190px), Lower panel (Underwater Drawdown, 70px)
    upper_h = 190
    lower_h = 70
    gap = 20
    upper_y = margin_t
    lower_y = upper_y + upper_h + gap

    # Determine X scale (trade 0 to N)
    n_points = len(eq)
    max_t = max(p["trade"] for p in eq) if n_points > 1 else 1

    def x_coord(t_idx: int) -> float:
        return margin_l + (t_idx / max_t) * plot_w

    # Upper Y scale (R-multiples)
    all_r = [p["cum_r"] for p in eq] + [p["hwm"] for p in eq]
    min_r = min(min(all_r), -0.5)
    max_r = max(max(all_r), 1.0) * 1.15
    r_range = max_r - min_r if max_r != min_r else 1.0

    def y_upper(r_val: float) -> float:
        return upper_y + upper_h - ((r_val - min_r) / r_range) * upper_h

    # Lower Y scale (Drawdown, max_dd to 0)
    min_dd = min(min(p["drawdown"] for p in eq), -1.5)
    dd_range = abs(min_dd) if min_dd != 0 else 1.0

    def y_lower(dd_val: float) -> float:
        return lower_y + ((abs(dd_val) / dd_range)) * lower_h

    # SVG header
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" height="100%">',
        '  <defs>',
        '    <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">',
        f'      <stop offset="0%" stop-color="{c["line"]}" stop-opacity="0.35"/>',
        f'      <stop offset="100%" stop-color="{c["line"]}" stop-opacity="0.0"/>',
        '    </linearGradient>',
        '    <linearGradient id="ddGrad" x1="0" y1="0" x2="0" y2="1">',
        f'      <stop offset="0%" stop-color="{c["dd"]}" stop-opacity="0.05"/>',
        f'      <stop offset="100%" stop-color="{c["dd"]}" stop-opacity="0.35"/>',
        '    </linearGradient>',
        '    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">',
        f'      <feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="{c["line"]}" flood-opacity="{c["glow"]}"/>',
        '    </filter>',
        '  </defs>',
        f'  <rect width="{width}" height="{height}" rx="10" fill="{c["bg"]}" stroke="{c["border"]}" stroke-width="1.5"/>',
    ]

    # Grid lines - Upper Panel
    n_ticks_upper = 5
    for i in range(n_ticks_upper + 1):
        tick_r = min_r + (r_range / n_ticks_upper) * i
        y_pos = y_upper(tick_r)
        svg.append(f'  <line x1="{margin_l}" y1="{y_pos:.1f}" x2="{width - margin_r}" y2="{y_pos:.1f}" stroke="{c["grid"]}" stroke-dasharray="3,3"/>')
        svg.append(f'  <text x="{margin_l - 10}" y="{y_pos + 4:.1f}" font-family="{font}" font-size="11" fill="{c["muted"]}" text-anchor="end">{tick_r:+.1f}R</text>')

    # Zero Line Upper
    if min_r <= 0 <= max_r:
        y_zero = y_upper(0.0)
        svg.append(f'  <line x1="{margin_l}" y1="{y_zero:.1f}" x2="{width - margin_r}" y2="{y_zero:.1f}" stroke="{c["axis"]}" stroke-width="1.2"/>')

    # Grid lines - Lower Panel
    y_dd_zero = lower_y
    svg.append(f'  <line x1="{margin_l}" y1="{y_dd_zero:.1f}" x2="{width - margin_r}" y2="{y_dd_zero:.1f}" stroke="{c["axis"]}" stroke-width="1.2"/>')
    svg.append(f'  <text x="{margin_l - 10}" y="{y_dd_zero + 4:.1f}" font-family="{font}" font-size="11" fill="{c["muted"]}" text-anchor="end">0.0R</text>')
    
    y_dd_bot = lower_y + lower_h
    svg.append(f'  <line x1="{margin_l}" y1="{y_dd_bot:.1f}" x2="{width - margin_r}" y2="{y_dd_bot:.1f}" stroke="{c["grid"]}" stroke-dasharray="3,3"/>')
    svg.append(f'  <text x="{margin_l - 10}" y="{y_dd_bot + 4:.1f}" font-family="{font}" font-size="11" fill="{c["muted"]}" text-anchor="end">{min_dd:.1f}R</text>')

    # Titles & Labels
    svg.append(f'  <text x="{margin_l}" y="24" font-family="{font}" font-size="13" font-weight="600" fill="{c["text"]}">Cumulative Performance ({metrics.get("total_net_r", 0.0):+.2f}R)</text>')
    svg.append(f'  <text x="{width - margin_r}" y="24" font-family="{font}" font-size="11" fill="{c["muted"]}" text-anchor="end">High-Water Mark: {max(p["hwm"] for p in eq):+.2f}R</text>')
    svg.append(f'  <text x="{margin_l}" y="{lower_y - 6}" font-family="{font}" font-size="11" font-weight="600" fill="{c["muted"]}">Underwater Profile (Drawdown)</text>')

    # Draw HWM Stepped Path
    hwm_pts = []
    for p in eq:
        x = x_coord(p["trade"])
        y = y_upper(p["hwm"])
        hwm_pts.append(f"{x:.1f},{y:.1f}")
    svg.append(f'  <polyline points="{" ".join(hwm_pts)}" fill="none" stroke="{c["hwm"]}" stroke-width="1.5" stroke-dasharray="4,4"/>')

    # Draw Area under Cumulative R
    area_pts = [f"{x_coord(0):.1f},{y_upper(0.0):.1f}"]
    line_pts = []
    for p in eq:
        x = x_coord(p["trade"])
        y = y_upper(p["cum_r"])
        area_pts.append(f"{x:.1f},{y:.1f}")
        line_pts.append(f"{x:.1f},{y:.1f}")
    area_pts.append(f"{x_coord(max_t):.1f},{y_upper(0.0):.1f}")
    svg.append(f'  <polygon points="{" ".join(area_pts)}" fill="url(#eqGrad)"/>')
    svg.append(f'  <polyline points="{" ".join(line_pts)}" fill="none" stroke="{c["line"]}" stroke-width="2.5" filter="url(#glow)"/>')

    # Draw Nodes on Equity Line
    for p in eq:
        x = x_coord(p["trade"])
        y = y_upper(p["cum_r"])
        svg.append(f'  <circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{c["bg"]}" stroke="{c["line"]}" stroke-width="2"/>')
        # Label each non-zero point
        if p["trade"] > 0:
            svg.append(f'  <text x="{x:.1f}" y="{y - 10:.1f}" font-family="{font}" font-size="11" font-weight="bold" fill="{c["text"]}" text-anchor="middle">{p["cum_r"]:+.2f}R</text>')

    # Draw Drawdown Area & Line
    dd_area_pts = [f"{x_coord(0):.1f},{lower_y:.1f}"]
    dd_line_pts = []
    for p in eq:
        x = x_coord(p["trade"])
        y = y_lower(p["drawdown"])
        dd_area_pts.append(f"{x:.1f},{y:.1f}")
        dd_line_pts.append(f"{x:.1f},{y:.1f}")
    dd_area_pts.append(f"{x_coord(max_t):.1f},{lower_y:.1f}")
    svg.append(f'  <polygon points="{" ".join(dd_area_pts)}" fill="url(#ddGrad)"/>')
    svg.append(f'  <polyline points="{" ".join(dd_line_pts)}" fill="none" stroke="{c["dd"]}" stroke-width="1.8"/>')

    # X Axis Labels
    for p in eq:
        x = x_coord(p["trade"])
        label = f'#{p["trade"]}' if p["trade"] > 0 else 'Start'
        svg.append(f'  <text x="{x:.1f}" y="{height - 12}" font-family="{font}" font-size="11" fill="{c["muted"]}" text-anchor="middle">{label}</text>')

    svg.append('</svg>')
    
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(svg), encoding="utf-8")


BOX_W = 25  # inner width of each tear-sheet column


def _cell(label: str, value: str) -> str:
    return f" {label}{value:>{BOX_W - 2 - len(label)}} "


def tear_sheet_box(m: dict) -> list[str]:
    """Three-column text box; every row is built to the same width."""
    rows = [
        (("Net Return:", f"{m['total_net_r']:+.2f}R"), ("Max Drawdown:", f"{m['max_dd_r']:+.2f}R"), ("Profit Factor:", f"{m['profit_factor']:.2f}")),
        (("Expectancy:", f"{m['expectancy_r']:+.2f}R/trd"), ("Recovery Factor:", f"{m['recovery_factor']:.2f}"), ("Payoff Ratio:", f"{m['payoff_ratio']:.2f}x")),
        (("Win Rate:", f"{m['win_rate']:.1f}%"), ("Max Consec Loss:", f"{m['max_loss_streak']:d}"), ("Cost Drag:", f"{m['cost_drag_pct']:.1f}%")),
        (("Avg Win:", f"{m['avg_win_r']:+.2f}R"), ("Max Consec Win:", f"{m['max_win_streak']:d}"), ("Avg Win Hold:", f"{m['win_hold']:.1f}h")),
        (("Avg Loss:", f"{m['avg_loss_r']:+.2f}R"), ("SQN Score:", f"{m['sqn']:.2f}"), ("Avg Loss Hold:", f"{m['loss_hold']:.1f}h")),
    ]
    rule = "─" * BOX_W
    heads = ("EDGE & EXPECTANCY", "CAPITAL PROTECTION", "EXECUTION EFFICIENCY")
    out = [f"┌{rule}┬{rule}┬{rule}┐", "│" + "│".join(h.center(BOX_W) for h in heads) + "│", f"├{rule}┼{rule}┼{rule}┤"]
    out += ["│" + "│".join(_cell(*c) for c in row) + "│" for row in rows]
    out.append(f"└{rule}┴{rule}┴{rule}┘")
    return out


def generate_trades_md(metrics: dict, output_path: Path = TRADES_MD_PATH) -> None:
    lines = [
        "# Paper Trade Ledger (Unitized R)",
        "",
        "> [!NOTE]",
        "> **Paper trades** — journaled tickets, no real capital; the harness cannot place live orders. All performance is unitized in **R-multiples** (risk units per trade) net of modeled taker fees and funding drag. Dollar sizing, margin, and account balances are strictly omitted.",
        "",
        "## Performance Tear-Sheet",
        "",
        "```text",
        *tear_sheet_box(metrics),
        "```",
        "",
        "## Cumulative Performance & Drawdown Profile",
        "",
        "![Cumulative Performance](assets/equity_curve.svg)",
        "",
        "---",
        "",
        "## The Book",
        "",
        "```text",
    ]

    # Compact Book List
    for p in metrics["processed"]:
        lines.append(f"#{p['id']} {p['side']} {p['symbol']} · {p['trigger']} · R:R {p['r_multiple']:.2f}{' · ' + p['setup'] if p['setup'] != 'untagged' else ''}")

    for ot in metrics["open_trades"]:
        arrow = "▲" if ot.get("side") == "long" else "▼"
        setup = ot.get("setup_tag") or "untagged"
        lines.append(f"#{ot['id']} {arrow} {ot.get('symbol', 'ETH')} · open · {setup}")
        lines.append(f"   └ entry {ot.get('entry_price'):,.2f} · sl {ot.get('stop_loss'):,.2f} · tp1 {ot.get('tp1'):,.2f} · tp2 {ot.get('tp2'):,.2f}")

    lines.extend([
        "```",
        "",
        "---",
        "",
        "## Setup Attribution (Alpha Decomposition)",
        "",
        "| Setup Tag | Trades | Win Rate | Net Return | Expectancy | Avg Hold | Gate Status |",
        "| :--- | :-: | :-: | -: | -: | -: | :--- |",
    ])

    for st in metrics["setup_table"]:
        lines.append(f"| `{st['setup']}` | {st['trades']} | {st['win_rate']:.1f}% | {st['net_r']:+.2f}R | {st['expectancy']:+.2f}R | {st['avg_hold']:.1f}h | {st['status']} |")

    lines.extend([
        "",
        "---",
        "",
        "## Chronological Trade Ledger",
        "",
        "| # | Date (UTC) | Dir | Symbol | Setup | Entry | Stop | TP1 | TP2 | Exit | Result | Hold | Realized R | Cum R |",
        "| :- | :- | :-: | :- | :- | -: | -: | -: | -: | -: | :-: | -: | -: | -: |",
    ])

    for p in metrics["processed"]:
        lines.append(
            f"| **{p['id']}** | {p['date']} | {p['side']} | {p['symbol']} | `{p['setup']}` | "
            f"{p['entry']:,.2f} | {p['stop']:,.2f} | {p['tp1']:,.2f} | {p['tp2']:,.2f} | "
            f"{p['exit']:,.2f} | {p['trigger']} | {p['hold_hours']:.1f}h | "
            f"**{p['r_multiple']:+.2f}R** | **{p['cum_r']:+.2f}R** |"
        )

    for ot in metrics["open_trades"]:
        arrow = "▲" if ot.get("side") == "long" else "▼"
        dt = datetime.fromtimestamp(ot["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M") if ot.get("ts") else "N/A"
        lines.append(
            f"| **{ot['id']}** | {dt} | {arrow} | {ot.get('symbol', 'ETH')} | `{ot.get('setup_tag') or 'untagged'}` | "
            f"{ot.get('entry_price'):,.2f} | {ot.get('stop_loss'):,.2f} | {ot.get('tp1'):,.2f} | {ot.get('tp2'):,.2f} | "
            f"*open* | *active* | *holding* | *open* | — |"
        )

    lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def update_readme(metrics: dict, readme_path: Path = README_MD_PATH) -> None:
    if not readme_path.exists():
        return

    content = readme_path.read_text(encoding="utf-8")

    scorecard_block = f"""<!-- SCOREBOARD_START -->
## Verified Performance (Unitized R)

![Cumulative Performance](assets/equity_curve.svg)

| Metric | Result | Metric | Result |
| :--- | :--- | :--- | :--- |
| **Cumulative Return** | **{metrics['total_net_r']:+.2f}R** | **Max Drawdown** | **{metrics['max_dd_r']:+.2f}R** |
| **Expectancy** | **{metrics['expectancy_r']:+.2f}R / trade** | **Profit Factor** | **{metrics['profit_factor']:.2f}** |
| **Win Rate** | **{metrics['win_rate']:.1f}%** ({metrics['wins_count']}W / {metrics['losses_count']}L) | **Recovery Factor** | **{metrics['recovery_factor']:.2f}** |
| **Cost Drag** | **{metrics['cost_drag_pct']:.1f}% of gross** | **Payoff Ratio** | **{metrics['payoff_ratio']:.2f}x** |

👉 **[View Full Verified Trade Ledger & Setup Attribution (TRADES.md)](TRADES.md)** · **[Live Desk Dashboard (wilsco.au/trade)](https://wilsco.au/trade)**
<!-- SCOREBOARD_END -->"""

    if "<!-- SCOREBOARD_START -->" in content:
        new_content = re.sub(
            r"<!-- SCOREBOARD_START -->.*?<!-- SCOREBOARD_END -->",
            scorecard_block,
            content,
            flags=re.DOTALL,
        )
    else:
        # Insert before License or at end of architecture
        pos = content.find("## Running tests")
        if pos != -1:
            new_content = content[:pos] + scorecard_block + "\n\n" + content[pos:]
        else:
            new_content = content + "\n\n" + scorecard_block + "\n"

    readme_path.write_text(new_content, encoding="utf-8")


WILSCO_SITE_TRADE_DIR = Path.home() / "Dev/Wilsco-site/trade"
SITE_MARKER = re.compile(r"(<!-- LEDGER:(\w+) -->).*?(<!-- /LEDGER:\2 -->)", re.S)


def _esc(text) -> str:
    return html.escape(str(text), quote=False)


def _r(v: float) -> str:
    return f"{v:+.2f}R".replace("-", "−")


def _px(v) -> str:
    return f"{v:,.2f}" if v is not None else "·"


def site_blocks(metrics: dict) -> dict[str, str]:
    """HTML fragments for wilsco.au/trade. R-multiples and prices only, never dollars."""
    m = metrics
    blocks = {}
    blocks["stats"] = "\n".join([
        f'    <div class="stat"><b>{_r(m["total_net_r"])}</b><span>Net result</span></div>',
        f'    <div class="stat"><b>{_r(m["expectancy_r"])}</b><span>Average per trade</span></div>',
        f'    <div class="stat"><b>{m["win_rate"]:.0f}%</b><span>Win rate ({m["wins_count"]}W / {m["losses_count"]}L)</span></div>',
        f'    <div class="stat"><b>{m["n"]}</b><span>Closed trades (gate is 30)</span></div>',
    ])
    cols = [
        ("Edge", [("Net result", _r(m["total_net_r"])), ("Average per trade", _r(m["expectancy_r"])),
                  ("Win rate", f'{m["win_rate"]:.1f}%'), ("Average win", _r(m["avg_win_r"])),
                  ("Average loss", _r(m["avg_loss_r"]))]),
        ("Protection", [("Max drawdown", _r(m["max_dd_r"])), ("Recovery factor", f'{m["recovery_factor"]:.2f}'),
                        ("Longest losing run", str(m["max_loss_streak"])), ("Longest winning run", str(m["max_win_streak"])),
                        ("SQN", f'{m["sqn"]:.2f}')]),
        ("Execution", [("Profit factor", f'{m["profit_factor"]:.2f}'), ("Payoff ratio", f'{m["payoff_ratio"]:.2f}×'),
                       ("Costs as % of gross", f'{m["cost_drag_pct"]:.1f}%'), ("Average win held", f'{m["win_hold"]:.1f}h'),
                       ("Average loss held", f'{m["loss_hold"]:.1f}h')]),
    ]
    blocks["tearsheet"] = "\n".join(
        f'      <div class="tear-col">\n        <h4>{h}</h4>\n'
        + "\n".join(f'        <div class="tear-row"><span>{k}</span><b>{v}</b></div>' for k, v in rows)
        + "\n      </div>" for h, rows in cols)

    opens = []
    for t in m.get("open_trades", []):
        arrow = "▲" if t["side"] == "long" else "▼"
        entry, stop = t["entry_price"], t["stop_loss"]
        risk = abs(entry - stop)
        levels = [("stop", stop, None), ("entry", entry, None), ("tp1", t.get("tp1"), 0.8), ("tp2", t.get("tp2"), 0.2)]
        levels = [lv for lv in levels if lv[1]]
        levels.sort(key=lambda lv: -lv[1])  # highest price on top
        rows = []
        for name, px, _share in levels:
            note = ""
            if name.startswith("tp") and risk:
                note = f" · {abs(px - entry) / risk:.2f}R"
            elif name == "stop":
                note = " · −1R before costs"
            rows.append(f"{name:<6}{_px(px)}{note}")
        setup = t.get("setup_tag") or "untagged"
        opens.append(
            '    <div class="active-posture">\n'
            f'      <div class="status-tag">Open · paper</div>\n'
            f'      <h3>Trade #{t["id"]} · {arrow} {_esc(t["symbol"])}</h3>\n'
            f'      <p class="setup">Setup: <code>{_esc(setup)}</code></p>\n'
            f'      <pre class="active-ladder">{chr(10).join(rows)}</pre>\n'
            "    </div>")
    blocks["open"] = "\n".join(opens) if opens else '    <p class="prose">Nothing open right now.</p>'

    blocks["setups"] = "\n".join(
        "          <tr>"
        f"<td><code>{_esc(r['setup'])}</code></td><td class=\"num\">{r['trades']}</td>"
        f"<td class=\"num\">{r['win_rate']:.0f}%</td><td class=\"num\"><strong>{_r(r['net_r'])}</strong></td>"
        f"<td class=\"num\">{_r(r['expectancy'])}</td><td class=\"num\">{r['avg_hold']:.1f}h</td>"
        f"<td class=\"tag-cell\">{_esc(r['status'])}</td></tr>"
        for r in m["setup_table"])

    book = []
    for p_ in m["processed"]:
        book.append(
            "          <tr>"
            f"<td><strong>#{p_['id']}</strong></td><td>{p_['date']}</td><td>{p_['side']}</td><td>{_esc(p_['symbol'])}</td>"
            f"<td><code>{_esc(p_['setup'])}</code></td><td class=\"num\">{_px(p_['entry'])}</td>"
            f"<td class=\"num\">{_px(p_['stop'])}</td><td class=\"num\">{_px(p_['exit'])}</td>"
            f"<td>{_esc(p_['trigger'])}</td><td class=\"num\">{p_['hold_hours']:.1f}h</td>"
            f"<td class=\"num\"><strong>{_r(p_['r_multiple'])}</strong></td></tr>")
    for t in m.get("open_trades", []):
        arrow = "▲" if t["side"] == "long" else "▼"
        opened = datetime.fromtimestamp(t["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        book.append(
            "          <tr>"
            f"<td><strong>#{t['id']}</strong></td><td>{opened}</td><td>{arrow}</td><td>{_esc(t['symbol'])}</td>"
            f"<td><code>{_esc(t.get('setup_tag') or 'untagged')}</code></td><td class=\"num\">{_px(t['entry_price'])}</td>"
            f"<td class=\"num\">{_px(t['stop_loss'])}</td><td class=\"num\">·</td>"
            f"<td>open</td><td class=\"num\">·</td><td class=\"num\">open</td></tr>")
    blocks["book"] = "\n".join(book)
    blocks["asof"] = datetime.now(tz=timezone.utc).astimezone().strftime("%-d %B %Y")
    return blocks


def fill_site_page(page: str, blocks: dict[str, str]) -> str:
    def sub(mt: re.Match) -> str:
        name = mt.group(2)
        if name not in blocks:
            return mt.group(0)
        body = blocks[name]
        inline = "\n" not in body
        return mt.group(1) + (body if inline else "\n" + body + "\n") + mt.group(3)
    return SITE_MARKER.sub(sub, page)


def sync_to_wilsco_site(metrics: dict, site_dir: Path = WILSCO_SITE_TRADE_DIR) -> bool:
    """Regenerate wilsco.au/trade from the same metrics: light-theme SVG plus the page's LEDGER blocks."""
    page_path = site_dir / "index.html"
    if not page_path.exists():
        return False
    generate_svg(metrics, site_dir / "equity_curve.svg", theme="light")
    page_path.write_text(fill_site_page(page_path.read_text(encoding="utf-8"), site_blocks(metrics)), encoding="utf-8")
    return True


def export_public_ledger() -> int:
    trades = load_trades()
    if not trades:
        print("No trades found in database.")
        return 1

    metrics = compute_metrics(trades)
    generate_svg(metrics)
    generate_trades_md(metrics)
    update_readme(metrics)
    sync_to_wilsco_site(metrics)

    print("✅ Public export complete (Unitized R):")
    print(f"  • Cumulative Return: {metrics['total_net_r']:+.2f}R (n={metrics['n']})")
    print(f"  • Expectancy:        {metrics['expectancy_r']:+.2f}R / trade")
    print(f"  • Profit Factor:     {metrics['profit_factor']:.2f}")
    print(f"  • Win Rate:          {metrics['win_rate']:.1f}%")
    print(f"  • Equity Curve:      {SVG_PATH.relative_to(ROOT)}")
    print(f"  • Trade Ledger:      {TRADES_MD_PATH.relative_to(ROOT)}")
    print(f"  • README updated:    {README_MD_PATH.relative_to(ROOT)}")
    if WILSCO_SITE_TRADE_DIR.exists():
        print(f"  • Wilsco-site sync:  {WILSCO_SITE_TRADE_DIR}/")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(export_public_ledger())
