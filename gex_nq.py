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
import urllib.request
from datetime import date, datetime, timezone

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/QQQ.json"
YH_URL   = "https://query1.finance.yahoo.com/v8/finance/chart/NQ%3DF?interval=1d&range=1d"
UA       = {"User-Agent": "Mozilla/5.0"}
MULT     = 100          # option contract multiplier
RFR      = 0.045        # risk-free rate for the BS flip sweep
OPT_RE   = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


# ============================================================ data fetch
def fetch_qqq():
    req = urllib.request.Request(CBOE_URL, headers=UA)
    d = json.load(urllib.request.urlopen(req, timeout=30))["data"]
    return float(d["current_price"]), d["options"]


def fetch_nq():
    req = urllib.request.Request(YH_URL, headers=UA)
    d = json.load(urllib.request.urlopen(req, timeout=25))
    return float(d["chart"]["result"][0]["meta"]["regularMarketPrice"])


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
    for o in opts:
        g = gex_dollars(o["gamma"], o["oi"], spot)
        s = csign * g if o["typ"] == "C" else psign * g
        net += s                                    # net GEX uses the FULL chain
        if win_lo <= o["strike"] <= win_hi:         # levels only from near-money
            per_strike[o["strike"]] = per_strike.get(o["strike"], 0.0) + s
            abs_strike[o["strike"]] = abs_strike.get(o["strike"], 0.0) + g

    # walls: most positive strike = call wall, most negative = put wall
    call_wall = max(per_strike, key=lambda k: per_strike[k])
    put_wall  = min(per_strike, key=lambda k: per_strike[k])

    # GX POC = biggest abs-gamma strike ; VAH/VAL = POC +/- 1-session expected move
    poc = max(abs_strike, key=lambda k: abs_strike[k])
    atm = min(opts, key=lambda o: (abs(o["strike"] - spot), o["dte"]))
    em = spot * atm["iv"] * math.sqrt(1.0 / 252.0)      # ~1 trading-day expected move
    vah, val = poc + em, poc - em

    # magnets: top abs-gamma strikes not already a named level
    named = {call_wall, put_wall, poc}
    mags = [k for k in sorted(abs_strike, key=lambda k: abs_strike[k], reverse=True)
            if k not in named][:3]

    # gamma flip via BS spot sweep (recompute gamma as spot shifts)
    flip = gamma_flip(opts, spot, csign, psign)

    return {
        "net_gex": net, "call_wall": call_wall, "put_wall": put_wall,
        "gamma_flip": flip, "gx_poc": poc, "gx_vah": vah, "gx_val": val,
        "magnets": [(k, f"Mag {int(round(k))}") for k in mags],
    }


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
    for k in ("call_wall", "put_wall", "gamma_flip", "gx_poc", "gx_vah", "gx_val"):
        if out.get(k) is not None:
            out[k] = out[k] * ratio
    out["magnets"] = [(p * ratio, lbl) for p, lbl in out.get("magnets", [])]
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


def emit_all(d, outdir="."):
    import os
    paths = {"pine": os.path.join(outdir, "gex_overlay.pine"),
             "txt":  os.path.join(outdir, "gex_levels.txt"),
             "js":   os.path.join(outdir, "GexLevels.js")}
    open(paths["pine"], "w").write(build_pine(d))
    open(paths["txt"], "w").write(build_txt(d))
    open(paths["js"], "w").write(build_js(d))
    return paths


# ============================================================ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-dte", type=int, default=45)
    ap.add_argument("--nq", type=float, default=None)
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--flip-puts", action="store_true")
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

    paths = emit_all(lv, outdir=args.outdir)
    if args.json:
        json.dump(lv, open(f"{args.outdir}/levels.json", "w"), indent=2)

    print()
    print(build_txt(lv))
    print("Wrote:", ", ".join(paths.values()) + (", levels.json" if args.json else ""))


if __name__ == "__main__":
    main()
