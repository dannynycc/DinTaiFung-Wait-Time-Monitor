# 鼎泰豐現場候位監控 · DinTaiFung Wait Time Monitor

即時監控鼎泰豐台灣全分店的現場候位時間，提供 Web 即時看板 + 變化事件分析。

![version](https://img.shields.io/badge/version-v2.1-brown) ![python](https://img.shields.io/badge/python-3.8%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

> 最後更新：2026-04-29 14:31

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

事件寫入 `watchdog.log`（含時間戳）。Process tree：

```
pythonw.exe  watchdog.py  ← 你啟動的（supervisor）
└─ python.exe  app.py     ← watchdog 自動 spawn + 監控
```

## 相依

- Python 3.8+（只用標準庫，**無需** `pip install`）
- `curl`（Windows 10+ / macOS / Linux 皆內建）

用 `curl` 而非 `requests` 是因為鼎泰豐伺服器憑證缺少 Subject Key Identifier，Python 的 SSL 驗證會拒絕，curl 預設較寬容。

## 檔案結構

```
.
├── app.py                    # 後端 + Web server（主程式）
├── watchdog.py               # Supervisor（pythonw 跑，自動重啟 app）
├── index.html                # 前端頁面（Chart.js + vanilla JS）
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

MIT

## 免責聲明

本專案僅使用鼎泰豐官網公開資料，供個人參考。
