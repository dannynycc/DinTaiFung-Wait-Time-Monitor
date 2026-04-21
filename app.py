"""
鼎泰豐全分店 — 候位監控 + Web 前端
啟動後開 http://localhost:5678 看圖表和表格
自動略過「無提供內用」的分店
"""

import sys
import io
import os
import csv
import json
import time
import subprocess
import threading
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

API_URL = "https://www.dintaifung.tw/Queue/Home/WebApiTest"
INTERVAL = 60
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "all_branches_log.csv")
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

STORE_NAME_MAP = {s["id"]: s["name"] for s in STORES}


# ── 資料收集 ──────────────────────────────────────

def fetch_store(store_id):
    """查詢單一分店"""
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
    """查詢所有分店，跳過無提供內用"""
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


def ensure_csv():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                "timestamp", "store_id", "store_name", "wait_time",
                "num_1", "num_2", "num_3", "num_4",
                "togo_numbers", "last_time"
            ])


def append_csv(ts, results):
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for info in results:
            writer.writerow([
                ts, info["store_id"], info["store_name"], info["wait_time"],
                info["num_1"], info["num_2"], info["num_3"], info["num_4"],
                info["togo_numbers"], info["last_time"]
            ])


def read_csv():
    rows = []
    if not os.path.exists(CSV_FILE):
        return rows
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def monitor_loop():
    ensure_csv()
    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            results = fetch_all_stores()
            if results:
                append_csv(now, results)
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
            rows = read_csv()
            self._json_response(rows)
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
    print("=" * 60, flush=True)

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
