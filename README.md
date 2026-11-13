<p align="center"><img src="assets/logo.png" width="160" alt="Flow Downloads"></p>

<h1 align="center">Flow Downloads</h1>

<p align="center">Gerenciador de downloads gratuito, rápido e leve — no estilo do IDM.</p>

Feito em Python, **sem nenhuma dependência para instalar** — só o Python.


**Baixar:** a versão mais recente está sempre em
[Releases](https://github.com/gabrielxyz404/Download-Manager/releases/latest) (`FlowDownloads-Setup.exe`,
com o SHA-256 em `SHA256SUMS.txt`). A app procura versões novas em **⚙ → Procurar atualizações**.

## Leve em segundo plano

O Flow Downloads foi feito para não pesar no computador:

| Situação | RAM | CPU |
|---|---|---|
| Em segundo plano, à espera do navegador | ~13 MB | 0% (não acorda nunca) |
| A baixar em segundo plano (8 conexões) | ~22 MB | < 1% |
| Janela aberta | ~38 MB | quase 0% parada |
| Depois de fechar a janela | volta a ~13 MB | 0% |

Como consegue isto:
- Em segundo plano **nada fica a verificar coisas de tempo a tempo**: o programa dorme até o navegador mandar um download.
- A janela só existe enquanto está a ser usada. Ao fechá-la (X ou **Ctrl+W**), o Flow Downloads continua
  em segundo plano e devolve ao sistema a memória da janela. Para sair de vez: **⚙ → Sair** (**Ctrl+Q**).
- Quando o navegador manda um download, aparece **só a janelinha de confirmação**, não o programa todo.
- O motor de download e o suporte a vídeos só são carregados quando são precisos.
- Abrir o Flow Downloads de novo (ou clicar em **Abrir Flow Downloads** na extensão) mostra a janela que já está a correr.
- **⚙ → Configurações → Sistema → Iniciar com o computador** deixa-o pronto em segundo plano sempre que liga o PC.

## Funcionalidades

