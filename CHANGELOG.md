# Novidades do Flow Downloads

## 0.9.19

**Novo: downloads gerados por formulários** (exportações, relatórios, alguns sites de alojamento de ficheiros). Antes ficavam sempre com o navegador.
- A extensão guarda o que o formulário enviou e a app envia exatamente o mesmo pedido: os campos, o tipo e a origem.
- Se o formulário leva a um endereço normal, a app baixa esse endereço com várias ligações, sem enviar o formulário outra vez.
- Se o ficheiro vem na própria resposta do formulário, a app usa uma só ligação. Repetir o formulário para pedir partes podia gerar outro ficheiro ou repetir uma ação.
- Ficam com o navegador, como antes: formulários com ficheiros anexados (multipart), formulários acima de 1 MB e janelas privadas.
- O conteúdo do formulário fica cifrado na lista de downloads, como os cookies, e é apagado quando o download termina. Não aparece no "Copiar diagnóstico".
- Extensão 0.9.13: recarregue-a em brave://extensions (↻).

## 0.9.18

**Novo: a app instala o ffmpeg sozinha.** Sem ele o YouTube fica em 360p e não há conversão para MP3. Antes era preciso instalá-lo à mão (winget).
- Na janela de vídeos aparece o botão **"Instalar ffmpeg (≈ 200 MB)"**, com o progresso. No fim, a lista de qualidades volta a aparecer, já com 1080p ou mais.
- Também em Configurações → Atualizações → Vídeos: instalar, atualizar ou remover.
- Vem das versões oficiais para Windows do projeto FFmpeg-Builds e é conferido com o SHA-256 publicado com elas: se não conferir, nada é guardado. Da transferência só se guardam o ffmpeg.exe e o ffprobe.exe, na pasta da app. Não precisa de administrador nem muda o PATH do Windows.

## 0.9.17

**Thinkbroadband: a causa real, encontrada com o "Copiar diagnóstico"**
- O servidor não conta as ligações, conta os **pedidos**. Ao fim de ~120 pedidos responde "429 Too Many Requests" e deixa de enviar durante ~75 s. A app fazia um pedido a cada ~2 s por ligação: 257 pedidos em 3 GB, duas paragens longas.
- Agora cada pedido traz muito mais dados (~15 s por ligação, até 256 MB), como fazem os outros gestores de downloads. No simulador que imita esse servidor (3 GB a 62,5 MB/s): de 139 s, com uma paragem de 75 s, para 48,5 s sem paragens (o máximo possível é 48 s). Com "Mostrar também na lista do navegador" ligado: de 155 s para 48,8 s; 5 GB em 82,6 s (máximo possível: 82 s).
- Se um servidor responder 429 na mesma, a app passa a pedir o pedaço inteiro de cada ligação, não abre nem reabre ligações enquanto o servidor está bloqueado (cada uma era mais um pedido) e espera mais antes de tentar de novo. Lembra-se desse servidor para os downloads seguintes.
- Corrigido: ao substituir várias ligações presas ao mesmo tempo, a app chegava a 11 ligações com o máximo em 8.
- O diagnóstico mostra também se o download estava na lista do navegador.
- Testes novos: servidor que responde 429 ao passar de N pedidos (com e sem a lista do navegador), e memória do servidor.

## 0.9.16

**Thinkbroadband (e servidores parecidos): parava, voltava e parava outra vez**
- As ligações presas que a app substituía ficavam abertas no servidor até acordarem (viu-se 15 ligações abertas com o máximo em 8). Agora são fechadas no momento em que são substituídas, e a app nunca passa do máximo de ligações.
- Depois de uma paragem, cada ligação pede o seu pedaço inteiro de uma vez, em vez de muitos pedidos seguidos (muitos pedidos parecem um robô a alguns servidores, que param toda a gente). A app lembra-se disso para esse servidor durante 6 horas. Num servidor de teste que castiga quem faz muitos pedidos: 49,5 s com 3 paragens → 23 s com 1.
- Dois testes automáticos novos: um servidor que só serve as ligações mais antigas, e um servidor que castiga muitos pedidos.

**Novo: "Copiar diagnóstico"** (botão direito num download). Copia o que aconteceu no download segundo a segundo: velocidade, ligações abertas e a receber, pedidos, paragens, ligações substituídas e erros. Não inclui o link nem os cookies, só o nome do servidor. Serve para colar numa mensagem quando um download se porta mal.

**Novo: Configurações → Downloads → "Ligações por download"**. Antes só se mudava na janela "+".

