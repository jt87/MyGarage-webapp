// Service worker: keeps the app usable with no connection (a basement, a barn,
// a dead zone). Served from /sw.js so it covers the whole site; the server
// fills in VERSION, which changes whenever a file in static/ does.
//
//   Pages     network first (4 s), then the saved copy. Every page is saved
//             ahead of time from /offline/manifest.json whenever the repo changes.
//   Thumbs    saved ahead of time; any saved size stands in for a missing one.
//   Files     manuals, receipts and STLs are saved the first time they're
//             opened (or all at once from Settings), then served from here,
//             including the byte ranges PDF.js asks for.
//   Static    cache first, per version.
//
// Edits made offline don't go through here: app.js keeps them in IndexedDB
// and sends them when it's back online (iOS has no Background Sync).

const VERSION = "__VERSION__";
const STATIC = `static-${VERSION}`;
const PAGES = "pages-v1";
const THUMBS = "thumbs-v1";
const FILES = "files-v1";
const META = "meta-v1";
const PAGE_TIMEOUT_MS = 4000;
// Never saved: logins, and Settings (it shows the API key).
const NO_STORE = ["/login", "/logout", "/setup", "/settings", "/sync"];

const PRECACHE = [
  `/static/style.css?v=${VERSION}`, `/static/app.js?v=${VERSION}`,
  `/static/pdfview.js?v=${VERSION}`, `/static/stl.js?v=${VERSION}`,
  "/static/icons.svg", "/static/chevron.svg", "/static/icon.svg", "/static/apple-touch-icon.png",
  "/static/vendor/pdfjs/pdf.min.js", "/static/vendor/pdfjs/pdf.worker.min.js",
  "/static/vendor/three/three.module.min.js", "/static/vendor/three/STLLoader.js",
  "/static/vendor/three/OrbitControls.js", "/manifest.webmanifest",
];

self.addEventListener("install", (e) => {
  e.waitUntil((async () => {
    const cache = await caches.open(STATIC);
    // One missing file shouldn't stop the rest from being saved.
    await Promise.all(PRECACHE.map((u) => cache.add(u).catch(() => {})));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) {
      if (k.startsWith("static-") && k !== STATIC) await caches.delete(k);
    }
    await self.clients.claim();
  })());
});

// ------------------------------------------------------------------ routing

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  const p = url.pathname;
  if (p === "/sw.js" || p === "/healthz" || p.startsWith("/webhook/") || p.startsWith("/offline/")
      || p === "/insurance.csv") return;
  if (p.startsWith("/static/") || p === "/manifest.webmanifest") {
    e.respondWith(cacheFirst(req, STATIC));
  } else if (p.startsWith("/thumb/")) {
    e.respondWith(thumb(req, url));
  } else if (p.startsWith("/files/")) {
    e.respondWith(file(req, url, e));
  } else if (req.mode === "navigate" || (req.headers.get("accept") || "").includes("text/html")) {
    e.respondWith(page(req, url, e));
  }
});

async function cacheFirst(req, name) {
  const cache = await caches.open(name);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok && res.type === "basic") cache.put(req, res.clone());
  return res;
}

// ------------------------------------------------------------------ pages

function pageKey(href) {
  const u = new URL(href, location.origin);
  u.searchParams.delete("msg");
  u.searchParams.delete("error");
  u.hash = "";
  return u.href;
}

function storable(res, url) {
  return res && res.ok && res.status === 200 && res.type === "basic" && !res.redirected
    && (res.headers.get("content-type") || "").includes("text/html")
    && !NO_STORE.some((p) => url.pathname.startsWith(p));
}

function stamped(res) {
  const headers = new Headers(res.headers);
  headers.set("x-saved-at", String(Date.now()));
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
}

// The saved copy is marked so the page can say it's showing one.
async function savedCopy(res) {
  const at = res.headers.get("x-saved-at") || "";
  const html = (await res.text()).replace("<body", `<body data-offline-copy="${at}"`);
  return new Response(html, { headers: { "content-type": "text/html; charset=utf-8" } });
}

