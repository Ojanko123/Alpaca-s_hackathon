"""
Universe and pair discovery.

Loads S&P 500 constituents, pulls historical price data, and ranks
candidate pairs by correlation + cointegration over the configured
lookback window.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass

import pandas as pd

from config import CONFIG

logger = logging.getLogger(__name__)


@dataclass
class CandidatePair:
    ticker_a: str
    ticker_b: str
    correlation: float
    coint_pvalue: float
    hedge_ratio: float  # beta from OLS regression of A on B


def get_sp500_tickers(trading_client=None) -> list[str]:
    """
    Return the S&P 500 candidate universe, verified against Alpaca's own
    tradable assets list.

    We use a static ticker list as the "which companies are in the index"
    source (index membership isn't something Alpaca's API exposes), then
    call trading_client.get_all_assets() to confirm each one is actually
    active/tradable on Alpaca right now, dropping any that aren't. This
    keeps the universe defined without depending on scraping an external
    site, while still genuinely using Alpaca's own data for verification.

    If trading_client is not provided, returns the static list unverified.
    """
    if trading_client is None:
        logger.info("No trading_client provided, returning static list unverified (%d tickers)", len(_SP500_TICKERS))
        return _SP500_TICKERS

    from alpaca.trading.requests import GetAssetsRequest
    from alpaca.trading.enums import AssetClass, AssetStatus

    request = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    all_assets = trading_client.get_all_assets(request)
    tradable_symbols = {a.symbol for a in all_assets if a.tradable}

    verified = [t for t in _SP500_TICKERS if t in tradable_symbols]
    dropped = set(_SP500_TICKERS) - set(verified)
    if dropped:
        logger.info("Dropped %d tickers not currently tradable on Alpaca: %s", len(dropped), sorted(dropped))

    logger.info("Universe: %d tickers verified tradable on Alpaca", len(verified))
    return verified


# S&P 500 candidate universe. Static list (index membership isn't exposed by
# Alpaca's API), verified against Alpaca's live tradable-assets list above
# before use. Update periodically to track index changes/rebalances.
_SP500_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "JPM",
    "V", "UNH", "XOM", "JNJ", "WMT", "MA", "PG", "HD", "CVX", "MRK",
    "ABBV", "LLY", "PEP", "KO", "COST", "AVGO", "BAC", "PFE", "TMO", "CSCO",
    "MCD", "ACN", "ABT", "CRM", "DHR", "NFLX", "ADBE", "LIN", "CMCSA", "TXN",
    "WFC", "NKE", "DIS", "PM", "VZ", "NEE", "RTX", "UPS", "INTC", "AMD",
    "QCOM", "HON", "UNP", "IBM", "LOW", "BA", "GE", "CAT", "AMGN", "SPGI",
    "INTU", "SBUX", "GS", "ELV", "PLD", "AMAT", "MDT", "BKNG", "ISRG", "DE",
    "BLK", "GILD", "ADI", "MMC", "SYK", "TJX", "VRTX", "CVS", "REGN", "ADP",
    "CI", "MDLZ", "SLB", "ZTS", "SO", "PGR", "MO", "DUK", "ETN", "BSX",
    "CB", "EOG", "AON", "ITW", "APD", "CL", "FCX", "USB", "PNC", "MU",
    "SCHW", "AXP", "MS", "LMT", "C", "GD", "T", "TGT", "MMM", "PYPL",
    "NOW", "SHW", "EMR", "FDX", "ORCL", "COP", "NSC", "ICE", "MCO", "APH",
    "KLAC", "PANW", "CDNS", "SNPS", "ROP", "AJG", "CME", "MSI", "TT", "MAR",
    "CSX", "PSA", "FTNT", "ADSK", "WM", "ECL", "NXPI", "PH", "AZO", "CARR",
    "MCK", "TDG", "AEP", "ANET", "SRE", "AIG", "O", "CPRT", "MPC", "PSX",
    "TFC", "OXY", "D", "TRV", "PCAR", "MSCI", "CTAS", "KMB", "F", "GM",
    "NUE", "HES", "EW", "DXCM", "MET", "PAYX", "AFL", "SPG", "KMI", "PRU",
    "ROST", "VRSK", "IDXX", "CMI", "CHTR", "YUM", "ODFL", "EXC", "OTIS", "GWW",
    "A", "CTSH", "FAST", "DD", "BK", "GEHC", "KDP", "VICI", "IQV", "HSY",
    "DOW", "EA", "CTVA", "BIIB", "KHC", "ON", "WELL", "EIX", "ALL", "XEL",
    # --- expanded set: additional liquid large/mid-cap names for more candidate pairs ---
    "ABNB", "ADM", "AEE", "AES", "ALB", "ALGN", "AME", "AMP", "AMT",
    "ARE", "AVB", "AVY", "BALL", "BAX", "BBY", "BDX", "BEN", "BF-B", "BR",
    "BRO", "BXP", "CAG", "CAH", "CBRE", "CCI", "CDW", "CE", "CF", "CHD",
    "CINF", "CLX", "CMA", "CMS", "CNC", "CNP", "COF", "COO", "CPB", "CPT",
    "CSGP", "DAL", "DFS", "DG", "DGX", "DLR", "DLTR", "DOV", "DPZ", "DRI",
    "DTE", "DVA", "DVN", "EBAY", "ED", "EFX", "EL", "EMN", "ENPH", "EQIX",
    "EQR", "ES", "ESS", "ETR", "EVRG", "EXPD", "EXPE", "EXR", "FANG", "FE",
    "FFIV", "FIS", "FITB", "FMC", "FOXA", "FRT", "GEN", "GIS", "GL",
    "GLW", "GPC", "GPN", "GRMN", "HAL", "HAS", "HBAN", "HIG", "HOLX", "HPE",
    "HPQ", "HRL", "HST", "HUBB", "HUM", "HWM", "IEX", "IFF", "INCY", "INVH",
    "IP", "IPG", "IR", "IRM", "IT", "IVZ", "J", "JBHT", "JBL", "JCI",
    "JKHY", "JNPR", "K", "KEY", "KEYS", "KIM", "KMX", "KR", "L", "LDOS",
    "LEN", "LH", "LHX", "LKQ", "LNT", "LUV", "LVS", "LW", "LYB", "MAA",
    "MAS", "MHK", "MKC", "MKTX", "MLM", "MOH", "MOS", "MPWR", "MRO", "MTB",
    "MTCH", "MTD", "NDAQ", "NDSN", "NEM", "NI", "NRG", "NTAP", "NTRS", "NVR",
]


def fetch_price_history(tickers: list[str], lookback_days: int, data_client) -> pd.DataFrame:
    """
    Fetch daily close prices for the given tickers over the lookback window.

    `data_client` is expected to be an Alpaca historical data client
    (injected so this module stays easy to unit test with a mock).
    Returns a DataFrame indexed by date, one column per ticker.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    from datetime import datetime, timedelta

    end = datetime.utcnow()
    start = end - timedelta(days=int(lookback_days * 1.6))  # buffer for weekends/holidays

    request = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,  # free/paper accounts only have access to IEX, not the paid SIP feed
    )
    bars = data_client.get_stock_bars(request).df

    # bars comes back multi-indexed (symbol, timestamp) -> pivot to wide format
    prices = bars["close"].unstack(level=0)
    prices = prices.tail(lookback_days)
    return prices


