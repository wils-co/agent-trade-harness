"""Executor — Hyperliquid testnet/mainnet order routing.

Phase 1-3: TESTNET=True, zero real funds.
Phase 4+: set TESTNET=False and provide API-wallet secret in env.

Env vars:
  HL_TESTNET_SECRET   — agent wallet private key (testnet)
  HL_MAINNET_SECRET   — API wallet key (mainnet, withdrawal-scoped)

The executor NEVER holds the master wallet key.
"""

from __future__ import annotations
import os
from dataclasses import dataclass

TESTNET = True  # flip to False only at Phase 4


@dataclass
class FillResult:
    ok: bool
    detail: str
    order_id: str | None = None


class PaperExecutor:
    """Testnet/paper fills — records intent, simulates fill at entry."""

    def __init__(self, risk_engine):
        self.risk = risk_engine

    def market_order(self, symbol: str, side: str, size_usd: float,
                     price: float) -> FillResult:
        px = f"{price}"
        sz = round(size_usd / price, 5)
        self.risk.log("order",
                      f"PAPER FILL {side} {symbol} ${size_usd:.2f} @ {px}")
        return FillResult(True, f"paper fill {sz} {symbol} @ {px}")


class HyperliquidExecutor:
    """Real exchange calls. Requires funded agent/API wallet key in env."""

    def __init__(self, risk_engine):
        self.risk = risk_engine
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        secret = (os.getenv("HL_TESTNET_SECRET") if TESTNET
                  else os.getenv("HL_MAINNET_SECRET"))
        if not secret:
            raise RuntimeError(
                f"no {'HL_TESTNET' if TESTNET else 'HL_MAINNET'}_SECRET set"
            )
        base = (constants.TESTNET_API_URL if TESTNET
                else constants.MAINNET_API_URL)
        self.info = Info(base, skip_ws=True)
        self.exchange = Exchange(None, base)  # wired at first use with wallet
        self._secret = secret
        self.base_url = base

    def account_equity(self) -> float:
        # account_value across perp + spot for the configured address
        raise NotImplementedError("wire to user state at Phase 4")

    def market_order(self, symbol: str, side: str, size_usd: float,
                     price: float) -> FillResult:
        # Phase 4: build wallet from _secret, place IOC limit near mid
        raise NotImplementedError("Phase 4 — enable after paper gates pass")


def get_executor(risk_engine):
    return HyperliquidExecutor(risk_engine) if not TESTNET else PaperExecutor(risk_engine)
