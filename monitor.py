#!/usr/bin/env python3
"""Drawdown monitor: tracks ATH and sends Telegram alerts on drawdown thresholds."""

import json
import os
import sys

import numpy as np
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
    print(f"Telegram notification sent.")


def fetch_price(ticker: str, lookback_days: int) -> tuple[float, float, str, str]:
    """Returns (current_price, period_high, period_high_date, last_close_date)."""
    data = yf.download(ticker, period=f"{lookback_days}d", auto_adjust=True, progress=False)
    if data.empty:
        raise ValueError(f"No data returned for {ticker}")

    close = data["Close"].squeeze()  # MultiIndex → Series for single-ticker downloads
    current_price = float(close.iloc[-1])
    high_idx = close.idxmax()
    period_high = float(close.max())
    period_high_date = str(high_idx.date())
    last_close_date = str(close.index[-1].date())
    return current_price, period_high, period_high_date, last_close_date


def calc_velocity(prices: list[dict], days: int) -> float | None:
    """Linear regression slope over last `days` closes, in % per day.
    Returns None if insufficient data."""
    window = prices[-days:]
    if len(window) < days:
        return None
    y = np.array([p["price"] for p in window], dtype=float)
    x = np.arange(len(y), dtype=float)
    slope = np.polyfit(x, y, 1)[0]
    return float(slope / y.mean() * 100)


def main() -> None:
    config = load_json("config.json")
    state = load_json("state.json")

    ticker: str = config["ticker"]
    thresholds: list[int] = sorted(config["thresholds"])  # e.g. [-20, -30, -40]
    lookback_days: int = config.get("lookback_days", 365)
    velocity_days: int = config.get("velocity_days", 10)
    crash_threshold: float = config.get("velocity_crash_threshold", -0.5)
    recovery_threshold: float = config.get("velocity_recovery_threshold", 0.3)

    telegram_token = os.environ.get("TELEGRAM_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not telegram_token or not telegram_chat_id:
        print("WARNING: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set — notifications disabled.")

    print(f"Fetching {ticker} (lookback {lookback_days}d)...")
    current_price, period_high, period_high_date, last_close_date = fetch_price(ticker, lookback_days)
    print(f"  Current price : {current_price:.4f} ({last_close_date})")
    print(f"  Period high   : {period_high:.4f} on {period_high_date}")

    # Update ATH
    stored_ath = state.get("ath")
    if stored_ath is None or period_high > stored_ath:
        state["ath"] = period_high
        state["ath_date"] = period_high_date
        if stored_ath is not None and period_high > stored_ath:
            print(f"  New ATH detected ({period_high:.4f} > {stored_ath:.4f}) — resetting state.")
            state["triggered_thresholds"] = []
            state["trough"] = None
            state["trough_date"] = None
            state["prices"] = []
            state["velocity_state"] = None

    ath: float = state["ath"]
    drawdown_pct: float = (current_price / ath - 1) * 100
    print(f"  ATH           : {ath:.4f} on {state['ath_date']}")
    print(f"  Drawdown      : {drawdown_pct:.2f}%")

    # Update trough
    stored_trough = state.get("trough")
    if stored_trough is None or current_price < stored_trough:
        state["trough"] = current_price
        state["trough_date"] = last_close_date
        if stored_trough is not None:
            print(f"  New trough: {current_price:.4f}")

    trough: float = state["trough"]
    recovery_pct: float = (current_price / trough - 1) * 100 if trough else 0.0
    print(f"  Trough        : {trough:.4f} on {state['trough_date']} (+{recovery_pct:.1f}% recovery)")

    # Update rolling price window
    prices: list[dict] = state.get("prices", [])
    prices.append({"date": last_close_date, "price": current_price})
    prices = prices[-velocity_days:]
    state["prices"] = prices

    # Velocity
    velocity = calc_velocity(prices, velocity_days)
    if velocity is not None:
        print(f"  Velocity      : {velocity:+.3f}%/day ({velocity_days}d slope)")

    triggered: list[int] = state.get("triggered_thresholds", [])

    # Entry alerts: threshold crossed downward
    for threshold in sorted(thresholds, reverse=True):  # -20, -30, -40 — closest first
        if drawdown_pct <= threshold and threshold not in triggered:
            triggered.append(threshold)
            print(f"  ALERT: drawdown crossed {threshold}%!")

            if telegram_token and telegram_chat_id:
                msg = (
                    f"⚠️ *Drawdown Alert: {ticker}*\n"
                    f"Drawdown: *{drawdown_pct:.2f}%* (threshold: {threshold}%)\n"
                    f"Current price: {current_price:.4f}\n"
                    f"ATH: {ath:.4f} ({state['ath_date']})"
                )
                send_telegram(telegram_token, telegram_chat_id, msg)

    # Recovery alerts: threshold crossed upward
    for threshold in sorted(triggered):  # -40, -30, -20 — deepest first
        if drawdown_pct > threshold:
            triggered.remove(threshold)
            print(f"  RECOVERY: drawdown back above {threshold}%!")

            if telegram_token and telegram_chat_id:
                msg = (
                    f"✅ *Recovery Alert: {ticker}*\n"
                    f"Drawdown: *{drawdown_pct:.2f}%* (recovered past {threshold}%)\n"
                    f"Current price: {current_price:.4f}\n"
                    f"ATH: {ath:.4f} ({state['ath_date']})\n"
                    f"Trough: {trough:.4f} (+{recovery_pct:.1f}% from trough)"
                )
                send_telegram(telegram_token, telegram_chat_id, msg)

    # Velocity alerts: fire on state transitions only
    if velocity is not None:
        in_drawdown = len(triggered) > 0
        new_velocity_state = state.get("velocity_state")

        if in_drawdown and velocity < crash_threshold:
            new_velocity_state = "accelerating"
        elif velocity > recovery_threshold:
            new_velocity_state = "recovering"
        else:
            new_velocity_state = None

        old_velocity_state = state.get("velocity_state")
        if new_velocity_state != old_velocity_state:
            print(f"  VELOCITY: {old_velocity_state} → {new_velocity_state}")
            state["velocity_state"] = new_velocity_state

            if telegram_token and telegram_chat_id:
                if new_velocity_state == "accelerating":
                    msg = (
                        f"📉 *Crash accelerating: {ticker}*\n"
                        f"Velocity: *{velocity:+.2f}%/day* ({velocity_days}d slope)\n"
                        f"Drawdown: {drawdown_pct:.2f}%"
                    )
                elif new_velocity_state == "recovering":
                    msg = (
                        f"🚀 *Recovery momentum: {ticker}*\n"
                        f"Velocity: *{velocity:+.2f}%/day* ({velocity_days}d slope)\n"
                        f"Drawdown: {drawdown_pct:.2f}%"
                    )
                elif old_velocity_state == "accelerating":
                    msg = (
                        f"🔄 *Crash decelerating: {ticker}*\n"
                        f"Velocity: *{velocity:+.2f}%/day* ({velocity_days}d slope)\n"
                        f"Drawdown: {drawdown_pct:.2f}%"
                    )
                else:
                    msg = None

                if msg:
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
