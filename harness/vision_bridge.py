"""Vision bridge — screenshot in, trade ticket out.

Flow: chart image -> vlm CLI (local qwen3-vl) -> levels JSON ->
      draft ticket (direction + SL/TP from zones) -> confirmation text.

Wilson provides ONLY the screenshot. The agent proposes the full ticket,
he confirms with one word.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

VLM_BIN = str(Path.home() / "Dev" / "bin" / "vlm")
OLLAMA_URL = "http://localhost:11434/api/generate"
VLM_MODEL = "qwen3-vl:30b"


def _vlm_direct(image_path: str, prompt: str) -> str:
    """Direct Ollama call with think-budget sized for qwen3-vl."""
    import base64
    b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    payload = {
        "model": VLM_MODEL,
        "prompt": prompt,
        "images": [b64],
        "stream": False,
        "keep_alive": "30m",
        # qwen3-vl burns hidden thinking tokens first; budget must cover both
        "options": {"num_predict": 2500, "temperature": 0.1},
    }
    req = Path("/tmp") / f"vl_direct_req_{int(time.time())}.json"
    req.write_text(json.dumps(payload))
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "280", OLLAMA_URL, "-d", f"@{req}"],
            capture_output=True, text=True, timeout=300,
        )
        d = json.loads(r.stdout or "{}")
        return (d.get("response") or "").strip()
    finally:
        req.unlink(missing_ok=True)


CHART_PROMPT = """You are reading a TradingView chart screenshot to draft a trade ticket.
Extract precisely:
1. ticker/symbol and timeframe
2. current price (exact, from price axis)
3. every horizontal line/level with its exact price value
4. shaded zones: color + exact top/bottom prices if labeled
5. fibonacci retracement: the 0 and 1 anchor prices and key levels (0.618 etc)

Return ONLY JSON:
{"symbol": "", "timeframe": "", "current_price": null,
 "levels": [{"price": 0, "note": ""}],
 "zones": [{"color": "", "role": "support|resistance|entry", "top": null, "bottom": null}],
 "fib": {"0": null, "1": null, "0.618": null},
 "confidence": "high|medium|low",
 "unreadable": false, "notes": ""}
