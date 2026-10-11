// Small progressive enhancements; every page works without JS.
(() => {
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  };

  // Photo gallery: thumbnails swap the main image; main image opens a lightbox.
  document.querySelectorAll("[data-gallery]").forEach((g) => {
    const main = g.querySelector(".gallery-main");
    const img = main && main.querySelector("img");
    g.querySelectorAll(".gallery-strip button").forEach((b) => {
      b.addEventListener("click", () => {
        img.src = b.dataset.src;
        img.alt = b.title;
        main.href = main.dataset.full = b.dataset.full;
        g.querySelectorAll(".gallery-strip button").forEach((x) => x.setAttribute("aria-pressed", x === b));
      });
    });
    const box = document.querySelector("[data-lightbox]");
    if (main && box && box.showModal) {
      main.addEventListener("click", (e) => {
        e.preventDefault();
        box.querySelector("img").src = main.dataset.full;
        box.showModal();
      });
      box.addEventListener("click", (e) => { if (e.target === box) box.close(); });
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

  // "/" focuses search.
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/input|textarea|select/i.test(document.activeElement.tagName)) {
      const s = document.querySelector(".search input");
      if (s) { e.preventDefault(); s.focus(); }
    }
  });
})();
