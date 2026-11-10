#!/usr/bin/env python3
"""GitHub Actions stop-level checker.

Does NOT trade and never touches keys. Fetches Jupiter v3 USD marks and,
when possible, a real Jupiter sell quote (token -> CASH). POSTs a wake-up
to the Grok Bot webhook routine only when the executable quote (or mark
fallback) hits the effective stop, or once when profit protection
activates. The routine performs the real sell.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DIR = Path(__file__).resolve().parent
LEVELS_PATH = DIR / "watch-levels.json"
STATE_FLAG_PATH = DIR / ".state-changed"

DEBOUNCE_SEC = 10 * 60
FIRE_BUFFER = 1.01  # mark fallback: fire when value <= effective_stop * buffer
MARK_NEAR_STOP = 1.03
CASH_MINT = "CASHx9KJUStyftLFWGvEVf59SGeG9sh5FfcnZMVPCASH"
CASH_DECIMALS = 6
DEFAULT_TOKEN_DECIMALS = 6
PP_ACTIVATE_MULT = 1.12
PP_TRAIL_MULT = 0.88
PP_LOCK_MULT = 1.05
DEFAULT_STOP_PCT = -15.0
PEAK_COMMIT_PCT = 0.5  # commit state when peak rises more than this percent
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
JUPITER_PRICE_URL = "https://lite-api.jup.ag/price/v3"
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_TOKEN_SEARCH_URL = "https://lite-api.jup.ag/tokens/v2/search"
GECKO_URL = (
    "https://api.geckoterminal.com/api/v2/simple/networks/solana/token_price/"
)

_DECIMALS_CACHE: Dict[str, int] = {CASH_MINT: CASH_DECIMALS}
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

def madrid_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Madrid"))
    except Exception:
        return datetime.now().astimezone()


def madrid_now_iso() -> str:
    return madrid_now().isoformat(timespec="seconds")


def madrid_now_label() -> str:
    return madrid_now().strftime("%Y-%m-%d %H:%M:%S %Z")


def redact(msg: Any) -> str:
    return _URL_RE.sub("<redacted>", str(msg))


def log(msg: str) -> None:
    print(f"{madrid_now_label()} {msg}", flush=True)


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def load_levels() -> Dict[str, Any]:
    if not LEVELS_PATH.exists():
        return {"positions": []}
    return json.loads(LEVELS_PATH.read_text())


def http_get_json(url: str, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_prices_jupiter(mints: List[str]) -> Dict[str, float]:
    if not mints:
        return {}
    qs = urllib.parse.urlencode({"ids": ",".join(mints)})
    data = http_get_json(f"{JUPITER_PRICE_URL}?{qs}")
    out: Dict[str, float] = {}
    if not isinstance(data, dict):
        return out
    for mint, info in data.items():
        if isinstance(info, dict) and info.get("usdPrice") is not None:
            out[mint] = float(info["usdPrice"])
        elif isinstance(info, (int, float, str)):
            try:
                out[mint] = float(info)
            except Exception:
                pass
    return out


def fetch_prices_gecko(mints: List[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    chunk = 30
    for i in range(0, len(mints), chunk):
        batch = mints[i : i + chunk]
        url = GECKO_URL + ",".join(batch)
        data = http_get_json(url)
        prices = (
            data.get("data", {}).get("attributes", {}).get("token_prices", {})
            if isinstance(data, dict)
            else {}
        )
        for mint, px in prices.items():
            try:
                out[mint] = float(px)
            except Exception:
                pass
    return out


def fetch_prices(mints: List[str]) -> Tuple[Dict[str, float], str]:
    try:
        prices = fetch_prices_jupiter(mints)
        missing = [m for m in mints if m not in prices]
        if not missing:
            return prices, "jupiter_v3"
        try:
            gecko = fetch_prices_gecko(missing)
            prices.update(gecko)
            return prices, "jupiter_v3+gecko"
        except Exception as e:
            log(f"WARN gecko fallback failed: {redact(e)}")
            return prices, "jupiter_v3_partial"
    except Exception as e:
        log(f"WARN jupiter failed: {redact(e)}; trying gecko")
        try:
            return fetch_prices_gecko(mints), "gecko"
        except Exception as e2:
            log(f"ERROR all price sources failed: {redact(e2)}")
            return {}, "none"


def resolve_decimals(mint: str, pos: Optional[Dict[str, Any]] = None) -> int:
    if pos is not None and pos.get("decimals") is not None:
        try:
            d = int(pos["decimals"])
            _DECIMALS_CACHE[mint] = d
            return d
        except Exception:
            pass
    if mint in _DECIMALS_CACHE:
        return _DECIMALS_CACHE[mint]
    try:
        qs = urllib.parse.urlencode({"query": mint})
        data = http_get_json(f"{JUPITER_TOKEN_SEARCH_URL}?{qs}", timeout=15.0)
        if isinstance(data, list):
            for tok in data:
                if (
                    isinstance(tok, dict)
                    and tok.get("id") == mint
                    and tok.get("decimals") is not None
                ):
                    d = int(tok["decimals"])
                    _DECIMALS_CACHE[mint] = d
                    return d
            for tok in data:
                if isinstance(tok, dict) and tok.get("decimals") is not None:
                    d = int(tok["decimals"])
                    _DECIMALS_CACHE[mint] = d
                    return d
    except Exception as e:
        log(
            f"WARN decimals lookup failed for {mint[:8]}: {redact(e)}; "
            f"defaulting to {DEFAULT_TOKEN_DECIMALS}"
        )
    _DECIMALS_CACHE[mint] = DEFAULT_TOKEN_DECIMALS
    return DEFAULT_TOKEN_DECIMALS


def amount_to_raw(amount: float, decimals: int) -> int:
    return int(round(float(amount) * (10 ** decimals)))


def fetch_sell_quote_cash(mint: str, amount: float, decimals: int) -> Optional[float]:
    raw = amount_to_raw(amount, decimals)
    if raw <= 0:
        return None
    qs = urllib.parse.urlencode(
        {
            "inputMint": mint,
            "outputMint": CASH_MINT,
            "amount": str(raw),
            "slippageBps": "150",
        }
    )
    url = f"{JUPITER_QUOTE_URL}?{qs}"
    try:
        data = http_get_json(url, timeout=20.0)
    except Exception as e:
        log(f"WARN quote failed mint={mint[:8]}: {redact(e)}")
        return None
    if not isinstance(data, dict) or data.get("outAmount") is None:
        keys = list(data) if isinstance(data, dict) else type(data)
        log(f"WARN quote missing outAmount mint={mint[:8]} payload_keys={keys}")
        return None
    try:
        return int(data["outAmount"]) / (10 ** CASH_DECIMALS)
    except Exception as e:
        log(f"WARN quote parse failed mint={mint[:8]}: {redact(e)}")
        return None


def stop_pct(pos: Dict[str, Any]) -> float:
    if pos.get("stop_pct") is not None:
        return float(pos["stop_pct"])
    return DEFAULT_STOP_PCT


def original_stop(pos: Dict[str, Any]) -> float:
    if pos.get("stop") is not None and pos.get("stop") != "":
        return float(pos["stop"])
    return float(pos["entry"]) * (1.0 + stop_pct(pos) / 100.0)


def protect_floor(pos: Dict[str, Any], peak: float, entry: float) -> float:
    trail = float(pos.get("pp_trail_mult") or PP_TRAIL_MULT)
    lock = float(pos.get("pp_lock_mult") or PP_LOCK_MULT)
    return max(peak * trail, entry * lock)


def effective_stop(pos: Dict[str, Any]) -> float:
    stop = original_stop(pos)
    entry = float(pos["entry"])
    peak = float(pos.get("peak_value") or 0)
    if pos.get("profit_protect_active"):
        return max(stop, protect_floor(pos, peak, entry))
    return stop


def parse_ts(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        try:
            return float(ts)
        except Exception:
            return None


def should_debounce(pos: Dict[str, Any], now: float) -> bool:
    last = parse_ts(pos.get("last_alert_at"))
    if last is None:
        return False
    return (now - last) < DEBOUNCE_SEC


def fire_reason(pos: Dict[str, Any], value: float, eff: float, peak: float, entry: float) -> str:
    stop = original_stop(pos)
    if pos.get("profit_protect_active"):
        floor = protect_floor(pos, peak, entry)
        if floor >= stop and abs(eff - floor) < 1e-9:
            return "profit_protect"
        if value <= stop * FIRE_BUFFER and eff == stop:
            return "stop"
        return "profit_protect" if eff > stop else "stop"
    return "stop"


def post_webhook(payload: Dict[str, Any]) -> str:
    url = os.environ.get("WATCHER_WEBHOOK_URL", "").strip()
    if not url:
        return "would-fire (no webhook configured)"

    body = json.dumps(payload).encode()
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    full_auth = os.environ.get("WATCHER_WEBHOOK_AUTH", "").strip()
    key = os.environ.get("WATCHER_WEBHOOK_KEY", "").strip()
    if full_auth:
        if ":" in full_auth and full_auth.split(":", 1)[0].lower() in (
            "authorization",
            "x-api-key",
            "x-webhook-key",
        ):
            name, val = full_auth.split(":", 1)
            headers[name.strip()] = val.strip()
        else:
            headers["Authorization"] = full_auth
    elif key:
        headers["Authorization"] = f"Bearer {key}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            _ = resp.read(256)
            return f"sent HTTP {code}"
    except urllib.error.HTTPError as e:
        return f"error: HTTP {e.code}"
    except Exception as e:
        return f"error: {redact(e)}"


def maybe_activate_profit_protect(
    pos: Dict[str, Any], value: float, peak: float, entry: float, symbol: str
) -> bool:
    changed = False
    activate_at = float(pos.get("activate_at") or (entry * PP_ACTIVATE_MULT))
    if (not pos.get("profit_protect_active")) and value >= activate_at:
        pos["profit_protect_active"] = True
        changed = True
        log(f"{symbol} profit_protect ACTIVATED value={value:.4f} activate_at={activate_at:.4f}")
    if (not pos.get("profit_protect_active")) and peak >= activate_at:
        pos["profit_protect_active"] = True
        changed = True
        log(f"{symbol} profit_protect ACTIVATED via peak={peak:.4f} activate_at={activate_at:.4f}")
    return changed


def snapshot_commit_fields(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = {}
    for pos in positions:
        mint = pos.get("mint")
        if not mint:
            continue
        out[mint] = {
            "peak_value": float(pos.get("peak_value") or 0),
            "profit_protect_active": bool(pos.get("profit_protect_active")),
            "last_alert_at": pos.get("last_alert_at"),
        }
    return out


def meaningful_change(before: Dict[str, Any], after_positions: List[Dict[str, Any]]) -> bool:
    after = snapshot_commit_fields(after_positions)
    if set(before) != set(after):
        return True
    for mint, b in before.items():
        a = after[mint]
        if bool(a["profit_protect_active"]) != bool(b["profit_protect_active"]):
            return True
        if a.get("last_alert_at") != b.get("last_alert_at"):
            return True
        old_peak = float(b.get("peak_value") or 0)
        new_peak = float(a.get("peak_value") or 0)
        if new_peak > old_peak:
            if old_peak <= 0:
                return True
            pct = (new_peak - old_peak) / old_peak * 100.0
            if pct > PEAK_COMMIT_PCT:
                return True
    return False


def process_once() -> int:
    levels = load_levels()
    positions: List[Dict[str, Any]] = list(levels.get("positions") or [])
    STATE_FLAG_PATH.write_text("no\n")
    if not positions:
        log("no positions in watch-levels.json")
        return 0

    before = snapshot_commit_fields(positions)
    mints = [p["mint"] for p in positions if p.get("mint")]
    prices, source = fetch_prices(mints)
    if not prices:
        log(f"ERROR no prices this run (source={source})")
        return 1

    now = time.time()
    now_iso = madrid_now_iso()
    quotes_fetched = 0
    fires = 0

    for pos in positions:
        mint = pos.get("mint")
        symbol = pos.get("symbol", "?")
        if not mint or mint not in prices:
            log(f"{symbol}: NO_PRICE")
            continue
        amount = float(pos["amount"])
        entry = float(pos["entry"])
        px = float(prices[mint])
        mark_value = px * amount

        peak = float(pos.get("peak_value") or mark_value)
        if mark_value > peak:
            peak = mark_value
            pos["peak_value"] = round(peak, 6)

        activated = maybe_activate_profit_protect(pos, mark_value, peak, entry, symbol)

        decimals = resolve_decimals(mint, pos)
        if pos.get("decimals") != decimals:
            pos["decimals"] = decimals

        quote_cash = fetch_sell_quote_cash(mint, amount, decimals)
        quotes_fetched += 1
        if quote_cash is not None:
            decision_source = "quote"
            value_for_decision = quote_cash
            pos["last_quote_cash"] = round(quote_cash, 6)
            if quote_cash > peak:
                peak = quote_cash
                pos["peak_value"] = round(peak, 6)
            if maybe_activate_profit_protect(pos, quote_cash, peak, entry, symbol):
                activated = True
            eff = effective_stop(pos)
            fire = quote_cash <= eff
            fire_line = eff
        else:
            decision_source = "mark_fallback"
            value_for_decision = mark_value
            eff = effective_stop(pos)
            fire_line = eff * FIRE_BUFFER
            fire = mark_value <= fire_line
            log(
                f"{symbol} quote unavailable; mark_fallback val={mark_value:.4f} "
                f"fire_line={fire_line:.4f}"
            )

        pos["peak_value"] = round(peak, 6)
        pos["last_value"] = round(mark_value, 6)
        pos["last_price_usd"] = px
        pos["last_effective_stop"] = round(eff, 6)
        pos["last_check_at"] = now_iso
        pos["last_decision_source"] = decision_source

        pp = "PP" if pos.get("profit_protect_active") else "—"
        qpart = f" quote={quote_cash:.4f}" if quote_cash is not None else ""
        log(
            f"{symbol}: mark={mark_value:.4f}{qpart} eff_stop={eff:.4f} "
            f"fire≤{fire_line:.4f} peak={peak:.4f} {pp} src={decision_source} "
            f"px={px:.8f} entry={entry:.4f}"
        )

        def send(reason: str, value: float, src: str, q: Optional[float]) -> None:
            nonlocal fires
            if should_debounce(pos, now):
                log(
                    f"{symbol} would fire ({reason}/{src}) but debounced "
                    f"last_alert_at={pos.get('last_alert_at')} val={value:.4f} eff={eff:.4f}"
                )
                return
            payload = {
                "symbol": symbol,
                "mint": mint,
                "reason": reason,
                "est_value": round(value, 6),
                "mark_value": round(mark_value, 6),
                "quote_cash": round(q, 6) if q is not None else None,
                "decision_source": src,
                "effective_stop": round(eff, 6),
                "entry": entry,
                "amount": amount,
                "peak_value": round(peak, 6),
                "profit_protect_active": bool(pos.get("profit_protect_active")),
                "price_usd": px,
                "price_source": source,
                "ts": now_iso,
                "wallet": levels.get("wallet"),
            }
            status = post_webhook(payload)
            log(
                f"FIRE {symbol} reason={reason} src={src} val={value:.4f} "
                f"mark={mark_value:.4f} eff_stop={eff:.4f} -> {status}"
            )
            pos["last_alert_at"] = now_iso
            pos["last_alert_status"] = status
            fires += 1

        # If both activate and stop trip in one check, send the stop (more urgent).
        if fire:
            send(fire_reason(pos, value_for_decision, eff, peak, entry), value_for_decision, decision_source, quote_cash)
        elif activated:
            send("profit_protect_activated", value_for_decision, decision_source, quote_cash)

    levels["positions"] = positions
    levels["updated_at"] = now_iso
    levels["last_price_source"] = source

    commit = meaningful_change(before, positions)
    if commit:
        atomic_write_json(LEVELS_PATH, levels)
        STATE_FLAG_PATH.write_text("yes\n")
        log("STATE_CHANGED=yes (peak>+0.5% or protection or alert)")
    else:
        STATE_FLAG_PATH.write_text("no\n")
        log("STATE_CHANGED=no (skip commit)")

    log(
        f"done price_source={source} n={len(positions)} quotes={quotes_fetched} "
        f"fires={fires}"
    )
    return 0


def main() -> int:
    webhook = bool(os.environ.get("WATCHER_WEBHOOK_URL", "").strip())
    log(f"check start webhook_configured={webhook} levels={LEVELS_PATH}")
    try:
        return process_once()
    except Exception as e:
        log(f"ERROR unhandled: {redact(e)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
