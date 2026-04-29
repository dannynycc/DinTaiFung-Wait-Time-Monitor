# Changelog

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
