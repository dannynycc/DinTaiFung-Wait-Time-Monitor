# 鼎泰豐現場候位監控 · DinTaiFung Wait Time Monitor

即時監控鼎泰豐台灣全分店的現場候位時間，提供 Web 即時看板 + 變化事件分析。

**線上看板 → https://dannynycc.github.io/DinTaiFung-Wait-Time-Monitor/**

![version](https://img.shields.io/badge/version-v3.5-brown) ![python](https://img.shields.io/badge/python-3.8%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

> 最後更新：2026-08-12 21:42 +08:00（桌機版分店卡片強制排成一列）

## 兩種跑法

| | 本機版 | 雲端版 |
|---|---|---|
| 抓取 | `app.py`（電腦要開著） | Cloudflare Worker cron |
| 儲存 | 本機 `wait_log.db` | Cloudflare D1 |
| 前端 | `index.html`（localhost:5678） | `docs/`（GitHub Pages） |
| 抓取時段 | 24 小時 | 台北 09:00–23:59 |

兩者互不干擾，可並存。本文件先講雲端版，本機版說明在後半。

## 雲端架構

```
Cloudflare Worker  (cron 每分鐘，台北 09:00-23:59)
      │  併發 11 次 POST 鼎泰豐 API
      ▼
   Cloudflare D1  (SQLite)
      │                          ╲  每日台北 03:00
      │ /api/latest /api/changes  ╲ GitHub Actions 匯出
      ▼                            ▼
 GitHub Pages 前端  ◀────  docs/data/YYYY-MM-DD.json
   即時卡片走 Worker API，歷史圖表走 repo 靜態檔
```

歷史資料刻意走 repo 靜態檔而非 Worker：不消耗 Worker 額度（該帳號與其他專案共用每日 10 萬次上限），且歷史資料在 GitHub 上直接可下載。

### 雲端版操作

```bash
cd worker
npm install
npx wrangler d1 execute dintaifung --remote --file=schema.sql   # 首次建表
npx wrangler deploy
npx wrangler tail                                                # 看即時 log
```

## 功能

### 資料蒐集
- 每 60 秒自動查詢鼎泰豐[官方候位查詢網頁](https://www.dintaifung.tw/Queue/?type=3)
- 自動略過「無提供內用」分店（目前是信義店）
- 雙表設計：
  - `wait_log` — 每分鐘 raw snapshot（完整 audit trail）
  - `wait_changes` — 只記錄 `wait_time` 變化事件（壓縮 ~16x，給 trend 用）

### Web 前端
- **日期下拉選單**：可切換看任何歷史日，跨午夜後自動出現新「今日」
- **即時狀態卡片**：每店一張，含現場等候 + 各桌型叫號 + **預計停止取號時間**（依時間早晚著色）
- **步階圖**：每店一條 stepped line，忠實表達「值維持到下次變化」
- **變化事件表格**：`時間 | 分店 | 5分→10分 | 前值持續 32 分鐘`，比每分鐘重複行有意義
- **單店篩選**：點卡片、點 Legend、或下拉選單，圖表+表格同步切換
- **手機/平板/桌機 RWD** 全支援
- 「今日」每 15 秒自動刷新；歷史日不刷新（省 CPU）
- 前端輪詢具備逾時取消與 in-flight guard，後端卡住或斷線時不會堆積未完成請求

## 快速啟動

雙擊 `start.bat`（推薦，含 watchdog 自動重啟）：

```
start.bat   # 背景啟動 watchdog + app（互動式略過已執行檢查）
stop.bat    # 停止整個 process tree
```

或直接跑（無 watchdog 守護，不建議生產用）：

```bash
python app.py
# 開 http://localhost:5678
```

### Watchdog 機制

`watchdog.py` 用 `pythonw.exe` 跑（GUI subsystem，完全脫離 console），每 30 秒檢查：

- `app.py` process 是否存活 → 死了就 5 秒後重啟
- HTTP `GET /api/stores` 是否回應 → 連續失敗 3 次 kill + 重啟
- 健康檢查會讀完整 response body，避免 supervisor 自己提早斷線造成 server 端錯誤 log

事件寫入 `watchdog.log`（含時間戳）。Process tree：

```
pythonw.exe  watchdog.py  ← 你啟動的（supervisor）
└─ python.exe  app.py     ← watchdog 自動 spawn + 監控
```

## 相依

- Python 3.8+（只用標準庫，**無需** `pip install`）
- `curl`（Windows 10+ / macOS / Linux 皆內建）

本機版用 `curl` 而非 `requests`，原因與憑證鏈有關。

> **2026-08-12 更正**：舊版說明寫「鼎泰豐伺服器憑證缺少 Subject Key Identifier」，暗示是站方憑證有問題 —— 這個描述不正確。實測從 GitHub Actions runner 以 OpenSSL 3.0.13 與 Node undici 連線，兩者都回報 `SSL_VERIFY=0`（驗證通過）。憑證本身沒問題，是 **Python `ssl` 模組在建立憑證鏈時**遇到中繼憑證缺 SKI 就無法完成 path building，較新的 OpenSSL 有替代的鏈結演算法。這也是雲端版能直接用標準 `fetch`、完全不需要 `curl` 的原因。

> Codex 2026-05-07 23:22 +08:00：`app.py` 會先以 bytes 接收 `curl` 的 stdout/stderr，再用 UTF-8 解碼，避免 Windows 預設 cp950 造成背景抓取執行緒拋出 `UnicodeDecodeError`。

> Codex 2026-05-08 10:22 +08:00：`app.py` 改用 threaded HTTP server，client 斷線時不再把 `ConnectionAbortedError` traceback 持續寫入 `server.err.log`；`watchdog.py` 啟動子行程後會關閉父行程持有的 log handle；前端輪詢新增 10 秒逾時取消。

## 檔案結構

```
.
├── worker/                   # ── 雲端版 ──
│   ├── src/index.js          #   Worker：cron 抓取 + 每日 roll-up + 唯讀 API
│   ├── schema.sql            #   D1 schema（wait_log / wait_changes / stop_changes
│   │                         #             / daily_summary / fetch_health）
│   └── wrangler.toml         #   部署設定（cron 時段、D1 綁定）
├── docs/                     # GitHub Pages 站台
│   ├── index.html            #   前端（時區已改為固定台北）
│   └── data/                 #   每日變化事件靜態檔 + index.json
├── tools/
│   └── export_history.py     # 本機 SQLite → docs/data JSON
├── .github/workflows/
│   └── daily-export.yml      # 每日台北 03:00 從 D1 匯出進 repo
│
├── app.py                    # ── 本機版 ── 後端 + Web server
├── watchdog.py               # Supervisor（pythonw 跑，自動重啟 app）
├── index.html                # 本機前端（Chart.js + vanilla JS）
├── start.bat                 # 雙擊啟動（背景 hidden）
├── stop.bat                  # 雙擊停止整個 process tree
├── wait_log.db               # SQLite 資料庫（自動產生）
│                              ├─ wait_log    每分鐘 raw 紀錄
│                              └─ wait_changes 變化事件（推導+持續累積）
├── server.log / server.err.log  # app.py 執行 log（gitignore）
├── watchdog.log              # watchdog 事件 log（gitignore）
├── README.md
└── CHANGELOG.md
```

## API

`app.py` 啟動後會在 `:5678` 提供：

| Endpoint | 說明 |
|---|---|
| `GET /` | 網頁前端 |
| `GET /api/changes?date=YYYY-MM-DD` | 指定日期的變化事件（chart + 表格） |
| `GET /api/latest` | 每店最新一筆 raw（給卡片用，含叫號 + last_time） |
| `GET /api/dates` | DB 中所有有資料的日期（給日期下拉用） |
| `GET /api/data?date=YYYY-MM-DD` | 指定日期 raw 紀錄（保留供 audit/debug） |
| `GET /api/stores` | 分店清單 |

雲端版（`https://dintaifung-queue.dannynycc.workers.dev`）提供同名端點，另加一個：

| Endpoint | 說明 |
|---|---|
| `GET /api/health` | 最近 20 次 cron 抓取紀錄（成功／失敗店數、耗時、錯誤訊息）。用來判斷「某段時間沒資料」是店家沒開還是排程掛了。 |

| `GET /api/summary?date=` | 每日彙總（roll-up 產物）：當日最長候位、各桌型首末叫號、原 raw 筆數 |
| `GET /api/stops` | 止號時間事件流 |

雲端版沒有 `/api/data`（raw 逐筆），raw 只在 D1 保留約 2–3 天供 `/api/latest` 使用。

## 每日 roll-up：raw 怎麼被壓成 1/13

raw 當天完整收集（前端卡片需要叫號與止號），過保留期後轉成「只留變化的地方」再刪除。

關鍵在**不要對整列做判斷**。實測 25 天真實資料，各欄位每店每天的變動次數差了 180 倍：

| 欄位 | 次數/店/天 | 處理方式 |
|---|---|---|
| `num_1` | 217.5 | 滾成每日彙總後丟棄 |
| `num_2` | 148.7 | 同上 |
| `togo_numbers` | 121.9 | 同上 |
| `num_3` | 65.6 | 同上 |
| `wait_time` | 53.3 | → `wait_changes` 完整事件流（無損） |
| `num_4` | 37.0 | 滾成每日彙總後丟棄 |
| `last_time` | **1.2** | → `stop_changes` 完整事件流（幾乎免費） |

「任一欄位變了才記一筆」會被 `num_1` 這種叫號計數器綁架，只壓 2.57x。分開處理才有效：

| 方案 | 每天成長 | D1 500 MB 可撐 |
|---|---|---|
| 不處理 | 881.0 KB | 1.6 年 |
| 整列事件化 | 368.1 KB | 3.8 年 |
| **每欄分開處理** | **68.5 KB** | **20 年** |

`wait_time` 與 `last_time` 存成事件流是**無損**的 —— 兩者都是步階函數，記錄每次轉換就能完美還原任一分鐘的值。被真正丟棄的只有叫號的分鐘級細節，其衍生指標（當日叫了幾組）保存在 `daily_summary`。

## 資料 Schema

### `wait_log` — 每分鐘 raw
```
timestamp, store_id, store_name, wait_time,
num_1, num_2, num_3, num_4, togo_numbers, last_time
```

### `wait_changes` — 變化事件
```
timestamp, store_id, store_name, wait_time,
prev_value,        -- 前一個值（NULL = 該店首筆）
duration_min       -- 前一個值持續了多少分鐘
```

## 分店對照

| storeId | 分店 |
|---|---|
| 0001 | 信義店（永久排除：無提供內用） |
| 0003 | 復興店 |
| 0005 | 天母店 |
| 0006 | 新竹店 |
| 0007 | 101店 |
| 0008 | 台中店 |
| 0009 | 板橋店 |
| 0010 | 高雄店 |
| 0011 | 南西店 |
| 0012 | A4店 |
| 0013 | A13店 |
| 0015 | 新生店 |

## 客製化

改 `app.py` 頂部的常數即可：

```python
INTERVAL = 60                       # 查詢頻率（秒）
PORT = 5678                         # Web server 埠號
EXCLUDED_STORE_IDS = {"0001"}       # 永久排除分店
```

要改監控的分店清單，編輯 `STORES` 列表。

## 趨勢觀察筆記

`wait_changes` 表搭配 `last_time` 欄位（預計停止取號時間）可以推出更深層的指標：

- **「停止取號時間」是熱度硬指標**：越早停 → 越熱門
  - 板橋店 04-25: 18:20 停 → 最熱
  - A4 店 04-25: 19:50 停 → 較不熱
- **凍結值**：店家停止取號後，wait_time 會凍結在最後一個值直到收店
  - `duration_min` 異常大（如 4 小時）= 已停止取號
- **歸 0** vs **凍結高值**：能歸 0 的店代表晚間真的排空；凍結高值的店代表停止取號時還有大量等候

## 授權

**程式碼 — MIT**（見 [LICENSE](LICENSE)）。

**資料 — 不主張著作權**（見 [DATA-LICENSE.md](DATA-LICENSE.md)）。`docs/data/` 下的候位紀錄是自鼎泰豐官方公開端點蒐集的**事實觀測**。事實本身不受著作權保護，本專案不擁有也不主張這些資料的權利，公開供自由取用（含商業用途），標示來源不強制但歡迎。

> 兩份分開放是刻意的：GitHub 以全文比對辨識授權，`LICENSE` 檔摻入任何非標準內容都會讓它從 `MIT` 掉成 `Other`，反而使程式碼授權變模糊。

> 2026-08-12 更正：v1.2 起網頁頁尾曾宣告 CC BY-NC-SA 4.0，與 README 的 MIT 並存且互相矛盾。實際上兩者是在講不同東西（程式碼 vs 資料），而且當時 repo 根本沒有 `LICENSE` 檔，兩個宣告都不具法律效力。現已分開釐清：程式碼走 MIT 並補上授權檔；資料不再套用 CC BY-NC-SA —— 因為那是自公開端點取得的事實，本專案無權對外授權自己不擁有的東西，NC 條款也會擋掉媒體引用、學術分析等正當用途。

## 免責聲明

本專案僅使用鼎泰豐官網公開資料，供個人參考。與鼎泰豐無任何關係。
