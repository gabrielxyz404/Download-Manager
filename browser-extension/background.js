// Flow Downloads browser integration: hands browser downloads over to the Flow Downloads app,
// which must be running (it listens on 127.0.0.1:17890).
const APP = "http://127.0.0.1:17890";
const HEADERS = { "X-FlowDL": "1" };
const passThrough = new Set(); // URLs we let the browser download itself

async function isEnabled() {
  const { enabled } = await chrome.storage.local.get({ enabled: true });
  return enabled;
}

async function updateBadge() {
  const on = await isEnabled();
  await chrome.action.setBadgeText({ text: on ? "" : "OFF" });
  await chrome.action.setBadgeBackgroundColor({ color: "#c62828" });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The app restarts itself (in under a second) to free memory; retry briefly so a
// request made at that exact moment is not lost.
async function appAlive(tries = 4) {
  for (let i = 0; i < tries; i++) {
    if (await pingOnce()) return true;
    if (i < tries - 1) await sleep(350);
  }
  return false;
}

async function pingOnce() {
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 800);
    const r = await fetch(APP + "/ping", { headers: HEADERS, signal: ctrl.signal });
    clearTimeout(timer);
    return r.ok;
  } catch {
    return false;
  }
}

async function postApp(path, payload, tries = 4) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(APP + path, {
        method: "POST",
        headers: { ...HEADERS, "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      return r.ok;
    } catch {
      if (i < tries - 1) await sleep(350);
    }
  }
  return false;
}

async function sendToApp(url, filename, referrer) {
  let cookies = "";
  try {
    const list = await chrome.cookies.getAll({ url });
    cookies = list.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch {}
  return postApp("/add", { url, filename, referrer, cookies });
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "flowdl-download",
    title: "Baixar com Flow Downloads",
    contexts: ["link", "image", "video", "audio"],
  });
  updateBadge();
});
chrome.runtime.onStartup.addListener(updateBadge);

// Messages from the popup (toolbar button).
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg.type === "status") {
      sendResponse({ enabled: await isEnabled(), app: await appAlive(2) });
    } else if (msg.type === "toggle") {
      await chrome.storage.local.set({ enabled: !!msg.enabled });
      await updateBadge();
      sendResponse({ ok: true });
    } else if (msg.type === "show") {
      sendResponse({ ok: await postApp("/show", {}) });
    } else if (msg.type === "video") {
      let cookie_jar = [];
      try {
        cookie_jar = await chrome.cookies.getAll({ url: msg.url });
      } catch {}
      sendResponse({ ok: await postApp("/video", { page: msg.url, title: "", media: [], cookie_jar }) });
    }
  })();
  return true; // async response
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.linkUrl || info.srcUrl;
  if (!url) return;
  if (!(await sendToApp(url, "", info.pageUrl || (tab && tab.url) || ""))) {
    console.warn("Flow Downloads não está aberto.");
  }
});

chrome.downloads.onCreated.addListener(async (item) => {
  const url = item.finalUrl || item.url;
  if (!/^(https?|ftp):/i.test(url)) return; // blob:, data: etc. stay in the browser
  if (passThrough.delete(url)) return;
  if (!(await isEnabled())) return;
  if (!(await appAlive())) return; // app closed: let the browser handle it

  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
  } catch {}

  const name = item.filename ? item.filename.split(/[\\/]/).pop() : "";
  if (!(await sendToApp(url, name, item.referrer || ""))) {
    passThrough.add(url);
    chrome.downloads.download({ url }); // fall back to the browser
  }
});
