#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.7 P0/P1-A 回归冒烟测试（只读源项目，不碰真实 database/）。

运行：
    cd /home/kevin/MyData/week_select_stocks
    python3 p0_smoke_test.py

覆盖：
  1. retry 的 allow_empty 语义
  2. 新 K 线 bar 的 7 字段格式
  3. cross 缓存签名：同参数复用、变参数重拉、旧无 meta 缓存重拉
  4. judge 本地复核 ChangePCT
  5. get_kline：OHLC 落盘；截断且基准日有成交 → 报错
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

SRC = "/home/kevin/MyData/kline-select-stocks-approach/screen_kline.py"


def load_rule():
    spec = importlib.util.spec_from_file_location("rule", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_cross_run(args):
    return ("| code | name | ChangePCT | MainNetFlow | MainNetFlow5D | IsDelisted | Ifsuspend |\n"
            "|---|---|---|---|---|---|---|\n"
            "| sz000001 | 测试 | 3.0 | 100 | 200 | 0 | 0 |\n")


def fake_kline_run(last_date):
    return ("| code | date | last | volume | open | high | low | amount |\n"
            "|---|---|---|---|---|---|---|---|\n"
            f"| sz000001 | {last_date} | 10 | 100 | 9.8 | 10.2 | 9.7 | 123456 |\n"
            "| sz000001 | 2026-10-22 | 9.9 | 90 | 9.8 | 10 | 9.7 | 100000 |\n")


def main():
    rule = load_rule()
    rule.time.sleep = lambda *_: None
    tmp = tempfile.mkdtemp(prefix="_p0_smoke_")
    old_db = rule.DB
    rule.DB = tmp
    try:
        # 1) retry
        assert rule.retry(lambda: [], tries=2, allow_empty=True) == []
        assert rule.retry(lambda: [], tries=2, allow_empty=False) is None
        print("PASS retry(empty)")

        # 2) bar
        b = rule.to_bar({"date": "2026-10-23", "last": "10.5", "volume": "1000",
                         "open": "10.0", "high": "10.8", "low": "9.9",
                         "amount": "1234567"})
        assert b == ("2026-10-23", 10.5, 1000.0, 10.0, 10.8, 9.9, 1234567.0), b
        print("PASS to_bar 7 fields")

        # 3) cross cache signature
        rule.run = fake_cross_run
        calls = []
        orig_run = rule.run

        def counting_run(args):
            calls.append(args)
            return orig_run(args)

        rule.run = counting_run
        lo, hi = rule.CHG_LO, rule.CHG_HI
        rows = rule.get_cross("2026-10-23", lo, hi, False)
        assert len(rows) == 1
        meta = json.load(open(os.path.join(tmp, "cross_2026-10-23.meta.json")))
        assert meta["sig"] == rule.cross_sig(lo, hi)
        n = len(calls)
        rule.get_cross("2026-10-23", lo, hi, False)
        assert len(calls) == n, "同参数不应重拉"
        rule.get_cross("2026-10-23", 5.0, 7.0, False)
        assert len(calls) == n + 1, "变参数必须重拉"
        meta = json.load(open(os.path.join(tmp, "cross_2026-10-23.meta.json")))
        assert meta["sig"] == rule.cross_sig(5.0, 7.0)
        # v2.7 起旧缓存（无 meta）不再迁移：默认参数也必须重拉
        os.remove(os.path.join(tmp, "cross_2026-10-23.meta.json"))
        json.dump([{"code": "sz000002"}], open(os.path.join(tmp, "cross_2026-10-23.json"), "w"))
        n = len(calls)
        rule.get_cross("2026-10-23", lo, hi, False)
        assert len(calls) == n + 1, "无 meta 必须重拉"
        print("PASS cross cache signature / no-meta refetch")

        # 4) judge local ChangePCT
        bars = [("2026-01-%02d" % (i % 28 + 1), 10.0, 100.0) for i in range(80)]
        S = bars[-1][0]
        det = {"code": "sz000001", "name": "测试", "d_star": S, "age": 0,
               "close": 10.0, "ma20": 10.0, "ma60": 10.0, "cl_s": 10.0,
               "m20s": 10.0, "m60s": 10.0, "vcross": 2.0, "_ci": 79, "_i": 79}
        row = {"ChangePCT": "5.0", "MainNetFlow": "100", "MainNetFlow5D": "200",
               "IsDelisted": "0", "Ifsuspend": "0"}
        ok, why = rule.judge(det, bars, S, 4, {S: {"sz000001": row}},
                             {S: set()}, {S: set()}, 1.75, 0.0, 0.01, 0.98,
                             rule.CHG_LO, rule.CHG_HI)
        assert ok, why
        bad = dict(row, ChangePCT="0.40")
        ok, why = rule.judge(det, bars, S, 4, {S: {"sz000001": bad}},
                             {S: set()}, {S: set()}, 1.75, 0.0, 0.01, 0.98,
                             rule.CHG_LO, rule.CHG_HI)
        assert not ok and "不在" in why, (ok, why)
        print("PASS judge local ChangePCT:", why)

        # 5a) get_kline success persists 7 fields
        rule.run = lambda args: fake_kline_run("2026-10-23")
        km, missing = rule.get_kline(["sz000001"], "2026-01-01", "2026-10-23",
                                     False, require_codes={"sz000001"})
        assert not missing
        assert len(km["sz000001"][-1]) == 7
        print("PASS get_kline 7-field persistence")

        # 5b) truncation on a stock that traded on end -> RuntimeError
        #     用 --refresh 绕过 5a 已落盘的完整缓存，模拟接口本次只返回旧数据
        rule.run = lambda args: fake_kline_run("2026-10-22")
        try:
            rule.get_kline(["sz000001"], "2026-01-01", "2026-10-23",
                           True, require_codes={"sz000001"})
        except RuntimeError as e:
            print("PASS get_kline critical truncation raises:", str(e)[:60])
        else:
            raise AssertionError("expected RuntimeError for truncated critical code")
    finally:
        rule.DB = old_db
        shutil.rmtree(tmp, ignore_errors=True)

    print("ALL P0 SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
