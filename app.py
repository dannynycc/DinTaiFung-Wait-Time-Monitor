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
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

API_URL = "https://www.dintaifung.tw/Queue/Home/WebApiTest"
INTERVAL = 60
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "wait_log.db")
OLD_CSV = os.path.join(BASE_DIR, "all_branches_log.csv")
PORT = 5678
# 只監聽本機。要讓區網其他裝置連（例如用手機看），啟動前設 DTF_HOST=0.0.0.0，
# 但先確認你信任那個網路 —— 見 main() 裡的說明。
HOST = os.environ.get("DTF_HOST", "127.0.0.1")

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

    # 唯一索引與 D1 那邊對齊（worker/migrations/002_review_fixes.sql）。
    # README 一直寫「D1 schema 與本機 wait_log.db 完全同構」，但雲端加了這兩個
    # 索引之後本機沒跟上，兩邊其實已經分岔 —— 現在補回來。
    # 搭配下方 db_insert 的 INSERT OR IGNORE，同一分鐘的重複寫入被安靜忽略。
    #
    # 分開執行並容錯：既有 DB 若剛好有重複列，建索引會失敗。那時不該讓整個
    # 程式起不來 —— 沒有唯一索引只是少一層保險，服務停掉才是真的問題。
    # （本機 wait_log.db 實測 257,049 + 14,645 筆，重複組合 0，會直接建成功。）
    with db_connect() as conn:
        for name, table in (("ux_wait_log_ts_store", "wait_log"),
                            ("ux_wait_changes_ts_store", "wait_changes")):
            try:
                conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table}(timestamp, store_id)"
                )
            except sqlite3.IntegrityError:
                print(f"  [警告] {table} 有重複的 (timestamp, store_id)，"
                      f"略過唯一索引 {name}", flush=True)


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
            """INSERT OR IGNORE INTO wait_changes
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
            """INSERT OR IGNORE INTO wait_log
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


# ── 本機前端 ──────────────────────────────────────
#
# 本機版原本有自己的 index.html，是 docs/index.html 的手工複本。
# 兩份各自演進的結果是分歧了 925 行、docs 版多出 14 個函式：
# 日曆式日期選擇、明確的連線錯誤狀態、抓取失敗就不延伸線條的保護、
# 固定台北時區、卡片的鍵盤無障礙 —— 本機版一個都沒有。
# 而 CHANGELOG 每次都寫「root/index.html 同步」，其實只同步了那一次改到的地方。
#
# 手動再同步一次只會再漂移一次。改成本機版直接用 docs/index.html，
# 差異用替換處理 —— 剩下一份前端，漂移在結構上就不可能發生。
#
# 每個替換都必須命中，否則丟例外。靜默失敗會產生一個「載入得起來但行為不對」
# 的頁面（例如 API_BASE 沒換掉，本機版就會去打線上 Worker 而不是本機 DB），
# 那比整頁壞掉更難發現。
FRONTEND_SRC = os.path.join(BASE_DIR, "docs", "index.html")

FRONTEND_PATCHES = [
    # (說明, 原字串, 取代為)
    ("API 走同源",
     "const API_BASE = 'https://dintaifung-queue.dannynycc.workers.dev';",
     "const API_BASE = '';   // 本機版：同源相對路徑"),

    # 本機沒有 docs/data 靜態檔，歷史日一律回頭問 API（DB 保留全部 raw）。
    # 指向一個真的存在、回空陣列的端點，而不是留著會 404 的路徑 ——
    # archiveDates 因此是空集合，fetchChanges 的靜態檔分支永遠不會進去。
    ("歷史檔索引指向本機空端點",
     "const ARCHIVE_BASE = 'data';",
     "const ARCHIVE_BASE = '/api/archive';   // 本機版：無靜態歷史檔"),

    # 本機版 24 小時收集，沒有抓取視窗。影響 x 軸預設範圍與
    # 「非抓取時段」的判斷 —— 沿用雲端的 09:00–21:30 會把凌晨的資料
    # 畫到軸外，並把正常的深夜抓取標成異常。
    ("時間軸改為 24 小時",
     "const FETCH_WINDOW_START_MIN = 9 * 60;         // 09:00",
     "const FETCH_WINDOW_START_MIN = 0;              // 本機版 24 小時收集"),
    ("時間軸改為 24 小時（結束）",
     "const FETCH_WINDOW_END_MIN = 21 * 60 + 30;     // 21:30",
     "const FETCH_WINDOW_END_MIN = 24 * 60 - 1;      // 本機版 24 小時收集"),

    # 標題加註，避免對著本機版的畫面以為在看線上站（兩者資料來源不同）。
    ("標題加註本機版",
     "<title>鼎泰豐全分店 — 候位監控</title>",
     "<title>鼎泰豐全分店 — 候位監控（本機版）</title>"),
]


