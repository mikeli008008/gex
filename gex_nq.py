#!/usr/bin/env python3
"""
gex_nq.py  —  QQQ gamma-exposure (GEX) -> NQ futures level mapper  [all-in-one]

ONE COMMAND does everything:
    python gex_nq.py

  * pulls the live QQQ options chain from CBOE (free delayed quotes, no API key)
  * pulls the live NQ front-month price from Yahoo (or pass --nq 29500)
  * computes:  Net GEX,  Gamma Flip (Black-Scholes spot sweep),
               Call Wall / Put Wall (net-per-strike),
               GX POC / VAH / VAL (gamma value area),  and top Magnets
  * scales every level to NQ via the live NQ/QQQ ratio
  * writes three ready-to-use files:
        gex_overlay.pine   -> paste into TradingView (input-based v6 overlay)
        gex_levels.txt     -> draw as horizontal lines in Tradovate (bulletproof)
        GexLevels.js       -> Tradovate custom indicator (auto-draws the lines)
    (+ levels.json if you pass --json)

No manual data collection anywhere. Delayed ~15 min and GEX/OI is a once-daily
number (OI settles post-close), so run it PRE-MARKET as your daily map.

Flags:  --max-dte 45   limit expiries considered (default 45)
        --nq 29500     override NQ price instead of auto-pull
        --outdir DIR   where to write files (default .)
        --json         also dump levels.json
        --flip-puts    flip dealer sign convention (advanced; default calls +, puts -)
"""

import re
import json
import math
import argparse
import time
import urllib.request
from datetime import date, datetime, timezone

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/QQQ.json"
YH_URL   = "https://query1.finance.yahoo.com/v8/finance/chart/NQ%3DF?interval=1d&range=1d"
UA       = {"User-Agent": "Mozilla/5.0"}
MULT     = 100          # option contract multiplier
RFR      = 0.045        # risk-free rate for the BS flip sweep
OPT_RE   = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _get_json(url, tries=6, base_delay=4, timeout=30):
    """GET json with retry+backoff. CBOE/Yahoo intermittently 503 or rate-limit
    GitHub runners; a single-shot fetch is why the daily job silently died."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except Exception as e:                       # noqa: BLE001
            last = e
            if i < tries - 1:
                time.sleep(base_delay * (i + 1))     # 4s, 8s, 12s, 16s, 20s
    raise RuntimeError(f"fetch failed after {tries} tries: {url} :: {type(last).__name__} {last}")



# ============================================================ data fetch
def fetch_qqq():
    d = _get_json(CBOE_URL)["data"]
    return float(d["current_price"]), d["options"]


def fetch_nq():
    d = _get_json(YH_URL)
    return float(d["chart"]["result"][0]["meta"]["regularMarketPrice"])


def fetch_ndx():
    """NDX cash index — used only for the NQ-NDX basis (the S: spread field)."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENDX?interval=1d&range=1d"
    d = _get_json(url)
    return float(d["chart"]["result"][0]["meta"]["regularMarketPrice"])


def fetch_crosscheck():
    """Best-effort independent QQQ GEX from InsiderFinance (free, no key).
    Returns QQQ-space levels for side-by-side validation. Non-fatal on failure."""
    url = "https://www.insiderfinance.io/gamma-exposure/QQQ"
    html = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30
                                  ).read().decode("utf-8", "ignore")

    def strong(label):
        m = re.search(r"<strong>\s*" + re.escape(label) + r"\s*:?\s*</strong>\s*\$?([\d,]+\.?\d*)",
                      html, re.I)
        return float(m.group(1).replace(",", "")) if m else None

    spot_m = re.search(r"spot price \(\$([\d,]+\.?\d*)\)", html)
    return {
        "src": "InsiderFinance",
        "spot": float(spot_m.group(1).replace(",", "")) if spot_m else None,
        "zero_gamma": strong("Zero-Gamma Level"),
        "call_wall":  strong("Call Wall"),
        "put_wall":   strong("Put Wall"),
    }


