#!/usr/bin/env python3
"""Weekly 12-month trend signals + NQ core sizing.

Strategy (backtested 2001-2026 on daily data, see trend/README.md):
  * 15 futures markets; each is LONG if price > price 252 trading days ago, else SHORT
  * each market sized to equal daily dollar risk (trailing 60-day EWMA of $ moves per contract)
  * the whole trend book targets TREND_VOL annual vol of account (default: half of NQ's trailing vol)
  * optional core: hold NQ_CORE_X times account notional long NQ (1.0 = unlevered NQ)
  * rebalance weekly (run Friday after close, trade next session)

Outputs (trend/):  latest.md  latest.json  history.csv
"""
import argparse, json, math, os, sys
from datetime import datetime, timezone
import numpy as np, pandas as pd

try:
    import yfinance as yf
except ImportError:
    sys.exit("pip install yfinance pandas numpy")

HERE = os.path.dirname(os.path.abspath(__file__))

# symbol: (name, sector, $ multiplier full-size, micro symbol or None, micro size as fraction of full)
MARKETS = {
    'ES': ('S&P 500',        'Equity', 50,         'MES', 0.1),
    'ZN': ('10Y T-Note',     'Rates',  1000,       None,  None),
    'ZB': ('30Y T-Bond',     'Rates',  1000,       None,  None),
    '6E': ('Euro FX',        'FX',     125000,     'M6E', 0.1),
    '6J': ('Japanese Yen',   'FX',     12_500_000, None,  None),
    '6A': ('Australian $',   'FX',     100000,     'M6A', 0.1),
    'CL': ('Crude Oil',      'Energy', 1000,       'MCL', 0.1),
    'NG': ('Natural Gas',    'Energy', 10000,      None,  None),
    'HO': ('Heating Oil',    'Energy', 42000,      None,  None),
    'GC': ('Gold',           'Metals', 100,        'MGC', 0.1),
    'SI': ('Silver',         'Metals', 5000,       'SIL', 0.2),
    'HG': ('Copper',         'Metals', 25000,      'MHG', 0.1),
    'ZC': ('Corn',           'Ags',    50,         None,  None),
    'ZS': ('Soybeans',       'Ags',    50,         None,  None),
    'ZW': ('Wheat',          'Ags',    50,         None,  None),
}
NQ_MULT, MNQ_MULT = 20, 2


