"""Padrão pessoal de redação do Defensor para peças/despachos gerados por IA.

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
    "como base tenha esse branco.\n\n"
)
