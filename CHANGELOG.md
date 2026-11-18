# Novidades do Flow Downloads

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
