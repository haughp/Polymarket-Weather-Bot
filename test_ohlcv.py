import pmxt

api = pmxt.Polymarket()
events = api.fetch_events(query='weather')
market = events[0].markets[0]

print(f'Testing OHLCV for: {market.question[:60]}')
try:
    hist = api.fetch_ohlcv(market.yes.outcome_id, resolution='1h', limit=10)
    print(f'Success: {len(hist)} candles')
except Exception as e:
    print(f'OHLCV Error: {type(e).__name__}: {e}')