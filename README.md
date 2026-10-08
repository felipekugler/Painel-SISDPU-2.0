# Painel SISDPU

Painel web para Defensores/as Públicos/as Federais que usam o **SISDPU** — centraliza a gestão da caixa de entrada, automatiza rotinas repetitivas no SISDPU e no e-proc, e usa IA (Claude) para diagnóstico jurídico, cálculos previdenciários e minuta de peças.

> Desenvolvido para uso interno na DPU. Projeto pessoal, sem vínculo institucional oficial.

---

## O que é

Nasceu como um sincronizador da caixa de entrada do SISDPU e cresceu para um painel completo de apoio à atuação previdenciária: lê e organiza os PAJs, automatiza ações repetitivas (movimentar/tramitar/concluir, peticionar no e-proc, baixar documentos de sistemas do INSS) e aciona o Claude Code para diagnóstico jurídico, cálculos e redação — sempre com o Defensor revisando e confirmando antes de qualquer ato real.

---

## Funcionalidades

### Caixa de entrada e organização

- **Dashboard** — lista de PAJs com filtros por coluna (ofício, pretensão, data, análise), sincronização em **3 níveis** (Levíssima/Leve/Completa, com download de anexos opcional) rodando em um widget flutuante que sobrevive a troca de página/fechar a aba.
- **Watchlist** — PAJs marcados para acompanhamento prioritário, com motivo.
- **Retornos** — PAJs com tramitação de "Atendimento de retorno", com resumo e sugestão de despacho por IA.
- **Arquivos SAT** — PAJs com pendência de juntada de documentos do SAT Central (INSS/Dataprev).
- **Movimentações pendentes** e **Petições pendentes** — rascunhos de ações no SISDPU/e-proc preparados offline (inclusive para PAJ/processo ainda não sincronizado) e executados quando os sistemas estiverem disponíveis; suportam **fila de movimentações sequenciais**.
- **Análises e Iniciais** — PAJs com diagnóstico ou petição inicial da IA aguardando revisão/ajuizamento.
- **Agenda** — calendário unificando audiências, prazos finais do e-proc e compromissos/afastamentos cadastrados manualmente.
- **Histórico** — log de todas as ações reais executadas (movimentações, peticionamentos, conclusões).

### Dentro do PAJ — Ações com IA

- **Analisar PAJ** — diagnóstico jurídico estruturado, com um playbook de teses dedicado por tipo de benefício: BPC/LOAS, benefício por incapacidade, pensão por morte, auxílio-reclusão, salário-maternidade e as aposentadorias (idade, tempo de contribuição, especial, PCD, professor) — além de uma análise ampla/genérica. Também gera um **Relatório do PAJ** (síntese cronológica da tramitação).
- **Minutar Petição** — insere um modelo padrão cadastrado (sem IA) ou gera uma petição com IA a partir de documentos selecionados do processo/PAJ, salva em "Peças IA".
- **Minutar Ofício** — a IA redige um ofício e cria a minuta diretamente no processo de ofícios do setor via **MCP-SEI**.
- **Cálculos** — Tempo de Contribuição (análise competência a competência do CNIS) e Qualidade de Segurado (período de graça a partir de poucos dados), cada um gerando um PDF em "Peças IA".
- **Consultas** — Consulta de RPV/Precatório (verifica pagamento e já registra a movimentação no SISDPU), Dossiê do Seguro-Desemprego Web, Extrato de processo no e-Sisrec/CRPS, inclusão na fila de Arquivos SAT.
- **Organizar Pasta para Ajuizamento** — reorganiza os documentos do PAJ numa subpasta pronta para peticionamento.
- **Ações no SISDPU** — console de movimentar/tramitar/concluir (um item ou uma fila de vários, executados em sequência).
- **Ações no e-proc** — peticionamento real no processo judicial (duas etapas: preparar → confirmar), com **Gerenciamento de Processo** (substabelecimento) automático quando configurado.
- **Movimentar Eproc + SISDPU** — executa as duas ações acima juntas, numa única confirmação.
- **Dar mera ciência** — resposta rápida a intimações que só exigem ciência, direto da caixa.

### e-proc

- **Consultar processo** — cabeçalho, linha do tempo de eventos e intimações, lidos direto do e-proc sem abrir navegador (via componente `eproc-trf4`).
- **Prazos** — prazos judiciais detectados, com cálculo de dias úteis/ciência ficta.
- **Triagem Inteligente de Intimações** — lê as intimações abertas, classifica por tipo de ato (sentença homologatória, sentença de mérito, RPV/precatório etc.) e já prepara a manifestação cabível para revisão — nunca protocola sozinha.

### Segurança

- **Cofre de credenciais** — uma senha-mestra (nunca salva em disco) protege as credenciais de SISDPU, e-proc e SAT Central, cifradas no próprio computador. A leitura direta do e-proc (consulta/triagem) usa um cadastro à parte com TOTP.

---

## Pré-requisitos

