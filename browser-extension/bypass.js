// Alt+click on a link: this download stays with the browser (the app does not take it).
// Only listens for clicks; does nothing else on the page.
addEventListener("click", (e) => {
  if (!e.altKey) return;
  const link = e.target && e.target.closest ? e.target.closest("a[href]") : null;
  if (link) chrome.runtime.sendMessage({ type: "bypass", url: link.href });
}, true);
