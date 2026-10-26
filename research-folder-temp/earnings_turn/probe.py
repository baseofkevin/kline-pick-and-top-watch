#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业绩转变三指标：数据可行性与分布探针（只读，不改选股程序）"""
import importlib.machinery, importlib.util, json, os, time
W='/home/kevin/.local/bin/westock'
HERE=os.path.dirname(os.path.abspath(__file__))
loader=importlib.machinery.SourceFileLoader('wk',W); spec=importlib.util.spec_from_loader('wk',loader)
wk=importlib.util.module_from_spec(spec); loader.exec_module(wk)

CODES=['sh600094','sh600249','sh600318','sh600333','sh600391','sh600435','sh600628','sh600764','sh600814','sh600824','sh600838','sh600863','sh603236','sh603330','sh603578','sh603677','sh603712','sh688007','sh688286','sz000768','sz000972','sz002031','sz002051','sz002076','sz002228','sz002292','sz002315','sz002421','sz002577','sz003017','sz300516','sz300771','sz300879','sz300886','sz300911','sz301069','sz301083','sz301101','sh600732','sh600036']
S='2026-10-23'

def fetch(typ, fields):
    out={}
    for i in range(0,len(CODES),10):
        ch=CODES[i:i+10]
        for tries in range(4):
            try:
                d=wk.call('data_finance',{'codes':','.join(ch),'fields':fields,'num':20,'type':typ})
                data=d.get('data') or {}
                for c,rows in data.items(): out[c]=rows or []
                break
            except Exception as e:
                print('retry',typ,ch[0],e)
                time.sleep(3*(tries+1))
        time.sleep(0.5)
    return out

def pub(r): return str(r.get('InfoPublDate'))[:10] if r.get('InfoPublDate') else None
def ed(r): return str(r.get('EndDate'))[:10] if r.get('EndDate') else None
def f(v):
    try: return float(v)
    except: return None
def pit(rows):
    return [r for r in rows if pub(r) and ed(r) and pub(r)<=S]

def main():
    inc={c:pit(v) for c,v in fetch('income','core').items()}
    bal={c:pit(v) for c,v in fetch('balance','all').items()}
    cf ={c:pit(v) for c,v in fetch('cashflow','all').items()}
    json.dump({'inc':inc,'bal':bal,'cf':cf},open(os.path.join(HERE,'raw.json'),'w'),ensure_ascii=False)
    res={}
    for c in CODES:
        # ---- 单季毛利率 ----
        incs=sorted(inc.get(c,[]),key=lambda r:ed(r))
        by={ed(r):r for r in incs}
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
        gk=sorted(gm)
        g_last=[gm[k] for k in gk[-3:]] if len(gk)>=3 else []
        g_up2 = len(g_last)==3 and g_last[0]<g_last[1]<g_last[2]
        # ---- 合同负债 ----
        bals=sorted(bal.get(c,[]),key=lambda r:ed(r))
        bl={ed(r):r for r in bals}
        bk=sorted(bl)
        cl_series={}
        for dt,r in bl.items():
            cl=(f(r.get('ContractLiability')) or 0)+(f(r.get('AdvancePayment')) or 0)
            cl_series[dt]=cl
        cl_growth=None; cl_ratio=None; cl_prev=None
        if bk:
            t=bk[-1]; y=int(t[:4])
            p=f'{y-1}{t[4:]}'
            if p in cl_series and cl_series[p] and cl_series[p]>0:
                cl_growth=(cl_series[t]/cl_series[p]-1)*100; cl_prev=cl_series[p]
            rev_ttm=f(by[t].get('OperatingRevenueTTM')) if t in by else None
            if rev_ttm and rev_ttm>0: cl_ratio=cl_series[t]/rev_ttm*100
        # ---- 经营现金流 ----
        cfs=sorted(cf.get(c,[]),key=lambda r:ed(r))
        cfd={ed(r):r for r in cfs}
        ck=sorted(cfd)
        ocf_ttm=ocf_prev=ocf_q=np_ttm=None; ocf_turn=None; ocf_ratio=None
        if ck:
            t=ck[-1]; r=cfd[t]
            ocf_ttm=f(r.get('NetOperateCashFlowTTM')); ocf_q=f(r.get('NetOperateCashFlow_Q'))
            py=f'{int(t[:4])-1}{t[4:]}'
            rp=cfd.get(py)
            ocf_prev=f(rp.get('NetOperateCashFlowTTM')) if rp else None
            if t in by: np_ttm=f(by[t].get('NPParentCompanyOwnersTTM'))
            ocf_turn=(ocf_ttm is not None and ocf_prev is not None and ocf_ttm>0 and ocf_prev<0)
            if ocf_ttm is not None and np_ttm not in (None,0) and np_ttm>0:
                ocf_ratio=ocf_ttm/np_ttm
        pass1=g_up2
        pass2=bool(cl_growth is not None and cl_growth>=20)
        pass3=bool((ocf_ratio is not None and ocf_ratio>1) or ocf_turn or (np_ttm is not None and np_ttm<=0 and ocf_ttm is not None and ocf_ttm>0))
        res[c]={'gm_last3':[round(x,2) for x in g_last],'gm_up2':g_up2,
                'latest_inc':gk[-1] if gk else None,
                'cl':cl_series.get(bk[-1]) if bk else None,
                'cl_prev':cl_prev,'cl_growth':round(cl_growth,1) if cl_growth is not None else None,
                'cl_ratio':round(cl_ratio,1) if cl_ratio is not None else None,
                'ocf_ttm':ocf_ttm,'ocf_q':ocf_q,'ocf_prev':ocf_prev,'np_ttm':np_ttm,
                'ocf_turn':ocf_turn,'ocf_ratio':round(ocf_ratio,2) if ocf_ratio is not None else None,
                'p1':pass1,'p2':pass2,'p3':pass3}
    json.dump(res,open(os.path.join(HERE,'metrics.json'),'w'),ensure_ascii=False,indent=1)
    print(f"{'code':10s}{'GM3':>22s}{'①':>3s}{'CLg%':>8s}{'CL/rev%':>9s}{'②':>3s}{'OCF/TTM':>10s}{'OCF/NP':>8s}{'③':>3s}{'ALL':>5s}")
    n1=n2=n3=na=0
    for c in CODES:
        m=res[c]
        allp=m['p1'] and m['p2'] and m['p3']
        n1+=m['p1']; n2+=m['p2']; n3+=m['p3']; na+=allp
        print(f"{c:10s}{str(m['gm_last3']):>22s}{str(m['p1']):>3s}"
              f"{str(m['cl_growth']):>8s}{str(m['cl_ratio']):>9s}{str(m['p2']):>3s}"
              f"{str(round(m['ocf_ttm']/1e8,1) if m['ocf_ttm'] else None):>10s}"
              f"{str(m['ocf_ratio']):>8s}{str(m['p3']):>3s}{str(allp):>5s}")
    print(f'\n样本 {len(CODES)} 只：① {n1} ｜ ② {n2} ｜ ③ {n3} ｜ 三条全过 {na}')

main()