## 0.9.15

- Corrigido: num servidor que só aceita poucas ligações, a app às vezes voltava logo ao número de ligações que o tinha feito parar (e o download andava aos soluços). Duas causas: numa paragem abriam-se várias ligações de teste em vez de uma, e a paragem era anotada depois de outra regra já ter reaberto todas as ligações. Também passam a fechar-se as ligações presas que já tinham sido substituídas.
- Configurações → Navegador: o texto da opção "Mostrar também na lista do navegador" ficava cortado na janela. Encurtado, e um teste novo verifica em todas as páginas das Configurações que nenhum texto passa da borda.

## 0.9.14

- Corrigido: em servidores que param cada resposta a meio (e obrigam a pedir de novo), a 0.9.11 tinha ficado 8 vezes mais lenta (43 s → 5 min num teste). A app voltou a esperar 6 s (em vez de 4) antes de concluir que o servidor parou toda a gente; os servidores com limite de ligações (thinkbroadband) continuam a terminar igual.
- Bateria de 86 servidores problemáticos repetida com o motor novo: 73 passam (2 mais do que antes); os restantes são servidores que mentem sobre o ficheiro, onde o certo é dar erro.

## 0.9.13

- **A internet cai durante minutos: o download já não pára.** Antes, se o servidor ou a internet ficassem inacessíveis (sem rede, router a reiniciar) mais de ~2,5 minutos, o download acabava em "Erro: Falha na conexão" e não continuava quando a ligação voltava. Agora a app continua a tentar (de 30 em 30 s, até 30 minutos sem receber nada) e mostra "sem ligação ao servidor: a tentar de novo"; quando a ligação volta, continua de onde estava. O mesmo ao retomar downloads quando o PC liga antes de o Wi-Fi estar pronto.
- **SourceForge e outros sites com verificação de robôs**: links colados na app davam "HTTP 403". A app apresentava-se com um nome de navegador sem ser um navegador, e esses sites recusam isso. Agora, se o primeiro pedido for recusado, a app apresenta-se com o seu próprio nome (FlowDownloads) e o download funciona. Downloads vindos do Brave continuam a usar a identidade do Brave.
- Testado com ficheiros reais (GitHub, PyPI, npm, SourceForge): todos completos e iguais ao original. Num ficheiro de 906 MB num servidor rápido, a app foi tão rápida como uma ligação única.

## 0.9.12

Correções encontradas numa revisão de código às versões 0.9.10 e 0.9.11:
- Com a opção "Mostrar os downloads também na lista do navegador" desligada: se a app reiniciasse a meio, o navegador podia voltar a baixar o mesmo ficheiro (duas cópias). E a opção não era respeitada com "Perguntar antes de baixar" ligado, nem ao continuar um download repetido.
- Uma paragem curta (por exemplo uma falha de Wi-Fi) deixava o resto de um download longo sem a ajuda do fim (a espera aos 99% podia voltar). O cuidado extra depois de uma paragem dura agora só 90 segundos.
- Ao fechar ligações a mais numa paragem, a app podia fechar justamente a que estava a receber. Já não.
- Janela da extensão: dois downloads anotados ao mesmo tempo já não se apagam um ao outro na lista dos últimos 5. Extensão 0.9.12: recarregue-a em brave://extensions (↻).
- Teste de resistência: 300 downloads seguidos, 0 falhados, memória estável.

## 0.9.11

**Downloads que não param**
- Servidores que só aceitam poucas ligações (como o thinkbroadband): já não ficam presos no fim. Antes, perto do fim, a app cortava e reabria todas as ligações ao mesmo tempo, o servidor via ligações a mais por um instante e parava toda a gente outra vez - e assim por minutos. Agora, num servidor que já parou uma vez, cada ligação espera um instante antes de reabrir, e o fim do download é tratado como o resto. Num simulador desse servidor: 2 min 34 s antes, 33 s agora.
- A app percebe mais depressa que o servidor parou toda a gente (4 s em vez de 6) e fecha as ligações a mais logo a seguir.
- Fim do download: deixou de partir o que falta em pedaços minúsculos (cada um custava um pedido novo); uma ligação lenta continua a ser ajudada (ideia do Gopeed, reescrita por nós).
- Comparado lado a lado com a 0.9.10 em servidores rápidos, lentos, que castigam ou com quedas: a mesma velocidade.

