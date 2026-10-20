#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K线选股规则 v2.3 —— 每周选股（扫描周期 K = 5 个交易日）

规则说明见同目录 K线选股规则_v2.md
v2 的含义 = 加入了「超级成长股基因」的标注工作（见下），选股参数与裁决条件未变。

目录结构：
    /home/kevin/week_select_stocks/
    ├── K线选股规则_v2.md
    ├── screen_kline.py          本程序
    ├── database/                从平台下载的原始数据（按日期命名）
    │   ├── cross_<起>_<止>.json     横截面粗筛原始返回
    │   ├── labels_<起>_<止>.json    排除集原始返回（ST + 次新分桶）
    │   ├── kline_<起>_<止>.json     候选并集的日K线原始数据
    │   ├── quote_<基准日>.json      最终名单的行情快照
    │   ├── gene_snapshot_<基准日>.json  基因标注用的全市场快照
    │   ├── gene_industry_map.json   申万二级行业 → 个股映射（长期缓存）
    │   └── result_<基准日>.json     本次运行结果与逐条剔除明细（便于追溯）
    └── csv/
        └── 选股_<基准日>.csv         按规则选出的股票

用法：
    python3 screen_kline.py                 # 跑最近交易日
    python3 screen_kline.py --date 2026-10-14
    python3 screen_kline.py --refresh       # 强制重新下载原始数据
    python3 screen_kline.py --n 2           # 每3个交易日（窗口必须=周期−1）

退出码：0 正常 ｜ 1 出错（含原始数据下载失败）

超级成长基因标注（v2，即规则文档第九节）：
    在已选出的股票后面加一列「超级成长基因」（是/否/无数据），仅作标注，
    **不筛选、不排序、不影响入选名单**。下列条件全部成立才算「是」：
      ① 弹性系数   归母净利同比% ÷ 营收同比%      > 1
         前提：归母净利 > 0（亏损时"同比"符号无意义）
         前提：营收同比 ≥ 5%（分母趋零时比率会把噪声放大成信号）
      ② 固定资产占比 (固定资产 + 在建工程)/总资产   > 20%
      ③ 行业扩产强度 Σ行业在建工程 ÷ Σ行业固定资产  < 20%
         前提：行业营收增速 > 0（萎缩行业里的低扩产不是瓶颈，是价值陷阱）
    ⚠️ ①的"归母净利>0"与③的"行业营收>0"不是额外筛选，而是指标本身有意义的前提。
       首版漏掉它们，实测把亏损的长春燃气、营收仅+1.12%的新世界误标为「是」。
    基因数据获取失败时该列留空（不标「否」），并在 stderr 给出告警——
    选股结果不受影响，但不得把「失败」误读成「已确认没有基因」。

⚠️ 修改登记规范（2026-10-15 15:47 起）：
    本程序的每次修改，都必须在 K线选股规则_v2.md 的「十、版本沿革」表中
    登记「修改时间」（精确到分钟）与变更内容。未登记时间的修改视为不合规。
    最近一次：2026-10-16 11:55（v2.3，据 18 周回测调参：C2 0.5%→1%，基因③ 25%→20%）
    上一条　：2026-10-16 09:50（v2.1，基因指标补三个前提条件）
    上上条　：2026-10-16 09:42（v2.0，文档改名 v1→v2，程序内引用同步；逻辑未变）
    更早　　：2026-10-16 09:39（v1.3，新增「超级成长基因」标注列）
    　　　　：2026-10-15 15:49（v1.2，文件头补本条规范）
    　　　　：2026-10-15 15:38（v1.1，补 nf is None 防线 + CROSS_TPL 注释）
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "database")
CSVDIR = os.path.join(HERE, "csv")

W = os.path.expanduser("~/.local/bin/westock")

# ---------------- 规则参数（见 K线选股规则_v2.md 第三节） ----------------
K_VOL = 1.75          # ② 启动日放量倍数
CHG_LO, CHG_HI = 2.0, 9.5     # ③ 当日涨幅区间 %
FLOW_MIN = 0.2        # ③ 主力净流入 / 流通市值 下限 %
C3_RATIO = 0.98       # ④ MA20 >= MA60 × 该值
C2_TOL = 0.01         # ④ MA20 近5日跌幅容忍度（v2.3 由 0.005 放宽；18周实测 0.005 偏紧）
MA60_GAP = 0.0        # 条件9 close/MA60 上限；0 = 关闭（已评估为失效）
LISTED_YEARS = 3      # ⑤ 上市满 N 年
KLINE_BATCH = 20      # westock kline 单批上限
LABEL_PAGE = 500
KLINE_LOOKBACK_DAYS = 300   # K线回溯长度（自然日），保证 MA60 与启动日检测有足够历史

