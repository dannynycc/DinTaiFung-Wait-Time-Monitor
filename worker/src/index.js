/**
 * 鼎泰豐現場候位 — Cloudflare Worker
 *
 * 兩個角色：
 *   1. scheduled()  cron 每分鐘抓 11 間分店寫進 D1
 *   2. fetch()      提供唯讀 API 給 GitHub Pages 前端
 *
 * 與本機 app.py 的差異，以及為什麼：
 *   - 不再用 curl subprocess。實測 GitHub runner 與 Node undici 都能通過鼎泰豐的
 *     TLS 憑證驗證（SSL_VERIFY=0），README 說的「憑證缺 SKI」其實是 Python ssl
 *     模組建立憑證鏈的限制，不是站方憑證有問題。
 *   - 11 店改成併發抓。序列版實測 12.4 秒，併發約 1.5 秒，避免與下一個 cron 週期重疊。
 *   - 多一張 fetch_health 表。雲端排程失敗是靜默的，沒有這張表就分不出
 *     「這段時間沒資料」是店家沒開還是我們掛了。
 */

const API_URL = 'https://www.dintaifung.tw/Queue/Home/WebApiTest';

// 信義店長期回傳「無提供內用」或 -1，永久排除（沿用 app.py 的判斷）
const STORES = [
  { id: '0003', name: '復興店' },
  { id: '0005', name: '天母店' },
  { id: '0006', name: '新竹店' },
  { id: '0007', name: '101店' },
  { id: '0008', name: '台中店' },
  { id: '0009', name: '板橋店' },
  { id: '0010', name: '高雄店' },
  { id: '0011', name: '南西店' },
  { id: '0012', name: 'A4店' },
  { id: '0013', name: 'A13店' },
  { id: '0015', name: '新生店' },
];

const FETCH_TIMEOUT_MS = 15000;

// raw 保留天數。cutoff = (今天 - N 天) 的 00:00，所以實際保留 N+1 個日曆日：
//   N=1 → 昨天+今天（2 天）    N=2 → 前天+昨天+今天（3 天）
// 用 2 是刻意留安全邊際：roll-up 不可逆，多留一天讓錯誤有機會被發現。
const RAW_RETENTION_DAYS = 2;
const HEALTH_RETENTION_DAYS = 14; // fetch_health 每天 751 筆，不修剪一年就 27 萬筆

// roll-up 觸發窗（台北時間）。cron 視窗是 09:00-21:30，這是每天最早的時機。
// 用「一段窗」而非單一分鐘：Cloudflare cron 是 best-effort，單一分鐘被延遲或
// 丟棄就整天不跑；而該分鐘的 cycle 若全店抓取失敗或寫入失敗也會提前 return/throw。
// roll-up 本身是冪等的（第一次跑完就沒有 < cutoff 的列了，後續都是 no-op），
// 所以多試幾分鐘的成本近乎零。
const ROLLUP_WINDOW = ['09:02', '09:03', '09:04', '09:05', '09:06'];

// ── 時間 ──────────────────────────────────────────
// Worker 一律跑 UTC。資料庫裡的時間戳全部是台北時間，格式 'YYYY-MM-DD HH:MM:SS'，
// 與本機 wait_log.db 完全一致，這樣兩邊的歷史資料才能直接接起來。
// 台灣自 1980 年起就沒有日光節約時間，固定 UTC+8，直接位移是精確的。
function taipeiTimestamp(d = new Date()) {
  return new Date(d.getTime() + 8 * 3600 * 1000)
    .toISOString().slice(0, 19).replace('T', ' ');
}

function taipeiDateString(d = new Date()) {
  return taipeiTimestamp(d).slice(0, 10);
}

function daysAgoTaipei(days) {
  return taipeiTimestamp(new Date(Date.now() - days * 86400 * 1000));
}

// ── 抓取 ──────────────────────────────────────────

