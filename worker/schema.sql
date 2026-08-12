-- D1 schema，與本機 wait_log.db 完全同構（D1 底層就是 SQLite）
-- 建立：npx wrangler d1 execute dintaifung --remote --file=worker/schema.sql

CREATE TABLE IF NOT EXISTS wait_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT    NOT NULL,   -- 'YYYY-MM-DD HH:MM:SS'，一律台北時間
    store_id     TEXT    NOT NULL,
    store_name   TEXT    NOT NULL,
    wait_time    TEXT    NOT NULL,
    num_1        TEXT,
    num_2        TEXT,
    num_3        TEXT,
    num_4        TEXT,
    togo_numbers TEXT,
    last_time    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_timestamp ON wait_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_store_ts  ON wait_log(store_id, timestamp);

CREATE TABLE IF NOT EXISTS wait_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT    NOT NULL,
    store_id     TEXT    NOT NULL,
    store_name   TEXT    NOT NULL,
    wait_time    TEXT    NOT NULL,
    prev_value   TEXT,               -- NULL = 該店首筆
    duration_min INTEGER             -- 前一個值持續幾分鐘
);
CREATE INDEX IF NOT EXISTS idx_changes_ts       ON wait_changes(timestamp);
CREATE INDEX IF NOT EXISTS idx_changes_store_ts ON wait_changes(store_id, timestamp);

-- ── 每日 roll-up 產物 ────────────────────────────
-- raw（wait_log）每天約 9,900 筆、881 KB，是空間主因。但實測顯示各欄位變動頻率差 180 倍：
--   num_1  每店每天變 217.5 次 ← 叫號計數器，貴
--   togo   每店每天變 121.9 次
--   wait_time 53.3 次          ← 已由 wait_changes 承接
--   last_time 每店每天只變 1.2 次 ← 「熱度硬指標」，幾乎免費
-- 所以不做「整列事件化」（只壓 2.57x），改成每欄各自處理：
-- 便宜的留完整事件流，昂貴的滾成每日彙總後刪除 raw。

-- 止號時間事件流。全期 25 天 11 店總共只有 333 筆。
CREATE TABLE IF NOT EXISTS stop_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    store_id    TEXT    NOT NULL,
    store_name  TEXT    NOT NULL,
    last_time   INTEGER,           -- unix 秒；0 = 尚未設定
    prev_value  INTEGER            -- NULL = 該店首筆
);
CREATE INDEX IF NOT EXISTS idx_stop_store_ts ON stop_changes(store_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_stop_ts       ON stop_changes(timestamp);

-- 每日彙總。num_1~4 與 togo 變動太頻繁不值得留事件流，
-- 但「當日叫到第幾號」「當日叫了幾組」這兩個衍生指標值得永久保存。
CREATE TABLE IF NOT EXISTS daily_summary (
    date        TEXT    NOT NULL,
    store_id    TEXT    NOT NULL,
    store_name  TEXT    NOT NULL,
    max_wait    INTEGER,           -- 當日最長候位（分鐘）
    first_num_1 INTEGER, last_num_1 INTEGER,   -- 差值 = 當日該桌型叫號組數
    first_num_2 INTEGER, last_num_2 INTEGER,
    first_num_3 INTEGER, last_num_3 INTEGER,
    first_num_4 INTEGER, last_num_4 INTEGER,
    togo_states INTEGER,           -- 當日外帶叫號的相異狀態數
    first_ts    TEXT,
    last_ts     TEXT,
    raw_rows    INTEGER,           -- 滾動前原本幾筆 raw（稽核用）
    PRIMARY KEY (date, store_id)
);

-- 抓取健康紀錄：每個 cron 週期一筆，用來事後查「某段時間沒資料是店家沒開還是我們掛了」
-- 本機版沒有這張表，是上雲後新增的：雲端排程失敗是靜默的，沒有這張表就分不出兩者。
CREATE TABLE IF NOT EXISTS fetch_health (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    ok_count    INTEGER NOT NULL,
    fail_count  INTEGER NOT NULL,
    elapsed_ms  INTEGER NOT NULL,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_ts ON fetch_health(timestamp);
