#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-A 下限研究：固定上限 20.5，比较 ChangePCT 下限 0/0.5/1/1.5/2 的名单。"""
import importlib.util, json, os, re
from collections import Counter

SRC="/home/kevin/MyData/kline-select-stocks-approach"; DB=SRC+"/database"
OUT=os.path.join(os.path.dirname(os.path.abspath(__file__)),"p1a_data")
spec=importlib.util.spec_from_file_location("rule",SRC+"/screen_kline.py")
rule=importlib.util.module_from_spec(spec); spec.loader.exec_module(rule)
km=json.load(open(DB+"/kline_2025-08-05_2026-10-14.json"))
days=json.load(open(DB+"/trading_calendar.json"))["days"]
mv_dates=sorted(os.path.basename(p)[3:-5] for p in __import__("glob").glob(DB+"/mv_????-??-??.json"))
def load(p,d):
    try: return json.load(open(p,encoding="utf-8"))
    except Exception: return d
CACHE={}
def replay(S,lo,hi=20.5):
    key=(S,lo)
    if key in CACHE: return CACHE[key]
    hist=[d for d in days if d<=S]; need=hist[-5:]
    cross={}
    for d in need:
        rows=load(os.path.join(OUT,f"cross0_{d}.json"),[])
        cross[d]={r["code"]:r for r in rows if r.get("code")}
    st={}; new={}
    for d in need:
        lb=load(os.path.join(DB,f"labels_{d}.json"),{"st":[],"new":[]})
        st[d]=set(lb["st"]); new[d]=set(lb["new"])
    pool=sorted({c for d in need for c in cross[d]})
    hit=[]
    for c in pool:
        name=next((cross[d][c].get("name") for d in need if c in cross[d]),"")
        det,why=rule.find_start(c,name,km.get(c,[]),S)
        if det is None: continue
        ok,why=rule.judge(det,km[c],S,4,cross,st,new,rule.K_VOL,rule.MA60_GAP,
                           rule.C2_TOL,rule.C3_RATIO,lo,hi)
        if not ok: continue
        row0=cross[det["d_star"]][c]
        nf=rule.fnum(row0.get("MainNetFlow"))
        mvf=load(os.path.join(DB,f"mv_{S}.json"),{}); fc=mvf.get(c,[None])[0]
        ratio=nf/fc*100 if (nf is not None and fc) else None
        if ratio is None or ratio < rule.FLOW_MIN: continue
        # 前瞻：S+1 收盘买入，持有 5/10/20 交易日，绝对收益
        bars=km[c]; si=next((i for i,b in enumerate(bars) if b[0]==S),None)
        rets={}
        if si is not None:
            for h in (5,10,20):
                j=si+1+h
                if si+1<len(bars) and j<len(bars) and bars[si+1][1]>0:
                    rets[h]=(bars[j][1]/bars[si+1][1]-1)*100
                else: rets[h]=None
        det.update({"S":S,"chg_d":rule.fnum(row0.get("ChangePCT")),
                    "flow_ratio":round(ratio,3),"board":("star" if c.startswith("sh688") else
                    "chinext" if c.startswith("sz30") else "main"),
                    "ret5":rets[5],"ret10":rets[10],"ret20":rets[20]})
        hit.append(det)
    CACHE[key]=hit
    return hit

base=json.load(open("audit_output.json"))["selections"]
base_keys={(x["code"],x["S"]) for x in base}
print("base",len(base))
for lo in (0,0.5,1,1.5,2):
    hits=[]
    for S in mv_dates: hits+=replay(S,lo)
    keys={(x["code"],x["S"]) for x in hits}
    new=[x for x in hits if (x["code"],x["S"]) not in base_keys]
    def med(vals):
        vals=[v for v in vals if v is not None]
        if not vals: return None
        vals=sorted(vals); n=len(vals); return vals[n//2] if n%2 else (vals[n//2-1]+vals[n//2])/2
    print(f"lo={lo:<4} total={len(hits):3} unique={len({x['code'] for x in hits}):3} "
          f"new={len(new):3} new_unique={len({x['code'] for x in new}):3} "
          f"ret5_med={med([x['ret5'] for x in new])} ret10_med={med([x['ret10'] for x in new])} "
          f"ret20_med={med([x['ret20'] for x in new])}")
    json.dump({"lo":lo,"hits":hits,"new":new},open(os.path.join(OUT,f"lower_{lo}.json"),"w"),
              ensure_ascii=False,indent=1)
