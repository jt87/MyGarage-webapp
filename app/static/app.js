// Interaction layer for the Apple-style UI. Every page still works without it.
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  };

  // ---------------------------------------------------------------- toast
  const toastEl = $("[data-toast]");
  let toastTimer;
  function toast(msg) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("show"), 1600);
  }

  // ---------------------------------------------------------------- stale page reload
  // A Home Screen app can sit in the background for days and Safari restores
  // pages from its back/forward cache, so reload a stale page on return.
  const loadedAt = Date.now();
  const STALE_MS = 2 * 60 * 1000;
  const canReload = () => !$("dialog[open]") && !$("#pdf") && !$("form[data-edit]") && !document.activeElement?.matches("input, textarea");
  addEventListener("pageshow", (e) => { if (e.persisted && Date.now() - loadedAt > STALE_MS) location.reload(); });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && Date.now() - loadedAt > STALE_MS && canReload()) location.reload();
  });

  // ---------------------------------------------------------------- nav bar
  // Large title collapses into the nav bar on scroll, as in iOS.
  const navbar = $("[data-navbar]");
  const bigTitle = $(".large-title h1, [data-title]");
  if (navbar && bigTitle && !navbar.classList.contains("always")) {
    new IntersectionObserver(([e]) => navbar.classList.toggle("scrolled", !e.isIntersecting),
      { rootMargin: `-${navbar.offsetHeight}px 0px 0px 0px` }).observe(bigTitle);
  } else if (navbar && bigTitle) {
    // Detail screens: the inline title appears once the page title scrolls away.
    navbar.classList.remove("always");
    navbar.classList.add("scrolled");
    const title = $(".nav-title", navbar);
    title.style.opacity = 0;
    new IntersectionObserver(([e]) => { title.style.opacity = e.isIntersecting ? 0 : 1; },
      { rootMargin: `-${navbar.offsetHeight}px 0px 0px 0px` }).observe(bigTitle);
  }

  // ---------------------------------------------------------------- photo gallery
  $$("[data-gallery]").forEach((g) => {
    const slides = $(".slides", g);
    const dots = $$(".dots i", g);
    let raf;
    slides.addEventListener("scroll", () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const i = Math.round(slides.scrollLeft / slides.clientWidth);
        dots.forEach((d, j) => d.toggleAttribute("aria-current", i === j));
      });
    }, { passive: true });
    const box = $("[data-lightbox]");
    if (box && box.showModal) {
      $$(".slide", g).forEach((s) => s.addEventListener("click", () => {
        $("img", box).src = s.dataset.full;
        box.showModal();
      }));
      box.addEventListener("click", (e) => { if (e.target === box || e.target.tagName === "IMG") box.close(); });
    }
  });

  // ---------------------------------------------------------------- list filter
  $$("[data-filter]").forEach((input) => {
    const items = $$(input.dataset.filter);
    input.addEventListener("input", () => {
      const terms = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      items.forEach((el) => {
        const t = el.dataset.text || el.textContent.toLowerCase();
        el.hidden = !terms.every((w) => t.includes(w));
      });
      $$("[data-filter-group]").forEach((g) => {
        const visible = $$(input.dataset.filter, g).filter((el) => !el.hidden);
        g.hidden = !visible.length;
        $$(input.dataset.filter, g).forEach((el) => el.classList.toggle("nosep", el === visible[0]));
      });
    });
  });

  // ---------------------------------------------------------------- a new major version
  const majorUpdate = $("[data-major-update]");
  if (majorUpdate && majorUpdate.showModal) majorUpdate.showModal();

  // ---------------------------------------------------------------- action sheet ("where to buy")
  const sheet = $("[data-sheet]");
  function openSheet(data) {
    if (!sheet || !sheet.showModal) return;
    const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    let html = `<div class="sheet-group"><div class="sheet-head"><strong>${esc(data.title)}</strong>${data.message ? `<span>${esc(data.message)}</span>` : ""}</div>`;
    data.links.forEach((l) => {
      const label = l.label === "Search web" ? "Search the Web" : `Search ${l.label}`;
      html += `<a class="sheet-btn" href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;
    });
    (data.more || []).forEach((m) => {
      html += `<a class="sheet-btn sheet-sub" href="${esc(m.url)}" target="_blank" rel="noopener noreferrer">Search “${esc(m.part)}”</a>`;
    });
    if (data.parts && data.parts.length) {
      html += `<button class="sheet-btn" type="button" data-copy="${esc(data.parts[0])}">Copy Part Number</button>`;
    }
    html += `</div><button class="sheet-btn sheet-cancel" type="button" data-close>Cancel</button>`;
    sheet.innerHTML = html;
    sheet.showModal();
  }
  if (sheet) {
    sheet.addEventListener("click", async (e) => {
      const copy = e.target.closest("[data-copy]");
      if (copy) {
        try { await navigator.clipboard.writeText(copy.dataset.copy); toast("Copied " + copy.dataset.copy); } catch {}
      }
      if (e.target === sheet || e.target.closest("[data-close], a, [data-copy]")) sheet.close();
    });
  }
  document.addEventListener("click", (e) => {
    const trigger = e.target.closest("[data-open-buy]");
    if (!trigger || e.target.closest("a")) return;
    const row = trigger.closest("[data-buy]");
    if (row) openSheet(JSON.parse(row.dataset.buy));
  });
  document.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-open-buy][role=button]")) {
      e.preventDefault();
      e.target.click();
    }
  });

  // ---------------------------------------------------------------- share sheet
  $$("[data-share]").forEach((b) => b.addEventListener("click", async () => {
    const data = { title: b.dataset.shareTitle || document.title, url: location.href.split("#")[0] };
    if (navigator.share) { try { await navigator.share(data); } catch {} }
    else { try { await navigator.clipboard.writeText(data.url); toast("Link Copied"); } catch {} }
  }));
  // Files: share the file itself (Save to Files, Print, AirDrop, Books).
  // Safari only allows the share sheet right after a tap, so the file is
  // fetched ahead of time; waiting for a download inside the tap handler
  // would lose that permission.
  $$("[data-share-file]").forEach((b) => {
    const url = b.dataset.shareFile;
    let file = null;
    fetch(url).then((r) => r.ok ? r.blob() : null).then((blob) => {
      if (blob) file = new File([blob], b.dataset.name, { type: blob.type });
    }).catch(() => {});
    b.addEventListener("click", async () => {
      try {
        if (file && navigator.canShare && navigator.canShare({ files: [file] })) {
          await navigator.share({ files: [file], title: b.dataset.name });
        } else if (navigator.share) {
          await navigator.share({ title: b.dataset.name, url: location.href.split("#")[0] });
        } else {
          location.href = url + "?download";
        }
      } catch (err) {
        if (!err || err.name !== "AbortError") location.href = url + "?download";
      }
    });
  });

  // ---------------------------------------------------------------- shopping checklist
  const KEY = "garage.shopping";
  const checked = new Set(store.get(KEY) || []);
  const boxes = $$("input[data-key]");
  const countEl = $("[data-check-count]");
  const baseCount = countEl ? countEl.textContent : "";
  const refresh = () => {
    if (countEl) countEl.textContent = checked.size ? `${checked.size} to buy · ${baseCount}` : baseCount;
  };
  boxes.forEach((cb) => {
    const row = cb.closest(".row");
    cb.checked = checked.has(cb.dataset.key);
    row.classList.toggle("checked", cb.checked);
    cb.addEventListener("change", () => {
      cb.checked ? checked.add(cb.dataset.key) : checked.delete(cb.dataset.key);
      row.classList.toggle("checked", cb.checked);
      store.set(KEY, [...checked]);
      refresh();
    });
  });
  refresh();
  const clear = $("[data-clear-checks]");
  if (clear) clear.addEventListener("click", () => {
    checked.clear(); store.set(KEY, []);
    boxes.forEach((cb) => { cb.checked = false; cb.closest(".row").classList.remove("checked"); });
    refresh();
  });

  // ---------------------------------------------------------------- settings
  // Each secret's row has its own reveal (and maybe copy) button.
  $$("[data-reveal]").forEach((reveal) => {
    const keyEl = $("[data-api-key]", reveal.closest(".row"));
    if (!keyEl) return;
    const label = reveal.getAttribute("aria-label");
    reveal.addEventListener("click", () => {
      const shown = keyEl.textContent === keyEl.dataset.value;
      keyEl.textContent = shown ? "•".repeat(24) : keyEl.dataset.value;
      reveal.setAttribute("aria-label", shown ? label : label.replace("Show", "Hide"));
    });
  });
  $$("[data-copy-key]").forEach((copyKey) => {
    const keyEl = $("[data-api-key]", copyKey.closest(".row"));
    if (keyEl) copyKey.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(keyEl.dataset.value); toast("API Key Copied"); } catch {}
    });
  });
  // Storage: confirm a switch of the primary copy before sending it.
  $$("[data-confirm-actions]").forEach((f) => f.addEventListener("submit", (e) => {
    const msg = e.submitter && e.submitter.dataset.confirm;
    if (msg && !confirm(msg)) e.preventDefault();
    else if (e.submitter) setTimeout(() => { e.submitter.textContent = "Working…"; }, 0);
  }));
  const methodSel = $("[data-method]");
  const extRow = $("[data-external-confirm]");
  if (methodSel && extRow) methodSel.addEventListener("change", () => { extRow.hidden = methodSel.value !== "external"; });

  // ---------------------------------------------------------------- keyboard (iPad/Mac)
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/input|textarea|select/i.test(document.activeElement.tagName)) {
      const s = $(".sidebar .searchbar input") && innerWidth >= 1024 ? $(".sidebar .searchbar input") : $(".searchbar input");
      if (s) { e.preventDefault(); s.focus(); }
      else location.href = "/search";
    }
  });

  // ---------------------------------------------------------------- flash message
  // Saves redirect back with ?msg=…; show it once, then tidy the URL.
  if (toastEl && toastEl.dataset.flash) toast(toastEl.dataset.flash);
  if (/[?&](msg|error)=/.test(location.search)) {
    const u = new URL(location.href);
    u.searchParams.delete("msg");
    if (!$("[data-form-error]:not([hidden])")) u.searchParams.delete("error");
    history.replaceState(history.state, "", u.pathname + u.search + u.hash);
  }

  // ---------------------------------------------------------------- service worker
  // Needs HTTPS (or localhost); over plain http on the LAN the app still works,
  // just not offline.
  const sw = "serviceWorker" in navigator && isSecureContext ? navigator.serviceWorker : null;
  function swCall(msg, onProgress) {
    return new Promise((resolve) => {
      const target = sw && sw.controller;
      if (!target) return resolve(null);
      const ch = new MessageChannel();
      ch.port1.onmessage = (e) => {
        if (e.data && e.data.done) resolve(e.data.result);
        else if (onProgress) onProgress(e.data);
      };
      target.postMessage(msg, [ch.port2]);
    });
  }
  if (sw) {
    sw.register("/sw.js").catch(() => {});
    const kick = () => { if (navigator.onLine && !document.body.dataset.offlineCopy) swCall({ type: "refresh" }); };
    if (sw.controller) kick(); else sw.addEventListener("controllerchange", kick, { once: true });
    if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
  }

  // ---------------------------------------------------------------- offline banner
  const offlineBar = $("[data-offline-bar]");
  function showOffline() {
    const copy = document.body.dataset.offlineCopy !== undefined;
    const off = !navigator.onLine || copy;
    if (offlineBar) {
      const at = Number(document.body.dataset.offlineCopy);
      const when = at ? ` from ${new Date(at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}` : "";
      $("[data-offline-text]", offlineBar).textContent = !navigator.onLine
        ? `Offline. Showing the copy saved on this device${when}.`
        : `Slow connection. Showing the copy saved on this device${when}.`;
      offlineBar.hidden = !off;
    }
    const note = $("[data-offline-note]");
    if (note) note.hidden = navigator.onLine;
  }
  showOffline();
  addEventListener("online", showOffline);
  addEventListener("offline", showOffline);

  // ---------------------------------------------------------------- outbox (edits waiting to send)
  // Edits made without a connection are kept in IndexedDB, files included,
  // and sent in order the next time the app is open and online. Each carries
  // an edit_id so a resend after a lost reply isn't saved twice.
  const outbox = (() => {
    let dbp;
    const db = () => dbp || (dbp = new Promise((res, rej) => {
      const r = indexedDB.open("mygarage", 1);
      r.onupgradeneeded = () => r.result.createObjectStore("outbox", { keyPath: "id" });
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    }));
    const tx = async (mode, fn) => {
      const d = await db();
      return new Promise((res, rej) => {
        const t = d.transaction("outbox", mode);
        const out = fn(t.objectStore("outbox"));
        t.oncomplete = () => res(out && "result" in out ? out.result : undefined);
        t.onerror = () => rej(t.error);
      });
    };
    return {
      add: (item) => tx("readwrite", (s) => s.put(item)),
      put: (item) => tx("readwrite", (s) => s.put(item)),
      remove: (id) => tx("readwrite", (s) => s.delete(id)),
      all: async () => { try { return ((await tx("readonly", (s) => s.getAll())) || []).sort((a, b) => a.created - b.created); } catch { return []; } },
      clear: () => tx("readwrite", (s) => s.clear()),
    };
  })();

  const uuid = () => (crypto.randomUUID ? crypto.randomUUID()
    : Date.now().toString(36) + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2));

  function formItem(form, submitter) {
    const fd = new FormData(form);
    if (submitter && submitter.name) fd.set(submitter.name, submitter.value);
    if (!fd.get("edit_id")) fd.set("edit_id", uuid());
    // Files become Blobs in IndexedDB; empty file inputs are dropped.
    const fields = [...fd.entries()].filter(([, v]) => !(v instanceof File) || v.size || v.name);
    return { id: fd.get("edit_id"), url: form.action, label: form.dataset.label || "Edit",
             done: form.dataset.done || location.pathname, fields, created: Date.now() };
  }

  async function send(item) {
    const fd = new FormData();
    item.fields.forEach(([k, v]) => (v instanceof Blob ? fd.append(k, v, v.name || "file") : fd.append(k, v)));
    const res = await fetch(item.url, { method: "POST", body: fd, headers: { Accept: "application/json" },
                                        credentials: "same-origin", redirect: "manual" });
    let data = {};
    try { data = await res.json(); } catch {}
    if (res.status === 401 || res.type === "opaqueredirect") {
      data = { ok: false, error: "You've been logged out. Log in again and it will be sent.", retry: true, auth: true };
    }
    return { status: res.status, ...data };
  }

  const withMsg = (url, msg) => {
    const [base, frag] = url.split("#");
    return `${base}${base.includes("?") ? "&" : "?"}msg=${encodeURIComponent(msg)}${frag ? "#" + frag : ""}`;
  };

  let flushing = false;
  async function flush(manual) {
    if (flushing || !navigator.onLine) return;
    flushing = true;
    let sent = 0;
    try {
      const run = async () => {
        for (const item of await outbox.all()) {
          if (item.error && !manual && !item.retry) continue;
          let r;
          try { r = await send(item); } catch { break; }  // still offline
          if (r.ok) { await outbox.remove(item.id); sent++; continue; }
          item.error = r.error || `Couldn't save (error ${r.status}).`;
          item.retry = !!r.retry;
          await outbox.put(item);
          if (r.retry) break;  // GitHub or the login is the problem: everything else would fail too
        }
      };
      if (navigator.locks) await navigator.locks.request("mygarage-outbox", run); else await run();
    } finally {
      flushing = false;
    }
    await renderOutbox();
    if (sent) {
      toast(sent === 1 ? "1 Edit Sent" : `${sent} Edits Sent`);
      if (canReload() && !$("form[data-edit]")) setTimeout(() => location.reload(), 900);
    }
  }

  const ago = (t) => {
    const s = (Date.now() - t) / 1000;
    return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} hr ago` : new Date(t).toLocaleDateString();
  };
  const escHTML = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  async function renderOutbox() {
    const items = await outbox.all();
    const note = $("[data-outbox-note]");
    if (note) {
      note.hidden = !items.length;
      note.innerHTML = items.length ? `${items.length} edit${items.length === 1 ? "" : "s"} waiting to send<br>` : "";
    }
    $$("[data-outbox]").forEach((box) => {
      const scope = box.dataset.outbox;
      const mine = scope ? items.filter((i) => i.done.split(/[?#]/)[0] === scope) : items;
      box.hidden = !mine.length;
      if (!mine.length) { box.innerHTML = ""; return; }
      const authErr = mine.some((i) => i.error && /logged out/i.test(i.error));
      box.innerHTML = `<div class="section-header">Waiting to Send</div><div class="group">` + mine.map((i) => {
        const files = i.fields.filter(([, v]) => v instanceof Blob).length;
        return `<div class="row has-icon" data-item="${escHTML(i.id)}"><span class="tile" style="--tile: var(${i.error && !i.retry ? "--red" : "--orange"})"><svg aria-hidden="true"><use href="/static/icons.svg#${i.error && !i.retry ? "alert-triangle" : "clock"}"/></svg></span>`
          + `<span class="row-body"><span class="row-title clip">${escHTML(i.label)}</span>`
          + `<span class="row-sub">${ago(i.created)}${files ? ` · ${files} file${files === 1 ? "" : "s"}` : ""}${i.error ? ` · ${escHTML(i.error)}` : ""}</span></span>`
          + `<button class="row-btn destructive" type="button" data-discard="${escHTML(i.id)}">Discard</button></div>`;
      }).join("") + `<button class="row action" type="button" data-send-now>Send Now</button>`
        + (authErr ? `<a class="row action" href="/login?next=${encodeURIComponent(location.pathname)}">Log In</a>` : "")
        + `</div><p class="section-footer">Saved on this device. Sent automatically when you're online with the app open.</p>`;
    });
  }
  document.addEventListener("click", async (e) => {
    const d = e.target.closest("[data-discard]");
    if (d && confirm("Discard this edit? It hasn't been saved to GitHub.")) {
      await outbox.remove(d.dataset.discard);
      renderOutbox();
    }
    if (e.target.closest("[data-send-now]")) {
      if (!navigator.onLine) return toast("You're Offline");
      flush(true);
    }
  });
  renderOutbox().then(() => flush());
  addEventListener("online", () => flush());
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") flush(); });

  // ---------------------------------------------------------------- edit forms
  // Sent with fetch so a failed save keeps what was typed. No connection (or
  // GitHub unreachable): kept in the outbox and sent later.
  $$("form[data-edit]").forEach((form) => {
    const errBox = $("[data-form-error]");
    const showError = (msg) => {
      if (!errBox) return alert(msg);
      errBox.textContent = msg;
      errBox.hidden = false;
      errBox.scrollIntoView({ block: "center", behavior: "smooth" });
    };
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const ask = (ev.submitter && ev.submitter.dataset.confirm) || form.dataset.confirmSubmit;
      if (ask && !confirm(ask)) return;
      const btns = $$("button[type=submit], button:not([type])", form);
      if (btns.some((b) => b.disabled)) return;
      const item = formItem(form, ev.submitter);
      const label = ev.submitter && ev.submitter.textContent;
      btns.forEach((b) => { b.disabled = true; });
      if (ev.submitter) ev.submitter.textContent = "Saving…";
      const restore = () => { btns.forEach((b) => { b.disabled = false; }); if (ev.submitter) ev.submitter.textContent = label; };
      const queue = async (why) => {
        try { await outbox.add({ ...item, error: why || "", retry: true }); }
        catch { restore(); return showError("Couldn't save on this device either. Try again when you're online."); }
        location.href = withMsg(item.done, why ? "Saved on this device. It will be sent later." : "Saved on this device. It will be sent when you're online.");
      };
      if (!navigator.onLine) return queue();
      let r;
      try { r = await send(item); } catch { return queue(); }
      if (r.ok) { location.href = withMsg(r.url || item.done, r.message || "Saved"); return; }
      if (r.auth) { restore(); return showError("You've been logged out. Log in again in another tab, then Save."); }
      if (r.retry) return queue(r.error);
      restore();
      showError(r.error || `Couldn't save (error ${r.status}).`);
      const force = $("[data-force]", form);
      if (force && r.status === 409) force.hidden = false;
    });
  });

  // Rows that can be added and removed (details, parts).
  document.addEventListener("click", (e) => {
    const add = e.target.closest("[data-add-row]");
    if (add) {
      const tpl = $(`template[data-row-template="${add.dataset.addRow}"]`);
      const rows = $(`[data-rows="${add.dataset.addRow}"]`);
      if (tpl && rows) {
        rows.appendChild(tpl.content.cloneNode(true));
        const input = $("[data-row]:last-child input", rows);
        if (input) input.focus();
      }
    }
    const rm = e.target.closest("[data-remove-row]");
    if (rm) rm.closest("[data-row]").remove();
  });

  // A saved copy of a form may be days old: default to today's date here.
  $$("input[data-today]").forEach((i) => {
    const d = new Date();
    i.value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  });

  // Add Files: help text follows the chosen type; the button names the files.
  $$("[data-kind-help]").forEach((help) => {
    const sync = () => {
      const kind = ($("input[name=kind]:checked") || {}).value || "photo";
      $$("[data-for]", help).forEach((s) => { s.hidden = s.dataset.for !== kind; });
      $$("[data-for-kind]").forEach((r) => { r.hidden = r.dataset.forKind !== kind; });
    };
    $$("input[name=kind]").forEach((r) => r.addEventListener("change", sync));
    sync();
  });
  $$("[data-file-input]").forEach((input) => input.addEventListener("change", () => {
    const label = $("[data-file-label]");
    const n = input.files.length;
    if (label) label.textContent = n === 1 ? input.files[0].name : n ? `${n} files chosen` : "Choose Photos or Files";
  }));

  // ---------------------------------------------------------------- offline files
  $$("[data-file-status]").forEach(async (el) => {
    const url = el.dataset.fileStatus;
    const check = async () => {
      const r = await swCall({ type: "status", urls: [url] });
      if (!r) return;
      el.hidden = false;
      el.textContent = r[url] ? "Saved on this device" : navigator.onLine ? "Saving for offline use…" : "Not saved on this device";
      return r[url];
    };
    if (!(await check()) && navigator.onLine) setTimeout(check, 4000);
  });

  const usage = $("[data-offline-usage]");
  if (usage) {
    const fmt = (b) => (b > 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b > 1e6 ? `${Math.round(b / 1e6)} MB` : `${Math.round(b / 1e3)} KB`);
    const refreshUsage = async () => {
      if (!sw) {
        const note = $("[data-needs-https]");
        if (note && !isSecureContext) note.hidden = false; usage.textContent = "Needs HTTPS"; $$("[data-save-all], [data-clear-offline]").forEach((b) => { b.disabled = true; }); return; }
      if (navigator.storage && navigator.storage.estimate) {
        try { usage.textContent = fmt((await navigator.storage.estimate()).usage || 0); } catch {}
      }
      const s = await swCall({ type: "summary" });
      const size = $("[data-save-all-size]");
      if (s && size) size.textContent = s.missing ? `${s.missing} · ${fmt(s.missingBytes)}` : "All saved";
    };
    refreshUsage();
    const all = $("[data-save-all]");
    if (all) all.addEventListener("click", async () => {
      if (!navigator.onLine) return toast("You're Offline");
      const size = $("[data-save-all-size]");
      all.disabled = true;
      await swCall({ type: "saveAll" }, (p) => { if (size && p) size.textContent = `${p.progress} of ${p.total}`; });
      all.disabled = false;
      toast("Saved for Offline");
      refreshUsage();
    });
    const clr = $("[data-clear-offline]");
    if (clr) clr.addEventListener("click", async () => {
      if (!confirm("Remove saved manuals, receipts and 3D files from this device? Pages and photos stay.")) return;
      await swCall({ type: "clearFiles" });
      toast("Saved Files Removed");
      refreshUsage();
    });
  }

  // Logging out removes everything this account saved on the device.
  $$('form[action="/logout"]').forEach((f) => f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const waiting = (await outbox.all()).length;
    if (waiting && !confirm(`${waiting} edit${waiting === 1 ? " hasn't" : "s haven't"} been sent yet. Log out and discard ${waiting === 1 ? "it" : "them"}?`)) return;
    await Promise.race([Promise.all([swCall({ type: "clear" }), outbox.clear().catch(() => {})]),
                        new Promise((r) => setTimeout(r, 1500))]);
    f.submit();
  }));
})();
