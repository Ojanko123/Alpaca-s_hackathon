# Alpaca-s_hackathon
# Options Alpha Agents — Pairs Trading Bot

An autonomous options pairs-trading agent built for the **Alpaca AI Trading
Agents Hackathon**. It finds statistically cointegrated S&P 500 stocks,
trades divergence in their price relationship through defined-risk options
spreads, and enforces tested risk controls throughout — position sizing,
stop-loss, take-profit, drawdown circuit-breakers, and concentration limits.

Full write-up: see `writeup.pdf`
Full technical documentation and build log: see `documentation.pdf`

## How it works

1. **Universe** - scans a curated list of ~348 liquid S&P 500 tickers,
   verified live against Alpaca's own tradable-assets API
2. **Pair discovery** - filters by correlation (≥0.80), then tests
   cointegration (Engle-Granger) over a 60-day lookback
3. **Signal** - computes a daily z-score on each pair's spread; enters at
   |z| ≥ 1.5
4. **Options structure** - expresses the signal as a bull call spread /
   bear put spread (defined-risk by construction)
5. **Risk check** - position sizing, ticker-overlap, and drawdown checked
   before every entry
6. **Execution** - places genuine multi-leg (MLEG) orders via Alpaca's
   Trading API
7. **Monitoring** - a 30-minute cycle independently checks drawdown and
   take-profit, separate from the once-daily entry/exit cycle

## Files

| File | Purpose |
|---|---|
| `config.py` | All strategy and risk parameters in one place |
| `universe.py` | Ticker universe + pair discovery (correlation/cointegration) |
| `signals.py` | Z-score signal engine, entry/exit/stop classification |
| `risk_manager.py` | Position sizing, drawdown circuit-breaker, ticker-overlap guard, broker reconciliation |
| `options_builder.py` | Translates a pairs signal into a defined-risk options spread |
| `executor.py` | Talks to Alpaca: resolves contracts, places/closes orders |
| `logger.py` | Structured decision logging |
| `main.py` | Daily entrypoint (`python main.py`) and risk-check entrypoint (`python main.py --risk-check`) |
| `test_connection.py` | Verifies Alpaca API credentials before running the full pipeline |

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your Alpaca paper trading API key/secret/account ID
python test_connection.py   # verify credentials
python main.py               # run the daily entry/exit cycle
python main.py --risk-check  # run the frequent drawdown/take-profit check
```

In production this runs on a schedule via Windows Task Scheduler — see
`documentation.pdf` for the full setup.

## Built in public

Live trading surfaced real production issues no dry-run testing caught -
a fill-verification gap, a close-order bug targeting the wrong symbol
type, a strike-collision edge case, and a concentration-risk gap. Each is
documented with its root cause and fix in `documentation.pdf`, alongside
the full build log and honestly-stated known limitations.
