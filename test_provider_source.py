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
