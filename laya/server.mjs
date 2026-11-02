// Self-hosted Laya: Jev's /v1/systemone on localhost.
//
// Laya (convaiinnovations/laya, run through @receptron/laya) answers typed
// questions about a state with calibrated probabilities and generates no text,
// the same request and response as TypeSafe's Jev. Running it here makes a
// decision free and keeps the state on this machine, so a gate can afford to
// ask about every email rather than a sample.
//
// The model is about 2 GB in memory. It loads on the first request and is
// released after LAYA_IDLE_SECONDS without one; the next request reloads it
// from the local cache in a few seconds. Requests run one at a time.
import http from "node:http";
import { Laya } from "@receptron/laya";

const HOST = process.env.LAYA_HOST || "127.0.0.1";
const PORT = Number(process.env.LAYA_PORT || 8935);
const IDLE_SECONDS = Number(process.env.LAYA_IDLE_SECONDS || 900);
const MAX_BODY = 4 * 1024 * 1024;
const MAX_BATCH = 500;

let laya = null;
let loading = null;
let lastUsed = 0;
let queue = Promise.resolve();
let served = 0;

function log(...args) {
  console.log(new Date().toISOString(), ...args);
}

async function model() {
  if (laya) return laya;
  if (!loading) {
    const t0 = Date.now();
    loading = Laya.load({ ...(process.env.LAYA_MODEL_DIR ? { modelDir: process.env.LAYA_MODEL_DIR } : {}) })
      .then((m) => {
        laya = m;
        log(`model loaded from ${m.modelDir} in ${Date.now() - t0} ms`);
        return m;
      })
      .finally(() => {
        loading = null;
      });
  }
  return loading;
}

// One forward pass at a time: the session is shared, and a queue keeps a burst
// of requests from multiplying the memory a run needs.
function serial(fn) {
  const run = queue.then(fn, fn);
  queue = run.catch(() => {});
  return run;
}

async function answer(item) {
  if (!item || typeof item !== "object" || !item.questions || typeof item.questions !== "object"
      || Array.isArray(item.questions) || !Object.keys(item.questions).length) {
    throw Object.assign(new Error("questions must be an object of name -> question"), { status: 400 });
  }
  const m = await model();
  lastUsed = Date.now();
  const out = await m.systemOne(item.state ?? "", item.questions);
  lastUsed = Date.now();
  served += 1;
  return out;
}

function send(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) });
  res.end(data);
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > MAX_BODY) {
        reject(Object.assign(new Error("request too large"), { status: 413 }));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"));
      } catch {
        reject(Object.assign(new Error("body is not JSON"), { status: 400 }));
      }
    });
    req.on("error", reject);
  });
}

function failure(err) {
  // A bad question (too many options, a repeated label) is the caller's to fix.
  const status = err.status || (/option|label|criteria|question|token/i.test(err.message) ? 422 : 500);
  return { status, body: { error: { type: status === 500 ? "server_error" : "invalid_request", message: err.message } } };
}

const server = http.createServer(async (req, res) => {
  const path = (req.url || "").split("?")[0];
  try {
    if (req.method === "GET" && path === "/health") {
      return send(res, 200, { ok: true, model: "laya", loaded: !!laya, loading: !!loading,
                              idle_seconds: IDLE_SECONDS, served });
    }
    if (req.method === "POST" && path === "/v1/systemone") {
      const body = await readJson(req);
      return send(res, 200, await serial(() => answer(body)));
    }
    // Many states in one request, each its own forward pass: a sweep over a
    // mailbox sends hundreds at once instead of one request per email.
    if (req.method === "POST" && path === "/v1/systemone/batch") {
      const body = await readJson(req);
      const items = Array.isArray(body.items) ? body.items : null;
      if (!items || items.length > MAX_BATCH) {
        return send(res, 400, { error: { type: "invalid_request", message: `items must be a list of at most ${MAX_BATCH}` } });
      }
      const results = await serial(async () => {
        const out = [];
        for (const item of items) {
          try {
            out.push(await answer(item));
          } catch (err) {
            out.push({ error: failure(err).body.error });
          }
        }
        return out;
      });
      return send(res, 200, { model: "laya", results });
    }
    send(res, 404, { error: { type: "not_found", message: `${req.method} ${path}` } });
  } catch (err) {
    const { status, body } = failure(err);
    if (status === 500) log("request failed:", err);
    send(res, status, body);
  }
});

setInterval(async () => {
  if (laya && !loading && Date.now() - lastUsed > IDLE_SECONDS * 1000) {
    await serial(async () => {
      if (laya && Date.now() - lastUsed > IDLE_SECONDS * 1000) {
        const m = laya;
        laya = null;
        await m.close();
        log(`model released after ${IDLE_SECONDS}s idle`);
      }
    });
  }
}, 30_000).unref();

async function shutdown() {
  server.close();
  if (laya) await laya.close().catch(() => {});
  process.exit(0);
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

server.listen(PORT, HOST, () => log(`laya listening on http://${HOST}:${PORT}`));