| Recurso do IDM | Flow Downloads |
|---|---|
| Aceleração de download (várias conexões) | ✅ até 32 conexões por arquivo, com teste de velocidade do servidor |
| Segmentação dinâmica | ✅ quando uma conexão termina, ela divide ao meio o maior pedaço restante |
| Retomar downloads | ✅ pausa/retoma e continua de onde parou; se a internet cair reconecta sozinho; ao reabrir o Flow Downloads (depois de fechar, reiniciar o PC ou faltar luz) os downloads que estavam a correr recomeçam sozinhos |
| Captura de vídeos e áudios | ✅ cole o link no botão do Flow Downloads na barra do navegador e escolha a resolução (1080p, 720p...) ou só o áudio em MP3 — sem botões por cima dos vídeos |
| Integração com o navegador | ✅ extensão para navegadores Chromium (Brave, Chrome, Edge, Opera); também clique-direito → "Baixar com Flow Downloads" |
| Download com um clique | ✅ ao clicar num link de download no navegador, o Flow Downloads assume |
| Download também visível no navegador | ✅ a transferência aparece na lista de downloads do navegador (com progresso), abre-se uma janelinha de progresso da app, e no fim o navegador mostra o download concluído, como sem a app |
| Limitador de velocidade | ✅ limite total e por download |
| Agendador | ✅ iniciar/parar em horários e dias definidos; ao terminar, fechar o app ou desligar o PC |
| Captura de sites (grabber) | ✅ lista vídeos, músicas, documentos etc. de uma página (e de páginas ligadas a ela) |
| Categorias | ✅ Compactados, Documentos, Músicas, Programas, Vídeos, Imagens — cada uma com a sua pasta (`Downloads\Vídeos`...) e regras por tipo, endereço ou nome (**⚙ → Configurações → Downloads**) |
| Tipos apanhados do navegador | ✅ como no IDM: só os tipos escolhidos (ou todos), e sites a ignorar (**⚙ → Configurações → Navegador**) |
| Download repetido | ✅ ao adicionar um link que já está na lista: abrir / retomar o existente, ou baixar de novo |
| Claro / escuro | ✅ preto e branco; botão ◐ troca num clique; **Automático** segue o Windows (**⚙ → Configurações → Aparência**) |
| Interface personalizável | ✅ painel de detalhes com o **mapa das partes**, filtros na barra lateral ou em botões, colunas à escolha (**⚙ → Colunas**) |
| Fila de downloads | ✅ número de downloads simultâneos configurável |
| Capturar links copiados | ✅ link copiado aparece automaticamente na barra de URL |
| HTTP / HTTPS / FTP | ✅ HTTP e HTTPS com várias conexões; FTP básico (1 conexão) |
| Alt+clique | ✅ segurar **Alt** ao clicar num link deixa esse download com o navegador; no menu da extensão: "Capturar neste site" e "Próximo download fica no navegador" |
| Abrir a app pelo navegador | ✅ se o Flow Downloads estiver fechado, o navegador abre-o sozinho no próximo download |
| Checksum | ✅ campo "Checksum" ao adicionar (MD5/SHA-1/SHA-256/SHA-512) e "Verificar checksum..." no menu do botão direito |
| Ao terminar um download | ✅ analisar com o antivírus do Windows, extrair `.zip`, correr um comando, abrir o ficheiro (**⚙ → Configurações → Downloads → Quando terminar**) |
| Quando todos terminarem | ✅ fechar o app, suspender ou desligar o PC (com contagem de 30 s que se pode cancelar) |
| PC acordado | ✅ o Windows não adormece a meio de downloads |
| Marca "veio da internet" | ✅ como um ficheiro gravado pelo navegador: o Windows avisa antes de abrir programas baixados |
| Disco cheio | ✅ verifica o espaço antes de começar; se faltar, pausa com uma mensagem clara e continua com "Retomar" |
| Limites por site | ✅ máximo de conexões para sites escolhidos (**⚙ → Limites por site**); e a app lembra-se do que cada servidor aguenta |
| Suporte a vídeos sempre atual | ✅ **⚙ → Configurações → Atualizações → Suporte a vídeos**: a versão mais recente do yt-dlp, verificada com o checksum oficial, sem reinstalar |

## Vídeos (YouTube, Instagram, TikTok, etc.)

1. Copie o link do vídeo.
2. No Brave, clique no ícone do **Flow Downloads** na barra de extensões.
3. Cole o link em **Baixar vídeo** e clique no botão.
4. O Flow Downloads abre uma janela para escolher a qualidade (1080p, 720p... ou só áudio).

Também pode colar o link do vídeo direto na barra de URL do Flow Downloads.

Para isto o Flow Downloads usa duas ferramentas gratuitas:

- **yt-dlp** — encontra o vídeo nos sites. O Flow Downloads instala-o sozinho: na janela do vídeo, clique em
  *Instalar suporte a vídeos* (ou `pip install yt-dlp`). Os sites mudam com frequência; se um vídeo
  deixar de funcionar, use **⚙ → Configurações → Atualizações → Suporte a vídeos**.
- **ffmpeg** — junta o vídeo e o áudio. Sem ele, o YouTube só dá até 360p e não há conversão para MP3.
  No Windows: abra o terminal e rode `winget install ffmpeg`, depois reabra o Flow Downloads.

> Baixe só vídeos que tem direito a guardar — alguns sites, como o YouTube, não permitem isso nos termos de uso.

## Como usar

1. Instale o Python 3.10+ em <https://www.python.org/downloads/> (no Windows, marque *"Add Python to PATH"*).
2. Baixe este projeto (botão **Code → Download ZIP**) e extraia.
3. Abra o app:
   - **Windows:** clique duas vezes em `Flow Downloads.pyw`
   - **Qualquer sistema:** `python -m flowdl`

Cole um link na barra de URL e clique em **Baixar**.

