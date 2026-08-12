# Changelog

## v3.1 — 2026-08-12 18:44

每日 roll-up：raw 當天照常完整收集，過保留期後轉成「只留變化的地方」再刪除。D1 的可用年限從 1.6 年延長到 20 年。

起點是 Danny 的提問：「raw 每天 1.5 MB 為什麼這麼大？資料結構不是已經 reduce 過了嗎？」

### 查證：先前的數字是錯的
- v3.0 記載「raw 每天約 15,100 筆、約 1.5 MB，不修剪約 330 天撐爆」是**估算值**，且拿本機 24 小時抓取的量直接套用到只跑 15 小時的雲端版。實測（各表拆進獨立 DB、VACUUM 後量檔案大小）為 **881 KB/天、581 天（1.6 年）**，高估了 1.8 倍。
- v2.0 的 16.6x 壓縮是套用在「前端讀什麼」，不是「存什麼」—— 該版 CHANGELOG 明載 raw「一筆不少地寫」。raw 從未被壓縮，它是刻意保留的 audit trail。
- raw 有 **44% 的體積是索引**（19.42 MB → 加兩個索引變 34.43 MB），不是資料本身。

### 實測：各欄位變動頻率差 180 倍
每店每天變動次數（25 天真實資料）：

| 欄位 | 次數 | | 欄位 | 次數 |
|---|---|---|---|---|
| `num_1` | 217.5 | | `wait_time` | 53.3 |
| `num_2` | 148.7 | | `num_4` | 37.0 |
| `togo_numbers` | 121.9 | | **`last_time`** | **1.2** |
| `num_3` | 65.6 | | | |

因此「整列事件化」（任一欄位變了才記一筆）**只壓縮 2.57x**（257,049 → 100,034 筆），被 `num_1` 這種計數器綁架。正解是每欄各自處理。

### 新增
- `stop_changes` 表 — 止號時間事件流。全期 25 天 11 間店僅 333 筆，保存成本近乎零，保住 README 稱為「熱度硬指標」的資料。
- `daily_summary` 表 — 每日彙總（當日最長候位、各桌型首末叫號、外帶叫號狀態數、原 raw 筆數稽核值）。每天 11 筆。
- `rollupRawLog()` — 每日台北 09:02 執行一次。三個 statement 同一 batch（單一交易），推導失敗就不會刪到 raw。
- `GET /api/summary?date=` 與 `GET /api/stops` 兩個端點。
- `ROLLUP_AT` 可用環境變數覆寫，讓本機測試能觸發同一條 production 程式碼路徑，不需改常數再改回來。

### 變更
- `pruneRawLog()`（30 天硬刪）改為 `rollupRawLog()`。
- `RAW_RETENTION_DAYS` 30 → 2。實際保留約 2–3 天：cutoff 取「兩天前的 00:00」，所以某日資料會活到第三天早上。刻意留這個安全邊際，因為 roll-up 不可逆。

### 效果（實測，非估算）
| 方案 | 每天成長 | D1 500 MB 可撐 |
|---|---|---|
| 不處理 | 881.0 KB | 1.6 年 |
| 整列事件化 | 368.1 KB | 3.8 年 |
| **每欄分開處理（本版）** | **68.5 KB** | **20 年** |

壓縮 12.9x。另有恆定 1.72 MB 的熱資料（保留窗內的 raw），不隨時間成長。

### 測試
- **跨日邊界**：`stop_changes` 推導 SQL 以 25 天真實資料**逐日**模擬執行（而非一次跑完），確保會踩到「每天只看得到當天 raw、`LAG()` 首筆回 NULL」的陷阱。天真寫法會每天每店多產生一筆假事件，25 天累積 275 筆垃圾且值是對的、事後幾乎看不出來。解法為 `COALESCE(LAG(...), 從 stop_changes 撈該店最後值)`。
- 結果與獨立 Python 推導的 ground truth **333 筆逐筆一致**。
- 本機灌入 257,049 筆真實 raw，透過 `wrangler dev` 觸發**同一條 production 程式碼路徑**執行 roll-up，六角度驗證：ground truth 比對、`daily_summary` 抽 3 天交叉比對原始資料、`wait_changes` 未被誤動、保留窗內 raw 完好、空間效果、新端點回應。
- 正式站部署後 7 個端點全數 200，既有資料完好。

### 更正
- 本次測試中一度得到「壓縮 238.9x」，該數字**灌水**：測試庫的 `wait_changes` 只有當次 session 的 13 筆，而非真實的 14,645 筆。補回真實資料後重算為 12.9x，文件一律採用後者。

## v3.0 — 2026-08-12 17:42

