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
  const canReload = () => !$("dialog[open]") && !$("#pdf") && !document.activeElement?.matches("input");
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
  $$("[data-share-file]").forEach((b) => b.addEventListener("click", async () => {
    const url = b.dataset.shareFile;
    try {
      const blob = await (await fetch(url)).blob();
      const file = new File([blob], b.dataset.name, { type: blob.type });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        await navigator.share({ files: [file], title: b.dataset.name });
        return;
      }
    } catch (err) { if (err && err.name === "AbortError") return; }
    location.href = url + "?download";
  }));

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

  // ---------------------------------------------------------------- keyboard (iPad/Mac)
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/input|textarea|select/i.test(document.activeElement.tagName)) {
      const s = $(".sidebar .searchbar input") && innerWidth >= 1024 ? $(".sidebar .searchbar input") : $(".searchbar input");
      if (s) { e.preventDefault(); s.focus(); }
      else location.href = "/search";
    }
  });
})();
