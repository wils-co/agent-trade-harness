"""Tests for Telegram notifications and live price check monitoring."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from harness.risk import RiskEngine
from harness.agent import Agent
from harness import notify


class TestTelegramNotification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "harness.db")
        self.pending_path = Path(self.tmp) / "pending.json"

        # Patch agent paths
        self.patch_db = patch("harness.agent.DB_PATH", Path(self.db_path))
        self.patch_pending = patch("harness.agent.PENDING_PATH", self.pending_path)
        self.patch_db.start()
        self.patch_pending.start()

        self.agent = Agent()
        self.agent.risk = RiskEngine(self.db_path)

    def tearDown(self):
        self.patch_db.stop()
        self.patch_pending.stop()

    @patch("harness.notify.send_telegram")
    def test_tp1_triggers_telegram_notification(self, mock_send):
        import re
        out = self.agent.cmd_ticket("long eth $35 entry 2400 sl 2380 tp1 2450 tp2 2500")
        m = re.search(r"confirm (\d+)", out)
        self.agent.cmd_confirm(int(m.group(1)))

        self.agent.cmd_tp1(1)
        self.assertTrue(mock_send.called)
        sent_text = mock_send.call_args[0][0]
        self.assertIn("TP1 HIT", sent_text)
        self.assertIn("ETH", sent_text)
        self.assertIn("Banked 80%", sent_text)
        self.assertIn("Stop moved to Breakeven", sent_text)

    @patch("harness.notify.send_telegram")
    def test_stop_out_triggers_telegram_notification(self, mock_send):
        import re
        out = self.agent.cmd_ticket("long eth $35 entry 2400 sl 2380 tp1 2450 tp2 2500")
        m = re.search(r"confirm (\d+)", out)
        self.agent.cmd_confirm(int(m.group(1)))

        # Stop out at 2375
        self.agent.cmd_close(1, 2375.0)
        self.assertTrue(mock_send.called)
        sent_text = mock_send.call_args[0][0]
        self.assertIn("STOPPED OUT", sent_text)
        self.assertIn("ETH", sent_text)

    @patch("harness.notify.send_telegram")
    def test_runner_tp2_triggers_telegram_notification(self, mock_send):
        import re
        out = self.agent.cmd_ticket("long eth $35 entry 2400 sl 2380 tp1 2450 tp2 2500")
        m = re.search(r"confirm (\d+)", out)
        self.agent.cmd_confirm(int(m.group(1)))

        self.agent.cmd_tp1(1)
        mock_send.reset_mock()

        # Runner hits TP2 @ 2500
        self.agent.cmd_close(1, 2500.0)
        self.assertTrue(mock_send.called)
        sent_text = mock_send.call_args[0][0]
        self.assertIn("TP2 HIT", sent_text)
        self.assertIn("RUNNER", sent_text)

    @patch("harness.notify.send_telegram")
    @patch("harness.agent.fetch_live_price")
    def test_cmd_check_auto_triggers_tp1_and_sl(self, mock_price, mock_send):
        import re
        # Trade 1: Long ETH entry 2400, sl 2380, tp1 2450, tp2 2500
        out1 = self.agent.cmd_ticket("long eth $35 entry 2400 sl 2380 tp1 2450 tp2 2500")
        m1 = re.search(r"confirm (\d+)", out1)
        self.agent.cmd_confirm(int(m1.group(1)))

        # Live price hits 2455 (above TP1 2450)
        mock_price.return_value = 2455.0
        check_out = self.agent.cmd_check()

        self.assertIn("TP1 HIT #1", check_out)
        self.assertTrue(mock_send.called)

        # Trade status is now runner (closed_tp1) with SL at BE 2400
        t1 = self.agent.risk.db.execute("SELECT * FROM trades WHERE id=1").fetchone()
        self.assertEqual(t1["status"], "closed_tp1")
        self.assertEqual(t1["stop_loss"], 2400.0)

        # Next check: live price drops to 2395 (below BE 2400)
        mock_price.return_value = 2395.0
        mock_send.reset_mock()
        check_out2 = self.agent.cmd_check()

        self.assertIn("CLOSED #1 [RUNNER", check_out2)
        self.assertTrue(mock_send.called)

        t1_final = self.agent.risk.db.execute("SELECT * FROM trades WHERE id=1").fetchone()
        self.assertEqual(t1_final["status"], "closed")

    @patch("urllib.request.urlopen")
    def test_send_telegram_direct_api(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true, "result": {"message_id": 999}}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = notify.send_telegram("test alert")
        self.assertTrue(res["ok"])


if __name__ == "__main__":
    unittest.main()
