#!/usr/bin/env python3
"""Rebalance an IBKR account to this week's trend targets (trend/latest.json).

Runs next to a logged-in IB Gateway (see trend/IBKR_SETUP.md). Default is a DRY RUN:
it prints the plan and places nothing.

  python ibkr_rebalance.py                 # dry run against paper gateway (port 4002)
  python ibkr_rebalance.py --execute       # place orders (paper accounts only)
  python ibkr_rebalance.py --execute --allow-live   # live account: do NOT use until paper is proven

What it does each run:
  1. loads targets (local latest.json or the GitHub raw URL)
  2. picks, per market, the contract month to hold: the nearest liquid month that is
     more than ROLL_BUFFER days from last trade (so physical-delivery markets roll
     before first notice)
  3. compares with current IBKR futures positions; positions in an old month are closed
     and re-opened in the new month (a roll)
  4. places Adaptive-algo market orders for the differences
Positions in futures this system doesn't manage are left alone and listed in the report.
"""
import argparse, json, os, sys, urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta

RAW_URL = 'https://raw.githubusercontent.com/mikeli008008/gex/main/trend/latest.json'
HERE = os.path.dirname(os.path.abspath(__file__))

# signal symbol -> (instrument used, IB symbol, IB exchange, liquid months or None=all, roll buffer days)
# Micros are used wherever they exist (finer sizing, no flip-flopping between full and micro).
Q = [3, 6, 9, 12]
INSTR = {
    'NQ': ('MNQ', 'MNQ', 'CME',   Q, 8),          # core
    'ES': ('MES', 'MES', 'CME',   Q, 8),
    'ZN': ('ZN',  'ZN',  'CBOT',  Q, 40),         # FND is ~end of month before delivery month
    'ZB': ('ZB',  'ZB',  'CBOT',  Q, 40),
    '6E': ('M6E', 'M6E', 'CME',   Q, 7),
    '6J': ('6J',  'JPY', 'CME',   Q, 7),
    '6A': ('M6A', 'M6A', 'CME',   Q, 7),
    'CL': ('MCL', 'MCL', 'NYMEX', None, 5),
    'NG': ('NG',  'NG',  'NYMEX', None, 5),
    'HO': ('HO',  'HO',  'NYMEX', None, 5),
    'GC': ('MGC', 'MGC', 'COMEX', [2, 4, 6, 8, 10, 12], 35),
    'SI': ('SIL', 'SIL', 'COMEX', [3, 5, 7, 9, 12], 35),
    'HG': ('MHG', 'MHG', 'COMEX', [3, 5, 7, 9, 12], 35),
    'ZC': ('ZC',  'ZC',  'CBOT',  [3, 5, 7, 9, 12], 30),
    'ZS': ('ZS',  'ZS',  'CBOT',  [1, 3, 5, 7, 8, 9, 11], 30),
    'ZW': ('ZW',  'ZW',  'CBOT',  [3, 5, 7, 9, 12], 30),
}


NO_TRADE_BAND = 0.20   # same-direction position within 20% of target: leave it


@dataclass
class Leg:
    sig: str          # signal symbol, e.g. 'GC'
    ib_symbol: str    # e.g. 'MGC'
    conid: int
    month: str        # YYYYMM
    qty: int          # signed contracts to trade
    reason: str


def load_targets(path):
    if path and os.path.exists(path):
        return json.load(open(path))
    with urllib.request.urlopen(RAW_URL, timeout=30) as r:
        return json.load(r)


def target_qty(t):
    """{signal symbol: signed contracts of the instrument in INSTR}."""
    out = {'NQ': int(t['nq_core']['MNQ'])}
    for m in t['markets']:
        instr = INSTR[m['sym']][0]
        out[m['sym']] = int(m['contracts_micro'] if instr != m['sym'] and m['contracts_micro'] is not None
                            else m['contracts_full'])
    return out


def choose_month(details, months, buffer_days, today):
    """details: list of (conid, contractMonth 'YYYYMM', lastTrade 'YYYYMMDD'). Pick the hold contract."""
    cands = []
    for conid, cm, ltd in details:
        if months and int(cm[4:6]) not in months:
            continue
        last = datetime.strptime(ltd[:8], '%Y%m%d').date()
        if last - timedelta(days=buffer_days) > today:
            cands.append((last, conid, cm))
    if not cands:
        raise RuntimeError('no eligible contract month')
    last, conid, cm = min(cands)
    return conid, cm


def plan(targets, positions, chosen, today=None):
    """Pure planning step.
    targets:   {sig: signed qty}
    positions: {ib_symbol: [(conid, month, qty), ...]}   current holdings
    chosen:    {sig: (conid, month)}                      contract to hold now
    returns    list[Leg], list of unmanaged ib symbols
    """
    legs = []
    managed = {INSTR[s][1] for s in INSTR}
    for sig, tgt in targets.items():
        ib_sym = INSTR[sig][1]
        conid, month = chosen[sig]
        held = positions.get(ib_sym, [])
        cur_in_target = 0
        for pc, pm, pq in held:
            if pc == conid:
                cur_in_target += pq
            elif pq:
                legs.append(Leg(sig, ib_sym, pc, pm, -pq, f'roll out of {pm}'))
        diff = tgt - cur_in_target
        same_side = tgt * cur_in_target > 0
        if diff and same_side and abs(diff) / abs(tgt) < NO_TRADE_BAND:
            continue                      # small re-size on an unchanged signal: not worth the costs
        if diff:
            legs.append(Leg(sig, ib_sym, conid, month, diff,
                            f'target {tgt:+d}, holding {cur_in_target:+d} in {month}'))
    unmanaged = sorted(s for s in positions if s not in managed)
    return legs, unmanaged