### Linha de comando

```bash
python -m flowdl https://exemplo.com/arquivo.zip -o C:\Downloads -c 16
```

Se interromper com `Ctrl+C`, rode o mesmo comando de novo para continuar de onde parou.

## Extensão do navegador

Com a extensão, os downloads que você inicia no navegador vão direto para o Flow Downloads
(incluindo cookies, então funciona em sites com login).

Funciona em navegadores baseados em Chromium: **Brave**, Chrome, Edge, Opera, Vivaldi.

**Instalar no Brave**
1. Abra `brave://extensions` na barra de endereços.
2. Ative o **Modo de desenvolvedor** (canto superior direito).
3. Clique em **Carregar sem compactação** (*Load unpacked*) e escolha a pasta `browser-extension` deste projeto.
4. Clique no ícone de peça 🧩 da barra e fixe o Flow Downloads (📌), para o ter sempre à mão.

Nos outros navegadores é igual, mudando o endereço: `chrome://extensions`, `edge://extensions`, `opera://extensions`.

Depois de atualizar o Flow Downloads, clique em **↻ (recarregar)** na extensão em `brave://extensions`.

O Flow Downloads precisa estar a correr (pode ser em segundo plano). Se estiver fechado, o navegador baixa normalmente.
No menu da extensão também pode ligar/desligar a captura de downloads (quando desligada aparece "OFF" no ícone).

**Nunca pior do que o navegador sozinho.** Quando um download começa, a extensão **pausa** o do navegador
(não o cancela) e passa-o à app, com os cookies e a identidade exata do navegador (User-Agent e os outros
cabeçalhos que sites protegidos, como os que usam Cloudflare, comparam). Só quando a app está mesmo a receber
o ficheiro é que o download do navegador é trocado pelo da app. Se o site recusar a app — um link que só serve
uma vez, uma página de proteção, um login — o navegador **continua o seu próprio download** de onde estava.

**A app fechada?** O navegador abre-a sozinho (a app regista-se no Brave, Chrome, Edge e Chromium quando
arranca, só para o teu utilizador). Enquanto ela arranca, o download do navegador fica em pausa; se não
arrancar, o navegador continua.

**O que aconteceu ao último download?** O menu da extensão mostra-o numa linha, por exemplo
"baixado pela app ✔", "o Brave baixou sozinho: a app não conseguiu (HTTP 403)" ou "deixado ao navegador:
veio de um formulário". Útil quando um site não funciona como esperado.

## Como funciona a aceleração

1. O Flow Downloads pergunta ao servidor o tamanho do arquivo e se ele aceita downloads em partes (`Range`).
2. O arquivo é dividido em partes (um arquivo de 1 GB com 8 conexões = 8 partes de 125 MB), cada uma
   baixada por uma conexão e gravada direto na posição certa — no fim não há nada para juntar.
3. **Teste de velocidade:** começa com 2 conexões (4 em arquivos grandes) e vai dobrando (2 → 4 → 8...)
   enquanto a velocidade total subir pelo menos 15%. Se não compensar (linha ou servidor no limite), para
   de abrir conexões — e volta a testar se a velocidade cair a meio. Se o servidor recusar conexões a mais
   (erro 503/429, ou 403 em alguns sites de ficheiros), usa só as que ele aceita, sem dar erro. O valor de "Conexões" é o **máximo**.
4. **Conexões reaproveitadas:** cada conexão fica aberta e vai pedindo pedaço atrás de pedaço (de ~2 s de
   dados cada), sem repetir o "aperto de mão" com o servidor. O primeiro pedido já traz o início do arquivo.
5. Quando uma conexão termina, ela pega metade da maior parte que ainda falta — todas ficam ocupadas até o fim.
   **Memória por servidor:** a app lembra-se de quantas conexões compensaram em cada servidor (e dos que
   limitam), e o próximo download desse servidor começa logo aí em vez de testar de novo.
