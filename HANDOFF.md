# HANDOFF — 開新 session 先讀這份

> 最後更新：2026-09-11 09:35 +08:00
> 完整脈絡在 `CHANGELOG.md` 的 v4.15 ～ v4.19。

---

## 待辦

目前沒有。（「驗證 D1 用量」已於 2026-09-11 完成，結果在 CHANGELOG v4.19：一天 110,762 列，額度的 2.2%。）

---

## 這個專案現在的樣子（2026-09-02 之後）

### 前端只有一份

`docs/index.html` 同時是線上站與本機版的前端。本機版由 `app.py` 的
`FRONTEND_PATCHES` 在提供時替換四個常數（API 位址、歷史檔路徑、24 小時軸、標題）。

**改 `docs/index.html` 的那四行時要同步更新 `app.py` 的 `FRONTEND_PATCHES`**，
否則本機版會回一頁「本機前端組不起來」並指出是哪一條沒命中
（刻意做成明顯失敗，不是靜默降級）。

root 的 `index.html` 已刪除（v4.17）——它曾是手工複本，分歧了 925 行。

### 動 D1 查詢前必做

```bash
npx wrangler d1 execute dintaifung --remote --json --command "<SQL>" | grep rows_read
```

兩個會讓索引失效的形態：對索引欄位做函式運算（`substr(timestamp,1,10)`）、
`GROUP BY` + 聚合（SQLite 沒有 index skip-scan）。
另外 D1 的 compound SELECT term 上限實測是 5（7 就回 `SQLITE_ERROR 7500`），
`UNION ALL` 要分批送 `batch()`。

### 本機版

- 目前**沒有在收集資料**（`wait_log.db` 最後寫入 2026-05-18），但隨時可啟動
- 只綁 `127.0.0.1`，白名單（`/`、`/index.html`、`/api/*`）以外一律 404
- 要讓區網看：`DTF_HOST=0.0.0.0 python app.py`
- `wait_log.db` 有 skip-worktree 旗標，**該旗標不隨 clone 傳播** ——
  新機器上要自己再設一次，否則本機版一跑，`git add -A` 就會把 39 MB 推上 public repo：
  ```bash
  git update-index --skip-worktree wait_log.db
  git ls-files -v wait_log.db     # 開頭是 S 才代表生效
  ```

### 監控

`daily-export` workflow 失敗時會開 GitHub Issue（v4.15.1）。
已在真實失敗中驗證（2026-09-03 開了 Issue #1，之後每天留言）。

🚨 **健康檢查的基準是「目標日 21:30 收盤」，不是「現在」**（v4.19）。
GitHub 的 `schedule` 常晚 2～3 小時起跑，任何拿「現在」算新鮮度的門檻都會天天假紅。
抓取視窗若改（`worker/wrangler.toml` 的 `crons`），要同步改 workflow 裡的 `WINDOW_CLOSE`。

---

## 已知但沒有處理的

| 項目 | 說明 |
|---|---|
| `-1` 顯示成「尚未營業」 | 深夜其實是「已打烊」。官方 API 只給 `-1`，無法區分兩者。要改得動共用的 `docs/index.html`，使用者當時剛叮嚀過不要動網頁端，所以擱置。 |
| `/api/dates` 讀 `daily_summary` 的 `DISTINCT date` | 隨天數線性成長（2026-09-02 是 209 列，一年後約 4,015 列）。以每次 216 rows_read 計仍是額度的 0.004%，暫時不動。 |
| `MEMORY.md` 179 行 | 逼近 200 行讀取上限，該壓縮。會動到其他專案的長期記憶，需要使用者點頭。 |
| 沒有自動化測試 | 這個專案沒有測試套件，所有驗證都是人工＋實測。改動後要自己跑一次真瀏覽器。 |

---

## 這次事故的教訓（別再犯）

1. **回傳幾列 ≠ 讀了幾列。** 一條只回 11 列的 `GROUP BY` 掃了 14,275 列，
   每分鐘跑一次就是每天 1,072 萬，額度的 2.1 倍。這種 bug 有潛伏期 ——
   上線時表小、一切正常，兩週後才炸。
2. **偵測得到但沒人知道，等於沒偵測。** `daily-export` 從 8/26 起每天都正確判定
   失敗，但只是躺在 Actions 頁面，拖了一週才由 Cloudflare 的告警信揭穿。
3. **沒查證就不要寫因果。** 我看到 `daily_summary` 只到 08-30 就寫了
   「roll-up 連四天沒跑」，查 `fetch_health` 才發現每天都 `ROLLUP_OK` ——
   額度在台北 08:00 重置，roll-up 09:02 跑時額度充足，超限是當天下午的事。
   已在 v4.18 訂正。
4. **守門的尺要釘在自己控制得了的量上。** v4.16 的健康檢查拿「現在」當基準，
   而「現在」由 GitHub 排程決定（實測每天晚 2～3 小時），於是 9 天假紅、9 天資料沒進 repo。
   手動觸發測試全綠是因為中午跑 cron 正在抓 —— 測方便的情境不等於測過排程情境。已在 v4.19 修正。