# ---------------- 超级成长基因标注（只标注，不参与任何裁决） ----------------
# 定位：在已选出的股票后面标注「是否具备超级成长股的基因底子」，
#       供人工精筛参考。**它不筛选、不排序、不影响入选名单**。
# 三个指标来自「周期躯壳 × 成长内核」框架，实测中区分度最高的三条：
#   ① 弹性系数   归母净利同比% ÷ 营收同比%        —— 经营杠杆是否在兑现
#   ② 固定资产占比 (固定资产 + 在建工程) / 总资产  —— 是否具备经营杠杆的物理基础
#   ③ 行业扩产强度 Σ行业在建工程 ÷ Σ行业固定资产   —— 行业供给是否在填平瓶颈（越低越好）
GENE_ELASTIC_MIN = 1.0      # ① 下限：利润增速必须快于营收增速
GENE_REV_GROWTH_MIN = 5.0   # ① 定义域下限：营收同比太小时弹性系数是噪声放大
                            #    （实测反例：新世界营收 +1.12%、利润 +4.2% → 弹性 3.78，
                            #      但两个增速都是噪声，且它是一家百货公司）
GENE_FA_MIN = 0.20          # ② 下限
GENE_IND_SUPPLY_MAX = 20.0  # ③ 上限（%）：行业在疯狂扩产则瓶颈将被填平
                            #    v2.3 由 25% 收紧为 20%。18周实测：放宽到 40% 命中只多 19%，
                            #    说明 25% 几乎不约束；20% 才真正剔除明显过剩的行业
GENE_IND_REV_GROWTH_MIN = 0.0  # ③ 前提：行业营收必须正增长。否则「低扩产强度」不是供给
                               #    瓶颈而是价值陷阱——没人愿意投资的衰退行业
                               #    （实测反例：长春燃气所在燃气Ⅱ行业营收 −5.05%）
GENE_BATCH = 12             # sector constituent 单批行业数
GENE_MIN_IND_N = 5          # 行业成员少于此数不做行业聚合（样本太小）

# 基因快照分两批拉取：条件引擎对 null 字段（如「没有在建工程」）一律判假，
# 若把 TConstruInProcess 写进同一个表达式，会把 600+ 只静默丢掉。
# 因此单独拉一批，缺失即视为 0（null = 没有在建工程，不是「未知」）。
GENE_TPL_A = ("intersect([TotalAssets > 0, OperatingRevenue > 0, TotalFixedAsset >= 0, "
              "NPParentCompanyOwners > -1e15, OperatingRevenueGrowRate > -1e15, "
              "NPParentCompanyYOY > -1e15])")
GENE_TPL_B = ("intersect([TotalAssets > 0, OperatingRevenue > 0, TConstruInProcess >= 0])")

# ⚠️ 表达式里的 `MainNetFlow > 0` 有两个作用，**不可删除**：
#    ① 作为筛选条件，它确实被「净流入 ÷ 流通市值 ≥ 下限」蕴含（流通市值恒正）；
#    ② 更关键的是「字段请求」——服务端**只返回表达式引用到的列**。删掉它，
#       返回表里就没有 MainNetFlow 字段，本地市值比全部算不出来，名单会被
#       静默清空（实测：粗筛池 703→924、入选 10→0，且拒绝明细看起来完全正常）。
#    同理，任何字段（ChangePCT / MainNetFlow5D …）一旦不再被引用就会从返回里消失。
#    改动本表达式后必须重跑验证；main() 里有对应防线会直接报错。
CROSS_TPL = ("intersect([ChangePCT >= {lo}, ChangePCT <= {hi}, MainNetFlow > 0, "
             "MainNetFlow5D > 0, IsDelisted == 0, Ifsuspend == 0])")
NEW_LABELS = ["listeddate_5days", "listeddate_3mons",
              "listeddate_1year", "listeddate_3year"]

CSV_HEADER = ["基准交易日", "序号", "代码", "名称", "启动日", "距启动交易日",
              "启动日量能倍数", "启动日涨幅%", "启动日净流入(万元)",
              "5日净流入(万元)", "净流入占流通市值%", "收盘价",
              "距MA20%", "距MA60%", "MA20", "MA60", "MA20÷MA60",
              "流通市值(亿)", "总市值(亿)", "PE", "PB", "换手率%",
              "超级成长基因"]


# ---------------- 基础工具 ----------------

def run(args):
    p = subprocess.run(args, capture_output=True, timeout=900)
    return p.stdout.decode("utf-8", "replace")


