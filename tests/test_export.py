"""Unit tests for harness.export (unitized public ledger & SVG generator)."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from harness.export import compute_metrics, generate_svg, generate_trades_md, update_readme


class TestExportPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.svg_path = self.tmp / "equity_curve.svg"
        self.trades_md_path = self.tmp / "TRADES.md"
        self.readme_path = self.tmp / "README.md"
        self.readme_path.write_text("# Test Repo\n\nSome text here.\n", encoding="utf-8")

        self.mock_trades = [
            {
                "id": 1,
                "ts": 1789687549.0,
                "symbol": "ETH",
                "side": "long",
                "entry_price": 2400.0,
                "stop_loss": 2363.0,
                "tp1": 2460.0,
                "tp2": 2522.0,
                "status": "closed",
                "exit_price": 2522.0,
                "net_pnl_usd": 92.67,
                "gross_pnl_usd": 97.84,
                "fees_usd": 3.57,
                "funding_usd": 1.60,
                "hold_seconds": 51235.0,
                "setup_tag": None,
                "thesis": "long eth entry 2400 sl 2363",
                "excluded_from_report": 0,
            },
            {
                "id": 2,
                "ts": 1789687597.0,
                "symbol": "ETH",
                "side": "short",
                "entry_price": 2448.25,
                "stop_loss": 2480.0,
                "tp1": 2420.0,
                "tp2": 2380.0,
                "status": "closed",
                "exit_price": 2480.0,
                "net_pnl_usd": -55.72,
                "gross_pnl_usd": -50.0,
                "fees_usd": 4.24,
                "funding_usd": 1.48,
                "hold_seconds": 13844.0,
                "setup_tag": None,
                "thesis": "short eth entry 2448.25 sl 2480",
                "excluded_from_report": 0,
            },
            {
                "id": 3,
                "ts": 1790207844.0,
                "symbol": "ETH",
                "side": "short",
                "entry_price": 2684.50,
                "stop_loss": 2719.50,
                "tp1": 2616.0,
                "tp2": 2563.0,
                "status": "open",
                "exit_price": None,
                "net_pnl_usd": None,
                "gross_pnl_usd": None,
                "fees_usd": None,
                "funding_usd": None,
                "hold_seconds": None,
                "setup_tag": "fade-contd",
                "thesis": "short eth entry 2684.50 sl 2719.50",
                "excluded_from_report": 0,
            },
        ]

    def test_compute_metrics(self):
        m = compute_metrics(self.mock_trades)
        self.assertEqual(m["n"], 2)
        self.assertAlmostEqual(m["total_net_r"], 0.74, places=2)
        self.assertEqual(len(m["open_trades"]), 1)
        self.assertEqual(m["processed"][0]["trigger"], "TP2")
        self.assertEqual(m["processed"][1]["trigger"], "SL")
        self.assertAlmostEqual(m["processed"][0]["r_multiple"], 1.85, places=2)
        self.assertAlmostEqual(m["processed"][1]["r_multiple"], -1.11, places=2)

    def test_generate_svg(self):
        m = compute_metrics(self.mock_trades)
        generate_svg(m, output_path=self.svg_path)
        self.assertTrue(self.svg_path.exists())
        svg_content = self.svg_path.read_text(encoding="utf-8")
        self.assertIn("<svg", svg_content)
        self.assertIn("Cumulative Performance", svg_content)
        self.assertIn("Underwater Profile", svg_content)

    def test_generate_trades_md(self):
        m = compute_metrics(self.mock_trades)
        generate_trades_md(m, output_path=self.trades_md_path)
        self.assertTrue(self.trades_md_path.exists())
        text = self.trades_md_path.read_text(encoding="utf-8")
        self.assertIn("Verified Trade Ledger (Unitized R)", text)
        self.assertIn("#1 ▲ ETH · TP2 · R:R 1.85", text)
        self.assertIn("#2 ▼ ETH · SL · R:R -1.11", text)
        self.assertIn("assets/equity_curve.svg", text)
        self.assertNotIn("$", text)  # Zero dollar amounts in public output!

    def test_update_readme(self):
        m = compute_metrics(self.mock_trades)
        update_readme(m, readme_path=self.readme_path)
        text = self.readme_path.read_text(encoding="utf-8")
        self.assertIn("<!-- SCOREBOARD_START -->", text)
        self.assertIn("## Verified Performance (Unitized R)", text)
        self.assertIn("assets/equity_curve.svg", text)
        self.assertNotIn("$", text)  # Zero dollar amounts in public output!
