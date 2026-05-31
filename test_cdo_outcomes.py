import sys
import os
import datetime
import pytest
from unittest.mock import patch, MagicMock
sys.path.insert(0, ".")


def _make_cdo_response(tmax_tenths: int, tmin_tenths: int):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = {
        "results": [
            {"datatype": "TMAX", "value": tmax_tenths},
            {"datatype": "TMIN", "value": tmin_tenths},
        ]
    }
    return resp


def test_fetch_cdo_extremes_returns_fahrenheit(monkeypatch):
    from outcome_backfiller import _fetch_cdo_extremes

    monkeypatch.setenv("NOAA_CDO_TOKEN", "test-token-123")

    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.return_value = _make_cdo_response(tmax_tenths=950, tmin_tenths=720)

    with patch("outcome_backfiller.httpx.Client", return_value=client_mock):
        max_t, min_t = _fetch_cdo_extremes("USW00094728", datetime.date(2026, 5, 29))

    assert max_t == pytest.approx(95.0, abs=0.01)   # 950 / 10
    assert min_t == pytest.approx(72.0, abs=0.01)   # 720 / 10


def test_fetch_cdo_extremes_returns_none_without_token(monkeypatch):
    from outcome_backfiller import _fetch_cdo_extremes

    monkeypatch.delenv("NOAA_CDO_TOKEN", raising=False)
    max_t, min_t = _fetch_cdo_extremes("USW00094728", datetime.date(2026, 5, 29))
    assert max_t is None
    assert min_t is None


def test_fetch_cdo_extremes_returns_none_on_http_error(monkeypatch):
    from outcome_backfiller import _fetch_cdo_extremes
    import httpx

    monkeypatch.setenv("NOAA_CDO_TOKEN", "test-token-123")

    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.side_effect = httpx.ConnectError("timeout")

    with patch("outcome_backfiller.httpx.Client", return_value=client_mock):
        max_t, min_t = _fetch_cdo_extremes("USW00094728", datetime.date(2026, 5, 29))

    assert max_t is None
    assert min_t is None


def test_fetch_daily_extremes_routes_new_york_to_cdo(monkeypatch):
    from outcome_backfiller import fetch_daily_extremes

    cdo_calls = []
    om_calls = []

    def mock_cdo(station_id, date):
        cdo_calls.append(station_id)
        return (88.0, 65.0)

    def mock_om(location_id, date):
        om_calls.append(location_id)
        return (30.0, 20.0)

    import outcome_backfiller
    monkeypatch.setattr(outcome_backfiller, "_fetch_cdo_extremes", mock_cdo)
    monkeypatch.setattr(outcome_backfiller, "_fetch_open_meteo_extremes", mock_om)

    max_t, min_t = fetch_daily_extremes("new_york", datetime.date(2026, 5, 29))

    assert len(cdo_calls) == 1
    assert cdo_calls[0] == "USW00094728"
    assert len(om_calls) == 0
    assert max_t == pytest.approx(88.0)


def test_fetch_daily_extremes_routes_shanghai_to_open_meteo(monkeypatch):
    from outcome_backfiller import fetch_daily_extremes

    cdo_calls = []
    om_calls = []

    def mock_cdo(station_id, date):
        cdo_calls.append(station_id)
        return (30.0, 20.0)

    def mock_om(location_id, date):
        om_calls.append(location_id)
        return (33.5, 25.1)

    import outcome_backfiller
    monkeypatch.setattr(outcome_backfiller, "_fetch_cdo_extremes", mock_cdo)
    monkeypatch.setattr(outcome_backfiller, "_fetch_open_meteo_extremes", mock_om)

    max_t, min_t = fetch_daily_extremes("shanghai", datetime.date(2026, 5, 29))

    assert len(cdo_calls) == 0
    assert len(om_calls) == 1
    assert max_t == pytest.approx(33.5)


def test_fetch_daily_extremes_falls_back_to_open_meteo_when_cdo_fails(monkeypatch):
    from outcome_backfiller import fetch_daily_extremes

    om_calls = []

    def mock_cdo(station_id, date):
        return (None, None)   # CDO failed

    def mock_om(location_id, date):
        om_calls.append(location_id)
        return (90.0, 68.0)

    import outcome_backfiller
    monkeypatch.setattr(outcome_backfiller, "_fetch_cdo_extremes", mock_cdo)
    monkeypatch.setattr(outcome_backfiller, "_fetch_open_meteo_extremes", mock_om)

    max_t, min_t = fetch_daily_extremes("dallas", datetime.date(2026, 5, 29))

    assert len(om_calls) == 1   # fallback triggered
    assert max_t == pytest.approx(90.0)
