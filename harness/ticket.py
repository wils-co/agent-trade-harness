"""Ticket parser — turns a one-line Telegram ticket into a structured order.

Accepted formats (order of fields doesn't matter, case-insensitive):

  long btc | lev 2x | size 250 | entry 112400 | sl 110900 | tp1 113800 | tp2 115600
  short eth size 200 entry 3120 sl 3180 tp1 3060
  long btc entry 112400 sl 110900            # size defaults to risk-cap-derived max
  long eth entry 2389 sl 2377 risk 5         # fixed $ risk; lev auto-set so liq stays past the stop

Also accepts fib shorthand:
  long btc fib 0:112400 0.618:115800 sl 111500 size 250

Returns dict or raises TicketError with a human-readable reason.
"""

from __future__ import annotations
import re


class TicketError(ValueError):
    pass


NUM = r"[\d,]+\.?\d*"

FIELD_PATTERNS = {
    "entry": re.compile(rf"entry[:\s]+({NUM})", re.I),
    "sl": re.compile(rf"(?:sl|stop)[:\s]+({NUM})", re.I),
    "tp1": re.compile(rf"tp\s*1[:\s]+({NUM})", re.I),
    "tp2": re.compile(rf"tp\s*2[:\s]+({NUM})", re.I),
    "size": re.compile(rf"size[:\s]+(?:usd[:\s]+)?({NUM})", re.I),
    "risk": re.compile(rf"risk[:\s]+(?:usd[:\s]+)?\$?({NUM})", re.I),
    "lev": re.compile(rf"(?:lev|leverage)[:\s]+(\d+)\s*x?", re.I),
    "split": re.compile(r"(?:split|ratio|tp1_pct|tp1_ratio)[:\s]+(\d+)(?:[/:]\d+)?", re.I),
}

# Decision context — optional, but this is what makes a trade learnable.
TAG_PATTERNS = {
    "setup": re.compile(r"setup[:=\s]+([a-z0-9_\-]+)", re.I),
    "regime": re.compile(r"regime[:=\s]+([a-z0-9_\-]+)", re.I),
    "timeframe": re.compile(r"(?:tf|timeframe)[:=\s]+([a-z0-9]+)", re.I),
    "conviction": re.compile(r"(?:conv|conviction)[:=\s]+([1-5])", re.I),
}

SIDE_PAT = re.compile(r"\b(long|short)\b", re.I)
SYMBOL_PAT = re.compile(
    r"\b(btc|eth|sol|arb|doge|xrp|avax|link|dot|hype)\b", re.I
)
LOOSE_DOLLAR_PAT = re.compile(rf"\$({NUM})")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def fetch_live_price(symbol: str) -> float | None:
    """Fast live mid-price lookup from Hyperliquid info API."""
    import urllib.request
    import json
    try:
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "allMids"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "agent-trade-harness"},
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            mids = json.loads(resp.read().decode("utf-8"))
            sym = symbol.upper()
            if sym in mids:
                return float(mids[sym])
    except Exception:
        pass
    return None


def parse_ticket(text: str) -> dict:
    t = text.strip()

    side_m = SIDE_PAT.search(t)
    if not side_m:
        raise TicketError("no side — start the ticket with 'long' or 'short'")
    side = side_m.group(1).lower()

    sym_m = SYMBOL_PAT.search(t.replace(side, "", 1))
    if not sym_m:
        raise TicketError("no symbol found (btc, eth, ...)")
    symbol = sym_m.group(1).upper()
    if symbol == side.upper():
        # pathological; symbol pattern matched something weird
        raise TicketError("could not read symbol")

    fields = {}
    for name, pat in FIELD_PATTERNS.items():
        m = pat.search(t)
        if m:
            fields[name] = _num(m.group(1))

    # Loose dollar amount (e.g. "long eth $35 ...") defaults to risk_usd
    if "risk" not in fields and "size" not in fields:
        m_dollar = LOOSE_DOLLAR_PAT.search(t)
        if m_dollar:
            fields["risk"] = _num(m_dollar.group(1))

    if "entry" not in fields:
        live_px = fetch_live_price(symbol)
        if live_px is not None:
            fields["entry"] = live_px
        else:
            raise TicketError("missing entry price (live price lookup unavailable)")

    if "sl" not in fields:
        raise TicketError("missing sl — stop-loss is mandatory")

    tags = {}
    for name, pat in TAG_PATTERNS.items():
        m = pat.search(t)
        if m:
            tags[name] = int(m.group(1)) if name == "conviction" else m.group(1).lower()

    # TP split ratio: default to 80% at TP1 (80/20)
    tp1_ratio = 0.80
    if "split" in fields:
        val = fields["split"]
        if 0 < val < 1:
            tp1_ratio = val
        elif 1 <= val <= 99:
            tp1_ratio = val / 100.0

    leverage = int(fields.get("lev", 1))
    return {
        "side": side,
        "symbol": symbol,
        "entry": fields["entry"],
        "sl": fields["sl"],
        "tp1": fields.get("tp1"),
        "tp2": fields.get("tp2"),
        "tp1_ratio": tp1_ratio,
        "size_usd": fields.get("size"),   # None => caller derives from risk cap
        "risk_usd": fields.get("risk"),   # explicit $ risk -> sizing + liq-aware leverage
        "leverage": leverage,
        "setup": tags.get("setup"),
        "regime": tags.get("regime"),
        "timeframe": tags.get("timeframe"),
        "conviction": tags.get("conviction"),
    }


def derive_size(ticket: dict, max_risk_usd: float) -> float:
    """Largest position size whose SL-implied risk fits under max_risk_usd."""
    entry, sl = ticket["entry"], ticket["sl"]
    dist_pct = abs(entry - sl) / entry * 100
    if dist_pct <= 0:
        raise TicketError("SL equals entry")
    return round(max_risk_usd * 100 / dist_pct, 2)


def format_ticket(o: dict) -> str:
    """Human-readable echo-back for confirmation."""
    lines = [
        f"{o['side'].upper()} {o['symbol']} | {o['leverage']}x | ${o['size_usd']:.0f}",
        f"entry {o['entry']:,.2f} | sl {o['sl']:,.2f}",
    ]
    r1 = int((o.get("tp1_ratio") or 0.80) * 100)
    r2 = 100 - r1
    if o.get("tp1"):
        lines[-1] += f" | tp1 {o['tp1']:,.2f} ({r1}%)"
    if o.get("tp2"):
        lines[-1] += f" | tp2 {o['tp2']:,.2f} ({r2}%)"
    ctx = [f"{k}={o[k]}" for k in ("setup", "regime", "timeframe", "conviction")
           if o.get(k)]
    if ctx:
        lines.append("ctx: " + " ".join(str(c) for c in ctx))
    return "\n".join(lines)


if __name__ == "__main__":
    demo = (
        "long btc | lev 1x | size 250 | "
        "entry 78771 | sl 76450 | tp1 83019 | tp2 84500"
    )
    print(format_ticket(parse_ticket(demo)))
