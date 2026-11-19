// More end-to-end checks with real Chromium + the extension + the real app:
// many downloads at once, tiny files, the extension being restarted by the browser mid-wait.
//   APP_PY=python3 xvfb-run -a node tests/e2e/extension_more.e2e.js
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
const WORK = fs.mkdtempSync(path.join(os.tmpdir(), "flowdl-e2e2-"));
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
async function waitFor(fn, ms = 15000, step = 250) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await fn()) return true; await sleep(step); }
  return false;
}

// Files: "big" ones arrive at ~2 MB/s (so the app takes them over), "tiny" ones at once.
const FILES = {};
for (let i = 0; i < 8; i++) FILES[`multi${i}.zip`] = crypto.randomBytes(2_500_000);
FILES["tiny.zip"] = crypto.randomBytes(3000);
FILES["restart.zip"] = crypto.randomBytes(3_000_000);
FILES["kept.zip"] = crypto.randomBytes(2_500_000);
let slowStart = false; // the restart test: the site answers the app slowly at first
const site = http.createServer((req, res) => {
  if (req.url === "/") {
    res.writeHead(200, { "Content-Type": "text/html" });
    return res.end(Object.keys(FILES).map((n) => `<a id="${n.replace(".zip", "")}" href="/f/${n}">${n}</a>`).join(" "));
  }
  const name = decodeURIComponent(req.url.split("/").pop());
  const body = FILES[name];
  if (!body) { res.writeHead(404); return res.end(); }
  const m = /bytes=(\d+)-(\d*)/.exec(req.headers.range || "");
  const a = m ? +m[1] : 0, b = m && m[2] ? Math.min(+m[2], body.length - 1) : body.length - 1;
  res.writeHead(m ? 206 : 200, { "Content-Type": "application/zip", "Content-Length": b - a + 1, "Accept-Ranges": "bytes",
    "ETag": '"' + name + '"', ...(m ? { "Content-Range": `bytes ${a}-${b}/${body.length}` } : {}) });
  const chunk = body.subarray(a, b + 1);
  if (name === "tiny.zip") return res.end(chunk);
  let i = 0;
  const appAsks = !!m; // the app asks with Range; the browser's first request does not
  const delay = name === "restart.zip" && appAsks && slowStart ? 6000 : 0;
  const tick = () => {
    if (res.destroyed) return;
    if (i >= chunk.length) return res.end();
    res.write(chunk.subarray(i, i + 65536)); i += 65536; setTimeout(tick, 30);
  };
  setTimeout(tick, delay);
});

let app = null;
function ping() {
  return new Promise((resolve) => {
    const r = http.get(APP + "/ping", { headers: { "X-FlowDL": "1" }, timeout: 800 }, (res) => { res.resume(); resolve(res.statusCode === 200); });
    r.on("error", () => resolve(false)); r.on("timeout", () => { r.destroy(); resolve(false); });
  });
}
async function startApp(extra = {}) {
  fs.mkdirSync(path.join(CFG, "FlowDownloads"), { recursive: true });
  fs.writeFileSync(path.join(CFG, "FlowDownloads", "settings.json"),
                   JSON.stringify({ dest_dir: DL, background_hint_shown: true, show_progress_window: false, max_active: 3,
                                    category_folders: false, ...extra }));
  app = spawn(APP_PY, [path.join(ROOT, "Flow Downloads.pyw"), "--background"],
              { env: { ...process.env, XDG_CONFIG_HOME: CFG, APPDATA: CFG }, stdio: "inherit" });
  return waitFor(ping, 10000);
}
function appTasks() {
  try { return JSON.parse(fs.readFileSync(path.join(CFG, "FlowDownloads", "downloads.json"))).tasks; } catch { return []; }
}
const leftovers = () => (fs.existsSync(DL) ? fs.readdirSync(DL, { recursive: true }).filter((f) => /\.part(\.json)?$/.test(String(f))) : []);

