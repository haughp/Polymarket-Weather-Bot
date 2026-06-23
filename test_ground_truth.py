import datetime as dt
import provider_backtest as pb

US_IN_OUTCOMES = {"nyc","chicago","miami","dallas","seattle","atlanta","austin"}

def test_us_outcomes_cities_use_db_actuals():
    # For US cities present in `outcomes`, the backtest must read DB actuals,
    # not a hardcoded IEM CLI station that disagrees with settlement.
    assert pb.us_actuals_source("nyc") == "settlement_db"
    assert pb.us_actuals_source("dallas") == "settlement_db"

def test_us_iem_only_cities_flagged_unverified():
    assert pb.us_actuals_source("houston") == "iem_cli_unverified"
