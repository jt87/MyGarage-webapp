// Small progressive enhancements; every page works without JS.
(() => {
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  };

  // Always show the latest repo content. Safari restores pages from its
  // back/forward cache, and a Home Screen app can sit in the background for
  // days without reloading, so reload when coming back to a stale page.
  const loadedAt = Date.now();
  const STALE_MS = 60 * 1000;
  addEventListener("pageshow", (e) => {
    if (e.persisted && Date.now() - loadedAt > STALE_MS) location.reload();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && Date.now() - loadedAt > STALE_MS
        && !document.querySelector("dialog[open]") && !document.getElementById("pdf")) {
      location.reload();
    }
  });

  // Photo gallery: swipeable slides, thumbnail strip, tap to enlarge.
  document.querySelectorAll("[data-gallery]").forEach((g) => {
    const slides = g.querySelector(".slides");
    const items = [...slides.children];
    const thumbs = [...g.querySelectorAll(".gallery-strip button")];
    const counter = g.querySelector("[data-current]");
    const show = (i) => {
      thumbs.forEach((t, j) => t.setAttribute("aria-pressed", i === j));
      if (counter) counter.textContent = i + 1;
    };
    thumbs.forEach((t) => t.addEventListener("click", () => {
      const i = +t.dataset.index;
      slides.scrollTo({ left: items[i].offsetLeft - slides.offsetLeft, behavior: "smooth" });
      show(i);
    }));
    let raf;
    slides.addEventListener("scroll", () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => show(Math.round(slides.scrollLeft / slides.clientWidth)));
    }, { passive: true });

    const box = document.querySelector("[data-lightbox]");
    if (box && box.showModal) {
      items.forEach((s) => s.addEventListener("click", () => {
        box.querySelector("img").src = s.dataset.full;
        box.showModal();
      }));
      box.addEventListener("click", (e) => { if (e.target === box || e.target.tagName === "IMG") box.close(); });
    }
  });

  // Client-side filter for long lists.
  document.querySelectorAll("[data-filter]").forEach((input) => {
    const items = document.querySelectorAll(input.dataset.filter);
    input.addEventListener("input", () => {
      const terms = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      items.forEach((el) => {
        const t = el.dataset.text || el.textContent.toLowerCase();
        el.hidden = !terms.every((w) => t.includes(w));
      });
      document.querySelectorAll(".cons-group").forEach((g) => {
        g.hidden = !g.querySelector(".cons:not([hidden])");
      });
    });
  });

  // Shopping-list checkboxes, remembered on this device.
  const KEY = "garage.shopping";
  const checked = new Set(store.get(KEY) || []);
  const boxes = document.querySelectorAll("input[data-key]");
  boxes.forEach((cb) => {
    const row = cb.closest(".cons");
    cb.checked = checked.has(cb.dataset.key);
    row.classList.toggle("checked", cb.checked);
    cb.addEventListener("change", () => {
      cb.checked ? checked.add(cb.dataset.key) : checked.delete(cb.dataset.key);
      row.classList.toggle("checked", cb.checked);
      store.set(KEY, [...checked]);
    });
  });
  const clear = document.querySelector("[data-clear-checks]");
  if (clear) clear.addEventListener("click", () => {
    checked.clear(); store.set(KEY, []);
    boxes.forEach((cb) => { cb.checked = false; cb.closest(".cons").classList.remove("checked"); });
  });

  // "/" focuses search (Mac keyboard, iPad Magic Keyboard).
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/input|textarea|select/i.test(document.activeElement.tagName)) {
      const s = document.querySelector(".search input");
      if (s) { e.preventDefault(); s.focus(); }
    }
  });
})();
