# Changelog

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
