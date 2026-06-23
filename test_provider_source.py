import polymarket_dry_run as pdr


def test_pf_city_maps_new_york_to_nyc():
    assert pdr.pf_city("new_york") == "nyc"


def test_pf_city_identity_for_others():
    assert pdr.pf_city("paris") == "paris"
    assert pdr.pf_city("shanghai") == "shanghai"
