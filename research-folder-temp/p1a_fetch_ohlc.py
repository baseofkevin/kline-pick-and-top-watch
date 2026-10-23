#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-A 研究：只拉「被 9.5% 排除的事件股 + 新最终名单股」的 OHLC。"""
import json, os, subprocess, sys, time
W = os.path.expanduser("~/.local/bin/westock")
SRC = "/home/kevin/MyData/kline-select-stocks-approach"
DB = os.path.join(SRC, "database")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "p1a_data")
km = json.load(open(os.path.join(DB, "kline_2025-08-05_2026-10-14.json")))
def ma(v,n,i): return sum(v[i-n+1:i+1])/n if i>=n-1 else None
excluded=set()
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
        if (cl[i]/cl[i-1]-1)*100 >= 9.5: excluded.add(c)
d=json.load(open(os.path.join(OUT,'final30.json')))
codes=excluded|{x['code'] for x in d['new']}|{x['code'] for x in d['base']}
codes=sorted(codes)
print('codes to fetch',len(codes),'excluded',len(excluded),flush=True)
path=os.path.join(OUT,'ohlc_2026-05-20_2026-10-14.json')
out=json.load(open(path)) if os.path.exists(path) else {}
todo=[c for c in codes if c not in out]
batch=20
for n in range(0,len(todo),batch):
    ch=todo[n:n+batch]
    rows=None
    for attempt in range(3):
        p=subprocess.run([W,'kline',','.join(ch),'--start','2026-05-20',
                          '--end','2026-10-14','--limit','400'],
                         capture_output=True,timeout=300)
        txt=p.stdout.decode('utf-8','replace')
        tbl=[l for l in txt.splitlines() if l.strip().startswith('|')]
        if len(tbl)>=2:
            hdr=[x.strip() for x in tbl[0].strip().strip('|').split('|')]
            rows=[]
            for ln in tbl[1:]:
                c=[x.strip() for x in ln.strip().strip('|').split('|')]
                if len(c)==len(hdr) and not all(set(v)<=set('-: ') for v in c):
                    rows.append(dict(zip(hdr,c)))
            if rows: break
        time.sleep(3)
    if not rows:
        print('FAIL batch',n//batch+1,ch[:3],flush=True); continue
    got=set()
    for r in rows:
        c=r.get('code') or (ch[0] if len(ch)==1 else None)
        if not c: continue
        try:
            bar=[r['date'],float(r['open']),float(r['last']),float(r['high']),
                 float(r['low']),float(r['volume']),float(r['amount'])]
        except (KeyError,ValueError):
            continue
        out.setdefault(c,[]).append(bar); got.add(c)
    for c in got: out[c].sort(key=lambda b:b[0])
    json.dump(out,open(path,'w',encoding='utf-8'),ensure_ascii=False)
    print(f'{min(n+batch,len(todo))}/{len(todo)} batches, codes {len(out)}',flush=True)
    time.sleep(0.4)
print('done codes',len(out),'missing',len([c for c in codes if c not in out]),flush=True)
