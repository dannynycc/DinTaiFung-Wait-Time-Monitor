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
const RAW_RETENTION_DAYS = 30;   // 見 pruneRawLog()

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

async function fetchStore(store) {
  const res = await fetch(API_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: `storeid=${encodeURIComponent(store.id)}`,
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
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

/** 併發抓全部分店。單店失敗不影響其他店（沿用 app.py 逐店 try/except 的行為）。 */
async function fetchAllStores() {
  const settled = await Promise.allSettled(STORES.map(fetchStore));
  const results = [];
  const failures = [];
  settled.forEach((s, i) => {
    if (s.status === 'fulfilled') results.push(s.value);
    else failures.push(`${STORES[i].id}:${s.reason?.message ?? s.reason}`);
  });
  return { results, failures };
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
function buildInserts(table, columns, rows) {
  const perRow = columns.length;
  const rowsPerStatement = Math.max(1, Math.floor(D1_MAX_BOUND_PARAMS / perRow));
  const tuple = `(${columns.map(() => '?').join(',')})`;
  const out = [];
  for (let i = 0; i < rows.length; i += rowsPerStatement) {
    const chunk = rows.slice(i, i + rowsPerStatement);
    out.push({
      sql: `INSERT INTO ${table} (${columns.join(',')}) VALUES ${chunk.map(() => tuple).join(',')}`,
      binds: chunk.flatMap((r) => columns.map((c) => r[c])),
    });
  }
  return out;
}

async function runCycle(env) {
  const started = Date.now();
  const ts = taipeiTimestamp();
  const { results, failures } = await fetchAllStores();

  if (results.length === 0) {
    await env.DB.prepare(
      'INSERT INTO fetch_health (timestamp, ok_count, fail_count, elapsed_ms, note) VALUES (?,?,?,?,?)'
    ).bind(ts, 0, failures.length, Date.now() - started, failures.join('; ').slice(0, 500)).run();
    return { ts, ok: 0, fail: failures.length, changes: 0 };
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

  const note = [
    writeError ? `WRITE_FAIL: ${writeError}` : null,
    failures.length ? `FETCH_FAIL: ${failures.join('; ')}` : null,
  ].filter(Boolean).join(' | ').slice(0, 500) || null;

  await env.DB.prepare(
    'INSERT INTO fetch_health (timestamp, ok_count, fail_count, elapsed_ms, note) VALUES (?,?,?,?,?)'
  ).bind(ts, writeError ? 0 : results.length, failures.length, Date.now() - started, note).run();

  if (writeError) throw new Error(`D1 write failed: ${writeError}`);

  // 4) 每天只在固定時刻修剪一次，避免每分鐘都跑 DELETE
  if (ts.slice(11, 16) === '03:30') await pruneRawLog(env);

  return { ts, ok: results.length, fail: failures.length, changes: changes.length };
}

/**
 * 修剪 wait_log（raw 每分鐘快照）。
 *
 * 背景：raw 每天約 15,100 筆、約 1.5 MB。D1 免費方案單一資料庫上限 500 MB，
 * 不修剪大約 330 天就會撐爆。wait_changes（每天約 600-750 筆）很小，永久保留。
 *
 * 目前是最保守的做法：直接硬刪超過 RAW_RETENTION_DAYS 的 raw。
 * 這是可以調的取捨 —— 見下方 TODO。
 */
async function pruneRawLog(env) {
  const cutoff = daysAgoTaipei(RAW_RETENTION_DAYS);
  await env.DB.prepare('DELETE FROM wait_log WHERE timestamp < ?').bind(cutoff).run();

  // TODO(Danny)：這裡的保留策略值得你自己決定，因為它直接決定未來還能做哪些分析。
  //
  //   選項 A（現況）硬刪 30 天前的 raw
  //     最省空間。但 README「趨勢觀察筆記」提到的「凍結值」判斷需要看
  //     連續的分鐘級資料，超過 30 天就再也回推不了。
  //
  //   選項 B  降頻取樣：超過 30 天的只留每 5 分鐘一筆
  //     空間降為 1/5，仍看得出當日形狀。實作大致是
  //       DELETE FROM wait_log
  //        WHERE timestamp < ? AND CAST(substr(timestamp,15,2) AS INTEGER) % 5 != 0
  //
  //   選項 C  滾成每日彙總表（每店每天最大值／歸零時刻／停止取號時間）後再刪 raw
  //     空間最省且保留長期趨勢，但當日細節永久消失。
  //
  // 選 B 或 C 的話改這個函式就好，其他地方不用動。
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
    // 限定近 6 小時是為了讓查詢只掃索引尾端，資料長大後也不會變慢。
    const since = taipeiTimestamp(new Date(Date.now() - 6 * 3600 * 1000));
    const { results } = await env.DB.prepare(`
      SELECT store_id, store_name, wait_time, num_1, num_2, num_3, num_4,
             togo_numbers, last_time, MAX(timestamp) AS timestamp
      FROM wait_log WHERE timestamp >= ?
      GROUP BY store_id ORDER BY store_id
    `).bind(since).all();
    return json(results ?? []);
  }

  if (path === '/api/changes') {
    const date = url.searchParams.get('date') || taipeiDateString();
    if (!DATE_RE.test(date)) return json({ error: 'bad date' }, 400);
    const { results } = await env.DB.prepare(`
      SELECT timestamp, store_id, store_name, wait_time, prev_value, duration_min
      FROM wait_changes
      WHERE timestamp >= ? AND timestamp < ?
      ORDER BY timestamp ASC, store_id ASC
    `).bind(`${date} 00:00:00`, `${date} 23:59:59`).all();
    return json(results ?? []);
  }

  if (path === '/api/dates') {
    const { results } = await env.DB.prepare(`
      SELECT DISTINCT substr(timestamp,1,10) AS d FROM wait_changes ORDER BY d DESC
    `).all();
    return json((results ?? []).map((r) => r.d));
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
