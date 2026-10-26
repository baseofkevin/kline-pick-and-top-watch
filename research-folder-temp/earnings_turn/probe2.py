#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业绩转变三指标：数据可行性与分布探针 v2（慢速、可续跑）"""
import importlib.machinery, importlib.util, json, os, time
W='/home/kevin/.local/bin/westock'
HERE=os.path.dirname(os.path.abspath(__file__))
loader=importlib.machinery.SourceFileLoader('wk',W); spec=importlib.util.spec_from_loader('wk',loader)
wk=importlib.util.module_from_spec(spec); loader.exec_module(wk)

CODES=['sh600094','sh600249','sh600318','sh600333','sh600391','sh600435','sh600628','sh600764','sh600814','sh600824','sh600838','sh600863','sh603236','sh603330','sh603578','sh603677','sh603712','sh688007','sh688286','sz000768','sz000972','sz002031','sz002051','sz002076','sz002228','sz002292','sz002315','sz002421','sz002577','sz003017','sz300516','sz300771','sz300879','sz300886','sz300911','sz301069','sz301083','sz301101','sh600732','sh600036']
S='2026-10-23'

def load(name):
    try: return json.load(open(os.path.join(HERE,name)))
    except Exception: return {}

def save(name,obj):
    json.dump(obj,open(os.path.join(HERE,name),'w'),ensure_ascii=False)

def fetch(typ, fields, cache_name):
    out=load(cache_name)
    todo=[c for c in CODES if not out.get(c)]
    for i in range(0,len(todo),5):
        ch=todo[i:i+5]
        for tries in range(8):
            try:
                d=wk.call('data_finance',{'codes':','.join(ch),'fields':fields,'num':20,'type':typ})
                data=d.get('data') or {}
                for c in ch:
                    if data.get(c): out[c]=data[c]
                break
            except Exception as e:
                msg=str(e)
                wait=12 if '限频' in msg else 4
                print(f'  retry {typ} {ch[0]}.. {tries+1}/8 {msg[:60]} sleep {wait}s',flush=True)
                time.sleep(wait)
        missing=[c for c in ch if not out.get(c)]
        if missing:
            print('  still missing',typ,missing,flush=True)
            for c in missing:
                for tries in range(4):
                    try:
                        d=wk.call('data_finance',{'code':c,'fields':fields,'num':20,'type':typ})
                        rows=(d.get('data') or {}).get(c)
                        if rows: out[c]=rows; break
                    except Exception as e:
                        time.sleep(12)
        save(cache_name,out)
        time.sleep(2.0)
    return out

def pub(r): return str(r.get('InfoPublDate'))[:10] if r.get('InfoPublDate') else None
def ed(r): return str(r.get('EndDate'))[:10] if r.get('EndDate') else None
def f(v):
    try: return float(v)
    except: return None
def pit(rows): return [r for r in rows if pub(r) and ed(r) and pub(r)<=S]

