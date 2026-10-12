"""验证假设：baostock 不允许多进程并发登录，错峰登录可解决。

probe_bulk 已证实 4 进程并发登录时 3 个失败（err=10001001 用户未登录）。
本脚本让各 worker 按编号错开 STAGGER 秒再 login，验证成功率与总耗时。
"""
import multiprocessing as mp
import sys, time, json, os

sys.path.insert(0, "backend")

OUT = sys.argv[1] if len(sys.argv) > 1 else "stagger_log.jsonl"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 40
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 4
STAGGER = float(sys.argv[4]) if len(sys.argv) > 4 else 130.0

K_FIELDS = "date,close"


def _log(rec):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def worker(payload):
    tasks, wid, stagger = payload
    import baostock as bs

    if stagger:
        time.sleep(stagger * wid)
    t0 = time.time()
    try:
        lg = bs.login()
        login_sec = round(time.time() - t0, 1)
        ok_login = (lg.error_code == "0")
    except Exception as e:
        _log({"wid": wid, "event": "login_exc", "sec": 0, "msg": str(e)[:80]})
        return

    _log({"wid": wid, "event": "login", "sec": login_sec,
          "code": lg.error_code, "msg": str(lg.error_msg)[:40]})

    got = 0
    for symbol, code in tasks:
        ts = time.time()
        try:
            rs = bs.query_history_k_data_plus(
                code, K_FIELDS, start_date="2024-09-01", end_date="2026-10-12",
                frequency="d", adjustflag="1")
            n = 0
            while rs.error_code == "0" and rs.next():
                rs.get_row_data()
                n += 1
            _log({"wid": wid, "event": "q", "sym": symbol,
                  "sec": round(time.time() - ts, 2), "rows": n,
                  "err": rs.error_code})
            if n:
                got += 1
        except Exception as e:
            _log({"wid": wid, "event": "q_exc", "sym": symbol,
                  "msg": str(e)[:60]})
    _log({"wid": wid, "event": "done", "got": got, "total": len(tasks)})
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

    print(f"{WORKERS} 进程，各 {N//WORKERS} 只，错峰 {STAGGER}s", flush=True)
    t0 = time.time()
    ctx = mp.get_context("spawn")
    with ctx.Pool(len(payloads)) as pool:
        pool.map(worker, payloads)
    print(f"总耗时 {time.time()-t0:.1f}s", flush=True)

    recs = [json.loads(l) for l in open(OUT, encoding="utf-8")]
    print("\n登录情况:")
    for r in recs:
        if r["event"] == "login":
            print(f"  w{r['wid']}: code={r['code']} {r['sec']}s msg={r['msg']}")
    q = [r for r in recs if r["event"] == "q"]
    good = [r for r in q if r.get("rows", 0) > 0]
    print(f"\n查询: {len(q)} 条，成功有数据 {len(good)} 条")
    errs = {}
    for r in q:
        errs[r.get("err")] = errs.get(r.get("err"), 0) + 1
    print("错误码分布:", errs)
    for r in recs:
        if r["event"] == "done":
            print(f"  w{r['wid']}: {r['got']}/{r['total']} 只有数据")


if __name__ == "__main__":
    main()