def fetch(sym, period='4y'):
    d = yf.download(f'{sym}=F', interval='1d', period=period, progress=False, auto_adjust=False)
    if d is None or d.empty:
        raise RuntimeError(f'no data for {sym}')
    d.columns = d.columns.get_level_values(0)
    d = d[['Open', 'High', 'Low', 'Close']].dropna()
    r = d.Close.pct_change()
    bad = (r.abs() > 0.15) & (r.shift(-1) * r < 0) & (r.shift(-1).abs() > 0.6 * r.abs())  # one-bar data spikes
    return d[~bad]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--outdir', default=HERE)
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    acct = float(cfg['account_usd'])
    nq_core_x = float(cfg.get('nq_core_x', 1.0))
    trend_vol_cfg = cfg.get('trend_vol', 'half_nq')

    data = {s: fetch(s) for s in MARKETS}
    nq = fetch('NQ')
    asof = min(d.index[-1] for d in list(data.values()) + [nq]).date()

    # ---- trend book vol target ----
    nq_vol = nq.Close.pct_change().tail(252).std() * math.sqrt(252)
    trend_vol = 0.5 * nq_vol if trend_vol_cfg == 'half_nq' else float(trend_vol_cfg)

    # ---- per-market signal + $ risk per contract; unit P&L for diversification estimate ----
    rows, units = [], {}
    for s, (name, sector, mult, micro, mfrac) in MARKETS.items():
        C = data[s].Close
        if len(C) < 300:
            raise RuntimeError(f'{s}: not enough history ({len(C)} bars)')
        sig = 1 if C.iloc[-1] > C.iloc[-253] else -1
        sd_pts = C.diff().ewm(span=60, min_periods=40).std()
        sd_usd = float(sd_pts.iloc[-1] * mult)
        hist_sig = np.sign(C - C.shift(252)).shift(1)
        units[s] = (hist_sig * C.diff() / sd_pts.shift(1)).dropna()
        rows.append(dict(sym=s, name=name, sector=sector, signal='LONG' if sig > 0 else 'SHORT', sig=sig,
                         last=float(C.iloc[-1]), chg_12m_pct=round((C.iloc[-1] / C.iloc[-253] - 1) * 100, 1),
                         usd_risk_per_contract_day=round(sd_usd), micro=micro, micro_frac=mfrac))

    U = pd.DataFrame(units).dropna().tail(504)
    div_ratio = U.sum(axis=1).std() / U.std().mean()          # portfolio sd vs single-market sd
    port_daily_usd = acct * trend_vol / math.sqrt(252)
    per_mkt_usd = port_daily_usd / div_ratio

    for r in rows:
        full = per_mkt_usd / r['usd_risk_per_contract_day']
        r['target_full'] = round(full, 2)
        r['contracts_full'] = int(round(full)) * r['sig']
        if r['micro']:
            r['contracts_micro'] = int(round(full / r['micro_frac'])) * r['sig']
        else:
            r['contracts_micro'] = None
        tradable = abs(r['contracts_full']) >= 1 or (r['contracts_micro'] not in (None, 0))
        r['tradable'] = bool(tradable)

    # ---- NQ core ----
    nq_last = float(nq.Close.iloc[-1])
    core_notional = acct * nq_core_x
    core = dict(nq_last=nq_last, notional=core_notional,
                NQ=round(core_notional / (nq_last * NQ_MULT), 2),
                MNQ=int(round(core_notional / (nq_last * MNQ_MULT))))

    # ---- flips vs last run ----
    prev_path = os.path.join(a.outdir, 'latest.json')
    prev = {}
    if os.path.exists(prev_path):
        try:
            prev = {r['sym']: r['signal'] for r in json.load(open(prev_path))['markets']}
        except Exception:
            prev = {}
    for r in rows:
        r['flipped'] = bool(prev) and prev.get(r['sym']) not in (None, r['signal'])

    out = dict(asof=str(asof), generated_utc=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
               account_usd=acct, trend_vol=round(trend_vol, 4), nq_vol=round(nq_vol, 4),
               div_ratio=round(div_ratio, 2), per_market_daily_risk_usd=round(per_mkt_usd),
               nq_core=core, markets=rows)
    os.makedirs(a.outdir, exist_ok=True)
    json.dump(out, open(prev_path, 'w'), indent=2, default=float)

    # ---- markdown ----
    L = [f"# Trend signals — as of {asof} close",
         f"_Generated {out['generated_utc']}. Trade at the next session; hold until next Friday's run._", "",
         f"**Account:** ${acct:,.0f} · **Trend book target vol:** {trend_vol*100:.1f}%/yr "
         f"(NQ trailing vol {nq_vol*100:.1f}%) · **Risk per market:** ~${per_mkt_usd:,.0f}/day", "",
         "## Core: NQ long",
         f"{nq_core_x:g}x notional = ${core_notional:,.0f} → **{core['MNQ']} MNQ** (= {core['NQ']} NQ) at {nq_last:,.2f}", "",
         "## Trend book",
         "| | Market | Signal | 12m % | Target (full) | Full contracts | Micro contracts |",
         "|---|---|---|---|---|---|---|"]
    order = ['Equity', 'Rates', 'FX', 'Energy', 'Metals', 'Ags']
    for r in sorted(rows, key=lambda x: (order.index(x['sector']), x['sym'])):
        flag = '🔁 ' if r['flipped'] else ''
        micro = f"{r['contracts_micro']:+d} {r['micro']}" if r['micro'] else '—'
        full = f"{r['contracts_full']:+d} {r['sym']}" if r['contracts_full'] else '0'
        warn = '' if r['tradable'] else ' ⚠️ too small'
        L.append(f"| {flag}| {r['sym']} {r['name']} | {r['signal']} | {r['chg_12m_pct']:+.1f} | "
                 f"{r['target_full']:.2f} | {full} | {micro}{warn} |")
    flips = [r['sym'] for r in rows if r['flipped']]
    L += ["", f"**Signal flips since last run:** {', '.join(flips) if flips else 'none'}", "",
          "_Rule: LONG if price > 252 trading days ago, else SHORT. Equal dollar risk per market. "
          "⚠️ = account too small to hold even one contract (full or micro) at target size; skip or accept oversizing. "
          "Contract math uses unadjusted front-month prices from Yahoo — double-check against your broker._"]
    md = '\n'.join(L) + '\n'
    open(os.path.join(a.outdir, 'latest.md'), 'w').write(md)

    # ---- history ----
    hp = os.path.join(a.outdir, 'history.csv')
    h = pd.DataFrame([dict(asof=asof, sym=r['sym'], signal=r['signal'], chg_12m_pct=r['chg_12m_pct'],
                           target_full=r['target_full'], contracts_full=r['contracts_full'],
                           contracts_micro=r['contracts_micro']) for r in rows])
    if os.path.exists(hp):
        old = pd.read_csv(hp)
        old = old[old.asof.astype(str) != str(asof)]
        h = pd.concat([old, h])
    h.to_csv(hp, index=False)
    print(md)


if __name__ == '__main__':
    main()