async function fetchStore(store, timeoutMs = FETCH_TIMEOUT_MS) {
  const res = await fetch(API_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: `storeid=${encodeURIComponent(store.id)}`,
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  const row = Array.isArray(data) && data.length ? data[0] : null;
  if (!row) throw new Error('empty payload');
  return {
    store_id: store.id,
    store_name: store.name,
    wait_time: String(row.wait_time ?? ''),
    num_1: row.num_1 ?? null,
    num_2: row.num_2 ?? null,
    num_3: row.num_3 ?? null,
    num_4: row.num_4 ?? null,
    togo_numbers: row.togo_numbers ?? '',
    last_time: Number(row.last_time ?? 0) || 0,
  };
}

/**
 * 併發抓全部分店。單店失敗不影響其他店（沿用 app.py 逐店 try/except 的行為）。
 *
 * 失敗的店重試一次：正式站實測 20 個 cycle 有 2 次單店逾時（約 10%），
 * 整天約 9,900 個資料點會漏掉 90 個。重試是 I/O 等待不是運算，
 * 不會侵蝕本來就吃緊的 CPU 額度（實測 7-8ms / 上限 10ms）。
 */
async function fetchRound(stores, timeoutMs) {
  const settled = await Promise.allSettled(stores.map((s) => fetchStore(s, timeoutMs)));
  const ok = [];
  const failed = [];
  settled.forEach((s, i) => {
    if (s.status === 'fulfilled') ok.push(s.value);
    else failed.push({ store: stores[i], reason: s.reason?.message ?? String(s.reason) });
  });
  return { ok, failed };
}

async function fetchAllStores(env = {}) {
  // 逾時可用 env 覆寫，讓本機能故意調到 1ms 逼出失敗、實際走一次重試路徑。
  // 沒有這個開關就只能「等它自己壞」，等於重試邏輯永遠沒被驗證過。
  const timeoutMs = Number(env.FETCH_TIMEOUT_MS) || FETCH_TIMEOUT_MS;

  const first = await fetchRound(STORES, timeoutMs);
  const results = first.ok;
  let failures = first.failed;
  let retried = 0;

  if (failures.length) {
    console.log(`retry round: ${failures.map((f) => f.store.id).join(',')}`);
    const second = await fetchRound(failures.map((f) => f.store), timeoutMs);
    results.push(...second.ok);
    retried = second.ok.length;
    failures = second.failed;
    console.log(`retry result: ${retried} recovered, ${failures.length} still failing`);
  }

  return {
    results: results.sort((a, b) => a.store_id.localeCompare(b.store_id)),
    failures: failures.map((f) => `${f.store.id}:${f.reason}`),
    retried,
  };
}

// ── 寫入 ──────────────────────────────────────────

// D1 對單一 query 的綁定參數有 100 個上限（本機 SQLite 是 32766，所以 app.py
// 的 executemany 直接搬過來會炸 "too many SQL variables"）。
// 11 店 × 10 欄 = 110 個參數就已超標，因此一律切塊。
const D1_MAX_BOUND_PARAMS = 100;

/**
 * 產生 multi-row INSERT，必要時切成多個 statement。
 * 只有佔位符數量是動態的，值一律綁定參數，不做字串拼接。
 */
// 用 INSERT OR IGNORE 搭配 (timestamp, store_id) 唯一索引：
// 若兩個 cron invocation 在同一分鐘重疊，第二次的重複列被安靜忽略，
// 而不是讓整個 batch 因唯一鍵衝突而 rollback（那會連當次資料都寫不進去）。
function buildInserts(table, columns, rows) {
  const perRow = columns.length;
  const rowsPerStatement = Math.max(1, Math.floor(D1_MAX_BOUND_PARAMS / perRow));
  const tuple = `(${columns.map(() => '?').join(',')})`;
  const out = [];
  for (let i = 0; i < rows.length; i += rowsPerStatement) {
    const chunk = rows.slice(i, i + rowsPerStatement);
    out.push({
      sql: `INSERT OR IGNORE INTO ${table} (${columns.join(',')}) VALUES ${chunk.map(() => tuple).join(',')}`,
      binds: chunk.flatMap((r) => columns.map((c) => r[c])),
    });
  }
  return out;
}

async function runCycle(env) {
  const started = Date.now();
  const ts = taipeiTimestamp();
  const { results, failures, retried } = await fetchAllStores(env);

  if (results.length === 0) {
    await writeHealth(env, ts, 0, failures.length, Date.now() - started,
      `ALL_FAIL: ${failures.join('; ')}`.slice(0, 500));
    // 全店失敗要 throw。原本只寫 health 就 return，等於一次完整斷線在
    // wrangler tail / Observability 裡完全看不到 —— 與本檔「用 await 讓失敗浮上來」
    // 的設計原則矛盾。health 已先寫入，throw 不會讓這筆紀錄消失。
    throw new Error(`all ${failures.length} stores failed`);
  }

  const statements = [];

  // 1) raw snapshot
  const rawCols = ['timestamp', 'store_id', 'store_name', 'wait_time',
    'num_1', 'num_2', 'num_3', 'num_4', 'togo_numbers', 'last_time'];
  const rawRows = results.map((r) => ({ ...r, timestamp: ts }));
  for (const s of buildInserts('wait_log', rawCols, rawRows)) {
    statements.push(env.DB.prepare(s.sql).bind(...s.binds));
  }

  // 2) 比對每店最後一筆變化事件，值不同才記一筆
  //    一次查回全部分店的最後狀態（1 個 query），避免逐店查（11 個 query）。
  //    D1 免費方案每次 Worker 呼叫上限 50 個 query，這樣整個週期只用掉 4 個。
  const lastRows = await env.DB.prepare(`
    SELECT store_id, wait_time, MAX(timestamp) AS timestamp
    FROM wait_changes
    GROUP BY store_id
  `).all();
  const lastByStore = new Map((lastRows.results ?? []).map((r) => [r.store_id, r]));

  const changes = [];
  for (const r of results) {
    const last = lastByStore.get(r.store_id);
    if (!last) {
      changes.push({ timestamp: ts, store_id: r.store_id, store_name: r.store_name,
        wait_time: r.wait_time, prev_value: null, duration_min: null });
    } else if (last.wait_time !== r.wait_time) {
      const t1 = Date.parse(last.timestamp.replace(' ', 'T') + 'Z');
      const t2 = Date.parse(ts.replace(' ', 'T') + 'Z');
      changes.push({ timestamp: ts, store_id: r.store_id, store_name: r.store_name,
        wait_time: r.wait_time, prev_value: last.wait_time,
        duration_min: Math.floor((t2 - t1) / 60000) });
    }
  }
  if (changes.length) {
    const chCols = ['timestamp', 'store_id', 'store_name', 'wait_time', 'prev_value', 'duration_min'];
    for (const s of buildInserts('wait_changes', chCols, changes)) {
      statements.push(env.DB.prepare(s.sql).bind(...s.binds));
    }
  }

  // 3) 寫入。健康紀錄刻意「不」放進同一個 batch：batch 是單一交易，
  //    寫入一失敗就整包 rollback，連「這次失敗了」這件事都留不下來。
  //    第一次本機測試就踩到這個 —— API 全回 200、資料全空、health 也空。
  let writeError = null;
  try {
    await env.DB.batch(statements);
  } catch (e) {
    writeError = e?.message ?? String(e);
  }

  if (writeError) {
    await writeHealth(env, ts, 0, failures.length, Date.now() - started,
      `WRITE_FAIL: ${writeError}`);
    throw new Error(`D1 write failed: ${writeError}`);
  }

  // 4) roll-up。放在每分鐘的路徑上會白白吃掉 CPU 額度（cron 的 CPU 實測 7-8ms、
  //    Free 上限 10ms），所以只在觸發窗內跑。時刻可用 env 覆寫，讓本機測試能走
  //    同一條 production 程式碼路徑，不必改常數再改回來（改回來忘了就是事故）。
  //    刻意放在 health 寫入「之前」：否則 roll-up 的結果無處可記，
  //    只會留在 wrangler tail 這種即逝的地方。
  const window = env.ROLLUP_AT ? [env.ROLLUP_AT] : ROLLUP_WINDOW;
  let rollupNote = null;
  if (window.includes(ts.slice(11, 16))) {
    try {
      const n = await rollupRawLog(env);
      rollupNote = n ? `ROLLUP_OK: ${n} raw rows` : 'ROLLUP_NOOP';
    } catch (e) {
      rollupNote = `ROLLUP_FAIL: ${e?.message ?? e}`;
    }
  }

  // 分類寫進 note，讓 CI 的健康檢查能分辨「已自我修復」與「真的有問題」。
  // RETRY_OK 是成功自我修復的訊號，不該被當成告警來源。
  const note = [
    failures.length ? `FETCH_FAIL: ${failures.join('; ')}` : null,
    retried ? `RETRY_OK: ${retried}` : null,
    rollupNote,
  ].filter(Boolean).join(' | ').slice(0, 500) || null;

  await writeHealth(env, ts, results.length, failures.length, Date.now() - started, note);

  if (rollupNote && rollupNote.startsWith('ROLLUP_FAIL')) {
    throw new Error(rollupNote);   // health 已寫入，throw 讓 Observability 也看得到
  }

  return { ts, ok: results.length, fail: failures.length, changes: changes.length, rollupNote };
}

function writeHealth(env, ts, ok, fail, elapsed, note) {
  return env.DB.prepare(
    'INSERT INTO fetch_health (timestamp, ok_count, fail_count, elapsed_ms, note) VALUES (?,?,?,?,?)'
  ).bind(ts, ok, fail, elapsed, note).run();
}

/**
 * 每日 roll-up：把過期的 raw 轉成「只留變化的地方」，然後刪掉 raw。
 *
 * 為什麼不是單純硬刪，也不是整列事件化：
 *   實測 25 天真實資料，各欄位每店每天的變動次數差了 180 倍
 *     num_1 217.5 / num_2 148.7 / togo 121.9 / num_3 65.6 / wait_time 53.3 / num_4 37.0
 *     last_time 只有 1.2
 *   「任一欄位變了才記一筆」被 num_1 這種計數器綁架，只壓 2.57x（257,049 → 100,034）。
 *   分開處理才有效：wait_time 與 last_time 留完整事件流（無損，可還原每分鐘的值），
 *   num_1~4 與 togo 滾成每日彙總。實測 881 KB/天 → 約 65 KB/天。
 *
 * 三個 statement 放同一個 batch = 單一交易。推導失敗就不會刪到 raw。
 */
async function rollupRawLog(env) {
  const cutoff = daysAgoTaipei(RAW_RETENTION_DAYS).slice(0, 10) + ' 00:00:00';

  // 1) 止號時間事件流。
  //    跨日邊界是這裡最容易寫錯的地方：每天只看得到當天的 raw，LAG() 在該店第一筆
  //    會回 NULL，天真的寫法會誤判成「首次出現」而每天每店多產生一筆假事件。
  //    因此 LAG 為 NULL 時要從既有 stop_changes 撈該店最後一個值接回來。
  //    此 SQL 已用 25 天真實資料逐日模擬驗證，333 筆與獨立推導的 ground truth 逐筆一致。
  const stopSql = `
    INSERT INTO stop_changes (timestamp, store_id, store_name, last_time, prev_value)
    SELECT timestamp, store_id, store_name, last_time, pv FROM (
      SELECT w.timestamp, w.store_id, w.store_name, w.last_time,
             COALESCE(
               LAG(w.last_time) OVER (PARTITION BY w.store_id ORDER BY w.timestamp),
               (SELECT s.last_time FROM stop_changes s
                 WHERE s.store_id = w.store_id
                 ORDER BY s.timestamp DESC LIMIT 1)
             ) AS pv
      FROM wait_log w
      WHERE w.timestamp < ?
    )
    WHERE pv IS NULL OR last_time IS NOT pv
    ORDER BY timestamp, store_id`;

  // 2) 每日彙總。max-min 的差值就是當日該桌型叫了幾組。
  //    用 MIN/MAX 而非時序首末是刻意的：實測 11 間店有 2 間（天母店、A4店）
  //    的叫號會在當日營業結束後重置回 1000，例如天母店
  //      MIN=1000 MAX=1378 時序首=1277 時序末=1000
  //    照字面取時序首末，「叫號組數」會算成 1000-1277 = 負 277。
  const summarySql = `
    INSERT OR REPLACE INTO daily_summary
      (date, store_id, store_name, max_wait,
       min_num_1, max_num_1, min_num_2, max_num_2,
       min_num_3, max_num_3, min_num_4, max_num_4,
       togo_states, first_ts, last_ts, raw_rows)
    SELECT substr(timestamp,1,10), store_id, store_name,
           MAX(CAST(wait_time AS INTEGER)),
           MIN(CAST(num_1 AS INTEGER)), MAX(CAST(num_1 AS INTEGER)),
           MIN(CAST(num_2 AS INTEGER)), MAX(CAST(num_2 AS INTEGER)),
           MIN(CAST(num_3 AS INTEGER)), MAX(CAST(num_3 AS INTEGER)),
           MIN(CAST(num_4 AS INTEGER)), MAX(CAST(num_4 AS INTEGER)),
           COUNT(DISTINCT togo_numbers), MIN(timestamp), MAX(timestamp), COUNT(*)
    FROM wait_log WHERE timestamp < ?
    GROUP BY substr(timestamp,1,10), store_id`;

  // fetch_health 也要修剪。它每個 cron 週期寫一筆 = 每天 751 筆、一年 27 萬筆，
  // 而 roll-up 原本只處理 wait_log，等於漏掉一張會無限成長的表。
  // 保留 14 天：足以回溯查「上週某天為什麼有空洞」，又不會累積。
  const healthCutoff = daysAgoTaipei(HEALTH_RETENTION_DAYS);

  // 先數有多少列要處理，回傳給呼叫端寫進 fetch_health。
  // 沒有這個數字就無法事後回答「那天 roll-up 到底跑了沒、處理了什麼」——
  // console.log 只存在於 wrangler tail，關掉就沒了。
  const pending = await env.DB.prepare(
    'SELECT COUNT(*) AS n FROM wait_log WHERE timestamp < ?'
  ).bind(cutoff).first();
  const n = pending?.n ?? 0;

  await env.DB.batch([
    env.DB.prepare(stopSql).bind(cutoff),
    env.DB.prepare(summarySql).bind(cutoff),
    env.DB.prepare('DELETE FROM wait_log WHERE timestamp < ?').bind(cutoff),
    env.DB.prepare('DELETE FROM fetch_health WHERE timestamp < ?').bind(healthCutoff),
  ]);

  console.log(`rollup done, cutoff=${cutoff}, raw rows processed=${n}`);
  return n;
}

// ── 唯讀 API ──────────────────────────────────────

const JSON_HEADERS = {
  'Content-Type': 'application/json; charset=utf-8',
  'Access-Control-Allow-Origin': '*',
  // 保險：瀏覽器端 60 秒內不重打。前端 poll 也對齊成 60 秒（資料本來就 60 秒才變）。
  'Cache-Control': 'public, max-age=60',
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: JSON_HEADERS });
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

