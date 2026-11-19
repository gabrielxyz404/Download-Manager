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
  if (!st.app) show("O Flow Downloads está fechado: clique em \"Abrir Flow Downloads\" (ou ele abre sozinho no próximo download).", "err");
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
  show("A abrir o Flow Downloads...");
  const res = await send({ type: "show" });
  if (res.ok) window.close();
  else show("Não consegui abrir o Flow Downloads. Abra-o pelo menu Iniciar.", "err");
});

$("capture").addEventListener("change", (e) => send({ type: "toggle", enabled: e.target.checked }));

async function lastDownload() {
  const { lastEvent, recent } = await chrome.storage.local.get(["lastEvent", "recent"]);
  const recentEvents = recent && recent.items;
  const when = (at) => {
    const mins = Math.round((Date.now() - at) / 60000);
    return mins < 1 ? "agora mesmo" : `há ${mins} min`;
  };
  if (recentEvents && recentEvents.length) {
    // The last few: a download that "did nothing" can be told apart from the ones after it.
    $("last").textContent = "Últimos downloads:\n" +
      recentEvents.map((e) => `• (${when(e.at)}) ${e.name || "download"} — ${e.text}`).join("\n");
    $("last").style.whiteSpace = "pre-line";
    return;
  }
  if (!lastEvent) {
    $("last").textContent = "Último download: a extensão ainda não viu nenhum.";
    return;
  }
  $("last").textContent = `Último download (${when(lastEvent.at)}): ${lastEvent.name} — ${lastEvent.text}`;
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

// "Nesta página": videos and audio the open page played or loaded.
function sizeText(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}

async function pageMedia() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https?:/.test(tab.url || "")) return;
  const { media = [] } = await send({ type: "pageMedia", tabId: tab.id });
  const list = $("mediaList");
  list.textContent = "";
  const label = { video: "Vídeo", audio: "Áudio", hls: "Transmissão (HLS)", dash: "Transmissão (DASH)" };
  for (const m of media) {
    const row = document.createElement("div");
    row.className = "item";
    const what = document.createElement("div");
    what.className = "what";
    const name = document.createElement("div");
    name.className = "name";
    let file = "";
    try { file = decodeURIComponent(new URL(m.url).pathname.split("/").pop()); } catch {}
    name.textContent = file || m.url;
    name.title = m.url;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = [label[m.kind] || m.kind, sizeText(m.size)].filter(Boolean).join(" · ");
    what.append(name, meta);
    const go = document.createElement("button");
    go.textContent = "⬇ Baixar";
    go.addEventListener("click", async () => {
      go.disabled = true;
      const res = await send({ type: "getMedia", media: m, page: tab.url, title: tab.title });
      show(res.ok ? (m.kind === "video" || m.kind === "audio" ? "✔ A baixar no Flow Downloads." :
                     "✔ Escolha a qualidade na janela do Flow Downloads.") :
           "Não consegui falar com o Flow Downloads.", res.ok ? "ok" : "err");
      go.disabled = false;
    });
    row.append(what, go);
    list.append(row);
  }
  if (!media.length) {
    const none = document.createElement("div");
    none.className = "meta";
    none.textContent = "Nenhum vídeo ou áudio carregado nesta página (ainda). Dê play ao vídeo, ou escolha a qualidade:";
    list.append(none);
  }
  $("pageQuality").onclick = async () => {
    const res = await send({ type: "video", url: tab.url });
    show(res.ok ? "✔ Escolha a qualidade na janela do Flow Downloads." : "Não consegui falar com o Flow Downloads.",
         res.ok ? "ok" : "err");
  };
  $("pageMedia").hidden = false;
}

refresh();
lastDownload();
siteSwitch();
pageMedia();
