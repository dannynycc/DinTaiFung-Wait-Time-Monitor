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
# 連續快速崩潰時的退避上限。5 分鐘足以讓人發現「它沒起來」，
# 又不會久到真的可恢復的故障（例如暫時佔用的埠）要等太久。
RESTART_BACKOFF_MAX = 300
# 活過這個秒數就算「這次啟動是成功的」，退避歸零。
# 取 WARMUP + 兩輪健康檢查：撐得過兩次 /api/stores 就不是啟動即崩。
HEALTHY_UPTIME = WARMUP + CHECK_INTERVAL * 2

# 即使是用 pythonw.exe 起 watchdog，啟動 app.py 也用 python.exe（讓 print 能 flush 到檔）
PYTHON_EXE = sys.executable.replace("pythonw.exe", "python.exe")


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# log 輪替門檻。三個 log 都是 append 模式、從來沒有人清 ——
# 實測 server.log 已經長到 4.5 MB、server.err.log 2.9 MB
# （.gitignore 原本註解說「每次跑都會重寫」，那是錯的）。
# app.py 每 60 秒印一行 11 店摘要，約 100 KB/天，一年就是 36 MB。
# 保留一份 .1 當上一輪的紀錄，再舊的就沒有查閱價值了。
LOG_MAX_BYTES = 5 * 1024 * 1024


def rotate_if_big(path: str) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) >= LOG_MAX_BYTES:
            prev = path + ".1"
            if os.path.exists(prev):
                os.remove(prev)
            os.replace(path, prev)
            log(f"rotated {os.path.basename(path)} -> {os.path.basename(prev)}")
    except Exception as e:
        # 輪替失敗不該拖垮 watchdog：log 太大只是不方便，app 停掉才是真的問題
        log(f"rotate failed for {os.path.basename(path)}: {e}")


def start_app() -> subprocess.Popen:
    rotate_if_big(SERVER_OUT)
    rotate_if_big(SERVER_ERR)
    rotate_if_big(WATCHDOG_LOG)
    out_fh = open(SERVER_OUT, "ab")
    err_fh = open(SERVER_ERR, "ab")
    flags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW: child 不開 console
        # CREATE_NEW_PROCESS_GROUP: 不繼承父的 console signal handler
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    # Codex 2026-05-08 10:22 +08:00：Popen 建立後父行程關閉 log handle，避免 watchdog 常駐時多持有檔案描述。
    try:
        return subprocess.Popen(
            [PYTHON_EXE, "-u", APP_PY],
            cwd=BASE_DIR,
            stdout=out_fh,
            stderr=err_fh,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
    finally:
        out_fh.close()
        err_fh.close()


def http_ok() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as r:
            # Codex 2026-05-08 10:22 +08:00：讀完整 body，避免健康檢查提早斷線造成 app 端 ConnectionAbortedError。
            r.read()
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
    # 連續快速崩潰時要退避。原本不論死幾次都固定等 5 秒，
    # 若 app.py 是「一啟動就死」（5678 埠被占用、DB 損毀、語法錯誤都會這樣），
    # watchdog 會每 45 秒重啟一次、永遠不停，把 server.err.log 灌爆卻修不好任何事。
    #
    # 判斷「這次重啟有沒有用」的依據是**上一輪 app 活了多久**，
    # 不是重啟流程本身花了多久 —— 後者永遠等於 backoff + WARMUP，拿它當條件
    # 會讓退避永遠歸零、完全失效（這個版本第一次就是這樣寫錯的）。
    backoff = RESTART_BACKOFF
    app_started = time.time()
    time.sleep(WARMUP)

    while True:
        time.sleep(CHECK_INTERVAL)

        if proc.poll() is not None:
            lived = time.time() - app_started
            # 活得比 HEALTHY_UPTIME 久 → 上次重啟是有效的，退避歸零重新算；
            # 一啟動就死才加倍。
            backoff = (RESTART_BACKOFF if lived >= HEALTHY_UPTIME
                       else min(backoff * 2, RESTART_BACKOFF_MAX))
            log(f"app DEAD (exit={proc.returncode}, lived {lived:.0f}s); "
                f"restart in {backoff}s")
            time.sleep(backoff)
            proc = start_app()
            app_started = time.time()
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
                app_started = time.time()
                log(f"app restarted PID={proc.pid}")
                fails = 0
                time.sleep(WARMUP)


if __name__ == "__main__":
    main()
