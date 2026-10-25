#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K线选股规则 v2.8 —— 每周选股（扫描周期 K = 5 个交易日）

规则说明见同目录 K线选股规则_v2.md
v2 的含义 = 加入了「超级成长股基因」的标注工作（见下），选股参数与裁决条件未变。
v2.6 为 P0 工程修复；v2.7 为 P1-A 候选池上下限调整（CHG_LO 2.0→0.5、CHG_HI 9.5→20.5）；
v2.8 把第九节基因口径整体替换为「单季EPS同比 / 三年EPS CAGR / 年报ROE」。

目录结构：
    /home/kevin/week_select_stocks/
    ├── K线选股规则_v2.md
    ├── screen_kline.py          本程序
    ├── database/                从平台下载的原始数据（按日期命名）
    │   ├── cross_<起>_<止>.json     横截面粗筛原始返回
    │   ├── labels_<起>_<止>.json    排除集原始返回（ST + 次新分桶）
    │   ├── kline_<起>_<止>.json     候选并集的日K线原始数据
    │   ├── quote_<基准日>.json      最终名单的行情快照
    │   ├── finance_<基准日>.json    基因标注用的候选股财务历史（利润表）
    │   └── result_<基准日>.json     本次运行结果与逐条剔除明细（便于追溯）
    └── csv/
        └── 选股_<基准日>.csv         按规则选出的股票

用法：
    python3 screen_kline.py                 # 跑最近交易日
    python3 screen_kline.py --date 2026-10-14
    python3 screen_kline.py --refresh       # 强制重新下载原始数据
    python3 screen_kline.py --n 2           # 每3个交易日（窗口必须=周期−1）

退出码：0 正常 ｜ 1 出错（含原始数据下载失败）

超级成长基因标注（v2.8，即规则文档第九节）：
    在已选出的股票后面加一列「超级成长基因」（是/否/无数据），仅作标注，
    **不筛选、不排序、不影响入选名单**。下列三条同时成立才算「是」：
      ① 最新单季 EPS 同比增长率 > 40%
      ② 过去三年 EPS 年均复合增长率 > 25%
      ③ 最新年报 ROE > 17%
    口径：
      · 单季 EPS 由同期累计基本 EPS 差分得到：
        Q1=一季报；Q2=半年报−一季报；Q3=三季报−半年报；Q4=年报−三季报。
        同比基期单季 EPS 必须 > 0，否则「同比」没有意义，判「否」。
      · 三年 CAGR = (最近年报 EPS ÷ 三年前年报 EPS)^(1/3) − 1，
        两期 EPS 均须 > 0；最近年报为 Y，基期为 Y−3。
      · ROE 取最新年报加权 ROE（ROEWeighted，缺失时回退 ROE）。
      · 只认基准日已披露的报告期（InfoPublDate ≤ 基准日），避免未来函数。
    财务数据获取失败时该列留空（不标「否」），并在 stderr 给出告警；
    单只股票无财务数据时标「无数据」。二者都不得误读成「已确认没有基因」。

⚠️ 修改登记规范（2026-10-15 15:47 起）：
    本程序的每次修改，都必须在 K线选股规则_v2.md 的「十、版本沿革」表中
    登记「修改时间」（精确到分钟）与变更内容。未登记时间的修改视为不合规。
    最近一次：2026-10-25 10:08（v2.8，第九节基因改为 EPS 单季同比/三年 CAGR/年报 ROE）
    上一条　：2026-10-23 20:14（v2.7，P1-A：涨幅区间 0.5~20.5，候选池上限/下限调整）
    上上条　：2026-10-23 19:18（v2.6，P0：K线截断/缓存签名/本地复核/交易日校验/OHLC 落盘）
    上上上条：2026-10-23 16:29（v2.5，修复 K 线补取只存 1 根 → 合格股被误判「历史不足」）
    更早　　：2026-10-16 11:55（v2.3，据 18 周回测调参：C2 0.5%→1%，基因③ 25%→20%）
    　　　　：2026-10-16 09:42（v2.0，文档改名 v1→v2，程序内引用同步；逻辑未变）
    　　　　：2026-10-16 09:39（v1.3，新增「超级成长基因」标注列）
    　　　　：2026-10-15 15:49（v1.2，文件头补本条规范）
    　　　　：2026-10-15 15:38（v1.1，补 nf is None 防线 + CROSS_TPL 注释）
