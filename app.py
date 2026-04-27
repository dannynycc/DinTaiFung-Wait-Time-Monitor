"""
鼎泰豐全分店 — 候位監控 + Web 前端
啟動後開 http://localhost:5678 看圖表和表格
自動略過「無提供內用」的分店
資料以 SQLite 儲存（自動從舊 CSV 遷移）
"""

import sys
import io
import os
import csv
import json
import time
import sqlite3
import subprocess
import threading
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

API_URL = "https://www.dintaifung.tw/Queue/Home/WebApiTest"
INTERVAL = 60
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "wait_log.db")
OLD_CSV = os.path.join(BASE_DIR, "all_branches_log.csv")
PORT = 5678

# 信義店長期回傳「無提供內用」或 -1，無實際候位資料 → 永久排除
EXCLUDED_STORE_IDS = {"0001"}

STORES = [
    {"id": "0001", "name": "信義店"},
    {"id": "0003", "name": "復興店"},
    {"id": "0005", "name": "天母店"},
    {"id": "0006", "name": "新竹店"},
    {"id": "0007", "name": "101店"},
    {"id": "0008", "name": "台中店"},
    {"id": "0009", "name": "板橋店"},
    {"id": "0010", "name": "高雄店"},
    {"id": "0011", "name": "南西店"},
    {"id": "0012", "name": "A4店"},
    {"id": "0013", "name": "A13店"},
    {"id": "0015", "name": "新生店"},
]


# ── DB ────────────────────────────────────────────

def db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db():
    """建立 table 和 index，若不存在"""
    with db_connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS wait_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT    NOT NULL,
                store_id     TEXT    NOT NULL,
                store_name   TEXT    NOT NULL,
                wait_time    TEXT    NOT NULL,
                num_1        TEXT,
                num_2        TEXT,
                num_3        TEXT,
                num_4        TEXT,
                togo_numbers TEXT,
                last_time    INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_timestamp ON wait_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_store_ts  ON wait_log(store_id, timestamp);

            CREATE TABLE IF NOT EXISTS wait_changes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT    NOT NULL,
                store_id     TEXT    NOT NULL,
                store_name   TEXT    NOT NULL,
                wait_time    TEXT    NOT NULL,
                prev_value   TEXT,
                duration_min INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_changes_ts       ON wait_changes(timestamp);
            CREATE INDEX IF NOT EXISTS idx_changes_store_ts ON wait_changes(store_id, timestamp);
        """)


def backfill_changes_if_empty():
    """從既有 wait_log 推導變化事件，一次性 backfill"""
    with db_connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM wait_changes").fetchone()[0]
        if n > 0:
            return
        rows = conn.execute("""
            WITH lagged AS (
                SELECT timestamp, store_id, store_name, wait_time,
                       LAG(wait_time)  OVER (PARTITION BY store_id ORDER BY timestamp) AS prev_value,
                       LAG(timestamp)  OVER (PARTITION BY store_id ORDER BY timestamp) AS prev_ts
                FROM wait_log
            )
            SELECT timestamp, store_id, store_name, wait_time, prev_value, prev_ts
            FROM lagged
            WHERE prev_value IS NULL OR wait_time != prev_value
            ORDER BY timestamp ASC, store_id ASC
        """).fetchall()

        # 計算 duration_min（前一個值持續了多久）
        # 第一個 change 沒有 prev → duration NULL
        # 之後每個 change 的 duration = (prev change 的 ts) 到 (這個 change 的 prev_ts) 之間
        records = []
        last_change_ts = {}  # store_id → 最後一次插入 wait_changes 的時間
        for r in rows:
            ts, sid, sname, val, prev_val, prev_ts = (
                r["timestamp"], r["store_id"], r["store_name"],
                r["wait_time"], r["prev_value"], r["prev_ts"]
            )
            duration = None
            if prev_val is not None and sid in last_change_ts:
                t1 = datetime.strptime(last_change_ts[sid], "%Y-%m-%d %H:%M:%S")
                t2 = datetime.strptime(ts,                  "%Y-%m-%d %H:%M:%S")
                duration = int((t2 - t1).total_seconds() // 60)
            records.append((ts, sid, sname, val, prev_val, duration))
            last_change_ts[sid] = ts

        conn.executemany(
            """INSERT INTO wait_changes
               (timestamp, store_id, store_name, wait_time, prev_value, duration_min)
               VALUES (?, ?, ?, ?, ?, ?)""",
            records
        )
        print(f"  [backfill] 推導 {len(records)} 個變化事件至 wait_changes", flush=True)


def migrate_csv_if_needed():
    """若舊 CSV 存在且 DB 空的，就搬過去"""
    if not os.path.exists(OLD_CSV):
        return
    with db_connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM wait_log").fetchone()["n"]
        if count > 0:
            return
        rows = []
        with open(OLD_CSV, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append((
                    r["timestamp"], r["store_id"], r["store_name"], r["wait_time"],
                    r.get("num_1"), r.get("num_2"), r.get("num_3"), r.get("num_4"),
                    r.get("togo_numbers", ""), int(r.get("last_time") or 0)
                ))
        conn.executemany(
            """INSERT INTO wait_log
               (timestamp, store_id, store_name, wait_time,
                num_1, num_2, num_3, num_4, togo_numbers, last_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows
        )
        print(f"  [遷移] 從 CSV 匯入 {len(rows)} 筆歷史資料", flush=True)

    # 備份 CSV（保留存檔但不再寫入）
    backup = OLD_CSV + ".migrated"
    if not os.path.exists(backup):
        os.rename(OLD_CSV, backup)
        print(f"  [遷移] 舊 CSV 已改名為 {os.path.basename(backup)}", flush=True)