async function page(req, url, e) {
  const key = pageKey(req.url);
  const cache = await caches.open(PAGES);
  const net = fetch(req).then(async (res) => {
    if (storable(res, url)) await cache.put(key, stamped(res.clone()));
    return res;
  });
  try {
    const res = await Promise.race([net, new Promise((r) => setTimeout(r, PAGE_TIMEOUT_MS, null))]);
    if (res) return res;
    const saved = await cache.match(key);
    if (saved) {
      e.waitUntil(net.catch(() => {}));  // a slow network still refreshes the saved copy
      return savedCopy(saved);
    }
    return await net;
  } catch {
    const saved = await cache.match(key)
      || (url.pathname === "/search" ? await cache.match(pageKey("/search")) : undefined)
      || await cache.match(pageKey("/offline"));
    return saved ? savedCopy(saved) : Response.error();
  }
}

// ------------------------------------------------------------------ thumbnails

async function thumb(req, url) {
  const cache = await caches.open(THUMBS);
  const versioned = url.searchParams.has("v");
  if (versioned) {
    const hit = await cache.match(req);
    if (hit) return hit;
  }
  try {
    const res = await fetch(req);
    if (res.ok && res.type === "basic" && !res.redirected) cache.put(req, res.clone());
    return res;
  } catch {
    const hit = await cache.match(req);
    if (hit) return hit;
    // Any saved size of the same photo will do; prefer the nearest bigger one.
    const want = Number(url.searchParams.get("w") || 640);
    const same = (await cache.keys()).map((k) => new URL(k.url)).filter((u) =>
      u.pathname === url.pathname && u.searchParams.get("v") === url.searchParams.get("v"));
    if (!same.length) return Response.error();
    same.sort((a, b) => score(a) - score(b));
    return cache.match(same[0].href);
    function score(u) {
      const w = Number(u.searchParams.get("w") || 640);
      return w >= want ? w - want : (want - w) * 4;
    }
  }
}

// ------------------------------------------------------------------ files

const fileKey = (url) => url.origin + url.pathname;
const checked = new Set();   // files revalidated since this worker started
const inflight = new Map();

async function file(req, url, e) {
  const key = fileKey(url);
  const cache = await caches.open(FILES);
  const hit = await cache.match(key);
  if (hit) {
    if (!checked.has(key)) e.waitUntil(revalidate(key, hit));
    const range = req.headers.get("range");
    return range ? slice(hit, range) : hit;
  }
  try {
    const res = await fetch(req);
    // PDF.js reads in byte ranges; save the whole file separately.
    if (res.ok) e.waitUntil(keep(key));
    return res;
  } catch {
    return new Response("This file isn't saved on this device.", { status: 504 });
  }
}

function keep(key) {
  if (inflight.has(key)) return inflight.get(key);
  const job = (async () => {
    try {
      const res = await fetch(key, { cache: "no-store" });
      if (res.ok && res.status === 200 && res.type === "basic" && !res.redirected) {
        await (await caches.open(FILES)).put(key, res);
        checked.add(key);
        return true;
      }
    } catch {}
    return false;
  })().finally(() => inflight.delete(key));
  inflight.set(key, job);
  return job;
}

// Files rarely change, so a saved copy is served at once and checked afterwards.
async function revalidate(key, hit) {
  checked.add(key);
  const etag = hit.headers.get("etag");
  try {
    const res = await fetch(key, { cache: "no-store", headers: etag ? { "If-None-Match": etag } : {} });
    if (res.status === 200 && res.type === "basic" && !res.redirected) {
      await (await caches.open(FILES)).put(key, res);
    } else if (res.status === 404) {
      await (await caches.open(FILES)).delete(key);
    }
  } catch {}
}

