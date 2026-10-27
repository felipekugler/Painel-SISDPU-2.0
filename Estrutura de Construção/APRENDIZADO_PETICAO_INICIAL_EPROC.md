# Aprendizado — "Preparar Petição Inicial no eproc"

Registro da aula ao vivo com o Defensor (caso-escola: PAJ 2026/029-03139,
JFPR, benefício por incapacidade — NB 731389667-1). Objetivo final: botão no
painel que deixa TUDO preenchido no e-proc e PARA antes do protocolo; o
Defensor confere e efetiva manualmente.

## Etapa 1 de 5 — "Informações do processo" (`acao=processo_cadastrar`)

Menu lateral "Petição Inicial" → `controlador.php?acao=processo_cadastrar`.
Breadcrumb: Informações do processo >> Assuntos >> Partes Autoras >>
Informações Adicionais (5ª = confirmação, presumivelmente).

1. **Toggle "Apoio por Inteligência Artificial"** (`#btnPredicao`, checkbox) —
   DEVE ficar LIGADO (regra do Defensor): o e-proc lê a petição inicial e
   sugere os assuntos na etapa seguinte. Vem ligado por padrão; o botão deve
   garantir `checked` mesmo assim.
2. **Documentos** — mesmo widget do peticionamento geral (Fine Uploader,
   ver `eproc_client.py` §"Peticionamento real"):
   - `input.qq-upload-input` aceita `multiple`: mandar os 17 arquivos de uma vez
     (≈7 MB) criou 17 blocos "Documento N" sozinho, todos `qq-upload-success`
     em <8s. (O peticionamento geral faz 1 por vez por causa de um travamento
     com ~43 MB via CDP — manter lote só abaixo de um limite seguro.)
   - Tipo: `#txtTipo_<n>` (autocomplete) + oculto `#selTipoArquivo_<n>` (ID do
     catálogo). Objeto JS `window.objAjaxAutoTipo_<n>.arrItens` = catálogo
     completo `[{id, descricao}]` (322 itens na JFPR). "Listar todos"
     (`#lblListarTipo_<n>`) abre o popup `#divInfraAjaxtxtTipo_<n>` com tudo.
   - No Chrome real, `mouseover/mousedown/mouseup/click` disparados via JS no
     `<a>` do `<li>` preencheram corretamente o ID oculto (no Playwright o
     painel usa clique de mouse real — manter o que já é comprovado lá).
   - Sigilo por documento: `#selIdSigilo_<n>` (padrão "Sem Sigilo (Nível 0)").
   - Depois: botão "Confirmar seleção de documentos" (`#btnEnviarArquivos`).
3. Campos ocultos que aparecem depois (classe `prePreenchimento` → pré-preenchidos
   pela IA do e-proc a partir da inicial): `#selRitoProcesso`,
   `#selIdClasseJudicial`, `#selIdSigilo`, `#txtValorCausa`,
   `#chkNaoAplicaValor`, `#chkRenunciaValor`, `#chkValorAlcada`.
   (A observar após "Confirmar seleção".)

### Após "Confirmar seleção de documentos" — "Informações Preliminares"

Os docs viram uma tabela (Seq/Formato/Nome/Tamanho/Tipo/Sigilo/Data envio) e
aparece o bloco de endereçamento. A IA do e-proc NÃO pré-preencheu nada aqui
(caso-escola) — o painel tem de deduzir tudo da inicial/cálculo:

| Campo | id | Valor no caso-escola | Fonte da dedução |
|---|---|---|---|
| Desejo entrar com a ação em | `selLocalidadeJudicial` | Curitiba (7050) | endereçamento da inicial ("SUBSEÇÃO JUDICIÁRIA DE CURITIBA/PR") |
| Rito | `selRitoProcesso` | 1 = JUIZADO ESPECIAL FEDERAL (2 = Ordinário, padrão!) | "JUIZADO ESPECIAL FEDERAL" no endereçamento |
| Área | `selIdGrupoCompetencia` | 3 = Previdenciária | réu INSS |
| Classe Processual | `selIdClasseJudicial` | 0000000161 = PROCEDIMENTO DO JUIZADO ESPECIAL CÍVEL | lista filtrada por rito+área (só 6 opções em JEF/Prev) |
| Nível de Sigilo | `selIdSigilo` | 0 = Sem Sigilo | padrão |
| Processo Originário / Juízo | `txtProcessoOriginario` / `txtDescJuizo` | vazio | — |
| Valor da Causa | `txtValorCausa` (máscara; digitar só dígitos, ex. `2785855` → 27.858,55) | R$ 27.858,55 | "TOTAL DO VALOR DA CAUSA" no `19 - Calculo do Valor da Causa.pdf` |
| Não se aplica / Renúncia > 60 SM | `chkNaoAplicaValor` / `chkRenunciaValor` | (a confirmar) | Termo de Renúncia JEF anexado? |