def build_local_html():
    """讀 docs/index.html，套上本機設定後回傳 bytes。"""
    if not os.path.exists(FRONTEND_SRC):
        raise RuntimeError(
            f"找不到前端來源 {FRONTEND_SRC}。"
            "本機版現在直接使用 docs/index.html，請確認 docs/ 目錄完整。"
        )
    with open(FRONTEND_SRC, encoding="utf-8") as f:
        html = f.read()

    for label, old, new in FRONTEND_PATCHES:
        if old not in html:
            raise RuntimeError(
                f"前端替換失敗（{label}）：docs/index.html 裡找不到目標字串。\n"
                f"  找的是：{old}\n"
                "docs/index.html 改過寫法時要同步更新 app.py 的 FRONTEND_PATCHES。"
            )
        html = html.replace(old, new, 1)
    return html.encode("utf-8")


def db_insert(ts, results):
    """同時寫 wait_log（raw）+ wait_changes（只在變化時插）"""
    with db_connect() as conn:
        # 1) 寫 raw log
        conn.executemany(
            """INSERT OR IGNORE INTO wait_log
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
                    """INSERT OR IGNORE INTO wait_changes
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
                    """INSERT OR IGNORE INTO wait_changes
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
        capture_output=True, timeout=15
    )
    # Codex 2026-05-07 23:22 +08:00：避免 Windows 預設 cp950 解碼 curl 輸出時拋例外。
    stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
    stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {stderr}")
    data = json.loads(stdout)
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
        # 注意：這裡刻意「不」呼叫 super().do_GET() 當 fallback。
        # 原本未知路徑會落到 SimpleHTTPRequestHandler 的靜態檔服務（directory=BASE_DIR），
        # 等於把整個專案目錄開放出去 —— 實測 GET /wait_log.db 回 200（39 MB 的完整
        # 候位資料庫）、GET /repro.py 回 200、GET / 還會列出目錄。搭配下方原本綁在
        # 0.0.0.0 的監聽，同一個區網裡的任何裝置都拿得到 wait_log.db、server.log、
        # worker/wrangler.toml。
        # 前端不需要任何本地靜態檔（Chart.js 走 CDN、favicon 是 data URI），
        # 所以最安全也最簡單的做法就是白名單以外一律 404。
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
            # 契約對齊 Worker 的 /api/stores：欄位名 store_id / store_name，
            # 且排除永久停用的分店。前端拿它當「權威分店清單」——
            # 回 {"id","name"} 的話，卡片標題會全部變成 undefined；
            # 沒排除信義店的話，畫面會多一張永遠寫著「資料中斷」的卡片。
            self._json_response([
                {"store_id": s["id"], "store_name": s["name"]}
                for s in STORES if s["id"] not in EXCLUDED_STORE_IDS
            ])
        elif parsed.path == "/api/archive/index.json":
            # 本機版沒有 docs/data 靜態歷史檔（DB 裡就有完整 raw）。
            # 回空陣列讓前端的「靜態檔優先」分支自然關閉，
            # 而不是讓它去打一個必定 404 的路徑再吞掉錯誤。
            self._json_response([])
        elif parsed.path == "/" or parsed.path == "/index.html":
            # 替換失敗要看得見。丟出去變成 500 + 一頁 traceback 的話，
            # 沒人會知道原因是「docs/index.html 改了寫法、FRONTEND_PATCHES 沒跟上」。
            try:
                payload = build_local_html()
            except RuntimeError as e:
                self._bytes_response(
                    ("<!DOCTYPE html><meta charset=utf-8>"
                     "<h1>本機前端組不起來</h1><pre>" + str(e) + "</pre>").encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                print(f"  [錯誤] 前端組裝失敗: {e}", flush=True)
                return
            self._bytes_response(payload, "text/html; charset=utf-8")
        else:
            self.send_error(404, "Not Found")

    def _json_response(self, obj):
        self._bytes_response(
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            cors=True,
        )

    # Codex 2026-05-08 10:22 +08:00：常駐服務穩定性修補，client 斷線時避免 traceback 持續寫入 server.err.log。
    def _bytes_response(self, payload, content_type, cors=False):
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def log_message(self, format, *args):   # noqa: A002 - 對齊基底類別的簽名
        pass                                 # 每筆請求都印會把 server.log 灌爆


# Codex 2026-05-08 10:22 +08:00：改用 threaded server，避免單一慢請求阻塞 watchdog 健康檢查。
class LongRunningHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


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

    # 綁 127.0.0.1 而不是 0.0.0.0。原本綁 0.0.0.0 等於把服務開放給整個區網，
    # 但這個服務從來就只給本機看（watchdog 的健康檢查用的也是 127.0.0.1，
    # README 寫的也是 http://localhost:5678）—— 對外監聽沒有帶來任何功能，
    # 只帶來暴露面。真的需要用手機在區網看時，設 DTF_HOST=0.0.0.0 再開一次，
    # 那是明確的選擇而不是預設值。
    server = LongRunningHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
        server.shutdown()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
