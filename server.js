// server.js — password-gated static serve of index.html
//
// Railway env vars required:
//   DASHBOARD_PASSWORD — single shared password for dashboard access
//   PORT — Railway injects this automatically
//
// The password is never written to the repo. Login attempts are rate-limited
// per client IP (10/min). On success a cookie `mm_auth` is set (base64 of pw,
// 24h lifetime). All paths except `/api/auth` and the login page require the
// cookie. No revenue-API glue — that was stripped in the April 17 cleanup.

const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const PORT = process.env.PORT || 8787;
const DASHBOARD_PASSWORD = process.env.DASHBOARD_PASSWORD || '';
// Shared secret for /api/forum POST. Workers send `X-Forum-Secret: <hex>`.
// Set in Railway env vars. If unset, POSTs are rejected (GET still works).
const FORUM_SECRET = process.env.FORUM_SECRET || '';

// Forum live store. Prefer a Railway-attached volume mounted at /data so
// posts persist across redeploys. Fall back to repo's data/ (ephemeral on
// Railway) if no volume is attached — in that mode posts between deploys
// are lost. Workers re-emit every 30 min so the floor heals quickly.
// 5MB/yr growth ceiling, no rotation needed.
const REPO_DATA_DIR = path.join(__dirname, 'data');
const VOLUME_DIR = '/data';
const FORUM_SEED = path.join(REPO_DATA_DIR, 'hermes_chat.jsonl');

function pickForumFile() {
  // Probe /data: must exist AND be writable. If yes, use it (persistent).
  try {
    fs.mkdirSync(VOLUME_DIR, { recursive: true });
    fs.accessSync(VOLUME_DIR, fs.constants.W_OK);
    const probe = path.join(VOLUME_DIR, '.write_probe');
    fs.writeFileSync(probe, 'ok');
    fs.unlinkSync(probe);
    return { dir: VOLUME_DIR, file: path.join(VOLUME_DIR, 'forum_live.jsonl'), persistent: true };
  } catch (_) {
    return { dir: REPO_DATA_DIR, file: path.join(REPO_DATA_DIR, 'forum_live.jsonl'), persistent: false };
  }
}

const FORUM_PICK = pickForumFile();
const FORUM_DIR = FORUM_PICK.dir;
const FORUM_FILE = FORUM_PICK.file;
if (FORUM_PICK.persistent) {
  console.log(`[forum] using persistent volume: ${FORUM_FILE}`);
} else {
  console.warn(`[forum] WARNING: no /data volume — using ephemeral ${FORUM_FILE}. ` +
    `Live posts WILL be lost on redeploy. Attach a Railway volume at /data to fix.`);
}
try {
  fs.mkdirSync(FORUM_DIR, { recursive: true });
  if (!fs.existsSync(FORUM_FILE) && fs.existsSync(FORUM_SEED)) {
    fs.copyFileSync(FORUM_SEED, FORUM_FILE);
    console.log(`[forum] seeded ${FORUM_FILE} from hermes_chat.jsonl`);
  }
} catch (e) {
  console.error('[forum] seed init failed:', e.message);
}

// Realized-revenue store. Same volume-vs-repo rules as the forum store.
// Append-only JSONL keyed on tx_hash. Idempotent on tx_hash dedupe.
const REVENUE_FILE = FORUM_PICK.persistent
  ? path.join(VOLUME_DIR, 'realized_revenue.jsonl')
  : path.join(REPO_DATA_DIR, 'realized_revenue.jsonl');
console.log(`[realized-revenue] store: ${REVENUE_FILE}`);

// Layer-2 pending-balance store. Snapshot-shaped (one full fleet aggregate
// per POST). Latest snapshot wins for the summary endpoint; full JSONL
// history kept for trend lines if we ever want them. Source: hourly run
// of scripts/layer2_revenue_collector.py (autonomous, no Chrome dep).
const REVENUE_PENDING_FILE = FORUM_PICK.persistent
  ? path.join(VOLUME_DIR, 'revenue_pending_layer2.jsonl')
  : path.join(REPO_DATA_DIR, 'revenue_pending_layer2.jsonl');