# ============================================================ parse + compute
def parse_opts(options, spot, max_dte):
    """return list of dicts: strike, typ, oi, gamma, iv, dte"""
    today = datetime.now(timezone.utc).date()
    out = []
    for o in options:
        m = OPT_RE.match(o.get("option", ""))
        if not m:
            continue
        _root, ymd, typ, strike8 = m.groups()
        oi = float(o.get("open_interest") or 0)
        gm = float(o.get("gamma") or 0)
        iv = float(o.get("iv") or 0)
        if oi <= 0 or gm <= 0:
            continue
        try:
            exp = datetime.strptime("20" + ymd, "%Y%m%d").date()
        except ValueError:
            continue
        dte = (exp - today).days
        if dte < 0 or dte > max_dte:
            continue
        out.append({
            "strike": int(strike8) / 1000.0,
            "typ": typ, "oi": oi, "gamma": gm, "iv": iv, "dte": dte,
        })
    return out


def gex_dollars(gamma, oi, spot):
    """dealer gamma exposure ($ per 1% move) for one option leg, unsigned"""
    return gamma * oi * MULT * spot * spot * 0.01


def bs_gamma(S, K, T, sigma):
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (RFR + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    npd1 = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return npd1 / (S * sigma * math.sqrt(T))


def compute(opts, spot, flip_puts=False):
    csign = -1.0 if flip_puts else 1.0
    psign = -csign

    # net GEX (all strikes) + per-strike maps
    win_lo, win_hi = spot * 0.88, spot * 1.12      # near-money window for level picking
    net = 0.0
    per_strike = {}     # strike -> net signed gex$  (windowed)
    abs_strike = {}     # strike -> total abs gex$   (windowed, for POC/VA/magnets)
    call_gex = {}       # strike -> call gamma$      (windowed, for call walls)
    put_gex  = {}       # strike -> put gamma$       (windowed, for put walls)
    call_oi  = {}       # strike -> call OI          (for max pain)
    put_oi   = {}       # strike -> put OI
    prof     = {}       # strike -> net signed gex$  (FULL chain, for the P: profile)
    for o in opts:
        g = gex_dollars(o["gamma"], o["oi"], spot)
        s = csign * g if o["typ"] == "C" else psign * g
        net += s                                    # net GEX uses the FULL chain
        k = o["strike"]
        prof[k] = prof.get(k, 0.0) + s
        if o["typ"] == "C":
            call_oi[k] = call_oi.get(k, 0.0) + o["oi"]
        else:
            put_oi[k] = put_oi.get(k, 0.0) + o["oi"]
        if win_lo <= k <= win_hi:                   # levels only from near-money
            per_strike[k] = per_strike.get(k, 0.0) + s
            abs_strike[k] = abs_strike.get(k, 0.0) + g
            if o["typ"] == "C":
                call_gex[k] = call_gex.get(k, 0.0) + g
            else:
                put_gex[k] = put_gex.get(k, 0.0) + g

    # walls: most positive strike = call wall, most negative = put wall
    call_wall = max(per_strike, key=lambda k: per_strike[k])
    put_wall  = min(per_strike, key=lambda k: per_strike[k])

    # GX POC = biggest abs-gamma strike ; VAH/VAL = POC +/- 1-session expected move
    poc = max(abs_strike, key=lambda k: abs_strike[k])
    atm_pool = [o for o in opts if o["dte"] >= 1] or opts   # skip 0DTE IV noise
    atm = min(atm_pool, key=lambda o: (abs(o["strike"] - spot), o["dte"]))
    sigma = spot * atm["iv"] * math.sqrt(1.0 / 252.0)   # ~1 trading-day 1-sigma move
    em = sigma
    vah, val = poc + em, poc - em

    # magnets: top abs-gamma strikes not already a named level
    named = {call_wall, put_wall, poc}
    mags = [k for k in sorted(abs_strike, key=lambda k: abs_strike[k], reverse=True)
            if k not in named][:3]

    # gamma flip via BS spot sweep (recompute gamma as spot shifts)
    flip = gamma_flip(opts, spot, csign, psign)

    # max pain: strike minimizing total option-holder payout (windowed)
    mp = max_pain(call_oi, put_oi, win_lo, win_hi)

    # ranked walls with detail (strike, gex$ , C/P ratio) for the override output
    def cp(k):
        c, p = call_gex.get(k, 0.0), put_gex.get(k, 0.0)
        return c / p if p > 0 else 99.9
    cwalls = [{"k": k, "gex": call_gex[k], "cp": cp(k)}
              for k in sorted(call_gex, key=lambda k: call_gex[k], reverse=True)
              if call_gex[k] > put_gex.get(k, 0.0)][:8]        # call-dominated only
    pwalls = [{"k": k, "gex": put_gex[k], "cp": cp(k)}
              for k in sorted(put_gex, key=lambda k: put_gex[k], reverse=True)
              if put_gex[k] > call_gex.get(k, 0.0)][:8]         # put-dominated only

    # full gamma profile for the P: section (strike -> net gex$), +/-13% of spot
    plo, phi = spot * 0.87, spot * 1.13
    profile = [(k, prof[k]) for k in sorted(prof) if plo <= k <= phi and abs(prof[k]) > 0]

    return {
        "net_gex": net, "call_wall": call_wall, "put_wall": put_wall,
        "gamma_flip": flip, "gx_poc": poc, "gx_vah": vah, "gx_val": val,
        "magnets": [(k, f"Mag {int(round(k))}") for k in mags],
        # extra fields for --override (QQQ units; scaled later):
        "max_pain": mp, "sigma": sigma, "call_walls": cwalls, "put_walls": pwalls,
        "profile": profile,
    }


def max_pain(call_oi, put_oi, lo, hi):
    strikes = sorted(k for k in set(call_oi) | set(put_oi) if lo <= k <= hi)
    if not strikes:
        return None
    best_k, best_pain = None, None
    for S in strikes:
        pain = sum(call_oi.get(K, 0) * max(0.0, S - K) for K in strikes) \
             + sum(put_oi.get(K, 0) * max(0.0, K - S) for K in strikes)
        if best_pain is None or pain < best_pain:
            best_pain, best_k = pain, S
    return best_k


def gamma_flip(opts, spot, csign, psign):
    """scan spot -15%..+15%, find net-gamma zero crossing nearest current spot"""
    lo, hi, n = spot * 0.85, spot * 1.15, 240
    xs, ys = [], []
    for j in range(n + 1):
        S = lo + (hi - lo) * j / n
        net = 0.0
        for o in opts:
            T = max(o["dte"], 0.5) / 365.0
            g = bs_gamma(S, o["strike"], T, o["iv"])
            val = gex_dollars(g, o["oi"], S)
            net += (csign if o["typ"] == "C" else psign) * val
        xs.append(S); ys.append(net)
    best = None
    for j in range(len(xs) - 1):
        if ys[j] == 0:
            cand = xs[j]
        elif ys[j] * ys[j + 1] < 0:                       # sign change -> interpolate
            t = ys[j] / (ys[j] - ys[j + 1])
            cand = xs[j] + t * (xs[j + 1] - xs[j])
        else:
            continue
        if best is None or abs(cand - spot) < abs(best - spot):
            best = cand
    return best


def scale_to_nq(levels, ratio):
    out = dict(levels)
    for k in ("call_wall", "put_wall", "gamma_flip", "gx_poc", "gx_vah", "gx_val",
              "max_pain", "sigma"):
        if out.get(k) is not None:
            out[k] = out[k] * ratio
    out["magnets"] = [(p * ratio, lbl) for p, lbl in out.get("magnets", [])]
    for wk in ("call_walls", "put_walls"):
        out[wk] = [{**w, "k": w["k"] * ratio} for w in out.get(wk, [])]
    out["profile"] = [(k * ratio, v) for k, v in out.get("profile", [])]
    return out


# ============================================================ EMIT (inlined)
def _f(x):
    return f"{float(x):.2f}"


def _regime(d):
    ng = d.get("net_gex")
    if ng is not None:
        return ("Negative γ (trend)", True) if ng < 0 else ("Positive γ (mean-revert)", False)
    s, fl = d.get("spot_nq"), d.get("gamma_flip")
    if s is not None and fl is not None:
        return ("Negative γ (trend)", True) if s < fl else ("Positive γ (mean-revert)", False)
    return ("Unknown", False)


def build_pine(d):
    regime_name, is_neg = _regime(d)

    def pin(name, default, title):
        dv = _f(default) if default else "0.0"
        return f'{name} = input.float({dv}, "{title}", group=G, inline="{name}")'

    L, a = [], None
    L = []; a = L.append
    a("//@version=6")
    a('indicator("GEX Levels (NQ) — auto-emit", overlay=true, max_lines_count=40, max_labels_count=40)')
    a('G = "GEX levels — regenerated pre-market by gex_nq.py"')
    a("")
    a(pin("callWall",  d.get("call_wall"),  "Call Wall"))
    a(pin("gammaFlip", d.get("gamma_flip"), "Gamma Flip"))
    a(pin("putWall",   d.get("put_wall"),   "Put Wall"))
    a('showGxVA = input.bool(%s, "Show GX POC/VAH/VAL", group=G)' % ("true" if d.get("gx_poc") else "false"))
    a(pin("gxPOC", d.get("gx_poc"), "GX POC"))
    a(pin("gxVAH", d.get("gx_vah"), "GX VAH"))
    a(pin("gxVAL", d.get("gx_val"), "GX VAL"))
    mags = d.get("magnets", [])[:4]
    for i, (p, lbl) in enumerate(mags):
        a(f'mag{i} = input.float({_f(p)}, "{lbl}", group="Magnets", inline="mag{i}")')
    a(f"nMag = {len(mags)}")
    a("")
    a(f'regimeTxt = "{regime_name}"')
    a("negGamma = %s" % ("true" if is_neg else "false"))
    a(f'asof = "{d.get("asof","")}"')
    a("")
    a("f_ray(_p, _col, _txt, _style) =>")
    a("    var line ln = na")
    a("    var label lb = na")
    a("    if barstate.islast and _p > 0")
    a("        line.delete(ln)")
    a("        ln := line.new(bar_index-1, _p, bar_index, _p, extend=extend.both, color=_col, width=1, style=_style)")
    a("        label.delete(lb)")
    a('        lb := label.new(bar_index, _p, _txt+"  "+str.tostring(_p, format.mintick), style=label.style_label_left, textcolor=_col, color=color.new(color.black,100), size=size.small)')
    a("")
    a('f_ray(callWall,  color.red,    "Call Wall",  line.style_solid)')
    a('f_ray(gammaFlip, color.yellow, "Gamma Flip", line.style_solid)')
    a('f_ray(putWall,   color.lime,   "Put Wall",   line.style_solid)')
    a("if showGxVA")
    a('    f_ray(gxPOC, color.orange, "GX POC", line.style_dashed)')
    a('    f_ray(gxVAH, color.gray,   "GX VAH", line.style_dotted)')
    a('    f_ray(gxVAL, color.gray,   "GX VAL", line.style_dotted)')
    for i in range(len(mags)):
        a(f'if nMag > {i}')
        a(f'    f_ray(mag{i}, color.aqua, "Magnet", line.style_dashed)')
    a("")
    a("bgcolor(negGamma ? color.new(color.red,94) : color.new(color.green,94))")
    a("var table t = table.new(position.top_right, 1, 2, border_width=1)")
    a("if barstate.islast")
    a("    table.cell(t, 0, 0, regimeTxt, text_color=negGamma?color.red:color.lime, text_size=size.small)")
    a("    table.cell(t, 0, 1, asof, text_color=color.gray, text_size=size.tiny)")
    return "\n".join(L) + "\n"


def build_txt(d):
    regime_name, _ = _regime(d)
    rows = []
    for key, lbl in [("call_wall","Call Wall  (red)"), ("gamma_flip","Gamma Flip (yellow)"),
                     ("put_wall","Put Wall   (green)"), ("gx_poc","GX POC   (orange)"),
                     ("gx_vah","GX VAH   (gray)"), ("gx_val","GX VAL   (gray)")]:
        if d.get(key):
            rows.append((d[key], lbl))
    for p, lbl in d.get("magnets", []):
        rows.append((p, f"{lbl} (aqua)"))
    rows.sort(key=lambda r: r[0], reverse=True)
    o = ["="*46, " NQ GEX LEVELS  —  draw as horizontal lines",
         f" Regime : {regime_name}", f" As of  : {d.get('asof','')}"]
    if d.get("spot_nq"):
        o.append(f" NQ spot: {_f(d['spot_nq'])}")
    if d.get("net_gex") is not None:
        o.append(f" Net GEX: {d['net_gex']/1e9:+.2f} $B/1%")
    o.append("="*46)
    for p, lbl in rows:
        o.append(f" {_f(p):>10}   {lbl}")
    o.append("="*46)
    o.append(" Tradovate: right-click chart > Horizontal Line, type each price.")
    return "\n".join(o) + "\n"


def build_js(d):
    plots, styles, rets = [], [], []
    def add(key, price, color, title):
        if not price:
            return
        plots.append(f'        {key}: {{ title: "{title}" }}')
        styles.append(f'            {key}: {{ color: "{color}", lineWidth: 1, lineStyle: 1 }}')
        rets.append(f"            {key}: {_f(price)}")
    add("callWall", d.get("call_wall"), "red", "Call Wall")
    add("gammaFlip", d.get("gamma_flip"), "yellow", "Gamma Flip")
    add("putWall", d.get("put_wall"), "lime", "Put Wall")
    add("gxPOC", d.get("gx_poc"), "orange", "GX POC")
    for i, (p, _l) in enumerate(d.get("magnets", [])[:4]):
        add(f"mag{i}", p, "aqua", f"Magnet {i+1}")
    return (
        '// GexLevels.js — paste into Tradovate Code Explorer (File > New)\n'
        f'// Auto-generated {d.get("asof","")}. Add to chart with "Overlay on price pane".\n'
        'const predef = require("./tools/predef");\n\n'
        'class GexLevels {\n    map() {\n        return {\n'
        + "\n".join(r + "," for r in rets) +
        '\n        };\n    }\n}\n\nmodule.exports = {\n'
        '    name: "GexLevels",\n    description: "GEX Levels (NQ) — auto-emit",\n'
        '    calculator: GexLevels,\n    tags: ["GEX"],\n    params: {},\n    plots: {\n'
        + ",\n".join(plots) + "\n    },\n    schemeStyles: {\n        dark: {\n"
        + ",\n".join(styles) + "\n        }\n    }\n};\n"
    )


def build_override(d, spread, live_em=False):
    """emit the S:<spread>|L:<entries> string the NQ/NDX GEX indicator ingests.
    Entry = price,CODE,NAME,tooltip1~tooltip2~...,weight  (structural weight=0,
    walls weight = Tot GEX in $M)."""
    def r(x):
        return int(round(x))

    zg  = d.get("gamma_flip")
    sig = d.get("sigma") or 0.0
    spot = d.get("spot_nq")
    ehigh = spot + sig
    elow  = spot - sig
    vh = (zg + 0.5 * sig) if zg else None
    vl = (zg - 0.5 * sig) if zg else None
    mp = d.get("max_pain")

    entries = []
    def add(price, code, name, tips, weight):
        if price is None:
            return
        entries.append(f"{r(price)},{code},{name},{'~'.join(tips)},{weight}")

    # sigma rides in ZG's weight slot: the indicator ignores weight for non-CW/PW
    # levels, and sigma is spot-independent, so Pine can rebuild EM live from close.
    add(zg, "ZG", "ZERO GAMMA",
        ["Zero Gamma", "Key pivot where dealer gamma flips", "Price magnet"],
        f"{sig:.1f}" if live_em else 0)
    add(mp, "MP", "MAX PAIN",
        ["Max Pain", "Strike with maximum option pain", "Expiry target"], 0)
    if not live_em:                       # static EM (anchored to generation-time spot)
        add(ehigh, "EH", "EM HIGH",
            ["Expected Move HIGH", "1-sigma upper boundary", "Statistical resistance"], 0)
        add(elow, "EL", "EM LOW",
            ["Expected Move LOW", "1-sigma lower boundary", "Statistical support"], 0)
    add(vh, "VH", "VOL HIGH",
        ["Vol Band HIGH", "Volatility-based resistance", "ZG + 25% EM"], 0)
    add(vl, "VL", "VOL LOW",
        ["Vol Band LOW", "Volatility-based support", "ZG - 25% EM"], 0)

    walls = [("CW", "Call Wall", d.get("call_walls", [])),
             ("PW", "Put Wall",  d.get("put_walls", []))]
    max_gex = max([w["gex"] for _c, _n, ws in walls for w in ws] + [1.0])
    for code, name, ws in walls:
        for w in ws:
            price, gex, cpr = w["k"], w["gex"], w["cp"]
            frm = (price - spot) / spot * 100 if spot else 0.0
            hold = int(round(min(90, 50 + 40 * (gex / max_gex))))
            tips = [f"Strike: {r(price)}", f"From Spot: {frm:+.2f}%",
                    f"Tot GEX: {gex/1e6:.2f}M", f"C/P Ratio: {cpr:.1f}",
                    f"Hold: {hold}% | Break: {100 - hold}%"]
            add(price, code, name, tips, f"{gex/1e6:.1f}")

    L_str = "S:{:.2f}|L:".format(spread) + ";".join(entries)

    # ----- P: gamma profile section (strike, value scaled to maxAbs=10, sign) -----
    prof = d.get("profile", [])
    if prof:
        maxabs = max(abs(v) for _k, v in prof) or 1.0
        pes = []
        for k, v in prof:
            scaled = v / maxabs * 10.0
            sign = 1 if v >= 0 else -1
            pes.append(f"{r(k)},{scaled:.1f},{sign}")
        return L_str + "|P:" + ";".join(pes) + "\n"
    return L_str + "\n"


def emit_all(d, outdir=".", spread=0.0, override=False, live_em=False):
    import os
    paths = {"pine": os.path.join(outdir, "gex_overlay.pine"),
             "txt":  os.path.join(outdir, "gex_levels.txt"),
             "js":   os.path.join(outdir, "GexLevels.js")}
    open(paths["pine"], "w").write(build_pine(d))
    open(paths["txt"], "w").write(build_txt(d))
    open(paths["js"], "w").write(build_js(d))
    if override:
        paths["override"] = os.path.join(outdir, "gex_override.txt")
        open(paths["override"], "w").write(build_override(d, spread, live_em))
    return paths


# ============================================================ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-dte", type=int, default=45)
    ap.add_argument("--nq", type=float, default=None)
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--flip-puts", action="store_true")
    ap.add_argument("--override", action="store_true",
                    help="also emit gex_override.txt for the NQ/NDX Pine indicator's Live GEX Override box")
    ap.add_argument("--live-em", action="store_true",
                    help="omit static EH/EL; carry sigma in ZG weight so Pine draws EM live off current close")
    ap.add_argument("--crosscheck", action="store_true",
                    help="fetch an independent QQQ GEX (InsiderFinance) and print side-by-side")
    args = ap.parse_args()

    print("Fetching QQQ chain from CBOE ...")
    qqq_spot, options = fetch_qqq()
    nq = args.nq if args.nq else fetch_nq()
    ratio = nq / qqq_spot
    print(f"QQQ spot {qqq_spot:.2f} | NQ {nq:.2f} | ratio {ratio:.4f}")

    opts = parse_opts(options, qqq_spot, args.max_dte)
    print(f"{len(opts)} live option legs within {args.max_dte} DTE")

    lv = compute(opts, qqq_spot, flip_puts=args.flip_puts)
    lv = scale_to_nq(lv, ratio)
    lv["spot_nq"] = nq
    lv["asof"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    spread = 0.0
    if args.override:
        try:
            spread = nq - fetch_ndx()          # NQ futures - NDX cash basis
        except Exception:
            spread = 0.0                        # non-fatal: basis is display metadata only

    paths = emit_all(lv, outdir=args.outdir, spread=spread, override=args.override,
                     live_em=args.live_em)
    if args.json:
        json.dump(lv, open(f"{args.outdir}/levels.json", "w"), indent=2)

    print()
    print(build_txt(lv))
    if args.override:
        print("----- Live GEX Override string -----")
        print(build_override(lv, spread, args.live_em))

    if args.crosscheck:
        print("----- Independent cross-check (both QQQ-derived, scaled to NQ) -----")
        try:
            cc = fetch_crosscheck()
            rows = [("Gamma Flip / Zero-Gamma", lv.get("gamma_flip"), cc.get("zero_gamma")),
                    ("Call Wall", lv.get("call_wall"), cc.get("call_wall")),
                    ("Put Wall",  lv.get("put_wall"),  cc.get("put_wall"))]
            print(f"  QQQ spot   ours {qqq_spot:.2f}   {cc['src']} {cc.get('spot')}")
            print(f"  {'level':24} {'OURS(NQ)':>10} {'THEIRS(NQ)':>11} {'Δ(NQ)':>8}")
            for name, ours, theirs in rows:
                if ours and theirs:
                    tnq = theirs * ratio
                    print(f"  {name:24} {ours:10.1f} {tnq:11.1f} {ours - tnq:8.1f}")
                else:
                    print(f"  {name:24} {'—' if not ours else f'{ours:.1f}':>10} "
                          f"{'—' if not theirs else f'{theirs*ratio:.1f}':>11}")
            print(f"  (source: {cc['src']}, QQQ options — validates our QQQ math, "
                  f"not the QQQ-vs-NDX question)")
        except Exception as e:
            print(f"  cross-check unavailable ({type(e).__name__}) — site HTML may have changed")

    print("Wrote:", ", ".join(paths.values()) + (", levels.json" if args.json else ""))


if __name__ == "__main__":
    main()
