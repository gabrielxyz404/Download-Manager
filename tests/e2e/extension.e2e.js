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
const site = http.createServer((req, res) => {
  if (req.url === "/") {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Set-Cookie": "sessao=abc123; Path=/" });
    return res.end('<a id="dl" href="/files/relatorio.zip">relatório</a>' +
                   '<a id="dl2" href="/files/silencioso.zip">2</a><a id="dl3" href="/files/fechado.zip">3</a>' +
                   '<a id="dl4" href="/files/off.zip">4</a>');
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
  if (req.url.startsWith("/files/")) {
    cookiesSeen.push(req.headers.cookie || "");
    if (!(req.headers.cookie || "").includes("sessao=abc123")) { res.writeHead(403); return res.end("login"); }
    const name = req.url.slice(7);
    const m = /bytes=(\d+)-(\d*)/.exec(req.headers.range || "");
    const headers = { "Content-Type": "application/zip", "Accept-Ranges": "bytes",
                      "Content-Disposition": `attachment; filename="${name}"` };
    if (m) {
      const start = +m[1], end = m[2] ? +m[2] : BIG.length - 1;
      res.writeHead(206, { ...headers, "Content-Range": `bytes ${start}-${end}/${BIG.length}`,
                           "Content-Length": end - start + 1 });
      return res.end(BIG.subarray(start, end + 1));
    }
    res.writeHead(200, { ...headers, "Content-Length": BIG.length });
    return res.end(BIG);
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
  await waitFor(async () => !(await ping()), 5000);
}
function windows(name) {
  const r = spawnSync("xdotool", ["search", "--onlyvisible", "--name", name], { encoding: "utf8" });
  return r.stdout.trim().split("\n").filter(Boolean);
}
async function pressIn(name, key) {
  if (!(await waitFor(() => windows(name).length > 0, 15000))) return false;
  const id = windows(name)[0];
  spawnSync("xdotool", ["windowfocus", "--sync", id]);
  spawnSync("xdotool", ["key", key]);
  return true;
}
function fileOk(name) {
  const p = path.join(DL, name);
  return fs.existsSync(p) && fs.readFileSync(p).equals(BIG);
}
const browserDownloads = (sw) => sw.evaluate(() => new Promise((r) => chrome.downloads.search({}, r)));

// ------------------------------------------------------------------ the test
(async () => {
  await new Promise((r) => site.listen(8771, "127.0.0.1", r));
  const SITE = "http://127.0.0.1:8771";
  check("Flow Downloads arranca em segundo plano", await startApp({ confirm_browser: true }));

  const ctx = await chromium.launchPersistentContext(path.join(WORK, "profile"), {
    headless: false, acceptDownloads: true, downloadsPath: BROWSER_DL,
    args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`],
  });
  let [sw] = ctx.serviceWorkers();
  if (!sw) sw = await ctx.waitForEvent("serviceworker");
  const extId = new URL(sw.url()).host;
  const page = await ctx.newPage();
  await page.goto(SITE + "/");
  const popup = async () => {
    const p = await ctx.newPage();
    await p.goto(`chrome-extension://${extId}/popup.html`);
    await p.waitForFunction(() => document.getElementById("appState").textContent !== "verificando...");
    return p;
  };

  // 1. Click a download link: the app takes it over, asks, downloads with the site's cookie.
  await page.click("#dl");
  check("Clique num link abre a janela do Flow Downloads", await pressIn("^Download do navegador$", "Return"));
  check("Ficheiro descarregado pelo Flow Downloads (com o cookie de login)",
        await waitFor(() => fileOk("relatorio.zip"), 20000));
  check("O navegador não ficou com o download", (await browserDownloads(sw)).length === 0);
  check("Cookie de sessão enviado em todos os pedidos", cookiesSeen.every((c) => c.includes("sessao=abc123")),
        `${cookiesSeen.length} pedidos`);

  // 2. Right-click menu "Baixar com Flow Downloads" (same code path the menu calls).
  await sw.evaluate((u) => sendToApp(u, "", "http://127.0.0.1:8771/"), SITE + "/files/menu.zip");
  check("Menu do botão direito envia para a app", await pressIn("^Download do navegador$", "Return"));
  check("Ficheiro do menu descarregado", await waitFor(() => fileOk("menu.zip"), 20000));

  // 3. Popup: paste a video link -> quality window -> download.
  let p = await popup();
  check("Menu da extensão mostra a app aberta", (await p.textContent("#appState")) === "aberto");
  if (clip) {
    await p.fill("#url", SITE + "/video.html");
    await p.click("#go");
    await p.waitForFunction(() => /Enviado|consegui/.test(document.getElementById("msg").textContent));
    check("Baixar vídeo: link enviado", /Enviado/.test(await p.textContent("#msg")));
    const opened = await waitFor(() => windows("^Baixar v").length > 0, 15000);
    check("Janela de qualidade do vídeo abriu", opened);
    await sleep(4000); // let it list the qualities
    await pressIn("^Baixar v", "Return");
    check("Vídeo descarregado", await waitFor(() => fs.existsSync(DL) &&
      fs.readdirSync(DL).some((f) => f.endsWith(".mp4") && fs.statSync(path.join(DL, f)).size > 10000), 40000),
      fs.existsSync(DL) ? fs.readdirSync(DL).join(", ") : "");
  } else {
    console.log("- passo do vídeo ignorado (sem ffmpeg)");
  }

  // 4. Popup: "Abrir Flow Downloads" shows the main window; Ctrl+W hides it again.
  await p.click("#open");
  check("Botão 'Abrir Flow Downloads' mostra a janela", await waitFor(() => windows("^Flow Downloads$").length > 0));
  await pressIn("^Flow Downloads$", "ctrl+w");
  check("Ctrl+W fecha a janela e a app continua", await waitFor(async () =>
    windows("^Flow Downloads$").length === 0 && await ping(), 10000));
  await p.close().catch(() => {});

  // 5. Capture switched off in the popup: the browser downloads by itself.
  p = await popup();
  await p.uncheck("#capture");
  await p.close();
  await page.click("#dl4");
  await sleep(3000);
  const off = await browserDownloads(sw);
  check("Captura desligada: o navegador descarrega sozinho", off.length === 1 && windows("^Download do navegador$").length === 0,
        off.map((d) => d.state).join(","));
  const badge = await sw.evaluate(() => chrome.action.getBadgeText({}));
  check("Ícone mostra OFF", badge === "OFF");
  p = await popup();
  await p.check("#capture");
  await p.close();

  // 6. Without confirmation: downloads silently, no window at all.
  await stopApp();
  await startApp({ confirm_browser: false });
  await page.click("#dl2");
  await sleep(1500);
  check("Sem confirmação: nenhuma janela aparece", windows("^Download do navegador$").length === 0);
  check("Sem confirmação: ficheiro descarregado em segundo plano", await waitFor(() => fileOk("silencioso.zip"), 20000));

  // 7. App closed: the browser handles the download normally.
  await stopApp();
  const before = (await browserDownloads(sw)).length;
  await page.click("#dl3");
  await sleep(3000);
  const after = await browserDownloads(sw);
  check("App fechada: o navegador descarrega normalmente", after.length === before + 1 &&
        after.every((d) => d.state === "complete"));
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