上雲。從「電腦要開著才有資料」的本機服務，改成 Cloudflare Worker 全天候抓取 + GitHub Pages 公開看板。本機版（`app.py` / `watchdog.py` / 根目錄 `index.html`）完整保留，兩者可並存。

### 新增
- `worker/` — Cloudflare Worker，兩個角色：cron 每分鐘抓 11 間分店寫入 D1；提供唯讀 API 給前端。
  - 正式網址 `https://dintaifung-queue.dannynycc.workers.dev`
  - 排程 `* 1-15 * * *`（UTC）= 台北 09:00–23:59，每天 900 次。深夜店家已停止取號，抓回來只是凍結值。
  - 11 店改為併發抓取。序列版實測 12.4 秒，併發約 0.3–0.5 秒。
  - 新增 `fetch_health` 表，每個 cron 週期記一筆。雲端排程失敗是靜默的，沒有這張表就分不出「某段時間沒資料」是店家沒開還是排程掛了。
- `docs/` — GitHub Pages 前端，由根目錄 `index.html` 移植。
- `docs/data/*.json` — 2026-04-21～05-17 共 23 天、14,645 筆變化事件（1.87 MB），從本機 `wait_log.db` 匯出。
- `tools/export_history.py` — 本機 SQLite 匯出成靜態 JSON 的工具。
- `.github/workflows/daily-export.yml` — 每日台北 03:00 把前一日事件從 D1 匯出進 repo，一天一個 commit。空資料會中止而不是用空檔覆蓋既有檔案。

### 修正
- 前端時區。原本用瀏覽器當地時間算「今天」與「止號時間」，本機自用沒問題（人與機器都在台灣），但公開網頁不行 —— 倫敦訪客的「今天」會錯一天。改為一律以台北時間（固定 UTC+8）計算。
- 前端輪詢由 15 秒改為 60 秒，對齊後端 cron 頻率。原本 3/4 的請求是重複的。

### 查證與更正
- README 原本記載「鼎泰豐憑證缺 Subject Key Identifier，Python 的 SSL 驗證會拒絕」，暗示站方憑證有問題。實測從 GitHub runner（美國 Azure IP）以 OpenSSL 3.0.13 與 Node undici 連線，兩者皆回報 `SSL_VERIFY=0`（驗證通過）。真正原因是 **Python `ssl` 模組建立憑證鏈的限制**，非站方憑證有問題。離開 Python 後 `curl` 相依即可移除。
- 同一次探測確認 API 無地區封鎖，美國 IP 可正常存取。

### 已知限制
- Worker cron 的 CPU 時間實測 7–8 ms，Workers Free 方案上限為 10 ms，餘裕僅 20–30%。若超標該次 cycle 會被中止，症狀是資料出現分鐘級空洞。`fetch_health` 表與每日匯出的健康檢查即為偵測手段。
- 部署後約 30 秒內出現過兩次 HTTP 500，其後 45 次連續請求（3 端點各 15 次）全數 200，未能重現。**根因未確認**，持續觀察中。
- ~~D1 免費方案單一資料庫上限 500 MB。`wait_log` 每天約 15,100 筆、約 1.5 MB，保留 30 天後硬刪。~~ **此段數字為估算且有誤，已於 v3.1 實測更正為 881 KB/天；保留策略亦於 v3.1 改為每日 roll-up。**

### 測試
- 本機 `wrangler dev --local`：六角度回歸（重複變化偵測、`duration_min` 計算、D1 參數上限、時區、日期過濾邊界、快取標頭）。
- 正式站：cron 連續分鐘級寫入確認、45 次 API 請求全數 200、真 Chrome 讀 DOM 驗證 24 個日期選項／11 張卡片／11 條圖表線。
- 歷史日路徑：切換至 2026-05-17 確認讀取 `docs/data/2026-05-17.json`，且零次 Worker `/api/changes` 呼叫。

## v2.3 — 2026-05-08 10:22

Codex 針對常駐服務做記憶體與斷線恢復檢查：未發現典型線性 memory leak，並補強 HTTP server、watchdog 與前端輪詢，降低長時間執行時的錯誤 log 膨脹與請求堆積風險。

### 修正
- `app.py` JSON/HTML 回應統一走 `_bytes_response()`，client 已斷線時安靜略過 `BrokenPipeError`、`ConnectionAbortedError`、`ConnectionResetError`，避免 `server.err.log` 被正常斷線 traceback 持續灌大。
- `watchdog.py` 健康檢查會讀完整 response body，避免 supervisor 自己提早關閉連線造成 app 端 `ConnectionAbortedError`。
- `watchdog.py` 啟動 `app.py` 後明確關閉父行程持有的 stdout/stderr log handle，避免 watchdog 常駐時多持有檔案描述。

