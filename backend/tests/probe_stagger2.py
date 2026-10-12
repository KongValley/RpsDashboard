"""干净环境下重测 baostock 的「错峰登录 + 并发抓取」。

上一次错峰测试的结论不可信：当时有多个被放弃的线程仍持有 baostock 的
socket（正是它们把后续查询全部拖成"超时"）。本脚本：
  - 每个 worker 独立进程，独立 login
  - worker 之间按编号错开 STAGGER 秒再登录
  - 每个 worker 逐条记录成败，可判定登录是否真的成功

判定标准：err=10001001（用户未登录）的查询数必须为 0。
"""
import multiprocessing as mp
import sys, time, json, os

sys.path.insert(0, "backend")

OUT = "stagger2.jsonl"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 80
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 4
STAGGER = float(sys.argv[3]) if len(sys.argv) > 3 else 140.0

FLD = "date,open,high,low,close,volume,amount,turn,pctChg,isST,tradestatus"


def _log(r):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")


def worker(payload):
    tasks, wid, stagger = payload
    import baostock as bs

    if stagger:
        time.sleep(stagger * wid)

    t0 = time.time()
    lg = bs.login()
    login_sec = round(time.time() - t0, 1)
    _log({"wid": wid, "event": "login", "sec": login_sec,
          "code": lg.error_code, "msg": str(lg.error_msg)[:40]})

    ok = noauth = err = rows = 0
    for symbol, code in tasks:
        ts = time.time()
        try:
            rs = bs.query_history_k_data_plus(
                code, FLD, start_date="2024-09-01", end_date="2026-10-12",
                frequency="d", adjustflag="1")
            n = 0
            while rs.error_code == "0" and rs.next():
                rs.get_row_data()
                n += 1
            rows += n
            if rs.error_code == "10001001":
                noauth += 1
            elif n > 0:
                ok += 1
            else:
                err += 1
        except Exception as e:
            err += 1
            _log({"wid": wid, "event": "exc", "sym": symbol, "msg": str(e)[:60]})
    _log({"wid": wid, "event": "done", "ok": ok, "noauth": noauth,
          "err": err, "rows": rows, "sec": round(time.time() - t0, 1)})
    try:
        bs.logout()
    except Exception:
        pass


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    import baostock as bs
    bs.login()
    rs = bs.query_stock_basic(code_name="", code="")
    rows = []
    while rs.error_code == "0" and rs.next():
        r = rs.get_row_data()
        if r[4] == "1" and r[5] == "1":
            rows.append((r[0].split(".")[-1], r[0]))
    bs.logout()
    syms = rows[:N]

    chunks = [[] for _ in range(WORKERS)]
    for i, s in enumerate(syms):
        chunks[i % WORKERS].append(s)
    payloads = [(c, i, STAGGER) for i, c in enumerate(chunks) if c]

    per = N / WORKERS
    print(f"{WORKERS} 进程 x {per:.0f} 只，错峰 {STAGGER}s"
          f"（最后一组登录于 t={STAGGER*(WORKERS-1):.0f}s）", flush=True)
    t0 = time.time()
    ctx = mp.get_context("spawn")
    with ctx.Pool(WORKERS) as pool:
        pool.map(worker, payloads)
    total = time.time() - t0

    recs = [json.loads(l) for l in open(OUT, encoding="utf-8")]
    print("\n登录:")
    for r in recs:
        if r["event"] == "login":
            print(f"  w{r['wid']}  code={r['code']}  {r['sec']}s  {r['msg']}")
    print("\n完成:")
    tok = tno = te = tr = 0
    for r in recs:
        if r["event"] == "done":
            print(f"  w{r['wid']}  ok={r['ok']} 未登录={r['noauth']} 其他错={r['err']} "
                  f"行数={r['rows']} 耗时={r['sec']}s")
            tok += r["ok"]; tno += r["noauth"]; te += r["err"]; tr += r["rows"]

    print(f"\n合计: ok={tok} 未登录={tno} 其他错={te} 行数={tr} 总耗时={total:.0f}s")
    ok_rate = tno / max(1, tok + tno + te)
    print(f"未登录占比 {ok_rate*100:.1f}%  -> "
          f"{'错峰方案有效 ✅' if ok_rate < 0.02 else '错峰无效 ❌ 只能单进程'}")
    if ok_rate < 0.02:
        rate = tok / max(1e-9, total) * 60 / WORKERS
        print(f"外推全市场 5224 只 ≈ {5224/max(1,tok) * total / 60:.1f} 分钟")


if __name__ == "__main__":
    main()
