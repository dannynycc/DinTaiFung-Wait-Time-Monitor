# HANDOFF — 開新 session 先讀這份

> 最後更新：2026-09-02 14:37 +08:00（session 結束時寫）
> 完整脈絡在 `CHANGELOG.md` 的 v4.15 ～ v4.18。

---

## 待辦（唯一一項，2026-09-03 之後執行）

### 驗證 D1 用量已回到正常

```bash
cd worker
npx wrangler d1 insights dintaifung --timePeriod 1d --sort-by reads --count 25
```

**為什麼要跑這個**：2026-09-02 修掉三條全表掃描的查詢（v4.15）之後，我只能給出
「每日約 76,000 rows_read」的推算 —— `insights` 的最小時間視窗是 1 天，
當天跑會橫跨修復前後，數字混在一起。**9/3 之後跑，那 24 小時全是新版，
才是乾淨的實證。**

#### 判準：符合以下全部才算通過

| 檢查 | 期望 |
|---|---|
| `SELECT store_id, wait_time, MAX(timestamp) ... GROUP BY store_id` | **完全不出現**（舊查詢已刪除）|
| `SELECT DISTINCT substr(timestamp,1,10) ... FROM wait_changes` | **完全不出現**（舊 `/api/dates`）|
| 所有查詢的 `totalRowsRead` 加總 | **< 200,000**（額度 5,000,000 的 4% 以內）|
| roll-up 的 `INSERT INTO stop_changes ...` | 約 70,000／次，一天 1 次 |
| cron 的 `UNION ALL` 逐店查詢 | 每次 11 列；讀取量太小的話可能根本不進清單 |

**若總量明顯超過 20 萬**：不要猜原因，用同樣的方法定位 ——
挑出 `avgRowsRead` 最大的那條，拿它去 `wrangler d1 execute --json` 單獨跑一次，
看 `meta.rows_read`。D1 的額度算的是**讀了幾列**不是回了幾列，
一條只回 11 列的查詢可能掃了整張表（這就是這次事故的根因）。

#### 解析輸出的兩個坑

- `insights` 的 stdout 前面有 wrangler 的橫線與警告，`s.index('[')` 會切錯位置。
  用 `s.index('[\n')` 找真正的 JSON 陣列起點。
- Windows 的 python 預設 cp950，印中文或 `✓` 會炸。前面加 `PYTHONIOENCODING=utf-8`。

一行搞定：

```bash
cd worker && npx wrangler d1 insights dintaifung --timePeriod 1d --sort-by reads --count 25 > /tmp/ins.json 2>&1
PYTHONIOENCODING=utf-8 python -c "
import json
s = open('/tmp/ins.json', encoding='utf-8', errors='replace').read()
d = json.loads(s[s.index('[\n'):])
print('總計 rows_read =', f\"{sum(q['totalRowsRead'] for q in d):,}\", ' / 額度 5,000,000')
for q in sorted(d, key=lambda x: -x['totalRowsRead']):
    print(f\"  {' '.join(q['query'].split())[:60]:62} {q['numberOfTimesRun']:>5} 次 x {q['avgRowsRead']:>7,} = {q['totalRowsRead']:>10,}\")
"
```

跑完把結果補進 `CHANGELOG.md`（新開一節或補在 v4.18 底下都可以），
然後**把這一節從 HANDOFF 刪掉** —— 待辦完成就不該繼續佔著版面。

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
這條路徑**還沒有在真實失敗中驗證過** —— 它只在 `if: failure()` 時執行，
目前只確認了 YAML 解析、shell 語法與 jq 表達式無誤。下次真的失敗時留意它有沒有動作。

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