def md(txt):
    """解析 westock CLI 的 markdown 表格"""
    tbl = [l for l in txt.splitlines() if l.strip().startswith("|")]
    if len(tbl) < 2:
        return []
    hdr = [x.strip() for x in tbl[0].strip().strip("|").split("|")]
    out = []
    for ln in tbl[1:]:
        c = [x.strip() for x in ln.strip().strip("|").split("|")]
        if len(c) == len(hdr) and not all(set(v) <= set("-: ") for v in c):
            out.append(dict(zip(hdr, c)))
    return out


def retry(fn, tries=4, base=1.5, what=""):
    """返回 None 表示彻底失败（调用方必须处理，不得静默继续）"""
    for i in range(tries):
        try:
            r = fn()
            if r:
                return r
        except Exception:
            pass
        if i < tries - 1:
            time.sleep(base * (i + 1))
    sys.stderr.write(f"  [失败] {what}（重试 {tries} 次）\n")
    return None


def ma(v, n, i):
    return sum(v[i - n + 1:i + 1]) / n if i >= n - 1 else None


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cap_to_yuan(v):
    """⚠️ quote 的市值字段单位随 --date 变化：无 --date 为亿元，有 --date 为元。
    这里按数量级统一归一化为「元」。"""
    if v is None:
        return None
    return v if v > 1e6 else v * 1e8


def jload(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def jdump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False)


# ---------------- 数据获取（含落盘） ----------------

def get_trading_days(end, refresh, span="2026-06-01"):
    """单文件缓存；已覆盖到 end 就直接复用，避免每天生成新文件"""
    path = os.path.join(DB, "trading_calendar.json")
    cached = jload(path, {})
    if not refresh and cached.get("end") == end and cached.get("days"):
        return cached["days"]
    rows = retry(lambda: md(run([W, "trade-calendar", "--start", span, "--end", end,
                                 "--trading-only", "--limit", "500"])),
                 what="交易日历")
    if rows is None:
        raise RuntimeError("交易日历获取失败，终止")
    ds = sorted(r["date"] for r in rows if r.get("date") and r["date"] <= end)
    jdump(path, {"end": end, "days": ds})
    return ds


def get_cross(day, lo, hi, refresh):
    """横截面粗筛，逐日单独落盘"""
    path = os.path.join(DB, f"cross_{day}.json")
    if os.path.exists(path) and not refresh:
        return jload(path, [])
    rows = retry(lambda: md(run([W, "screen", "condition", "--expression",
                                 CROSS_TPL.format(lo=lo, hi=hi),
                                 "--date", day, "--limit", "6000"])),
                 what=f"横截面粗筛 {day}")
    if rows is None:
        raise RuntimeError(f"横截面粗筛失败：{day}（不得据此产出名单）")
    jdump(path, rows)
    time.sleep(0.8)
    return rows


def get_labels(day, refresh):
    path = os.path.join(DB, f"labels_{day}.json")
    if os.path.exists(path) and not refresh:
        return jload(path, {"st": [], "new": []})
    st, new = set(), set()
    for L in ["risk_st"] + NEW_LABELS:
        off = 0
        while True:
            rows = retry(lambda: md(run([W, "screen", "label", "--type", L,
                                         "--date", day, "--limit", str(LABEL_PAGE),
                                         "--offset", str(off)])),
                         what=f"标签 {L} @ {day}")
            if rows is None:
                # 静默继续会导致次新股漏剔，必须让整次运行失败
                raise RuntimeError(f"排除集拉取失败：{L} @ {day}")
            (st if L == "risk_st" else new).update(
                r["code"] for r in rows if r.get("code"))
            if len(rows) < LABEL_PAGE:
                break
            off += LABEL_PAGE
        time.sleep(0.4)
    out = {"st": sorted(st), "new": sorted(new)}
    jdump(path, out)
    return out


