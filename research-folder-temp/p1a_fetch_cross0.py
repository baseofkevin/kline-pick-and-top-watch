#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-A 下限研究：拉 91 天 ChangePCT>=0、<=20.5 的横截面。"""
import json, os, subprocess, time
W=os.path.expanduser("~/.local/bin/westock")
DB="/home/kevin/MyData/kline-select-stocks-approach/database"
OUT=os.path.join(os.path.dirname(os.path.abspath(__file__)),"p1a_data")
EXPR=("intersect([ChangePCT >= 0, ChangePCT <= 20.5, MainNetFlow > 0, "
      "MainNetFlow5D > 0, IsDelisted == 0, Ifsuspend == 0])")
days=json.load(open(os.path.join(DB,"trading_calendar.json")))["days"]
days=[d for d in days if "2026-06-01"<=d<="2026-10-14"]
def md(txt):
    tbl=[l for l in txt.splitlines() if l.strip().startswith("|")]
    if len(tbl)<2: return []
    hdr=[x.strip() for x in tbl[0].strip().strip("|").split("|")]
    out=[]
    for ln in tbl[1:]:
        c=[x.strip() for x in ln.strip().strip("|").split("|")]
        if len(c)==len(hdr) and not all(set(v)<=set("-: ") for v in c):
            out.append(dict(zip(hdr,c)))
    return out
for i,d in enumerate(days,1):
    p=os.path.join(OUT,f"cross0_{d}.json")
    if os.path.exists(p):
        print(f"{i}/{len(days)} {d} cached",flush=True); continue
    rows=[]
    for attempt in range(3):
        q=subprocess.run([W,"screen","condition","--expression",EXPR,
                          "--date",d,"--limit","6000"],capture_output=True,timeout=300)
        rows=md(q.stdout.decode("utf-8","replace"))
        if rows: break
        time.sleep(2)
    if not rows:
        print(f"{i}/{len(days)} {d} EMPTY",flush=True); continue
    json.dump(rows,open(p,"w",encoding="utf-8"),ensure_ascii=False)
    print(f"{i}/{len(days)} {d} {len(rows)}",flush=True)
    time.sleep(.4)
print("done",flush=True)
