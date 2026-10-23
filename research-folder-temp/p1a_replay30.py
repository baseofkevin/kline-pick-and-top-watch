#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-A 研究：用 ChangePCT<=30 的横截面重放 18 周，比较 9.5% 上限前后的最终名单。"""
import importlib.util, json, os, re

SRC = "/home/kevin/MyData/kline-select-stocks-approach"
DB = os.path.join(SRC, "database")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "p1a_data")

spec = importlib.util.spec_from_file_location("rule", os.path.join(SRC, "screen_kline.py"))
rule = importlib.util.module_from_spec(spec); spec.loader.exec_module(rule)

km = json.load(open(os.path.join(DB, "kline_2025-08-05_2026-10-14.json")))
days = json.load(open(os.path.join(DB, "trading_calendar.json")))["days"]
mv_dates = sorted(os.path.basename(p)[3:-5] for p in
                  __import__("glob").glob(os.path.join(DB, "mv_????-??-??.json")))

def load(p, default):
    try: return json.load(open(p, encoding="utf-8"))
    except Exception: return default

def replay(S, chg_hi):
    hist = [d for d in days if d <= S]
    need = hist[-5:]  # N=4 => K=5
    cross = {}
    for d in need:
        rows = load(os.path.join(OUT, f"cross30_{d}.json"), [])
        cross[d] = {r["code"]: r for r in rows if r.get("code")}
    st, new = {}, {}
    for d in need:
        lb = load(os.path.join(DB, f"labels_{d}.json"), {"st": [], "new": []})
        st[d] = set(lb["st"]); new[d] = set(lb["new"])
    pool = sorted({c for d in need for c in cross[d]})
    hit, rej = [], {}
    for c in pool:
        name = next((cross[d][c].get("name") for d in need if c in cross[d]), "")
        det, why0 = rule.find_start(c, name, km.get(c, []), S)
        if det is None:
            rej[why0] = rej.get(why0, 0) + 1; continue
        ok, why = rule.judge(det, km[c], S, 4, cross, st, new,
                             rule.K_VOL, rule.MA60_GAP, rule.C2_TOL, rule.C3_RATIO,
                             rule.CHG_LO, chg_hi)
        if not ok:
            k = re.sub(r"[\d.]+", "#", why)
            rej[k] = rej.get(k, 0) + 1; continue
        row0 = cross[det["d_star"]][c]
        nf = rule.fnum(row0.get("MainNetFlow"))
        mvf = load(os.path.join(DB, f"mv_{S}.json"), {})
        fc = mvf.get(c, [None])[0]
        ratio = nf / fc * 100 if (nf is not None and fc) else None
        if ratio is None or ratio < rule.FLOW_MIN:
            rej["flow"] = rej.get("flow", 0) + 1; continue
        det.update({"S": S, "chg_d": rule.fnum(row0.get("ChangePCT")),
                    "chg_s": (det["close"] / km[c][-1][1] - 1) * 100 if km[c] else None,
                    "flow_ratio": round(ratio, 3), "float_cap": fc,
                    "board": ("star" if c.startswith("sh688") else
                              "chinext" if c.startswith("sz30") else
                              "bse" if c.startswith("bj") else "main")})
        hit.append(det)
    return hit, rej, pool, cross

all_hits = []
for S in mv_dates:
    hit, rej, pool, cross = replay(S, 30.0)
    print(f"{S}: pool={len(pool)} hit={len(hit)}", flush=True)
    all_hits.extend(hit)

base = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "audit_output.json")))["selections"]
base_keys = {(x["code"], x["S"]) for x in base}
new = [x for x in all_hits if (x["code"], x["S"]) not in base_keys]
print("base", len(base), "final30", len(all_hits), "new", len(new))
print("new picks:")
for x in sorted(new, key=lambda z: (z["S"], z["code"])):
    print(x["S"], x["code"], x["name"], "d*", x["d_star"], "age", x["age"],
          "chg_d", round(x["chg_d"], 2), "chg_s", round(x["chg_s"], 2),
          "board", x["board"], "flow", x["flow_ratio"])
json.dump({"final30": all_hits, "new": new, "base": base},
          open(os.path.join(OUT, "final30.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