"""
import argparse
import csv
import datetime as dt
import hashlib
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
CHG_LO, CHG_HI = 0.5, 20.5    # ③ 当日涨幅区间 %（v2.7：原 2.0~9.5）
FLOW_MIN = 0.2        # ③ 主力净流入 / 流通市值 下限 %
C3_RATIO = 0.98       # ④ MA20 >= MA60 × 该值
C2_TOL = 0.01         # ④ MA20 近5日跌幅容忍度（v2.3 由 0.005 放宽；18周实测 0.005 偏紧）
MA60_GAP = 0.0        # 条件9 close/MA60 上限；0 = 关闭（已评估为失效）
LISTED_YEARS = 3      # ⑤ 上市满 N 年
KLINE_BATCH = 20      # westock kline 单批上限
LABEL_PAGE = 500
KLINE_LOOKBACK_DAYS = 300   # K线回溯长度（自然日），保证 MA60 与启动日检测有足够历史
MIN_HISTORY = 70      # find_start 要求基准日 S 的索引 i ≥ 70（即含当日共 ≥71 根）
                      # ⚠️ 补取分支曾只存 1 根，i 恒为 0 → 合格股被误报「K线历史不足(0根)」

# ---------------- 缓存/数据完整性（v2.6，P0） ----------------
# K 线 bar 的落盘格式（v2.6 起）：
#   (date, close, volume, open, high, low, amount)
# 前 3 位与旧版完全一致，旧缓存（3 元组）仍可读；后 4 位旧缓存没有，为 None。
# 保留前 3 位是为了不重写 116MB 历史缓存，同时让新数据能支持 ATR/收盘位置/成交额。
BAR_ORDER = "date,close,volume,open,high,low,amount"

# ⚠️ CROSS_TPL 一旦改动，必须同时把 CROSS_CACHE_VERSION +1。
#    否则旧 cross_*.json 会被当成当前表达式的结果复用（服务端字段随表达式来去）。
CROSS_CACHE_VERSION = 2

# ---------------- 成长基因标注（只标注，不参与任何裁决） ----------------
# 定位：在已选出的股票后面标注「是否具备超级成长股的基因底子」，
#       供人工精筛参考。**它不筛选、不排序、不影响入选名单**。
# 三条同时成立才标「是」：
#   ① 最新单季 EPS 同比增长率 > 40%
#   ② 过去三年 EPS 年均复合增长率 > 25%
#   ③ 最新年报 ROE > 17%
GENE_EPS_Q_YOY_MIN = 40.0    # ① 单季 EPS 同比下限（%）
GENE_EPS_CAGR3_MIN = 25.0    # ② 三年 EPS CAGR 下限（%）
GENE_ROE_MIN = 17.0          # ③ 最新年报 ROE 下限（%）
GENE_EPS_CAGR_YEARS = 3      # ② 复合增长年数：最近年报 Y 对比 Y−3
GENE_FIN_PERIODS = 20        # data_finance 拉取期数（5 年，够算 3 年 CAGR）
GENE_FIN_BATCH = 10          # data_finance 单批股票数

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


def retry(fn, tries=4, base=1.5, what="", allow_empty=False):
    """返回 None 表示彻底失败（调用方必须处理，不得静默继续）。

    allow_empty=True 时，空列表/空字符串视为**成功但为空**；只应在
    「空结果本身合法」的场景使用（如标签分页的下一页）。默认 False，
    因为历史日期横截面接口会把失败静默表现为空列表。
    """
    for i in range(tries):
        try:
            r = fn()
            if r or (allow_empty and r is not None):
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


def to_bar(r):
    """westock kline 行 → (date, close, volume, open, high, low, amount)。

    ⚠️ 顺序故意把 close/volume 放在第 2/3 位，与 v2.5 及更早的 3 元组缓存兼容；
    旧缓存没有后 4 位时按 None 处理。新增字段只用于后续的质量过滤，
    不改变 find_start/judge 的现有口径。
    """
    return (r["date"], float(r["last"]), float(r["volume"]),
            fnum(r.get("open")), fnum(r.get("high")),
            fnum(r.get("low")), fnum(r.get("amount")))


def cross_sig(lo, hi):
    """横截面缓存签名：表达式 + 参数 + 版本。参数变化 → 缓存自动失效。"""
    expr = CROSS_TPL.format(lo=lo, hi=hi)
    return hashlib.sha1(f"v{CROSS_CACHE_VERSION}|{expr}".encode("utf-8")).hexdigest()


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
    """横截面粗筛，逐日单独落盘。

    v2.6：缓存带签名（表达式 + lo/hi + 版本）。改 --chg-lo/--chg-hi 或 CROSS_TPL
    后不会再复用旧结果；旧版无 meta 的缓存仅在「默认参数 + 版本 1」时迁移一次。
    空结果仍视为失败：历史日期接口会静默返空，不能把失败当成「当天 0 只」。
    """
    path = os.path.join(DB, f"cross_{day}.json")
    meta_path = os.path.join(DB, f"cross_{day}.meta.json")
    sig = cross_sig(lo, hi)
    expr = CROSS_TPL.format(lo=lo, hi=hi)
    if os.path.exists(path) and not refresh:
        meta = jload(meta_path, {})
        if meta.get("sig") == sig:
            return jload(path, [])
        # v2.7 起默认区间已变（0.5~20.5），旧无 meta 缓存不再迁移，
        # 一律按新签名重拉，避免把 9.5% 截断池当成新池复用。
        print(f"      [缓存失效] {day}：参数/表达式已变，重新拉取", flush=True)
    rows = retry(lambda: md(run([W, "screen", "condition", "--expression",
                                 expr, "--date", day, "--limit", "6000"])),
                 what=f"横截面粗筛 {day}")
    if rows is None:
        raise RuntimeError(f"横截面粗筛失败：{day}（不得据此产出名单）")
    jdump(path, rows)
    jdump(meta_path, {"sig": sig, "version": CROSS_CACHE_VERSION,
                      "expression": expr})
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
                         what=f"标签 {L} @ {day}",
                         # 第 2 页起，空页是正常的「没有更多」；
                         # 首屏为空仍判失败——不能把接口故障当成「没有次新股」。
                         allow_empty=(off > 0))
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


def get_kline(codes, beg, end, refresh, require_codes=None):
    """拉取候选并集的日 K，并保证「基准日有成交的股票」一定有 end 日 K。

    v2.6 P0：
      · bar 落盘增加 open/high/low/amount（前 3 位兼容旧缓存）；
      · 批量接口截断（末日≠end）会逐只重取；
      · require_codes（通常是 cross[end] 的代码）重取后仍缺 end 日 K 时直接报错，
        不再以「K线无 S 数据」静默剔除。
    """
    require_codes = set(require_codes or ())
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
                km.setdefault(c, []).append(to_bar(r))
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

    def fetch_full(c):
        """单只拉取完整历史；返回升序 bar 列表或 None。"""
        rows = retry(lambda: md(run([W, "kline", c, "--start", beg, "--end", end,
                                     "--limit", "400"])), tries=2, what=f"补取 {c}")
        if not rows:
            return None
        try:
            out = sorted((to_bar(r) for r in rows), key=lambda b: b[0])
        except (KeyError, ValueError):
            return None
        return out or None

    # 批量拉取时单只失败会被静默跳过 —— 逐只补取，避免丢失合格股票
    # ⚠️ 补取必须写入**整段历史**。此处曾误写 `r = rows[0]`，只存 1 根 K 线：
    #    单只接口按日期倒序返回，rows[0] 恰是基准日那根，于是 find_start 的
    #    i 恒为 0、命中 i<MIN_HISTORY，报「K线历史不足(0根)」——与真次新股的
    #    文案完全相同，且坏结果已被 jdump 写进 kline_*.json（同日重跑不再重取、
    #    补取第二轮也不再视其为 missing），合格股会被永久性地静默剔除。
    for rnd in range(2):
        missing = [c for c in codes if c not in km]
        if not missing:
            break
        sys.stderr.write(f"  补取缺失的 {len(missing)} 只（第 {rnd+1} 轮）...\n")
        still = []
        for c in missing:
            got = fetch_full(c)
            if got:
                km[c] = got
                if len(km[c]) < MIN_HISTORY:
                    sys.stderr.write(
                        f"\n  [警告] {c} 补取仅 {len(km[c])} 根 K 线"
                        f"（需 ≥{MIN_HISTORY} 根）：若它本应入选，多半是取数不全\n")
            else:
                still.append(c)
            jdump(path, km)
            time.sleep(0.4)
        if not still:
            break

    # v2.6 P0：批量接口可能「状态 success 但只返回前 400 行」，导致末日远早于 end。
    # 这类必须重取；重取后仍缺且该股在基准日有成交（require_codes）时直接报错，
    # 不能把它当成停牌、用「K线无 S 数据」静默剔除。
    trunc = [c for c in codes if km.get(c) and km[c][-1][0] != end]
    if trunc:
        sys.stderr.write(f"\n  校验：{len(trunc)} 只 K 线末日≠{end}，逐只重取 ...\n")
        for rnd in range(2):
            still = []
            for c in trunc:
                got = fetch_full(c)
                if got and got[-1][0] == end and len(got) >= len(km.get(c, [])):
                    km[c] = got
                else:
                    still.append(c)
                jdump(path, km)
                time.sleep(0.4)
            trunc = still
            if not trunc:
                break
        if trunc:
            critical = sorted(set(trunc) & require_codes)
            if critical:
                raise RuntimeError(
                    f"K线截断且重取失败：{critical[:8]}"
                    f"{'…' if len(critical) > 8 else ''} 在 {end} 有成交，"
                    f"不能按停牌处理。请加 --refresh 或检查接口。")
            sys.stderr.write(
                f"  [警告] {len(trunc)} 只 K 线末日≠{end}（多为停牌），"
                f"将按「无 {end} 数据」剔除\n")

    for k in km:
        km[k].sort(key=lambda b: b[0])
    jdump(path, km)

    missing = [c for c in codes if c not in km]
    critical_missing = sorted(set(missing) & require_codes)
    if critical_missing:
        raise RuntimeError(
            f"K线完全缺失：{critical_missing[:8]}"
            f"{'…' if len(critical_missing) > 8 else ''} 在 {end} 有成交，"
            f"不能按停牌处理。请加 --refresh 或检查接口。")
    return km, missing


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


# ---------------- 成长基因标注（独立于上面的裁决） ----------------

def _westock_module():
    """把 westock CLI 当模块加载，复用它已有的鉴权 / JSON-RPC。

    westock CLI 目前没有 finance 子命令，而 data_finance 是拿财务历史的唯一入口。
    这里不复制令牌与续期逻辑，避免两套认证代码各自过期。
    """
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("westock_cli", W)
    spec = importlib.util.spec_from_loader("westock_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    if not hasattr(mod, "call"):
        raise RuntimeError("当前 westock CLI 不支持 data_finance 的模块调用")
    return mod


def get_finance(codes, day, refresh):
    """拉取候选股的利润表核心字段，按基准日缓存。

    返回 ({code: [rows]}, 失败代码列表)。rows 是 data_finance 的原始报告期记录，
    含 BasicEPS / ROE / ROEWeighted / EndDate / InfoPublDate。
    """
    path = os.path.join(DB, f"finance_{day}.json")
    cached = {} if refresh else jload(path, {})
    todo = [c for c in codes if c not in cached]
    failed = []
    if not todo:
        return cached, failed
    try:
        wk = _westock_module()
    except Exception as e:
        sys.stderr.write(f"  [失败] 加载 westock 模块：{e}\n")
        return cached, list(codes)
    for i in range(0, len(todo), GENE_FIN_BATCH):
        ch = todo[i:i + GENE_FIN_BATCH]
        d = retry(lambda: wk.call("data_finance", {
            "codes": ",".join(ch), "fields": "core",
            "num": GENE_FIN_PERIODS, "type": "income"}),
            what=f"财务数据 第{i // GENE_FIN_BATCH + 1}批")
        if d is None:
            failed.extend(ch)
            continue
        data = d.get("data") if isinstance(d, dict) else None
        if not isinstance(data, dict):
            failed.extend(ch)
            continue
        for c in ch:
            rows = data.get(c)
            if rows:
                cached[c] = rows
            else:
                failed.append(c)
        jdump(path, cached)
        time.sleep(0.4)
    return cached, failed


def _pub_date(v):
    """InfoPublDate 形如 '2026-08-15 00:00:00 +0800 CST' → '2026-08-15'。"""
    return str(v)[:10] if v else None


def _diff(a, b):
    return a - b if a is not None and b is not None else None


def gene_metrics(rows, day):
    """按基准日已披露的报告期，计算三条基因指标。

    ⚠️ 必须按 InfoPublDate ≤ day 过滤：data_finance 返回的是「今天能看到的全部
    报告期」，若不做点位过滤，回测历史日期时会用上当时尚未披露的财报。
    """
    rows = [r for r in rows
            if _pub_date(r.get("InfoPublDate")) and _pub_date(r.get("EndDate"))
            and _pub_date(r.get("InfoPublDate")) <= day]
    by = {_pub_date(r["EndDate"]): r for r in rows}
    if not by:
        return None      # 基准日前没有已披露财报 → 调用方标「无数据」

    latest = max(by)
    y, m = int(latest[:4]), int(latest[5:7])

    def cum(dt):
        r = by.get(dt)
        return fnum(r.get("BasicEPS")) if r else None

    # 单季 EPS = 本期累计 − 上一期累计；Q1 本身就是单季
    if m == 3:
        cur_q, prev_q = cum(latest), cum(f"{y-1}-03-31")
    elif m == 6:
        cur_q = _diff(cum(latest), cum(f"{y}-03-31"))
        prev_q = _diff(cum(f"{y-1}-06-30"), cum(f"{y-1}-03-31"))
    elif m == 9:
        cur_q = _diff(cum(latest), cum(f"{y}-06-30"))
        prev_q = _diff(cum(f"{y-1}-09-30"), cum(f"{y-1}-06-30"))
    elif m == 12:
        cur_q = _diff(cum(latest), cum(f"{y}-09-30"))
        prev_q = _diff(cum(f"{y-1}-12-31"), cum(f"{y-1}-09-30"))
    else:
        cur_q = prev_q = None

    eps_yoy = None
    if cur_q is not None and prev_q is not None and prev_q > 0:
        eps_yoy = (cur_q / prev_q - 1) * 100

    # 年报序列：最近年报 Y 与 Y−3 的 EPS 算三年 CAGR
    annuals = sorted(
        ((int(dt[:4]), fnum(r.get("BasicEPS")),
          fnum(r.get("ROEWeighted")), fnum(r.get("ROE")))
         for dt, r in by.items() if dt.endswith("-12-31")),
        reverse=True)
    roe = None
    annual_y = eps_annual = base_y = eps_base = cagr = None
    if annuals:
        annual_y, eps_annual, roe_w, roe_raw = annuals[0]
        roe = roe_w if roe_w is not None else roe_raw
        base_y = annual_y - GENE_EPS_CAGR_YEARS
        base = next((x for x in annuals if x[0] == base_y), None)
        if base:
            eps_base = base[1]
            if eps_annual is not None and eps_base is not None \
                    and eps_annual > 0 and eps_base > 0:
                cagr = ((eps_annual / eps_base) ** (1.0 / GENE_EPS_CAGR_YEARS) - 1) * 100

    reasons = []
    if eps_yoy is None:
        reasons.append("单季EPS同比不可用（基期EPS≤0或数据缺失）")
    elif eps_yoy <= GENE_EPS_Q_YOY_MIN:
        reasons.append(f"单季EPS同比 {eps_yoy:.2f}% ≤ {GENE_EPS_Q_YOY_MIN:g}%")
    if cagr is None:
        reasons.append("三年EPS CAGR不可用（两期EPS须>0）")
    elif cagr <= GENE_EPS_CAGR3_MIN:
        reasons.append(f"三年EPS CAGR {cagr:.2f}% ≤ {GENE_EPS_CAGR3_MIN:g}%")
    if roe is None:
        reasons.append("最新年报ROE缺失")
    elif roe <= GENE_ROE_MIN:
        reasons.append(f"最新年报ROE {roe:.2f}% ≤ {GENE_ROE_MIN:g}%")

    return {"gene": not reasons,
            "reason": "；".join(reasons) if reasons else "",
            "latest_report": latest,
            "eps_q": round(cur_q, 4) if cur_q is not None else None,
            "eps_q_prev": round(prev_q, 4) if prev_q is not None else None,
            "eps_q_yoy": round(eps_yoy, 2) if eps_yoy is not None else None,
            "annual_y": annual_y, "eps_annual": eps_annual,
            "base_y": base_y, "eps_base": eps_base,
            "eps_cagr3": round(cagr, 2) if cagr is not None else None,
            "roe": round(roe, 2) if roe is not None else None}


def compute_gene(codes, day, refresh):
    """返回 ({code: metrics}, 失败代码列表)；全部失败返回 (None, codes)。

    单只股票没有财务数据不算「整体失败」：该股标「无数据」，不影响其他股票。
    """
    if not codes:
        return {}, []
    rows_by, failed = get_finance(codes, day, refresh)
    if not rows_by:
        return None, failed
    info = {}
    for c in codes:
        rows = rows_by.get(c)
        if not rows:
            continue
        g = gene_metrics(rows, day)
        if g is None:
            continue      # 只有基准日之后的财报 → 「无数据」，不是「否」
        info[c] = g
    return info, failed


# ---------------- 裁决 ----------------

def find_start(code, name, bars, S):
    """在 S 日回溯该股的启动日 d*。失败时返回 (None, 原因)"""
    if not bars:
        return None, "无K线数据"
    i = next((j for j, x in enumerate(bars) if x[0] == S), None)
    if i is None:
        return None, f"K线无 {S} 数据"
    if i < MIN_HISTORY:
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
          k_vol, gap, c2, c3, chg_lo=CHG_LO, chg_hi=CHG_HI):
    c = det["code"]
    d0 = det["d_star"]
    if det["age"] > N:
        return False, f"⑦ 距启动{det['age']}日 > N={N}"
    if det["vcross"] < k_vol:
        return False, f"② 启动日放量不足({det['vcross']:.2f}<{k_vol})"
    row0 = cross_by_day.get(d0, {}).get(c)
    if row0 is None:
        return False, f"③ 启动日({d0})未通过日频条件"
    # v2.6 P0：服务端条件必须在本地复核。否则缓存参数不一致/字段缺失时，
    # 会静默放进不符合规则的股票（旧版只检查 row0 是否存在）。
    chg = fnum(row0.get("ChangePCT"))
    if chg is None:
        return False, "③ 启动日涨幅字段缺失"
    if not (chg_lo - 1e-9 <= chg <= chg_hi + 1e-9):
        return False, f"③ 启动日涨幅{chg:.2f}%不在[{chg_lo},{chg_hi}]"
    nf = fnum(row0.get("MainNetFlow"))
    if nf is None:
        return False, "③ 启动日净流入字段缺失"
    if nf <= 0:
        return False, "③ 启动日净流入≤0"
    nf5 = fnum(row0.get("MainNetFlow5D"))
    if nf5 is None:
        return False, "③ 5日净流入字段缺失"
    if nf5 <= 0:
        return False, "③ 5日净流入≤0"
    for flag in ("IsDelisted", "Ifsuspend"):
        v = fnum(row0.get(flag))
        if v is not None and v != 0:
            return False, f"③ 启动日 {flag}≠0"
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
    ap = argparse.ArgumentParser(description="K线选股规则 v2.8（每周选股，K=5）")
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
    if S not in days:
        prev = max((d for d in days if d <= S), default=None)
        print(f"错误：基准日 {S} 不是交易日；最近交易日为 {prev or '无'}",
              file=sys.stderr)
        return 1
    if a.chg_lo > a.chg_hi:
        print(f"错误：--chg-lo {a.chg_lo} > --chg-hi {a.chg_hi}", file=sys.stderr)
        return 1
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

    # v2.6 P0：字段缺失必须在裁决前报错。服务端只返回表达式引用到的列，
    # 若 CROSS_TPL 改动或旧缓存缺列，judge 的本地复核会全部失败，
    # 看起来像「没有合格股」，实际是数据问题。
    bad_fields = []
    for d in need:
        for c, r in cross_by_day[d].items():
            for k in ("ChangePCT", "MainNetFlow", "MainNetFlow5D"):
                if fnum(r.get(k)) is None:
                    bad_fields.append((d, c, k))
    if bad_fields:
        d, c, k = bad_fields[0]
        raise RuntimeError(
            f"横截面字段缺失 {len(bad_fields)} 处（如 {d} {c} {k}）。"
            f"请检查 CROSS_TPL 是否仍引用这些字段，或加 --refresh 重建缓存。")

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
    km, km_missing = get_kline(pool, kbeg, S, a.refresh,
                               require_codes=set(cross_by_day[S]))
    hit_cnt = sum(1 for c in pool if km.get(c))
    print(f"      命中 {hit_cnt}/{len(pool)}")
    if km_missing:
        critical = sorted(set(km_missing) & set(cross_by_day[S]))
        if critical:
            raise RuntimeError(
                f"基准日有成交但完全取不到 K 线：{critical[:8]}"
                f"{'…' if len(critical) > 8 else ''}。"
                f"不能按停牌处理，请加 --refresh 或检查接口。")
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
                        a.k_vol, a.ma60_gap, C2_TOL, C3_RATIO,
                        a.chg_lo, a.chg_hi)
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
    gene, gene_failed = compute_gene([x["code"] for x in final], S, a.refresh)
    if gene is None:
        # 财务数据是**附加标注**，不该因为它失败就让整周选股作废；
        # 但也绝不能静默标成「否」——那会被误读为「已确认没有基因」。
        print("      ⚠️ 财务数据获取失败：「超级成长基因」列留空（选股结果不受影响）",
              file=sys.stderr)
    elif gene_failed:
        print(f"      ⚠️ {len(gene_failed)} 只财务数据缺失，标「无数据」："
              f"{', '.join(gene_failed[:8])}{'…' if len(gene_failed) > 8 else ''}",
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
              "基因标注参数": {"单季EPS同比下限%": GENE_EPS_Q_YOY_MIN,
                               "三年EPS_CAGR下限%": GENE_EPS_CAGR3_MIN,
                               "最新年报ROE下限%": GENE_ROE_MIN,
                               "三年CAGR基期": f"最近年报年−{GENE_EPS_CAGR_YEARS}",
                               "数据是否可用": gene is not None,
                               "缺失代码": gene_failed},
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
