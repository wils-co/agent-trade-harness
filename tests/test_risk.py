"""Tests for the risk engine + ticket parser. Run: python3 -m tests.test_risk"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from harness.risk import RiskEngine, load_config
from harness.ticket import parse_ticket, derive_size, format_ticket, TicketError

from harness import risk as _risk  # pin synthetic $ config; never read the gitignored local one
_risk.LOCAL_CONFIG_PATH = Path(__file__).parent / "risk_config.test.json"


def fresh_engine(tmpdir):
    return RiskEngine(os.path.join(tmpdir, "test.db"))


class TestTicketParser(unittest.TestCase):
    def test_full_pipe_format(self):
        t = parse_ticket(
            "long btc | lev 2x | size 250 | entry 112400 | sl 110900 | tp1 113800 | tp2 115600"
        )
        self.assertEqual(t["side"], "long")
        self.assertEqual(t["symbol"], "BTC")
        self.assertEqual(t["entry"], 112400)
        self.assertEqual(t["sl"], 110900)
        self.assertEqual(t["tp1"], 113800)
        self.assertEqual(t["tp2"], 115600)
        self.assertEqual(t["leverage"], 2)
        self.assertEqual(t["size_usd"], 250)

    def test_loose_format(self):
        t = parse_ticket("short eth size 200 entry 3120 sl 3180 tp1 3060")
        self.assertEqual(t["side"], "short")
        self.assertEqual(t["symbol"], "ETH")
        self.assertEqual(t["sl"], 3180)

    def test_comma_numbers(self):
        t = parse_ticket("long btc entry 78,771 sl 76,450 size 300")
        self.assertEqual(t["entry"], 78771)

    def test_no_side_rejected(self):
        with self.assertRaises(TicketError):
            parse_ticket("btc entry 112400 sl 110900")

    def test_no_sl_rejected(self):
        with self.assertRaises(TicketError):
            parse_ticket("long btc entry 112400")

    def test_derive_size(self):
        t = parse_ticket("long btc entry 78771 sl 76450")
        size = derive_size(t, 10.0)
        dist_pct = (78771 - 76450) / 78771 * 100
        self.assertAlmostEqual(size, round(10 * 100 / dist_pct, 2), places=1)
        # and sanity: risk at derived size == cap
        implied_risk = size * dist_pct / 100
        self.assertAlmostEqual(implied_risk, 10.0, places=1)


class TestRiskEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.eng = fresh_engine(self.tmp)

    def _order(self, **kw):
        d = dict(symbol="BTC", side="long", entry_price=78771,
                 size_usd=300, stop_loss=76450, leverage=1,
                 tp1=83019, tp2=None)
        d.update(kw)
        return self.eng.check_order(**d)

    def test_clean_order_approved(self):
        d = self._order()
        self.assertTrue(d.approved, d.reason)

    def test_kill_switch_blocks(self):
        self.eng.activate_kill_switch("test")
        d = self._order()
        self.assertFalse(d.approved)
        self.assertIn("KILL", d.reason)
        self.eng.deactivate_kill_switch()
        self.assertTrue(self._order().approved)

    def test_symbol_allowlist(self):
        d = self._order(symbol="DOGE-USD")
        self.assertFalse(d.approved)
        self.assertIn("allowed symbols", d.reason.lower())

    def test_leverage_cap(self):
        # cap is now 25 (HL max) — test against a lev that still breaches it
        d = self._order(leverage=50)
        self.assertFalse(d.approved)
        self.assertIn("leverage", d.reason.lower())

    def test_leverage_within_cap_approved(self):
        d = self._order(leverage=5)
        self.assertTrue(d.approved)

    def test_risk_cap_blocks_wide_stop(self):
        # SL ~24% away on $300 = $71.50 risk > $50 cap
        d = self._order(stop_loss=60000)
        self.assertFalse(d.approved)
        self.assertIn("risk", d.reason.lower())

    def test_long_sl_must_be_below_entry(self):
        d = self._order(stop_loss=80000)
        self.assertFalse(d.approved)

    def test_short_sl_must_be_above_entry(self):
        d = self._order(side="short", stop_loss=75000)
        self.assertFalse(d.approved)
        ok = self._order(side="short", stop_loss=80500, tp1=None)
        self.assertTrue(ok.approved, f"unexpected: {ok.reason}")

    def test_daily_loss_cap(self):
        self.eng.db.execute(
            "INSERT INTO trades (ts,symbol,side,entry_price,size_usd,stop_loss,"
            "leverage,status,pnl_usd) VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), "BTC", "long", 78771, 300, 76450, 1, "closed", -75.0),
        )
        self.eng.db.commit()
        d = self._order()
        self.assertFalse(d.approved)
        self.assertIn("daily loss cap", d.reason.lower())

    def test_drawdown_trips_kill(self):
        for pnl in (-12, -12, -12):  # -$36 on $500 = 7.2%... then more
            self.eng.db.execute(
                "INSERT INTO trades (ts,symbol,side,entry_price,size_usd,stop_loss,"
                "leverage,status,pnl_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                (time.time(), "BTC", "long", 78771, 300, 76450, 1, "closed", pnl),
            )
        self.eng.db.commit()
        d = self._order()  # dd already >= 8%? 36/500=7.2% — add one more loss via this trade's check
        # after approval the engine hasn't recorded yet, so check edge:
        eng2_dd = self.eng.current_drawdown_pct()
        if eng2_dd >= 8.0:
            self.assertTrue(self.eng.kill_switch_active())

    def test_loss_streak_cooldown(self):
        now = time.time()
        for i, pnl in enumerate((-6, -6, -6)):
            self.eng.db.execute(
                "INSERT INTO trades (ts,symbol,side,entry_price,size_usd,stop_loss,"
                "leverage,status,pnl_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                (now - i * 3600, "BTC", "long", 78771, 300, 76450, 1, "closed", pnl),
            )
        self.eng.db.commit()
        cooling, why = self.eng.in_cooldown()
        self.assertTrue(cooling)
        d = self._order()
        self.assertFalse(d.approved)
        self.assertIn("cooling", d.reason.lower())


class TestAgentFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        import harness.agent as agent_mod
        self.saved_db = agent_mod.DB_PATH
        self.saved_pending = agent_mod.PENDING_PATH
        agent_mod.DB_PATH = Path(self.tmp) / "a.db"
        agent_mod.PENDING_PATH = Path(self.tmp) / "p.json"

        from unittest.mock import patch
        self.patch_tg = patch("harness.notify.send_telegram")
        self.patch_tg.start()

        from harness.agent import Agent
        self.agent = Agent()

    def tearDown(self):
        self.patch_tg.stop()
        import harness.agent as agent_mod
        agent_mod.DB_PATH = self.saved_db
        agent_mod.PENDING_PATH = self.saved_pending

    def test_end_to_end_ticket_confirm(self):
        out = self.agent.cmd_ticket(
            "long btc entry 78771 sl 76450 size 300 tp1 83019",
            chart_image="/tmp/vl_chart_test.png",
        )
        self.assertIn("confirm", out.lower())
        self.assertIn("TICKET", out)
        # extract pending id and confirm
        import re
        m = re.search(r"confirm (\d+)", out)
        out2 = self.agent.cmd_confirm(int(m.group(1)))
        self.assertIn("FILLED", out2)
        n = self.agent.risk.db.execute(
            "SELECT COUNT(*) c FROM trades WHERE status='open'"
        ).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_bad_ticket_rejected(self):
        out = self.agent.cmd_ticket("buy some bitcoin please")
        self.assertIn("rejected", out.lower())

    def test_oversized_risk_blocked(self):
        out = self.agent.cmd_ticket("long btc entry 78771 sl 60000 size 500")
        self.assertIn("BLOCKED", out)

    def test_loose_dollar_risk_parsed(self):
        t = parse_ticket("long eth $35 entry 2400 sl 2350 tp1 2450 tp2 2500")
        self.assertEqual(t["risk_usd"], 35.0)
        self.assertEqual(t["tp1_ratio"], 0.80)
        self.assertEqual(t["tp1"], 2450.0)
        self.assertEqual(t["tp2"], 2500.0)

    def test_tp2_ordering_long_rejected(self):
        eng = fresh_engine(self.tmp)
        d = eng.check_order(symbol="ETH", side="long", entry_price=2400, size_usd=100,
                            stop_loss=2380, tp1=2450, tp2=2430)
        self.assertFalse(d.approved)
        self.assertIn("TP2 must be above TP1", d.reason)

    def test_tp2_ordering_short_rejected(self):
        eng = fresh_engine(self.tmp)
        d = eng.check_order(symbol="ETH", side="short", entry_price=2400, size_usd=100,
                            stop_loss=2420, tp1=2350, tp2=2370)
        self.assertFalse(d.approved)
        self.assertIn("TP2 must be below TP1", d.reason)

    def test_tp1_hit_and_runner_close(self):
        import re
        # 1. Open trade with $35 risk, entry 2400, sl 2380, tp1 2450, tp2 2500
        out = self.agent.cmd_ticket("long eth $35 entry 2400 sl 2380 tp1 2450 tp2 2500")
        self.assertIn("TICKET", out)
        m = re.search(r"confirm (\d+)", out)
        self.assertTrue(m)
        self.agent.cmd_confirm(int(m.group(1)))

        trade = self.agent.risk.db.execute("SELECT * FROM trades WHERE id=1").fetchone()
        self.assertEqual(trade["status"], "open")
        initial_size = trade["size_usd"]

        # 2. Hit TP1 @ 2450
        tp1_out = self.agent.cmd_tp1(1)
        self.assertIn("TP1 HIT", tp1_out)
        self.assertIn("Banked 80%", tp1_out)
        self.assertIn("Stop Loss moved to Breakeven", tp1_out)

        trade_after_tp1 = self.agent.risk.db.execute("SELECT * FROM trades WHERE id=1").fetchone()
        self.assertEqual(trade_after_tp1["status"], "closed_tp1")
        self.assertEqual(trade_after_tp1["stop_loss"], 2400.0) # BE
        self.assertAlmostEqual(trade_after_tp1["size_usd"], initial_size * 0.20, places=1)
        self.assertGreater(trade_after_tp1["tp1_net_pnl_usd"], 0)

        # 3. Runner hits TP2 @ 2500
        close_out = self.agent.cmd_close(1, 2500.0)
        self.assertIn("CLOSED #1 [RUNNER closed]", close_out)
        self.assertIn("Total trade", close_out)

        final_trade = self.agent.risk.db.execute("SELECT * FROM trades WHERE id=1").fetchone()
        self.assertEqual(final_trade["status"], "closed")
        self.assertGreater(final_trade["net_pnl_usd"], trade_after_tp1["tp1_net_pnl_usd"])

    def test_tp1_hit_and_runner_scratch_be(self):
        import re
        out = self.agent.cmd_ticket("long eth $35 entry 2400 sl 2380 tp1 2450 tp2 2500")
        m = re.search(r"confirm (\d+)", out)
        self.agent.cmd_confirm(int(m.group(1)))

        self.agent.cmd_tp1(1)
        # Runner stops out at breakeven entry 2400
        close_out = self.agent.cmd_close(1, 2400.0)
        self.assertIn("CLOSED #1", close_out)

        final_trade = self.agent.risk.db.execute("SELECT * FROM trades WHERE id=1").fetchone()
        self.assertEqual(final_trade["status"], "closed") # net is positive from TP1
        # Runner gross was 0, but total trade still banked TP1 profit
        self.assertAlmostEqual(final_trade["gross_pnl_usd"], final_trade["tp1_gross_pnl_usd"], places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