async function slice(res, range) {
  const blob = await res.blob();
  const size = blob.size;
  const m = /bytes=(\d*)-(\d*)/.exec(range);
  let start = m && m[1] ? Number(m[1]) : NaN;
  let end = m && m[2] ? Number(m[2]) : size - 1;
  if (Number.isNaN(start)) { start = Math.max(0, size - end); end = size - 1; }  // suffix range
  end = Math.min(end, size - 1);
  if (!m || start > end) {
    return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}` } });
  }
  return new Response(blob.slice(start, end + 1), {
    status: 206,
    headers: {
      "Content-Type": res.headers.get("content-type") || "application/octet-stream",
      "Content-Range": `bytes ${start}-${end}/${size}`,
      "Content-Length": String(end - start + 1),
      "Accept-Ranges": "bytes",
    },
  });
}

// ------------------------------------------------------------------ saving ahead

let lastRefresh = 0;
let refreshing = null;

async function manifest() {
  const res = await fetch("/offline/manifest.json", { cache: "no-store" });
  if (!res.ok || res.redirected || !(res.headers.get("content-type") || "").includes("json")) return null;
  return res.json();
}

async function pool(items, n, fn) {
  const queue = [...items];
  await Promise.all(Array.from({ length: n }, async () => {
    while (queue.length) {
      try { await fn(queue.shift()); } catch {}
    }
  }));
}

// Save every page and thumbnail when the repo has changed since last time.
function refresh(force) {
  if (refreshing) return refreshing;
  if (!force && Date.now() - lastRefresh < 60 * 1000) return Promise.resolve(false);
  lastRefresh = Date.now();
  refreshing = (async () => {
    const m = await manifest();
    if (!m) return false;
    const meta = await caches.open(META);
    const prev = await meta.match("/__version");
    if (!force && prev && (await prev.text()) === m.version) return false;

    const pages = await caches.open(PAGES);
    let ok = 0;
    await pool(m.pages, 4, async (u) => {
      const url = new URL(u, location.origin);
      const res = await fetch(url, { headers: { Accept: "text/html" }, credentials: "same-origin" });
      if (storable(res, url)) { await pages.put(pageKey(url.href), stamped(res)); ok++; }
    });
    if (!ok) return false;  // logged out, or the server is struggling: try again later
    const wantPages = new Set(m.pages.map((u) => pageKey(u)));
    for (const k of await pages.keys()) {
      if (!wantPages.has(k.url) && new URL(k.url).pathname !== "/search") await pages.delete(k);
    }

    const thumbs = await caches.open(THUMBS);
    const have = new Set((await thumbs.keys()).map((k) => k.url));
    await pool(m.thumbs.filter((u) => !have.has(new URL(u, location.origin).href)), 4, async (u) => {
      const res = await fetch(u);
      if (res.ok && res.type === "basic" && !res.redirected) await thumbs.put(u, res);
    });
    // Drop thumbnails of photos that were replaced or removed.
    const photoIds = new Set(m.thumbs.map((u) => photoId(new URL(u, location.origin))));
    for (const k of await thumbs.keys()) {
      const u = new URL(k.url);
      if (u.searchParams.has("v") && !photoIds.has(photoId(u))) await thumbs.delete(k);
    }

    const files = await caches.open(FILES);
    const wantFiles = new Set(m.files.map((f) => new URL(f.url, location.origin).href));
    for (const k of await files.keys()) if (!wantFiles.has(k.url)) await files.delete(k);

    await meta.put("/__version", new Response(m.version));
    return true;
  })().finally(() => { refreshing = null; });
  return refreshing;
}

const photoId = (u) => `${u.pathname}?v=${u.searchParams.get("v")}`;

async function saveAll(port) {
  const m = await manifest();
  if (!m) return { ok: false };
  const files = await caches.open(FILES);
  const have = new Set((await files.keys()).map((k) => k.url));
  const todo = m.files.filter((f) => f.kind !== "image" && !have.has(new URL(f.url, location.origin).href));
  let done = 0;
  await pool(todo, 2, async (f) => {
    await keep(new URL(f.url, location.origin).href);
    done++;
    if (port) port.postMessage({ progress: done, total: todo.length });
  });
  return { ok: true, saved: done };
}

async function status(urls) {
  const files = await caches.open(FILES);
  const out = {};
  for (const u of urls) out[u] = !!(await files.match(fileKey(new URL(u, location.origin))));
  return out;
}

async function summary() {
  const m = await manifest().catch(() => null);
  const files = await caches.open(FILES);
  const have = new Set((await files.keys()).map((k) => k.url));
  const docs = m ? m.files.filter((f) => f.kind !== "image") : [];
  const missing = docs.filter((f) => !have.has(new URL(f.url, location.origin).href));
  return { files: have.size, missing: missing.length, missingBytes: missing.reduce((s, f) => s + f.size, 0) };
}

self.addEventListener("message", (e) => {
  const msg = e.data || {};
  const port = e.ports && e.ports[0];
  const reply = (v) => port && port.postMessage({ done: true, result: v });
  const work = (async () => {
    switch (msg.type) {
      case "refresh": return reply(await refresh(!!msg.force));
      case "saveAll": return reply(await saveAll(port));
      case "status": return reply(await status(msg.urls || []));
      case "summary": return reply(await summary());
      case "clearFiles":
        await caches.delete(FILES);
        return reply(true);
      case "clear":  // logging out: nothing from this account stays on the device
        for (const k of await caches.keys()) if (!k.startsWith("static-")) await caches.delete(k);
        lastRefresh = 0;
        return reply(true);
    }
  })().catch(() => reply(null));
  e.waitUntil(work);
});