def main():
    inc={c:pit(v) for c,v in fetch('income','core','raw_inc.json').items()}
    bal={c:pit(v) for c,v in fetch('balance','all','raw_bal.json').items()}
    cf ={c:pit(v) for c,v in fetch('cashflow','all','raw_cf.json').items()}
    print('coverage inc/bal/cf:',len(inc),len(bal),len(cf),'of',len(CODES))
    res={}
    for c in CODES:
        incs=sorted(inc.get(c,[]),key=lambda r:ed(r)); by={ed(r):r for r in incs}
        gm={}
        for dt,r in by.items():
            rev=f(r.get('OperatingRevenue')); cost=f(r.get('OperatingCost'))
            if rev is None or cost is None or rev<=0: continue
            y,m=int(dt[:4]),int(dt[5:7])
            pq={3:None,6:f'{y}-03-31',9:f'{y}-06-30',12:f'{y}-09-30'}[m]
            if pq is None: rq,cq=rev,cost
            else:
                pr=by.get(pq)
                if not pr: continue
                prv=f(pr.get('OperatingRevenue')); pc=f(pr.get('OperatingCost'))
                if prv is None or pc is None: continue
                rq,cq=rev-prv,cost-pc
            if rq>0: gm[dt]=(rq-cq)/rq*100
        gk=sorted(gm); g_last=[gm[k] for k in gk[-3:]] if len(gk)>=3 else []
        g_up2=len(g_last)==3 and g_last[0]<g_last[1]<g_last[2]
        bals=sorted(bal.get(c,[]),key=lambda r:ed(r)); bl={ed(r):r for r in bals}; bk=sorted(bl)
        cl_series={(dt):((f(r.get('ContractLiability')) or 0)+(f(r.get('AdvancePayment')) or 0)) for dt,r in bl.items()}
        cl_growth=cl_ratio=cl_prev=None
        if bk:
            t=bk[-1]; p=f'{int(t[:4])-1}{t[4:]}'
            if cl_series.get(p) and cl_series[p]>0:
                cl_growth=(cl_series[t]/cl_series[p]-1)*100; cl_prev=cl_series[p]
            rev_ttm=f(by[t].get('OperatingRevenueTTM')) if t in by else None
            if rev_ttm and rev_ttm>0: cl_ratio=cl_series[t]/rev_ttm*100
        cfs=sorted(cf.get(c,[]),key=lambda r:ed(r)); cfd={ed(r):r for r in cfs}; ck=sorted(cfd)
        ocf_ttm=ocf_q=np_ttm=None; ocf_turn=False; ocf_ratio=None; latest_cf=ck[-1] if ck else None
        if ck:
            t=ck[-1]; r=cfd[t]
            ocf_ttm=f(r.get('NetOperateCashFlowTTM')); ocf_q=f(r.get('NetOperateCashFlow_Q'))
            rp=cfd.get(f'{int(t[:4])-1}{t[4:]}')
            ocf_prev=f(rp.get('NetOperateCashFlowTTM')) if rp else None
            if t in by: np_ttm=f(by[t].get('NPParentCompanyOwnersTTM'))
            ocf_turn=(ocf_ttm is not None and ocf_prev is not None and ocf_ttm>0 and ocf_prev<0)
            if ocf_ttm is not None and np_ttm not in (None,0) and np_ttm>0: ocf_ratio=ocf_ttm/np_ttm
        p1=g_up2
        p2=bool(cl_growth is not None and cl_growth>=20)
        p3=bool((ocf_ratio is not None and ocf_ratio>1) or ocf_turn or (np_ttm is not None and np_ttm<=0 and ocf_ttm is not None and ocf_ttm>0))
        res[c]={'gm_last3':[round(x,2) for x in g_last],'gm_up2':g_up2,'latest_inc':gk[-1] if gk else None,
                'latest_bal':bk[-1] if bk else None,'latest_cf':latest_cf,
                'cl':cl_series.get(bk[-1]) if bk else None,'cl_prev':cl_prev,
                'cl_growth':round(cl_growth,1) if cl_growth is not None else None,
                'cl_ratio':round(cl_ratio,1) if cl_ratio is not None else None,
                'ocf_ttm':ocf_ttm,'ocf_q':ocf_q,'ocf_prev':ocf_prev,'np_ttm':np_ttm,
                'ocf_turn':ocf_turn,'ocf_ratio':round(ocf_ratio,2) if ocf_ratio is not None else None,
                'p1':p1,'p2':p2,'p3':p3}
    save('metrics2.json',res)
    print(f"{'code':10s}{'GM3':>24s}{'①':>7s}{'CLg%':>8s}{'CL/rev':>8s}{'②':>6s}{'OCF亿':>8s}{'OCF/NP':>8s}{'③':>6s}{'ALL':>6s}")
    n1=n2=n3=na=0
    for c in CODES:
        m=res[c]; allp=m['p1'] and m['p2'] and m['p3']; n1+=m['p1']; n2+=m['p2']; n3+=m['p3']; na+=allp
        print(f"{c:10s}{str(m['gm_last3']):>24s}{str(m['p1']):>7s}{str(m['cl_growth']):>8s}{str(m['cl_ratio']):>8s}{str(m['p2']):>6s}"
              f"{str(round(m['ocf_ttm']/1e8,1) if m['ocf_ttm'] is not None else None):>8s}{str(m['ocf_ratio']):>8s}{str(m['p3']):>6s}{str(allp):>6s}")
    print(f'\n样本 {len(CODES)} 只：① {n1} ｜ ② {n2} ｜ ③ {n3} ｜ 三条全过 {na}')

main()