def find_candidate_pairs(prices: pd.DataFrame) -> list[CandidatePair]:
    """
    Scan all pairwise combinations for correlation + cointegration.

    Uses the Engle-Granger two-step method (via statsmodels) since it's
    simple, fast enough for a ~500-name universe when pre-filtered by
    correlation first, and well understood for a write-up explanation.
    """
    from statsmodels.tsa.stattools import coint
    import numpy as np

    candidates: list[CandidatePair] = []
    tickers = prices.columns.tolist()
    corr_matrix = prices.corr()

    # Pre-filter by correlation before running the more expensive coint test
    pre_filtered = []
    for a, b in itertools.combinations(tickers, 2):
        corr = corr_matrix.loc[a, b]
        if pd.notna(corr) and corr >= CONFIG.min_correlation:
            pre_filtered.append((a, b, corr))

    logger.info("Correlation pre-filter: %d pairs above %.2f", len(pre_filtered), CONFIG.min_correlation)

    for a, b, corr in pre_filtered:
        series_a = prices[a].dropna()
        series_b = prices[b].dropna()
        joined = pd.concat([series_a, series_b], axis=1).dropna()
        if len(joined) < CONFIG.lookback_days * 0.8:
            continue  # not enough overlapping data

        try:
            _, pvalue, _ = coint(joined.iloc[:, 0], joined.iloc[:, 1])
        except Exception as exc:
            logger.warning("Cointegration test failed for %s/%s: %s", a, b, exc)
            continue

        if pvalue <= CONFIG.cointegration_pvalue_max:
            # hedge ratio via simple OLS: A = beta * B + const
            beta = np.polyfit(joined.iloc[:, 1], joined.iloc[:, 0], 1)[0]
            candidates.append(
                CandidatePair(
                    ticker_a=a,
                    ticker_b=b,
                    correlation=float(corr),
                    coint_pvalue=float(pvalue),
                    hedge_ratio=float(beta),
                )
            )

    # rank by strongest cointegration (lowest p-value) first
    candidates.sort(key=lambda c: c.coint_pvalue)
    top = candidates[: CONFIG.max_pairs_tracked]
    logger.info("Selected top %d cointegrated pairs", len(top))
    return top
