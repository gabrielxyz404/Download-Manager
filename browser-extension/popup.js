const $ = (id) => document.getElementById(id);

function send(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, (res) => resolve(res || {})));
}

function show(text, kind) {
  $("msg").textContent = text;
  $("msg").className = kind || "";
}

async function refresh() {
  const st = await send({ type: "status" });
  $("capture").checked = !!st.enabled;
  $("dot").className = "dot " + (st.app ? "on" : "off");
  $("appState").textContent = st.app ? "aberto" : "fechado";
  if (!st.app) show("Abra o Flow Downloads no computador para baixar.", "err");
  else if (st.extensionOk === false)
    show("⚠ Extensão desatualizada: abra brave://extensions e clique em ↻ (recarregar) no Flow Downloads.", "err");
}

$("videoForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("url").value.trim();
  if (!/^https?:\/\//i.test(url)) {
    show("O link deve começar com http:// ou https://", "err");
    return;
  }
  $("go").disabled = true;
  show("Enviando...");
  const res = await send({ type: "video", url });
  $("go").disabled = false;
  if (res.ok) {
    show("✔ Enviado! Escolha a qualidade na janela do Flow Downloads.", "ok");
    $("url").value = "";
  } else {
    show("Não consegui falar com o Flow Downloads. Ele está aberto?", "err");
  }
});

$("open").addEventListener("click", async () => {
  const res = await send({ type: "show" });
  if (res.ok) window.close();
  else show("O Flow Downloads não está a correr. Abra-o no computador.", "err");
});

$("capture").addEventListener("change", (e) => send({ type: "toggle", enabled: e.target.checked }));

async function lastDownload() {
  const { lastEvent } = await chrome.storage.local.get("lastEvent");
  if (!lastEvent) {
    $("last").textContent = "Último download: a extensão ainda não viu nenhum.";
    return;
  }
  const mins = Math.round((Date.now() - lastEvent.at) / 60000);
  const when = mins < 1 ? "agora mesmo" : `há ${mins} min`;
  $("last").textContent = `Último download (${when}): ${lastEvent.name} — ${lastEvent.text}`;
}

// "Capture on this site": the site of the open tab, added to / removed from the app's skip list.
async function siteSwitch() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  let host = "";
  try { host = new URL(tab && tab.url).hostname.replace(/^www\./, ""); } catch {}
  if (!host || !tab.url.startsWith("http")) return;
  const res = await send({ type: "site", host });
  if (!res.ok) return;
  $("siteName").textContent = host;
  $("site").checked = !res.skipped;
  $("siteRow").hidden = false;
  $("site").addEventListener("change", async (e) => {
    const r = await send({ type: "site", host, skip: !e.target.checked });
    if (r.ok) {
      $("site").checked = !r.skipped;
      show(r.skipped ? `Os downloads de ${host} ficam com o navegador.` : `${host} volta a ser capturado.`, "ok");
    }
  });
}

$("next").addEventListener("click", async () => {
  await send({ type: "bypassNext", on: true });
  show("✔ O próximo download (nos próximos 2 minutos) fica com o navegador.", "ok");
});

refresh();
lastDownload();
siteSwitch();