### 變更
- `app.py` 從單執行緒 `HTTPServer` 改為 `ThreadingHTTPServer` 包裝的 `LongRunningHTTPServer`，避免單一慢請求阻塞 `/api/stores` 健康檢查。
- `index.html` 前端輪詢新增 in-flight guard、10 秒 fetch timeout 與日期切換時的舊請求取消，後端卡住或斷線時不會持續疊未完成請求。
- `README.md` 版本徽章更新為 `v2.3`，補上本次常駐穩定性與斷線恢復說明。
- 本次修改檔案皆依使用者要求標註 `Codex 2026-05-08 10:22 +08:00`。

### 測試
- `python -m py_compile app.py watchdog.py`
- `GET /` 回應 `200`
- `GET /api/latest` 回應 `200`
- 連續 240 次 API 請求粗略壓測：`app.py` 工作集曾上升後回落，未呈現線性 memory leak 型態
- 重啟 watchdog/app 後確認僅保留一組背景行程，且 `server.err.log` 未再新增健康檢查造成的斷線 traceback

## v2.2 — 2026-05-07 23:22

Codex 首次接手維護：修正 Windows 環境下 `curl` 輸出被預設 cp950 解碼導致背景抓取執行緒拋出 `UnicodeDecodeError` 的問題，並同步 README 與版本資訊。

### 修正
- `app.py` 呼叫 `curl` 時改以 bytes 接收 stdout/stderr，再明確用 UTF-8 解碼，避免分店查詢結果因 Windows 預設碼頁解碼失敗而變成 `None`。

### 變更
- `README.md` 版本徽章更新為 `v2.2`。
- `README.md` 補上 Codex 接手維護時間與本次編碼修正說明。
- 本次起 CHANGELOG 依使用者要求以繁體中文撰寫。

### 測試
- `python -m py_compile app.py watchdog.py`
- `GET /`、`GET /api/stores`、`GET /api/latest` 均回應 `200`
- 確認 `:5678` 正在 Listen，且 watchdog/app 僅保留一組背景行程

## v2.1 — 2026-04-29 14:31

維運強化：補上 watchdog 自動重啟機制與 start/stop 腳本，解決 server 偶發離線無人重拉的問題。

### Added
- **`watchdog.py`** — 獨立 supervisor process，用 `pythonw.exe` 跑（GUI subsystem 不 attach console，免疫 console signal）
  - 每 30 秒檢查 `app.py` process 狀態 + HTTP `/api/stores` 健康檢查
  - process 退出 → 5 秒 backoff 後重啟
  - HTTP 連續失敗 3 次 → kill + 重啟
  - 所有事件寫 `watchdog.log`（含時間戳）
- **`start.bat`** — 雙擊啟動 watchdog（背景 hidden）
  - 已在執行 → 偵測 PID 並略過，不會重複啟動
  - 偵測到孤兒 `app.py`（watchdog 已死但 app 還在）→ 自動清掉再起新 watchdog
- **`stop.bat`** — 雙擊停止整個 process tree（先殺 watchdog 避免重啟競爭，再殺 app）

### Fixed (root cause)
- **server 自己掛掉之謎**：`HTTPServer.serve_forever()` 在 main thread，console close signal (`CTRL_CLOSE_EVENT`) 會打斷它並被 `try/except KeyboardInterrupt` 安靜吞掉，留下空 stderr + log 戛然而止的特徵。原先用 `Start-Process python.exe` 啟動仍 attach 在 hidden console，PowerShell session 結束時連帶被殺
- 改用 `pythonw.exe` 啟 watchdog + `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` 啟 app.py，整個 process tree 完全脫離 console session

### Changed
- `.gitignore` 加入 `watchdog.log`（runtime log，不需追蹤）

### Tested
9 種情境逐一驗證（見 commit message）：全停→啟、running→啟（略過）、running→停、空→停、連兩次啟、kill app（自動重啟）、kill watchdog（app 變孤兒）、孤兒→啟（自動清乾淨）、回歸測試。

## v2.0 — 2026-04-27

大版本：資料層改用 event-sourced 設計、UI 從「分鐘 log」改為「變化事件」。

### Added — 資料層
- **`wait_changes` 表**：只記錄 `wait_time` 變化事件（含 `prev_value`、`duration_min`），相對 raw `wait_log` 壓縮 **16.6x**（64,748 → 3,896 筆）
- **啟動時自動 backfill**：用 SQL `LAG()` window function 從既有 `wait_log` 推導變化事件
- **`db_insert` 雙寫**：raw 一筆不少地寫 `wait_log`，僅在值真的變化時才插 `wait_changes`
- **`EXCLUDED_STORE_IDS`**：永久排除信義店（API 永遠 -1，無內用）

