r"""Padrão pessoal de redação do Defensor para peças/despachos gerados por IA.

Regras que NÃO estão em nenhum playbook público (ex.: `teses-dpu`) nem em
`CLAUDE.md` do workspace Ofício Geral — que, neste painel (OFICIO_GERAL =
PREV4-Painel), nem sequer existe como arquivo. Por isso ficam centralizadas
AQUI, em código, como única fonte de verdade compartilhada pelos dois motores
de elaboração do painel:

- `chat_service.py` (botão "Elaborar" — genérico, qualquer skill do catálogo
  ou "Claude decide", produz só o `.txt`; o `.docx` é gerado depois por um
  script externo separado, quando existir).
- `analise_pretensao_service.py` (botão "Analisar" — diagnóstico por
  pretensão + reenvio; quando avança pra petição, o próprio Claude roda o
  pipeline `teses-dpu` e gera o `.docx` na mesma sessão).

Motivo de existir (bug real, PAJ 2026/029-01486 e 2026/029-01789, 05-06/08/
2026): a primeira vez que o "Analisar" avançou pra redação, a peça saiu fora
do padrão (travessão em frase corrida, Fatos misturando miserabilidade/
deficiência, colchete de conferência sem fonte vermelha) porque esse padrão
vivia só na memória privada da conversa interativa, inacessível ao
subprocesso `claude -p` que o painel dispara. Corrigido primeiro só no
"Analisar"; replicado aqui pro "Elaborar" pra não depender de qual botão o
Defensor clicou.

Itens j-o (30/09/2026, PAJ 2026/029-02667): o Defensor corrigiu manualmente
uma petição gerada pela IA por estar "massante, cheia de detalhes
irrelevantes, texto pesado, detalhista, sem dar enfoque principal no
cumprimento dos requisitos legais" — comparação linha a linha entre o
rascunho da IA e a versão corrigida (35 mil → 16 mil caracteres, mais de
55% de corte) e com os modelos de referência do setor
(`C:\Claude\DPU\Modelos\Benefício Assistencial\Modelo - BPC Idoso.doc` e
`...\Modelo - BPC Deficiente.doc`) mostrou um padrão claro: a IA tende a
narrar a cronologia administrativa despacho por despacho, multiplicar teses
subsidiárias/preventivas para argumentos que a autarquia nem levantou, dar
tópico de Direito fundamentado a preliminares processuais de rotina, e
"fazer a conta" (per capita, dedução de gastos) com dados parciais em vez
de deixar a quantificação para a perícia. Os modelos do setor fazem o
oposto: tese abstrata pode ser longa e citar lei/jurisprudência na íntegra,
mas a aplicação ao caso concreto é sempre curta.
"""

from __future__ import annotations

