# PyDM — Gerenciador de Downloads gratuito

Um gerenciador de downloads no estilo do IDM (Internet Download Manager), feito em Python,
**sem nenhuma dependência para instalar** — só o Python.

## Funcionalidades

| Recurso do IDM | PyDM |
|---|---|
| Aceleração de download (várias conexões) | ✅ até 32 conexões por arquivo |
| Segmentação dinâmica | ✅ quando uma conexão termina, ela divide ao meio o maior pedaço restante |
| Retomar downloads | ✅ pausa/retoma e continua de onde parou; se a internet cair reconecta sozinho; ao reabrir o PyDM (depois de fechar, reiniciar o PC ou faltar luz) os downloads que estavam a correr recomeçam sozinhos |
| Captura de vídeos e áudios | ✅ cole o link no botão do PyDM na barra do navegador e escolha a resolução (1080p, 720p...) ou só o áudio em MP3 — sem botões por cima dos vídeos |
| Integração com o navegador | ✅ extensão para navegadores Chromium (Brave, Chrome, Edge, Opera); também clique-direito → "Baixar com PyDM" |
| Download com um clique | ✅ ao clicar num link de download no navegador, o PyDM assume |
| Limitador de velocidade | ✅ limite total e por download |
| Agendador | ✅ iniciar/parar em horários e dias definidos; ao terminar, fechar o app ou desligar o PC |
| Captura de sites (grabber) | ✅ lista vídeos, músicas, documentos etc. de uma página (e de páginas ligadas a ela) |
| Categorias | ✅ Compactados, Documentos, Músicas, Programas, Vídeos, Imagens |
| Tema escuro | ✅ Exibir → Tema escuro |
| Interface personalizável | ✅ Exibir → Colunas |
| Fila de downloads | ✅ número de downloads simultâneos configurável |
| Capturar links copiados | ✅ link copiado aparece automaticamente na barra de URL |
| HTTP / HTTPS / FTP | ✅ HTTP e HTTPS com várias conexões; FTP básico (1 conexão) |

## Vídeos (YouTube, Instagram, TikTok, etc.)

1. Copie o link do vídeo.
2. No Brave, clique no ícone do **PyDM** na barra de extensões.
3. Cole o link em **Baixar vídeo** e clique no botão.
4. O PyDM abre uma janela para escolher a qualidade (1080p, 720p... ou só áudio).

Também pode colar o link do vídeo direto na barra de URL do PyDM.

Para isto o PyDM usa duas ferramentas gratuitas:

- **yt-dlp** — encontra o vídeo nos sites. O PyDM instala-o sozinho: na janela do vídeo, clique em
  *Instalar suporte a vídeos* (ou `pip install yt-dlp`). Os sites mudam com frequência; se um vídeo
  deixar de funcionar, use **Opções → Instalar/atualizar suporte a vídeos**.
- **ffmpeg** — junta o vídeo e o áudio. Sem ele, o YouTube só dá até 360p e não há conversão para MP3.
  No Windows: abra o terminal e rode `winget install ffmpeg`, depois reabra o PyDM.

> Baixe só vídeos que tem direito a guardar — alguns sites, como o YouTube, não permitem isso nos termos de uso.

## Como usar

1. Instale o Python 3.10+ em <https://www.python.org/downloads/> (no Windows, marque *"Add Python to PATH"*).
2. Baixe este projeto (botão **Code → Download ZIP**) e extraia.
3. Abra o app:
   - **Windows:** clique duas vezes em `PyDM.pyw`
   - **Qualquer sistema:** `python -m pydm`

Cole um link na barra de URL e clique em **Baixar**.

### Linha de comando

```bash
python -m pydm https://exemplo.com/arquivo.zip -o C:\Downloads -c 16
```

Se interromper com `Ctrl+C`, rode o mesmo comando de novo para continuar de onde parou.

## Extensão do navegador

Com a extensão, os downloads que você inicia no navegador vão direto para o PyDM
(incluindo cookies, então funciona em sites com login).

Funciona em navegadores baseados em Chromium: **Brave**, Chrome, Edge, Opera, Vivaldi.

**Instalar no Brave**
1. Abra `brave://extensions` na barra de endereços.
2. Ative o **Modo de desenvolvedor** (canto superior direito).
3. Clique em **Carregar sem compactação** (*Load unpacked*) e escolha a pasta `browser-extension` deste projeto.
4. Clique no ícone de peça 🧩 da barra e fixe o PyDM (📌), para o ter sempre à mão.

Nos outros navegadores é igual, mudando o endereço: `chrome://extensions`, `edge://extensions`, `opera://extensions`.

Depois de atualizar o PyDM, clique em **↻ (recarregar)** na extensão em `brave://extensions`.

O PyDM precisa estar aberto. Se estiver fechado, o navegador baixa normalmente.
No menu da extensão também pode ligar/desligar a captura de downloads (quando desligada aparece "OFF" no ícone).

## Como funciona a aceleração

1. O PyDM pergunta ao servidor o tamanho do arquivo e se ele aceita downloads em partes (`Range`).
2. O arquivo é dividido em N partes, cada uma baixada por uma conexão, gravando direto na posição certa.
3. Quando uma conexão termina, ela pega metade da maior parte que ainda falta — todas ficam ocupadas até o fim.
4. O progresso é salvo em `arquivo.part.json`, por isso dá para retomar depois.

Se o servidor não aceita partes, o download é feito com uma conexão só (e não pode ser retomado).

## Estrutura

```
pydm/engine.py      motor de download (conexões, segmentação, retomada)
pydm/manager.py     fila e lista de downloads salva em disco
pydm/gui.py         interface gráfica (Tkinter)
pydm/cli.py         linha de comando
pydm/bridge.py      servidor local (127.0.0.1:17890) que recebe links da extensão
pydm/grabber.py     captura de arquivos de sites
pydm/video.py       downloads de vídeo/áudio (yt-dlp)
pydm/scheduler.py   agendador
pydm/limiter.py     limitador de velocidade
browser-extension/  extensão do navegador
tests/              testes automáticos (python -m unittest)
```

As configurações ficam em `%APPDATA%\pydm` (Windows) ou `~/.config/pydm` (Linux/macOS).
