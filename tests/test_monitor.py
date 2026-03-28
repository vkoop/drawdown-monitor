"""Unit tests for monitor.py."""

import copy
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from monitor import fetch_price, load_json, main, save_json, send_telegram

BASE_STATE = {
    "ath": 100.0,
    "ath_date": "2025-01-01",
    "last_price": None,
    "last_check": None,
    "triggered_thresholds": [],
}

BASE_CONFIG = {
    "ticker": "TEST.DE",
    "thresholds": [-20, -30, -40],
    "lookback_days": 365,
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
    fetch_result = fetch_override or (current_price, ath, state["ath_date"])

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


@pytest.mark.parametrize("price,ath,expected", [
    (100.0, 100.0,  0.0),
    ( 70.0, 100.0, -30.0),
    (110.0, 100.0,  10.0),
])
def test_drawdown_formula(price, ath, expected):
    assert (price / ath - 1) * 100 == pytest.approx(expected)


def test_fetch_price_returns_correct_values():
    closes = [100.0, 120.0, 90.0]
    with patch("monitor.yf.download", return_value=_make_ohlcv(closes)):
        price, high, high_date = fetch_price("TEST.DE", 365)

    assert price == pytest.approx(90.0)
    assert high == pytest.approx(120.0)
    assert high_date == "2025-01-02"


def test_fetch_price_raises_on_empty_data():
    with patch("monitor.yf.download", return_value=pd.DataFrame()):
        with pytest.raises(ValueError, match="No data returned"):
            fetch_price("MISSING.DE", 365)


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


def test_json_round_trip(tmp_path):
    data = {"ath": 123.45, "triggered_thresholds": [-20]}
    path = str(tmp_path / "state.json")
    save_json(path, data)
    assert load_json(path) == data


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


def test_ath_reset_clears_triggered_thresholds():
    state = {**BASE_STATE, "ath": 100.0, "triggered_thresholds": [-20]}
    saved, _ = _run_main(state, fetch_override=(110.0, 110.0, "2025-06-01"))
    assert saved["ath"] == pytest.approx(110.0)
    assert saved["triggered_thresholds"] == []
