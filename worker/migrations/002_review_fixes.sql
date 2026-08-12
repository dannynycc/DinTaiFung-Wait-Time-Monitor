-- 三方 code review 後的修正遷移（v3.3）
-- 執行：npx wrangler d1 execute dintaifung --remote --file=migrations/002_review_fixes.sql

-- ── 1. 防止 cron 重疊產生重複列 ────────────────────
-- Cloudflare cron 各分鐘的觸發是獨立 invocation，理論上可並行。
-- 實測 190 個 cycle 沒撞到（重複 (timestamp,store_id) = 0 筆），但那只證明
-- 沒發生過、不證明不會發生。加唯一索引讓它在結構上不可能。
-- 搭配 INSERT OR IGNORE：重複的同分鐘寫入被安靜忽略，而不是讓整個 batch 失敗。
CREATE UNIQUE INDEX IF NOT EXISTS ux_wait_log_ts_store
    ON wait_log(timestamp, store_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_wait_changes_ts_store
    ON wait_changes(timestamp, store_id);

-- ── 2. daily_summary 欄位改名 ─────────────────────
-- 原欄位名 first_num_* / last_num_* 與實際計算（MIN/MAX）不符。
-- 實測 11 間店有 2 間（天母店、A4店）的叫號會在當日營業結束後重置回 1000，
-- 所以「時序上的最後一個值」是 1000，而非當日最大值：
--     天母店 MIN=1000 MAX=1378 時序首=1277 時序末=1000
-- 這代表 MIN/MAX 才是正確算法 —— 若照欄位名的字面語意取時序首末，
-- 「當日叫號組數」會算成 1000-1277 = 負 277。錯的是名稱不是計算。
-- 此表目前為空（roll-up 尚未首次執行），直接重建無資料成本。
DROP TABLE IF EXISTS daily_summary;

CREATE TABLE daily_summary (
    date        TEXT    NOT NULL,
    store_id    TEXT    NOT NULL,
    store_name  TEXT    NOT NULL,
    max_wait    INTEGER,           -- 當日最長候位（分鐘）；全日未營業時為 0
    min_num_1   INTEGER, max_num_1 INTEGER,   -- max-min = 當日該桌型叫號組數
    min_num_2   INTEGER, max_num_2 INTEGER,   -- （叫號會在收店後重置，故用 MIN/MAX
    min_num_3   INTEGER, max_num_3 INTEGER,   --   而非時序首末，見上方說明）
    min_num_4   INTEGER, max_num_4 INTEGER,
    togo_states INTEGER,           -- 當日外帶叫號的相異狀態數
    first_ts    TEXT,
    last_ts     TEXT,
    raw_rows    INTEGER,           -- 滾動前原本幾筆 raw（稽核用）
    PRIMARY KEY (date, store_id)
);
