# 鼎泰豐現場候位監控 · DinTaiFung Wait Time Monitor

即時監控鼎泰豐台灣全分店的現場候位時間，提供終端機 CLI 版與網頁版。

![version](https://img.shields.io/badge/version-v1.2-brown) ![python](https://img.shields.io/badge/python-3.8%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

## 功能

- 每 60 秒自動查詢鼎泰豐 [官方候位 API](https://www.dintaifung.tw/Queue/?type=3)
- 自動略過「無提供內用」的分店（目前是信義店）
- 資料以 SQLite 儲存（`wait_log.db`），長期累積效能不衰減
- 手機、平板、桌機 RWD 支援
- 即時狀態卡片：每間店一張，依等候時間分紅/橘/綠/灰
- 趨勢圖（Chart.js）：每間店一條線、時間為 X 軸
- 滑鼠 hover 線或 legend → 該條線加粗、其他淡化
- 點卡片、Legend、或下拉選單 → 單店模式
- 歷史紀錄表格，可依分店篩選
- 前端每 15 秒自動刷新（免手動重整）

## 快速啟動

```bash
python app.py
# 開 http://localhost:5678
```

## 相依

- Python 3.8+（只用標準庫，**無需** `pip install`）
- `curl`（Windows 10+ / macOS / Linux 皆內建）

用 curl 而非 `requests` 是因為鼎泰豐伺服器憑證缺少 Subject Key Identifier，Python 的 SSL 驗證會拒絕，curl 預設較寬容。

## 檔案結構

```
.
├── app.py                    # 後端 + Web server（主程式）
├── index.html                # 前端頁面（Chart.js + vanilla JS）
├── wait_log.db               # SQLite 資料庫（自動產生，首次 clone 附示例資料）
├── README.md
└── CHANGELOG.md
```

## API

`app.py` 啟動後會在 `:5678` 提供：

| Endpoint | 說明 |
|---|---|
| `GET /` | 網頁前端 |
| `GET /api/data` | 回傳所有歷史紀錄（JSON 陣列） |
| `GET /api/stores` | 回傳分店清單 |

## 分店對照

| storeId | 分店 |
|---|---|
| 0001 | 信義店（無提供內用，自動略過） |
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
INTERVAL = 60   # 查詢頻率（秒）
PORT = 5678     # Web server 埠號
```

要改監控的分店清單，編輯 `STORES` 列表。

## 授權

MIT

## 免責聲明

本專案僅使用鼎泰豐官網公開資料，供個人參考。如有商業用途請先取得鼎泰豐授權。
