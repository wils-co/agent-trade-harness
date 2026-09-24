"""Telegram notifier for agent-trade-harness.

Sends alerts via the Hermes `oxalpha` profile's Telegram bot
when trades hit TP1, TP2, or are stopped out.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

logger = logging.getLogger("harness.notify")

OXALPHA_ENV_PATH = Path.home() / ".hermes" / "profiles" / "oxalpha" / ".env"
OXALPHA_DIR_PATH = Path.home() / ".hermes" / "profiles" / "oxalpha" / "channel_directory.json"


def get_telegram_credentials() -> tuple[str, str]:
    """Resolve (bot_token, chat_id).

    Prioritizes environment variables, then falls back to ~/.hermes/profiles/oxalpha/.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token and OXALPHA_ENV_PATH.is_file():
        try:
            for line in OXALPHA_ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip("\"' ")
                    break
        except Exception as e:
            logger.warning("Could not read oxalpha .env: %s", e)

    if not chat_id and OXALPHA_DIR_PATH.is_file():
        try:
            data = json.loads(OXALPHA_DIR_PATH.read_text(encoding="utf-8"))
            tg_list = data.get("platforms", {}).get("telegram", [])
            if tg_list and "id" in tg_list[0]:
                chat_id = str(tg_list[0]["id"])
        except Exception:
            pass

    return token, chat_id


def send_telegram(text: str, parse_mode: str | None = "Markdown") -> dict:
    """Send text to Telegram.

    Primary: Direct Bot API HTTPS POST.
    Fallback: CLI `oxalpha send --to telegram`.
    Never raises exceptions that would disrupt trade execution.
    """
    token, chat_id = get_telegram_credentials()
    if not token or not chat_id:
        # Try CLI fallback
        return _send_via_cli(text)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "result": res}
    except urllib.error.HTTPError as e:
        # If Markdown parse error (HTTP 400), retry plain text
        if e.code == 400 and parse_mode:
            return send_telegram(text, parse_mode=None)
        logger.error("Telegram API HTTPError: %s", e)
        return _send_via_cli(text)
    except Exception as e:
        logger.error("Telegram API send failed: %s", e)
        return _send_via_cli(text)


def _send_via_cli(text: str) -> dict:
    """Fallback to hermes/oxalpha CLI."""
    oxalpha_bin = Path.home() / ".local" / "bin" / "oxalpha"
    cmd = [str(oxalpha_bin), "send", "--to", "telegram", text] if oxalpha_bin.exists() else [
        "hermes", "-p", "oxalpha", "send", "--to", "telegram", text
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        if proc.returncode == 0:
            return {"ok": True, "method": "cli"}
        return {"ok": False, "error": proc.stderr or proc.stdout}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def notify_tp1(r: dict) -> dict:
    """Format and send notification when TP1 is hit."""
    pct_label = int(r.get("ratio", 0.8) * 100)
    runner_pct = 100 - pct_label
    msg = (
        f"🎯 *TP1 HIT* `#{r['id']}` *{r['side'].upper()} {r['symbol']}* @ {r['exit_price']:,.2f}\n"
        f"💰 *Banked {pct_label}%* (${r['tranche_size']:.2f}): gross {r['gross']:+.2f} | "
        f"fees -{r['fees']:.2f} | *net {r['net']:+.2f}*\n"
        f"🏃 *Runner:* {runner_pct}% (${r['runner_size']:.2f})\n"
        f"🛡️ *Stop moved to Breakeven* @ {r['breakeven_sl']:,.2f} (risk-free)"
    )
    return send_telegram(msg)


def notify_close(r: dict) -> dict:
    """Format and send notification when trade or runner is closed / stopped."""
    status = r.get("status", "closed")
    is_runner = r.get("is_runner", False)
    sym = r.get("symbol", "").upper()
    side = r.get("side", "").upper()
    tag = f"*{side} {sym}*" if sym else ""

    if is_runner:
        if status == "stopped" or (r.get("runner_gross", 0) <= 0 and abs(r.get("runner_gross", 0)) < 1.0):
            header = f"🛡️ *RUNNER SCRATCHED (BE)* `#{r['id']}` {tag} @ {r.get('exit_price', 0):,.2f}"
        else:
            header = f"🏁 *TP2 HIT* `#{r['id']}` *[RUNNER]* {tag} @ {r.get('exit_price', 0):,.2f}"
        msg = (
            f"{header}\n"
            f"🏃 *Runner:* gross {r['runner_gross']:+.2f} | net {r['runner_net']:+.2f}\n"
            f"🏆 *Total trade:* gross {r['gross']:+.2f} | fees -{r['fees']:.2f} | "
            f"funding -{r['funding']:.2f} | *net {r['net']:+.2f}*\n"
            f"⏱️ Held {r['hold_hours']:.2f}h"
        )
    else:
        if status == "stopped":
            header = f"🛑 *STOPPED OUT* `#{r['id']}` {tag} @ {r.get('exit_price', 0):,.2f}"
        else:
            header = f"🏁 *TAKE PROFIT / CLOSED* `#{r['id']}` {tag} @ {r.get('exit_price', 0):,.2f}"
        msg = (
            f"{header}\n"
            f"📊 gross {r['gross']:+.2f} | fees -{r['fees']:.2f} | "
            f"funding -{r['funding']:.2f} | *net {r['net']:+.2f}*\n"
            f"⏱️ Held {r['hold_hours']:.2f}h"
        )
    return send_telegram(msg)