def get_kline(codes, beg, end, refresh):
    span_desc = f"{beg}_{end}"
    path = os.path.join(DB, f"kline_{span_desc}.json")
    km = {} if refresh else jload(path, {})
    todo = [c for c in codes if c not in km]
    chunks = [todo[i:i + KLINE_BATCH] for i in range(0, len(todo), KLINE_BATCH)]
    for n, ch in enumerate(chunks, 1):
        rows = retry(lambda: md(run([W, "kline", ",".join(ch), "--start", beg,
                                     "--end", end, "--limit", "400"])),
                     what=f"日K线 第{n}批")
        if rows is None:
            raise RuntimeError(f"日K线拉取失败（第 {n} 批），终止以免产出不可信名单")
        for r in rows:
            c = r.get("code") or (ch[0] if len(ch) == 1 else None)
            if not c:
                continue
            try:
                km.setdefault(c, []).append(
                    (r["date"], float(r["last"]), float(r["volume"])))
            except (KeyError, ValueError):
                continue
        for k in km:
            km[k].sort(key=lambda b: b[0])
        jdump(path, km)
        sys.stderr.write(f"\r  日K线 {n}/{len(chunks)} 批  库内 {len(km)} 只")
        sys.stderr.flush()
        time.sleep(0.4)
    if chunks:
        sys.stderr.write("\n")

    # 批量拉取时单只失败会被静默跳过 —— 逐只补取，避免丢失合格股票
    for rnd in range(2):
        missing = [c for c in codes if c not in km]
        if not missing:
            break
        sys.stderr.write(f"  补取缺失的 {len(missing)} 只（第 {rnd+1} 轮）...\n")
        still = []
        for c in missing:
            rows = retry(lambda: md(run([W, "kline", c, "--start", beg, "--end", end,
                                         "--limit", "400"])), tries=2, what=f"补取 {c}")
            if rows:
                r = rows[0]
                try:
                    km[c] = sorted([(r["date"], float(r["last"]), float(r["volume"]))],
                                   key=lambda b: b[0])
                except (KeyError, ValueError):
                    still.append(c)
            else:
                still.append(c)
            jdump(path, km)
            time.sleep(0.4)
        if not still:
            break
    for k in km:
        km[k].sort(key=lambda b: b[0])
    jdump(path, km)
    return km, [c for c in codes if c not in km]


def get_quotes(codes, day, refresh):
    path = os.path.join(DB, f"quote_{day}.json")
    if os.path.exists(path) and not refresh:
        cached = jload(path, {})
        if all(c in cached for c in codes):
            return cached
        out = dict(cached)
    else:
        out = {}
    for i in range(0, len(codes), 20):
        ch = [c for c in codes[i:i + 20] if c not in out]
        if not ch:
            continue
        # 必须传 --date，否则取到实时值，与基准日口径不一致
        rows = retry(lambda: md(run([W, "quote", ",".join(ch), "--date", day])),
                     tries=2, what="行情")
        for r in (rows or []):
            c = r.get("code") or r.get("symbol") or (ch[0] if len(ch) == 1 else None)
            if c:
                out[c] = r
        jdump(path, out)
        time.sleep(0.5)
    return out


# ---------------- 超级成长基因标注（独立于上面的裁决） ----------------

def get_industry_map(refresh):
    """申万二级行业 → 个股映射。长期缓存（行业归属变化很慢），--refresh 强制更新。
    失败返回 None（调用方负责提示，不得静默当作「没有基因」）。"""
    path = os.path.join(DB, "gene_industry_map.json")
    if os.path.exists(path) and not refresh:
        m = jload(path, {})
        if m:
            return m
    rows = retry(lambda: md(run([W, "sector", "ranking"])), what="申万行业清单")
    if not rows:
        return None
    codes = [r["code"] for r in rows
             if r.get("code", "").startswith("pt0") and r.get("name")]
    if not codes:
        return None
    mapping = {}
    for i in range(0, len(codes), GENE_BATCH):
        ch = codes[i:i + GENE_BATCH]
        txt = retry(lambda: run([W, "sector", "constituent", ",".join(ch),
                                 "--limit", "0"]),
                    what=f"行业成分股 第{i // GENE_BATCH + 1}批")
        if txt is None:
            return None
        cur = None
        for ln in txt.splitlines():
            s = ln.strip()
            if "成分股-" in s and "[" in s:
                cur = s.split("成分股-", 1)[1].split("[", 1)[0].strip()
            if not s.startswith("|"):
                continue
            c = [x.strip() for x in s.strip("|").split("|")]
            if len(c) < 2 or c[0] in ("code", "") or all(set(v) <= set("-: ") for v in c):
                continue
            if cur and c[0][:2] in ("sh", "sz", "bj"):
                mapping[c[0]] = cur
        time.sleep(0.3)
    if not mapping:
        return None
    jdump(path, mapping)
    return mapping


def get_gene_snapshot(day, refresh):
    """全市场基因原始数据（按基准日缓存）。失败返回 None。"""
    path = os.path.join(DB, f"gene_snapshot_{day}.json")
    if os.path.exists(path) and not refresh:
        cached = jload(path, None)
        if cached:
            return cached
    ra = retry(lambda: md(run([W, "screen", "condition", "--expression", GENE_TPL_A,
                               "--date", day, "--limit", "8000"])),
               what=f"基因快照A {day}")
    if not ra:
        return None
    rb = retry(lambda: md(run([W, "screen", "condition", "--expression", GENE_TPL_B,
                               "--date", day, "--limit", "8000"])),
               what=f"基因快照B {day}")
    if not rb:
        return None
    out = {"a": ra,
           "b": {r["code"]: r.get("TConstruInProcess") for r in rb if r.get("code")}}
    jdump(path, out)
    return out


