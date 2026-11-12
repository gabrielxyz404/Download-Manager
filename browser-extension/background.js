// PyDM browser integration: hands browser downloads over to the PyDM app,
// which must be running (it listens on 127.0.0.1:17890).
const APP = "http://127.0.0.1:17890";
const HEADERS = { "X-PyDM": "1" };
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

async function appAlive() {
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

async function postApp(path, payload) {
  try {
    const r = await fetch(APP + path, {
      method: "POST",
      headers: { ...HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return r.ok;
  } catch {
    return false;
  }
}

async function sendToApp(url, filename, referrer) {
  let cookies = "";
  try {
    const list = await chrome.cookies.getAll({ url });
    cookies = list.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch {}
  try {
    const r = await fetch(APP + "/add", {
      method: "POST",
      headers: { ...HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify({ url, filename, referrer, cookies }),
    });
    return r.ok;
  } catch {
    return false;
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "pydm-download",
    title: "Baixar com PyDM",
    contexts: ["link", "image", "video", "audio"],
  });
  updateBadge();
});
chrome.runtime.onStartup.addListener(updateBadge);

// Messages from the popup (toolbar button).
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg.type === "status") {
      sendResponse({ enabled: await isEnabled(), app: await appAlive() });
    } else if (msg.type === "toggle") {
      await chrome.storage.local.set({ enabled: !!msg.enabled });
      await updateBadge();
      sendResponse({ ok: true });
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
    console.warn("PyDM não está aberto.");
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
