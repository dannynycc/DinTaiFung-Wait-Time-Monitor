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
            CREATE INDEX IF NOT EXISTS idx_timestamp        ON wait_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_store_ts         ON wait_log(store_id, timestamp);
        """)


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
    with db_connect() as conn:
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


def db_read_all():
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT timestamp, store_id, store_name, wait_time,
                      num_1, num_2, num_3, num_4, togo_numbers, last_time
               FROM wait_log
               ORDER BY timestamp ASC, store_id ASC"""
        ).fetchall()
    return [dict(r) for r in rows]


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
        if self.path == "/api/data":
            self._json_response(db_read_all())
        elif self.path == "/api/stores":
            self._json_response(STORES)
        elif self.path == "/" or self.path == "/index.html":
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