def compute_gene(day, refresh):
    """返回 {code: {elastic, fa_ratio, ind_supply, gene, ...}}；失败返回 None。"""
    snap = get_gene_snapshot(day, refresh)
    if snap is None:
        return None
    imap = get_industry_map(refresh)
    if imap is None:
        return None
    info = {}
    for r in snap["a"]:
        c = r.get("code")
        if not c:
            continue
        ta = fnum(r.get("TotalAssets"))
        fa = fnum(r.get("TotalFixedAsset"))
        rg = fnum(r.get("OperatingRevenueGrowRate"))
        pg = fnum(r.get("NPParentCompanyYOY"))
        rev = fnum(r.get("OperatingRevenue"))
        np_ = fnum(r.get("NPParentCompanyOwners"))
        if None in (ta, fa, rg, pg, rev, np_) or ta <= 0 or rev <= 0:
            continue
        cip = fnum(snap["b"].get(c))
        if cip is None:
            cip = 0.0        # ⚠️ null = 没有在建工程，不是「未知」
        info[c] = {"name": r.get("name"),
                   "ind": imap.get(c, "未分类"),
                   "rev": rev, "np": np_, "rg": rg, "pg": pg,
                   "fa_ratio": (fa + cip) / ta,
                   "elastic": (pg / rg) if rg > 0 else None,
                   "cip": cip, "fa": fa}
    # ③ 行业聚合：扩产强度 = Σ在建 ÷ Σ固资；行业营收增速 = 营收加权
    agg = {}
    for c, v in info.items():
        g = agg.setdefault(v["ind"], [0.0, 0.0, 0, 0.0, 0.0])
        g[0] += v["cip"]
        g[1] += v["fa"]
        g[2] += 1
        g[3] += v["rev"] * v["rg"]
        g[4] += v["rev"]
    supply, ind_rg = {}, {}
    for k, v in agg.items():
        ok = v[2] >= GENE_MIN_IND_N
        supply[k] = (v[0] / v[1] * 100.0) if (ok and v[1] > 0) else None
        ind_rg[k] = (v[3] / v[4]) if (ok and v[4] > 0) else None
    for v in info.values():
        v["ind_supply"] = supply.get(v["ind"])
        v["ind_rev_growth"] = ind_rg.get(v["ind"])
        v["gene"] = bool(
            v["np"] > 0                              # ① 必须盈利：亏损时「同比」的符号无意义
            and v["elastic"] is not None
            and v["elastic"] > GENE_ELASTIC_MIN
            and v["rg"] >= GENE_REV_GROWTH_MIN       # ① 分母不能太小，否则比值是噪声放大
            and v["fa_ratio"] > GENE_FA_MIN          # ②
            and v["ind_supply"] is not None
            and v["ind_supply"] < GENE_IND_SUPPLY_MAX    # ③
            and v["ind_rev_growth"] is not None
            and v["ind_rev_growth"] > GENE_IND_REV_GROWTH_MIN)  # ③ 前提：行业须在扩张
    return info


# ---------------- 裁决 ----------------

def find_start(code, name, bars, S):
    """在 S 日回溯该股的启动日 d*。失败时返回 (None, 原因)"""
    if not bars:
        return None, "无K线数据"
    i = next((j for j, x in enumerate(bars) if x[0] == S), None)
    if i is None:
        return None, f"K线无 {S} 数据"
    if i < 70:
        return None, f"K线历史不足({i}根)"
    cl = [x[1] for x in bars]
    vo = [x[2] for x in bars]
    up = 0
    for j in range(i, 19, -1):
        m = ma(cl, 20, j)
        if m and cl[j] > m:
            up += 1
        else:
            break
    if up == 0:
        return None, "未站上 MA20"
    ci = i - (up - 1)
    return {"code": code, "name": name, "d_star": bars[ci][0], "age": i - ci,
            "close": cl[i], "ma20": ma(cl, 20, i), "ma60": ma(cl, 60, i),
            "cl_s": cl[ci], "m20s": ma(cl, 20, ci), "m60s": ma(cl, 60, ci),
            "vcross": (vo[ci] / (sum(vo[ci - 5:ci]) / 5)
                       if ci >= 5 and sum(vo[ci - 5:ci]) else 0),
            "_ci": ci, "_i": i}, ""


