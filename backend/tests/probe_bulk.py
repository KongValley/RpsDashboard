"""受控实验：40 只股票 × 4 进程，逐只记录耗时与成败，找出吞吐瓶颈。

用途：判断「多进程批量拉取」到底是
  (a) 查询本身随会话变慢  (b) 大量查询失败触发退避  (c) 连接阻塞
"""
import multiprocessing as mp
import sys, time, json, os, traceback

sys.path.insert(0, "backend")

OUT = sys.argv[1] if len(sys.argv) > 1 else "probe_log.jsonl"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 40
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 4
START = "2024-09-01"
END = "2026-10-12"

K_FIELDS = "date,open,high,low,close,volume,amount,turn,pctChg,isST,tradestatus"


def _log(rec):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def worker(payload):
    """模块级 worker（Windows spawn 要求可 pickle）。"""
    tasks, wid = payload
    import baostock as bs

    recs = []
    t_login = time.time()
    try:
        lg = bs.login()
        if lg.error_code != "0":
            return [{"sym": "?", "wid": wid, "event": "login_fail",
                     "sec": round(time.time() - t_login, 2),
                     "msg": lg.error_msg}]
    except Exception as e:
        return [{"sym": "?", "wid": wid, "event": "login_exc",
                 "sec": round(time.time() - t_login, 2), "msg": str(e)[:100]}]

    recs.append({"sym": "*", "wid": wid, "event": "login",
                 "sec": round(time.time() - t_login, 2)})

    for symbol, code in tasks:
        t0 = time.time()
        try:
            rs = bs.query_history_k_data_plus(
                code, K_FIELDS, start_date=START, end_date=END,
                frequency="d", adjustflag="1")
            n = 0
            err = rs.error_code
            msg = rs.error_msg
            while err == "0" and rs.next():
                rs.get_row_data()
                n += 1
            recs.append({"sym": symbol, "wid": wid, "event": "ok",
                         "sec": round(time.time() - t0, 2), "rows": n,
                         "err": err, "msg": str(msg)[:60]})
        except Exception as e:
            recs.append({"sym": symbol, "wid": wid, "event": "exc",
                         "sec": round(time.time() - t0, 2),
                         "msg": f"{type(e).__name__}:{str(e)[:80]}"})

    try:
        bs.logout()
    except Exception:
        pass
    return recs


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
            rows.append(r)
    bs.logout()
    syms = [(r[0].split(".")[-1], r[0]) for r in rows][:N]
    print(f"取 {len(syms)} 只做实验", flush=True)

    chunks = [[] for _ in range(WORKERS)]
    for i, s in enumerate(syms):
        chunks[i % WORKERS].append(s)
    payloads = [(c, i) for i, c in enumerate(chunks) if c]

    t0 = time.time()
    ctx = mp.get_context("spawn")
    with ctx.Pool(len(payloads)) as pool:
        for batch in pool.imap_unordered(worker, payloads):
            for r in batch:
                _log(r)
    total = time.time() - t0

    recs = [json.loads(l) for l in open(OUT, encoding="utf-8")]
    ok = [r for r in recs if r["event"] == "ok"]
    bad = [r for r in recs if r["event"] in ("exc",)]
    zero = [r for r in ok if r.get("rows", 0) == 0]
    times = sorted(r["sec"] for r in ok)

    print(f"\n总耗时 {total:.1f}s  ({len(syms)} 只 / {len(payloads)} 进程)")
    print(f"成功 {len(ok)}  异常 {len(bad)}  零行 {len(zero)}")
    if times:
        import statistics
        print(f"单只耗时: min={times[0]:.2f} p50={statistics.median(times):.2f} "
              f"p90={times[int(len(times)*0.9)]:.2f} max={times[-1]:.2f}")
    for r in bad[:6]:
        print("  EXC", r["sym"], r["sec"], r["msg"])
    for r in recs[:3]:
        if r["event"] == "login":
            print(f"  worker{r['wid']} login {r['sec']}s")
    print(f"\n外推全市场 5224 只 / {len(payloads)} 进程 ≈ "
          f"{total / len(syms) * 5224 / 60:.1f} 分钟")


if __name__ == "__main__":
    main()