- **Python 3.11+**
- **Tesseract OCR** (idioma `por`) — para extrair texto de PDFs escaneados.
- **Microsoft Word** — gera/converte petições e minutas em PDF.
- **Claude Code CLI**, com login ativo — motor de toda a análise, cálculo e elaboração de peças por IA.
- **Playwright + Chromium** — instalado automaticamente pelo instalador.

---

## Instalação

```bash
# 1. Clone o repositório
git clone https://github.com/felipekugler/Painel-SISDPU.git
cd Painel-SISDPU

# 2. Crie o ambiente virtual (requer Python 3.11+)
python -m venv .venv            # Windows
# python3.12 -m venv .venv      # macOS/Linux (use o 3.11+ do Homebrew; o python3 do sistema no macOS é 3.9)
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# 3. Instale as dependências
pip install -r requirements.txt

# 4. Instale o Chromium do Playwright
python -m playwright install chromium

# 5. Instale o Tesseract OCR (idioma "por")
#   Windows: baixe em https://github.com/UB-Mannheim/tesseract/wiki
#            marque "Portuguese"; caminho padrao C:\Program Files\Tesseract-OCR\
#   macOS:   brew install tesseract tesseract-lang
#   Linux:   sudo apt install tesseract-ocr tesseract-ocr-por
# Verifique (precisa estar no PATH):
tesseract --version

# 6. Configure o .env
copy .env.example .env       # Windows
# cp .env.example .env       # macOS/Linux
# Edite .env e preencha OFICIO_GERAL (pasta onde ficam os PAJs)
```

> Em Windows, o pacote `Instalação.zip` (gerado à parte deste repositório) automatiza os passos acima via `INSTALAR.bat`, e inclui também o componente de leitura direta do e-proc.

---

## Configuração

Edite o arquivo `.env`:

```env
OFICIO_GERAL=C:\Users\<seu_usuario>\Desktop\Ofício Geral
# macOS/Linux: OFICIO_GERAL=/Users/<seu_usuario>/Desktop/Ofício Geral
OFICIO_DESCRICAO=2ª Categoria - DPU/UF
```

`OFICIO_GERAL` aponta para a pasta onde ficam `PAJs/` e as peças geradas. `OFICIO_DESCRICAO` é apenas o texto exibido na sidebar (cosmético).

As credenciais do SISDPU, e-proc e SAT Central são cadastradas em **Configurações → Cofre de credenciais**, protegidas por uma senha-mestra escolhida por você (nunca salva em disco). O `.env` ainda aceita `SISDPU_USERNAME`/`SISDPU_PASSWORD` como alternativa legada, mas o cofre é o caminho recomendado.

---

## Uso

```bash
# Iniciar o servidor (porta 8001)
.venv\Scripts\python.exe app.py     # Windows
# .venv/bin/python app.py           # macOS/Linux

# Acessar no navegador
# http://localhost:8001
```

O painel mostra todos os PAJs detectados em `OFICIO_GERAL/PAJs/PAJ-*/`. Veja a seção **Funcionalidades** acima para o que cada parte faz.

---

## Estrutura

```
Painel-SISDPU/
├── app.py              # FastAPI + Uvicorn
├── config.py           # Configurações (lê .env)
├── ingestao/           # Clientes externos: SISDPU/e-proc/SAT/SEI (Playwright/HTTP)
├── routes/             # Endpoints (dashboard, PAJ, prazos, intimações, e-proc, SISDPU...)
├── services/           # Lógica de negócio (IA, cálculos, cofre, histórico...)
├── templates/          # HTML (Jinja2 + DaisyUI + Alpine.js)
├── static/             # CSS e JS estáticos
└── requirements.txt
```

---

## Workspace Ofício Geral

Este painel lê os dados da pasta apontada por `OFICIO_GERAL`, que contém `PAJs/PAJ-YYYY-NNN-XXXXX/` — uma subpasta por PAJ (NNN = código de 3 dígitos da unidade) com `sisdpu.txt`, `metadata.json`, `Arquivos/`, "Peças IA/" e demais arquivos gerados.

---

## Tecnologias

- **Backend:** FastAPI + Uvicorn
- **Frontend:** DaisyUI (Tailwind CSS) + Alpine.js
- **Automação:** Playwright (SISDPU/e-proc/SAT/SEI), PyMuPDF, Tesseract OCR
- **IA:** Claude Code via CLI — sem API paga
- **e-proc headless:** componente `eproc-trf4` (HTTP direto, sem navegador)

---

## Aviso

Este projeto foi desenvolvido para uso pessoal por um Defensor Público Federal. Cada usuário é responsável, por sua conta e risco, do uso que for feito, sob sua exclusiva responsabilidade. O sistema funciona, mas ainda está em desenvolvimento e testes, por isso erros podem acontecer, sendo igualmente de responsabilidade do próprio usuário.

Ele **não armazena dados de assistidos** — os arquivos de processos ficam apenas na máquina local do Defensor e são ignorados pelo Git. Consulte o `.gitignore` para confirmar o que é e não é versionado.

Contribuições são bem-vindas. Abra uma issue ou PR.

---

## Licença

[MIT](LICENSE) — uso livre, sem garantias.
