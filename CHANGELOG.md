# Novidades do Flow Downloads

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
