// End-to-end test: real Chromium + the browser extension + the real Flow Downloads app.
//
// Needs: Node + Playwright (with Chromium), xdotool, a display (run under xvfb-run on Linux),
// optionally ffmpeg + yt-dlp for the video step.
//   APP_PY=python3 xvfb-run -a node tests/e2e/extension.e2e.js
const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const { spawn, spawnSync } = require("child_process");

const ROOT = path.resolve(__dirname, "..", "..");
const EXT = path.join(ROOT, "browser-extension");
const APP_PY = process.env.APP_PY || "python3";
const WORK = fs.mkdtempSync(path.join(os.tmpdir(), "flowdl-e2e-"));
const CFG = path.join(WORK, "cfg");
const DL = path.join(WORK, "dl");
const BROWSER_DL = path.join(WORK, "browser-dl");
const APP = "http://127.0.0.1:17890";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];

function check(name, ok, detail = "") {
  results.push(ok);
  console.log(`${ok ? "✔" : "✘"} ${name}${detail ? " — " + detail : ""}`);
}

// ------------------------------------------------------------------ test web site
const BIG = crypto.randomBytes(3_000_000);
let clip = null;
const clipPath = path.join(WORK, "clip.mp4");
if (spawnSync("ffmpeg", ["-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
  "-f", "lavfi", "-i", "sine", "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
  "-shortest", clipPath]).status === 0) clip = fs.readFileSync(clipPath);

const cookiesSeen = [];
const links = ["relatorio", "lento", "cancelar", "confirmado", "naoquero", "menu", "off", "fechado"]
  .map((n) => `<a id="${n}" href="/${n === "lento" || n === "cancelar" ? "slow" : "files"}/${n}.zip">${n}</a>`).join(" ");
const site = http.createServer((req, res) => {
  if (req.url === "/") {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Set-Cookie": "sessao=abc123; Path=/" });
    return res.end(links);
  }
  if (req.url === "/video.html") {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    return res.end('<html><head><meta charset="utf-8"><title>Clip de teste</title></head>' +
                   '<body><video src="/clip.mp4" controls></video></body></html>');
  }
  if (req.url === "/clip.mp4" && clip) {
    res.writeHead(200, { "Content-Type": "video/mp4", "Content-Length": clip.length });
    return res.end(clip);
  }
  const slow = req.url.startsWith("/slow/");
  if (req.url.startsWith("/files/") || slow) {
    cookiesSeen.push(req.headers.cookie || "");
    if (!(req.headers.cookie || "").includes("sessao=abc123")) { res.writeHead(403); return res.end("login"); }
    const name = req.url.split("/").pop();
    const m = /bytes=(\d+)-(\d*)/.exec(req.headers.range || "");
    const start = m ? +m[1] : 0, end = m && m[2] ? +m[2] : BIG.length - 1;
    const headers = { "Content-Type": "application/zip", "Accept-Ranges": "bytes",
                      "Content-Disposition": `attachment; filename="${name}"`, "Content-Length": end - start + 1 };
    if (m) headers["Content-Range"] = `bytes ${start}-${end}/${BIG.length}`;
    res.writeHead(m ? 206 : 200, headers);
    const body = BIG.subarray(start, end + 1);
    if (!slow) return res.end(body);
    let i = 0; // ~400 KB/s in total, so we can watch / pause / cancel it
    const tick = () => {
      if (res.destroyed) return;
      if (i >= body.length) return res.end();
      res.write(body.subarray(i, i + 16384)); i += 16384; setTimeout(tick, 40);
    };
    return tick();
  }
  res.writeHead(404); res.end();
});

