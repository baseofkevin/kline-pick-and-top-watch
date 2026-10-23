#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-A：统计 9.5% 上限漏掉的事件/最终名单，以及其中封死 vs 可买。"""
import json, os, sys
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP

SRC = "/home/kevin/MyData/kline-select-stocks-approach"
DB = os.path.join(SRC, "database")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "p1a_data")
km = json.load(open(os.path.join(DB, "kline_2025-08-05_2026-10-14.json")))
ohlc = json.load(open(os.path.join(OUT, "ohlc_2026-05-20_2026-10-14.json")))

def ma(v,n,i): return sum(v[i-n+1:i+1])/n if i>=n-1 else None

def board(c):
    if c.startswith("sh688"): return "star", 0.20
    if c.startswith("sz30"): return "chinext", 0.20
    if c.startswith("bj"): return "bse", 0.30
    return "main", 0.10

def limit_price(prev, pct):
    # A 股涨跌停价：昨收 × (1+幅度)，四舍五入到分
    return float((Decimal(str(prev)) * (Decimal(1) + Decimal(str(pct)))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def state_at(c, day):
    """返回 (状态, 是否收盘可买, 是否一字, 收盘距涨停%)"""
    bars = ohlc.get(c)
    if not bars: return None
    idx = next((i for i,b in enumerate(bars) if b[0] == day), None)
    if idx is None or idx == 0: return None
    b = bars[idx]
    prev = bars[idx-1][2]  # close
    _, pct = board(c)
    lim = limit_price(prev, pct)
    o, cl, hi, lo = b[1], b[2], b[3], b[4]
    eps = 0.005
    one = (abs(hi-lim) <= eps and abs(lo-lim) <= eps)
    if one:
        st = "一字板"
    elif hi < lim - eps:
        st = "未触板"
    elif cl >= lim - eps:
        st = "收盘封死"
    else:
        st = "触板未封"
    near = (lim - cl) / lim * 100 if lim else None
    return st, (cl < lim - eps), one, near

def next_day(c, day):
    bars = ohlc.get(c)
    if not bars: return None
    idx = next((i for i,b in enumerate(bars) if b[0] == day), None)
    if idx is None or idx+1 >= len(bars): return None
    b = bars[idx+1]; prev = bars[idx][2]
    _, pct = board(c)
    lim = limit_price(prev, pct)
    o, cl, hi, lo = b[1], b[2], b[3], b[4]
    eps = 0.005
    one = (abs(hi-lim) <= eps and abs(lo-lim) <= eps)
    sealed = (cl >= lim - eps)
    open_limit = (o >= lim - eps)
    return {"date": b[0], "state": "一字板" if one else ("收盘封死" if sealed else ("触板未封" if hi >= lim-eps else "未触板")),
            "one_word": one, "open_limit": open_limit, "buyable_close": cl < lim-eps,
            "next_limit": lim, "next_open": o, "next_close": cl}

# 重算事件宇宙
events=[]
for c,b in km.items():
    if len(b)<80: continue
    cl=[x[1] for x in b]; vo=[x[2] for x in b]
    for i in range(70,len(b)):
        m20=ma(cl,20,i); m20p=ma(cl,20,i-1); m60=ma(cl,60,i)
        if not m20 or not m20p or not m60: continue
        if not (cl[i-1] <= m20p and cl[i] > m20): continue
        if m20 < m60*0.98: continue
        base=sum(vo[i-5:i])/5
        if not base or vo[i]/base < 1.75: continue
        chg=(cl[i]/cl[i-1]-1)*100
        if b[i][0] >= "2026-06-01":
            events.append((c,b[i][0],chg))

def summarize(name, items):
    print(f"\n== {name} n={len(items)} ==")
    print("boards", Counter(board(c)[0] for c,_,_ in items))
    st=Counter(); buy=0; no=0
    for c,d,chg in items:
        r=state_at(c,d)
        if r is None: no+=1; continue
        st[r[0]]+=1; buy += 1 if r[1] else 0
    print("d* states", st, "d* close buyable", buy, "no OHLC", no)
    # next day
    ns=Counter(); nb=0; no2=0; one2=0
    for c,d,chg in items:
        r=next_day(c,d)
        if r is None: no2+=1; continue
        ns[r["state"]]+=1
        if r["one_word"]: one2+=1
        elif r["buyable_close"]: nb+=1
    print("d*+1 states", ns, "d*+1 one-word", one2, "d*+1 close buyable", nb, "no next", no2)

excluded=[e for e in events if e[2]>=9.5]
summarize("被9.5%排除的事件宇宙", excluded)

d=json.load(open(os.path.join(OUT,"final30.json")))
new=[(x["code"],x["S"],x["chg_d"]) for x in d["new"]]
base=[(x["code"],x["S"],x.get("chg",0)) for x in d["base"]]
summarize("放宽到30%后新增的最终名单", new)
summarize("原9.5%基线最终名单（对照）", base)

# 最终名单 S 日状态（更贴近“出名单时能不能买”）
print("\n== 最终名单 S 日状态 ==")
for label, items in [("new",new),("base",base)]:
    st=Counter(); one=Counter(); near=[]
    for c,S,chg in items:
        r=state_at(c,S)
        if r is None: st["无OHLC"]+=1; continue
        st[r[0]]+=1
        if r[2]: one["one_word"]+=1
        if r[3] is not None and r[3] <= 0.5: near.append((c,S,r[0],round(r[3],3)))
    print(label, "S states", st, "one-word", one, "near<=0.5%", len(near))
    print("  near examples", near[:10])