Ordem importa: localidade → rito → área (cada `change` recarrega via AJAX as
opções do seguinte, ~2s); **selecionar a Classe RECARREGA A PÁGINA INTEIRA**
(aviso "aguarde a atualização da tela") — os valores anteriores e os docs
sobrevivem ao reload, mas o valor da causa deve ser preenchido DEPOIS da classe.

### Regras do Defensor para "Informações Preliminares" (2026-10-08)

- Localidade: **Curitiba** por padrão.
- Rito conforme valor da causa: **até 60 SM → JUIZADO ESPECIAL FEDERAL + marcar
  `chkRenunciaValor`** ("Renuncia valor excedente a 60 salários mínimos");
  **acima de 60 SM → RITO ORDINÁRIO (COMUM), sem renúncia**.
- Área: **Previdenciária** por padrão.
- Classe conforme rito: JEF → **PROCEDIMENTO DO JUIZADO ESPECIAL CÍVEL**;
  ordinário → **PROCEDIMENTO COMUM**; mandado de segurança → **MANDADO DE SEGURANÇA**.
- Sigilo: **Sem Sigilo (Nível 0)**.
- Processo Originário: **nunca** preencher (só para distribuição por dependência).
- Valor da causa: **o mesmo que consta na petição inicial** ("Dá-se à causa o
  valor de R$ ..."); conferir com o PDF de cálculo.
- "Não se aplica": sempre desmarcado.
- Antes de "Próxima" (`#btnSalvar`, name `sbmProcessoEtapa1`): conferir tabela
  de documentos + campos.

## Etapa 2 de 5 — "Assuntos" (`acao=processo_cadastrar_2`)

A IA do e-proc (toggle da etapa 1) aparece AQUI: tabela `#tblSugestaoAssuntos`
"Escolha um dos assuntos sugeridos (ordem decrescente de probabilidade)", uma
`<tr>` por sugestão com o caminho completo + tooltip de glossário. No caso-escola:
1. Urbano (art. 60), Auxílio por Incapacidade Temporária, Benefícios em Espécie, DIREITO PREVIDENCIÁRIO
2. Urbana (art. 42/44), Aposentadoria por Incapacidade Permanente, ...
3. Rural (art. 59/63), Auxílio por Incapacidade Temporária, ...
4. Auxílio-Acidente (art. 86), ...
Alternativa: árvore de assuntos + busca (`#txtFiltroPesquisa`, radio
`#rdoAssunto`/`#rdoGlossario`). Painel direito "Assuntos selecionados" →
Assunto Principal (`#txtDesAssunto`) + botão "Incluir". O glossário pede
complementar com assunto de pedido genérico (Concessão, Restabelecimento...).

### Regras do Defensor para "Assuntos" (2026-10-08)

- Conferir a sugestão da IA com o pedido da inicial; no BI urbano basta a 1ª:
  **"Urbano (art. 60), Auxílio por Incapacidade Temporária, Benefícios em
  Espécie, DIREITO PREVIDENCIÁRIO"** (código **04010503**) — MESMO com pedido
  subsidiário de aposentadoria por incapacidade permanente ou auxílio-acidente.
  Nenhum assunto complementar.
- Incluir = clicar a seta verde (`img[src*="transportar.gif"]`, tooltip
  "Incluir assunto") na `<tr>` da sugestão em `#tblSugestaoAssuntos`.
- Após incluir: aparece em "Assuntos selecionados" como **Assunto Principal**
  (com X para remover) e o painel esquerdo vira "Selecionar Demais Assuntos"
  (campo "Outro Assunto"), que fica sem uso.
- TODO: aprender a regra para outras pretensões (BPC, pensão, aposentadorias,
  rural) — a sugestão nem sempre será a 1ª.

## Etapa 3 — "Partes do processo" (em JEF o fluxo vira "3 de 4")

"Próxima" da etapa 2 = `#sbmProcessoEtapa2`. URL leva `id_assunto_principal=2708`.
Parte Ré já vem fixa: INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS.
Campos (formulário JEF previdenciário):
- `txtAutorCpf` + [Consultar] → preenche `txtAutorNome` (readonly)
- `txtAutorCep` + [Consultar] → preenche `txtAutorLocalidade` (readonly)
- `txtAutorTelefone`* , `txtAutorEmail`, `chkAutorTelefoneWhatsapp`, `chkContatoAlternativo`
- radios: `rdoJusticaGratuita` (6=Sim/3=Não), `rdoPossuiRepresentante` (S/N),
  `rdoAutorInternacaoHosp` (S/N), `rdoAutorPreso` (S/N)
- `txtNumeroBeneficio`* (NB requerido)
- `selEspecialidade`* "Qual especialidade atende a patologia causadora da
  incapacidade?" — opções trazem a DATA ESTIMADA DA PERÍCIA (ex. 53=ORTOPEDISTA
  17/12/2026, 70=PSIQUIATRA 03/12/2026, 54=CARDIOLOGISTA, 52=NEUROLOGISTA,
  63=MEDICINA DO TRABALHO, 65=OFTALMOLOGISTA, 66=ONCOLOGISTA; demais "sem data")
