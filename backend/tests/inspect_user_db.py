"""检查用户已有 RPS 看板的本地数据库结构与数据规模。"""
import sqlite3

DB = r"RPS_USER_DB"

c = sqlite3.connect(DB)
tables = [r[0] for r in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")]

print(f"库大小: {DB}")
print(f"表数量: {len(tables)}\n")

for n in tables:
    cnt = c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
    cols = [r[1] for r in c.execute(f'PRAGMA table_info("{n}")')]
    print(f"=== {n}  ({cnt:,} 行)")
    print(f"    列: {', '.join(cols)}")
    try:
        row = c.execute(f'SELECT * FROM "{n}" LIMIT 1').fetchone()
        print(f"    样例: {row}")
    except Exception as e:
        print(f"    读取样例失败: {e}")
    # 日期范围
    for cand in ("date", "trade_date", "day", "dt"):
        if cand in cols:
            try:
                lo, hi = c.execute(
                    f'SELECT MIN("{cand}"), MAX("{cand}") FROM "{n}"').fetchone()
                print(f"    {cand} 范围: {lo} ~ {hi}")
            except Exception:
                pass
            break
    print()

c.close()
