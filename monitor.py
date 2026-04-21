"""
鼎泰豐新竹店 現場候位時間監控
每分鐘查詢一次，記錄到 CSV 並在 terminal 即時顯示
"""

import sys
import io
import time
import csv
import os
import json
import subprocess
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

API_URL = "https://www.dintaifung.tw/Queue/Home/WebApiTest"
STORE_ID = "0006"  # 新竹店
INTERVAL = 60      # 每 60 秒查詢一次
CSV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hsinchu_wait_log.csv")


def fetch_wait_time():
    """呼叫鼎泰豐 API 取得新竹店候位資訊（用 curl 避開 SSL 問題）"""
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", API_URL,
         "-d", f"storeid={STORE_ID}",
         "-H", "Content-Type: application/x-www-form-urlencoded"],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr}")
    data = json.loads(result.stdout)
    if not data:
        return None
    return data[0]


def format_status(info):
    """將 API 回傳格式化為可讀字串"""
    wt = info["wait_time"]
    if wt == "-1":
        return "尚未營業"
    return f"等候 {wt} 分鐘 | 叫號: {info['num_1']}/{info['num_2']}/{info['num_3']}/{info['num_4']}"


def ensure_csv():
    """確認 CSV 存在，沒有就建立 header"""
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "wait_time", "num_1", "num_2", "num_3", "num_4", "togo_numbers", "last_time"])


def append_csv(ts, info):
    """寫一筆到 CSV"""
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            ts,
            info["wait_time"],
            info["num_1"],
            info["num_2"],
            info["num_3"],
            info["num_4"],
            info["togo_numbers"],
            info["last_time"],
        ])


def main():
    ensure_csv()
    print("=" * 55, flush=True)
    print("  鼎泰豐新竹店 — 現場候位時間監控", flush=True)
    print(f"  每 {INTERVAL} 秒查詢一次，Ctrl+C 停止", flush=True)
    print(f"  記錄檔: {CSV_FILE}", flush=True)
    print("=" * 55, flush=True)

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            info = fetch_wait_time()
            if info is None:
                print(f"[{now}] API 回傳空資料", flush=True)
            else:
                status = format_status(info)
                print(f"[{now}] {status}", flush=True)
                append_csv(now, info)
        except Exception as e:
            print(f"[{now}] 錯誤: {e}", flush=True)

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
