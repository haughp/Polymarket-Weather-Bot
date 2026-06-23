import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import polymarket_dry_run as pdr
from database_schema import Base, LadderSnapshot, TradeSimulation


def _cand(lo, hi, w, p):
    return {"market": {}, "question": f"[{lo},{hi})", "range": (lo, hi),
            "width": w, "midpoint": (lo+hi)/2, "yes_price": p}

def test_ladder_rows_built_for_all_candidates():
    cands = [_cand(36,37,1,0.15), _cand(37,38,1,0.20), _cand(38,39,1,0.30)]
    F = cands[1]; neighbour = cands[2]
    rows = pdr.build_ladder_rows("paris", "max", "2026-06-24", cands, F, neighbour)
    assert len(rows) == 3
    f_row = next(r for r in rows if r["bucket_lo"] == 37)
    assert f_row["is_F"] is True and f_row["is_second_leg"] is False
    n_row = next(r for r in rows if r["bucket_lo"] == 38)
    assert n_row["is_second_leg"] is True


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def test_bad_ladder_row_does_not_abort_trade_commit(session):
    """Reviewer-flagged guarantee: LadderSnapshot is only validated by SQLAlchemy
    at flush/commit, not at session.add(). Since ladder rows and the real
    TradeSimulation share one session/commit, a malformed ladder row must be
    caught and rolled back via its own SAVEPOINT (session.begin_nested()) so it
    can never abort the outer transaction that also persists the trade record.

    This reproduces the exact failure mode: a non-numeric value in a Numeric
    column (bucket_lo) raises a StatementError at flush time — i.e. inside
    begin_nested(), which flushes on exit — not at add() time.
    """
    market_date = datetime.date(2026, 6, 24)

    # The real trade record, added the same way record_dry_run does before
    # touching the ladder-logging block.
    session.add(TradeSimulation(
        location_id="paris", mode="max", market_date=market_date,
        clob_token_id="tok1", question_text="q", market_side="F",
        order_type="YES", price=0.2, size=5.0, dry_run=True,
    ))

    # Simulate record_dry_run's ladder-logging try block with one malformed row.
    try:
        with session.begin_nested():
            session.add(LadderSnapshot(
                location_id="paris", mode="max", market_date=market_date,
                bucket_lo="not-a-number",  # invalid for Numeric(5,1) -> flush-time error
                bucket_width=1, yes_price=0.2, is_F=True, is_second_leg=False,
            ))
    except Exception as e:
        caught = e
    else:
        caught = None

    assert caught is not None, "expected the malformed row to raise at flush time"

    # The outer commit (the same one that persists TradeSimulation in
    # record_dry_run/main) must still succeed, and the trade row must survive —
    # only the bad ladder row should have been rolled back.
    session.commit()

    assert session.query(TradeSimulation).count() == 1
    assert session.query(LadderSnapshot).count() == 0