// ------------------------------------------------------------------ helpers
let app = null;
function ping() {
  return new Promise((resolve) => {
    const req = http.get({ host: "127.0.0.1", port: 17890, path: "/ping", headers: { "X-FlowDL": "1" },
                           timeout: 500 }, (r) => { r.resume(); resolve(r.statusCode === 200); });
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}
async function waitFor(fn, ms = 15000, step = 250) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await fn()) return true; await sleep(step); }
  return false;
}
async function startApp(settings) {
  fs.mkdirSync(path.join(CFG, "FlowDownloads"), { recursive: true });
  fs.writeFileSync(path.join(CFG, "FlowDownloads", "settings.json"),
                   JSON.stringify({ dest_dir: DL, background_hint_shown: true, ...settings }));
  app = spawn(APP_PY, [path.join(ROOT, "Flow Downloads.pyw"), "--background"],
              { env: { ...process.env, XDG_CONFIG_HOME: CFG, APPDATA: CFG }, stdio: "inherit" });
  return waitFor(ping, 10000);
}
async function stopApp() {
  if (app) { app.kill(); app = null; }
  spawnSync("pkill", ["-f", "flowdl.service import run"]);
  await waitFor(async () => !(await ping()), 5000);
}
function appTasks() {
  try { return JSON.parse(fs.readFileSync(path.join(CFG, "FlowDownloads", "downloads.json"))).tasks; }
  catch { return []; }
}
function windows(name) {
  const r = spawnSync("xdotool", ["search", "--onlyvisible", "--name", name], { encoding: "utf8" });
  return r.stdout.trim().split("\n").filter(Boolean);
}
async function pressIn(name, key) {
  if (!(await waitFor(() => windows(name).length > 0, 15000))) return false;
  spawnSync("xdotool", ["windowfocus", "--sync", windows(name)[0]]);
  spawnSync("xdotool", ["key", key]);
  return true;
}
const leftovers = () => (fs.existsSync(DL) ? fs.readdirSync(DL).filter((f) => f.endsWith(".part") || f.endsWith(".json")) : []);
const sameAsBig = (file) => fs.existsSync(file) && fs.readFileSync(file).equals(BIG);

