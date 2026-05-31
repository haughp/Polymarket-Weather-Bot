import sys
import datetime
import pytest
from unittest.mock import patch, MagicMock
sys.path.insert(0, ".")


def _make_nws_responses(target_date_str: str, high_f: float):
    """Build mock httpx responses for the two NWS API calls."""
    points_resp = MagicMock()
    points_resp.raise_for_status = lambda: None
    points_resp.json.return_value = {
        "properties": {
            "forecast": "https://api.weather.gov/gridpoints/FWD/66,104/forecast"
        }
    }

    forecast_resp = MagicMock()
    forecast_resp.raise_for_status = lambda: None
    forecast_resp.json.return_value = {
        "properties": {
            "periods": [
                {
                    "startTime": f"{target_date_str}T06:00:00-05:00",
                    "endTime":   f"{target_date_str}T18:00:00-05:00",
                    "isDaytime": True,
                    "temperature": high_f,
                    "temperatureUnit": "F",
                    "name": "Today",
                },
                {
                    "startTime": f"{target_date_str}T18:00:00-05:00",
                    "endTime":   f"{target_date_str}T06:00:00-05:00",
                    "isDaytime": False,
                    "temperature": 72.0,
                    "temperatureUnit": "F",
                    "name": "Tonight",
                },
            ]
        }
    }
    return points_resp, forecast_resp


def test_fetch_nws_forecast_returns_high_temp():
    from ecmwf_forecast_pipeline import fetch_nws_forecast

    target = datetime.date(2026, 6, 1)
    pts, fcast = _make_nws_responses("2026-06-01", 95.0)

    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.side_effect = [pts, fcast]

    with patch("ecmwf_forecast_pipeline.httpx.Client", return_value=client_mock):
        result = fetch_nws_forecast(32.8998, -97.0403, target, "America/Chicago", "fahrenheit")

    assert result is not None
    temp, peak_utc = result
    assert temp == pytest.approx(95.0, abs=0.1)
    assert isinstance(peak_utc, datetime.datetime)


def test_fetch_nws_forecast_converts_to_celsius():
    from ecmwf_forecast_pipeline import fetch_nws_forecast

    target = datetime.date(2026, 6, 1)
    pts, fcast = _make_nws_responses("2026-06-01", 95.0)

    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.side_effect = [pts, fcast]

    with patch("ecmwf_forecast_pipeline.httpx.Client", return_value=client_mock):
        result = fetch_nws_forecast(32.8998, -97.0403, target, "America/Chicago", "celsius")

    assert result is not None
    temp, _ = result
    assert temp == pytest.approx(35.0, abs=0.2)   # (95 - 32) * 5/9


def test_fetch_nws_forecast_returns_none_on_http_error():
    from ecmwf_forecast_pipeline import fetch_nws_forecast
    import httpx

    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.side_effect = httpx.ConnectError("timeout")

    with patch("ecmwf_forecast_pipeline.httpx.Client", return_value=client_mock):
        result = fetch_nws_forecast(32.8998, -97.0403, datetime.date.today(),
                                    "America/Chicago", "fahrenheit")
    assert result is None


def test_extract_forecast_routes_dallas_to_nws(monkeypatch):
    from ecmwf_forecast_pipeline import extract_forecast

    nws_calls = []
    ecmwf_calls = []

    def mock_nws(lat, lon, target_date, timezone, units, mode='max'):
        nws_calls.append(True)
        return (95.0, datetime.datetime(2026, 6, 1, 18, 0, 0))

    def mock_ecmwf(lat, lon, target_date, timezone, units, mode='max'):
        ecmwf_calls.append(True)
        return (20.0, datetime.datetime(2026, 6, 1, 12, 0, 0))

    import ecmwf_forecast_pipeline
    monkeypatch.setattr(ecmwf_forecast_pipeline, "fetch_nws_forecast", mock_nws)
    monkeypatch.setattr(ecmwf_forecast_pipeline, "fetch_ecmwf_daily_and_peak", mock_ecmwf)

    result = extract_forecast("dallas")
    assert result is not None
    assert len(nws_calls) == 1
    assert len(ecmwf_calls) == 0


def test_extract_forecast_routes_shanghai_to_ecmwf(monkeypatch):
    from ecmwf_forecast_pipeline import extract_forecast

    nws_calls = []
    ecmwf_calls = []

    def mock_nws(lat, lon, target_date, timezone, units, mode='max'):
        nws_calls.append(True)
        return (30.0, datetime.datetime(2026, 6, 1, 8, 0, 0))

    def mock_ecmwf(lat, lon, target_date, timezone, units, mode='max'):
        ecmwf_calls.append(True)
        return (32.5, datetime.datetime(2026, 6, 1, 6, 0, 0))

    import ecmwf_forecast_pipeline
    monkeypatch.setattr(ecmwf_forecast_pipeline, "fetch_nws_forecast", mock_nws)
    monkeypatch.setattr(ecmwf_forecast_pipeline, "fetch_ecmwf_daily_and_peak", mock_ecmwf)

    result = extract_forecast("shanghai")
    assert result is not None
    assert len(nws_calls) == 0
    assert len(ecmwf_calls) == 1


def test_fetch_nws_forecast_returns_none_when_no_matching_period():
    from ecmwf_forecast_pipeline import fetch_nws_forecast

    target = datetime.date(2026, 6, 1)
    # Mock returns periods for 2026-06-02, not the requested 2026-06-01
    pts, fcast = _make_nws_responses("2026-06-02", 95.0)

    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.side_effect = [pts, fcast]

    with patch("ecmwf_forecast_pipeline.httpx.Client", return_value=client_mock):
        result = fetch_nws_forecast(32.8998, -97.0403, target, "America/Chicago", "fahrenheit")

    assert result is None
