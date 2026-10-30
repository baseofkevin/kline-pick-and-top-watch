#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读审计：核对 result_*.json 的计数自洽性、产物完整性、数据缺口。"""
import glob
import json
import os
import re

BASE = "/home/kevin/MyData/kline-select-stocks-approach"
DB = os.path.join(BASE, "database")
CSV = os.path.join(BASE, "csv")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main():
    rs = sorted(glob.glob(os.path.join(DB, "result_*.json")))
    print("=" * 100)
    print("A. 计数自洽性（应满足：粗筛池 == 入选数 + Σ剔除非）")
    print("=" * 100)
    print(f"{'基准日':12s}{'粗筛池':>7s}{'入选':>6s}{'名单':>6s}{'Σ剔除':>7s}"
          f"{'差额':>6s}{'CSV行':>7s}{'CSV?':>6s}")
    for p in rs:
        d = load(p)
        S = d.get("基准日") or os.path.basename(p)
        pool = d.get("粗筛池")
        n_in = d.get("入选数")
        names = d.get("名单") or []
        rej = d.get("剔除非") or {}
        s = sum(rej.values())
        gap = (pool - n_in - s) if None not in (pool, n_in) else None
        cp = os.path.join(CSV, f"选股_{S}.csv")
        rows = None
        if os.path.exists(cp):
            with open(cp, encoding="utf-8-sig") as f:
                rows = sum(1 for _ in f) - 1
        print(f"{S:12s}{str(pool):>7s}{str(n_in):>6s}{len(names):>6d}{s:>7d}"
              f"{str(gap):>6s}{str(rows):>7s}{'有' if rows is not None else '缺':>6s}")

    print()
    print("=" * 100)
    print("B. 剔除原因键中的 # 占位符来源（代码里 judge 的归一化 vs find_start 未归一化）")
    print("=" * 100)
    for p in rs:
        d = load(p)
        rej = d.get("剔除非") or {}
        masked = [k for k in rej if "#" in k]
        unmasked_num = [k for k in rej if "#" not in k and re.search(r"\d", k)]
        print(f"\n{os.path.basename(p)}: 键 {len(rej)} 个；含# {len(masked)} 个；"
              f"含具体数字未归一 {len(unmasked_num)} 个")
        for k, v in sorted(rej.items(), key=lambda kv: -kv[1]):
            print(f"    {v:>4d}  {k}")

    print()
    print("=" * 100)
    print("C. 覆盖区间内 mv_/cross_/labels_ 数据缺口")
    print("=" * 100)
    for p in rs:
        d = load(p)
        cov = d.get("覆盖区间") or []
        miss = {pre: [x for x in cov if not os.path.exists(
            os.path.join(DB, f"{pre}_{x}.json"))]
            for pre in ("cross", "labels", "mv")}
        print(f"{os.path.basename(p):26s} 覆盖 {cov[0] if cov else '-'}~{cov[-1] if cov else '-'}"
              f"  缺 cross={miss['cross']} labels={miss['labels']} mv={miss['mv']}")

    print()
    print("=" * 100)
    print("D. 2026-10-27 产物核对")
    print("=" * 100)
    p = os.path.join(DB, "result_2026-10-27.json")
    if os.path.exists(p):
        d = load(p)
        print("入选数 =", d.get("入选数"), "名单长度 =", len(d.get("名单") or []),
              "粗筛池 =", d.get("粗筛池"))
        print("名单代码:", [x["code"] for x in d.get("名单") or []])
    print("csv 目录内 10-27 文件:",
          [os.path.basename(x) for x in glob.glob(os.path.join(CSV, "*2026-10-27*"))])
    print("database 内 10-27 mv/quote/finance:",
          [os.path.basename(x) for x in glob.glob(os.path.join(DB, "*2026-10-27*"))])


if __name__ == "__main__":
    main()
