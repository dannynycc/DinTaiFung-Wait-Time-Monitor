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
