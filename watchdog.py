"""
鼎泰豐 server watchdog — 監控 app.py 活性，斷線/掛掉自動重啟。

用 pythonw.exe 跑此檔，watchdog 本身不 attach console，不會被 console signal 殺。
watchdog 啟動 app.py 也用 CREATE_NO_WINDOW，子 process 同樣脫離 console。

行為：
  - 啟動 app.py，等 WARMUP 秒
  - 每 CHECK_INTERVAL 秒：
      a) process poll() 已退 → 立即重啟
      b) HTTP /api/stores 失敗連續 FAIL_LIMIT 次 → kill + 重啟
      c) HTTP 通 → 清空連敗計數
  - 所有事件寫入 watchdog.log（含時間戳）
"""

import os
import sys
import time
import subprocess
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_PY = os.path.join(BASE_DIR, "app.py")
WATCHDOG_LOG = os.path.join(BASE_DIR, "watchdog.log")
SERVER_OUT = os.path.join(BASE_DIR, "server.log")
SERVER_ERR = os.path.join(BASE_DIR, "server.err.log")

HEALTH_URL = "http://127.0.0.1:5678/api/stores"
CHECK_INTERVAL = 30
WARMUP = 10
FAIL_LIMIT = 3
RESTART_BACKOFF = 5

# 即使是用 pythonw.exe 起 watchdog，啟動 app.py 也用 python.exe（讓 print 能 flush 到檔）
PYTHON_EXE = sys.executable.replace("pythonw.exe", "python.exe")


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def start_app() -> subprocess.Popen:
    out_fh = open(SERVER_OUT, "ab")
    err_fh = open(SERVER_ERR, "ab")
    flags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW: child 不開 console
        # CREATE_NEW_PROCESS_GROUP: 不繼承父的 console signal handler
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        [PYTHON_EXE, "-u", APP_PY],
        cwd=BASE_DIR,
        stdout=out_fh,
        stderr=err_fh,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )


def http_ok() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main() -> None:
    log("=== watchdog started ===")
    proc = start_app()
    log(f"app started PID={proc.pid}")
    fails = 0
    time.sleep(WARMUP)

    while True:
        time.sleep(CHECK_INTERVAL)

        if proc.poll() is not None:
            log(f"app DEAD (exit={proc.returncode}); restart in {RESTART_BACKOFF}s")
            time.sleep(RESTART_BACKOFF)
            proc = start_app()
            log(f"app restarted PID={proc.pid}")
            fails = 0
            time.sleep(WARMUP)
            continue

        if http_ok():
            if fails > 0:
                log(f"health recovered after {fails} fail(s)")
            fails = 0
        else:
            fails += 1
            log(f"health FAIL {fails}/{FAIL_LIMIT} PID={proc.pid}")
            if fails >= FAIL_LIMIT:
                log("FAIL_LIMIT reached -> kill+restart")
                stop_proc(proc)
                time.sleep(RESTART_BACKOFF)
                proc = start_app()
                log(f"app restarted PID={proc.pid}")
                fails = 0
                time.sleep(WARMUP)


if __name__ == "__main__":
    main()
