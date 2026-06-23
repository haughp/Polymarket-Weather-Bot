import polymarket_dry_run as pdr

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