Never guess a price. Set unreadable=true if labels are blurry or overlapping."""


def read_chart(image_path: str, upscale: bool = True) -> dict:
    """Run the local VLM on a chart image, return parsed JSON."""
    p = Path(image_path)
    if not p.exists():
        raise FileNotFoundError(image_path)

    target = str(p)
    tmp = None
    if upscale:
        # TG thumbnails arrive tiny; upscale for reliable OCR of axis labels
        code = (
            "from PIL import Image; import sys\n"
            "img = Image.open(sys.argv[1]); w,h = img.size\n"
            "f = max(2, min(4, int(1200/max(w,h)) or 2))\n"
            "img.convert('RGB').resize((w*f,h*f), Image.LANCZOS)"
            ".save(sys.argv[2])\n"
        )
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        r = subprocess.run(
            ["python3", "-c", code, str(p), tmp.name],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            target = tmp.name

    last_err = None
    for attempt in range(3):
        try:
            out = _vlm_direct(target, CHART_PROMPT)
            # strip markdown fences if the model adds them
            out = out.removeprefix("```json").removeprefix("```")
            out = out.removesuffix("```").strip()
            start, end = out.find("{"), out.rfind("}")
            if start == -1:
                raise RuntimeError(f"no JSON in vlm output: {out[:200]}")
            return json.loads(out[start:end+1])
        except (RuntimeError, json.JSONDecodeError) as e:
            last_err = e
            continue
    raise RuntimeError(f"vlm failed after 3 attempts: {last_err}")


def _clean(v):
    """VLMs return numbers as strings like '78,771.63' — normalize."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def draft_ticket(chart: dict, risk_cap_usd: float, direction_hint: str | None = None) -> dict:
    """Turn extracted levels into a concrete ticket proposal."""
    if chart.get("unreadable"):
        raise ValueError(f"chart unreadable: {chart.get('notes', 'no detail')}")

    current = _clean(chart.get("current_price"))
    if not current:
        raise ValueError("could not read current price from chart")

    levels = sorted(
        [(_clean(l.get("price")), (l.get("note") or "").lower())
         for l in chart.get("levels", []) if _clean(l.get("price"))],
        key=lambda x: x[0],
    )

    # classify levels relative to current price
    above = [(p, n) for p, n in levels if p > current * 1.001]
    below = [(p, n) for p, n in levels if p < current * 0.999]

    def pick(cands, prefer_notes, fallback_index):
        # prefer levels whose note matches keywords, else nearest/farthest by index
        for kw in prefer_notes:
            for p, n in cands:
                if kw in n:
                    return p, n
        if not cands:
            return None, ""
        idx = fallback_index(len(cands))
        return cands[idx]

    fib = {k: _clean(v) for k, v in (chart.get("fib") or {}).items()}

    direction = direction_hint
    if not direction:
        # crude default: resistance zone overhead + support below => range play off support
        res_zone = next((z for z in chart.get("zones", [])
                         if "resist" in (z.get("role") or "").lower()
                         or "short" in (z.get("role") or "").lower()), None)
        sup_zone = next((z for z in chart.get("zones", [])
                         if "support" in (z.get("role") or "").lower()
                         or "long" in (z.get("role") or "").lower()), None)
        direction = "long" if sup_zone else ("short" if res_zone else None)

    if direction == "long":
        sl_p, sl_n = pick(below, ["sl", "stop"], lambda n: 0)          # nearest below
        tp2_p, tp2_n = pick(above, ["tp2", "target"], lambda n: n - 1) # farthest above
        tp1_p, tp1_n = pick([x for x in above if x[0] != tp2_p],
                            ["tp1"], lambda n: n - 1)
        entry = current
    elif direction == "short":
        sl_p, _ = pick(above, ["sl", "stop"], lambda n: n - 1)
        tp2_p, _ = pick(below, ["tp2", "target"], lambda n: 0)
        tp1_p, _ = pick([x for x in below if x[0] != tp2_p], ["tp1"],
                        lambda n: min(1, n - 1))
        entry = current
    else:
        raise ValueError(
            "could not infer direction — say 'long' or 'short' alongside the image"
        )

    if not sl_p:
        raise ValueError("no stop-loss level found below/above price on chart")

    dist_pct = abs(entry - sl_p) / entry * 100
    size = round(risk_cap_usd * 100 / dist_pct, 2)

    return {
        "side": direction,
        "symbol": (chart.get("symbol") or "BTC").upper().split("/")[0].split("-")[0],
        "entry": entry,
        "sl": sl_p,
        "tp1": tp1_p,
        "tp2": tp2_p,
        "size_usd": size,
        "leverage": 1,
        "fib_anchors": {"0": fib.get("0"), "1": fib.get("1")},
        "chart_confidence": chart.get("confidence"),
    }


def format_proposal(t: dict, risk_cap: float) -> str:
    risk = abs(t["entry"] - t["sl"]) / t["entry"] * t["size_usd"]
    lines = [
        f"**Proposed trade** (from your chart):",
        f"{t['side'].upper()} {t['symbol']} | 1x | ${t['size_usd']:.0f}",
        f"entry ~{t['entry']:,.0f} | sl {t['sl']:,.0f}"
        + (f" | tp1 {t['tp1']:,.0f}" if t['tp1'] else "")
        + (f" | tp2 {t['tp2']:,.0f}" if t['tp2'] else ""),
        f"risk ${risk:.2f} / ${risk_cap:.0f} cap"
        f" | chart confidence: {t.get('chart_confidence', '?')}",
        "",
        "Reply **go** to confirm, or correct any number first.",
    ]
    return "\n".join(lines)
