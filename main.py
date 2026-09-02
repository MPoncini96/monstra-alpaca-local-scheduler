#!/usr/bin/env python3
"""
Monstra -> Alpaca portfolio rebalancer (local scheduler edition).

What this does:
  1. Pulls your target holdings (symbol -> weight) from your Monstra portfolio
     via GET /api/v1/portfolio/latest.
  2. Reads your current Alpaca account positions and equity via the Alpaca
     Trading API.
  3. Computes the buy/sell orders needed to move your Alpaca account toward
     the Monstra target weights.
  4. Logs the plan and -- only when DRY_RUN=false -- submits market orders.

This runs once and exits. Run it on a schedule using Windows Task Scheduler,
macOS launchd, or cron -- see README.md. If you'd rather not manage your own
scheduler, see the sibling package `monstra-alpaca-render-worker`, which runs
the same logic continuously as an always-on Render Background Worker.

Setup:
  1. Get a Monstra API key at https://www.monstra.bot/dashboard/api and set
     MONSTRA_API_KEY.
  2. Get Alpaca API keys from https://app.alpaca.markets (paper and/or live)
     and set ALPACA_PAPER_API_KEY/ALPACA_PAPER_API_SECRET and/or
     ALPACA_LIVE_API_KEY/ALPACA_LIVE_API_SECRET.
  3. Set ALPACA_MODE to "paper" (default) or "live" to choose which key pair
     and endpoint this run uses.
  4. Run: python main.py

Safety:
  - ALPACA_MODE defaults to "paper" -- it will not touch a live account
    unless you explicitly set ALPACA_MODE=live.
  - DRY_RUN defaults to true. No orders are ever sent until you set it to
    false. Because this is meant to run unattended on a schedule, there is
    no interactive EXECUTE step -- setting DRY_RUN=false is itself the
    confirmation. Read that twice before you do it on a live account.
  - Orders are whole-share MARKET/DAY orders only.
  - Sells are capped at your current share count -- this script never opens
    a short position.
  - Trades below MIN_TRADE_DOLLARS are skipped to avoid dust orders.

This script places real trades with real money once ALPACA_MODE=live and
DRY_RUN=false. Review the README and the logged plan carefully.
"""

import json
import logging
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ------------------------------------------------------------------
# 1. CONFIG
# ------------------------------------------------------------------

MONSTRA_API_KEY = os.environ.get("MONSTRA_API_KEY", "PASTE_YOUR_MONSTRA_API_KEY_HERE")
MONSTRA_API_BASE_URL = os.environ.get("MONSTRA_API_BASE_URL", "https://www.monstra.bot")

ALPACA_MODE = os.environ.get("ALPACA_MODE", "paper").strip().lower()  # "paper" or "live"
if ALPACA_MODE == "live":
    ALPACA_API_KEY = os.environ.get("ALPACA_LIVE_API_KEY", "PASTE_YOUR_ALPACA_LIVE_API_KEY_HERE")
    ALPACA_API_SECRET = os.environ.get("ALPACA_LIVE_API_SECRET", "PASTE_YOUR_ALPACA_LIVE_API_SECRET_HERE")
    ALPACA_TRADING_BASE = "https://api.alpaca.markets"
else:
    ALPACA_MODE = "paper"
    ALPACA_API_KEY = os.environ.get("ALPACA_PAPER_API_KEY", "PASTE_YOUR_ALPACA_PAPER_API_KEY_HERE")
    ALPACA_API_SECRET = os.environ.get("ALPACA_PAPER_API_SECRET", "PASTE_YOUR_ALPACA_PAPER_API_SECRET_HERE")
    ALPACA_TRADING_BASE = "https://paper-api.alpaca.markets"

