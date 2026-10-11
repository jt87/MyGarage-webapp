// In-page PDF reader built on PDF.js. Safari on iPhone/iPad shows only the
// first page of a PDF inside an iframe, and opening the raw file from a Home
// Screen app leaves no way back, so manuals render here instead.
// Pages render only near the viewport and are released when far away, which
// keeps long manuals inside iOS's canvas memory limit.
(() => {
  const el = document.getElementById("pdf");
  const bar = document.querySelector("[data-pdf-toolbar]");
  if (!el) return;
  const fail = () => {
    el.innerHTML = `<p>Couldn't display this PDF here. <a href="${el.dataset.src}" target="_blank" rel="noopener">Open it in Safari's viewer</a>.</p>`;
  };
  if (!window.pdfjsLib) return fail();
  pdfjsLib.GlobalWorkerOptions.workerSrc =
    "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";

  const ZOOMS = [1, 1.5, 2, 3];
  const MAX_PIXELS = 12e6; // stay under iOS's per-canvas limit
  let zoom = 0; // index into ZOOMS; 1 = fit width
  let doc, ratio = 1.294; // height/width; updated from page 1
  const pages = [];
  const input = bar.querySelector("[data-page-input]");
  const zoomLabel = bar.querySelector("[data-zoom-label]");

  const fitWidth = () => el.clientWidth;

  function layout(p) {
    const w = fitWidth() * ZOOMS[zoom];
    p.div.style.width = w + "px";
    p.div.style.height = w * (p.ratio || ratio) + "px";
  }

  async function render(p) {
    const w = fitWidth() * ZOOMS[zoom];
    if (p.renderedWidth === w || p.busy) return;
    p.busy = true;
    try {
      p.page = p.page || (await doc.getPage(p.n));
      const base = p.page.getViewport({ scale: 1 });
      p.ratio = base.height / base.width;
      layout(p);
      let dpr = Math.min(window.devicePixelRatio || 1, 2);
      while ((w * dpr) * (w * p.ratio * dpr) > MAX_PIXELS && dpr > 0.75) dpr -= 0.25;
      const vp = p.page.getViewport({ scale: (w / base.width) * dpr });
      const canvas = document.createElement("canvas");
      canvas.width = Math.floor(vp.width);
      canvas.height = Math.floor(vp.height);
      await p.page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
      release(p);
      p.div.appendChild(canvas);
      p.renderedWidth = w;
    } catch (e) {
      console.warn("page", p.n, e);
    } finally {
      p.busy = false;
    }
  }

  function release(p) {
    p.div.querySelectorAll("canvas").forEach((c) => { c.width = c.height = 0; c.remove(); });
    p.renderedWidth = 0;
  }

  const near = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      const p = pages[e.target.dataset.n - 1];
      if (e.isIntersecting) render(p);
      else release(p);
    });
  }, { rootMargin: "150% 0px" });

  const current = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (e.isIntersecting && document.activeElement !== input) input.value = e.target.dataset.n;
    });
  }, { rootMargin: "-45% 0px -45% 0px" });

  function relayout() {
    pages.forEach((p) => { layout(p); p.renderedWidth = 0; });
    zoomLabel.textContent = zoom === 0 ? "Fit" : Math.round(ZOOMS[zoom] * 100) + "%";
    // Re-render what's on screen now; the observer handles the rest.
    pages.forEach((p) => {
      const r = p.div.getBoundingClientRect();
      if (r.bottom > -innerHeight && r.top < innerHeight * 2) render(p);
    });
  }

  pdfjsLib.getDocument({ url: el.dataset.src, disableAutoFetch: true, rangeChunkSize: 262144 })
    .promise.then(async (d) => {
      doc = d;
      const first = await doc.getPage(1);
      const v = first.getViewport({ scale: 1 });
      ratio = v.height / v.width;
      el.innerHTML = "";
      for (let n = 1; n <= doc.numPages; n++) {
        const div = document.createElement("div");
        div.className = "pdf-page";
        div.dataset.n = n;
        div.setAttribute("aria-label", `Page ${n}`);
        const p = { n, div, page: n === 1 ? first : null };
        pages.push(p);
        layout(p);
        el.appendChild(div);
        near.observe(div);
        current.observe(div);
      }
      bar.hidden = false;
      bar.querySelector("[data-page-count]").textContent = doc.numPages;
      input.max = doc.numPages;
      const m = location.hash.match(/page=(\d+)/);
      if (m) goTo(+m[1]);
    })
    .catch((e) => { console.error(e); fail(); });

  function goTo(n) {
    n = Math.max(1, Math.min(pages.length, n | 0));
    pages[n - 1].div.scrollIntoView({ block: "start" });
    history.replaceState(null, "", "#page=" + n);
  }

  input.addEventListener("change", () => goTo(+input.value));
  bar.querySelectorAll("[data-zoom]").forEach((b) => b.addEventListener("click", () => {
    const d = +b.dataset.zoom;
    const keep = +input.value || 1;
    zoom = d === 0 ? 0 : Math.max(0, Math.min(ZOOMS.length - 1, zoom + d));
    relayout();
    goTo(keep);
  }));

  let lastW = el.clientWidth;
  addEventListener("resize", () => {
    clearTimeout(window.__pdfResize);
    window.__pdfResize = setTimeout(() => {
      if (el.clientWidth !== lastW) { lastW = el.clientWidth; relayout(); }
    }, 200);
  });
})();