def judge(det, bars, S, N, cross_by_day, st_by_day, new_by_day,
          k_vol, gap, c2, c3):
    c = det["code"]
    d0 = det["d_star"]
    if det["age"] > N:
        return False, f"⑦ 距启动{det['age']}日 > N={N}"
    if det["vcross"] < k_vol:
        return False, f"② 启动日放量不足({det['vcross']:.2f}<{k_vol})"
    row0 = cross_by_day.get(d0, {}).get(c)
    if row0 is None:
        return False, f"③ 启动日({d0})未通过日频条件"
    if det["m20s"] is None or det["m60s"] is None:
        return False, "④ 启动日无均线数据"
    if det["m20s"] < det["m60s"] * c3:
        return False, f"④ 启动日 MA20 < MA60×{c3}"
    cl = [x[1] for x in bars]
    m20s5 = ma(cl, 20, det["_ci"] - 5) if det["_ci"] >= 5 else None
    if c2 is not None and (m20s5 is None or det["m20s"] <= m20s5 * (1 - c2)):
        return False, f"④ 启动日 MA20 近5日跌逾{c2*100}%"
    if gap and det["cl_s"] / det["m60s"] >= gap:
        return False, f"④ 启动日 距MA60 ≥ {gap}"
    if c in st_by_day.get(d0, set()):
        return False, "⑤ ST"
    if c in new_by_day.get(d0, set()):
        return False, "⑤ 次新（未满3年）"
    for j in range(det["_ci"] + 1, det["_i"] + 1):
        m = ma(cl, 20, j)
        if not m or cl[j] <= m:
            return False, "⑥ 启动后跌破 MA20"
    return True, ""


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="K线选股规则 v1（每周选股，K=5）")
    ap.add_argument("--date", help="基准交易日（默认最近交易日）")
    ap.add_argument("--n", type=int, default=4,
                    help="新鲜度窗口，必须 = 扫描周期−1（每周 K=5 用 4=默认；"
                         "每3日用 2；每日用 0）")
    ap.add_argument("--k-vol", type=float, default=K_VOL)
    ap.add_argument("--chg-lo", type=float, default=CHG_LO)
    ap.add_argument("--chg-hi", type=float, default=CHG_HI)
    ap.add_argument("--flow-ratio", type=float, default=FLOW_MIN)
    ap.add_argument("--ma60-gap", type=float, default=MA60_GAP)
    ap.add_argument("--refresh", action="store_true",
                    help="强制重新下载原始数据（默认复用 database/ 下的已有文件）")
    a = ap.parse_args()

    if not os.path.exists(W):
        print(f"错误：找不到 westock CLI（{W}）", file=sys.stderr)
        return 1
    os.makedirs(DB, exist_ok=True)
    os.makedirs(CSVDIR, exist_ok=True)

    print("[1/6] 交易日历 ...", flush=True)
    today = time.strftime("%Y-%m-%d")
    days = get_trading_days(today, a.refresh)
    if not days:
        print("错误：交易日历为空", file=sys.stderr)
        return 1
    S = a.date or days[-1]
    hist = [d for d in days if d <= S]
    if len(hist) < a.n + 1:
        print(f"错误：基准日 {S} 之前交易日不足（需要 {a.n+1} 天）", file=sys.stderr)
        return 1
    need = hist[-(a.n + 1):]
    span = f"{need[0]}_{need[-1]}"
    print(f"      基准日 {S}，扫描周期 K={a.n+1}，覆盖 {need[0]} ~ {need[-1]}"
          f"（{len(need)} 个交易日）")

    print(f"[2/6] 横截面粗筛（{len(need)} 天）...", flush=True)
    cross_by_day = {}
    for d in need:
        rows = get_cross(d, a.chg_lo, a.chg_hi, a.refresh)
        cross_by_day[d] = {r["code"]: r for r in rows if r.get("code")}
        print(f"      {d}: {len(cross_by_day[d])} 只")

    print("[3/6] 排除集（ST + 次新分桶）...", flush=True)
    st_by_day, new_by_day = {}, {}
    for d in need:
        lb = get_labels(d, a.refresh)
        st_by_day[d] = set(lb["st"])
        new_by_day[d] = set(lb["new"])
    print(f"      ST {len(st_by_day[need[-1]])} 只，"
          f"次新 {len(new_by_day[need[-1]])} 只")

    pool = sorted({c for d in need for c in cross_by_day[d]})
    print(f"[4/6] 日K线：候选并集 {len(pool)} 只 ...", flush=True)
    kbeg = (dt.date.fromisoformat(need[0])
            - dt.timedelta(days=KLINE_LOOKBACK_DAYS)).isoformat()
    km, km_missing = get_kline(pool, kbeg, S, a.refresh)
    hit_cnt = sum(1 for c in pool if km.get(c))
    print(f"      命中 {hit_cnt}/{len(pool)}")
    if km_missing:
        print(f"      ⚠️ {len(km_missing)} 只取不到K线，将按「无K线数据」剔除："
              f"{', '.join(km_missing[:8])}{'…' if len(km_missing) > 8 else ''}")

    print("[5/6] 逐条裁决 ...", flush=True)
    hit, rej = [], {}
    for c in pool:
        name = next((cross_by_day[d][c].get("name") for d in need
                     if c in cross_by_day[d]), "")
        det, why0 = find_start(c, name, km.get(c, []), S)
        if det is None:
            rej[why0] = rej.get(why0, 0) + 1
            continue
        ok, why = judge(det, km[c], S, a.n, cross_by_day, st_by_day, new_by_day,
                        a.k_vol, a.ma60_gap, C2_TOL, C3_RATIO)
        if ok:
            hit.append(det)
        else:
            k = re.sub(r"[\d.]+", "#", why)      # 归一化，便于统计
            rej[k] = rej.get(k, 0) + 1

    print("[6/6] 取行情 / 计算市值比 / 写结果 ...", flush=True)
    q = get_quotes([x["code"] for x in hit], S, a.refresh) if hit else {}
    # ⚠️ 行情缺失会让「市值比」算不出来；绝不能因此静默剔除（会悄悄丢掉合格股票）
    if hit:
        missing = [x["code"] for x in hit
                   if not cap_to_yuan(fnum((q.get(x["code"]) or {})
                                           .get("circulating_market_cap")))]
        if missing:
            raise RuntimeError(
                f"行情数据缺失 {len(missing)}/{len(hit)} 只（如 {missing[:5]}）。"
                f"市值比无法计算，终止以免产出不可信名单。"
                f"可重跑一次或加 --refresh。")
    # ⚠️ 同上一道防线，但针对「启动日净流入」字段：字段缺失时市值比同样算不出来。
    #    典型成因是 CROSS_TPL 不再引用 MainNetFlow —— 服务端只返回被引用的列，
    #    于是一切 nf=None、ratio=None，若不在此处报错，就会全部落进下面
    #    「③ 启动日市值比 < 下限」的拒绝分支，得到一份「入选 0 只」却看不出异常的名单。
    if hit:
        noflow = [x["code"] for x in hit
                  if fnum(((cross_by_day.get(x["d_star"]) or {})
                           .get(x["code"]) or {}).get("MainNetFlow")) is None]
        if noflow:
            raise RuntimeError(
                f"启动日净流入字段缺失 {len(noflow)}/{len(hit)} 只（如 {noflow[:5]}）。"
                f"市值比无法计算，终止以免产出不可信名单。"
                f"请检查 CROSS_TPL 是否仍引用 MainNetFlow"
                f"（服务端只返回表达式引用到的列）。")
    final, rej2 = [], {}
    for x in hit:
        r = q.get(x["code"], {})

        def num(k):
            return fnum(r.get(k))
        nf = fnum(cross_by_day[x["d_star"]][x["code"]].get("MainNetFlow"))
        nf5 = fnum(cross_by_day[x["d_star"]][x["code"]].get("MainNetFlow5D"))
        chg = fnum(cross_by_day[x["d_star"]][x["code"]].get("ChangePCT"))
        fc = cap_to_yuan(num("circulating_market_cap"))
        tc = cap_to_yuan(num("total_market_cap"))
        ratio = nf / fc * 100 if (nf is not None and fc) else None
        if a.flow_ratio > 0 and (ratio is None or ratio < a.flow_ratio):
            rej2[f"③ 启动日市值比 < {a.flow_ratio}%"] = \
                rej2.get(f"③ 启动日市值比 < {a.flow_ratio}%", 0) + 1
            continue
        x.update({
            "chg": chg, "net_flow": nf, "net_flow_5d": nf5,
            "flow_ratio": round(ratio, 3) if ratio is not None else None,
            "r20": round(x["close"] / x["ma20"] * 100 - 100, 2),
            "r60": round(x["close"] / x["ma60"] * 100 - 100, 2),
            "m20_m60": round(x["ma20"] / x["ma60"], 4),
            "float_cap_yi": round(fc / 1e8, 2) if fc else None,
            "total_cap_yi": round(tc / 1e8, 2) if tc else None,
            "pe": num("pe_ratio"), "pb": num("pb_ratio"),
            "turnover": num("turnover_rate"),
        })
        final.append(x)
    for k, v in rej2.items():
        rej[k] = rej.get(k, 0) + v

    # ---- 超级成长基因标注（只标注，不影响上面任何裁决结果）----
    gene = compute_gene(S, a.refresh)
    if gene is None:
        # 基因数据是**附加标注**，不该因为它失败就让整周选股作废；
        # 但也绝不能静默标成「否」——那会被误读为「已确认没有基因」。
        print("      ⚠️ 基因快照获取失败：「超级成长基因」列留空（选股结果不受影响）",
              file=sys.stderr)
    for x in final:
        if gene is None:
            x["gene"] = ""
            x["gene_detail"] = None
            continue
        g = gene.get(x["code"])
        x["gene"] = ("是" if g["gene"] else "否") if g else "无数据"
        x["gene_detail"] = g
    if gene is not None and final:
        n_yes = sum(1 for x in final if x["gene"] == "是")
        n_no = sum(1 for x in final if x["gene"] == "否")
        n_na = sum(1 for x in final if x["gene"] == "无数据")
        print(f"      基因标注：是 {n_yes} 只 ｜ 否 {n_no} 只 ｜ 无数据 {n_na} 只")

    final.sort(key=lambda x: (x["age"], -x["vcross"]))

    # ---- 写 CSV ----
    csv_path = os.path.join(CSVDIR, f"选股_{S}.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        for n, x in enumerate(final, 1):
            w.writerow([
                S, n, x["code"], x.get("name") or "", x["d_star"], x["age"],
                round(x["vcross"], 2), x.get("chg"),
                round(x["net_flow"] / 1e4, 2) if x.get("net_flow") is not None else "",
                round(x["net_flow_5d"] / 1e4, 2) if x.get("net_flow_5d") is not None else "",
                x.get("flow_ratio"), x["close"], x["r20"], x["r60"],
                round(x["ma20"], 4), round(x["ma60"], 4), x["m20_m60"],
                x.get("float_cap_yi"), x.get("total_cap_yi"),
                x.get("pe"), x.get("pb"), x.get("turnover"),
                x.get("gene", ""),
            ])

    # ---- 写运行明细（便于追溯）----
    result = {"基准日": S, "扫描周期K": a.n + 1, "新鲜度窗口N": a.n,
              "覆盖区间": need,
              "参数": {"K_VOL": a.k_vol, "涨幅区间": [a.chg_lo, a.chg_hi],
                       "市值比下限": a.flow_ratio, "MA60上限": a.ma60_gap,
                       "C2容忍": C2_TOL, "C3比例": C3_RATIO,
                       "上市满年数": LISTED_YEARS},
              "基因标注参数": {"弹性系数下限": GENE_ELASTIC_MIN,
                               "固定资产占比下限": GENE_FA_MIN,
                               "行业扩产强度上限": GENE_IND_SUPPLY_MAX,
                               "数据是否可用": gene is not None},
              "粗筛池": len(pool), "入选数": len(final),
              "剔除非": dict(sorted(rej.items(), key=lambda kv: -kv[1])),
              "名单": final}
    res_path = os.path.join(DB, f"result_{S}.json")
    jdump(res_path, result)

    # ---- 打印 ----
    print(f"\n粗筛池 {len(pool)} 只 → 入选 {len(final)} 只\n")
    print("剔除明细：")
    for k, v in sorted(rej.items(), key=lambda kv: -kv[1]):
        print(f"    {k}: {v}")
    if final:
        print(f"\n{'代码':10s}{'名称':9s}{'启动日':12s}{'距启动':>6s}"
              f"{'量能':>7s}{'距MA20':>8s}{'距MA60':>8s}{'市值比':>8s}"
              f"{'流通市值亿':>11s}{'PE':>10s}{'基因':>7s}")
        for x in final:
            print(f"{x['code']:10s}{(x.get('name') or '')[:8]:9s}"
                  f"{x['d_star']:12s}{x['age']:6d}{x['vcross']:7.2f}"
                  f"{x['r20']:8.2f}{x['r60']:8.2f}"
                  f"{(x.get('flow_ratio') or 0):7.2f}%"
                  f"{str(x.get('float_cap_yi') or '-'):>11s}"
                  f"{str(x.get('pe') or '-'):>10s}"
                  f"{str(x.get('gene') or '-'):>7s}")
    print(f"\nCSV    : {csv_path}")
    print(f"明细   : {res_path}")
    print(f"原始数据: {DB}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
