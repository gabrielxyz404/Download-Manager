// Flow Downloads browser integration: hands browser downloads over to the Flow Downloads app,
// which must be running (it listens on 127.0.0.1:17890).
const APP = "http://127.0.0.1:17890";
// Every request says which extension version is talking, so the app can ask for an update.
const HEADERS = { "X-FlowDL": "1", "X-FlowDL-Ext": chrome.runtime.getManifest().version };
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

async function postJson(path, payload, tries = 4) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(APP + path, {
        method: "POST",
        headers: { ...HEADERS, "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      return r.ok ? await r.json() : null;
    } catch {
      if (i < tries - 1) await sleep(350);
    }
  }
  return null;
}

// ------------------------------------------------------------------ watched requests
// What the browser really sent: its method (a form's POST cannot be repeated by the app) and
// the Cookie header each site actually got. Kept for a minute, in memory only.
const seen = new Map(); // url -> { method, cookie, time }
const SEEN_MS = 60000;

function remember(details) {
  const cookie = (details.requestHeaders || []).find((h) => h.name.toLowerCase() === "cookie");
  seen.set(details.url, { method: details.method, cookie: cookie ? cookie.value : null, time: Date.now() });
  if (seen.size > 300) {
    const old = Date.now() - SEEN_MS;
    for (const [k, v] of seen) if (v.time < old || seen.size > 300) seen.delete(k);
  }
}

chrome.webRequest.onSendHeaders.addListener(
  remember,
  { urls: ["<all_urls>"], types: ["main_frame", "sub_frame", "other", "xmlhttprequest"] },
  ["requestHeaders", "extraHeaders"] // extraHeaders: needed to see the Cookie header
);

function seenFor(url) {
  const r = seen.get(url);
  return r && Date.now() - r.time < SEEN_MS ? r : null;
}

const hostOf = (url) => { try { return new URL(url).hostname.toLowerCase(); } catch { return ""; } };

// The Cookie header for each site involved: the link's site and the site it led to.
async function cookiesByHost(urls) {
  const out = {};
  for (const u of urls) {
    const host = hostOf(u);
    if (!host || out[host]) continue;
    const r = seenFor(u);
    if (r && r.cookie !== null) {
      out[host] = r.cookie; // exactly what the browser sent
      continue;
    }
    try { // not watched (e.g. the extension was asleep): ask the browser's cookie store
      const list = await chrome.cookies.getAll({ url: u });
      if (list.length) out[host] = list.map((c) => `${c.name}=${c.value}`).join("; ");
    } catch {}
  }
  return out;
}

// Hand a download to the app. It downloads with many connections; the browser then shows
// the same download in its list (it receives the bytes from the app as they arrive).
// ``url`` is the link the download started from; ``finalUrl`` where it led (often a
// short-lived signed address): the app uses the latter, and the link to get a fresh one.
async function sendToApp(url, filename, referrer, finalUrl = "") {
  const cookies_by_host = await cookiesByHost([url, finalUrl || url]);
  const res = await postJson("/add", {
    url, final_url: finalUrl && finalUrl !== url ? finalUrl : "", filename, referrer,
    cookies_by_host, mirror: true,
  });
  if (!res) return false;
  if (res.rid) showInBrowser(res.rid); // not awaited: may wait for the user's confirmation
  return true;
}

async function showInBrowser(rid) {
  for (let i = 0; i < 90; i++) { // up to ~30 min waiting for "Baixar agora" in the app
    chrome.runtime.getPlatformInfo(() => {}); // keeps this service worker awake while we wait
    let st = null;
    try {
      const r = await fetch(`${APP}/request/${rid}`, { headers: HEADERS }); // answers within 20 s
      if (!r.ok) return;
      st = await r.json();
    } catch {
      await sleep(1000);
      continue;
    }
    if (st.state === "started") {
      const id = await chrome.downloads.download({ url: APP + st.stream, conflictAction: "uniquify" });
      const { mirrors = {} } = await chrome.storage.session.get("mirrors");
      mirrors[id] = rid;
      await chrome.storage.session.set({ mirrors });
      return;
    }
    if (st.state !== "waiting") return; // cancelled, or "download later" (app only)
  }
}

async function mirrorOf(downloadId) {
  const { mirrors = {} } = await chrome.storage.session.get("mirrors");
  return mirrors[downloadId];
}

async function forgetMirror(downloadId) {
  const { mirrors = {} } = await chrome.storage.session.get("mirrors");
  delete mirrors[downloadId];
  await chrome.storage.session.set({ mirrors });
}

// The browser's buttons control the app's download: pause, resume, cancel; when the
// browser has saved the file, the app removes its temporary copy.
chrome.downloads.onChanged.addListener(async (delta) => {
  const rid = await mirrorOf(delta.id);
  if (!rid) return;
  if (delta.state && delta.state.current === "complete") {
    const [item] = await chrome.downloads.search({ id: delta.id });
    await postApp("/mirror-done", { rid, path: item ? item.filename : "" });
    await forgetMirror(delta.id);
  } else if (delta.error && delta.error.current === "USER_CANCELED") {
    await postApp("/control", { rid, action: "cancel" });
    await forgetMirror(delta.id);
  } else if (delta.paused) {
    await postApp("/control", { rid, action: delta.paused.current ? "pause" : "resume" });
  }
});

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
      const app = await appAlive(2);
      let extensionOk = true;
      if (app) {
        try {
          const r = await fetch(APP + "/ping", { headers: HEADERS });
          extensionOk = (await r.json()).extension_ok !== false;
        } catch {}
      }
      sendResponse({ enabled: await isEnabled(), app, extensionOk });
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
  const url = item.url; // the link the download started from
  const finalUrl = item.finalUrl || item.url; // where it led after redirects
  if (finalUrl.startsWith(APP) || url.startsWith(APP)) return; // the browser's copy of an app download
  if (!/^(https?|ftp):/i.test(finalUrl)) return; // blob:, data: etc. stay in the browser
  if (passThrough.delete(url) || passThrough.delete(finalUrl)) return;
  const watched = seenFor(url) || seenFor(finalUrl);
  if (watched && watched.method !== "GET") return; // a form's POST: only the browser can repeat it
  if (!(await isEnabled())) return;
  if (!(await appAlive())) return; // app closed: let the browser handle it
  // Ask before taking it: only the file types / sites chosen in the app (an older app that
  // does not know this question answers nothing useful: then take it, as before).
  const verdict = await postJson("/check", {
    url, final_url: finalUrl, filename: item.filename ? item.filename.split(/[\\/]/).pop() : "",
    mime: item.mime || "", referrer: item.referrer || "",
  }, 2);
  if (verdict && verdict.capture === false) return; // the browser simply carries on

  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
  } catch {}

  const name = item.filename ? item.filename.split(/[\\/]/).pop() : "";
  if (!(await sendToApp(url, name, item.referrer || "", finalUrl))) {
    passThrough.add(url);
    chrome.downloads.download({ url }); // fall back to the browser
  }
});
