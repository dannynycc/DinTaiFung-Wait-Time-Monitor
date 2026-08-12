"""
把本機 wait_log.db 的變化事件匯出成 docs/data/YYYY-MM-DD.json，供 GitHub Pages 直接讀取。

為什麼只匯出 wait_changes 不匯出 wait_log：
  raw 每天約 15,100 筆、25 天共 257,049 筆（約 38 MB）。放進 git 會讓 repo 永久變胖，
  而前端的圖表與表格本來就只用變化事件（每天約 600-750 筆）。
  raw 留在本機 wait_log.db 當 audit trail。

為什麼不匯進 D1：
  前端歷史日一律優先讀 docs/data 的靜態檔，D1 只服務「今日」與尚未匯出的日子。
  把三個月前的資料塞進 D1 只會吃掉 500 MB 的單庫額度卻幾乎不會被查到。

用法：
  python tools/export_history.py            # 匯出全部日期
  python tools/export_history.py 2026-05-01 # 只匯出指定日期
"""
import datetime
import io
import json
import os
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FILE = os.path.join(BASE_DIR, "wait_log.db")
DATA_DIR = os.path.join(BASE_DIR, "docs", "data")

COLUMNS = ["timestamp", "store_id", "store_name", "wait_time", "prev_value", "duration_min"]


def export(only_date=None):
    if not os.path.exists(DB_FILE):
        print(f"找不到 {DB_FILE}")
        return 1

    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row

    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(timestamp,1,10) FROM wait_changes ORDER BY 1"
    )]
    if only_date:
        dates = [d for d in dates if d == only_date]

    written = []
    for d in dates:
        # 上界用隔日 00:00:00 而非當日 23:59:59，後者會漏掉正好落在 23:59:59 的事件
        nxt = (datetime.date.fromisoformat(d) + datetime.timedelta(days=1)).isoformat()
        rows = [dict(r) for r in conn.execute(
            f"""SELECT {','.join(COLUMNS)} FROM wait_changes
                WHERE timestamp >= ? AND timestamp < ?
                ORDER BY timestamp ASC, store_id ASC""",
            (f"{d} 00:00:00", f"{nxt} 00:00:00"),
        )]
        path = os.path.join(DATA_DIR, f"{d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, separators=(",", ":"))
        written.append((d, len(rows), os.path.getsize(path)))

    conn.close()
    rebuild_index()

    total = sum(n for _, n, _ in written)
    size = sum(s for _, _, s in written)
    for d, n, s in written:
        print(f"  {d}  {n:>4} 筆  {s/1024:>6.1f} KB")
    print(f"\n共 {len(written)} 天 / {total} 筆變化事件 / {size/1024:.1f} KB")
    return 0


def rebuild_index():
    """掃描 docs/data 重建日期索引，讓前端下拉選單知道有哪些歷史檔。"""
    dates = sorted(
        f[:-5] for f in os.listdir(DATA_DIR)
        if f.endswith(".json") and f != "index.json" and len(f) == 15
    )
    with open(os.path.join(DATA_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(dates, f, separators=(",", ":"))
    print(f"index.json 更新：{len(dates)} 個日期")


if __name__ == "__main__":
    sys.exit(export(sys.argv[1] if len(sys.argv) > 1 else None))