**Navegador**
- Nova opção em Configurações → Navegador: **"Mostrar os downloads também na lista do navegador"**. Desligada, a app guarda o ficheiro ela própria (como o IDM): o disco só o grava uma vez e o navegador não pergunta uma segunda vez onde guardar. Extensão 0.9.11: recarregue-a em brave://extensions (↻).
- Correção: com "Perguntar onde guardar cada ficheiro" ligado no Brave, a segunda janela "Salvar como" continuava a aparecer (o Brave pergunta sempre, mesmo quando a extensão pede para não perguntar). Desligue essa opção do Brave, ou a nova opção acima.
- A janela da extensão mostra os **últimos 5 downloads** e o que aconteceu a cada um.
- Downloads pedidos por outras extensões ficam sempre com o navegador.

## 0.9.10

- **Corrigido: download parado a 0 B/s em servidores que só aceitam poucas ligações** (visto no thinkbroadband: "o servidor parou: 4 ligações" e 13 ligações abertas, para sempre). Esses servidores só voltam a enviar quando o número de ligações abertas desce, e a app ficava à espera que as ligações acordassem. Agora, se a paragem durar mais de 2 segundos, a app fecha as ligações a mais e o servidor volta a enviar. Depois de um servidor parar uma vez, a app nunca mais passa do número que ele aceitou nesse download, nem para acabar as últimas partes.

## 0.9.9

- **Ficheiros grandes arrancam mais depressa.** Num download que aparece na lista do Brave, as ligações novas pediam ao início blocos muito pequenos e demoravam a ganhar velocidade (visto: um ficheiro de 5 GB começava a ~6 MB/s e ficava 16 s atrás do Brave). Agora pedem blocos do tamanho certo desde o início, e os ficheiros grandes começam com 8 ligações em vez de 4.
- **A app diz porque não ficou com um ficheiro grande.** Quando um ficheiro com mais de 20 MB fica com o navegador por o tipo não estar na lista (ou o site estar ignorado), aparece um aviso a dizer porquê e como mudar. Extensão 0.9.9: recarregue-a em brave://extensions (↻).

## 0.9.8

- **O Brave já não pergunta duas vezes onde guardar.** Com a opção do Brave "Perguntar onde guardar cada ficheiro" ligada, aparecia uma segunda janela "Salvar como" quando a app ficava com o download. Agora só aparece a do Brave, e o ficheiro fica com o nome que escolheu nela. Extensão 0.9.8: recarregue-a em brave://extensions (↻).
- **A app já não fica lenta num site durante um mês** depois de um download em que o servidor parou. Quando um servidor parava várias vezes, a app passava a usar menos ligações com ele durante 30 dias, e os downloads seguintes desse site ficavam mais lentos do que no navegador. Agora isso dura só algumas horas. O limite que o próprio servidor impõe (quando recusa mais ligações) continua a ser respeitado.

## 0.9.7

- **Ficheiros de tipos pouco comuns já vão para a app.** Um ficheiro com uma extensão que não está na lista (".dat", ".vhdx", ".pak"...) mas que o site envia como "ficheiro para guardar" ficava com o navegador (visto: o ficheiro de 5 GB do testfile.org, "testfile.org-5GB.dat"). Agora a app fica com ele. Os tipos que tirou da lista continuam com o navegador, e ficheiros pequenos do dia a dia (texto, imagens, legendas) também.

## 0.9.6

- **Já não cria pastas vazias** ("Compactados", "Vídeos"...) quando o download aparece na lista do Brave: o ficheiro vai para a pasta do navegador e a app já não cria a pasta da categoria.
- Quando a app **não fica com um download** e o deixa ao navegador (o site recusou a app, falta de espaço...), aparece um **aviso a dizer porquê**. Antes o navegador simplesmente continuava e não se sabia o que tinha acontecido.

## 0.9.5

Testes a fundo: o motor contra 86 tipos de servidor, todos os botões e janelas da app, 300 downloads seguidos, e o Brave com 8 downloads ao mesmo tempo. 23 erros corrigidos, cada um com um teste.

**Downloads**
- Um ficheiro **cortado a meio** (servidores sem partes) já não é dado como concluído: a app tenta de novo do início (3 vezes) e, se não conseguir, diz porquê.
- Servidores que respondem com **outra parte** do ficheiro, ou que o **substituem a meio** (outro tamanho), já não produzem ficheiros misturados.
- Uma resposta que acaba sem dados já não faz a app repetir o pedido milhares de vezes por segundo.
- Ficheiros vazios (0 bytes) em servidores como o Amazon S3; ficheiros comprimidos pelo servidor (gzip) são desempacotados.
- Retomar num servidor que limita ligações já não dá "o link deixou de funcionar".
- Downloads começados ao mesmo tempo com o **mesmo nome** já não escrevem no mesmo ficheiro.
- Nomes com ";" ou aspas (Content-Disposition) ficam certos.

