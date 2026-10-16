# Weekly trend signals (NQ core + 12-month trend overlay)

Every Friday after the close, `.github/workflows/trend.yml` runs `trend_signals.py` and commits:

- **`latest.md`**: this week's targets, readable on GitHub or on your phone
- `latest.json`: same data, machine-readable
- `history.csv`: one row per market per week

## What it does

1. **Core:** hold `nq_core_x` × account notional long NQ (1.0 = unlevered NQ, sized in MNQ).
2. **Trend overlay:** 15 futures markets (ES, ZN, ZB, 6E, 6J, 6A, CL, NG, HO, GC, SI, HG, ZC, ZS, ZW).
   Each one is **LONG if price > price 252 trading days ago, else SHORT**, sized so every market risks
   the same dollars per day. The whole book targets half of NQ's trailing vol (`trend_vol: "half_nq"`),
   or a fixed annual vol such as `0.10`.

Trade the new targets at the next session. Only adjust a position if it's off by at least 1 contract.

## Config (`config.json`)

```json
{ "account_usd": 500000, "nq_core_x": 1.0, "trend_vol": "half_nq" }
```

## Backtest (daily data 2001–Oct 2026, after costs and rolls; research code is separate)

| | CAGR | Vol | Sharpe | Max DD |
|---|---|---|---|---|
| NQ buy & hold (1x) | 12.7% | 22.9% | 0.64 | −54% |
| **1x NQ + half-size trend overlay** | **18.5%** | 24.9% | **0.81** | −54% |
| Same, 2010–now | 23.1% | 23.3% | 1.01 | −30% (NQ alone −35%) |

The trend book's correlation to NQ is about −0.05 monthly. Its best years have come when stocks fell
(2008: +17%, 2022: +19%). On its own it's lumpy: 8 down years out of 26, and −23% in 2016.

## Caveats

- Uses Yahoo unadjusted front-month prices, so contract rolls add small fake jumps. Check the
  contract counts against your broker before trading.
- Below about $300k several markets can't be held even as one micro (marked ⚠️).
- Micro contracts listed: MES, M6E, M6A, MCL, MGC, SIL, MHG. Confirm availability and specs with your broker.
