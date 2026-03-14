const http = require("http");
const path = require("path");
const { pathToFileURL } = require("url");
const { chromium } = require("playwright");

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------
const state = {
  apiKey: "",
  browser: null,
};

const pool = {
  size: parseInt(process.env.POOL_SIZE || "10", 10),
  available: [],        // [{context, page, ready: Promise}]
  active: new Map(),    // sessionId → {context, page, queue, ready, overflow}
};

// ---------------------------------------------------------------------------
// Browser & page helpers
// ---------------------------------------------------------------------------
async function ensureBrowser() {
  if (state.browser) return state.browser;
  state.browser = await chromium.launch({ headless: true });
  return state.browser;
}

function getFileUrl() {
  const fileUrl = pathToFileURL(path.join(__dirname, "index.html")).toString();
  return `${fileUrl}?key=${encodeURIComponent(state.apiKey)}`;
}

async function warmPage() {
  const browser = await ensureBrowser();
  const vpW = parseInt(process.env.VIEWPORT_WIDTH || "1920", 10);
  const vpH = parseInt(process.env.VIEWPORT_HEIGHT || "1920", 10);
  const context = await browser.newContext({ viewport: { width: vpW, height: vpH } });
  const page = await context.newPage();
  await page.goto(getFileUrl(), { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => window.__SV__ && window.__SV__.ready === true);
  return { context, page };
}

async function initPool(n) {
  const tasks = [];
  for (let i = 0; i < n; i++) {
    tasks.push(
      warmPage()
        .then((item) => pool.available.push(item))
        .catch((err) => console.error("[pool] warmPage failed:", err.message))
    );
  }
  await Promise.all(tasks);
  console.log(`[pool] Ready: ${pool.available.length}/${n} pages`);
}

async function acquire(sessionId) {
  if (pool.active.has(sessionId)) {
    return pool.active.get(sessionId);
  }

  let item;
  let overflow = false;
  if (pool.available.length > 0) {
    item = pool.available.pop();
  } else {
    // Overflow: create on-demand beyond pool size
    console.log(`[pool] Pool exhausted, creating overflow page for ${sessionId}`);
    item = await warmPage();
    overflow = true;
  }

  const session = {
    id: sessionId,
    context: item.context,
    page: item.page,
    queue: Promise.resolve(),
    last_active: Date.now(),
    overflow,
  };
  pool.active.set(sessionId, session);
  return session;
}

async function release(sessionId) {
  const session = pool.active.get(sessionId);
  if (!session) return;
  pool.active.delete(sessionId);

  // Close old page & context
  try {
    if (session.page) await session.page.close();
  } catch {}
  try {
    if (session.context) await session.context.close();
  } catch {}

  // Overflow pages are discarded; pool pages get refilled
  if (!session.overflow) {
    warmPage()
      .then((item) => pool.available.push(item))
      .catch((err) => console.error("[pool] refill failed:", err.message));
  }
}

// ---------------------------------------------------------------------------
// Bridge calls (same as before)
// ---------------------------------------------------------------------------
async function callBridge(session, method, params) {
  session.last_active = Date.now();
  const payload = { method, params: params || {} };
  return session.page.evaluate(
    (req) => window.__SV__[req.method](req.params),
    payload
  );
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------
const handlers = {
  async start(params) {
    const key = (params && params.apiKey) || process.env.GOOGLE_MAPS_API_KEY || "";
    if (!key) throw new Error("missing_api_key");
    state.apiKey = key;
    await ensureBrowser();
    if (pool.available.length === 0 && pool.active.size === 0) {
      await initPool(pool.size);
    }
    return { started: true, poolSize: pool.size };
  },

  async createSession(session) {
    // Session already acquired by the router — just confirm
    return { sessionId: session.id, created: true };
  },

  async init(session, params) {
    return callBridge(session, "init", params);
  },

  async getState(session) {
    return callBridge(session, "getState", {});
  },

  async setPov(session, params) {
    return callBridge(session, "setPov", params);
  },

  async setPano(session, params) {
    return callBridge(session, "setPano", params);
  },

  async setPosition(session, params) {
    return callBridge(session, "setPosition", params);
  },

  async waitForStable(session, params) {
    return callBridge(session, "waitForStable", params);
  },

  async waitForRender(session, params) {
    return callBridge(session, "waitForRender", params);
  },

  async screenshot(session, params) {
    const quality = (params && params.quality) || 85;
    const format = (params && params.format) || "jpeg";
    // Hide any residual Google Maps UI overlays for a clean capture
    await session.page.evaluate(() => {
      const s = document.createElement("style");
      s.id = "_sv_hide";
      s.textContent =
        ".gmnoprint, .gm-bundled-control, .gm-style-cc, " +
        ".gm-compass, .gm-sv-label, .gm-iv-address, " +
        ".gm-style > div > div > div[style*='z-index'] { " +
        "display: none !important; }";
      document.head.appendChild(s);
    });
    await new Promise((r) => setTimeout(r, 50));
    const opts = { type: format, fullPage: false };
    if (format === "jpeg") opts.quality = quality;
    const buf = await session.page.screenshot(opts);
    // Restore controls
    await session.page.evaluate(() => {
      const s = document.getElementById("_sv_hide");
      if (s) s.remove();
    });
    return { imageBase64: buf.toString("base64") };
  },

  async closeSession(session) {
    await release(session.id);
    return { closed: true };
  },
};

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------
function sendJson(res, statusCode, body) {
  res.writeHead(statusCode, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

function parseBody(req) {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (chunk) => (data += chunk));
    req.on("end", () => {
      try {
        resolve(JSON.parse(data));
      } catch {
        resolve({});
      }
    });
  });
}

function matchRoute(method, pathname) {
  // POST /start
  if (method === "POST" && pathname === "/start") {
    return { handler: "start", sid: null };
  }
  // POST /session/create
  if (method === "POST" && pathname === "/session/create") {
    return { handler: "createSession", sid: null };
  }
  // GET /health
  if (method === "GET" && pathname === "/health") {
    return { handler: "_health", sid: null };
  }

  // Session-scoped routes: /session/:sid/<action>
  const m = pathname.match(/^\/session\/([^/]+)\/([a-zA-Z]+)$/);
  if (m) {
    const sid = decodeURIComponent(m[1]);
    const action = m[2];
    return { handler: action, sid };
  }

  // GET /session/:sid/state  (also handled above with action = "state")
  return null;
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const pathname = url.pathname;
  const route = matchRoute(req.method, pathname);

  if (!route) {
    return sendJson(res, 404, { ok: false, error: "not_found" });
  }

  try {
    // Health endpoint
    if (route.handler === "_health") {
      return sendJson(res, 200, {
        ok: true,
        result: {
          status: "ok",
          pool: {
            total: pool.size,
            available: pool.available.length,
            active: pool.active.size,
          },
        },
      });
    }

    // /start — no session needed
    if (route.handler === "start") {
      const body = await parseBody(req);
      const result = await handlers.start(body);
      return sendJson(res, 200, { ok: true, result });
    }

    // /session/create — acquire page from pool
    if (route.handler === "createSession") {
      if (!state.apiKey) {
        return sendJson(res, 400, { ok: false, error: "not_started" });
      }
      const body = await parseBody(req);
      const sessionId = body.sessionId;
      if (!sessionId || typeof sessionId !== "string") {
        return sendJson(res, 400, { ok: false, error: "missing_sessionId" });
      }
      const session = await acquire(sessionId);
      const result = await handlers.createSession(session);
      return sendJson(res, 200, { ok: true, result });
    }

    // All other routes require a session
    const sid = route.sid;
    if (!sid) {
      return sendJson(res, 400, { ok: false, error: "missing_session_id" });
    }
    if (!state.apiKey) {
      return sendJson(res, 400, { ok: false, error: "not_started" });
    }

    const session = pool.active.get(sid);
    if (!session) {
      return sendJson(res, 404, { ok: false, error: "session_not_found" });
    }

    const handler = handlers[route.handler];
    if (!handler) {
      return sendJson(res, 404, { ok: false, error: `unknown_method:${route.handler}` });
    }

    // Queue the operation per-session to serialize Playwright calls
    const body = req.method === "GET" ? {} : await parseBody(req);
    const resultPromise = new Promise((resolve, reject) => {
      const run = async () => handler(session, body);
      session.queue = session.queue
        .then(run, run)
        .then(resolve)
        .catch(reject);
    });

    const result = await resultPromise;
    return sendJson(res, 200, { ok: true, result });
  } catch (err) {
    return sendJson(res, 500, { ok: false, error: String(err.message || err) });
  }
});

// ---------------------------------------------------------------------------
// Startup & shutdown
// ---------------------------------------------------------------------------
async function shutdown() {
  // Close all active sessions
  for (const session of pool.active.values()) {
    try {
      if (session.page) await session.page.close();
      if (session.context) await session.context.close();
    } catch {}
  }
  pool.active.clear();

  // Close pooled pages
  for (const item of pool.available) {
    try {
      if (item.page) await item.page.close();
      if (item.context) await item.context.close();
    } catch {}
  }
  pool.available.length = 0;

  if (state.browser) await state.browser.close();
  state.browser = null;
}

process.on("SIGINT", async () => {
  await shutdown();
  process.exit(0);
});

process.on("SIGTERM", async () => {
  await shutdown();
  process.exit(0);
});

const PORT = parseInt(process.env.STREETVIEW_HOST_PORT || "3000", 10);
server.listen(PORT, () => {
  console.log(`[host] Listening on port ${PORT}`);
  // Pool is initialized on the first /start call when an API key is provided
});