**Brave**
- Se o Brave parar a extensão enquanto ela espera pela app, o download do Brave **já não fica parado para sempre** (a extensão acorda sozinha de 30 em 30 s). Extensão 0.9.5: recarregue-a em brave://extensions (↻).

**Janela**
- Uma categoria com o nome de outra já existente **impedia a janela de abrir**: agora é recusada (e listas antigas com nomes repetidos voltam a abrir).
- "Baixar no navegador" num download concluído **apagava o ficheiro**; "Atualizar link" baixava-o de novo. Corrigido.
- Cada abertura e fecho da janela deixava ~2 MB presos na memória.
- O botão de um download **em fila** não fazia nada: agora "Começar já".
- "Marcar ficheiros como vindos da internet" passa a valer logo, sem reiniciar.
- Avisos para checksum mal escrito, limites por site inválidos, profundidade inválida, dias do agendador; avisos à frente da janela certa; "Abrir" e "Abrir pasta" dizem quando o ficheiro ou a pasta já não existem; caixas de texto mudam de cor com o tema; janela das categorias com o botão Guardar visível.

**Ficheiros e pedidos estranhos** (testados com milhares de entradas malformadas)
- Um pedido malformado vindo do próprio computador **fechava a app em segundo plano**: agora é recusado e a app continua.
- Um ficheiro de configurações ou a lista de downloads **danificados** (editados à mão, falha do disco) **impediam a app de abrir** ou eram **apagados sem aviso**: agora a app abre sempre, cada valor estranho volta ao normal, e o ficheiro danificado fica guardado ao lado (`.bad`). Uma lista com lixo no fim é recuperada inteira.
- "Procurar atualizações" e "Atualizar suporte a vídeos" **ficavam presos** em "A procurar..." com certas respostas do servidor: agora dizem sempre o que aconteceu.
- Uma memória de servidores danificada fazia **todos os downloads falharem**; agora é ignorada.
- "Não capturar neste site" só aceita nomes de sites reais (antes "*" desligava a captura de tudo).
- No modo Brave: ficheiros vazios e pedidos repetidos depois do fim já não confundem o navegador; downloads visíveis no navegador acompanham melhor o progresso da app.

## 0.9.4

- **Corrigido: velocidade a 0 com as ligações a subir (visto: 8 → 21) e downloads mais lentos do que o navegador.** Quando o servidor (ou a internet) parava todas as ligações, a app lia a queda como "precisa de mais ligações", e ao cortar e reabrir as ligações paradas abria ainda mais no servidor. Alguns servidores e routers castigam isso, e o download ficava preso. Agora:
  - numa paragem geral a app **espera** com as ligações que tem: não abre, não corta e não divide trabalho;
  - usa **metade das ligações** durante 90 s e, se o servidor voltar a parar no mesmo download, lembra-se disso para a próxima vez;
  - nunca aumenta as ligações com a velocidade perto de 0.
  Num servidor que castiga ligações a mais: antes não terminava (40 ligações abertas); agora termina com 4.

## 0.9.3

Revisão completa da app (três análises do código + medições). Cada correção tem um teste que falhava antes.

- **Segurança:** um site podia enviar downloads para a app (pedidos com origem "null"). Agora só a nossa extensão.
- **A lista de downloads já não se perde:** duas gravações ao mesmo tempo podiam estragar o ficheiro e, ao abrir, a lista ficava vazia. As configurações também passam a ser gravadas de forma segura e logo que mudam.
- **Nunca duas cópias da app a correr ao mesmo tempo** (escreviam os mesmos ficheiros).
- **Pausar é imediato** (podia demorar 30 s).
- **Downloads do Brave com a fila cheia** começam logo e nunca se perdem; a extensão aguenta ser reiniciada pelo Brave a meio, e se a app demorar, o Brave nunca fica com o download parado para sempre.
- **Mais rápido no modo Brave:** as primeiras ligações arrancam todas de uma vez; ~1 s ganho num servidor longe, ~30% menos pedidos. A app reage em 0,1 s a um download novo com a janela aberta.
- **Windows:** ficheiro temporário "esparso" (sem encher de zeros), menos escritas forçadas no disco, IPv6 avariado já não atrasa 30 s, cópias temporárias apagadas mesmo quando o Windows as segura, nomes como CON/NUL tratados.
- **Novo botão "Baixar no navegador"** para sites que recusam a app; "Atualizar link" funciona com o download a correr.
- **Comando ao terminar:** nomes com "&" já não correm comandos; ficheiros extraídos de um zip mantêm a marca "veio da internet"; o ficheiro guardado pelo Brave recebe a marca com o site verdadeiro.
- **Agendador** não falha a hora se o PC estava a dormir (até 15 min); vídeos com o mesmo título não se substituem; ficheiros sem tamanho aparecem logo no Brave; o aviso "concluído" só aparece quando o Brave já tem o ficheiro.
- **Linha de comando:** Ctrl+C e repetir o comando continua mesmo de onde parou.
- **Desinstalar** pergunta se apaga também a lista e as configurações; atualizações apagam os ficheiros antigos.
- O FTP deixou de ser anunciado (nunca funcionou): esses links ficam com o navegador.
- Extensão 0.9.3: recarregue-a em brave://extensions (botão ↻) depois de atualizar a app.

