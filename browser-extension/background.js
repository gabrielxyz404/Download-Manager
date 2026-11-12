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

chrome.action.onClicked.addListener(async () => {
  await chrome.storage.local.set({ enabled: !(await isEnabled()) });
  updateBadge();
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

// ---------------------------------------------------------------- video capture
// Remember video/audio files each tab loads, so the floating button can offer them.
const MEDIA_EXT = /\.(mp4|webm|mkv|mov|m4v|flv|mp3|m4a|ogg|opus|wav)(\?|$)/i;
const SEGMENT = /\.(ts|m4s)(\?|$)|googlevideo\.com|[?&](range|bytestart)=/i;

chrome.webRequest.onHeadersReceived.addListener(
  (d) => {
    if (d.tabId < 0) return;
    const h = {};
    for (const x of d.responseHeaders || []) h[x.name.toLowerCase()] = x.value || "";
    const type = (h["content-type"] || "").split(";")[0].trim().toLowerCase();
    const hls = /mpegurl/.test(type) || /\.m3u8(\?|$)/i.test(d.url);
    const media = /^(video|audio)\//.test(type) || MEDIA_EXT.test(d.url);
    if ((!hls && !media) || SEGMENT.test(d.url)) return;
    let size = 0;
    const m = /\/(\d+)\s*$/.exec(h["content-range"] || "");
    size = m ? +m[1] : +(h["content-length"] || 0);
    if (!hls && size && size < 300 * 1024) return; // ignore tiny clips / sounds
    rememberMedia(d.tabId, { url: d.url, mime: hls ? "application/x-mpegurl" : type, size, hls });
  },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other"] },
  ["responseHeaders"]
);

let mediaQueue = Promise.resolve(); // serialize read-modify-write of session storage
function rememberMedia(tabId, item) {
  mediaQueue = mediaQueue.then(() => storeMedia(tabId, item)).catch(() => {});
}

async function storeMedia(tabId, item) {
  const key = "m:" + tabId;
  const list = (await chrome.storage.session.get(key))[key] || [];
  if (list.some((x) => x.url === item.url)) return;
  list.push(item);
  await chrome.storage.session.set({ [key]: list.slice(-20) });
}

chrome.tabs.onRemoved.addListener((tabId) => chrome.storage.session.remove("m:" + tabId));
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.url) chrome.storage.session.remove("m:" + tabId); // navigated to another page
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== "pydm-video") return;
  (async () => {
    const key = "m:" + (sender.tab ? sender.tab.id : -1);
    const media = [...((await chrome.storage.session.get(key))[key] || [])];
    if (/^https?:/i.test(msg.src || "") && !media.some((x) => x.url === msg.src)) {
      media.unshift({ url: msg.src, mime: "", size: 0, hls: /\.m3u8/i.test(msg.src) });
    }
    let cookie_jar = [];
    try {
      cookie_jar = await chrome.cookies.getAll({ url: msg.page });
    } catch {}
    const ok = await postApp("/video", {
      page: msg.page,
      title: (sender.tab && sender.tab.title) || msg.title || "",
      media,
      cookie_jar,
    });
    sendResponse({ ok });
  })();
  return true; // async response
});