def notify(text):
    tok, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if not (tok and chat):
        return
    data = json.dumps({'chat_id': chat, 'text': text[:4000]}).encode()
    req = urllib.request.Request(f'https://api.telegram.org/bot{tok}/sendMessage', data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        print(f'telegram failed: {e}', file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=4002, help='4002 = IB Gateway paper, 4001 = live')
    ap.add_argument('--client-id', type=int, default=17)
    ap.add_argument('--targets', default=os.path.join(HERE, 'latest.json'),
                    help='local latest.json; falls back to GitHub raw URL if missing')
    ap.add_argument('--max-age-days', type=int, default=6, help='refuse targets older than this')
    ap.add_argument('--execute', action='store_true', help='actually place orders')
    ap.add_argument('--allow-live', action='store_true', help='permit a non-paper account')
    a = ap.parse_args()

    from ib_async import IB, Future, MarketOrder, TagValue

    t = load_targets(a.targets if os.path.exists(a.targets) else None)
    asof = datetime.strptime(t['asof'], '%Y-%m-%d').date()
    today = date.today()
    if (today - asof).days > a.max_age_days:
        sys.exit(f'targets are from {asof}, older than {a.max_age_days} days — Friday job may have failed; not trading')
    targets = target_qty(t)

    ib = IB()
    ib.connect(a.host, a.port, clientId=a.client_id, timeout=20)
    accts = ib.managedAccounts()
    acct = accts[0]
    is_paper = acct.startswith('DU')
    if a.execute and not is_paper and not a.allow_live:
        sys.exit(f'account {acct} is LIVE; refusing to trade without --allow-live')

    # contract month to hold, per market
    chosen, specs, problems = {}, {}, []
    for sig in list(targets):
        _, ib_sym, exch, months, buf = INSTR[sig]
        try:
            cds = ib.reqContractDetails(Future(symbol=ib_sym, exchange=exch, currency='USD'))
            if not cds:
                raise RuntimeError(f'no contracts for {ib_sym} on {exch}')
            det = [(cd.contract.conId, cd.contractMonth, cd.contract.lastTradeDateOrContractMonth) for cd in cds]
            chosen[sig] = choose_month(det, months, buf, today)
            specs.update({cd.contract.conId: cd.contract for cd in cds})
        except Exception as e:                      # skip this market, keep going
            problems.append(f'{sig} ({ib_sym}): {e}')
            targets.pop(sig)

    # current futures positions
    positions = {}
    for p in ib.positions(acct):
        c = p.contract
        if c.secType != 'FUT' or not p.position:
            continue
        positions.setdefault(c.symbol, []).append((c.conId, c.lastTradeDateOrContractMonth[:6], int(p.position)))
        specs.setdefault(c.conId, c)

    legs, unmanaged = plan(targets, positions, chosen)

    lines = [f"Trend rebalance — targets as of {asof} — account {acct} ({'PAPER' if is_paper else 'LIVE'})",
             f"mode: {'EXECUTE' if a.execute else 'DRY RUN (nothing sent)'}", '']
    if not legs:
        lines.append('Already on target. No orders.')
    for l in legs:
        lines.append(f"{'BUY ' if l.qty > 0 else 'SELL'} {abs(l.qty):>3} {l.ib_symbol} {l.month}  ({l.sig}: {l.reason})")
    if unmanaged:
        lines += ['', f"Not managed, left alone: {', '.join(unmanaged)}"]
    if problems:
        lines += ['', 'SKIPPED (check symbol/permissions):'] + [f'  {p}' for p in problems]

    if a.execute and legs:
        lines.append('')
        trades = []
        for l in legs:
            c = specs[l.conid]
            ib.qualifyContracts(c)
            o = MarketOrder('BUY' if l.qty > 0 else 'SELL', abs(l.qty), account=acct,
                            algoStrategy='Adaptive', algoParams=[TagValue('adaptivePriority', 'Normal')])
            trades.append((l, ib.placeOrder(c, o)))
        ib.sleep(30)
        for l, tr in trades:
            s = tr.orderStatus
            lines.append(f"{l.ib_symbol} {l.month} {l.qty:+d}: {s.status} filled {s.filled:g} @ {s.avgFillPrice:g}")

    report = '\n'.join(lines)
    print(report)
    os.makedirs(os.path.join(HERE, 'logs'), exist_ok=True)
    with open(os.path.join(HERE, 'logs', f'rebalance_{today}.log'), 'a') as f:
        f.write(f'--- {datetime.now().isoformat(timespec="seconds")}\n{report}\n')
    notify(report)
    ib.disconnect()


if __name__ == '__main__':
    main()
