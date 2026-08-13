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
    "protocolo/expedição (nome do(a) Defensor(a) assinante, dado presumido/"
    "não confirmado, qualquer placeholder) vai entre colchetes `[ ]` no "
    "texto. Se você mesmo gerar o `.docx` final nesta sessão (não apenas o "
    "`.txt`), esse trecho entre colchetes deve SAIR EM FONTE VERMELHA — "
    "aplique um pós-processamento no `Document` do python-docx (varrer os "
    "runs, colorir de vermelho `RGBColor(0xFF,0,0)` só o trecho entre "
    "colchetes, preservando fonte/tamanho/negrito do resto do run) ANTES de "
    "salvar. Se o `.docx` for gerado depois por outra etapa do painel (fora "
    "desta sessão), deixe os colchetes no `.txt` mesmo assim — são o sinal "
    "de que aquele trecho precisa de conferência.\n\n"
)