def db_insert(ts, results):
    """同時寫 wait_log（raw）+ wait_changes（只在變化時插）"""
    with db_connect() as conn:
        # 1) 寫 raw log
        conn.executemany(
            """INSERT INTO wait_log
               (timestamp, store_id, store_name, wait_time,
                num_1, num_2, num_3, num_4, togo_numbers, last_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(
                ts, r["store_id"], r["store_name"], r["wait_time"],
                r.get("num_1"), r.get("num_2"), r.get("num_3"), r.get("num_4"),
                r.get("togo_numbers", ""), int(r.get("last_time") or 0)
            ) for r in results]
        )

        # 2) 對每個 store 比對最後一筆變化，若值不同就插入
        for r in results:
            sid = r["store_id"]
            new_val = r["wait_time"]
            last = conn.execute(
                """SELECT timestamp, wait_time FROM wait_changes
                   WHERE store_id = ?
                   ORDER BY timestamp DESC LIMIT 1""",
                (sid,)
            ).fetchone()
            if last is None:
                # 此店第一筆變化（包含開站第一筆）
                conn.execute(
                    """INSERT INTO wait_changes
                       (timestamp, store_id, store_name, wait_time, prev_value, duration_min)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (ts, sid, r["store_name"], new_val, None, None)
                )
            elif last["wait_time"] != new_val:
                # 值變了 → 計算上一個值持續多久
                t1 = datetime.strptime(last["timestamp"], "%Y-%m-%d %H:%M:%S")
                t2 = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                duration = int((t2 - t1).total_seconds() // 60)
                conn.execute(
                    """INSERT INTO wait_changes
                       (timestamp, store_id, store_name, wait_time, prev_value, duration_min)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (ts, sid, r["store_name"], new_val, last["wait_time"], duration)
                )


def db_read_by_date(date_str):
    """讀指定日期（YYYY-MM-DD）的資料；DB 仍保留全部歷史"""
    from datetime import datetime as _dt, timedelta
    start = _dt.strptime(date_str, "%Y-%m-%d")
    end = start + timedelta(days=1)
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT timestamp, store_id, store_name, wait_time,
                      num_1, num_2, num_3, num_4, togo_numbers, last_time
               FROM wait_log
               WHERE timestamp >= ? AND timestamp < ?
               ORDER BY timestamp ASC, store_id ASC""",
            (start.strftime("%Y-%m-%d 00:00:00"),
             end.strftime("%Y-%m-%d 00:00:00"))
        ).fetchall()
    return [dict(r) for r in rows]


def db_latest_per_store():
    """每店最新一筆 raw 資料（給卡片用，含叫號）；排除無內用店家"""
    placeholders = ",".join("?" * len(EXCLUDED_STORE_IDS)) or "''"
    with db_connect() as conn:
        rows = conn.execute(f"""
            SELECT timestamp, store_id, store_name, wait_time,
                   num_1, num_2, num_3, num_4, togo_numbers, last_time
            FROM wait_log w1
            WHERE store_id NOT IN ({placeholders})
              AND timestamp = (
                SELECT MAX(timestamp) FROM wait_log w2
                WHERE w2.store_id = w1.store_id
              )
            ORDER BY store_id ASC
        """, tuple(EXCLUDED_STORE_IDS)).fetchall()
    return [dict(r) for r in rows]


def db_distinct_dates():
    """回傳 DB 中所有有資料的日期，新到舊"""
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(timestamp, 1, 10) AS d FROM wait_log ORDER BY d DESC"
        ).fetchall()
    return [r["d"] for r in rows]