(async () => {
  await new Promise((r) => site.listen(8772, "127.0.0.1", r));
  const SITE = "http://127.0.0.1:8772";
  check("App arranca", await startApp());
  const ctx = await chromium.launchPersistentContext(path.join(WORK, "profile"), {
    headless: false, acceptDownloads: true, downloadsPath: BROWSER_DL,
    args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`],
  });
  let [sw] = ctx.serviceWorkers();
  if (!sw) sw = await ctx.waitForEvent("serviceworker");
  const page = await ctx.newPage();
  await page.goto(SITE + "/");
  const all = () => sw.evaluate(() => new Promise((r) => chrome.downloads.search({}, r)));
  const same = (file, name) => fs.existsSync(file) && fs.readFileSync(file).equals(FILES[name]);
  // (Under test, the browser saves downloads under random names: files are matched by content.)
  const holds = (d, name) => d.state === "complete" && d.filename && same(d.filename, name);

  // 1. Eight downloads at once (more than the browser's 6 connections to the app; more than
  //    the app's 3 simultaneous downloads): all must end complete and intact, once each.
  for (let i = 0; i < 8; i++) await page.click(`#multi${i}`);
  const doneAll = await waitFor(async () => {
    const l = await all();
    return Object.keys(FILES).filter((n) => n.startsWith("multi")).every((n) => l.some((d) => holds(d, n)));
  }, 90000, 500);
  const l1 = await all();
  check("8 downloads ao mesmo tempo: todos concluídos e intactos no navegador", doneAll,
        l1.map((d) => `${path.basename(d.filename || "?")}:${d.state}`).join(" "));
  const copies = Object.keys(FILES).filter((n) => n.startsWith("multi")).map((n) => l1.filter((d) => holds(d, n)).length);
  check("…sem ficheiros repetidos (cada um uma vez)", copies.every((c) => c === 1), copies.join(","));
  check("…o navegador não ficou com nenhum download parado ou falhado",
        !l1.some((d) => d.state === "interrupted" || (d.state === "in_progress" && d.paused)),
        l1.filter((d) => d.state !== "complete").map((d) => `${d.url}:${d.state}:${d.error || ""}`).join(" "));
  await waitFor(() => leftovers().length === 0, 15000);
  check("…e a app não deixou cópias temporárias", leftovers().length === 0, leftovers().join(","));
  const tasks1 = appTasks().filter((t) => (t.url || "").includes("/multi"));
  check("…cada download aparece uma vez na lista da app", tasks1.length === 8, `${tasks1.length} na lista`);

  // 2. A tiny file: the browser has it before the app could; it stays the browser's, intact.
  await page.click("#tiny");
  let tiny = null;
  await waitFor(async () => { tiny = (await all()).find((d) => holds(d, "tiny.zip")); return !!tiny; }, 20000);
  check("Ficheiro minúsculo: fica completo e intacto", !!tiny, tiny ? `${tiny.state} ${tiny.error || ""}` : "nenhum");
  await sleep(1500);
  const tinyCount = (await all()).filter((d) => holds(d, "tiny.zip") || (d.url || "").includes("tiny")).length;
  check("…e só uma vez", tinyCount === 1, `${tinyCount} downloads`);

  // 3. The browser restarts the extension's background work while it waits for the app (the
  //    site answers the app slowly): the browser's download must not stay paused forever.
  slowStart = true;
  await page.click("#restart");
  await sleep(1200); // the extension is now waiting for the app's "started"
  const extHost = new URL(sw.url()).host;
  // What the browser does to an idle extension: stop its background work (DevTools protocol).
  const cdp = await ctx.newCDPSession(page);
  await cdp.send("ServiceWorker.enable");
  await cdp.send("ServiceWorker.stopAllWorkers");
  await sleep(1000);
  // Checked on disk, without touching the extension (opening one of its pages would wake it).
  const onDisk = () => (fs.existsSync(BROWSER_DL) ? fs.readdirSync(BROWSER_DL) : [])
    .some((f) => same(path.join(BROWSER_DL, f), "restart.zip"));
  const restartOk = await waitFor(onDisk, 90000, 1000);
  check("O navegador pára a extensão a meio: o download termina intacto (não fica parado)", restartOk,
        restartOk ? "" : "o ficheiro não chegou à pasta do navegador em 90 s");

  // 4. "Show downloads in the browser's list" off: the app keeps the file (one copy on disk),
  //    and the browser's own download is dropped, not completed a second time.
  slowStart = false;
  if (app) app.kill();
  spawnSync("pkill", ["-f", "Flow Downloads.pyw"]);
  await waitFor(async () => !(await ping()), 10000);
  check("App reinicia com a opção desligada", await startApp({ browser_list: false }));
  await page.click("#kept");
  const keptFile = path.join(DL, "kept.zip");
  const keptOk = await waitFor(() => fs.existsSync(keptFile) && fs.readFileSync(keptFile).equals(FILES["kept.zip"]), 60000, 500);
  check("Lista do navegador desligada: a app guarda o ficheiro, intacto", keptOk);
  await sleep(1500);
  const inBrowser = (await all()).filter((d) => d.state === "complete" && d.filename && same(d.filename, "kept.zip"));
  check("…e o navegador não grava uma segunda cópia", inBrowser.length === 0, `${inBrowser.length} cópias no navegador`);
  const stuck = (await all()).filter((d) => (d.url || "").includes("kept") && d.state === "in_progress");
  check("…nem fica com um download parado", stuck.length === 0, stuck.map((d) => `${d.state}:${d.paused}`).join(","));

  await ctx.close();
  if (app) app.kill();
  spawnSync("pkill", ["-f", "Flow Downloads.pyw"]);
  site.close();
  try { fs.rmSync(WORK, { recursive: true, force: true }); } catch {}
  const ok = results.filter(Boolean).length;
  console.log(`\n${ok}/${results.length} verificações passaram`);
  process.exit(ok === results.length ? 0 : 1);
})().catch((e) => { console.error(e); spawnSync("pkill", ["-f", "Flow Downloads.pyw"]); process.exit(1); });
