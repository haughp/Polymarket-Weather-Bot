import polymarket_dry_run as pdr


def test_pf_city_maps_new_york_to_nyc():
    assert pdr.pf_city("new_york") == "nyc"


def test_pf_city_identity_for_others():
    assert pdr.pf_city("paris") == "paris"
    assert pdr.pf_city("shanghai") == "shanghai"


def test_bias_block_uses_matrix_cell_only(monkeypatch):
    # The bias source resolution helper returns (bias, source) from the matrix cell,
    # ignoring LIVE_BIAS / static dicts entirely.
    import polymarket_dry_run as pdr
    cell = {"provider": "icon_seamless", "bias": -0.41, "mae_debiased": 0.53, "unit": "C"}
    bias, source = pdr.resolve_bias("paris", "max", cell)
    assert bias == -0.41
    assert source.startswith("matrix")


def test_bias_block_skips_when_no_cell():
    import polymarket_dry_run as pdr
    assert pdr.resolve_bias("paris", "max", None) is None


def test_matrix_cell_lookup_resolves_nyc_via_pf_city_alias():
    # provider_matrix.json keys New York as "nyc"; the runtime's location_id
    # (from the forecasts table) is "new_york". The matrix-cell lookups in
    # record_dry_run() and main() must go through pf_city() so NYC resolves
    # to its populated cell instead of silently missing (matrix.get(loc_id)
    # would look up "new_york", which is never a key in provider_matrix.json).
    fake_matrix = {
        "nyc": {
            "max": {
                "provider": "ncep_nbm_conus",
                "bias": 1.0,
                "mae_debiased": 1.0,
                "unit": "F",
            }
        }
    }

    # Naive lookup keyed directly by the runtime location_id fails.
    assert fake_matrix.get("new_york", {}).get("max") is None

    # Lookup through pf_city() finds the real cell.
    cell = fake_matrix.get(pdr.pf_city("new_york"), {}).get("max")
    assert cell is not None
    assert cell["provider"] == "ncep_nbm_conus"
    assert cell["bias"] == 1.0
