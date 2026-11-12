# PyDM — Gerenciador de Downloads gratuito

Um gerenciador de downloads no estilo do IDM (Internet Download Manager), feito em Python,
**sem nenhuma dependência para instalar** — só o Python.

## Funcionalidades

| Recurso do IDM | PyDM |
|---|---|
| Aceleração de download (várias conexões) | ✅ até 32 conexões por arquivo |
| Segmentação dinâmica | ✅ quando uma conexão termina, ela divide ao meio o maior pedaço restante |
| Retomar downloads | ✅ pausa/retoma, e continua de onde parou mesmo depois de fechar o app ou cair a internet (reconecta sozinho) |
| Integração com o navegador | ✅ extensão para Chrome, Edge, Brave, Opera e Firefox; também clique-direito → "Baixar com PyDM" |
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

**Chrome / Edge / Brave / Opera**
1. Abra `chrome://extensions` (ou `edge://extensions`).
2. Ative o **Modo do desenvolvedor**.
3. Clique em **Carregar sem compactação** e escolha a pasta `browser-extension`.

**Firefox**
1. Abra `about:debugging#/runtime/this-firefox`.
2. **Carregar extensão temporária** → escolha `browser-extension/manifest.json`.

O PyDM precisa estar aberto. Se estiver fechado, o navegador baixa normalmente.
Clique no ícone da extensão para ligar/desligar a captura (aparece "OFF").

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
pydm/scheduler.py   agendador
pydm/limiter.py     limitador de velocidade
browser-extension/  extensão do navegador
tests/              testes automáticos (python -m unittest)
```

As configurações ficam em `%APPDATA%\pydm` (Windows) ou `~/.config/pydm` (Linux/macOS).
