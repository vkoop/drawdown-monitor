"""Unit tests for monitor.py."""

import copy
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from monitor import calc_velocity, fetch_price, load_json, main, save_json, send_telegram

BASE_STATE = {
    "ath": 100.0,
    "ath_date": "2025-01-01",
    "trough": None,
    "trough_date": None,
    "triggered_thresholds": [],
    "prices": [],
    "velocity_state": None,
}

BASE_CONFIG = {
    "ticker": "TEST.DE",
    "thresholds": [-20, -30, -40],
    "lookback_days": 365,
    "velocity_days": 5,
    "velocity_crash_threshold": -0.5,
    "velocity_recovery_threshold": 0.3,
}


def _make_ohlcv(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"Close": closes}, index=dates)


def _run_main(state: dict, drawdown_pct: float = 0.0, env: dict | None = None,
              fetch_override: tuple | None = None):
    """Run main() with controlled state. fetch_override bypasses drawdown_pct."""
    state = copy.deepcopy(state)  # prevent list mutation leaking across tests
    ath = state["ath"]
    current_price = ath * (1 + drawdown_pct / 100)
    fetch_result = fetch_override or (current_price, ath, state["ath_date"], "2025-01-10")

    saved = {}

    def fake_save(path, data):
        saved.update(data)

    default_env = {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
    effective_env = default_env if env is None else env

    with (
        patch("monitor.load_json", side_effect=[BASE_CONFIG, state]),
        patch("monitor.save_json", side_effect=fake_save),
        patch("monitor.fetch_price", return_value=fetch_result),
        patch("monitor.send_telegram") as mock_tg,
        patch.dict("os.environ", effective_env, clear=True),
    ):
        main()

    return saved, mock_tg


def _state_with_prices(prices: list[float], triggered=None, velocity_state=None) -> dict:
    """Build a state with a price history window."""
    return {
        **BASE_STATE,
        "trough": min(prices),
        "trough_date": "2025-01-01",
        "triggered_thresholds": triggered or [],
        "velocity_state": velocity_state,
        "prices": [{"date": f"2025-01-{i+1:02d}", "price": p} for i, p in enumerate(prices)],
    }


# ---------------------------------------------------------------------------
# Drawdown formula
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("price,ath,expected", [
    (100.0, 100.0,  0.0),
    ( 70.0, 100.0, -30.0),
    (110.0, 100.0,  10.0),
])
def test_drawdown_formula(price, ath, expected):
    assert (price / ath - 1) * 100 == pytest.approx(expected)


# ---------------------------------------------------------------------------
# fetch_price
# ---------------------------------------------------------------------------

def test_fetch_price_returns_correct_values():
    closes = [100.0, 120.0, 90.0]
    with patch("monitor.yf.download", return_value=_make_ohlcv(closes)):
        price, high, high_date, last_date = fetch_price("TEST.DE", 365)

    assert price == pytest.approx(90.0)
    assert high == pytest.approx(120.0)
    assert high_date == "2025-01-02"
    assert last_date == "2025-01-03"


def test_fetch_price_raises_on_empty_data():
    with patch("monitor.yf.download", return_value=pd.DataFrame()):
        with pytest.raises(ValueError, match="No data returned"):
            fetch_price("MISSING.DE", 365)


# ---------------------------------------------------------------------------
# send_telegram
# ---------------------------------------------------------------------------

def test_send_telegram_posts_correct_payload():
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None

    with patch("monitor.requests.post", return_value=mock_resp) as mock_post:
        send_telegram("mytoken", "123456", "hello")

    _, kwargs = mock_post.call_args
    assert kwargs["json"]["text"] == "hello"
    assert kwargs["json"]["chat_id"] == "123456"
    assert "mytoken" in mock_post.call_args[0][0]


def test_send_telegram_raises_on_http_error():
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("HTTP 403")

    with patch("monitor.requests.post", return_value=mock_resp):
        with pytest.raises(Exception, match="HTTP 403"):
            send_telegram("bad_token", "123", "msg")


# ---------------------------------------------------------------------------
# load_json / save_json
# ---------------------------------------------------------------------------

def test_json_round_trip(tmp_path):
    data = {"ath": 123.45, "triggered_thresholds": [-20]}
    path = str(tmp_path / "state.json")
    save_json(path, data)
    assert load_json(path) == data


# ---------------------------------------------------------------------------
# calc_velocity
# ---------------------------------------------------------------------------

def test_calc_velocity_returns_none_when_insufficient_data():
    prices = [{"date": "2025-01-01", "price": p} for p in [100.0, 99.0]]
    assert calc_velocity(prices, days=5) is None


def test_calc_velocity_negative_for_declining_prices():
    prices = [{"date": f"2025-01-0{i+1}", "price": 100.0 - i} for i in range(5)]
    assert calc_velocity(prices, days=5) < 0


def test_calc_velocity_positive_for_rising_prices():
    prices = [{"date": f"2025-01-0{i+1}", "price": 100.0 + i} for i in range(5)]
    assert calc_velocity(prices, days=5) > 0


def test_calc_velocity_zero_for_flat_prices():
    prices = [{"date": f"2025-01-0{i+1}", "price": 100.0} for i in range(5)]
    assert calc_velocity(prices, days=5) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Entry / exit threshold alerts
# ---------------------------------------------------------------------------

def test_no_alert_above_all_thresholds():
    saved, mock_tg = _run_main({**BASE_STATE}, drawdown_pct=-10.0)
    assert saved["triggered_thresholds"] == []
    mock_tg.assert_not_called()


def test_alert_fires_when_threshold_crossed():
    saved, mock_tg = _run_main({**BASE_STATE}, drawdown_pct=-25.0)
    assert -20 in saved["triggered_thresholds"]
    mock_tg.assert_called_once()


def test_no_duplicate_alert_for_already_triggered():
    state = {**BASE_STATE, "triggered_thresholds": [-20]}
    saved, mock_tg = _run_main(state, drawdown_pct=-25.0)
    assert saved["triggered_thresholds"].count(-20) == 1
    mock_tg.assert_not_called()


def test_multiple_thresholds_fire_at_once():
    saved, mock_tg = _run_main({**BASE_STATE}, drawdown_pct=-35.0)
    assert -20 in saved["triggered_thresholds"]
    assert -30 in saved["triggered_thresholds"]
    assert mock_tg.call_count == 2


def test_no_telegram_when_env_vars_missing():
    _, mock_tg = _run_main({**BASE_STATE}, drawdown_pct=-25.0, env={})
    mock_tg.assert_not_called()


def test_ath_reset_clears_state():
    state = {**BASE_STATE, "ath": 100.0, "triggered_thresholds": [-20],
             "prices": [{"date": "2025-01-01", "price": 80.0}], "velocity_state": "accelerating"}
    saved, _ = _run_main(state, fetch_override=(110.0, 110.0, "2025-06-01", "2025-06-01"))
    assert saved["ath"] == pytest.approx(110.0)
    assert saved["triggered_thresholds"] == []
    assert len(saved["prices"]) == 1  # reset + new price appended
    assert saved["velocity_state"] is None


# ---------------------------------------------------------------------------
# Trough tracking
# ---------------------------------------------------------------------------

def test_trough_set_on_first_run():
    saved, _ = _run_main({**BASE_STATE}, drawdown_pct=-25.0)
    assert saved["trough"] == pytest.approx(75.0)


def test_trough_updates_on_new_low():
    state = {**BASE_STATE, "trough": 80.0, "trough_date": "2025-01-10"}
    saved, _ = _run_main(state, drawdown_pct=-25.0)
    assert saved["trough"] == pytest.approx(75.0)


def test_trough_not_updated_on_recovery():
    state = {**BASE_STATE, "trough": 60.0, "trough_date": "2025-01-10"}
    saved, _ = _run_main(state, drawdown_pct=-25.0)
    assert saved["trough"] == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# Recovery alerts
# ---------------------------------------------------------------------------

def test_recovery_alert_fires_when_drawdown_improves():
    state = {**BASE_STATE, "trough": 75.0, "trough_date": "2025-01-10", "triggered_thresholds": [-20]}
    saved, mock_tg = _run_main(state, drawdown_pct=-10.0)
    assert -20 not in saved["triggered_thresholds"]
    mock_tg.assert_called_once()


def test_recovery_alert_includes_trough_info():
    state = {**BASE_STATE, "trough": 75.0, "trough_date": "2025-01-10", "triggered_thresholds": [-20]}
    _, mock_tg = _run_main(state, drawdown_pct=-10.0)
    msg = mock_tg.call_args[0][2]
    assert "Recovery" in msg
    assert "75" in msg


def test_no_recovery_alert_still_below_threshold():
    state = {**BASE_STATE, "trough": 65.0, "trough_date": "2025-01-10", "triggered_thresholds": [-20]}
    saved, mock_tg = _run_main(state, drawdown_pct=-25.0)
    assert -20 in saved["triggered_thresholds"]
    mock_tg.assert_not_called()


def test_recovery_removes_only_recovered_threshold():
    state = {**BASE_STATE, "trough": 60.0, "trough_date": "2025-01-10",
             "triggered_thresholds": [-20, -30]}
    saved, mock_tg = _run_main(state, drawdown_pct=-25.0)
    assert -30 not in saved["triggered_thresholds"]
    assert -20 in saved["triggered_thresholds"]
    mock_tg.assert_called_once()


# ---------------------------------------------------------------------------
# Velocity alerts
# ---------------------------------------------------------------------------

def test_velocity_alert_crash_accelerating():
    # 5 steeply declining prices → crash accelerating
    state = _state_with_prices([100, 98, 95, 91, 86], triggered=[-20])
    saved, mock_tg = _run_main(state, drawdown_pct=-25.0)
    assert saved["velocity_state"] == "accelerating"
    calls = [c[0][2] for c in mock_tg.call_args_list]
    assert any("accelerating" in m for m in calls)


def test_velocity_alert_recovery_momentum():
    # 5 rising prices while recovering (no thresholds triggered)
    state = _state_with_prices([86, 88, 91, 95, 98], triggered=[])
    saved, mock_tg = _run_main(state, drawdown_pct=-5.0)
    assert saved["velocity_state"] == "recovering"
    calls = [c[0][2] for c in mock_tg.call_args_list]
    assert any("momentum" in m for m in calls)


def test_velocity_alert_crash_decelerating():
    # Was accelerating, prices now flat near current level → deceleration alert
    # current_price = 75.0 (drawdown -25%), window after append: [75.8,75.5,75.2,75.0,75.0]
    # slope ≈ -0.26%/day → above crash_threshold (-0.5) → state resets to None
    state = _state_with_prices([76.0, 75.8, 75.5, 75.2, 75.0], triggered=[-20],
                                velocity_state="accelerating")
    saved, mock_tg = _run_main(state, drawdown_pct=-25.0)
    assert saved["velocity_state"] is None
    calls = [c[0][2] for c in mock_tg.call_args_list]
    assert any("decelerating" in m for m in calls)


def test_no_velocity_alert_when_insufficient_data():
    # Fewer prices than velocity_days → no velocity alert
    state = {**BASE_STATE, "triggered_thresholds": [-20],
             "prices": [{"date": "2025-01-01", "price": 80.0}]}
    saved, mock_tg = _run_main(state, drawdown_pct=-25.0)
    mock_tg.assert_not_called()


def test_no_repeat_velocity_alert_same_state():
    # Already "accelerating", still accelerating → no new alert
    state = _state_with_prices([100, 98, 95, 91, 86], triggered=[-20],
                                velocity_state="accelerating")
    _, mock_tg = _run_main(state, drawdown_pct=-25.0)
    calls = [c[0][2] for c in mock_tg.call_args_list]
    assert not any("accelerating" in m for m in calls)
