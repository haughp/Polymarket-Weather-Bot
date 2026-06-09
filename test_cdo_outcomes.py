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


# ── IEM ASOS fetcher ────────────────────────────────────────────────────────────

def _make_iem_response(csv_body: str):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.text = csv_body
    return resp


def _patch_iem_client(monkeypatch, resp):
    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    if isinstance(resp, Exception):
        client_mock.get.side_effect = resp
    else:
        client_mock.get.return_value = resp
    return patch("outcome_backfiller.httpx.Client", return_value=client_mock)


def test_fetch_iem_extremes_celsius_from_hourly(monkeypatch):
    from outcome_backfiller import _fetch_iem_extremes
    # onlycomma rows: station,valid,tmpc — daily max/min are 19.0 / 12.0
    body = (
        "station,valid,tmpc\n"
        "EGLC,2026-06-05 06:00,12.00\n"
        "EGLC,2026-06-05 14:00,19.00\n"
        "EGLC,2026-06-05 18:00,16.00\n"
    )
    with _patch_iem_client(monkeypatch, _make_iem_response(body)):
        max_t, min_t = _fetch_iem_extremes("london", datetime.date(2026, 6, 5))
    assert max_t == pytest.approx(19.0)
    assert min_t == pytest.approx(12.0)


def test_fetch_iem_extremes_fahrenheit_for_us_city(monkeypatch):
    from outcome_backfiller import _fetch_iem_extremes
    body = (
        "station,valid,tmpf\n"
        "KLGA,2026-06-05 06:00,65.00\n"
        "KLGA,2026-06-05 15:00,90.00\n"
    )
    with _patch_iem_client(monkeypatch, _make_iem_response(body)):
        max_t, min_t = _fetch_iem_extremes("new_york", datetime.date(2026, 6, 5))
    assert max_t == pytest.approx(90.0)   # native °F, no conversion
    assert min_t == pytest.approx(65.0)


def test_fetch_iem_extremes_none_for_unknown_city():
    from outcome_backfiller import _fetch_iem_extremes
    # hong_kong has no IEM_STATIONS entry (excluded → not in dict)
    assert _fetch_iem_extremes("hong_kong", datetime.date(2026, 6, 5)) == (None, None)


def test_fetch_iem_extremes_none_on_empty_body(monkeypatch):
    from outcome_backfiller import _fetch_iem_extremes
    with _patch_iem_client(monkeypatch, _make_iem_response("station,valid,tmpc\n")):
        assert _fetch_iem_extremes("london", datetime.date(2026, 6, 5)) == (None, None)


def test_fetch_iem_extremes_none_on_http_error(monkeypatch):
    from outcome_backfiller import _fetch_iem_extremes
    import httpx
    with _patch_iem_client(monkeypatch, httpx.ConnectError("timeout")):
        assert _fetch_iem_extremes("london", datetime.date(2026, 6, 5)) == (None, None)


# ── Routing cascade: IEM first, then NOAA CDO (US), then Open-Meteo ────────────────

def test_fetch_daily_extremes_routes_new_york_to_iem(monkeypatch):
    from outcome_backfiller import fetch_daily_extremes

    iem_calls, cdo_calls, om_calls = [], [], []

    def mock_iem(location_id, date):
        iem_calls.append(location_id)
        return (88.0, 65.0)

    import outcome_backfiller
    monkeypatch.setattr(outcome_backfiller, "_fetch_iem_extremes", mock_iem)
    monkeypatch.setattr(outcome_backfiller, "_fetch_cdo_extremes",
                        lambda s, d: cdo_calls.append(s) or (30.0, 20.0))
    monkeypatch.setattr(outcome_backfiller, "_fetch_open_meteo_extremes",
                        lambda l, d: om_calls.append(l) or (30.0, 20.0))

    max_t, min_t = fetch_daily_extremes("new_york", datetime.date(2026, 5, 29))

    assert iem_calls == ["new_york"]
    assert cdo_calls == []        # IEM won; CDO not tried
    assert om_calls == []
    assert max_t == pytest.approx(88.0)


def test_fetch_daily_extremes_routes_shanghai_to_iem(monkeypatch):
    from outcome_backfiller import fetch_daily_extremes

    iem_calls, om_calls = [], []

    import outcome_backfiller
    monkeypatch.setattr(outcome_backfiller, "_fetch_iem_extremes",
                        lambda l, d: iem_calls.append(l) or (33.5, 25.1))
    monkeypatch.setattr(outcome_backfiller, "_fetch_open_meteo_extremes",
                        lambda l, d: om_calls.append(l) or (99.9, 99.9))

    max_t, min_t = fetch_daily_extremes("shanghai", datetime.date(2026, 5, 29))

    assert iem_calls == ["shanghai"]
    assert om_calls == []
    assert max_t == pytest.approx(33.5)


def test_fetch_daily_extremes_excluded_city_skips_iem(monkeypatch):
    from outcome_backfiller import fetch_daily_extremes

    iem_calls, om_calls = [], []

    import outcome_backfiller
    monkeypatch.setattr(outcome_backfiller, "_fetch_iem_extremes",
                        lambda l, d: iem_calls.append(l) or (1.0, 1.0))
    monkeypatch.setattr(outcome_backfiller, "_fetch_open_meteo_extremes",
                        lambda l, d: om_calls.append(l) or (34.2, 28.1))

    # hong_kong is in IEM_EXCLUDED → must NOT call IEM, falls to Open-Meteo
    max_t, min_t = fetch_daily_extremes("hong_kong", datetime.date(2026, 5, 29))

    assert iem_calls == []
    assert om_calls == ["hong_kong"]
    assert max_t == pytest.approx(34.2)


def test_fetch_daily_extremes_falls_back_to_cdo_then_om_when_iem_empty(monkeypatch):
    from outcome_backfiller import fetch_daily_extremes

    cdo_calls, om_calls = [], []

    import outcome_backfiller
    monkeypatch.setattr(outcome_backfiller, "_fetch_iem_extremes", lambda l, d: (None, None))
    monkeypatch.setattr(outcome_backfiller, "_fetch_cdo_extremes",
                        lambda s, d: cdo_calls.append(s) or (None, None))   # CDO also fails
    monkeypatch.setattr(outcome_backfiller, "_fetch_open_meteo_extremes",
                        lambda l, d: om_calls.append(l) or (90.0, 68.0))

    max_t, min_t = fetch_daily_extremes("dallas", datetime.date(2026, 5, 29))

    assert cdo_calls == ["USW00003927"]   # US city: CDO tried after IEM
    assert om_calls == ["dallas"]         # then Open-Meteo
    assert max_t == pytest.approx(90.0)
