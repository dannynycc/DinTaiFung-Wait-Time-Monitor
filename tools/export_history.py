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
  python tools/export_history.py             # 匯出全部日期
  python tools/export_history.py 2026-05-01  # 只匯出指定日期
  python tools/export_history.py --force     # 允許用比現有檔案更少的資料覆蓋

⚠️ 這個工具讀的是**本機** wait_log.db，而 docs/data 裡多數檔案是**雲端 D1** 匯出的。
   兩邊的資料範圍不同（本機版自 2026-05-18 起就沒再跑），不帶參數執行會拿本機
   資料去覆蓋同日期的雲端檔案。因此預設帶「暴跌守門」，見 would_shrink()。
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


def would_shrink(path, new_count):
    """既有檔案比新資料多就回傳既有筆數，否則回 None。

    daily-export.yml 有同樣的守門，這個工具原本沒有 —— 但它們寫的是同一批檔案。
    那個守門是 2026-08-12 的真實事故後才加的：當天 17:51 手動觸發匯出，把只累積到
    一半的 43 筆蓋掉原本會有的 225 筆，隔天該日變成歷史日後，前端優先讀靜態檔，
    等於 81% 的資料在畫面上消失，而流程從頭到尾都是綠的。
    「比現有檔案少」永遠是可疑的：正常情況只會愈補愈多。
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            old = len(json.load(f))
    except Exception:
        return None          # 既有檔案讀不出來就不擋，讓這次寫入把它修好
    return old if new_count < old else None


def export(only_date=None, force=False):
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
    skipped = []
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

        old = would_shrink(path, len(rows))
        if old is not None and not force:
            skipped.append((d, len(rows), old))
            continue

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

    if skipped:
        print(f"\n跳過 {len(skipped)} 天（本機資料比既有檔案少，拒絕覆蓋）：")
        for d, new, old in skipped:
            print(f"  {d}  本機 {new} 筆 < 既有 {old} 筆")
        print("這通常代表該日的檔案是雲端 D1 匯出的、比本機完整。")
        print("確知既有檔案有誤才用 --force 覆蓋。")
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
    args = sys.argv[1:]
    force = "--force" in args
    positional = [a for a in args if not a.startswith("--")]
    sys.exit(export(positional[0] if positional else None, force=force))
