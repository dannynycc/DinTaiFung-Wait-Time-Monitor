# Changelog

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