INSTRUCAO_ESTILO_PECA_DPU = (
    "PADRÃO DE REDAÇÃO PESSOAL DESTE DEFENSOR (regras que NÃO estão em "
    "nenhuma skill/playbook nem em CLAUDE.md — sigam à risca mesmo que o "
    "modelo/skill usado sugira algo diferente):\n"
    "  a. PROIBIDO usar travessão (—) como pontuação de aposto/parêntese em "
    "qualquer frase (ex.: \"o requisito — que é incontroverso —, não foi...\"). "
    "Reescreva com vírgula ou parênteses. Travessão só é aceitável como "
    "numeração de tópico (\"I — DOS FATOS\") ou traço de citação "
    "bibliográfica, nunca dentro de uma frase corrida.\n"
    "  b. Se a peça tiver um tópico \"Dos Fatos\" (petição, recurso, "
    "manifestação etc.): ele contém SOMENTE a causa de pedir objetiva "
    "(requerimento/decisão do INSS ou CRAS e a cronologia processual). NÃO "
    "inclua dados de renda/composição familiar (miserabilidade) nem de "
    "saúde/contexto social (deficiência/incapacidade) nos Fatos — esses "
    "dados ficam 100% concentrados no respectivo tópico de Direito, onde "
    "podem cruzar com dados pessoais/sociais para a análise cabível.\n"
    "  c. Cada tópico de Direito segue o padrão: tese abstrata primeiro "
    "(doutrina/lei/jurisprudência em termos gerais), seguida da transição "
    "explícita 'No caso concreto, ...' aplicando a tese aos fatos deste "
    "assistido — não pule direto para o caso concreto sem antes assentar a "
    "tese em abstrato.\n"
    "  d. Todo trecho que precise de CONFERÊNCIA MANUAL do Defensor antes do "
    "protocolo/expedição (dado presumido/não confirmado, qualquer outro "
    "placeholder) vai entre colchetes `[ ]` no texto — EXCETO nome do(a) "
    "Defensor(a) assinante e foro/endereçamento, que têm valor padrão fixo e "
    "são preenchidos direto, sem colchete (ver itens e/f/g abaixo). Se você "
    "mesmo gerar o `.docx` final nesta sessão (não apenas o `.txt`), esse "
    "trecho entre colchetes deve SAIR EM FONTE VERMELHA — aplique um "
    "pós-processamento no `Document` do python-docx (varrer os runs, "
    "colorir de vermelho `RGBColor(0xFF,0,0)` só o trecho entre colchetes, "
    "preservando fonte/tamanho/negrito do resto do run) ANTES de salvar. Se "
    "o `.docx` for gerado depois por outra etapa do painel (fora desta "
    "sessão), deixe os colchetes no `.txt` mesmo assim — são o sinal de que "
    "aquele trecho precisa de conferência.\n"
    "  e. Nome do(a) Defensor(a) assinante: **sempre \"Felipe Belache "
    "Kugler\"** — preencher direto onde a peça/despacho pedir o nome de "
    "quem assina (ex.: rodapé da petição, campo `[NOME DO(A) DEFENSOR(A)]` "
    "de qualquer modelo), nunca deixar em colchete nem pedir conferência "
    "(só ele usa este painel, orientação de 24/08/2026). Se um dia outro "
    "Defensor passar a usar o painel, esta regra precisa voltar a ser "
    "colchete/pergunta em vez de valor fixo.\n"
    "  f. Foro de endereçamento (juízo da petição inicial/peça judicial): "
    "**sempre a Subseção Judiciária de Curitiba/PR** — tanto no cabeçalho "
    "de endereçamento (\"AO JUÍZO ... DA SUBSEÇÃO JUDICIÁRIA DE...\") quanto "
    "no local do fecho (\"[CIDADE/UF], [DATA]\"), mesmo que o assistido "
    "resida em outro município (orientação de 24/08/2026: é a prática "
    "vigente deste Defensor, não uma inferência a partir do endereço do "
    "assistido). Preencher direto, sem colchete. Só usar um foro diferente "
    "se isto tiver sido informado EXPRESSAMENTE antes desta elaboração "
    "(nesta conversa, ou registrado no PAJ) — não deduzir outro foro por "
    "conta própria a partir de CEP/endereço/dados do processo.\n"
    "  g. Endereçamento SEM número de vara: como a distribuição é aleatória "
    "e não se sabe de antemão qual vara/juízo vai receber o feito, o "
    "cabeçalho de endereçamento NUNCA indica número de vara — nem no "
    "Juizado Especial Federal, nem na Justiça Federal comum. Usar sempre a "
    "forma genérica (ex.: \"AO JUÍZO FEDERAL DA SUBSEÇÃO JUDICIÁRIA DE...\" "
    "ou \"AO JUÍZO DO JUIZADO ESPECIAL FEDERAL DA SUBSEÇÃO JUDICIÁRIA "
    "DE...\", conforme o rito cabível — JEF ou comum), nunca \"___ª Vara\"/"
    "\"1ª Vara\" ou qualquer número específico, mesmo que o modelo usado "
    "como base tenha esse branco.\n"
    "  h. Ordem do tópico \"Dos Pedidos\" (petição inicial — vale também pra "
    "outras peças judiciais com lista de pedidos): sempre nesta sequência, "
    "PULANDO os itens que não se aplicarem ao caso (sem alterar a ordem "
    "relativa dos que restarem):\n"
    "     1. gratuidade de justiça;\n"
    "     2. tramitação prioritária, quando cabível (idoso, PCD, ou outra "
    "hipótese legal — art. 1.048 do CPC);\n"
    "     3. tutela de urgência/liminar;\n"
    "     4. citação da parte ré (e entrega da documentação administrativa "
    "que possuir sobre a causa, se for o caso);\n"
    "     5. pedidos preliminares, se houver (o que não for citação/liminar "
    "mas precede o mérito — ex.: produção de documento específico);\n"
    "     6. pedidos de MÉRITO, agrupados num único item ou item+subitens "
    "(condenação a conceder/restabelecer o benefício, pagamento das "
    "prestações vencidas, conversão em benefício mais gravoso se cabível);\n"
    "     7. pedidos de PROVA por último, sempre nesta ordem interna: "
    "pericial (com a especialidade médica sugerida) → avaliação social, se "
    "cabível → testemunhal/documental/demais provas em Direito admitidas.\n"
    "  i. Negrito (marcador `**...**`) nas expressões-chave de cada item de "
    "\"Dos Pedidos\", sempre que aparecerem: o nome do instituto do próprio "
    "item (**gratuidade da justiça**, **tramitação prioritária**, **tutela "
    "de urgência**, **citação**); o verbo do pedido de mérito "
    "(**conceder**/**restabelecer**); dados técnicos do benefício com seus "
    "valores (**NB [número]**, **DER [data]**, **DCB [data]**, **DIB "
    "[data]**); a frase **pagamento das prestações vencidas**; a "
    "especialidade médica da perícia (ex.: **médico especialista em "
    "psiquiatria**); e o valor da causa por extenso, quando já resolvido "
    "(ex.: **R$ 27.235,62 (vinte e sete mil, duzentos e trinta e cinco "
    "reais e sessenta e dois centavos)**). Se ainda for usar o marcador "
    "`{{VALOR_CAUSA}}` (ver `INSTRUCAO_VALOR_CAUSA`) por não ter o valor "
    "calculado nesta sessão, negrite o próprio marcador "
    "(`**{{VALOR_CAUSA}}**`) — o painel troca só o texto do trecho ao "
    "injetar o valor real depois, preservando o negrito ao redor.\n"
    "  j. ENXUTEZ é regra geral, não só do tópico Fatos: parágrafos curtos, "
    "sem redundância (a mesma informação não se repete em dois parágrafos "
    "ou tópicos diferentes) e sem reforço retórico (adjetivação, ênfase "
    "repetida, \"cabe registrar\"/\"cumpre destacar\"/\"é de se notar\" como "
    "muletas). Compare sempre com o padrão real desta Defensoria em "
    "`C:\\Claude\\DPU\\Modelos\\Benefício Assistencial\\` (Modelo - BPC "
    "Idoso/Deficiente): são peças enxutas, onde a tese abstrata (lei/"
    "doutrina/jurisprudência) pode ser longa e literal, mas a aplicação ao "
    "caso concreto é sempre curta e direta — 1 a 3 parágrafos, não uma "
    "reconstituição forense de cada documento.\n"
    "  k. \"Dos Fatos\" tem de 3 a 6 frases, sem exceção (regra endurecida "
    "após correção real do Defensor no PAJ 2026/029-02667, 30/09/2026, que "
    "cortou uma narrativa de 8 parágrafos com despacho por despacho para "
    "isto): (i) o benefício/direito que a parte tinha ou pleiteou; (ii) o "
    "que o INSS/CRAS decidiu (indeferiu/cessou) e o fundamento em UMA frase "
    "resumida, nunca a transcrição do despacho; (iii) a consequência lógica "
    "(recurso administrativo pendente e/ou o ajuizamento). NÃO narre a "
    "cronologia administrativa passo a passo (cada despacho com seu número "
    "e data, cada intimação, cada comparecimento à agência) — cite datas "
    "só quando tecnicamente indispensáveis (DER, DCB, DIB, NB) e NUNCA "
    "reproduza blocos de citação literal do INSS nos Fatos (citação "
    "literal de norma/jurisprudência é conteúdo do tópico de Direito, não "
    "dos Fatos).\n"
    "  l. UMA tese central por requisito controvertido — nada de blindagem "
    "preventiva. Desenvolva, no corpo da petição, só a(s) tese(s) que "
    "sustentam o requisito que EFETIVAMENTE fundamentou a decisão "
    "administrativa impugnada. NÃO crie um tópico de Direito subsidiário "
    "só para prevenir um argumento que a autarquia sequer levantou (ex.: um "
    "tópico inteiro \"do não cômputo do Bolsa Família\" quando o próprio "
    "despacho do INSS não computou Bolsa Família; um tópico \"da nulidade "
    "por ausência de motivação\" paralelo à tese de mérito já suficiente; "
    "um tópico só pra registrar o que a parte \"não\" está pedindo). Se, "
    "ao escrever, você perceber que só está incluindo um tópico \"para "
    "prevenir\" ou \"por cautela\", é sinal de cortá-lo, não de escrevê-lo "
    "com a ressalva.\n"
    "  m. Preliminares processuais NÃO viram tópico de Direito fundamentado "
    "— só a gratuidade da justiça continua como preliminar (uma frase "
    "curta: fundamento legal + \"por ser juridicamente necessitada/pessoa "
    "hipossuficiente\"). Competência do juízo, tramitação prioritária, "
    "prerrogativas da DPU (intimação pessoal, prazo em dobro, dispensa de "
    "procuração) e interesse de agir/desnecessidade de exaurimento da via "
    "administrativa vão DIRETO para \"Dos Pedidos\" (ver item h), sem tese "
    "dedicada nem fundamentação própria — a menos que o caso concreto tenha "
    "uma controvérsia REAL sobre um desses pontos (ex.: o INSS alega falta "
    "de prévio requerimento administrativo), hipótese em que o ponto "
    "controvertido específico pode, aí sim, merecer um parágrafo de Direito.\n"
    "  n. Subsunção ao caso concreto = fatos essenciais, não perícia "
    "documental. Ao aplicar a tese ao caso, cite só os fatos que sustentam "
    "o requisito (idade, renda, doença + CID, o documento que comprova), "
    "SEM: (i) identificar cada documento por número de protocolo/anexo/ID/"
    "página; (ii) fazer do zero, na petição, o cálculo aritmético de renda "
    "per capita ou de dedução de gastos com valores estimados ou parciais "
    "— prefira o fato qualitativo (\"o núcleo familiar arca com gastos de "
    "saúde não cobertos pelo SUS que comprometem a renda disponível\") e "
    "deixe a quantificação exata para a perícia/avaliação social já "
    "requerida, a menos que o dado seja seguro e o cálculo indispensável "
    "à tese; (iii) listar item a item cada medicamento/insumo com marca, "
    "posologia e valor quando um resumo qualitativo do quadro clínico "
    "basta; (iv) repetir o mesmo fato em mais de um parágrafo ou tópico.\n"
    "  o. NUNCA embuta dúvida ou ressalva de conferência no MEIO de um "
    "parágrafo argumentativo (ex.: \"...conforme o Anexo I da Portaria X "
    "[conferir: não consta no PAJ]...\"). Se o dado é verificável por você "
    "mesmo (texto de lei, súmula, portaria, tema de repercussão geral), "
    "PESQUISE e cite corretamente — não deixe placeholder para algo que "
    "você tem como confirmar sozinho. Se o dado é específico deste caso e "
    "você não tem certeza dele, e ele NÃO é indispensável à tese central, "
    "é preferível OMITIR essa alegação específica a incluí-la com uma "
    "ressalva que compromete a credibilidade da peça perante o juízo. "
    "Reserve o colchete de conferência (item d) para dados pontuais e "
    "isolados de preenchimento factual (nome, data, valor definitivo) — "
    "nunca para uma frase inteira de dúvida jurídica ou factual dentro da "
    "argumentação.\n"
    "  p. Datas e prazos SEMPRE em algarismos, nunca por extenso: data "
    "completa em DD/MM/AAAA (\"15/01/2025\", nunca \"15 de janeiro de "
    "2025\"); mês e ano em MM/AAAA (\"01/2025\", nunca \"janeiro de 2025\" "
    "ou \"outubro de 2026\"); prazos e períodos com o número em algarismos "
    "(\"30 dias\", \"90 dias\", \"12 meses\", \"2 anos\", nunca \"trinta "
    "dias\"/\"noventa dias\"). Única exceção: o extenso entre parênteses "
    "logo depois do algarismo (\"30 (trinta) dias\"). Citação literal de "
    "lei/jurisprudência mantém o texto original.\n\n"
)
