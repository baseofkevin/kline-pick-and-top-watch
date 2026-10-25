#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
judge_top.py —— 单只股票「四因子独立观察」（不合并总分，不筛选）

用法：
    python3 judge_top.py sh600519
    python3 judge_top.py 600519 --date 2026-10-23
    python3 judge_top.py sz300502 --json
    python3 judge_top.py sh600519 --refresh
    python3 judge_top.py sh600519 --no-html

它只做一件事：
    把动量、换手、业绩、量价四个因子分别计算、分别展示，
    不把四个因子合成一个总分，也不自动给出“是不是顶部”的结论。

四个因子：
    ① 动量（价格）：RET5 / RET20 / RET60 / 距60日高点。
       只描述涨势强弱和位置，不判断顶部。
    ② 换手（交易）：20日均换手、自身250日分位、1月/1年换手比。
       只描述交易是否拥挤，不单独判断顶部。
    ③ 业绩（背景）：单季归母净利润同比、环比及其相对加速度。
       相对加速度 = (本季增速 - 上季增速) / |上季增速|。
       只描述基本面动能，不作为顶部因子；同时展示原始季度数据。
       净利润和营业收入都使用单季口径（不是累计值）。
    ④ 量价（观察）：5日/20日量比、5日振幅、5日涨跌、距60日高点。
       “放量盘整”只作观察，不单独触发。

输出原则：
    · 每张卡片只描述自己的维度；
    · 不输出总分、不输出综合顶部结论；
    · 数据不足时显示“数据不足，不判断”；
    · 原始数据、计算口径和结果同时展示，方便使用者自行判断。

数据：
    · 日 K：data_kline，前复权，向前取 3 年；
    · 财务：data_finance，fields=all，num=60，按 InfoPublDate 过滤；
    · 财务加速度口径为单季归母净利润，不是营业收入。

⚠️ 边界：
    本程序不预测精确最高点，也不构成买卖指令。
"""

import argparse
import datetime as dt
import html
import importlib.machinery
import importlib.util
import json
import os
import statistics as st
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "database")
OUT_DIR = os.path.join(HERE, "judge_top_output")
CACHE_VERSION = 2  # 财务从 20 期改 60 期后，旧缓存需要失效
W = os.path.expanduser("~/.local/bin/westock")

# 执行过程默认打到 stdout；--json 时改到 stderr，保证 stdout 只有 JSON。
LOG_STREAM = sys.stdout
QUIET = False


def log(msg):
    if not QUIET:
        print(msg, file=LOG_STREAM, flush=True)

# ---------------- 权重（合计 100%） ----------------
W_RET5 = 0.05
W_RET20 = 0.20
W_RET60 = 0.12
W_POS250 = 0.08
W_AVG20 = 0.20
W_STD20 = 0.08
W_RATIO = 0.12
W_ACCEL = 0.15
FIN_NUM = 60  # 20 期只能覆盖约 5 年，历史分析会缺上一季

WEIGHTS = {
    "RET5": W_RET5, "RET20": W_RET20, "RET60": W_RET60, "POS250": W_POS250,
    "AVG20": W_AVG20, "STD20": W_STD20, "RATIO": W_RATIO, "ACCEL": W_ACCEL,
}

# 绝对阈值（仅历史样本 <60 个时回退；正常评分用自身 250 日分位）
SCORE_TABLE = {
    "RET5":  [(0.20, 100), (0.12, 85), (0.08, 65), (0.04, 45), (0.00, 30)],
    "RET20": [(0.50, 100), (0.30, 85), (0.20, 70), (0.10, 50), (0.00, 30)],
    "RET60": [(1.00, 100), (0.60, 85), (0.40, 70), (0.20, 50), (0.00, 30)],
    "POS250": [(-0.005, 100), (-0.02, 85), (-0.05, 70), (-0.10, 50), (-0.20, 30)],
    "AVG20": [(15.0, 100), (10.0, 85), (8.0, 70), (5.0, 50), (3.0, 35)],
    "STD20": [(5.0, 100), (4.0, 85), (3.0, 70), (2.0, 50), (1.0, 30)],
    "RATIO": [(2.5, 100), (2.0, 85), (1.5, 70), (1.2, 50), (1.0, 35)],
}

# 相对分位对应的标签（P95/P90/P75/P50/P25/更低）
PCT_LABELS = {
    "RET5": ["短线极热", "短线很热", "短线偏热", "短线中性", "短线偏冷", "短线冷"],
    "RET20": ["20日极热", "20日很热", "20日偏热", "20日中性", "20日偏冷", "20日冷"],
    "RET60": ["60日极热", "60日很热", "60日偏热", "60日中性", "60日偏冷", "60日冷"],
    "POS250": ["贴近250日高点", "距高点很近", "高位", "中位",
               "偏低", "深度回撤"],
    "AVG20": ["换手极高", "换手很高", "换手偏高", "换手中等",
              "换手偏低", "换手低"],
    "STD20": ["换手极不稳定", "换手很不稳定", "换手不稳定", "换手中等",
              "换手较稳", "换手稳定"],
    "RATIO": ["换手显著放大", "换手明显放大", "换手放大", "换手中等",
              "换手收缩", "换手明显收缩"],
}

LABELS = {
    "RET5": ["短线极热", "短线过热", "短线偏热", "短线偏强", "短线平", "短线弱"],
    "RET20": ["20日极热", "20日过热", "20日偏热", "20日偏强", "20日平", "20日弱"],
    "RET60": ["60日极热", "60日过热", "60日偏热", "60日偏强", "60日平", "60日弱"],
    "POS250": ["贴近250日高点", "距高点很近", "高位", "高位小幅回落",
               "明显回落", "深度回落"],
    "AVG20": ["换手极高", "换手很高", "换手偏高", "换手偏高", "换手正常", "换手低"],
    "STD20": ["换手极不稳定", "换手很不稳定", "换手不稳定", "换手偏乱",
              "换手较稳", "换手稳定"],
    "RATIO": ["换手显著放大", "换手明显放大", "换手放大", "换手略放大",
              "换手平稳", "换手收缩"],
    "ACCEL": ["业绩大幅减速", "业绩明显减速", "业绩减速", "业绩平稳",
              "业绩加速", "业绩强加速"],
}


# ---------------- 数据访问 ----------------

def westock_module():
    loader = importlib.machinery.SourceFileLoader("westock_cli", W)
    spec = importlib.util.spec_from_loader("westock_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    if not hasattr(mod, "call"):
        raise RuntimeError("当前 westock CLI 不支持模块调用")
    return mod


def call(wk, tool, args, tries=4):
    for i in range(tries):
        try:
            return wk.call(tool, args)
        except Exception:
            if i < tries - 1:
                time.sleep(1.2 * (i + 1))
    return None


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pdate(v):
    return str(v)[:10] if v else None


def _nodes(d):
    """兼容 data_kline 单码（d['nodes']）与多码（d['data'] 列表）两种返回。"""
    if not isinstance(d, dict):
        return []
    if isinstance(d.get("nodes"), list):
        return d["nodes"]
    data = d.get("data")
    if isinstance(data, dict):
        return data.get("nodes") or []
    if isinstance(data, list):
        out = []
        for blk in data:
            out.extend((blk.get("data") or {}).get("nodes") or [])
        return out
    return []


def fetch_kline(wk, code, end, refresh):
    """向前取 3 年日 K（qfq），按日期去重。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    cache = os.path.join(OUT_DIR, f"top_{code}_{end}.json")
    if os.path.exists(cache) and not refresh:
        try:
            obj = json.load(open(cache, encoding="utf-8"))
            if (obj.get("v") == CACHE_VERSION
                    and obj.get("kline")
                    and obj.get("finance") is not None):
                log(f"[数据] 缓存命中：{cache}")
                log(f"[数据] 日K {len(obj['kline'])} 根；财务 {len(obj['finance'])} 期")
                return obj["kline"], obj["finance"]
        except Exception:
            pass

    log(f"[数据] 拉取日K：{code}，向前 3 年，分 3 段")
    ed = dt.date.fromisoformat(end)
    merged = {}
    for i in range(3):
        ce = ed - dt.timedelta(days=365 * i)
        cs = ce - dt.timedelta(days=365)
        d = call(wk, "data_kline", {
            "code": code, "start": cs.isoformat(), "end": ce.isoformat(),
            "period": "day", "limit": 300, "fq": "qfq"})
        if d is None:
            sys.stderr.write(f"  [告警] K线第 {i + 1} 段获取失败\n")
            continue
        for x in _nodes(d):
            if x.get("date"):
                merged[x["date"]] = x
        time.sleep(0.2)

    if merged:
        ks = sorted(merged)
        log(f"[数据] 日K完成：{len(ks)} 根（{ks[0]} ~ {ks[-1]}）")
    else:
        log("[数据] 日K完成：0 根（接口未返回数据）")

    log(f"[数据] 拉取财务：{code}（最多 {FIN_NUM} 期）")
    fin = call(wk, "data_finance", {
        "code": code, "fields": "all", "num": FIN_NUM, "type": "income"})
    rows = []
    if isinstance(fin, dict):
        data = fin.get("data") or {}
        if isinstance(data, dict):
            rows = data.get(code) or []

    obj = {"v": CACHE_VERSION, "code": code, "end": end,
           "kline": sorted(merged.values(), key=lambda x: x["date"]),
           "finance": rows}
    if isinstance(fin, dict):
        log(f"[数据] 财务完成：{len(rows)} 期")
    else:
        log("[数据] 财务获取失败：0 期")
    # 财务调用失败时不要把空结果写进缓存，否则下次会误用「无数据」。
    if isinstance(fin, dict) and merged:
        try:
            json.dump(obj, open(cache, "w", encoding="utf-8"),
                      ensure_ascii=False)
            log(f"[缓存] 写入：{cache}")
        except Exception as e:
            log(f"[缓存] 写入失败：{e}")
    return obj["kline"], rows


# ---------------- 因子计算 ----------------