console.log(`[revenue-pending] store: ${REVENUE_PENDING_FILE}`);

function readRealizedRevenueAll() {
  try {
    const txt = fs.readFileSync(REVENUE_FILE, 'utf8');
    const out = [];
    for (const line of txt.split(/\r?\n/)) {
      const s = line.trim();
      if (!s.startsWith('{')) continue;
      try { out.push(JSON.parse(s)); } catch (_) {}
    }
    return out;
  } catch (_) { return []; }
}

function realizedRevenueHasHash(hash) {
  if (!hash) return false;
  try {
    const txt = fs.readFileSync(REVENUE_FILE, 'utf8');
    // Cheap substring check first; full parse only if substring matches.
    if (txt.indexOf(`"${hash}"`) === -1) return false;
    for (const line of txt.split(/\r?\n/)) {
      const s = line.trim();
      if (!s.startsWith('{')) continue;
      try {
        const o = JSON.parse(s);
        if (o.tx_hash === hash) return true;
      } catch (_) {}
    }
  } catch (_) {}
  return false;
}

function readForumAll() {
  try {
    const txt = fs.readFileSync(FORUM_FILE, 'utf8');
    const out = [];
    for (const line of txt.split(/\r?\n/)) {
      const s = line.trim();
      if (!s.startsWith('{')) continue;
      try { out.push(JSON.parse(s)); } catch (_) {}
    }
    return out;
  } catch (_) { return []; }
}

function constantTimeEq(a, b) {
  const ab = Buffer.from(a || '', 'utf8');
  const bb = Buffer.from(b || '', 'utf8');
  if (ab.length !== bb.length) return false;
  return crypto.timingSafeEqual(ab, bb);
}

if (!DASHBOARD_PASSWORD) {
  console.error('[fatal] DASHBOARD_PASSWORD env var is required');
  process.exit(1);
}

const PW_COOKIE_VALUE = Buffer.from(DASHBOARD_PASSWORD).toString('base64');

// Crude per-IP rate limit for /api/auth (10 failed attempts per 60s)
const authAttempts = new Map(); // ip -> { count, resetAt }
function rateLimit(ip) {
  const now = Date.now();
  let e = authAttempts.get(ip);
  if (!e || e.resetAt < now) {
    e = { count: 0, resetAt: now + 60_000 };
    authAttempts.set(ip, e);
  }
  e.count += 1;
  return e.count > 10;
}

function parseCookies(header) {
  const out = {};
  (header || '').split(';').forEach(c => {
    const i = c.indexOf('=');
    if (i > 0) out[c.slice(0, i).trim()] = c.slice(i + 1).trim();
  });
  return out;
}

function isAuthed(req) {
  const cookies = parseCookies(req.headers.cookie || '');
  return cookies.mm_auth === PW_COOKIE_VALUE;
}

function loginPage(res, err) {
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
  res.end(`<!DOCTYPE html><html><head><title>moneymaker-fleet Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0a0e17;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}
  .login{background:#141b2d;padding:40px;border-radius:12px;border:1px solid #1e3a5f;width:340px;text-align:center}
  h1{color:#00d4ff;font-size:1.4em;margin-bottom:8px;letter-spacing:2px}
  p{color:#666;font-size:0.85em;margin-bottom:24px}
  input{width:100%;padding:12px 16px;background:#0a0e17;border:1px solid #1e3a5f;border-radius:8px;color:#e0e0e0;font-size:1em;margin-bottom:16px;outline:none}
  input:focus{border-color:#00d4ff}
  button{width:100%;padding:12px;background:linear-gradient(135deg,#00d4ff,#0088ff);color:#000;font-weight:700;border:none;border-radius:8px;font-size:1em;cursor:pointer}
  button:hover{opacity:0.9}
  .err{color:#ff4444;font-size:0.85em;margin-top:8px;min-height:1em}
</style></head><body>
<div class="login">
  <h1>MONEYMAKER</h1>
  <p>Command Center</p>
  <form id="f" onsubmit="return doLogin()">
    <input type="password" id="pw" placeholder="Password" autofocus autocomplete="current-password">
    <button type="submit">ENTER</button>
    <div class="err" id="err">${err || ''}</div>
  </form>
</div>
<script>
function doLogin(){
  var pw=document.getElementById('pw').value;
  fetch('/api/auth',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})})
    .then(function(r){return r.json().then(function(d){return {status:r.status,data:d}})})
    .then(function(x){
      if(x.data.ok){document.cookie='mm_auth='+x.data.cookie+';path=/;max-age=86400;samesite=lax';location.reload();}
      else if(x.status===429){document.getElementById('err').textContent='Too many attempts — wait a minute';}
      else{document.getElementById('err').textContent='Wrong password';}
    })
    .catch(function(){document.getElementById('err').textContent='Network error';});
  return false;
}
</script></body></html>`);
}

