import sys
import datetime
import pytest
sys.path.insert(0, ".")
import run_ecmwf_loop


def test_loop_continues_after_pipeline_crash(monkeypatch):
    """Loop body must survive an exception in _run_pipeline and keep iterating."""
    calls = []

    def mock_pipeline(full_refresh=True):
        calls.append(full_refresh)
        if len(calls) == 2:           # first inside-loop call crashes
            raise RuntimeError("simulated import error")
        if len(calls) >= 4:           # stop after 3 successful loop iterations
            raise SystemExit(0)

    monkeypatch.setattr(run_ecmwf_loop, "_run_pipeline", mock_pipeline)
    monkeypatch.setattr(run_ecmwf_loop.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        run_ecmwf_loop, "_next_ecmwf_utc",
        lambda: datetime.datetime.utcnow() - datetime.timedelta(hours=1),
    )

    with pytest.raises(SystemExit):
        run_ecmwf_loop.main()

    assert len(calls) >= 4, (
        f"Loop should have continued after crash on call 2, got {len(calls)} calls"
    )


def test_sleep_cap_is_1800(monkeypatch):
    """sleep_secs must never exceed 1800 (30 min) regardless of ECMWF window distance."""
    sleep_values = []

    def mock_sleep(secs):
        sleep_values.append(secs)
        raise SystemExit(0)   # stop after first sleep

    def mock_pipeline(full_refresh=True):
        pass

    # Put next ECMWF window 10 hours away — old code would sleep 3600, new code 1800
    far_future = datetime.datetime.utcnow() + datetime.timedelta(hours=10)
    monkeypatch.setattr(run_ecmwf_loop, "_run_pipeline", mock_pipeline)
    monkeypatch.setattr(run_ecmwf_loop.time, "sleep", mock_sleep)
    monkeypatch.setattr(run_ecmwf_loop, "_next_ecmwf_utc", lambda: far_future)

    with pytest.raises(SystemExit):
        run_ecmwf_loop.main()

    assert sleep_values[0] <= 1800, (
        f"Expected sleep ≤ 1800s, got {sleep_values[0]}s"
    )