(async () => {
  await new Promise((r) => site.listen(8771, "127.0.0.1", r));
  const SITE = "http://127.0.0.1:8771";
  check("Flow Downloads arranca em segundo plano", await startApp({}));

  const ctx = await chromium.launchPersistentContext(path.join(WORK, "profile"), {
    headless: false, acceptDownloads: true, downloadsPath: BROWSER_DL,
    args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`],
  });
  let [sw] = ctx.serviceWorkers();
  if (!sw) sw = await ctx.waitForEvent("serviceworker");
  const extId = new URL(sw.url()).host;
  const page = await ctx.newPage();
  await page.goto(SITE + "/");
  const all = () => sw.evaluate(() => new Promise((r) => chrome.downloads.search({}, r)));
  const mirrored = async (name) => (await all()).find((d) => d.url.startsWith("http://127.0.0.1:17890/stream/") &&
                                                          d.filename.includes(name) || (d.url.startsWith("http://127.0.0.1:17890/stream/") && d.id === mirrored.ids?.[name]));
  const byUrl = async () => (await all()).filter((d) => d.url.startsWith("http://127.0.0.1:17890/stream/"));
  const popup = async () => {
    const p = await ctx.newPage();
    await p.goto(`chrome-extension://${extId}/popup.html`);
    await p.waitForFunction(() => document.getElementById("appState").textContent !== "verificando...");
    return p;
  };
  const newest = async (before) => {
    let item = null;
    await waitFor(async () => { const l = await byUrl(); item = l.find((d) => !before.includes(d.id)); return !!item; }, 15000);
    return item;
  };
  const finished = async (id, ms = 30000) => {
    let it = null;
    await waitFor(async () => { [it] = await sw.evaluate((i) => new Promise((r) => chrome.downloads.search({ id: i }, r)), id); return it && it.state === "complete"; }, ms);
    return it;
  };

  // 1. Click a download link: the app downloads it, and the browser shows it in its list.
  let ids = (await byUrl()).map((d) => d.id);
  await page.click("#relatorio");
  let item = await newest(ids);
  check("O download aparece na lista do navegador", !!item, item ? item.filename : "");
  item = item && await finished(item.id);
  check("No navegador fica 'concluído', com o ficheiro inteiro", !!item && item.state === "complete" && sameAsBig(item.filename),
        item ? `${item.state} ${item.error || ""} ${item.bytesReceived}/${item.totalBytes} igual=${sameAsBig(item.filename)}` : "nenhum");
  check("O download original do site foi substituído pelo da app", (await all()).every((d) => d.url.startsWith(APP)));
  check("Cookie de login enviado ao site", cookiesSeen.every((c) => c.includes("sessao=abc123")), `${cookiesSeen.length} pedidos`);
  await waitFor(() => leftovers().length === 0, 5000);
  check("A app não deixa cópia temporária (o ficheiro fica só na pasta do navegador)", leftovers().length === 0, leftovers().join(","));
  await waitFor(() => appTasks().some((t) => t.path === (item && item.filename)), 5000);
  check("Na lista da app, o download aponta para o ficheiro do navegador", appTasks().some((t) => t.path === (item && item.filename)));

  // 2. A slower download: progress window, pause / resume from the browser's list.
  ids = (await byUrl()).map((d) => d.id);
  await page.click("#lento");
  item = await newest(ids);
  if (false) { console.log("DEBUG all:", JSON.stringify((await all()).map(d=>[d.url.slice(0,60), d.state, d.error])), "tasks:", JSON.stringify(appTasks().map(t=>[t.filename,t.status,t.mirror])), "wins:", spawnSync("xdotool",["search","--onlyvisible","--name","."],{encoding:"utf8"}).stdout.split("\n").filter(Boolean).map(w=>spawnSync("xdotool",["getwindowname",w],{encoding:"utf8"}).stdout.trim())); }
  check("Abre a janela de progresso da app", await waitFor(() => windows("lento.zip").length > 0, 8000));
  await waitFor(async () => { const [i] = await sw.evaluate((x) => new Promise((r) => chrome.downloads.search({ id: x }, r)), item.id); return i.bytesReceived > 300000; }, 15000);
  const [during] = await sw.evaluate((x) => new Promise((r) => chrome.downloads.search({ id: x }, r)), item.id);
  check("O navegador mostra o progresso a avançar", during.state === "in_progress" && during.bytesReceived > 0 && during.totalBytes === BIG.length,
        `${during.bytesReceived}/${during.totalBytes}`);
  await sw.evaluate((x) => chrome.downloads.pause(x), item.id);
  check("Pausar no navegador pausa a app", await waitFor(() => appTasks().some((t) => t.filename === "lento.zip" && t.status === "Pausado"), 8000));
  await sleep(1000);
  await sw.evaluate((x) => chrome.downloads.resume(x), item.id);
  item = await finished(item.id, 40000);
  check("Retomar no navegador continua e termina, ficheiro inteiro", !!item && sameAsBig(item.filename));
  check("A janela de progresso fecha-se sozinha no fim", await waitFor(() => windows("lento.zip").length === 0, 5000));

  // 3. Cancel in the browser: the app cancels too and removes its temporary file.
  ids = (await byUrl()).map((d) => d.id);
  await page.click("#cancelar");
  item = await newest(ids);
  await sleep(1500);
  await sw.evaluate((x) => chrome.downloads.cancel(x), item.id);
  check("Cancelar no navegador cancela na app", await waitFor(() => !appTasks().some((t) => t.filename === "cancelar.zip"), 8000));
  check("…e apaga a cópia temporária", await waitFor(() => leftovers().length === 0, 5000), leftovers().join(","));
  await waitFor(() => windows("cancelar.zip").length === 0, 5000);

  // 4. Right-click menu "Baixar com Flow Downloads" (same code the menu calls).
  ids = (await byUrl()).map((d) => d.id);
  await sw.evaluate((u) => sendToApp(u, "", "http://127.0.0.1:8771/"), SITE + "/files/menu.zip");
  item = await newest(ids);
  const menuFound = !!item;
  item = item && await finished(item.id);
  check("Menu do botão direito: também aparece e termina no navegador", !!item && sameAsBig(item.filename),
        menuFound ? `${item && item.state} ${item && item.error}` : "não apareceu");

  // 5. Popup: video link -> quality window -> download (app only).
  let p = await popup();
  check("Menu da extensão mostra a app aberta", (await p.textContent("#appState")) === "aberto");
  if (clip) {
    await p.fill("#url", SITE + "/video.html");
    await p.click("#go");
    await p.waitForFunction(() => /Enviado|consegui/.test(document.getElementById("msg").textContent));
    check("Baixar vídeo: link enviado", /Enviado/.test(await p.textContent("#msg")));
    check("Janela de qualidade do vídeo abriu", await waitFor(() => windows("^Baixar v").length > 0, 15000));
    await sleep(4000);
    await pressIn("^Baixar v", "Return");
    check("Vídeo descarregado", await waitFor(() => fs.existsSync(DL) &&
      fs.readdirSync(DL).some((f) => f.endsWith(".mp4") && fs.statSync(path.join(DL, f)).size > 10000), 40000));
  }

  // 6. "Abrir Flow Downloads" and Ctrl+W.
  await p.click("#open");
  check("Botão 'Abrir Flow Downloads' mostra a janela", await waitFor(() => windows("^Flow Downloads$").length > 0));
  await pressIn("^Flow Downloads$", "ctrl+w");
  check("Ctrl+W fecha a janela e a app continua", await waitFor(async () =>
    windows("^Flow Downloads$").length === 0 && await ping(), 10000));
  await p.close().catch(() => {});

  // 7. Capture switched off: the browser downloads by itself.
  p = await popup();
  await p.uncheck("#capture");
  await p.close();
  const n0 = (await all()).length;
  await page.click("#off");
  await sleep(3000);
  const offItem = (await all()).find((d) => d.url.endsWith("/off.zip"));
  check("Captura desligada: o navegador descarrega sozinho", (await all()).length === n0 + 1 && !!offItem &&
        offItem.state === "complete" && windows("off.zip").length === 0);
  check("Ícone mostra OFF", (await sw.evaluate(() => chrome.action.getBadgeText({}))) === "OFF");
  p = await popup();
  await p.check("#capture");
  await p.close();

  // 8. With confirmation turned on: "Baixar agora" -> shown in the browser; Esc -> nothing.
  await stopApp();
  await startApp({ confirm_browser: true });
  ids = (await byUrl()).map((d) => d.id);
  await page.click("#confirmado");
  check("Com confirmação: abre a janela de confirmação", await pressIn("^Download do navegador$", "Return"));
  item = await newest(ids);
  item = item && await finished(item.id);
  check("…e depois de confirmar aparece e termina no navegador", !!item && sameAsBig(item.filename));
  const n1 = (await all()).length;
  await page.click("#naoquero");
  await pressIn("^Download do navegador$", "Escape");
  await sleep(2500);
  check("Cancelar na confirmação: nada fica no navegador", (await all()).length === n1);

  // 9. App closed: the browser handles the download normally.
  await stopApp();
  const before = (await all()).length;
  await page.click("#fechado");
  await sleep(3000);
  const after = await all();
  const closedItem = after.find((d) => d.url.endsWith("/fechado.zip"));
  check("App fechada: o navegador descarrega normalmente", after.length === before + 1 && !!closedItem &&
        closedItem.state === "complete");
  p = await popup();
  check("Menu da extensão mostra a app fechada", (await p.textContent("#appState")) === "fechado");

  await ctx.close();
  site.close();
  const failed = results.filter((r) => !r).length;
  console.log(`\n${results.length - failed}/${results.length} verificações passaram`);
  process.exit(failed ? 1 : 0);
})().catch(async (e) => {
  console.error("ERRO", e);
  await stopApp();
  process.exit(1);
});