### Added — API
- `GET /api/changes?date=YYYY-MM-DD` — 回傳指定日期變化事件，含 carry-over（每店前一日最後值補當日 00:00 起點）
- `GET /api/latest` — 回傳每店最新一筆 raw（給卡片用，含叫號 + 預計停止取號）
- `GET /api/dates` — DB 中所有有資料的日期（給日期下拉）

### Added — 前端
- **日期下拉選單**（header）：可切換歷史日期，跨午夜後新「今日」自動出現
- **卡片新增「預計停止取號」**：依時間早晚著色（紅 ≤18:30 / 橘 ≤19:00 / 棕 普通 / 灰 尚未設定）
- **圖表改步階線** (`stepped: 'before'`)：忠實表達「值維持到下次變化」的離散性質
- **表格改 transition log**：`時間 | 分店 | 5分→10分 | 前值持續 32 分鐘`，比每分鐘重複行有意義 10 倍

### Changed
- 前端主要資料源從 `/api/data` 改為 `/api/changes` + `/api/latest`
- 歷史日期不 auto-refresh（資料不變動就不浪費 CPU）
- 預設只顯示今日資料（DB 仍累積全歷史）

### Performance
- 切換日期、刷新前端速度大幅提升
- 表格 DOM 從 ~10 萬節點降到 ~3-5 千節點
- 用 `DocumentFragment` 批次插入減少 reflow

## v1.2 — 2026-04-21

### Added
- **SQLite 儲存層** — CSV 換成 `wait_log.db`，建立 `wait_log` table 搭配 `idx_timestamp` 與 `idx_store_ts` 兩個索引，大資料量查詢更快
- **自動遷移** — 啟動時偵測舊 CSV，若 DB 空則自動匯入（1021 筆歷史資料保留），舊 CSV 改名為 `.migrated` 備份
- **Mobile RWD** — 三段 media query（≤900px 平板、≤600px 手機、≤380px iPhone SE），卡片、圖表、表格分別縮放；手機隱藏「外帶叫號」次要欄位
- **Footer 標示資料來源** — 採 taiwanstat.com 的中性風格：資料來源連結、非官方聲明、CC BY-NC-SA 4.0 授權、GitHub Issues 聯絡

### Changed
- 資料檔：`all_branches_log.csv` → `wait_log.db`（SQLite）
- `/api/data` 改從 DB 讀取，API schema 不變（向下相容前端）
- `.gitignore` 加入 `server.log`、`server.err.log`、`*.csv.migrated`

### Removed
- `all_branches_log.csv`（資料已全部遷移至 SQLite）

## v1.1 — 2026-04-21

### Removed
- `monitor.py` — 原單分店 CLI 版本，功能已完全被 `app.py` 涵蓋
- `hsinchu_wait_log.csv` — 舊 CLI 版產生的 log，資料已併入 `all_branches_log.csv`

### Changed
- README 精簡，只保留 Web 版說明

## v1.0 — 2026-04-21

首次釋出。

### Features
- **後端**（`app.py`）
  - 每 60 秒 POST 鼎泰豐 `/Queue/Home/WebApiTest`，逐一查詢 12 間分店
  - 自動略過 `wait_time: "無提供內用"` 的分店（目前為信義店）
  - Python 標準庫 `http.server` + `threading`，無第三方相依
  - 用 `subprocess` 呼叫 `curl` 繞過鼎泰豐憑證缺少 Subject Key Identifier 的 SSL 問題
  - 提供 `/api/data`、`/api/stores` JSON 端點
- **前端**（`index.html` + Chart.js 4）
  - 即時狀態卡片：依等候時間著色（紅 ≥40 分 / 橘 ≥15 分 / 綠 <15 分 / 灰 未營業）
  - 多線趨勢圖，每間店固定顏色
  - 滑鼠 hover 線或 Legend → 該線加粗、其他淡化（`legendHover` plugin，用 `lastHoveredIdx` 避免 mousemove 瘋狂重繪）
  - 三種單店篩選方式：點卡片、點 Legend、下拉選單
  - X 軸用 `ticks.callback` 強制 `HH:mm` 格式，不顯示秒
  - 前端每 15 秒自動刷新
  - 安全的 DOM 渲染（`createElement` + `textContent`，避免 XSS）
- **CLI 版**（`monitor.py`）：僅監控新竹店，純終端機輸出，寫入獨立 CSV

### Data
- 包含一份初始示例資料（2026-04-21 上午的新竹店與全分店紀錄）
- CSV 用 `git update-index --skip-worktree` 標記，本地持續累積的資料不會汙染 commit