- Procuração: `selProcuracaoPoderes` (multi: 3=Dar quitação, 7=Receber,
  10=Transigir) + área de upload
- Honorários contratuais: `rdoRetencaoHonorarioContratual` (S/N)
- Contato do advogado: `txtAdvogadoTelefone`*/`txtAdvogadoEmail`* JÁ vêm
  preenchidos com os dados do Defensor logado.

### Regras do Defensor para "Partes" (BI) — 2026-10-08

Página específica por pretensão (BI tem campos próprios: NB, especialidade).
- CPF / CEP / Telefone / E-mail: do PAJ (metadata.json → telefones/emails/
  enderecos) ou da qualificação da inicial. E-mail só se houver.
- WhatsApp e Contato alternativo: DESMARCADOS (autor e advogado).
- Justiça gratuita: Sim. Representante / Internação / Preso: Não.
- NB: do pedido PRINCIPAL da inicial, só dígitos (ex. 7313896671).
- Especialidade: a indicada na inicial (pedido de perícia "por médico
  especialista em ..."); se houver mais de uma, a de agenda mais próxima
  (data estimada aparece no texto da opção).
- Procuração: "Selecionar todos" os poderes. Honorários contratuais: Não.
- Contato do advogado: já vem preenchido (readonly).

### CPF + [Consultar] — dois caminhos
- CPF já na base do e-proc → reconhece o autor automaticamente.
- CPF NÃO cadastrado → sequência de `alert()` NATIVOS (bloqueiam a aba; no
  Playwright tratar com `page.on("dialog")` → accept):
  1. "Não existe registro de pessoa com o CPF informado."
  2. "Selecione o sexo"
  3. "Os dados informados originam-se do Cadastro de Pessoas Físicas."
  e abre o modal "Cadastro de Pessoa Física" num IFRAME `#ifrSubFrm`
  (botões Salvar / Fechar). Vem da Receita: Nome (ro), Sexo, Data de
  nascimento, Nacionalidade, UF=PR. Vazios: Estado civil, Profissão,
  Naturalidade, Mãe/Pai, Escolaridade, Identidade (tipo/nº/emissão), Endereço
  (CEP/logradouro/nº/compl/bairro/cidade), Contato (forma/contato).
  Campos-chave: selEstCivil, txtProfissao, selUfNaturalidade/
  selLocalidadeNaturalidade, txtMae, selEscolaridade, selTipoIdent/
  txtIdentPrinc/txtDataEmissao, txtCep/txtEndLog/txtEndNum/txtEndComp/
  txtBairro/selUf/selLocalidade, selTipoCont/txtContato.
  ATENÇÃO: após o Consultar, os radios da página principal mudaram
  (Justiça gratuita e Representante ficaram em "Não") — sempre (re)definir os
  radios DEPOIS do cadastro.

### Regras do Defensor para o modal "Cadastro de Pessoa Física"

Só preencher o necessário:
- Sexo (se a Receita não trouxer, atribuir pelo nome); Estado civil (da
  qualificação da inicial; senão "Não Informado" = 8; Solteiro = 2);
  Raça/Etnia "Não declarado" (já vem).
- Dependentes: nada.
- Endereço: digitar CEP em `txtCep` → lupa `a#txtImage` ("Buscar endereço pelo
  CEP") preenche logradouro/bairro/UF/cidade → completar `txtEndNum` e
  `txtEndComp` (da inicial) → `#btnIncEnd` (Incluir). Linha aparece na tabela.
- Contato: `selTipoCont` = 1 (Telefone) + `txtContato` = telefone do PAJ →
  `#btnIncCont` (Incluir).
- Salvar (botão "Salvar" no topo do iframe, sem id). Modal fecha SEM alert.
- Depois do Salvar o Nome NÃO aparece sozinho: clicar [Consultar] do CPF de
  novo → Nome preenchido (sem alerts, pessoa agora existe).

### Resto da página de Partes (feito no caso-escola)
- CEP do autor `txtAutorCep` + 2º [Consultar] → `txtAutorLocalidade` "Curitiba/PR".
- Telefone digitado só com dígitos — máscara formata "(41) 99844-3749".
- Radios via `.click()` funcionam.
- `selEspecialidade` e `selProcuracaoPoderes` são bootstrap-select (jQuery
  `selectpicker`); para poderes há botão "Selecionar Todos" dentro do
  `.bootstrap-select` (clicar nele) → Dar quitação, Receber, Transigir.
- AVISO do e-proc na especialidade: "Para mais de uma patologia ou
  especialidade diferente das disponíveis, deverá ser selecionada a
  especialidade Médico do Trabalho." (conflita com a regra "agenda mais
  próxima"). DECISÃO do Defensor: seguir a INICIAL — a especialidade é
  escolhida ao minutar; se a inicial indicar expressamente mais de uma, a de
  data mais próxima; Médico do Trabalho só quando a inicial o indicar.
- `txtHonorariosSucumbenciaisPercentual` (=10) é campo OCULTO ao usuário
  (só aparece com honorários contratuais = Sim) — ignorar.
- Procuração tem upload próprio (`fileProcuracao`) — NÃO anexar de novo (já
  vai como documento PROCURAÇÃO na etapa 1).

## ⚠️ O fluxo do BI é ESPECÍFICO (alerta do Defensor, 2026-10-08)

No benefício por incapacidade o e-proc tem um formulário próprio (assunto
04010503 → página de Partes com NB/especialidade/procuração) e PULA a etapa de
seleção do réu: o INSS já vem fixado como Parte Ré, e o wizard vira "4 de 4"
(Informações do processo → Assuntos → Partes → Informações Adicionais), em vez
dos 5 passos genéricos (que incluem uma etapa de Partes Rés).
Outras pretensões (BPC, pensão, salário-maternidade, etc.) têm fases
PARCIALMENTE DIFERENTES — ainda não mapeadas. Arquitetura do botão: um fluxo
por tipo de pretensão (o BI é o 1º), com detecção da etapa atual pelo
título da página ("Peticionamento Eletrônico (N de M) - <nome>") em vez de
assumir uma sequência fixa. Aprender os demais fluxos em casos-escola próprios.

## Etapa 4 de 4 — "Informações Adicionais" (`acao=processo_cadastrar_4`)

PONTO DE PARADA DO BOTÃO: botões `#btnSalvar` ("Finalizar") e
`#btnAssinarCertificado` ("Assinar com Certificado Digital") = DISTRIBUIÇÃO
REAL — NUNCA clicar; é o Defensor quem efetiva.
Também tem "< Retornar para Etapa Inicial" e "< Anterior".
Checkboxes:
- `chkDoencaGrave` prioridade doença grave
- `chkCautAntecTutela` Requer Liminar/Antecipação de Tutela
- `chkIntervMinisterioPublico`
- `chkIdosoVisual` prioridade idoso (DISABLED — automático pela data de nasc.?)
- `chkDeficiente`, `chkCrianca`, `chkPessoaComTEA`, `chkPessoaLei14289`
- `chkJuizoDigital` Opção por Juízo 100% Digital
- `chkDesinteresseTA` Manifesto desinteresse na Tramitação Ágil
- `chkAtendeReqLei8213_91` "A petição inicial atende aos requisitos das
  alíneas a, b e c do inciso I do art. 129-A da Lei 8.213/91" — VEM MARCADO
- `chkDemandaComplexa` processo estrutural/complexo
Abaixo: nova área de documentos (mesmo widget) + tabela dos 17 já enviados.

### Regras do Defensor para a etapa 4 (2026-10-08)
- `chkCautAntecTutela`: marcar SE a inicial tiver pedido de tutela
  antecipada/urgência (caso-escola: sim — item IV e pedido "b").
- `chkAtendeReqLei8213_91`: vem marcado — manter.
- Todas as demais: desmarcadas.
- FIM DA AUTOMAÇÃO: daqui em diante o Defensor confere e peticiona manualmente
  (Finalizar / Assinar com Certificado Digital).

### "Finalizar" abre modal de conferência (ainda NÃO distribui)
Clicar `#btnSalvar` ("Finalizar") abre o modal "Peticionamento Eletrônico —
Resumo das Informações": localidade, rito, tipo de ação (classe), sigilo,
assunto principal, partes (autor (Principal) X INSS (Principal)) e os
documentos como ícones com a sigla do tipo (INIC, RG, END, PROC, DECLPOBRE,
TERMREN, INDEFERIMENTO, PROCADM, LAUDO, FORM, EXMMED, RECEIT, PRONT, LAUDO,
CNIS, INFBEN, CALC). Pergunta "Confirmar ajuizamento do processo?" com
[Confirmar ajuizamento] (= DISTRIBUIÇÃO REAL, irreversível) e [Cancelar].
(Pendente: o Defensor decidir se o botão para ANTES de "Finalizar" ou já no
modal de conferência.)

### "Confirmar ajuizamento" → tela "Processo distribuído" (`acao=processo_finalizado`)
O modal de conferência é um IFRAME; botão `#sbmConfirmar`. Caso-escola
ajuizado de verdade a pedido expresso do Defensor (2026-10-08), ~5s:
- Nº Processo: `#lblDesNumProcesso` (link `<a>`) → 5061205-34.2026.4.04.7000
- Chave para Consulta: `#lblDesChaveConsulta` → 393774225226
- Classe, Magistrado ("EDUARDO RIVERA PALMEIRA FILHO - Juízo D do 3° Núcleo
  de Justiça 4.0 - PR"), Partes (AUTOR X RÉU) e aviso de distribuição
  (Res. 42/2019-TRF4).
- Botões: Imprimir Extrato, Enviar uma nova petição, Fechar.
### Pós-ajuizamento (regras do Defensor, 2026-10-08)
1. **SISDPU — movimentação "Petição inicial"** (fase já existe em
   `sisdpu_fases_todas`), descrição com os dados do ajuizamento (Nº Processo
   + Chave para Consulta), anexo = petição inicial em **DOCX**
   (`<PAJ>/Peças IA/peticao_inicial.docx`), tramitação para a caixa
   **AJUIZADOS** (`"01- PREV_AJUIZADOS"` em `sisdpu_caixas_todas`).
   → reusar "Ações no SISDPU" (acoes_sisdpu) — nada novo a automatizar.
2. **e-proc — Gerenciamento de Processo** para a caixa padrão
   (`eproc_procurador_padrao` = "1- DPU CTBA - DIVISÃO PREVIDENCIÁRIA
   ( DPUPREV )") → reusar o módulo de gerenciamento já existente.
Executado pelo painel no caso-escola (2026-10-08), ambos com SUCESSO:
- Ações no SISDPU (página do PAJ): chip "Petição inicial"; descrição
  "Ajuizada ação de concessão de benefício por incapacidade, com pedido de
  tutela de urgência, em face do INSS.\n\nNº Processo: ...\nChave para
  Consulta: ..."; anexo via "Adicionar Petições IA" → peticao_inicial.docx,
  Descrição "Petição inicial", Tipo "Petições"; tramitar chip
  "01- PREV_AJUIZADOS" (Sem prazo); concluir trâmite "Análise prévia" (vem
  marcado). "Executar no SISDPU" abre `confirm()` NATIVO (no fluxo
  automático usar `executar(true)`). Resultado: PAJ passou a constar só em
  "01- PREV_AJUIZADOS"; contador da caixa 53→52.
- Gerenciamento: modal global de base.html, aberto por
  `window.dispatchEvent(new CustomEvent('abrir-gerenciar-processo',
  {detail:{processo}}))`; padrões já certos (tipo "S" Substituir procurador +
  procurador padrão DPUPREV) → "Gerenciamento executado com sucesso no e-proc
  para 1 parte(s)." (~15s). Backend: POST /api/acoes-eproc/gerenciar
  {processo, tipo, procurador_valor}.
Vínculo processo↔PAJ: NÃO cadastrar — o cartório cadastra o processo
judicial no PAJ posteriormente (decisão do Defensor, 2026-10-08).

## Implementação (2026-10-08) — botão "Preparar Petição Inicial no eproc"
- `services/peticao_inicial_service.py` — plano (só leitura): arquivos de
  Ajuizamento + tipo sugerido (REGRAS_TIPO), dados extraídos do PDF da inicial
  (valor, rito/classe pelo teto de 60 SM, localidade, CPF/CEP/nº/compl.,
  estado civil, sexo, NB do pedido principal, especialidade(s), tutela,
  título da ação → descrição SISDPU) + telefone/e-mail do metadata.
- `ingestao/eproc_peticao_inicial.py` — `preparar(plano)` (para na 4/4) e
  `ajuizar()` (Finalizar + `#sbmConfirmar`, lê nº/chave). Sob `_browser_lock`.
- `routes/peticao_inicial.py` — GET plano, POST preparar, POST ajuizar
  (registra no Histórico, origem "eproc").
- `static/js/peticao_inicial.js` + `templates/partials/peticao_inicial_modal.html`
  — na seção "Movimentar Sistemas" do PAJ (só com pasta Ajuizamento). Pós:
  `/api/acoes-eproc/gerenciar` (tipo S + procurador padrão) e rascunho do
  "Ações no SISDPU" + `executar(true)` no console `#acoesSisdpuPaj`.

## Fluxo BI completo — resumo para implementação
1. Modal no painel: arquivos de `Ajuizamento/` + tipo e-proc sugerido (editável).
2. Menu Petição Inicial → garantir `#btnPredicao` ligado → upload em lote →
   tipos → `#btnEnviarArquivos`.
3. Localidade Curitiba → rito (≤60 SM JEF+renúncia / >60 SM ordinário) →
   área Previdenciária → classe (reload!) → valor da causa da inicial →
   conferir → `#btnSalvar`.
4. Assuntos: conferir 1ª sugestão da IA com a inicial → seta verde → `#sbmProcessoEtapa2`.
5. Partes: CPF+Consultar (cadastro novo se preciso: alerts + iframe) → CEP+
   Consultar → telefone/e-mail → radios → NB → especialidade → poderes
   "Selecionar Todos" → honorários Não → Próxima.
6. Informações Adicionais: tutela conforme inicial; art. 129-A mantido → PARAR.

## Fluxo GENÉRICO ("outros assuntos") — caso-escola BPC (2026-10-23)

Caso-escola: PAJ 2026/029-03147 (JFPR), restabelecimento de BPC à pessoa com
deficiência (NB 227.937.759-9), com tutela de urgência. Fluxo de 5 etapas:
Informações do processo >> Assuntos >> Partes Autoras >> **Partes Rés** >>
Informações Adicionais. O título "(N de 5)" já aparece na etapa 1 — só depois
do assunto se sabe se o e-proc vai para o formulário próprio do BI (4 etapas)
ou para o genérico (5 etapas). Detectar pela URL/título:
`acao=processo_cadastrar_3_autor` → genérico; formulário com NB/especialidade → BI.

### Etapa 1 — idêntica ao BI
Upload em lote, tipos, Curitiba, rito pelo teto de 60 SM (JEF + renúncia),
área **Previdenciária** (vale também para BPC/assistencial), classe
PROCEDIMENTO DO JUIZADO ESPECIAL CÍVEL, valor da causa da inicial
(R$ 39.060,25 aqui, mesmo com o cálculo dando R$ 39.060,29 — o Defensor manda
seguir a inicial). Lista de áreas no JEF Curitiba: Cível, Criminal,
Habitacional, Previdenciária, Saúde, Tributária.

Tipos de documento — regras novas do Defensor:
- **Nunca usar "ANEXOS DA PETIÇÃO INICIAL".**
- CadÚnico: **COMPROVANTES** (comprovante de cadastro) ou **EXTRATO** (folha resumo).
- RNM (estrangeiro): DOCUMENTO DE IDENTIFICAÇÃO.
- Carta de Concessão: CARTA DE CONCESSÃO. "PROCADM ..." = PROCESSO ADMINISTRATIVO
  (o REGRAS_TIPO atual não casa "procadm" nem "carta de concessao" — corrigir).
- Documentos médicos genéricos: LAUDO; Solicitação de Informações Médicas DPU: FORMULÁRIO.
- No Chrome, os tipos foram definidos por JS: `#lblListarTipo_<n>`.click() →
  achar `#divInfraAjaxtxtTipo_<n> li a` com o texto exato → disparar
  mouseover/mousedown/mouseup/click → `#selTipoArquivo_<n>` preenchido.

### Etapa 2 — Assuntos
A IA sugeriu uma única opção: "Pessoa com deficiência, Benefício Assistencial
(Art. 203,V CF/88), DIREITO ASSISTENCIAL" → código **140201**
(`id_assunto_principal=2311`). Regra: incluir a sugestão (seta verde) como
assunto principal, **sem complementares**, se bater com o pedido da inicial.
Árvore mostra DIREITO ASSISTENCIAL (14) e DIREITO PREVIDENCIÁRIO (04).

### Etapa 3 de 5 — Partes (autores) (`acao=processo_cadastrar_3_autor`)
Formulário genérico de consulta/inclusão: `selTipoPessoa` (ENT/PF/PJ), CPF,
"Sem CPF" (estrangeiro sem CPF / menor), Outros Documentos, Pesquisar pelo
nome, botão Consultar.
1. Tipo Pessoa = **PF**, CPF só dígitos, Consultar.
2. CPF não cadastrado → `alert()` nativo e redireciona para a PÁGINA INTEIRA
   `acao=pessoa_cadastrar&acao_retorno=processo_cadastrar_3_autor` (não é
   iframe como no BI). Mesmos ids do BI (selSexo, selEstCivil, selTipoEtnia,
   txtCep + `a#txtImage`, txtEndNum/txtEndComp + `#btnIncEnd`, selTipoCont=1 +
   txtContato + `#btnIncCont`) e mais:
   - `selNacionalidade` (padrão "Brasileira") → usar a da qualificação da
     inicial (Venezuelano = 220). Não confundir com `selPais` (país do endereço).
   - `chkPCD` + `selTipoDeficiencia` (multiselect com checkboxes): **NÃO
     marcar** (regra do Defensor — pode dar problema no fluxo futuro). No
     caso-escola ficou marcado Visual só porque já tinha sido salvo. Se marcar
     sem escolher o tipo por clique real, o Salvar dá alert pedindo o tipo;
     NÃO chamar `selectpicker('refresh')` (cria um 2º widget duplicado).
   - Salvar → alert "<NOME> salvo com sucesso." → volta à etapa 3 já com a
     busca feita ("1 pessoa localizada"): linha com Principal (Sim) + Incluir.
3. Incluir → tabela "Partes (autores) a utilizar neste ajuizamento":
   Principal? (Sim), Tipo Representação [Definir] (opcional), Inf. Extra
   [Benefício], Justiça Gratuita (select 3=Não Requerida / 6=Requerida).
   Regras do Defensor: **só Justiça Gratuita = Requerida**. Representação só
   se houver representante (curador, genitor etc.). Inf. Extra/Benefício
   (janela "Informações do Benefício": implantação sem/com pedido prévio) —
   **não preencher**.
4. Próxima = `#btnProxima`.

### Etapa 4 de 5 — Partes (Réus) (`acao=processo_cadastrar_3_reu`)
Mesmo formulário. Tipo Pessoa = **Entidade** (ENT) → `#txtEntidade`
(autocomplete; digitar "INSTITUTO NACIONAL DO SEGURO" → item
"INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS") → Principal Sim →
`#btnIncluirEnt`. A tela pode reabrir com PF selecionado e um `alert`
"Escolha uma entidade." — trocar para ENT antes. Linha: RÉU, Principal Sim.
`#btnProxima`.

### Etapa 5 de 5 — Informações Adicionais (`acao=processo_cadastrar_4`)
Diferenças para o BI: **não existem** `chkAtendeReqLei8213_91` nem
`chkDesinteresseTA`. `chkIdosoVisual` veio MARCADO e desabilitado (autor com
60 anos — automático pela data de nascimento). Aplicada a regra do BI:
`chkCautAntecTutela` se a inicial pede tutela; demais desmarcados
(inclusive `chkDeficiente`). PARAR — Finalizar/Assinar só pelo Defensor.

### Ajuizamento e pós (2026-10-23, a pedido do Defensor)
"Finalizar" abre o MESMO modal de conferência do BI (iframe, `#sbmConfirmar`)
→ `processo_finalizado`: 5065719-30.2026.4.04.7000, chave 136323796926,
10ª VF de Curitiba. Pós-ajuizamento idêntico ao do BI, pelo painel:
gerenciamento (S + DPUPREV) → rascunho "Petição inicial" + docx → revisão
SIS-DPU 2.0 (cadastro do processo, movimentação, tramitação 01- PREV_AJUIZADOS)
→ cadastro/sincronização do processo + chave no PAJ → execução ✓.

### Implementação do fluxo genérico (2026-10-23)
- Plano (`peticao_inicial_service.extrair_dados`): `fluxo` = "bi" | "generico"
  pelo título da ação ("AÇÃO DE ..."; BPC/LOAS/assistencial → genérico;
  incapacidade/auxílio-doença/invalidez/auxílio-acidente → BI); `assunto`
  (BI urbano; BPC deficiência → "Pessoa com deficiência, Benefício
  Assistencial..."; senão vazio = 1ª sugestão da IA), `reu` (INSS),
  `autor.nacionalidade`. NB/especialidade só obrigatórios no BI (rota).
- REGRAS_TIPO: sem regra → tipo VAZIO (o modal destaca e a rota exige);
  nunca ANEXOS DA PETIÇÃO INICIAL.
- Automação: `_etapa3_partes_conforme_fluxo` decide pela página (campo NB →
  BI; `processo_cadastrar_3_autor` → `_etapa3_autores_generico` +
  `_etapa4_reus_generico`). `_cadastrar_pessoa(pagina_inteira=True)` para o
  cadastro em página. Etapa final comum (`processo_cadastrar_4`); `ajuizar()`
  vale para os dois.
- NÃO TESTADO AO VIVO no Playwright do painel: id do campo CPF (achado pelo
  `<label>` "CPF:"), botão "Consultar os dados preenchidos" (pelo nome
  acessível), select "Principal?" ao lado de `#btnIncluirEnt`. Validar no
  próximo caso de outro assunto.

### Alerts nativos no Chrome (claude-in-chrome)
Cada `alert()` congela a aba para a extensão (screenshot/JS dão timeout) e o
Defensor tem de fechar manualmente. Clicar botões que podem gerar alert com
`setTimeout(() => b.click(), 100)` para a chamada não travar. No Playwright do
painel tratar com `page.on("dialog")` → accept.

## Classificação dos arquivos (1º passo do botão)

Decisão do Defensor: o botão começa LISTANDO os arquivos da pasta
`Ajuizamento` e SUGERINDO um tipo do catálogo do e-proc para cada um, num
modal igual ao do peticionamento — o usuário só ajusta o que precisar.
A sugestão vem do rótulo padronizado que a skill SISDPU-OrganizarArquivos já
grava no nome (`NN - Rótulo...`); catálogo = o já importado em
Configurações › e-proc (`tipos_todos`).

Mapeamento usado no caso-escola (IDs da JFPR):

| Rótulo da pasta Ajuizamento | Tipo e-proc | id |
|---|---|---|
| Peticao Inicial | PETIÇÃO INICIAL | 1 |
| RG e CPF | CARTEIRA DE IDENTIDADE | 8 |
| Comprovante de Residencia | COMPROVANTE DE RESIDÊNCIA | 10 |
| Outorga de Poderes DPU | PROCURAÇÃO | 2 |
| Declaracao de Insuficiencia de Recursos | DECLARAÇÃO DE HIPOSSUFICIÊNCIA/POBREZA | 59 |
| Termo de Renuncia JEF | TERMO DE RENÚNCIA | 88 |
| Comunicacao de Decisao INSS | CARTA DE INDEFERIMENTO | 327 |
| Processo Administrativo | PROCESSO ADMINISTRATIVO | 61 |
| Pericia Medica INSS | PERÍCIA | 318 |
| Formulario DPU - Solicitacao de Informacoes Medicas | FORMULÁRIO | 86 |
| Exames Medicos | EXAME MÉDICO | 89 |
| Receituarios Medicos | RECEITUÁRIO | 83 |
| Prontuario Medico | PRONTUÁRIO | 84 |
| Laudos Medicos | LAUDO | 33 |
| CNIS - Extrato Previdenciario | CADASTRO NACIONAL DE INFORMAÇÕES SOCIAIS - CNIS | 67 |
| Declaracao de Beneficios INSS | INFORMAÇÃO DE BENEFÍCIO | 50 |
| Calculo do Valor da Causa | CÁLCULOS | 504 |

Pendente de confirmação do Defensor: RG/CPF (alt. DOCUMENTO DE
IDENTIFICAÇÃO), Laudos (alt. ATESTADO MÉDICO), Perícia INSS (alt. LAUDO
PERICIAL). Casar por descrição normalizada, nunca por id fixo (ids podem
variar entre JFPR/JFSC/JFRS).
