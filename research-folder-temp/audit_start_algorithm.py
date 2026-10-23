#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对 screen_kline.py 的「刚启动」定义做离线复核（只读源项目 database/，只写本目录）。

它回答四个问题：
  1. 按现行代码回放 18 个周频基准日，能选出多少只？（用于确认实现与文档一致）
  2. 这些入选股在结构上像不像「刚启动」？（MA20/MA60、MA60 斜率、20 日新高、箱体振幅）
  3. 「涨幅 ≤ 9.5%」这道服务端条件会砍掉多少放量上穿事件？其中多少是主板涨停附近？
  4. 以基准日次日收盘买入，5/10/20 日后的绝对收益与横截面超额收益如何？

⚠️ 口径与边界（不要把这当成收益证明）：
  - K 线只有 (date, close, volume)，没有 open/high/low/amount；无法计算收盘位置、ATR、成交额。
  - 全市场 K 线截止 2026-10-14；20 日收益样本只到 2026-09-11 左右。
  - 收益为 close-to-close，未扣交易成本、印花税、滑点，也未处理涨停买不进。
  - 样本只有 91 个交易日、单一市场状态；所有结论都是结构性的，不是 alpha 证明。

用法：
    python3 audit_start_algorithm.py
    python3 audit_start_algorithm.py --src /home/kevin/MyData/kline-select-stocks-approach