async function handleApi(url, env) {
  const path = url.pathname;

  if (path === '/api/stores') {
    return json(STORES.map((s) => ({ store_id: s.id, store_name: s.name })));
  }

  if (path === '/api/latest') {
    // SQLite 特性：使用 MAX() 聚合時，同一列的其他裸欄位保證取自 MAX 命中的那一列。
    // 因此這一句就能取得每店最新快照，不需要相關子查詢。
    //
    // 視窗錨定在「資料最後一筆」而不是「現在」。
    // 原本寫成「現在往前 6 小時」，但 cron 只跑台北 09:00-21:30，
    // 每天午夜過後最後一筆資料就超過 6 小時 → 回空陣列 → 整頁卡片消失，
    // 每天 00:00-09:06 共 9 小時都是空白。錨定在資料上就不受抓取視窗影響。
    const { results } = await env.DB.prepare(`
      SELECT store_id, store_name, wait_time, num_1, num_2, num_3, num_4,
             togo_numbers, last_time, MAX(timestamp) AS timestamp
      FROM wait_log
      WHERE timestamp >= (SELECT datetime(MAX(timestamp), '-6 hours') FROM wait_log)
      GROUP BY store_id ORDER BY store_id
    `).all();
    return json(results ?? []);
  }

  if (path === '/api/changes') {
    const date = url.searchParams.get('date') || taipeiDateString();
    if (!DATE_RE.test(date)) return json({ error: 'bad date' }, 400);
    // 上界用「隔日 00:00:00」而非「當日 23:59:59」：後者會漏掉正好落在
    // 23:59:59 的事件。本機 app.py 用的就是隔日 00:00:00，這是搬移時改壞的。
    const next = new Date(Date.parse(`${date}T00:00:00Z`) + 86400000)
      .toISOString().slice(0, 10);
    const { results } = await env.DB.prepare(`
      SELECT timestamp, store_id, store_name, wait_time, prev_value, duration_min
      FROM wait_changes
      WHERE timestamp >= ? AND timestamp < ?
      ORDER BY timestamp ASC, store_id ASC
    `).bind(`${date} 00:00:00`, `${next} 00:00:00`).all();
    return json(results ?? []);
  }

  if (path === '/api/dates') {
    const { results } = await env.DB.prepare(`
      SELECT DISTINCT substr(timestamp,1,10) AS d FROM wait_changes ORDER BY d DESC
    `).all();
    return json((results ?? []).map((r) => r.d));
  }

  if (path === '/api/summary') {
    // 每日彙總（roll-up 產物）。不帶 date 就回全部，量很小：每天 11 筆。
    const date = url.searchParams.get('date');
    if (date && !DATE_RE.test(date)) return json({ error: 'bad date' }, 400);
    const stmt = date
      ? env.DB.prepare('SELECT * FROM daily_summary WHERE date = ? ORDER BY store_id').bind(date)
      : env.DB.prepare('SELECT * FROM daily_summary ORDER BY date DESC, store_id LIMIT 500');
    const { results } = await stmt.all();
    return json(results ?? []);
  }

  if (path === '/api/stops') {
    // 止號時間事件流。每店每天只變約 1.2 次，全部回傳也很輕。
    const { results } = await env.DB.prepare(`
      SELECT timestamp, store_id, store_name, last_time, prev_value
      FROM stop_changes ORDER BY timestamp DESC LIMIT 1000
    `).all();
    return json(results ?? []);
  }

  if (path === '/api/health') {
    const { results } = await env.DB.prepare(`
      SELECT timestamp, ok_count, fail_count, elapsed_ms, note
      FROM fetch_health ORDER BY timestamp DESC LIMIT 20
    `).all();
    return json(results ?? []);
  }

  return json({ error: 'not found' }, 404);
}

export default {
  // 用 await 而非 ctx.waitUntil：waitUntil 會把例外吞進背景，
  // 排程失敗就完全無聲。await 才會讓失敗出現在 wrangler tail 與 Observability。
  async scheduled(_event, env, _ctx) {
    const r = await runCycle(env);
    console.log(`cycle ${r.ts} ok=${r.ok} fail=${r.fail} changes=${r.changes}`);
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, OPTIONS',
      } });
    }
    if (request.method !== 'GET') return json({ error: 'method not allowed' }, 405);
    if (!url.pathname.startsWith('/api/')) {
      return json({ error: 'see /api/latest, /api/changes, /api/dates, /api/stores, /api/health' }, 404);
    }

    // 保險：邊緣快取。自訂網域下同時段多位訪客共用一次 Worker 呼叫。
    // workers.dev 子網域上 Cloudflare 文件說法不一致，所以上面的 Cache-Control
    // 才是實際保證的那一層，這裡純屬加分。
    const cache = caches.default;
    const hit = await cache.match(request);
    if (hit) return hit;

    const res = await handleApi(url, env);
    if (res.status === 200) ctx.waitUntil(cache.put(request, res.clone()));
    return res;
  },
};
