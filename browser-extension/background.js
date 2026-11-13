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

let lastFailure = ""; // why the last request to the app failed (shown in the popup)

async function postJson(path, payload, tries = 4) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(APP + path, {
        method: "POST",
        headers: { ...HEADERS, "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      lastFailure = r.ok ? "" : `a app respondeu ${r.status} a ${path}`;
      return r.ok ? await r.json() : null;
    } catch (e) {
      lastFailure = `pedido ${path} falhou: ${e && e.message ? e.message : e}`;
      if (i < tries - 1) await sleep(350);
    }
  }
  return null;
}

// What happened to the last download the extension saw, shown in the popup: when something
// does not work on a user's computer, this says where it stopped.
async function note(name, text) {
  try {
    await chrome.storage.local.set({ lastEvent: { at: Date.now(), name: name || "", text } });
  } catch {}
}
// Addresses may carry old-style (Latin-1) accents such as %E7, which decodeURIComponent
// rejects: never let a name for the popup stop a download from being taken.
function safeDecode(text) {
  try { return decodeURIComponent(text); } catch {}
  try { return unescape(text); } catch {}
  return text;
}
const shortName = (url, name) => (name || safeDecode(url.split("?")[0].split("/").pop() || url)).slice(0, 80);

// ------------------------------------------------------------------ watched requests
// What the browser really sent: its method (a form's POST cannot be repeated by the app) and
// the Cookie header each site actually got. Kept for a minute, in memory only.
const seen = new Map(); // url -> { method, cookie, time }
const SEEN_MS = 60000;

// Headers the app repeats exactly as the browser sent them (protected sites compare them).
const BROWSER_HEADERS = ["accept", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-dest",
                         "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user", "upgrade-insecure-requests", "dnt"];

function remember(details) {
  const header = (name) => (details.requestHeaders || []).find((h) => h.name.toLowerCase() === name);
  const cookie = header("cookie"), ua = header("user-agent"), lang = header("accept-language");
  const hdrs = {};
  for (const h of details.requestHeaders || []) {
    if (BROWSER_HEADERS.includes(h.name.toLowerCase()) && h.value) hdrs[h.name.toLowerCase()] = h.value;
  }
  seen.set(details.url, { method: details.method, cookie: cookie ? cookie.value : null,
                          ua: ua ? ua.value : null, lang: lang ? lang.value : null, hdrs, time: Date.now() });
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
// ``original``: the browser's own download of this file, paused while the app checks that
// the site lets it download (null when there is none, e.g. the right-click menu).
async function sendToApp(url, filename, referrer, finalUrl = "", original = null, received = null) {
  const cookies_by_host = await cookiesByHost([url, finalUrl || url]);
  const watched = seenFor(finalUrl || url) || seenFor(url) || {};
  const res = await postJson("/add", {
    url, final_url: finalUrl && finalUrl !== url ? finalUrl : "", filename, referrer,
    cookies_by_host, mirror: true,
    // the browser's identity, exactly as sent to the site (Cloudflare & co. check it)
    user_agent: watched.ua || navigator.userAgent,
    accept_language: watched.lang || (navigator.languages || []).join(","),
    browser_headers: watched.hdrs || {},
    // what the browser itself is receiving: the app checks it gets the same file
    browser_size: received ? received.size : 0,
    browser_type: received ? received.type : "",
  });
  if (!res) return false;
  if (res.rid) showInBrowser(res.rid, original); // not awaited: may wait for the user's confirmation
  return true;
}

async function dropOriginal(original) {
  if (!original) return;
  try { await chrome.downloads.cancel(original.id); } catch {}
  try { await chrome.downloads.erase({ id: original.id }); } catch {}
}

async function showInBrowser(rid, original = null) {
  for (let i = 0; i < 90; i++) { // up to ~30 min waiting for "Baixar agora" in the app
    chrome.runtime.getPlatformInfo(() => {}); // keeps this service worker awake while we wait
    let st = null;
    try {
      const r = await fetch(`${APP}/request/${rid}`, { headers: HEADERS }); // answers within 20 s
      if (!r.ok) {
        if (original) await giveBack(original, `a app respondeu ${r.status}`);
        return;
      }
      st = await r.json();
    } catch {
      await sleep(1000);
      continue;
    }
    if (st.state === "started") { // the app is receiving the file: the browser shows the app's copy
      await dropOriginal(original);
      const id = await chrome.downloads.download({ url: APP + st.stream, conflictAction: "uniquify" });
      const { mirrors = {} } = await chrome.storage.session.get("mirrors");
      mirrors[id] = rid;
      await chrome.storage.session.set({ mirrors });
      if (original) await note(original.label, "baixado pela app ✔");
      return;
    }
    if (st.state === "failed") { // the site refused the app: the browser carries on by itself
      if (original) await giveBack(original, st.error || "o site recusou");
      return;
    }
    if (st.state !== "waiting") { // cancelled in the app, or "download later" (app only)
      await dropOriginal(original);
      return;
    }
  }
  if (original) await giveBack(original, "a app não começou a tempo");
}

// The app could not download it: the browser's own download continues where it was.
async function giveBack(original, reason) {
  try {
    if (original.paused) await chrome.downloads.resume(original.id);
  } catch {
    passThrough.add(original.url);
    chrome.downloads.download({ url: original.url }); // could not resume: start it again
  }
  await note(original.label, "o Brave baixou sozinho: a app não conseguiu (" + reason + ")");
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

// The right-click entry. Made again each time the extension starts (not only when it is
// installed), so it is there even if the browser lost it, e.g. after the files were updated.
function setupMenu() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "flowdl-download",
      title: "Baixar com Flow Downloads",
      contexts: ["link", "image", "video", "audio"],
    }, () => void chrome.runtime.lastError);
  });
}
setupMenu();
chrome.runtime.onInstalled.addListener(() => {
  setupMenu();
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
  if (await sendToApp(url, "", info.pageUrl || (tab && tab.url) || "")) {
    await note(shortName(url), "enviado à app (botão direito)");
  } else {
    await note(shortName(url), "botão direito: não consegui enviar à app — " + (lastFailure || "app fechada"));
  }
});

