// Floating "Baixar este vídeo" button shown over videos (like IDM's download panel).
(() => {
  if (window.__pydmVideoButton) return;
  window.__pydmVideoButton = true;

  const MIN_W = 200, MIN_H = 110;
  let btn = null, current = null, hideTimer = null, lastMove = 0;

  function makeButton() {
    btn = document.createElement("div");
    btn.setAttribute("data-pydm", "1");
    btn.textContent = "⬇ Baixar este vídeo";
    Object.assign(btn.style, {
      position: "fixed", zIndex: "2147483647", display: "none",
      background: "#1976d2", color: "#fff", font: "600 13px/1.2 system-ui, Segoe UI, sans-serif",
      padding: "7px 12px", borderRadius: "6px", cursor: "pointer", userSelect: "none",
      boxShadow: "0 2px 10px rgba(0,0,0,.4)", pointerEvents: "auto",
    });
    btn.addEventListener("mouseenter", () => clearTimeout(hideTimer));
    btn.addEventListener("mouseleave", scheduleHide);
    btn.addEventListener("click", onClick, true);
    (document.fullscreenElement || document.documentElement).appendChild(btn);
  }

  function videoAt(x, y) {
    for (const v of document.querySelectorAll("video")) {
      const r = v.getBoundingClientRect();
      if (r.width >= MIN_W && r.height >= MIN_H &&
          x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return v;
    }
    return null;
  }

  function place(v) {
    if (!btn) makeButton();
    const host = document.fullscreenElement || document.documentElement;
    if (btn.parentNode !== host) host.appendChild(btn); // stay visible in fullscreen
    const r = v.getBoundingClientRect();
    btn.style.display = "block";
    btn.style.top = Math.max(4, r.top + 10) + "px";
    btn.style.left = Math.max(4, r.right - btn.offsetWidth - 10) + "px";
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => { if (btn) btn.style.display = "none"; current = null; }, 2500);
  }

  document.addEventListener("mousemove", (e) => {
    const now = Date.now();
    if (now - lastMove < 120) return;
    lastMove = now;
    if (btn && e.target === btn) return;
    const v = videoAt(e.clientX, e.clientY);
    if (v) {
      current = v;
      clearTimeout(hideTimer);
      place(v);
    } else if (current) {
      scheduleHide();
    }
  }, { passive: true });

  window.addEventListener("scroll", () => { if (current && btn.style.display === "block") place(current); },
                          { passive: true });

  function flash(text, color) {
    btn.textContent = text;
    btn.style.background = color;
    setTimeout(() => { btn.textContent = "⬇ Baixar este vídeo"; btn.style.background = "#1976d2"; }, 3000);
  }

  function onClick(e) {
    e.preventDefault();
    e.stopPropagation();
    const v = current;
    const src = v && (v.currentSrc || v.src) || "";
    chrome.runtime.sendMessage(
      { type: "pydm-video", page: location.href, title: document.title, src },
      (res) => {
        if (chrome.runtime.lastError || !res || !res.ok) {
          flash("⚠ Abra o PyDM primeiro", "#c62828");
        } else {
          flash("✔ Enviado para o PyDM", "#2e7d32");
        }
      });
  }
})();