## 0.9.2

- **Quanto demorou:** o painel de um download concluído mostra o tempo que levou, quando chegou o primeiro byte e a velocidade média e máxima; durante o download, a velocidade máxima também aparece. Útil para comparar com o navegador.
- Ligações a 0 B/s: também quando as ligações novas nunca chegam a receber nada, a app já não abre mais (ficam no máximo escolhido + 2).

## 0.9.1

- **Corrigido: centenas de ligações a 0 B/s.** Quando o servidor (ou a internet) parava de enviar para todas as ligações ao mesmo tempo, a app abria uma ligação nova por cada parte parada; as novas também paravam, e o número dobrava a cada poucos segundos (visto: 280, depois 400). Agora, numa paragem geral, a app só volta a ligar as que já tem (esperando cada vez mais); e nunca passa do máximo escolhido + 2 (mais as paradas que ainda estão a fechar). Num teste com 45 s sem dados: antes 101 ligações abertas no servidor e a subir; agora no máximo 17, e o download continua quando o servidor volta.

## 0.9.0

- **Visual novo, só preto e branco** (claro ou escuro). O botão **◐** troca num clique; **Automático** segue o Windows.
- **Painel de detalhes** à direita: o download escolhido com velocidade, tempo que falta, ligações e o **mapa das partes** (o que cada ligação já baixou). Botões conforme o estado: Pausar, Retomar, Abrir, Tentar de novo.
- **Erros explicados**: o painel diz o que aconteceu e o que fazer. Novo **Atualizar link** para links que expiraram: o download continua de onde parou.
- **Botão ⚙** no lugar da barra de menus, e uma janela de **Configurações** (Ctrl+,) com tudo por secções: Aparência, Downloads, Velocidade, Navegador, Sistema, Atualizações.
- Filtros na barra lateral ou em botões por cima da lista; campo do link com dica; menos colunas por defeito (o resto está no painel).

## 0.8.0

- **Ícone ao lado do relógio do Windows:** mostra os downloads e a velocidade ao passar o rato; duplo clique abre a app; botão direito: pausar tudo, retomar tudo, ligar/desligar o limite de velocidade, sair. Aviso do Windows quando um download termina ou falha.
- **Vídeos e áudios desta página** no menu da extensão: lista o que a página tocou ou carregou (ficheiros e transmissões), com "Baixar" em cada um, e "Escolher qualidade" para a página. Sem botões por cima dos vídeos.
- **Atualizações:** Ajuda → Procurar atualizações (e uma verificação por dia). A versão nova é verificada com o SHA-256 publicado e instalada sozinha; a app volta a abrir.
- **Mais rápido em servidores com vários endereços** (CDNs): as ligações espalham-se por eles.
- **Fim mais rápido:** o pedaço de uma ligação presa passa logo para outra; o download termina assim que chegam todos os bytes.
- O navegador abre a app quando ela está fechada; Alt+clique deixa o download com o navegador; checksum; ações ao terminar (antivírus, extrair zip, comando, abrir); suspender o PC no fim; o PC não adormece a meio; marca "veio da internet"; disco cheio tratado com uma mensagem clara.

## 0.7.0

- O navegador abre o Flow Downloads quando está fechado; memória por servidor; limites por site; checksum; ações ao terminar; PC acordado; marca "veio da internet"; suporte a vídeos atualizável.

## 0.6.0

- Nunca pior do que o navegador sozinho: o download do navegador fica em pausa e só é trocado quando a app o está mesmo a receber; links que só servem uma vez, páginas de proteção e sites com Cloudflare.