"""
import argparse
import glob
import importlib.util
import json
import os
import statistics as st
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = "/home/kevin/MyData/kline-select-stocks-approach"


def load_rule(src):
    p = os.path.join(src, "screen_kline.py")
    spec = importlib.util.spec_from_file_location("screen_kline_rule", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def jload(path, default=None):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def ma(bars, n, i):
    if i < n - 1 or i >= len(bars):
        return None
    return sum(x[1] for x in bars[i - n + 1:i + 1]) / n


def build_mas(km):
    ma20, ma60 = {}, {}
    for c, bars in km.items():
        ma20[c] = [ma(bars, 20, i) for i in range(len(bars))]
        ma60[c] = [ma(bars, 60, i) for i in range(len(bars))]
    return ma20, ma60


def replay(src, rule, km, cal, verbose=True):
    """按 main() 的口径回放 18 个周频基准日；返回入选明细与逐周统计。"""
    db = os.path.join(src, "database")
    cross, labels = {}, {}
    for d in cal:
        p = os.path.join(db, f"cross_{d}.json")
        if os.path.exists(p):
            cross[d] = {r["code"]: r for r in jload(p, []) if r.get("code")}
        p = os.path.join(db, f"labels_{d}.json")
        if os.path.exists(p):
            z = jload(p, {"st": [], "new": []})
            labels[d] = {"st": set(z["st"]), "new": set(z["new"])}

    mv_dates = sorted(os.path.basename(x)[3:-5]
                      for x in glob.glob(os.path.join(db, "mv_*.json")))
    selections, per_week = [], []
    for S in mv_dates:
        hist = [d for d in cal if d <= S]
        if len(hist) < 5:
            continue
        need = hist[-5:]
        if not all(d in cross and d in labels for d in need):
            continue
        mv = jload(os.path.join(db, f"mv_{S}.json"), {})
        pool = sorted({c for d in need for c in cross[d]})
        hit = 0
        for c in pool:
            name = next((cross[d][c].get("name") for d in need if c in cross[d]), "")
            det, _ = rule.find_start(c, name, km.get(c, []), S)
            if det is None:
                continue
            ok, _ = rule.judge(
                det, km[c], S, 4, cross,
                {d: labels[d]["st"] for d in need},
                {d: labels[d]["new"] for d in need},
                rule.K_VOL, rule.MA60_GAP, rule.C2_TOL, rule.C3_RATIO,
                2.0, 9.5)  # 固定审计时 v2.5 口径，避免被 v2.7 默认参数影响
            if not ok:
                continue
            row = cross[det["d_star"]][c]
            try:
                nf = float(row["MainNetFlow"])
                fc = float(mv[c][0])
            except (KeyError, TypeError, ValueError):
                continue
            if not fc or nf / fc * 100 < rule.FLOW_MIN:
                continue
            det["S"] = S
            det["flow_ratio"] = nf / fc * 100
            det["chg"] = float(row.get("ChangePCT") or "nan")
            det.pop("_ci", None)
            det.pop("_i", None)
            selections.append(det)
            hit += 1
        per_week.append((S, len(pool), hit))
        if verbose:
            print(f"  {S}  池 {len(pool):4d}  入选 {hit:2d}")
    return selections, per_week


def profile(src, selections, km):
    """入选股在 d* 与 S 的结构画像。"""
    idx = {c: {b[i][0]: i for i in range(len(b))} for c, b in km.items()}
    ma20, ma60 = build_mas(km)
    out = {k: 0 for k in (
        "n", "m20_lt_m60", "m60_falling20", "trend_ok", "new20_high",
        "range20_le12", "ext_gt10", "not_new20")}
    ranges, ratios, exts = [], [], []
    for x in selections:
        c, b = x["code"], km[x["code"]]
        i, si = idx[c][x["d_star"]], idx[c][x["S"]]
        m20, m60 = ma20[c][i], ma60[c][i]
        m60p = ma60[c][i - 20] if i >= 20 else None
        prior = [z[1] for z in b[i - 20:i]]
        hi = max(prior)
        rg = (max(prior) - min(prior)) / min(prior)
        ext = b[si][1] / ma20[c][si] - 1
        out["n"] += 1
        out["m20_lt_m60"] += m20 < m60
        out["m60_falling20"] += bool(m60p and m60 <= m60p)
        out["trend_ok"] += bool(m60p and m20 > m60 and m60 > m60p)
        out["new20_high"] += b[i][1] > hi
        out["not_new20"] += b[i][1] <= hi
        out["range20_le12"] += rg <= 0.12
        out["ext_gt10"] += ext > 0.10
        ranges.append(rg)
        ratios.append(m20 / m60)
        exts.append(ext)
    def q(v, p):
        v = sorted(v)
        return v[min(len(v) - 1, int(len(v) * p))]
    out["prior20_range_pct"] = [round(q(ranges, p) * 100, 2) for p in (.25, .5, .75, .9)]
    out["m20_over_m60"] = [round(q(ratios, p), 4) for p in (.05, .25, .5, .75, .95)]
    out["ext_at_S_pct"] = [round(q(exts, p) * 100, 2) for p in (.25, .5, .75, .9, .95)]
    out["ext_max_pct"] = round(max(exts) * 100, 2)
    return out


def event_profile(km):
    """全市场「放量上穿 MA20 + 现行结构条件」事件的结构画像（不是入选股）。"""
    ma20, ma60 = build_mas(km)
    n = 0
    m20_lt_m60 = m60_falling = not_new20 = wide20 = maxvol_other = 0
    for c, b in km.items():
        for t in range(70, len(b)):
            if ma20[c][t] is None or ma20[c][t - 1] is None or ma60[c][t] is None:
                continue
            cl, vo = b[t][1], b[t][2]
            if not (cl > ma20[c][t] and b[t - 1][1] <= ma20[c][t - 1]):
                continue
            prev = [b[k][2] for k in range(t - 5, t)]
            if len(prev) < 5 or sum(prev) <= 0:
                continue
            chg = cl / b[t - 1][1] - 1
            if not (2 <= chg * 100 <= 9.5):
                continue
            if vo / (sum(prev) / 5) < 1.75:
                continue
            if ma20[c][t] < ma60[c][t] * 0.98:
                continue
            m20p5 = ma(b, 20, t - 5)
            if m20p5 is not None and ma20[c][t] <= m20p5 * 0.99:
                continue
            prior = [x[1] for x in b[t - 20:t]]
            if len(prior) < 20:
                continue
            n += 1
            m20_lt_m60 += ma20[c][t] < ma60[c][t]
            m60p = ma(b, 60, t - 20)
            m60_falling += bool(m60p and ma60[c][t] < m60p)
            not_new20 += cl <= max(prior)
            wide20 += (max(prior) - min(prior)) / min(prior) > 0.15
            ks = [k for k in range(t - 2, min(t + 3, len(b))) if b[k]]
            maxvol_other += bool(ks) and max(ks, key=lambda k: b[k][2]) != t
    return {"n": n,
            "m20_lt_m60_pct": round(m20_lt_m60 / n * 100, 1),
            "m60_falling20_pct": round(m60_falling / n * 100, 1),
            "not_new20_high_pct": round(not_new20 / n * 100, 1),
            "prior20_range_gt15_pct": round(wide20 / n * 100, 1),
            "max_vol_not_on_dstar_pct": round(maxvol_other / n * 100, 1)}


def cap_exclusion(km):
    """全市场 91 日：放量上穿 MA20 的事件里，有多少被 9.5% 上限砍掉。"""
    ma20, ma60 = build_mas(km)
    n_cross = n_vol = n_band = n_struct = 0
    n_ge95 = n_95_105 = n_ge105 = n_lt2 = 0
    for c, b in km.items():
        for t in range(70, len(b)):
            if t < 1 or ma20[c][t] is None or ma20[c][t - 1] is None or ma60[c][t] is None:
                continue
            cl, vo = b[t][1], b[t][2]
            if not (cl > ma20[c][t] and b[t - 1][1] <= ma20[c][t - 1]):
                continue
            prev = [b[k][2] for k in range(t - 5, t)]
            if len(prev) < 5 or sum(prev) <= 0:
                continue
            chg = cl / b[t - 1][1] - 1
            vr = vo / (sum(prev) / 5)
            struct = ma20[c][t] >= ma60[c][t] * 0.98
            n_cross += 1
            if vr < 1.75:
                continue
            n_vol += 1
            if struct:
                n_struct += 1
                if chg * 100 >= 9.5:
                    n_ge95 += 1
                    if chg * 100 < 10.5:
                        n_95_105 += 1
                    else:
                        n_ge105 += 1
                if chg * 100 < 2:
                    n_lt2 += 1
            if 2 <= chg * 100 <= 9.5:
                n_band += 1
    return {
        "ma20_cross": n_cross,
        "vol_ge_1.75": n_vol,
        "vol_and_chg_2_9.5": n_band,
        "vol_and_struct": n_struct,
        "struct_chg_ge_9.5": n_ge95,
        "struct_chg_9.5_10.5": n_95_105,
        "struct_chg_ge_10.5": n_ge105,
        "struct_chg_lt_2": n_lt2,
        "pct_of_vol_struct_excluded": round(n_ge95 / n_struct * 100, 1) if n_struct else None,
    }


def forward(src, selections, km):
    """S+1 收盘买入，5/10/20 日 close-to-close；基准=同日全市场横截面中位数。"""
    idx = {c: {b[i][0]: i for i in range(len(b))} for c, b in km.items()}
    alldates = sorted({b[i][0] for b in km.values() for i in range(len(b))})
    pos = {d: i for i, d in enumerate(alldates)}

    def next_date(d):
        for x in alldates:
            if x > d:
                return x
        return None

    def ret(c, d, h):
        nxt = next_date(d)
        if not nxt or nxt not in idx.get(c, {}):
            return None
        i = idx[c][nxt]
        b = km[c]
        return b[i + h][1] / b[i][1] - 1 if i + h < len(b) else None

    def bench(d, h):
        nxt = next_date(d)
        if not nxt:
            return None
        rs = []
        for c, b in km.items():
            if nxt in idx[c]:
                i = idx[c][nxt]
                if i + h < len(b):
                    rs.append(b[i + h][1] / b[i][1] - 1)
        return st.median(rs) if rs else None

    out = {}
    for h in (5, 10, 20):
        ex, ab = [], []
        for x in selections:
            r, bm = ret(x["code"], x["S"], h), bench(x["S"], h)
            if r is not None and bm is not None:
                ab.append(r)
                ex.append(r - bm)
        if ex:
            out[h] = {
                "n": len(ex),
                "abs_median_pct": round(st.median(ab) * 100, 2),
                "excess_median_pct": round(st.median(ex) * 100, 2),
                "excess_mean_pct": round(st.mean(ex) * 100, 2),
                "win_rate_pct": round(100 * sum(1 for z in ex if z > 0) / len(ex), 1),
            }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--out", default=os.path.join(HERE, "audit_output.json"))
    a = ap.parse_args()
    src = os.path.abspath(a.src)
    db = os.path.join(src, "database")
    if not os.path.exists(os.path.join(src, "screen_kline.py")):
        print(f"找不到源项目：{src}", file=sys.stderr)
        return 1
    rule = load_rule(src)
    cal = jload(os.path.join(db, "trading_calendar.json"), {}).get("days") or []
    km = jload(os.path.join(db, "kline_2025-08-05_2026-10-14.json"), {})
    if not cal or not km:
        print("缺少 trading_calendar.json 或 kline_2025-08-05_2026-10-14.json", file=sys.stderr)
        return 1
    print(f"[1/3] 回放 18 个周频基准日（{cal[0]} ~ {cal[-1]}）...")
    sels, per_week = replay(src, rule, km, cal)
    print(f"      合计入选 {len(sels)} 只，去重 {len({x['code'] for x in sels})} 只")
    print("[2/3] 结构画像 ...")
    prof = profile(src, sels, km)
    print("[3/4] 全市场事件画像 ...")
    evp = event_profile(km)
    print("[4/4] 9.5% 上限与前瞻收益 ...")
    cap = cap_exclusion(km)
    fwd = forward(src, sels, km)
    out = {"selected": len(sels), "unique": len({x['code'] for x in sels}),
           "per_week": per_week, "profile": prof, "event_profile": evp,
           "cap_exclusion": cap, "forward": fwd, "selections": sels}
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n=== 结构画像 ===")
    for k, v in prof.items():
        print(f"  {k}: {v}")
    print("\n=== 全市场事件画像（放量上穿+结构）===")
    for k, v in evp.items():
        print(f"  {k}: {v}")
    print("\n=== 9.5% 上限 ===")
    for k, v in cap.items():
        print(f"  {k}: {v}")
    print("\n=== 前瞻收益（S+1 收盘买入）===")
    for h, v in fwd.items():
        print(f"  {h}日: {v}")
    print(f"\n明细已写入 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
