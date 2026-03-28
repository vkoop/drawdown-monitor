#!/usr/bin/env python3
"""Drawdown monitor: tracks ATH and sends Telegram alerts on drawdown thresholds."""

import json
import os
import sys
from datetime import datetime, timezone

import requests
import yfinance as yf


def load_json(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def save_json(path: str, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def send_telegram(token: str, chat_id: str, message: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    print(f"Telegram notification sent: {message}")


def fetch_price(ticker: str, lookback_days: int) -> tuple[float, float, str]:
    """Returns (current_price, period_high, period_high_date)."""
    data = yf.download(ticker, period=f"{lookback_days}d", auto_adjust=True, progress=False)
    if data.empty:
        raise ValueError(f"No data returned for {ticker}")

    current_price = float(data["Close"].iloc[-1])
    high_idx = data["Close"].idxmax()
    period_high = float(data["Close"].max())
    period_high_date = str(high_idx.date())
    return current_price, period_high, period_high_date


def main() -> None:
    config = load_json("config.json")
    state = load_json("state.json")

    ticker: str = config["ticker"]
    thresholds: list[int] = sorted(config["thresholds"])  # e.g. [-20, -30, -40]
    lookback_days: int = config.get("lookback_days", 365)

    telegram_token = os.environ.get("TELEGRAM_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not telegram_token or not telegram_chat_id:
        print("WARNING: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set — notifications disabled.")

    print(f"Fetching {ticker} (lookback {lookback_days}d)...")
    current_price, period_high, period_high_date = fetch_price(ticker, lookback_days)
    print(f"  Current price : {current_price:.4f}")
    print(f"  Period high   : {period_high:.4f} on {period_high_date}")

    # Update ATH if new all-time high within lookback window
    stored_ath = state.get("ath")
    if stored_ath is None or period_high > stored_ath:
        state["ath"] = period_high
        state["ath_date"] = period_high_date
        # When a new ATH is reached, reset triggered thresholds
        if stored_ath is not None and period_high > stored_ath:
            print(f"  New ATH detected ({period_high:.4f} > {stored_ath:.4f}) — resetting triggered thresholds.")
            state["triggered_thresholds"] = []

    ath: float = state["ath"]
    drawdown_pct: float = (current_price / ath - 1) * 100
    print(f"  ATH           : {ath:.4f} on {state['ath_date']}")
    print(f"  Drawdown      : {drawdown_pct:.2f}%")

    state["last_price"] = current_price
    state["last_check"] = datetime.now(timezone.utc).isoformat()

    triggered: list[int] = state.get("triggered_thresholds", [])

    for threshold in sorted(thresholds, reverse=True):  # -20, -30, -40 — fire closest first
        if drawdown_pct <= threshold and threshold not in triggered:
            triggered.append(threshold)
            print(f"  ALERT: drawdown crossed {threshold}%!")

            if telegram_token and telegram_chat_id:
                msg = (
                    f"⚠️ *Drawdown Alert: {ticker}*\n"
                    f"Drawdown: *{drawdown_pct:.2f}%* (threshold: {threshold}%)\n"
                    f"Current price: {current_price:.4f}\n"
                    f"ATH: {ath:.4f} ({state['ath_date']})\n"
                    f"Checked: {state['last_check'][:10]}"
                )
                send_telegram(telegram_token, telegram_chat_id, msg)

    state["triggered_thresholds"] = triggered
    save_json("state.json", state)
    print("State saved.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