ALPACA_DATA_BASE = "https://data.alpaca.markets"

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() not in ("false", "0", "no")
MIN_TRADE_DOLLARS = float(os.environ.get("MIN_TRADE_DOLLARS", "25"))
LOG_FILE_PATH = Path(os.environ.get("ALPACA_LOG_FILE_PATH", str(Path(__file__).with_name("alpaca_rebalance.log"))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("alpaca_rebalance")

# ------------------------------------------------------------------
# 2. HTTP helpers (stdlib only -- nothing to install)
# ------------------------------------------------------------------


def _monstra_get(path):
    req = urllib.request.Request(
        f"{MONSTRA_API_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {MONSTRA_API_KEY}"},
    )
    return _send(req)


def _alpaca(method, url, data=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("APCA-API-KEY-ID", ALPACA_API_KEY)
    req.add_header("APCA-API-SECRET-KEY", ALPACA_API_SECRET)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    return _send(req)


def _send(req):
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"error": raw}
        return err.code, payload


# ------------------------------------------------------------------
# 3. Monstra: fetch target holdings
# ------------------------------------------------------------------


def fetch_monstra_holdings():
    if "PASTE_YOUR" in MONSTRA_API_KEY:
        sys.exit("Set MONSTRA_API_KEY (env var or the constant at the top) first.")

    status, payload = _monstra_get("/api/v1/portfolio/latest")
    if status != 200:
        sys.exit(f"Monstra portfolio fetch failed ({status}): {payload}")

    holdings = (payload.get("data") or {}).get("holdings") or {}
    if not holdings:
        sys.exit("Monstra returned no holdings for this account -- nothing to rebalance.")
    return holdings  # { "AAPL": 0.25, "MSFT": 0.10, ... } fractions summing to ~1


# ------------------------------------------------------------------
# 4. Alpaca: account, positions, prices, orders
# ------------------------------------------------------------------


def get_account_snapshot():
    status, payload = _alpaca("GET", f"{ALPACA_TRADING_BASE}/v2/account")
    if status != 200:
        sys.exit(f"Could not load Alpaca account ({status}): {payload}")
    total_value = float(payload["equity"])

    status, rows = _alpaca("GET", f"{ALPACA_TRADING_BASE}/v2/positions")
    if status != 200:
        sys.exit(f"Could not load Alpaca positions ({status}): {rows}")

    positions = {row["symbol"]: float(row["qty"]) for row in rows}
    return total_value, positions


def get_last_prices(symbols):
    if not symbols:
        return {}
    params = urllib.parse.urlencode({"symbols": ",".join(sorted(symbols))})
    status, payload = _alpaca("GET", f"{ALPACA_DATA_BASE}/v2/stocks/trades/latest?{params}")
    if status != 200:
        sys.exit(f"Could not fetch Alpaca prices ({status}): {payload}")

    prices = {}
    for symbol, entry in (payload.get("trades") or {}).items():
        price = entry.get("p")
        if price:
            prices[symbol] = float(price)
    return prices


def build_trade_plan(holdings, total_value, positions, prices):
    symbols = set(holdings) | set(positions)
    plan = []
    for symbol in sorted(symbols):
        price = prices.get(symbol)
        if not price:
            log.info("  skipping %s: no live price available", symbol)
            continue

        target_dollars = holdings.get(symbol, 0.0) * total_value
        target_shares = math.floor(target_dollars / price)
        current_shares = int(positions.get(symbol, 0))
        delta_shares = target_shares - current_shares

        if delta_shares > 0:
            side, quantity = "buy", delta_shares
        elif delta_shares < 0:
            # Never sells more than is currently held -- this script does not short.
            side, quantity = "sell", min(-delta_shares, current_shares)
        else:
            continue

        if quantity <= 0 or quantity * price < MIN_TRADE_DOLLARS:
            continue

        plan.append(
            {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": price,
                "current_shares": current_shares,
                "target_shares": target_shares,
            }
        )
    return plan


def place_order(trade):
    order = {
        "symbol": trade["symbol"],
        "qty": trade["quantity"],
        "side": trade["side"],
        "type": "market",
        "time_in_force": "day",
    }
    status, payload = _alpaca("POST", f"{ALPACA_TRADING_BASE}/v2/orders", data=order)
    if status not in (200, 201):
        log.error("  FAILED %s %s %s: (%s) %s", trade["side"], trade["quantity"], trade["symbol"], status, payload)
    else:
        log.info("  submitted %s %s %s", trade["side"], trade["quantity"], trade["symbol"])


# ------------------------------------------------------------------
# 5. Main
# ------------------------------------------------------------------


def main():
    log.info("=== Alpaca rebalance run starting (mode=%s, DRY_RUN=%s) ===", ALPACA_MODE, DRY_RUN)

    if "PASTE_YOUR" in ALPACA_API_KEY or "PASTE_YOUR" in ALPACA_API_SECRET:
        sys.exit(f"Alpaca API key/secret for mode={ALPACA_MODE} not set.")

    holdings = fetch_monstra_holdings()
    total_value, positions = get_account_snapshot()
    prices = get_last_prices(set(holdings) | set(positions))

    plan = build_trade_plan(holdings, total_value, positions, prices)
    if not plan:
        log.info("Account already matches target weights (within MIN_TRADE_DOLLARS). Nothing to do.")
        return

    log.info("Account value: $%.2f", total_value)
    log.info("Proposed trades:")
    for trade in plan:
        log.info(
            "  %-4s %6d %-8s @ ~$%.2f  (%s -> %s shares)",
            trade["side"],
            trade["quantity"],
            trade["symbol"],
            trade["price"],
            trade["current_shares"],
            trade["target_shares"],
        )

    if DRY_RUN:
        log.info("DRY_RUN is true -- no orders sent. Set DRY_RUN=false to enable live trading.")
        return

    log.info("Submitting orders...")
    for trade in plan:
        place_order(trade)


if __name__ == "__main__":
    main()
