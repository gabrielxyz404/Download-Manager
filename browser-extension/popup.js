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
  if (!st.app) show("Abra o PyDM no computador para baixar.", "err");
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
    show("✔ Enviado! Escolha a qualidade na janela do PyDM.", "ok");
    $("url").value = "";
  } else {
    show("Não consegui falar com o PyDM. Ele está aberto?", "err");
  }
});

$("capture").addEventListener("change", (e) => send({ type: "toggle", enabled: e.target.checked }));

refresh();