6. **Sem ficar nos 99%:** uma conexão muito mais lenta que as outras é reaberta (pode cair num servidor
   mais rápido), e no fim as conexões rápidas assumem o que a mais lenta ainda tem por receber.
7. **Sem paragens:** uma conexão que deixa de receber (o servidor ou a linha calou-se sem a fechar) é trocada
   ao fim de 4 s se as outras estão a receber, ou 6 s se todas pararam — em vez de esperar 30 s. Servidores
   lentos a responder não são cortados em ciclo: a espera aprende o tempo de resposta do servidor.
8. **Na lista do navegador**, a app trabalha perto do início do ficheiro, para o progresso do navegador
   acompanhar o da app (o navegador só pode receber o ficheiro a partir do início).
9. O progresso é salvo em `arquivo.part.json` (gravado no disco antes de ser anotado, para aguentar faltas de luz).

### Comparar com e sem o Flow Downloads

```bash
python tools/benchmark.py https://exemplo.com/instalador.exe
```

Baixa o mesmo arquivo com 1 conexão (como o navegador) e com o Flow Downloads, mostra a velocidade de
cada um e confirma que os arquivos são idênticos. O ganho depende da internet e do servidor: é maior
quando cada conexão é limitada pelo servidor ou pela distância, e pequeno quando uma só conexão já
enche a sua internet.

Se o servidor não aceita partes, o download é feito com uma conexão só (e não pode ser retomado).

## Segurança e fiabilidade

- **Cookies de login só vão para o site certo**, como num navegador: depois de um redirecionamento
  para outro site, os teus cookies e palavras-passe não seguem.
- **Cookies protegidos no disco**: encriptados com a proteção do próprio Windows (DPAPI); noutros
  sistemas o ficheiro só é legível pelo teu utilizador.
- **Nunca mistura versões**: se o ficheiro mudar no servidor entre uma pausa e o retomar, o
  download recomeça com a versão nova (If-Range), em vez de juntar bocados das duas.
- **Links que expiram** (GitHub, armazenamento na nuvem): a app guarda o link original e pede um
  endereço novo sempre que o temporário expira.
- **Downloads de formulários** (POST) ficam com o navegador, que é o único que os sabe repetir.
- **Certificado HTTPS inválido**: avisa logo, com uma mensagem clara.
- **Servidor ocupado** ("tente daqui a X segundos"): espera exatamente o que o servidor pede.
- **A extensão diz a sua versão**: se estiver desatualizada, a app e o menu da extensão avisam.

## Testes

```bash
python -m unittest tests.test_engine tests.test_features tests.test_video tests.test_service  # motor, vídeo, serviço
python -m unittest tests.test_gui                     # interface (precisa de ecrã; no Linux: xvfb-run)
node tests/e2e/extension.e2e.js                       # extensão num Chromium real (Playwright + xdotool)
python tests/e2e/system_e2e.py                        # falta de luz, internet instável, Ctrl+C, consumo... (Linux)
```

## Estrutura

```
flowdl/service.py     serviço em segundo plano (leve, sem verificações periódicas)
flowdl/engine.py      motor de download (conexões, segmentação, retomada)
flowdl/manager.py     fila e lista de downloads salva em disco
flowdl/gui.py         interface gráfica (Tkinter), criada só quando é precisa
flowdl/cli.py         linha de comando
flowdl/bridge.py      servidor local mínimo (127.0.0.1:17890) que recebe links da extensão
flowdl/grabber.py     captura de arquivos de sites
flowdl/video.py       downloads de vídeo/áudio (yt-dlp)
flowdl/scheduler.py   agendador
flowdl/limiter.py     limitador de velocidade
flowdl/autostart.py   iniciar com o computador
browser-extension/    extensão do navegador
assets/logo.png       logo
tests/                testes automáticos (python -m unittest)
```

As configurações ficam em `%APPDATA%\FlowDownloads` (Windows) ou `~/.config/FlowDownloads` (Linux/macOS).