def db_read_changes_by_date(date_str):
    """讀指定日期的變化事件 + 把每店「當日最早」的延伸 carry-over：
    為了讓 chart 在當日畫線時起點正確，每店在 date_str 之前最後一個值
    當作 date_str 00:00 的起點補進來。"""
    from datetime import datetime as _dt, timedelta
    start = _dt.strptime(date_str, "%Y-%m-%d")
    end = start + timedelta(days=1)
    start_str = start.strftime("%Y-%m-%d 00:00:00")
    end_str   = end.strftime("%Y-%m-%d 00:00:00")

    placeholders = ",".join("?" * len(EXCLUDED_STORE_IDS)) or "''"
    excluded = tuple(EXCLUDED_STORE_IDS)
    with db_connect() as conn:
        # (a) 該日內的變化事件（排除無內用店家）
        rows_in = conn.execute(
            f"""SELECT timestamp, store_id, store_name, wait_time,
                       prev_value, duration_min
                FROM wait_changes
                WHERE timestamp >= ? AND timestamp < ?
                  AND store_id NOT IN ({placeholders})
                ORDER BY timestamp ASC, store_id ASC""",
            (start_str, end_str) + excluded
        ).fetchall()

        # (b) 每店在 date_str 之前的最後一個值（carry-over），讓 chart 起點有值
        carry = conn.execute(
            f"""SELECT store_id, store_name, wait_time
                FROM wait_changes c1
                WHERE timestamp < ?
                  AND store_id NOT IN ({placeholders})
                  AND timestamp = (
                      SELECT MAX(timestamp) FROM wait_changes c2
                      WHERE c2.store_id = c1.store_id AND c2.timestamp < ?
                  )""",
            (start_str,) + excluded + (start_str,)
        ).fetchall()

    result = []
    seen_stores = set(r["store_id"] for r in rows_in)
    for c in carry:
        # 只在該日內這家店「沒有變化點」時才用 carry-over 撐起點
        # （有變化點時起點會有 prev_value 用 step chart 表達）
        if c["store_id"] not in seen_stores:
            result.append({
                "timestamp": start_str,
                "store_id": c["store_id"],
                "store_name": c["store_name"],
                "wait_time": c["wait_time"],
                "prev_value": None,
                "duration_min": None,
                "_carry": True,
            })
    for r in rows_in:
        result.append(dict(r))

    result.sort(key=lambda x: (x["timestamp"], x["store_id"]))
    return result


# ── 資料收集 ──────────────────────────────────────

def fetch_store(store_id):
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", API_URL,
         "-d", f"storeid={store_id}",
         "-H", "Content-Type: application/x-www-form-urlencoded"],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr}")
    data = json.loads(result.stdout)
    return data[0] if data else None


def fetch_all_stores():
    results = []
    for store in STORES:
        if store["id"] in EXCLUDED_STORE_IDS:
            continue  # 永久排除（如信義店）
        try:
            info = fetch_store(store["id"])
            if info and info.get("wait_time") != "無提供內用":
                info["store_name"] = store["name"]
                results.append(info)
        except Exception as e:
            print(f"  [警告] {store['name']} 查詢失敗: {e}", flush=True)
    return results


def monitor_loop():
    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            results = fetch_all_stores()
            if results:
                db_insert(now, results)
                summary = " | ".join(
                    f"{r['store_name']}:{r['wait_time']}分"
                    if r["wait_time"] != "-1" else f"{r['store_name']}:未營業"
                    for r in results
                )
                print(f"[{now}] {len(results)}店 — {summary}", flush=True)
            else:
                print(f"[{now}] 無資料", flush=True)
        except Exception as e:
            print(f"[{now}] 錯誤: {e}", flush=True)
        time.sleep(INTERVAL)


# ── Web 伺服器 ────────────────────────────────────

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        if parsed.path == "/api/data":
            qs = parse_qs(parsed.query)
            date_str = qs.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]
            self._json_response(db_read_by_date(date_str))
        elif parsed.path == "/api/dates":
            self._json_response(db_distinct_dates())
        elif parsed.path == "/api/changes":
            qs = parse_qs(parsed.query)
            date_str = qs.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]
            self._json_response(db_read_changes_by_date(date_str))
        elif parsed.path == "/api/latest":
            self._json_response(db_latest_per_store())
        elif parsed.path == "/api/stores":
            self._json_response(STORES)
        elif parsed.path == "/" or parsed.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
                self.wfile.write(f.read())
        else:
            super().do_GET()

    def _json_response(self, obj):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def log_message(self, *args):
        pass


# ── 主程式 ────────────────────────────────────────

def main():
    print("=" * 60, flush=True)
    print("  鼎泰豐全分店 — 候位監控 + Web 前端", flush=True)
    print(f"  前端: http://localhost:{PORT}", flush=True)
    print(f"  每 {INTERVAL} 秒查詢所有分店，Ctrl+C 停止", flush=True)
    print(f"  自動略過「無提供內用」的分店", flush=True)
    print(f"  DB: {os.path.basename(DB_FILE)}", flush=True)
    print("=" * 60, flush=True)

    ensure_db()
    migrate_csv_if_needed()
    backfill_changes_if_empty()

    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