function serveStatic(req, res, filePath, contentType) {
  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Not found');
      return;
    }
    res.writeHead(200, {
      'Content-Type': contentType,
      'Cache-Control': 'no-cache, max-age=0',
    });
    res.end(data);
  });
}

function clientIp(req) {
  const fwd = req.headers['x-forwarded-for'];
  if (fwd) return fwd.split(',')[0].trim();
  return req.socket.remoteAddress || 'unknown';
}

const server = http.createServer((req, res) => {
  // POST /api/auth
  if (req.method === 'POST' && req.url === '/api/auth') {
    const ip = clientIp(req);
    if (rateLimit(ip)) {
      res.writeHead(429, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'rate-limited' }));
      return;
    }
    let body = '';
    req.on('data', chunk => {
      body += chunk;
      if (body.length > 1024) { req.destroy(); }
    });
    req.on('end', () => {
      let provided = '';
      try { provided = JSON.parse(body).password || ''; } catch (_) {}
      if (provided === DASHBOARD_PASSWORD) {
        // Reset this IP's rate bucket on success
        authAttempts.delete(ip);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, cookie: PW_COOKIE_VALUE }));
      } else {
        res.writeHead(401, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false }));
      }
    });
    return;
  }

  // Health endpoint — unauthenticated, no secrets exposed
  if (req.url === '/healthz') {
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    res.end('ok');
    return;
  }

  // ---- Forum API (unauthenticated; POST gated by X-Forum-Secret) ----
  const urlNoQs = (req.url || '').split('?')[0];

  if (req.method === 'POST' && urlNoQs === '/api/forum') {
    if (!FORUM_SECRET) {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'forum-secret-not-configured' }));
      return;
    }
    const sent = req.headers['x-forum-secret'] || '';
    if (!constantTimeEq(String(sent), FORUM_SECRET)) {
      res.writeHead(401, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'unauthorized' }));
      return;
    }
    let body = '';
    req.on('data', chunk => {
      body += chunk;
      if (body.length > 4096) { req.destroy(); }
    });
    req.on('end', () => {
      let p;
      try { p = JSON.parse(body); } catch (_) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'bad-json' }));
        return;
      }
      const { node, model, topic, msg, reply_to } = p || {};
      if (!node || !model || !topic || !msg) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'missing-fields' }));
        return;
      }
      if (typeof msg !== 'string' || Buffer.byteLength(msg, 'utf8') > 2048) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'msg-too-large' }));
        return;
      }
      const entry = {
        ts: new Date().toISOString(),
        id: `${String(node).replace(/[^a-z0-9-]/gi,'')}-${Date.now()}-${Math.random().toString(36).slice(2,7)}`,
        from: `[${node} - ${model}]`,
        topic: String(topic).slice(0, 32),
        msg: String(msg),
      };
      if (reply_to) entry.reply_to = String(reply_to).slice(0, 128);
      try {
        fs.appendFileSync(FORUM_FILE, JSON.stringify(entry) + '\n');
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'append-failed' }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, id: entry.id, ts: entry.ts }));
    });
    return;
  }

  if (req.method === 'GET' && urlNoQs === '/api/forum') {
    const qs = (req.url || '').split('?')[1] || '';
    const params = new URLSearchParams(qs);
    const since = params.get('since') || '';
    let limit = parseInt(params.get('limit') || '200', 10);
    if (!Number.isFinite(limit) || limit < 1) limit = 200;
    if (limit > 1000) limit = 1000;
    let rows = readForumAll();
    if (since) rows = rows.filter(r => (r.ts || '') > since);
    rows.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''));
    rows = rows.slice(0, limit);
    res.writeHead(200, {
      'Content-Type': 'application/json',
      'Cache-Control': 'no-cache, no-store, max-age=0',
    });
    res.end(JSON.stringify(rows));
    return;
  }

  if (req.method === 'GET' && urlNoQs === '/api/forum/stats') {
    const rows = readForumAll();
    const dayCutoff = new Date(Date.now() - 86400 * 1000).toISOString();
    const last24 = rows.filter(r => (r.ts || '') >= dayCutoff);
    const models = new Set();
    const nodes = new Set();
    for (const r of last24) {
      const m = /^\[(.+?)\s-\s(.+)\]$/.exec(r.from || '');
      if (m) { nodes.add(m[1]); models.add(m[2]); }
    }
    const sortedTs = rows.map(r => r.ts).filter(Boolean).sort();
    const lastTs = sortedTs.length ? sortedTs[sortedTs.length - 1] : null;
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      total: rows.length,
      posts_24h: last24.length,
      models_24h: models.size,
      nodes_24h: nodes.size,
      last_ts: lastTs,
    }));
    return;
  }

  // ---- Realized-revenue API (on-chain truth) ----
  // POST: same X-Forum-Secret auth as /api/forum. Idempotent on tx_hash.
  if (req.method === 'POST' && urlNoQs === '/api/realized-revenue') {
    if (!FORUM_SECRET) {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'forum-secret-not-configured' }));
      return;
    }
    const sent = req.headers['x-forum-secret'] || '';
    if (!constantTimeEq(String(sent), FORUM_SECRET)) {
      res.writeHead(401, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'unauthorized' }));
      return;
    }
    let body = '';
    req.on('data', chunk => {
      body += chunk;
      if (body.length > 4096) { req.destroy(); }
    });
    req.on('end', () => {
      let p;
      try { p = JSON.parse(body); } catch (_) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'bad-json' }));
        return;
      }
      const { tx_hash, chain, ts, from, to, amount, token, service, usd_at_time } = p || {};
      if (!tx_hash || !chain || !ts || !token) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'missing-fields' }));
        return;
      }
      if (typeof tx_hash !== 'string' || tx_hash.length > 128) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'tx_hash-invalid' }));
        return;
      }
      // Idempotent: if hash already stored, return ok with deduped: true
      if (realizedRevenueHasHash(tx_hash)) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, deduped: true, tx_hash }));
        return;
      }
      const entry = {
        tx_hash: String(tx_hash),
        chain: String(chain),
        ts: String(ts),
        from: from ? String(from) : '',
        to: to ? String(to) : '',
        amount: typeof amount === 'number' ? amount : Number(amount) || 0,
        token: String(token),
        service: service ? String(service) : 'unknown',
        usd_at_time: typeof usd_at_time === 'number' ? usd_at_time : Number(usd_at_time) || 0,
        recorded_at: new Date().toISOString(),
      };
      try {
        fs.appendFileSync(REVENUE_FILE, JSON.stringify(entry) + '\n');
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'append-failed' }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, tx_hash: entry.tx_hash, recorded_at: entry.recorded_at }));
    });
    return;
  }

  if (req.method === 'GET' && urlNoQs === '/api/realized-revenue') {
    const qs = (req.url || '').split('?')[1] || '';
    const params = new URLSearchParams(qs);
    const since = params.get('since') || '';
    let limit = parseInt(params.get('limit') || '200', 10);
    if (!Number.isFinite(limit) || limit < 1) limit = 200;
    if (limit > 1000) limit = 1000;
    let rows = readRealizedRevenueAll();
    if (since) rows = rows.filter(r => (r.ts || '') > since);
    rows.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''));
    rows = rows.slice(0, limit);
    res.writeHead(200, {
      'Content-Type': 'application/json',
      'Cache-Control': 'no-cache, no-store, max-age=0',
    });
    res.end(JSON.stringify(rows));
    return;
  }

  // DELETE: wipe the JSONL store. Same X-Forum-Secret auth. Used by the
  // collector after sender_addresses.json schema changes so a fresh re-fire
  // populates with up-to-date classification (cleaner than partial reclassify).
  if (req.method === 'DELETE' && urlNoQs === '/api/realized-revenue/admin/reset') {
    if (!FORUM_SECRET) {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'forum-secret-not-configured' }));
      return;
    }
    const sent = req.headers['x-forum-secret'] || '';
    if (!constantTimeEq(String(sent), FORUM_SECRET)) {
      res.writeHead(401, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'unauthorized' }));
      return;
    }
    try {
      const before = readRealizedRevenueAll().length;
      // Truncate (preserves file existence + permissions).
      fs.writeFileSync(REVENUE_FILE, '');
      console.log(`[realized-revenue] admin/reset: wiped ${before} rows`);
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, rows_deleted: before }));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'reset-failed', detail: e.message }));
    }
    return;
  }

  if (req.method === 'GET' && urlNoQs === '/api/realized-revenue/summary') {
    const rows = readRealizedRevenueAll();
    const cutoff30 = new Date(Date.now() - 30 * 86400 * 1000).toISOString();
    const by_service = {};
    const by_service_30d = {};
    let total_lifetime = 0;
    let total_30d = 0;
    let last_ts = null;
    for (const r of rows) {
      const svc = r.service || 'unknown';
      // SIGNATURE_ONLY entries are presence-only (no USD known); skip from totals
      if (r.token === 'SIGNATURE_ONLY') continue;
      const usd = Number(r.usd_at_time) || 0;
      total_lifetime += usd;
      by_service[svc] = (by_service[svc] || 0) + usd;
      if ((r.ts || '') >= cutoff30) {
        total_30d += usd;
        by_service_30d[svc] = (by_service_30d[svc] || 0) + usd;
      }
      if (!last_ts || (r.ts || '') > last_ts) last_ts = r.ts || last_ts;
    }
    // Round for cleanliness
    const round2 = o => {
      const out = {};
      for (const k of Object.keys(o)) out[k] = Math.round(o[k] * 100) / 100;
      return out;
    };
    res.writeHead(200, {
      'Content-Type': 'application/json',
      'Cache-Control': 'no-cache, no-store, max-age=0',
    });
    res.end(JSON.stringify({
      total_30d: Math.round(total_30d * 100) / 100,
      total_lifetime: Math.round(total_lifetime * 100) / 100,
      by_service: round2(by_service),
      by_service_30d: round2(by_service_30d),
      tx_count: rows.length,
      last_ts: last_ts,
    }));
    return;
  }

  // ---- Layer-2 pending-balance API (autonomous on-node truth) ----
  // POST: same X-Forum-Secret auth. Snapshot-shaped (one full fleet aggregate
  // per call). Append-only — latest line wins for summary.
  if (req.method === 'POST' && urlNoQs === '/api/revenue-pending') {
    if (!FORUM_SECRET) {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'forum-secret-not-configured' }));
      return;
    }
    const sent = req.headers['x-forum-secret'] || '';
    if (!constantTimeEq(String(sent), FORUM_SECRET)) {
      res.writeHead(401, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'unauthorized' }));
      return;
    }
    let body = '';
    req.on('data', chunk => {
      body += chunk;
      // Snapshot can be larger than a single tx row (per-node array). Cap at
      // 256 KB — current snapshot is ~12 KB so we have plenty of headroom.
      if (body.length > 262144) { req.destroy(); }
    });
    req.on('end', () => {
      let p;
      try { p = JSON.parse(body); } catch (_) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'bad-json' }));
        return;
      }
      if (!p || typeof p !== 'object' || !p.services) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'missing-services' }));
        return;
      }
      const entry = {
        generated_utc: typeof p.generated_utc === 'string' ? p.generated_utc : new Date().toISOString(),
        recorded_at: new Date().toISOString(),
        fleet_count: Number(p.fleet_count) || 0,
        ok_hosts: Number(p.ok_hosts) || 0,
        services: p.services,
        source: typeof p.source === 'string' ? p.source : 'layer2:on-node-introspection',
        note: typeof p.note === 'string' ? p.note.slice(0, 600) : '',
      };
      try {
        fs.appendFileSync(REVENUE_PENDING_FILE, JSON.stringify(entry) + '\n');
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'append-failed', detail: e.message }));
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, recorded_at: entry.recorded_at }));
    });
    return;
  }

  // GET: latest snapshot (no auth). The dashboard tile polls this.
  if (req.method === 'GET' && urlNoQs === '/api/revenue-pending/summary') {
    let last = null;
    try {
      const txt = fs.readFileSync(REVENUE_PENDING_FILE, 'utf8');
      // Find last non-empty JSON line (cheap; file stays small — one row/hour
      // = ~8 KB/day, ~3 MB/yr ceiling).
      const lines = txt.split(/\r?\n/);
      for (let i = lines.length - 1; i >= 0; i--) {
        const s = lines[i].trim();
        if (!s.startsWith('{')) continue;
        try { last = JSON.parse(s); break; } catch (_) {}
      }
    } catch (_) {
      // file may not exist yet — that's fine, return empty shell
    }
    res.writeHead(200, {
      'Content-Type': 'application/json',
      'Cache-Control': 'no-cache, no-store, max-age=0',
    });
    if (!last) {
      res.end(JSON.stringify({
        empty: true,
        message: 'no Layer-2 snapshot yet — first hourly run pending',
      }));
      return;
    }
    res.end(JSON.stringify(last));
    return;
  }

  // Public OpenAPI spec for the scraping API — served unauthenticated so
  // RapidAPI / Apify / SwaggerHub can ingest it via "import from URL".
  // Path is intentionally specific so it never collides with index.html
  // routing. Cache 5 min so frequent re-imports don't read from disk.
  if (req.method === 'GET' && urlNoQs === '/api/scraping-api/openapi.yaml') {
    const filePath = path.join(__dirname, 'scripts', 'scraping_api', 'openapi.yaml');
    fs.readFile(filePath, (err, data) => {
      if (err) {
        res.writeHead(404, { 'Content-Type': 'text/plain' });
        res.end('openapi.yaml not found');
        return;
      }
      res.writeHead(200, {
        'Content-Type': 'application/x-yaml; charset=utf-8',
        'Cache-Control': 'public, max-age=300',
        'Access-Control-Allow-Origin': '*',
      });
      res.end(data);
    });
    return;
  }

  // Everything else requires auth
  if (!isAuthed(req)) { loginPage(res); return; }

  // Serve index.html for /, /index.html
  const urlPath = (req.url || '/').split('?')[0];
  if (urlPath === '/' || urlPath === '/index.html') {
    serveStatic(req, res, path.join(__dirname, 'index.html'), 'text/html; charset=utf-8');
    return;
  }

  // Serve any other static asset living next to index.html, but ONLY if the
  // path does not escape the repo root (defensive path sanitization).
  const safeTail = urlPath.replace(/^\/+/, '').replace(/\.\.\//g, '');
  const full = path.join(__dirname, safeTail);
  if (!full.startsWith(__dirname)) {
    res.writeHead(403, { 'Content-Type': 'text/plain' });
    res.end('Forbidden');
    return;
  }
  // Restrict to common static types so we never accidentally serve server.js,
  // package.json, .env, or anything sensitive.
  const ext = path.extname(full).toLowerCase();
  const mimeMap = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
  };
  if (!mimeMap[ext]) {
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('Not found');
    return;
  }
  serveStatic(req, res, full, mimeMap[ext]);
});

server.listen(PORT, () => {
  console.log(`[mm-dashboard] listening on :${PORT}`);
});