def pct_rank(values, x, min_n=60):
    """x 在 values 中的百分位（0~100）；样本不足返回 None。"""
    if x is None:
        return None
    vals = [v for v in values if v is not None]
    if len(vals) < min_n:
        return None
    return 100.0 * sum(1 for v in vals if v <= x) / len(vals)


def rolling_max(vals, win):
    from collections import deque
    out = [None] * len(vals)
    dq = deque()
    for i, v in enumerate(vals):
        while dq and vals[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
        while dq[0] <= i - win:
            dq.popleft()
        if i >= win - 1:
            out[i] = vals[dq[0]]
    return out


def roll_mean(vals, k):
    out = [None] * len(vals)
    s = 0.0
    cnt = 0
    for i, v in enumerate(vals):
        if v is not None:
            s += v
            cnt += 1
        if i >= k:
            old = vals[i - k]
            if old is not None:
                s -= old
                cnt -= 1
        if i >= k - 1 and cnt == k:
            out[i] = s / k
    return out


def kline_metrics(nodes):
    if len(nodes) < 30:
        return None
    cl = [fnum(x.get("last")) for x in nodes]
    hi = [fnum(x.get("high")) for x in nodes]
    if any(v is None for v in cl):
        return None
    tv = [fnum(x.get("exchange")) for x in nodes]
    pv = [fnum(x.get("volume")) for x in nodes]
    t = len(nodes) - 1

    def ret(k):
        return cl[t] / cl[t - k] - 1 if t >= k and cl[t - k] else None

    def avg_tv(k):
        if t + 1 < k:
            return None
        vals = [v for v in tv[t - k + 1:t + 1] if v is not None]
        return sum(vals) / len(vals) if vals else None

    std20 = None
    v20 = [v for v in tv[t - 19:t + 1] if v is not None]
    if len(v20) == 20:
        std20 = st.pstdev(v20)

    av20, av250 = avg_tv(20), avg_tv(250)

    def high_stats(k, require=True):
        if require and len(hi) < k:
            return None, None, None, None
        start = max(0, len(hi) - k)
        idx = max(range(start, len(hi)), key=lambda i: hi[i])
        h = hi[idx]
        return (h, nodes[idx]["date"], t - idx,
                cl[t] / h - 1 if h else None)

    high20, high20_date, days_since_high20, pos20 = high_stats(20)
    high60, high60_date, days_since_high60, pos60 = high_stats(60)
    high120, high120_date, days_since_high120, pos120 = high_stats(120)
    high250, high250_date, days_since_high, pos250 = high_stats(250, require=False)

    av5 = avg_tv(5)
    vol_ratio5 = (av5 / av20) if (av5 is not None and av20) else None

    def avg_pv(k):
        if t + 1 < k:
            return None
        vals = [v for v in pv[t - k + 1:t + 1] if v is not None]
        return sum(vals) / len(vals) if vals else None

    pav5, pav20 = avg_pv(5), avg_pv(20)
    pvol_ratio5 = (pav5 / pav20) if (pav5 is not None and pav20) else None
    pos = pos60 if pos60 is not None else pos250
    vol_stall = bool(
        pos is not None and pos >= -0.05
        and vol_ratio5 is not None and vol_ratio5 >= 1.5
        and ret(5) is not None and abs(ret(5)) <= 0.02
        and ret(20) is not None and ret(20) >= 0.10)
    vol_up = bool(
        pos is not None and pos >= -0.05
        and vol_ratio5 is not None and vol_ratio5 >= 2.0
        and ret(5) is not None and ret(5) >= 0.02)

    ma20 = sum(cl[-20:]) / 20 if len(cl) >= 20 else None
    ma60 = sum(cl[-60:]) / 60 if len(cl) >= 60 else None

    # 历史分位：用过去最多 250 个因子值，判断当前值在自身历史中的位置。
    n = len(cl)
    ret5_hist = [cl[i] / cl[i - 5] - 1 for i in range(5, n) if cl[i - 5]]
    ret20_hist = [cl[i] / cl[i - 20] - 1 for i in range(20, n) if cl[i - 20]]
    ret60_hist = [cl[i] / cl[i - 60] - 1 for i in range(60, n) if cl[i - 60]]
    mx250 = rolling_max(hi, 250)
    pos250_hist = [cl[i] / mx250[i] - 1
                   for i in range(249, n) if mx250[i]]
    avg20_hist = roll_mean(tv, 20)
    avg250_hist = roll_mean(tv, 250)
    std20_hist = []
    for i in range(19, n):
        win = [v for v in tv[i - 19:i + 1] if v is not None]
        std20_hist.append(st.pstdev(win) if len(win) == 20 else None)
    ratio_hist = [(avg20_hist[i] / avg250_hist[i]
                   if (avg20_hist[i] is not None and avg250_hist[i])
                   else None)
                  for i in range(n)]

    pct = {
        "RET5": pct_rank(ret5_hist[-250:], ret(5)),
        "RET20": pct_rank(ret20_hist[-250:], ret(20)),
        "RET60": pct_rank(ret60_hist[-250:], ret(60)),
        "POS250": pct_rank(pos250_hist[-250:], pos250),
        "AVG20": pct_rank(avg20_hist[-250:], av20),
        "STD20": pct_rank(std20_hist[-250:], std20),
        "RATIO": pct_rank(ratio_hist[-250:],
                          (av20 / av250) if (av20 is not None and av250) else None),
        "ACCEL": None,
    }
    pct_n = {
        "RET5": min(len(ret5_hist), 250),
        "RET20": min(len(ret20_hist), 250),
        "RET60": min(len(ret60_hist), 250),
        "POS250": min(len(pos250_hist), 250),
        "AVG20": min(len([v for v in avg20_hist if v is not None]), 250),
        "STD20": min(len([v for v in std20_hist if v is not None]), 250),
        "RATIO": min(len([v for v in ratio_hist if v is not None]), 250),
        "ACCEL": 0,
    }

    return {
        "bars": len(nodes), "date": nodes[-1]["date"], "close": cl[t],
        "ret5": ret(5), "ret20": ret(20), "ret60": ret(60),
        "av5": av5, "av20": av20, "av60": avg_tv(60), "av250": av250,
        "std20": std20,
        "ratio": (av20 / av250) if (av20 is not None and av250) else None,
        "high20": high20, "high20_date": high20_date,
        "days_since_high20": days_since_high20, "pos20": pos20,
        "high60": high60, "high60_date": high60_date,
        "days_since_high60": days_since_high60, "pos60": pos60,
        "high120": high120, "high120_date": high120_date,
        "days_since_high120": days_since_high120, "pos120": pos120,
        "high250": high250, "high250_date": high250_date,
        "days_since_high": days_since_high, "pos250": pos250,
        "vol_ratio5": vol_ratio5, "vol_stall": vol_stall, "vol_up": vol_up,
        "pav5": pav5, "pav20": pav20, "pvol_ratio5": pvol_ratio5,
        "pct": pct, "pct_n": pct_n,
        "close_5_ago": cl[t - 5] if t >= 5 else None,
        "close_20_ago": cl[t - 20] if t >= 20 else None,
        "close_60_ago": cl[t - 60] if t >= 60 else None,
        "range5_high": max(cl[-5:]) if len(cl) >= 5 else None,
        "range5_low": min(cl[-5:]) if len(cl) >= 5 else None,
        "ma20": ma20, "ma60": ma60,
        "below_ma20": (cl[t] < ma20) if ma20 else None,
    }


def market_phase(km):
    """区分「历史顶已过」和「当前反弹/局部高位」，避免一句话混淆。"""
    pos250, pos60, ret20 = km.get("pos250"), km.get("pos60"), km.get("ret20")
    if pos250 is not None and pos250 <= -0.20:
        if pos60 is not None and pos60 >= -0.05:
            if ret20 is not None and ret20 > 0:
                return "历史顶已过，当前反弹接近局部高点"
            return "历史顶已过，当前在局部高位附近"
        return "历史顶已过，当前处于回撤/震荡"
    if pos250 is not None and pos250 >= -0.05:
        return "接近250日高点"
    if pos60 is not None and pos60 >= -0.03:
        return "接近60日局部高点"
    if pos250 is not None and pos250 >= -0.10:
        return "高位小幅回落"
    if pos250 is not None and pos250 >= -0.20:
        return "中高位回落"
    return "未处于明显高位"


def prev_quarter(end_date):
    y, m = int(end_date[:4]), int(end_date[5:7])
    if m == 3:
        return f"{y - 1}-12-31"
    if m == 6:
        return f"{y}-03-31"
    if m == 9:
        return f"{y}-06-30"
    if m == 12:
        return f"{y}-09-30"
    return None


def year_ago(end_date):
    y, m = int(end_date[:4]), int(end_date[5:7])
    return f"{y - 1}-{m:02d}-{end_date[8:10]}"


def relative_accel(cur, prev, min_abs_base):
    """相对加速度 = (本期增速 - 上期增速) / |上期增速|。

    用绝对值作分母，避免上期负增长时方向反转；上期增速接近 0 时不可比。
    返回 (值, 说明)；值用小数表示，0.5 = 50%。
    """
    if cur is None or prev is None:
        return None, "缺少上一期增速，无法计算相对加速度"
    if abs(prev) < min_abs_base:
        return None, "上期增速接近 0，相对加速度不可比"
    note = "上期增速为负，相对加速度只作参考" if prev < 0 else ""
    return (cur - prev) / abs(prev), note


def fin_metrics(rows, end):
    valid = [r for r in rows
             if pdate(r.get("InfoPublDate")) and pdate(r.get("EndDate"))
             and pdate(r.get("InfoPublDate")) <= end]
    valid.sort(key=lambda r: pdate(r["EndDate"]))

    seq = []
    for r in valid:
        y = fnum(r.get("NPParentCompanyYOY_Q"))
        if y is None:
            continue
        seq.append({
            "end": pdate(r["EndDate"]),
            "pub": pdate(r.get("InfoPublDate")),
            "yoy": y,
            "cut": fnum(r.get("NPParentCompanyCutYOY_Q")),
            "profit_q": fnum(r.get("NPParentCompanyOwners_Q")),
            "profit": (fnum(r.get("NPParentCompanyOwners_Q"))
                       if fnum(r.get("NPParentCompanyOwners_Q")) is not None
                       else fnum(r.get("NPParentCompanyOwners"))),
            "revenue_q": fnum(r.get("OperatingRevenue_Q")
                              or r.get("TotalOperatingRevenue_Q")
                              or r.get("OperatingRevenue")
                              or r.get("TotalOperatingRevenue")),
            "revenue_yoy": fnum(r.get("OperatingRevenueGrowRate_Q")),
        })
    if not seq:
        return None

    latest = seq[-1]
    prev = None
    pq = prev_quarter(latest["end"])
    if pq:
        prev = next((x for x in seq if x["end"] == pq), None)

    prev2 = None
    if prev:
        pq2 = prev_quarter(prev["end"])
        prev2 = next((x for x in seq if x["end"] == pq2), None) if pq2 else None

    yoy_base_end = year_ago(latest["end"])
    yoy_base = next((x for x in seq if x["end"] == yoy_base_end), None)
    prev_yoy_base_end = year_ago(prev["end"]) if prev else None
    prev_yoy_base = (next((x for x in seq if x["end"] == prev_yoy_base_end), None)
                     if prev_yoy_base_end else None)

    accel_pp = (latest["yoy"] - prev["yoy"]) if prev else None
    accel, accel_note = relative_accel(
        latest["yoy"], prev["yoy"] if prev else None, 1.0)

    accel_cut = None
    if prev and latest.get("cut") is not None and prev.get("cut") is not None:
        accel_cut, _ = relative_accel(latest["cut"], prev["cut"], 1.0)

    accel_z = None

    stale_days = None
    if latest.get("pub"):
        try:
            stale_days = (dt.date.fromisoformat(end)
                          - dt.date.fromisoformat(latest["pub"])).days
        except Exception:
            pass

    qoq = qoq_prev = qoq_accel = qoq_accel_pp = None
    qoq_note = ""
    qoq_accel_note = ""
    if prev is None:
        qoq_note = "缺少上一季度数据，环比不可比"
    elif latest.get("profit_q") is None or prev.get("profit_q") is None:
        qoq_note = "单季利润缺失，环比不可比"
    elif latest["profit_q"] <= 0 or prev["profit_q"] <= 0:
        qoq_note = "本季或上季利润≤0，环比不可比"
    else:
        qoq = latest["profit_q"] / prev["profit_q"] - 1
        if prev2 and prev2.get("profit_q") is not None and prev2["profit_q"] > 0:
            qoq_prev = prev["profit_q"] / prev2["profit_q"] - 1
            qoq_accel_pp = (qoq - qoq_prev) * 100
            qoq_accel, qoq_accel_note = relative_accel(qoq, qoq_prev, 0.01)
            if qoq_accel is None:
                qoq_note = qoq_accel_note
        else:
            qoq_note = "缺少上上季度数据，无法判断环比是否加速"

    revenue_qoq = None
    if (latest.get("revenue_q") is not None and prev
            and prev.get("revenue_q") not in (None, 0)):
        revenue_qoq = latest["revenue_q"] / prev["revenue_q"] - 1

    history = []
    for i, x in enumerate(seq):
        a = None
        if i > 0 and seq[i - 1]["end"] == prev_quarter(x["end"]):
            a = x["yoy"] - seq[i - 1]["yoy"]
        q = None
        if i > 0 and seq[i - 1]["end"] == prev_quarter(x["end"]):
            p0, p1 = seq[i - 1].get("profit_q"), x.get("profit_q")
            if p0 is not None and p1 is not None and p0 > 0 and p1 > 0:
                q = p1 / p0 - 1
        qa = None
        if q is not None and history and history[-1].get("qoq") is not None:
            qa = (q - history[-1]["qoq"]) * 100
        history.append({
            "end": x["end"], "pub": x["pub"], "yoy": x["yoy"],
            "cut": x["cut"], "accel": a, "qoq": q, "qoq_accel": qa,
        })

    return {
        "latest_end": latest["end"], "latest_pub": latest["pub"],
        "latest_yoy": latest["yoy"], "prev_yoy": prev["yoy"] if prev else None,
        "accel": accel, "accel_pp": accel_pp, "accel_note": accel_note,
        "accel_cut": accel_cut, "accel_z": accel_z,
        "eav": None, "profit": latest.get("profit_q"),
        "profit_q": latest.get("profit_q"),
        "prev_profit_q": prev.get("profit_q") if prev else None,
        "prev2_profit_q": prev2.get("profit_q") if prev2 else None,
        "prev_end": prev["end"] if prev else None,
        "yoy_base_end": yoy_base_end,
        "yoy_base_profit_q": yoy_base.get("profit_q") if yoy_base else None,
        "prev_yoy_base_end": prev_yoy_base_end,
        "prev_yoy_base_profit_q": (prev_yoy_base.get("profit_q")
                                   if prev_yoy_base else None),
        "latest_revenue": latest.get("revenue_q"),
        "latest_revenue_yoy": latest.get("revenue_yoy"),
        "prev_revenue": prev.get("revenue_q") if prev else None,
        "yoy_base_revenue": yoy_base.get("revenue_q") if yoy_base else None,
        "revenue_qoq": revenue_qoq,
        "qoq": qoq, "qoq_prev": qoq_prev,
        "qoq_accel": qoq_accel, "qoq_accel_pp": qoq_accel_pp,
        "qoq_note": qoq_note, "qoq_accel_note": qoq_accel_note,
        "stale_days": stale_days, "n_reports": len(seq),
        "history": history[-6:],
    }


# ---------------- 评分 ----------------

def table_score(kind, x):
    """按 SCORE_TABLE 从高到低匹配；返回 (score, label)。"""
    if x is None:
        return 50.0, "数据不足"
    table = SCORE_TABLE[kind]
    labels = LABELS[kind]
    for i, (th, sc) in enumerate(table):
        if x >= th:
            return float(sc), labels[i]
    return float(table[-1][1] - 15), labels[-1]


# 自身历史分位 -> 得分；P50 映射到 40，避免中性股票被总分阈值误判
PCT_TABLE = [
    (95.0, 100), (90.0, 85), (75.0, 65),
    (50.0, 40), (25.0, 25), (0.0, 15),
]


def percentile_score(kind, pct):
    """把自身历史分位映射成 20~100 分；越高越偏顶部风险。"""
    if pct is None:
        return None, None
    labels = PCT_LABELS.get(kind, LABELS[kind])
    for i, (th, sc) in enumerate(PCT_TABLE):
        if pct >= th:
            return float(sc), labels[i]
    return 20.0, labels[-1]


def score_factor(kind, x, pct):
    """有历史分位就用相对分位；样本不足回退到绝对阈值。"""
    if pct is not None:
        return percentile_score(kind, pct)
    return table_score(kind, x)


def accel_score(fm):
    """业绩加速度：越低（越减速）分数越高；正加速反而降低顶部风险。"""
    if not fm or fm.get("accel") is None:
        return 50.0, "业绩数据不足"
    if fm.get("profit") is not None and fm["profit"] <= 0:
        return 50.0, "盈利为负，仅参考"
    a = fm["accel"]
    labels = LABELS["ACCEL"]
    if a <= -0.50:
        return 100.0, labels[0]
    if a <= -0.30:
        return 85.0, labels[1]
    if a <= -0.10:
        return 65.0, labels[2]
    if a <= 0.10:
        return 50.0, labels[3]
    if a <= 0.30:
        return 30.0, labels[4]
    return 15.0, labels[5]


def fmt_pct(x, signed=True):
    if x is None:
        return "—"
    return (f"{x * 100:+.2f}%" if signed else f"{x * 100:.2f}%")


def fmt_pp(x, limit=500):
    if x is None:
        return "—"
    if abs(x) > limit:
        return f"{x:+.0f}pp（极端）"
    return f"{x:+.1f}pp"


def fmt_pp_safe(x, limit=500):
    if x is None:
        return "—"
    if abs(x) > limit:
        return "极端正值（基数异常）" if x > 0 else "极端负值（基数异常）"
    return f"{x:+.1f}pp"


def fmt_pct_point(x):
    return f"{x:+.1f}%" if x is not None else "—"


def fmt_pct_frac(x, limit=1000.0):
    """x 是小数（0.1 = 10%），用于环比等可能极端的增速。"""
    if x is None:
        return "—"
    y = x * 100
    if abs(y) > limit:
        return "极端值（基数异常）"
    return f"{y:+.1f}%"


def earnings_judgement(fm):
    """同比加速度：增长率的相对变化。

    相对加速度 = (本季同比 - 上季同比) / |上季同比|；
    >= +10% 为加速，>= +50% 为强加速；对称判断减速。
    """
    if not fm:
        return {"state": "数据不足", "direction": "未知",
                "is_accelerating": False, "is_decelerating": False,
                "tag": "【数据不足】", "note": "无可用财务数据"}
    if fm.get("accel") is None:
        return {"state": "数据不足", "direction": "未知",
                "is_accelerating": False, "is_decelerating": False,
                "tag": "【数据不足】",
                "note": fm.get("accel_note") or "最近两个季度增速不连续，无法计算"}
    if fm.get("profit") is not None and fm["profit"] <= 0:
        return {"state": "盈利为负", "direction": "未知",
                "is_accelerating": False, "is_decelerating": False,
                "tag": "【盈利为负，仅参考】",
                "note": "单季利润为负，增速差的参考意义弱"}

    a = fm["accel"]
    if a >= 0.50:
        state, tag = "强加速", "【强加速】"
        note = f"相对加速度 {a * 100:+.1f}%（≥+50%）"
    elif a >= 0.10:
        state, tag = "加速", "【加速】"
        note = f"相对加速度 {a * 100:+.1f}%（≥+10%）"
    elif a > -0.10:
        state, tag = "基本持平", "【基本持平】"
        note = f"相对加速度 {a * 100:+.1f}%，在 ±10% 内"
    elif a > -0.50:
        state, tag = "减速", "【减速】"
        note = f"相对加速度 {a * 100:+.1f}%（≤−10%）"
    else:
        state, tag = "强减速", "【强减速】"
        note = f"相对加速度 {a * 100:+.1f}%（≤−50%）"

    if fm.get("accel_note"):
        note += "；" + fm["accel_note"]
    if abs(a) > 5.0:
        note += "（基数异常，谨慎解读）"
    direction = "加速" if a >= 0.10 else ("减速" if a <= -0.10 else "持平")
    return {"state": state, "direction": direction,
            "is_accelerating": a >= 0.10, "is_decelerating": a <= -0.10,
            "tag": tag, "note": note}


def qoq_judgement(fm):
    """环比提示：单季净利环比增速的相对变化。

    环比受季节性影响很大，只作辅助；同比加速度仍是主口径。
    """
    if not fm or fm.get("qoq") is None:
        return {"state": "不可比", "direction": "未知",
                "is_accelerating": False, "is_decelerating": False,
                "tag": "【不可比】",
                "note": (fm or {}).get("qoq_note") or "缺少环比数据"}
    a = fm.get("qoq_accel")
    if a is None:
        return {"state": "不可比", "direction": "未知",
                "is_accelerating": False, "is_decelerating": False,
                "tag": "【不可比】",
                "note": fm.get("qoq_note") or "缺少上上季度数据"}
    if abs(fm["qoq"]) > 5.0 or abs(a) > 5.0:
        return {"state": "基数异常", "direction": "未知",
                "is_accelerating": False, "is_decelerating": False,
                "tag": "【基数异常】",
                "note": "环比增速极端，通常由低基数/负基数造成，不判断加速"}

    if a >= 0.50:
        state, tag = "强加速", "【强加速】"
        note = f"相对加速度 {a * 100:+.1f}%（≥+50%）"
    elif a >= 0.10:
        state, tag = "加速", "【加速】"
        note = f"相对加速度 {a * 100:+.1f}%（≥+10%）"
    elif a > -0.10:
        state, tag = "基本持平", "【基本持平】"
        note = f"相对加速度 {a * 100:+.1f}%，在 ±10% 内"
    elif a > -0.50:
        state, tag = "减速", "【减速】"
        note = f"相对加速度 {a * 100:+.1f}%（≤−10%）"
    else:
        state, tag = "强减速", "【强减速】"
        note = f"相对加速度 {a * 100:+.1f}%（≤−50%）"

    if fm.get("qoq_accel_note"):
        note += "；" + fm["qoq_accel_note"]
    if abs(a) > 5.0:
        note += "（基数异常，谨慎解读）"
    direction = "加速" if a >= 0.10 else ("减速" if a <= -0.10 else "持平")
    return {"state": state, "direction": direction,
            "is_accelerating": a >= 0.10, "is_decelerating": a <= -0.10,
            "tag": tag, "note": note}


def evaluate(code, end, refresh, with_quote=True):
    wk = westock_module()
    nodes, fin_rows = fetch_kline(wk, code, end, refresh)
    km = kline_metrics(nodes)
    if km is None:
        if not nodes:
            q = call(wk, "data_quote", {"code": code, "date": end})
            has_quote = isinstance(q, dict) and bool(q.get(code))
            if not has_quote:
                raise RuntimeError(
                    f"{code} 无行情数据：请检查代码是否存在；"
                    "基金/ETF、退市或停牌股票可能不在覆盖范围内。"
                    "A股代码示例：sh600519 / sz300750")
            raise RuntimeError(
                f"{code} 当前无 K 线（可能停牌），无法计算短期因子")
        raise RuntimeError(
            f"{code} 的 K 线仅 {len(nodes)} 根，至少需要 30 根（约 6 周）"
            "才能计算短期因子；新股/次新股暂不适用")

    log(f"[计算] K线因子：5日 {fmt_pct(km['ret5'])} | 20日 {fmt_pct(km['ret20'])} | "
        f"60日 {fmt_pct(km['ret60'])} | 距250日高点 {fmt_pct(km['pos250'])}")
    log(f"[计算] 换手因子：20日均 {km['av20']:.2f}% | "
        f"标准差 {km['std20']:.2f} | 1月/1年 {km['ratio']:.2f}"
        if (km['av20'] is not None and km['std20'] is not None
            and km['ratio'] is not None) else
        "[计算] 换手因子：数据不完整，换手历史不足")

    phase = market_phase(km)
    vr = km.get("pvol_ratio5")
    hi5, lo5 = km.get("range5_high"), km.get("range5_low")
    range5 = (hi5 / lo5 - 1) if (hi5 and lo5) else None
    log(f"[位置] {phase} | 距60日高点 {fmt_pct(km.get('pos60'))}"
        f"（{km.get('high60_date') or '—'}） | 距250日高点 {fmt_pct(km.get('pos250'))}")
    log(f"[量价] 5日/20日量比 {vr:.2f}" if vr is not None else
        "[量价] 5日/20日量比 —")
    log(f"       5日涨跌 {fmt_pct(km.get('ret5'))} | "
        f"5日振幅 {fmt_pct(range5)} | 距60日高点 {fmt_pct(km.get('pos60'))}")

    fm = fin_metrics(fin_rows, end)
    quote = {}
    if with_quote:
        q = call(wk, "data_quote", {"code": code, "date": end})
        if isinstance(q, dict):
            quote = q.get(code) or {}

    # 各项得分
    pct = km.get("pct") or {}
    s_ret5, l_ret5 = score_factor("RET5", km["ret5"], pct.get("RET5"))
    s_ret20, l_ret20 = score_factor("RET20", km["ret20"], pct.get("RET20"))
    s_ret60, l_ret60 = score_factor("RET60", km["ret60"], pct.get("RET60"))
    s_pos, l_pos = score_factor("POS250", km["pos250"], pct.get("POS250"))
    s_av20, l_av20 = score_factor("AVG20", km["av20"], pct.get("AVG20"))
    s_std, l_std = score_factor("STD20", km["std20"], pct.get("STD20"))
    s_ratio, l_ratio = score_factor("RATIO", km["ratio"], pct.get("RATIO"))
    fallback = [k for k in ("RET5", "RET20", "RET60", "POS250",
                            "AVG20", "STD20", "RATIO")
                if pct.get(k) is None]
    score_mode = "relative" if not fallback else "mixed"
    s_accel, l_accel = accel_score(fm)
    ej = earnings_judgement(fm)
    qj = qoq_judgement(fm)
    if fm:
        log(f"[业绩] 最新 {fm['latest_end']} | 单季同比 "
            f"{fmt_pct_point(fm['latest_yoy'])} | 上季 "
            f"{fmt_pct_point(fm.get('prev_yoy'))} | 加速度 "
            f"{fmt_pct_frac(fm.get('accel'))} | 状态 {ej['state']}")
        log(f"[业绩] 环比 {fmt_pct_frac(fm.get('qoq'))} | "
            f"上季环比 {fmt_pct_frac(fm.get('qoq_prev'))} | "
            f"环比加速度 {fmt_pct_frac(fm.get('qoq_accel'))} | 状态 {qj['state']}")
    else:
        log("[业绩] 无可用财务数据，业绩卡片不判断")

    items = [
        ("价格", "RET5", "5日涨幅", km["ret5"], s_ret5, l_ret5, fmt_pct(km["ret5"])),
        ("价格", "RET20", "20日涨幅", km["ret20"], s_ret20, l_ret20, fmt_pct(km["ret20"])),
        ("价格", "RET60", "60日涨幅", km["ret60"], s_ret60, l_ret60, fmt_pct(km["ret60"])),
        ("价格", "POS250", "距250日高点", km["pos250"], s_pos, l_pos, fmt_pct(km["pos250"])),
        ("换手", "AVG20", "20日均换手", km["av20"], s_av20, l_av20,
         f"{km['av20']:.2f}%" if km["av20"] is not None else "—"),
        ("换手", "STD20", "20日换手标准差", km["std20"], s_std, l_std,
         f"{km['std20']:.2f}" if km["std20"] is not None else "—"),
        ("换手", "RATIO", "1月/1年换手比", km["ratio"], s_ratio, l_ratio,
         f"{km['ratio']:.2f}" if km["ratio"] is not None else "—"),
        ("业绩", "ACCEL", "单季盈利加速度", fm["accel"] if fm else None,
         s_accel, l_accel,
         fmt_pct_frac(fm.get('accel')) if fm and fm.get("accel") is not None else "—"),
    ]

    total = 0.0
    for _, key, _, _, sc, _, _ in items:
        total += sc * WEIGHTS[key]

    pos = km["pos250"]
    if pos is not None and pos <= -0.20:
        if phase == "历史顶已过，当前反弹接近局部高点":
            verdict = "历史顶已过，当前反弹接近局部高点"
            summary = (f"当前价仍低于 250 日高点（{km['high250_date']}）"
                       f"{abs(pos) * 100:.1f}%，不是新的历史顶；但已接近 60 日"
                       "局部高点（或处于反弹高位），追高的短期风险在上升。")
        else:
            verdict = "顶部大概率已过"
            summary = (f"当前价已低于 250 日高点（{km['high250_date']}）20% 以上，"
                       "历史最高点大概率已经出现；当前更可能处于回撤/震荡，"
                       "而不是新的顶部。")
    elif total >= 75:
        verdict = "已进入顶部高风险区"
        summary = ("多项过热指标共振，当前处于顶部高风险区；"
                   "但无法保证这就是精确最高点，需结合是否跌破 MA20 确认。")
    elif total >= 60:
        verdict = "顶部风险偏高"
        summary = "高位特征较明显，追高性价比低，需警惕放量滞涨或跌破短期均线。"
    elif total >= 45:
        verdict = "高位震荡，未见确认"
        summary = "有一定高位特征，但尚未形成多因子共振，继续观察量价是否背离。"
    else:
        verdict = "未见明确顶部信号"
        summary = "当前过热程度不高，或已充分调整；顶部风险偏低。"

    if pos is None:
        position = "数据不足"
    elif pos >= -0.05:
        position = "高位区（距250日高点<5%）"
    elif pos >= -0.10:
        position = "高位小幅回落（5%~10%）"
    elif pos >= -0.20:
        position = "明显回落（10%~20%）"
    else:
        position = "深度回落（>20%）"

    completeness = 40
    if km["bars"] >= 250:
        completeness += 25
    if km["ratio"] is not None:
        completeness += 15
    if km["std20"] is not None:
        completeness += 10
    if fm and fm.get("accel") is not None:
        completeness += 10

    drivers = sorted(items, key=lambda x: -x[4] * WEIGHTS[x[1]])[:3]
    driver_txt = "、".join(f"{d[2]}（{d[4]:.0f}分）" for d in drivers)

    warnings = []
    if km["bars"] < 250:
        warnings.append(f"K线仅 {km['bars']} 根，不足 250 日；位置类指标只作参考")
    if not fm:
        warnings.append("无可用财务数据，业绩卡片不判断")
    elif fm.get("accel") is None:
        warnings.append("最近两个季度增速不连续，业绩卡片不判断")
    elif fm.get("stale_days") and fm["stale_days"] > 200:
        warnings.append(f"最新财报已 {fm['stale_days']} 天，业绩背景偏旧")
    if km.get("below_ma20"):
        warnings.append("当前收盘已跌破 MA20（仅作事实提示）")

    log("[完成] 4 个因子独立计算（不合并总分）")

    result = {
        "code": code, "name": quote.get("name") or "",
        "date": end, "close": km["close"],
        "high250": km["high250"], "high250_date": km["high250_date"],
        "days_since_high": km["days_since_high"], "pos250": pos,
        "score": round(total, 1), "verdict": verdict,
        "position": position, "summary": summary,
        "drivers": driver_txt, "completeness": completeness,
        "phase": phase,
        "score_mode": score_mode,
        "percentile": pct,
        "percentile_n": km.get("pct_n"),
        "local": {
            "pos20": km.get("pos20"), "pos60": km.get("pos60"),
            "pos120": km.get("pos120"),
            "high20_date": km.get("high20_date"),
            "high60_date": km.get("high60_date"),
            "high120_date": km.get("high120_date"),
        },
        "volume": {
            "vol_ratio5": km.get("vol_ratio5"),
            "vol_stall": km.get("vol_stall"),
            "vol_up": km.get("vol_up"),
        },
        "items": items, "kline": km, "finance": fm,
        "quote": quote, "warnings": warnings,
        "earnings": {
            "state": ej["state"], "direction": ej["direction"],
            "is_accelerating": ej["is_accelerating"],
            "is_decelerating": ej["is_decelerating"],
            "tag": ej["tag"], "note": ej["note"],
            "qoq": fm.get("qoq") if fm else None,
            "qoq_prev": fm.get("qoq_prev") if fm else None,
            "qoq_accel": fm.get("qoq_accel") if fm else None,
            "qoq_state": qj["state"], "qoq_direction": qj["direction"],
            "qoq_is_accelerating": qj["is_accelerating"],
            "qoq_is_decelerating": qj["is_decelerating"],
            "qoq_tag": qj["tag"], "qoq_note": qj["note"],
        },
    }
    result["views"] = factor_views(result)
    return result


# ---------------- 输出 ----------------

def trend_arrow(a):
    if a is None:
        return "—"
    return "↑ 加速" if a >= 5 else ("↓ 减速" if a <= -5 else "→ 持平")


def consecutive_qoq(hist, limit=4):
    """从最新季度往前取连续可比的环比记录。"""
    out = []
    for h in reversed(hist or []):
        if h.get("qoq") is None:
            break
        out.append(h)
    return list(reversed(out))[-limit:]


def earnings_combo(ej):
    """把同比、环比两个方向合并成一句结论。"""
    yoy_dir = ej.get("direction")
    qoq_dir = ej.get("qoq_direction")
    if yoy_dir in ("加速", "减速") and qoq_dir == yoy_dir:
        return f"同比与环比同向（{yoy_dir}）"
    if yoy_dir in ("加速", "减速") and qoq_dir in ("加速", "减速"):
        return f"同比与环比背离（同比{yoy_dir}、环比{qoq_dir}）"
    if yoy_dir in ("加速", "减速"):
        return f"以同比为主（{yoy_dir}），环比{ej.get('qoq_state', '不可比')}"
    if yoy_dir == "持平" and qoq_dir in ("加速", "减速"):
        return f"同比基本持平，环比{qoq_dir}；以环比为辅，注意季节性"
    if yoy_dir in ("加速", "减速") and qoq_dir == "持平":
        return f"同比{yoy_dir}，环比基本持平"
    if yoy_dir == "持平" and qoq_dir == "持平":
        return "同比与环比均基本持平"
    return "数据不足，无法判断"


def _fmt_px(v):
    return f"{v:.2f}" if v is not None else "—"


def _fmt_yi(v):
    if v is None:
        return "—"
    return f"{v / 1e8:,.2f} 亿元"


def _fmt_vol(v):
    if v is None:
        return "—"
    return f"{v:,.0f}"


def _insufficient(key, title, subtitle, note, calc=None):
    return {
        "key": key, "title": title, "subtitle": subtitle,
        "state": "数据不足", "tone": "muted", "headline": "数据不足，不判断",
        "metrics": [], "calc": calc or [], "note": note, "wide": False,
    }


def factor_views(r):
    """四个因子各自独立出结果；这里不做加权、不汇总、不判顶。"""
    km = r.get("kline") or {}
    fm = r.get("finance")
    ej = r.get("earnings") or {}
    pct = km.get("pct") or {}
    bars = km.get("bars") or 0
    views = []

    # 1. 动量：只描述涨势强弱和位置，不判断顶部
    mom_calc = [
        ("当前收盘", _fmt_px(km.get("close"))),
        ("20日前收盘", _fmt_px(km.get("close_20_ago"))),
        ("60日前收盘", _fmt_px(km.get("close_60_ago"))),
        ("60日最高收盘", f"{_fmt_px(km.get('high60'))}（{km.get('high60_date') or '—'}）"),
        ("20日涨幅计算", f"收盘 {_fmt_px(km.get('close'))} ÷ 20日前收盘 "
                      f"{_fmt_px(km.get('close_20_ago'))} − 1 = {fmt_pct(km.get('ret20'))}"),
        ("5日涨幅计算", f"收盘 {_fmt_px(km.get('close'))} ÷ 5日前收盘 "
                     f"{_fmt_px(km.get('close_5_ago'))} − 1 = {fmt_pct(km.get('ret5'))}"),
        ("60日涨幅计算", f"收盘 {_fmt_px(km.get('close'))} ÷ 60日前收盘 "
                      f"{_fmt_px(km.get('close_60_ago'))} − 1 = {fmt_pct(km.get('ret60'))}"),
    ]
    if bars < 60 or km.get("ret20") is None or km.get("pos60") is None:
        views.append(_insufficient(
            "momentum", "动量", "股价走势的延续性",
            "K线不足 60 日或动量数据缺失，本轮不判断。", mom_calc))
    else:
        ret20, ret60, pos60 = km["ret20"], km.get("ret60"), km["pos60"]
        if ret20 >= 0.20:
            state, tone = "过热", "warn"
        elif ret20 >= 0.10:
            state, tone = "偏热", "warn"
        elif ret20 >= 0:
            state, tone = "中性", "info"
        else:
            state, tone = "偏弱", "muted"
        views.append({
            "key": "momentum", "title": "动量",
            "subtitle": "股价走势的延续性",
            "state": state, "tone": tone,
            "headline": f"20日 {fmt_pct(ret20)}，距60日高点 {fmt_pct(pos60)}",
            "metrics": [
                ("20日涨幅", fmt_pct(ret20)),
                ("5日涨幅", fmt_pct(km.get("ret5"))),
                ("60日涨幅", fmt_pct(ret60)),
                ("距60日高点", fmt_pct(pos60)),
            ],
            "calc": mom_calc,
            "note": (
                "动量只描述“已经涨了多少、是否还在高位”，不等于见顶。"
                "高位强动量既可能延续，也可能是加速赶顶，需要你结合其他因子判断。"
            ),
            "wide": False,
        })

    # 2. 换手：只描述交易拥挤度，不判断顶部
    av20, av250 = km.get("av20"), km.get("av250")
    ratio = km.get("ratio")
    pct_av20 = pct.get("AVG20")
    turn_calc = [
        ("20日均换手", f"{av20:.2f}%" if av20 is not None else "—"),
        ("250日均换手", f"{av250:.2f}%" if av250 is not None else "—"),
        ("1月/1年换手比", f"{ratio:.2f}" if ratio is not None else "—"),
        ("自身分位", f"P{pct_av20:.0f}" if pct_av20 is not None else "—"),
        ("分位样本", f"{(km.get('pct_n') or {}).get('AVG20') or 0} 个交易日"),
        ("换手比计算", f"20日均 {av20:.2f}% ÷ 250日均 {av250:.2f}% = "
                     f"{ratio:.2f}" if (av20 is not None and av250 and ratio is not None) else "—"),
    ]
    if bars < 60 or av20 is None or av250 is None or pct_av20 is None:
        views.append(_insufficient(
            "turnover", "换手", "顶部区域换手通常偏高",
            "换手历史不足 60 日或换手数据缺失，本轮不判断。", turn_calc))
    else:
        if pct_av20 >= 75 and ratio is not None and ratio >= 1.2:
            state, tone = "偏高", "warn"
        elif pct_av20 >= 50:
            state, tone = "中性", "info"
        else:
            state, tone = "偏低", "muted"
        views.append({
            "key": "turnover", "title": "换手",
            "subtitle": "顶部区域换手通常偏高",
            "state": state, "tone": tone,
            "headline": f"20日均换手 {av20:.2f}%，自身 {pct_av20:.0f} 分位",
            "metrics": [
                ("20日均换手", f"{av20:.2f}%"),
                ("自身分位", f"P{pct_av20:.0f}"),
                ("1月/1年换手比", f"{ratio:.2f}" if ratio is not None else "—"),
                ("250日均换手", f"{av250:.2f}%"),
            ],
            "calc": turn_calc,
            "note": (
                "换手只描述交易是否拥挤。高换手可能是顶部派发，也可能是突破放量；"
                "不能单独当作顶部信号。"
            ),
            "wide": False,
        })

    # 3. 业绩：同比、环比、营收三块分开，原始数据与结果放在一起
    earn_calc = []
    earn_blocks = []
    if fm:
        latest_end = fm.get("latest_end") or "—"
        latest_pub = fm.get("latest_pub") or "披露日未知"
        prev_end = fm.get("prev_end") or "—"
        prev2_end = None
        # 上上季报告期：从上一季往前推一个季度
        if fm.get("prev_end"):
            prev2_end = prev_quarter(fm["prev_end"]) or "—"
        yoy_base_end = fm.get("yoy_base_end") or "—"
        prev_base_end = fm.get("prev_yoy_base_end") or "—"

        earn_blocks = [
            {
                "title": "同比", "subtitle": "单季归母净利润",
                "tone": "warn" if (fm.get("accel") is not None and fm["accel"] <= -0.10) else (
                    "good" if (fm.get("accel") is not None and fm["accel"] >= 0.10) else "info"),
                "results": [
                    ("本季同比", fmt_pct_point(fm.get("latest_yoy"))),
                    ("上季同比", fmt_pct_point(fm.get("prev_yoy"))),
                    ("同比加速度", fmt_pct_frac(fm.get("accel"))),
                ],
                "sources": [
                    (f"本季 {latest_end}", _fmt_yi(fm.get("profit_q"))),
                    (f"上年同期 {yoy_base_end}", _fmt_yi(fm.get("yoy_base_profit_q"))),
                    (f"上季 {prev_end}", _fmt_yi(fm.get("prev_profit_q"))),
                    (f"上季上年同期 {prev_base_end}", _fmt_yi(fm.get("prev_yoy_base_profit_q"))),
                ],
                "formulas": [
                    (f"本季 {_fmt_yi(fm.get('profit_q'))} ÷ 上年同期 "
                     f"{_fmt_yi(fm.get('yoy_base_profit_q'))} − 1 = "
                     f"{fmt_pct_point(fm.get('latest_yoy'))}"),
                    (f"上季 {_fmt_yi(fm.get('prev_profit_q'))} ÷ 上年同期 "
                     f"{_fmt_yi(fm.get('prev_yoy_base_profit_q'))} − 1 = "
                     f"{fmt_pct_point(fm.get('prev_yoy'))}"),
                    (f"({fmt_pct_point(fm.get('latest_yoy'))} − "
                     f"{fmt_pct_point(fm.get('prev_yoy'))}) ÷ "
                     f"|{fmt_pct_point(fm.get('prev_yoy'))}| = "
                     f"{fmt_pct_frac(fm.get('accel'))}"),
                ],
            },
            {
                "title": "环比", "subtitle": "单季归母净利润",
                "tone": "warn" if (fm.get("qoq_accel") is not None and fm["qoq_accel"] <= -0.10) else (
                    "good" if (fm.get("qoq_accel") is not None and fm["qoq_accel"] >= 0.10) else "info"),
                "results": [
                    ("本季环比", fmt_pct_frac(fm.get("qoq"))),
                    ("上季环比", fmt_pct_frac(fm.get("qoq_prev"))),
                    ("环比加速度", fmt_pct_frac(fm.get("qoq_accel"))),
                ],
                "sources": [
                    (f"本季 {latest_end}", _fmt_yi(fm.get("profit_q"))),
                    (f"上季 {prev_end}", _fmt_yi(fm.get("prev_profit_q"))),
                    (f"上上季 {prev2_end}", _fmt_yi(fm.get("prev2_profit_q"))),
                ],
                "formulas": [
                    (f"本季 {_fmt_yi(fm.get('profit_q'))} ÷ 上季 "
                     f"{_fmt_yi(fm.get('prev_profit_q'))} − 1 = "
                     f"{fmt_pct_frac(fm.get('qoq'))}"),
                    (f"上季 {_fmt_yi(fm.get('prev_profit_q'))} ÷ 上上季 "
                     f"{_fmt_yi(fm.get('prev2_profit_q'))} − 1 = "
                     f"{fmt_pct_frac(fm.get('qoq_prev'))}"),
                    (f"({fmt_pct_frac(fm.get('qoq'))} − "
                     f"{fmt_pct_frac(fm.get('qoq_prev'))}) ÷ "
                     f"|{fmt_pct_frac(fm.get('qoq_prev'))}| = "
                     f"{fmt_pct_frac(fm.get('qoq_accel'))}"),
                ],
            },
            {
                "title": "营收", "subtitle": "单季营业收入（不是累计）",
                "tone": "info",
                "results": [
                    ("本季营收", _fmt_yi(fm.get("latest_revenue"))),
                    ("营收同比", fmt_pct_point(fm.get("latest_revenue_yoy"))),
                    ("营收环比", fmt_pct_frac(fm.get("revenue_qoq"))),
                ],
                "sources": [
                    (f"本季 {latest_end}", _fmt_yi(fm.get("latest_revenue"))),
                    (f"上年同期 {yoy_base_end}", _fmt_yi(fm.get("yoy_base_revenue"))),
                    (f"上季 {prev_end}", _fmt_yi(fm.get("prev_revenue"))),
                ],
                "formulas": [
                    (f"本季 {_fmt_yi(fm.get('latest_revenue'))} ÷ 上年同期 "
                     f"{_fmt_yi(fm.get('yoy_base_revenue'))} − 1 = "
                     f"{fmt_pct_point(fm.get('latest_revenue_yoy'))}"),
                    (f"本季 {_fmt_yi(fm.get('latest_revenue'))} ÷ 上季 "
                     f"{_fmt_yi(fm.get('prev_revenue'))} − 1 = "
                     f"{fmt_pct_frac(fm.get('revenue_qoq'))}"),
                ],
            },
        ]

        # 给 JSON/命令行保留一份扁平数据，但 HTML 优先使用 earn_blocks
        earn_calc = [
            ("报告期", f"{latest_end}（{latest_pub} 披露）"),
            ("加速口径", "单季归母净利润（不是营业收入）"),
        ]

    if not fm:
        views.append(_insufficient(
            "earnings", "业绩", "同比 / 环比 / 营收",
            "无可用财务数据，业绩背景不判断。", earn_calc))
    else:
        yoy_d = ej.get("direction") or "未知"
        qoq_d = ej.get("qoq_direction") or "未知"
        if fm.get("profit_q") is not None and fm["profit_q"] <= 0:
            state, tone = "盈利为负", "muted"
            note = "单季归母净利润为负，同比/环比加速度参考意义弱；只作背景展示。"
        elif any("加速" in str(x) for x in (yoy_d, qoq_d)):
            state, tone = "加速", "good"
            note = "同比或环比在加速。业绩加速是基本面背景，不是顶部信号，也不计入顶部判断。"
        elif any("减速" in str(x) for x in (yoy_d, qoq_d)):
            state, tone = "减速", "warn"
            note = "同比或环比在减速。它说明基本面动能走弱，但不等于股价已经见顶。"
        elif fm.get("accel") is None and fm.get("qoq_accel") is None:
            state, tone = "数据不足", "muted"
            note = "最近季度不连续或缺少基数，加速度不可用；只展示原始数据。"
        else:
            state, tone = "持平", "info"
            note = "同比/环比变化不大。业绩背景中性，不构成顶部判断。"
        detail = []
        if fm.get("accel_note"):
            detail.append(fm["accel_note"])
        if fm.get("qoq_accel_note"):
            detail.append(fm["qoq_accel_note"])
        if fm.get("accel") is not None and abs(fm["accel"]) > 5.0:
            detail.append("上期增速基数较小，相对加速度百分比会被放大，需结合原始利润看")
        if detail:
            note += " " + "；".join(dict.fromkeys(detail)) + "。"
        views.append({
            "key": "earnings", "title": "业绩",
            "subtitle": "同比 / 环比 / 营收（背景）",
            "state": state, "tone": tone,
            "headline": (
                f"最新报告期 {fm.get('latest_end') or '—'}，"
                f"单季归母净利润同比 {fmt_pct_point(fm.get('latest_yoy'))}"
            ),
            "metrics": [],
            "calc": [],
            "blocks": earn_blocks,
            "note": note,
            "wide": True,
        })
    # 4. 量价：放量盘整只作观察，不单独触发
    hi5, lo5 = km.get("range5_high"), km.get("range5_low")
    range5 = (hi5 / lo5 - 1) if (hi5 and lo5) else None
    vr, ret5, pos60 = km.get("pvol_ratio5"), km.get("ret5"), km.get("pos60")
    vol_calc = [
        ("5日均量（原始）", _fmt_vol(km.get("pav5"))),
        ("20日均量（原始）", _fmt_vol(km.get("pav20"))),
        ("5日/20日量比", f"{vr:.2f}" if vr is not None else "—"),
        ("量比计算", (f"5日均量 {_fmt_vol(km.get('pav5'))} ÷ 20日均量 "
                    f"{_fmt_vol(km.get('pav20'))}")),
        ("5日收盘区间", f"{_fmt_px(lo5)} ~ {_fmt_px(hi5)}"),
        ("5日振幅", fmt_pct(range5)),
        ("5日涨跌", fmt_pct(ret5)),
        ("距60日高点", fmt_pct(pos60)),
    ]
    if bars < 60 or vr is None or range5 is None or pos60 is None:
        views.append(_insufficient(
            "volume", "量价", "放量但价格盘整",
            "K线不足 60 日或量价数据缺失，本轮不判断。", vol_calc))
    else:
        if pos60 >= -0.05 and vr >= 1.5 and range5 <= 0.03:
            state, tone = "放量盘整", "warn"
            note = "高位放量、价格窄幅盘整。历史样本显示它的增量有限，只能作为观察项，不能单独触发。"
        elif pos60 >= -0.05 and vr >= 1.5 and ret5 >= 0.02:
            state, tone = "放量上涨", "warn"
            note = "高位放量且价格继续上涨，更像加速赶顶，但同样不能单独确认顶部。"
        elif vr >= 1.5:
            state, tone = "放量（非高位）", "info"
            note = "成交量放大，但价格不在 60 日高位附近；不作为高位量价信号。"
        else:
            state, tone = "未触发", "ok"
            note = "没有出现明显的放量盘整或放量上涨，量价项不构成观察信号。"
        views.append({
            "key": "volume", "title": "量价",
            "subtitle": "放量但价格盘整（只观察）",
            "state": state, "tone": tone,
            "headline": f"量比 {vr:.2f}，5日振幅 {fmt_pct(range5)}，5日涨跌 {fmt_pct(ret5)}",
            "metrics": [
                ("5日/20日量比", f"{vr:.2f}"),
                ("5日振幅", fmt_pct(range5)),
                ("5日涨跌", fmt_pct(ret5)),
                ("距60日高点", fmt_pct(pos60)),
            ],
            "calc": vol_calc,
            "note": note,
            "wide": False,
        })

    return views


def print_report(r):
    name = f" {r['name']}" if r["name"] else ""
    print("=" * 78)
    print(f"四因子独立观察  |  {r['code']}{name}")
    print(f"基准日 {r['date']}   K线最后交易日 {r['kline']['date']}   "
          f"收盘 {r['close']:.2f}")
    print(f"250日高点 {r['high250']:.2f}（{r['high250_date']}，"
          f"距今 {r['days_since_high']} 个交易日）   "
          f"距高点 {fmt_pct(r['pos250'])}")
    print("说明：以下 4 个因子各自独立展示，程序不合并总分，也不自动判断顶部。")
    for v in r.get("views") or []:
        print("-" * 78)
        print(f"{v['title']}：{v['state']}  |  {v['headline']}")
        if v.get("blocks"):
            for b in v["blocks"]:
                print(f"  [{b.get('title')}] {b.get('subtitle')}")
                for label, value in b.get("results", []):
                    print(f"    结果  {label:<12}{value}")
                if b.get("sources"):
                    print("    数据来源：")
                    for label, value in b["sources"]:
                        print(f"      {label:<20}{value}")
                if b.get("formulas"):
                    print("    计算：")
                    for formula in b["formulas"]:
                        print(f"      {formula}")
        if v.get("metrics"):
            print("  指标：")
            for label, value in v["metrics"]:
                print(f"    {label:<14}{value}")
        if v.get("calc"):
            print("  计算数据：")
            for label, value in v["calc"]:
                print(f"    {label:<14}{value}")
        print(f"  说明：{v['note']}")
    print("-" * 78)
    if r.get("warnings"):
        print("数据与口径提示：")
        for w in r["warnings"]:
            print(f"  · {w}")
    print("=" * 78)
    print("程序只展示 4 个因子的独立结果，不输出总分，不替你做综合判断。")


def _h(v):
    return html.escape("—" if v is None else str(v))


def _num(v, nd=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{nd}f}"
    except Exception:
        return str(v)


HTML_CSS = """
:root{
  --bg:#f3f6fb; --panel:#ffffff; --ink:#162033; --muted:#6b778c;
  --line:#e6ebf3; --navy:#0b1b30; --teal:#0f766e;
  --warn:#b45309; --warn-bg:#fff7e6; --warn-line:#f59e0b;
  --good:#047857; --good-bg:#ecfdf5; --good-line:#10b981;
  --info:#1d4ed8; --info-bg:#eff6ff; --info-line:#60a5fa;
  --ok:#0f766e; --ok-bg:#f0fdfa; --ok-line:#14b8a6;
  --muted-bg:#f1f5f9; --muted-line:#94a3b8;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;background:
    radial-gradient(1100px 500px at 10% -10%,#e0f2fe 0,transparent 55%),
    radial-gradient(900px 500px at 100% 0,#ccfbf1 0,transparent 50%),
    var(--bg);
  color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  line-height:1.6;
}
.page{max-width:1180px;margin:0 auto;padding:28px 20px 44px}
.hero{
  position:relative;overflow:hidden;border-radius:26px;padding:30px 32px 26px;
  background:linear-gradient(130deg,#081524 0%,#0d2b3f 52%,#0f4c52 100%);
  color:#f8fbff;box-shadow:0 24px 60px rgba(8,21,36,.25);
}
.hero:after{
  content:"";position:absolute;right:-120px;top:-150px;width:420px;height:420px;border-radius:50%;
  background:radial-gradient(circle,rgba(45,212,191,.22),transparent 65%);
}
.eyebrow{font-size:11px;letter-spacing:.18em;color:#8ee7dc;font-weight:700;text-transform:uppercase}
.title-row{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin:8px 0 6px;position:relative;z-index:1}
h1{font-size:31px;line-height:1.25;margin:0;letter-spacing:.01em}
.code{font-size:13px;font-weight:600;color:#b9d7e6;background:rgba(255,255,255,.1);
  border:1px solid rgba(255,255,255,.15);padding:3px 9px;border-radius:999px;margin-left:10px;vertical-align:middle}
.meta{font-size:13px;color:#b9c9d8;position:relative;z-index:1}
.meta strong{color:#fff;font-weight:600}
.notice{display:inline-flex;align-items:center;gap:8px;margin-top:14px;padding:7px 12px;border-radius:999px;
  background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.16);color:#d9fff8;font-size:12px;
  position:relative;z-index:1}
.dot{width:7px;height:7px;border-radius:50%;background:#2dd4bf;box-shadow:0 0 0 4px rgba(45,212,191,.15)}
.hero-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:22px;position:relative;z-index:1}
.hero-stat{background:rgba(255,255,255,.075);border:1px solid rgba(255,255,255,.12);border-radius:16px;padding:13px 14px}
.hero-stat label{display:block;font-size:11px;color:#a9c2d2;letter-spacing:.04em}
.hero-stat b{display:block;font-size:21px;color:#fff;margin:3px 0 1px;line-height:1.3}
.hero-stat small{display:block;font-size:11px;color:#a9c2d2}
.hero-stat b.neg{color:#7dd3c7}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:22px 24px;margin-top:18px;
  box-shadow:0 12px 32px rgba(15,23,42,.055)}
.section-head{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:16px}
h2{font-size:19px;line-height:1.3;margin:0 0 4px}
.section-head p{margin:0;font-size:12.5px;color:var(--muted)}
.factor-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:15px}
.factor-card{
  border:1px solid var(--line);border-left:5px solid var(--line);border-radius:16px;padding:17px;
  background:linear-gradient(180deg,#fff,#fbfcff);display:flex;flex-direction:column;gap:12px;
  min-width:0;
}
.factor-card.wide{grid-column:1/-1}
.factor-card.tone-warn{border-left-color:var(--warn-line)}
.factor-card.tone-good{border-left-color:var(--good-line)}
.factor-card.tone-info{border-left-color:var(--info-line)}
.factor-card.tone-ok{border-left-color:var(--ok-line)}
.factor-card.tone-muted{border-left-color:var(--muted-line)}
.factor-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}
.factor-head h3{font-size:17px;line-height:1.3;margin:0}
.factor-head h3 small{display:block;font-size:11px;font-weight:500;color:var(--muted);margin-top:3px}
.state{display:inline-flex;align-items:center;white-space:nowrap;border-radius:999px;padding:4px 10px;
  font-size:12px;font-weight:700;border:1px solid transparent}
.state-warn{color:var(--warn);background:var(--warn-bg);border-color:#fde68a}
.state-good{color:var(--good);background:var(--good-bg);border-color:#a7f3d0}
.state-info{color:var(--info);background:var(--info-bg);border-color:#bfdbfe}
.state-ok{color:var(--ok);background:var(--ok-bg);border-color:#99f6e4}
.state-muted{color:#64748b;background:var(--muted-bg);border-color:#e2e8f0}
.headline{font-size:14px;font-weight:650;color:#243044;margin:0;line-height:1.55}
.metric-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.metric{background:#f7f9fd;border:1px solid #edf1f7;border-radius:11px;padding:8px 10px;min-width:0}
.metric label{display:block;font-size:10.5px;color:var(--muted);line-height:1.4}
.metric b{display:block;font-size:14px;line-height:1.45;word-break:break-word}
.calc{background:#f8fafc;border:1px solid #e8eef6;border-radius:12px;padding:10px 12px}
.calc-title{font-size:11px;font-weight:700;letter-spacing:.06em;color:#64748b;margin-bottom:2px}
.calc-grid{display:grid;grid-template-columns:1fr;gap:0}
.factor-card.wide .calc-grid{grid-template-columns:1fr 1fr;column-gap:26px}
.calc-item{display:flex;justify-content:space-between;gap:14px;padding:5px 0;border-bottom:1px dashed #e2e8f0;
  font-size:11.5px;min-width:0}
.calc-item:last-child{border-bottom:0}
.calc-item span{color:#64748b;flex:0 0 auto}
.calc-item b{color:#1e293b;font-weight:600;text-align:right;word-break:break-word}
.earn-blocks{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.earn-block{border:1px solid #e8eef6;border-top:4px solid #94a3b8;border-radius:14px;background:#fbfcff;padding:12px;min-width:0}
.earn-block.tone-warn{border-top-color:var(--warn-line)}
.earn-block.tone-good{border-top-color:var(--good-line)}
.earn-block.tone-info{border-top-color:var(--info-line)}
.earn-block h4{font-size:15px;margin:0;line-height:1.3}
.earn-block .earn-sub{font-size:10.5px;color:var(--muted);margin:2px 0 8px}
.earn-results{display:grid;gap:5px;margin-bottom:10px}
.earn-result{display:flex;justify-content:space-between;gap:10px;background:#fff;border:1px solid #edf1f7;border-radius:9px;padding:6px 8px;font-size:12px}
.earn-result span{color:#64748b;flex:0 0 auto}
.earn-result b{text-align:right;word-break:break-word}
.earn-title{font-size:10px;font-weight:700;letter-spacing:.06em;color:#64748b;margin:8px 0 4px}
.earn-source-row{display:flex;justify-content:space-between;gap:10px;font-size:11px;padding:3px 0;border-bottom:1px dashed #e2e8f0}
.earn-source-row:last-child{border-bottom:0}
.earn-source-row span{color:#64748b}
.earn-source-row b{text-align:right;font-weight:600;word-break:break-word}
.earn-formula{font-size:10.5px;color:#475569;background:#f1f5f9;border-radius:7px;padding:5px 7px;margin-top:4px;line-height:1.55;word-break:break-word}
.note{font-size:11.5px;color:#64748b;margin:0;line-height:1.65}
.warnings{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.warnings li{background:#fffaf0;border:1px solid #fde9c8;border-radius:10px;padding:9px 11px;font-size:12px;color:#7c4a03}
.empty{background:#f8fafc;border:1px dashed #dbe4f0;border-radius:10px;padding:10px 12px;color:#64748b;font-size:12px}
.foot{margin-top:20px;padding:2px 4px;color:#7c8798;font-size:11.5px;line-height:1.8}
.foot strong{color:#475569}
@media (max-width:960px){
  .factor-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .factor-card.wide{grid-column:1/-1}
  .earn-blocks{grid-template-columns:1fr}
  .hero-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:640px){
  .page{padding:14px 11px 28px}
  .hero{padding:22px 18px;border-radius:20px}
  h1{font-size:24px}
  .code{margin-left:6px}
  .hero-grid{grid-template-columns:1fr 1fr;gap:9px}
  .panel{padding:17px 15px;border-radius:16px}
  .factor-grid{grid-template-columns:1fr}
  .factor-card.wide{grid-column:auto}
  .factor-card.wide .calc-grid{grid-template-columns:1fr}
  .earn-blocks{grid-template-columns:1fr}
}
@media print{
  body{background:#fff}
  .page{max-width:none;padding:0}
  .hero{box-shadow:none;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .panel{box-shadow:none;break-inside:avoid}
  .factor-card{break-inside:avoid}
}
"""


def html_report(r):
    name = r.get("name") or ""
    code = r["code"]
    date = r["date"]
    kline = r.get("kline") or {}
    loc = r.get("local") or {}
    display_name = name if name else code
    code_html = f'<span class="code">{_h(code)}</span>' if name else ""
    views = list(r.get("views") or [])
    views = [v for v in views if not v.get("wide")] + [v for v in views if v.get("wide")]

    lines = []
    add = lines.append
    add("<!DOCTYPE html>")
    add('<html lang="zh-CN">')
    add("<head>")
    add('<meta charset="utf-8">')
    add('<meta name="viewport" content="width=device-width, initial-scale=1">')
    add(f"<title>四因子观察 | {_h(code)} {_h(display_name)} | {_h(date)}</title>")
    add(f"<style>{HTML_CSS}</style>")
    add("</head>")
    add("<body>")
    add('<div class="page">')

    add('<header class="hero">')
    add('<div class="eyebrow">Factor Watch · 四因子独立观察</div>')
    add('<div class="title-row">')
    add(f"<h1>{_h(display_name)}{code_html}</h1>")
    add("</div>")
    add(f'<div class="meta">基准日 <strong>{_h(date)}</strong> · K线最后交易日 '
        f'{_h(kline.get("date"))} · 收盘 <strong>{_h(_num(r.get("close")))}</strong></div>')
    add('<div class="notice"><span class="dot"></span>'
        '4 个因子各自独立展示，不合并总分，不自动判断顶部</div>')
    add('<div class="hero-grid">')
    add(f'''<div class="hero-stat"><label>250日高点</label><b>{_h(_num(r.get("high250")))}</b>
      <small>{_h(r.get("high250_date"))} · 距今 {_h(r.get("days_since_high"))} 个交易日</small></div>''')
    pos_cls = "neg" if (r.get("pos250") is not None and r["pos250"] < 0) else ""
    add(f'''<div class="hero-stat"><label>距250日高点</label><b class="{pos_cls}">{_h(fmt_pct(r.get("pos250")))}</b>
      <small>只作位置背景，不是顶部结论</small></div>''')
    add(f'''<div class="hero-stat"><label>距60日高点</label><b>{_h(fmt_pct(loc.get("pos60")))}</b>
      <small>{_h(loc.get("high60_date") or "—")}</small></div>''')
    fm = r.get("finance") or {}
    add(f'''<div class="hero-stat"><label>最新报告期</label><b>{_h(fm.get("latest_end") or "—")}</b>
      <small>业绩背景，不参与顶部判断</small></div>''')
    add("</div>")
    add("</header>")

    add('<section class="panel">')
    add('<div class="section-head"><div><h2>四因子独立观察</h2>'
        '<p>每张卡片只描述自己的维度；程序不做加权、不合计、不替你做综合判断。</p></div></div>')
    add('<div class="factor-grid">')
    for v in views:
        wide = " wide" if v.get("wide") else ""
        tone = v.get("tone") or "muted"
        add(f'<article class="factor-card{tone and " tone-" + tone}{wide}">')
        add('<div class="factor-head">')
        add(f'<h3>{_h(v.get("title"))}<small>{_h(v.get("subtitle"))}</small></h3>')
        add(f'<span class="state state-{_h(tone)}">{_h(v.get("state"))}</span>')
        add("</div>")
        add(f'<p class="headline">{_h(v.get("headline"))}</p>')
        if v.get("blocks"):
            add('<div class="earn-blocks">')
            for b in v["blocks"]:
                add(f'<section class="earn-block tone-{_h(b.get("tone", "info"))}">')
                add(f'<h4>{_h(b.get("title"))}</h4>')
                add(f'<div class="earn-sub">{_h(b.get("subtitle"))}</div>')
                add('<div class="earn-results">')
                for label, value in b.get("results", []):
                    add(f'<div class="earn-result"><span>{_h(label)}</span><b>{_h(value)}</b></div>')
                add('</div>')
                if b.get("sources"):
                    add('<div class="earn-title">数据来源</div>')
                    for label, value in b["sources"]:
                        add(f'<div class="earn-source-row"><span>{_h(label)}</span><b>{_h(value)}</b></div>')
                if b.get("formulas"):
                    add('<div class="earn-title">计算</div>')
                    for formula in b["formulas"]:
                        add(f'<div class="earn-formula">{_h(formula)}</div>')
                add('</section>')
            add('</div>')
        if v.get("metrics"):
            add('<div class="metric-grid">')
            for label, value in v["metrics"]:
                add(f'<div class="metric"><label>{_h(label)}</label><b>{_h(value)}</b></div>')
            add("</div>")
        if v.get("calc"):
            add('<div class="calc"><div class="calc-title">计算数据</div><div class="calc-grid">')
            for label, value in v["calc"]:
                add(f'<div class="calc-item"><span>{_h(label)}</span><b>{_h(value)}</b></div>')
            add("</div></div>")
        add(f'<p class="note">{_h(v.get("note"))}</p>')
        add("</article>")
    add("</div>")
    add("</section>")

    add('<section class="panel">')
    add('<div class="section-head"><div><h2>数据与口径</h2>'
        '<p>每个因子独立判断；数据不足时该卡片显示“数据不足，不判断”。</p></div></div>')
    if r.get("warnings"):
        add('<ul class="warnings">')
        for w in r["warnings"]:
            add(f"<li>{_h(w)}</li>")
        add("</ul>")
    else:
        add('<div class="empty">无额外数据提示。</div>')
    add("</section>")

    add('<div class="foot"><strong>说明</strong>：本页只展示动量、换手、业绩、量价四个因子的独立结果，'
        '不合并总分，不输出顶部结论，也不构成买卖建议。<br>'
        '数据来源：腾讯自选股（westock）日 K、财务数据；财务按披露日过滤，结果仅供研究参考。</div>')
    add("</div>")
    add("</body>")
    add("</html>")
    return "\n".join(lines) + "\n"


def write_html_file(r):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"top_{r['code']}_{r['date']}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_report(r))
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="单只股票四因子独立观察（不合并总分，不筛选）")
    ap.add_argument("code", help="股票代码：sh600519 / 600519 / 600519.SH")
    ap.add_argument("--date", help="基准交易日，默认今天（YYYY-MM-DD）")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新拉取")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--verbose", action="store_true",
                    help="打印执行过程和完整命令行报告（默认只生成文件）")
    ap.add_argument("--quiet", action="store_true",
                    help="不输出任何命令行内容，只写结果文件")
    ap.add_argument("--no-html", "--no-md", dest="no_html", action="store_true",
                    help="不生成 HTML 结果文件（--no-md 为兼容旧用法）")
    a = ap.parse_args(argv)

    global LOG_STREAM, QUIET
    QUIET = a.quiet or not a.verbose
    if a.json:
        LOG_STREAM = sys.stderr

    code = a.code.strip().lower()
    if "." in code:
        left, right = code.split(".", 1)
        code = right + left if right in ("sh", "sz", "bj") else left
    if not code.startswith(("sh", "sz", "bj")):
        if code.startswith(("5", "6", "9")):
            code = "sh" + code
        elif code.startswith(("0", "1", "2", "3")):
            code = "sz" + code
        elif code.startswith(("4", "8")):
            code = "bj" + code
    end = a.date or dt.date.today().isoformat()
    log(f"[开始] {code}，基准日 {end}")

    try:
        r = evaluate(code, end, a.refresh)
    except BrokenPipeError:
        raise
    except Exception as e:
        sys.stderr.write(f"判断失败：{e}\n")
        return 1

    if a.json:
        out = {k: v for k, v in r.items()
               if k not in ("items", "quote", "score", "verdict",
                            "position", "summary", "drivers", "phase",
                            "score_mode", "percentile", "percentile_n",
                            "volume")}
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        sys.stdout.flush()
    elif a.verbose:
        print_report(r)
        sys.stdout.flush()

    if not a.no_html:
        try:
            html_path = write_html_file(r)
            if not a.quiet and not a.json:
                print(f"[输出] HTML：{html_path}")
                sys.stdout.flush()
        except Exception as e:
            sys.stderr.write(f"[输出] HTML 写入失败：{e}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)