chrome.downloads.onCreated.addListener(async (item) => {
  try {
    await handleDownload(item);
  } catch (e) { // never fail silently: say what went wrong (the browser keeps the download)
    await note(shortName(item.finalUrl || item.url || "", ""), "erro na extensão: " + (e && e.message ? e.message : e));
  }
});

async function handleDownload(item) {
  const url = item.url; // the link the download started from
  const finalUrl = item.finalUrl || item.url; // where it led after redirects
  if (finalUrl.startsWith(APP) || url.startsWith(APP)) return; // the browser's copy of an app download
  const label = shortName(finalUrl, item.filename ? item.filename.split(/[\\/]/).pop() : "");
  if (!/^(https?|ftp):/i.test(finalUrl)) {
    return note(label, "deixado ao navegador: o ficheiro foi criado pela própria página (" + finalUrl.split(":")[0] + ":)");
  }
  if (passThrough.delete(url) || passThrough.delete(finalUrl)) return;
  const watched = seenFor(url) || seenFor(finalUrl);
  if (watched && watched.method !== "GET") {
    return note(label, "deixado ao navegador: veio de um formulário (" + watched.method + ")");
  }
  if (!(await isEnabled())) return note(label, "deixado ao navegador: a captura está desligada");
  if (!(await appAlive())) return note(label, "deixado ao navegador: a app não respondeu");
  // Ask before taking it: only the file types / sites chosen in the app (an older app that
  // does not know this question answers nothing useful: then take it, as before).
  const verdict = await postJson("/check", {
    url, final_url: finalUrl, filename: item.filename ? item.filename.split(/[\\/]/).pop() : "",
    mime: item.mime || "", referrer: item.referrer || "",
  }, 2);
  if (verdict && verdict.capture === false) {
    return note(label, `deixado ao navegador: tipo fora da lista da app (${item.mime || "tipo desconhecido"}) ou site ignorado`);
  }
  const checkProblem = verdict ? "" : lastFailure;

  // Pause (not cancel) the browser's download: it is only dropped once the app is really
  // receiving the file. If the site refuses the app (a link valid only once, a login or a
  // protection page...), the browser simply carries on: never worse than without the app.
  const original = { id: item.id, url, label, paused: false };
  try {
    await chrome.downloads.pause(item.id);
    original.paused = true;
  } catch {}

  const name = item.filename ? item.filename.split(/[\\/]/).pop() : "";
  const received = { size: item.totalBytes > 0 ? item.totalBytes : 0, type: item.mime || "" };
  if (!(await sendToApp(url, name, item.referrer || "", finalUrl, original, received))) {
    if (original.paused) {
      try { await chrome.downloads.resume(item.id); } catch {}
    }
    return note(label, "o Brave baixou sozinho: não consegui enviar à app — " +
                (lastFailure || checkProblem || "sem resposta"));
  }
  await note(label, "enviado à app, a confirmar…");
}
