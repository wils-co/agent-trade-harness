#!/usr/bin/env python3.11
"""Read-only desk CLI for Hyperliquid + Polymarket.

No keys. No Exchange. No orders. Wraps the official Hermes info clients
and the paper ticket harness already in this repo.

  desk markets
  desk brief ETH
  desk l2 ETH
  desk funding ETH
  desk atr ETH               # daily ATR & target calibration guidance
  desk state                 # needs HYPERLIQUID_USER_ADDRESS
  desk poly [query]
  desk ticket "long eth entry 2500 sl 2400 size 200 setup:vwap-reclaim tf:15m conv:4"
  desk confirm <trade_id>
  desk check
  desk watch [interval_secs]
  desk tp1 <trade_id> [exit_price]
  desk close <trade_id> <exit_price>
  desk flatten <price>
  desk report
  desk status
  desk export                # generate public unitized (R) ledger & SVG equity curve
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
PY = HOME / ".local/bin/python3.11"
if not PY.exists():
    PY = Path(sys.executable)

HL_CANDIDATES = [
    HOME / ".hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py",
    HOME / ".hermes/profiles/35b/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py",
    HOME / ".hermes/hermes-agent/optional-skills/blockchain/hyperliquid/scripts/hyperliquid_client.py",
]
POLY_CANDIDATES = [
    HOME / ".hermes/skills/finance/polymarket/scripts/polymarket.py",
    HOME / ".hermes/profiles/35b/skills/finance/polymarket/scripts/polymarket.py",
    HOME / ".hermes/hermes-agent/optional-skills/finance/polymarket/scripts/polymarket.py",
]


def _first(paths: list[Path]) -> Path:
    for p in paths:
        if p.is_file():
            return p
    raise SystemExit("official skill script not found — reinstall hyperliquid/polymarket")


def _run(script: Path, args: list[str], extra_env: dict[str, str] | None = None) -> int:
    env = os.environ.copy()
    # Project .env is honoured by the HL client (cwd fallback).
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [str(PY), str(script), *args],
        cwd=str(ROOT),
        env=env,
    )
    return proc.returncode


def _hl(args: list[str]) -> int:
    return _run(_first(HL_CANDIDATES), args)


def _poly(args: list[str]) -> int:
    return _run(_first(POLY_CANDIDATES), args)


def print_atr(coin: str = "ETH") -> int:
    url = "https://api.hyperliquid.xyz/info"
    now = int(time.time() * 1000)
    start = now - (20 * 86400 * 1000)
    req = urllib.request.Request(
        url,
        data=json.dumps({
            "type": "candleSnapshot",
            "req": {"coin": coin.upper(), "interval": "1d", "startTime": start}
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            candles = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Failed to fetch candles for ATR: {e}")
        return 1

    if not candles:
        print(f"No candles returned for {coin}.")
        return 1

    trs = []
    prev_close = None
    for c in candles:
        h = float(c["h"])
        l = float(c["l"])
        cl = float(c["c"])
        if prev_close is not None:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        else:
            tr = h - l
        trs.append(tr)
        prev_close = cl

    last_c = float(candles[-1]["c"])
    atr_14 = sum(trs[-14:]) / 14 if len(trs) >= 14 else sum(trs) / len(trs)
    atr_7 = sum(trs[-7:]) / 7 if len(trs) >= 7 else sum(trs) / len(trs)

    pct_14 = (atr_14 / last_c) * 100
    pct_7 = (atr_7 / last_c) * 100

    tp1_pct = pct_14 * 0.55
    tp1_pts = last_c * (tp1_pct / 100)
    tp2_pct = pct_14 * 1.10
    tp2_pts = last_c * (tp2_pct / 100)
    sl_pct = pct_14 * 0.28
    sl_pts = last_c * (sl_pct / 100)

    print(f"Mark / Close: {last_c:,.2f}")
    print(f"14-Day ATR:   ${atr_14:,.2f} ({pct_14:.2f}% / day)")
    print(f" 7-Day ATR:   ${atr_7:,.2f} ({pct_7:.2f}% / day)")
    print("Calibration Guidance:")
    print(f"  Suggested SL  (~0.28x ATR): ±{sl_pct:.2f}% (±${sl_pts:,.2f})")
    print(f"  Suggested TP1 (~0.55x ATR): ±{tp1_pct:.2f}% (±${tp1_pts:,.2f})")
    print(f"  Suggested TP2 (~1.10x ATR): ±{tp2_pct:.2f}% (±${tp2_pts:,.2f})")
    return 0


def _harness(args: list[str]) -> int:
    proc = subprocess.run(
        [str(PY), "-m", "harness.agent", *args],
        cwd=str(ROOT),
    )
    return proc.returncode


def usage() -> int:
    print(__doc__.strip())
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        return usage()
    cmd, *rest = argv

    if cmd == "markets":
        return _hl(["markets", "--limit", "12", "--sort", "volume"])
    if cmd in ("l2", "book"):
        coin = rest[0] if rest else "ETH"
        return _hl(["l2", coin, "--levels", "8"])
    if cmd == "funding":
        coin = rest[0] if rest else "ETH"
        return _hl(["funding", coin, "--hours", "24", "--limit", "8"])
    if cmd in ("atr", "volatility"):
        coin = rest[0] if rest else "ETH"
        return print_atr(coin)
    if cmd == "brief":
        coin = rest[0] if rest else "ETH"
        print(f"=== {coin} book ===", flush=True)
        rc = _hl(["l2", coin, "--levels", "5"])
        if rc:
            return rc
        print(f"\n=== {coin} funding (24h) ===", flush=True)
        rc_f = _hl(["funding", coin, "--hours", "24", "--limit", "8"])
        print(f"\n=== {coin} volatility & daily ATR ===", flush=True)
        print_atr(coin)
        return rc_f
    if cmd == "state":
        return _hl(["state", *rest])
    if cmd == "poly":
        if rest:
            return _poly(["search", " ".join(rest)])
        return _poly(["trending", "--limit", "8"])
    if cmd == "ticket":
        if not rest:
            raise SystemExit('usage: desk ticket "long eth entry 2500 sl 2400 size 200"')
        return _harness(["ticket", " ".join(rest)])
    if cmd == "status":
        return _harness(["status"])
    if cmd == "tp1":
        if not rest:
            raise SystemExit("usage: desk tp1 <trade_id> [exit_price]")
        return _harness(["tp1", *rest])
    if cmd == "close":
        if len(rest) < 2:
            raise SystemExit("usage: desk close <trade_id> <exit_price>")
        return _harness(["close", *rest])
    if cmd == "flatten":
        if not rest:
            raise SystemExit("usage: desk flatten <price>")
        return _harness(["flatten", *rest])
    if cmd == "report":
        return _harness(["report"])
    if cmd == "confirm":
        if not rest:
            raise SystemExit("usage: desk confirm <id>")
        return _harness(["confirm", *rest])
    if cmd == "check":
        return _harness(["check"])
    if cmd == "watch":
        return _harness(["watch", *rest])
    if cmd == "export":
        from harness.export import export_public_ledger
        return export_public_ledger()

    raise SystemExit(f"unknown command: {cmd}\n\n{__doc__.strip()}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
