# PAINEL SISDPU - Análise Completa da Arquitetura

## Documento de Especialista: Visão 360° do Projeto

**Data da Análise:** 27/06/2026  
**Versão do Painel:** 0.3.14  
**Escopo:** Análise profunda da arquitetura, fluxo de dados, componentes e padrões de desenvolvimento

---

## 📋 Índice

1. [Visão Geral](#visão-geral)
2. [Stack Tecnológico](#stack-tecnológico)
3. [Estrutura de Diretórios](#estrutura-de-diretórios)
4. [Arquitetura Geral](#arquitetura-geral)
5. [Componentes Principais](#componentes-principais)
6. [Fluxos de Dados](#fluxos-de-dados)
7. [Padrões e Convenções](#padrões-e-convenções)
8. [Como Estender](#como-estender)

---

## 📌 Visão Geral

### O que é o PAINEL SISDPU?

O **PAINEL SISDPU** é um sistema web para **Defensores Públicos Federais** que centraliza:

- **Monitoramento** de Pedidos de Assistência Jurídica (PAJs) vindos do SISDPU
- **Sincronização automática** da caixa de entrada (login + extração de movimentações)
- **Download de peças** em PDF com OCR automático
- **Detecção de prazos processuais** (regras por rito: cível, JEF, penal, administrativo)
- **Chat interativo** com Claude (IA) para triagem e elaboração de peças
- **Geração de DOCX/PDF** via scripts do defensor
- **Dashboard unificado** com filtros, watchlist e busca textual

### Objetivo Principal

Servir como **hub de produtividade jurídica**, permitindo que um defensor:
1. Sincronize sua caixa SISDPU com um clique
2. Consulte todos os PAJs em um painel web unificado
3. Elabore peças jurídicas com auxílio de IA (Claude)
4. Acompanhe prazos críticos
5. Mantenha histórico e memória de cada caso

---

## 🛠️ Stack Tecnológico

### Backend

| Tecnologia | Versão | Função |
|------------|--------|--------|
| **Python** | 3.11+ | Linguagem principal |
| **FastAPI** | ≥0.110 | Framework web assíncrono |
| **Uvicorn** | ≥0.29 | Servidor ASGI |
| **Jinja2** | ≥3.1 | Templates HTML |
| **python-dotenv** | ≥1.0 | Carregamento de env vars |

### Ingestão de Dados

| Tecnologia | Versão | Função |
|------------|--------|--------|
| **Playwright** | ≥1.40 | Automação do navegador (SISDPU login + scraping) |
| **PyMuPDF (Fitz)** | ≥1.23 | Extração de texto e metadados de PDFs |
| **Tesseract OCR** | (sistema) | OCR de documentos escaneados (português) |
| **Pillow** | ≥10.0 | Processamento de imagens |
| **pytesseract** | ≥0.3.10 | Interface Python para Tesseract |

### Frontend

| Tecnologia | Versão | Função |
|------------|--------|--------|
| **Tailwind CSS** | (CDN) | CSS utilitário |
| **DaisyUI** | 4.12.23 | Componentes UI prontos |
| **Alpine.js** | 3.14.8 | Reatividade lightweight no cliente |
| **HTMX** | 2.0.4 | AJAX sem JavaScript verboso |
| **SSE (Server-Sent Events)** | - | Streaming de logs de sincronização |

### Integração

| Tecnologia | Função |
|------------|--------|
| **Claude Code CLI** | Elaboração de peças via shell (sem API paga) |
| **SISDPU** | Sistema judicial da DPU (scrapeado via Playwright) |
| **Google Calendar API** | Opcional: sincronização de prazos |

---

## 📁 Estrutura de Diretórios

```
Painel-SISDPU/
├── app.py                      # Entrada da aplicação (FastAPI + Uvicorn)
├── config.py                   # Configurações centralizadas (variáveis de ambiente)
├── requirements.txt            # Dependências do projeto
├── pyproject.toml              # Config Ruff (linter)
├── .env                        # Variáveis de ambiente (Git-ignored)
├── .env.example                # Template do .env
├── .gitignore                  # Ignora PAJs e dados locais
│
├── routes/                     # Endpoints FastAPI (controllers)
│   ├── __init__.py
│   ├── dashboard.py            # GET / (lista PAJs) + /api/pajs
│   ├── paj.py                  # GET /paj/{paj_norm}, melhorar OCR, notas
│   ├── sync.py                 # GET /api/sync* (sincronização via SSE)
│   ├── chat.py                 # Elaboração assistida (skill-based)
│   ├── chat_livre.py           # Chat livre (sem skill específica)
│   ├── files.py                # Serving de arquivos (PDFs, .txt)
│   ├── busca.py                # Busca textual em movs + peças
│   ├── docgen.py               # Geração de DOCX/PDF
│   ├── calendar.py             # Integração Google Calendar (prazos)
│   ├── prazos.py               # Aba de prazos processuais
│   ├── watchlist.py            # PAJs em acompanhamento prioritário
│   ├── pipeline_monitor.py     # Histórico de sincronizações
│   └── configuracoes.py        # Settings da unidade (não implementado)
│
├── services/                   # Lógica de negócio (models + biz logic)
│   ├── __init__.py
│   ├── paj_service.py          # Leitura/manipulação de PAJs (metadados, peças)
│   ├── sync_service.py         # Orquestração de sincronização (async + lock)
│   ├── chat_service.py         # Comunicação com Claude CLI (stream-json)
│   ├── chat_livre_service.py   # Chat livre (persistência em historico.jsonl)
│   ├── prompt_builder.py       # Montagem dinâmica de PROMPT_MAX.md
│   ├── skills_catalog.py       # Catálogo de skills (mapeamento slug → descrição)
│   ├── ocr_llm_service.py      # OCR fallback via Claude Sonnet (visão)
│   ├── prazo_processual.py     # Cálculo de prazos (dias úteis, dobra DPU, etc)
│   ├── busca_service.py        # Full-text search em PAJs
│   ├── docgen_service.py       # Invoca gerar_docx.py / gerar_peticao.py
│   ├── watchlist_service.py    # Persistência de PAJs marcados
│   ├── calendar_service.py     # Integração Google Calendar
│   ├── pipeline_monitor_service.py  # Histórico de sincronizações (jsonl)
│   ├── cota_service.py         # Re-disparo automático após renovação de cota
│   ├── ambiente_service.py     # Verificação de dependências (Tesseract, etc)
│   ├── historico.py            # Leitura/persistência de histórico.jsonl por PAJ
│   ├── nomes_pecas.py          # Mapping tipo de peça → nomes sugeridos
│   ├── triagem_lote.py         # Triagem de PAJs para "Elaborar Todos"
│   ├── analise_ia_service.py   # Análise IA (skill PAINEL-AnalisarPAJ)
│   └── config_painel_service.py # Config persistida (painel_config.json)
│
├── ingestao/                   # Pipeline de sincronização (scraping + OCR + parsing)
│   ├── __init__.py
│   ├── sisdpu_client.py        # Cliente Playwright headless (login + listagem)
│   ├── sincronizador.py        # Orquestrador (lista caixa → baixa PDFs → monta txt)
│   ├── parser.py               # sisdpu.txt → metadata.json (regex + parsing)
│   ├── ocr.py                  # PyMuPDF + Tesseract + ocrmypdf (fallback OCR-LLM)
│   └── prazos.py               # Detecção de prazos processuais (regex)
│
├── templates/                  # Templates Jinja2 (HTML com DaisyUI)
│   ├── base.html               # Layout base (sidebar + main)
│   ├── dashboard.html          # Listagem de PAJs (filtros + watchlist)
│   ├── paj_detail.html         # Detalhe de um PAJ (abas)
│   ├── chat.html               # Chat interativo (skill-based)
│   ├── chat_livre.html         # Chat livre
│   ├── prazos.html             # Listagem de prazos
│   ├── watchlist.html          # PAJs em acompanhamento
│   ├── pipeline.html           # Histórico de sincronizações
│   ├── configuracoes.html      # Config da unidade
│   └── partials/               # Componentes reutilizáveis
│       ├── sync_modal.html     # Modal de sincronização (SSE)
│       ├── pipeline_modal.html # Modal do pipeline
│       ├── analise_modal.html  # Modal de análise IA
│       └── limpeza_modal.html  # Modal para limpar anexos
│
├── static/                     # Arquivos estáticos (CSS + JS)
│   ├── css/
│   │   └── custom.css          # Estilos customizados
│   └── js/
│       └── app.js              # JavaScript global (Alpine store, helpers)
│
├── logs/                       # Diretório de logs (app.log, rotação diária)
│   └── sisdpu_debug/           # Screenshots/HTML de debug (sync troubleshooting)
│
├── .claude/                    # Config do Claude Code (skills, memory)
│   └── download_server.py      # (gerado) Servidor para download de artefatos
│
├── .env                        # Git-ignored; preencher com suas credenciais
├── painel_config.json          # Config persistida (theme, filtros, etc)
└── stop.py                     # Script para parar o servidor
```

---

## 🏗️ Arquitetura Geral

### Visão Camadas

```
┌─────────────────────────────────────────────────────────────────┐
│                        Frontend (Browser)                        │
│  Jinja2 (HTML) + DaisyUI (CSS) + Alpine.js (reatividade)        │
│  HTMX (AJAX) + SSE (streaming logs)                             │
└─────────────────────────────────────────────────────────────────┘
                              ↕ HTTP/SSE
┌─────────────────────────────────────────────────────────────────┐
│                      FastAPI (Backend)                           │
│  Uvicorn em porta 8001                                          │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Routes (Controllers)                                    │   │
│  │ • dashboard.py → GET / (lista PAJs)                    │   │
│  │ • paj.py → GET /paj/* (detalhe + APIs)                │   │
│  │ • sync.py → GET /api/sync (sincronização)             │   │
│  │ • chat.py → POST /api/chat/* (elaboração)             │   │
│  │ • busca.py → GET /api/busca (full-text)               │   │
│  │ • ... (mais 5 rotas)                                   │   │
│  └─────────────────────────────────────────────────────────┘   │
│           ↓                                        ↓             │
│  ┌──────────────────────┐            ┌────────────────────┐    │
│  │ Services (Logic)     │            │ Ingestão (ETL)     │    │
│  │ • paj_service        │◄───────────┤ • sincronizador    │    │
│  │ • sync_service       │            │ • sisdpu_client    │    │
│  │ • chat_service       │            │ • parser           │    │
│  │ • prompt_builder     │            │ • ocr              │    │
│  │ • prazo_processual   │            └────────────────────┘    │
│  │ • ... (mais 10)      │                                       │
│  └──────────────────────┘                                       │
│           ↓                                                      │
└─────────────────────────────────────────────────────────────────┘
                              ↕ I/O
┌─────────────────────────────────────────────────────────────────┐
│                      Data Layer                                  │
│  ┌──────────────────────┬──────────────────────────────────┐   │
│  │ Filesystem (Workspace)                                 │   │
│  │ ~/OFICIO_GERAL/                                        │   │
│  │ ├── PAJs/                                              │   │
│  │ │   └── PAJ-YYYY-NNN-XXXXX/                           │   │
│  │ │       ├── sisdpu.txt (formato canonico)             │   │
│  │ │       ├── metadata.json (metadados estruturados)    │   │
│  │ │       ├── PROMPT_MAX.md (contexto para IA)          │   │
│  │ │       ├── elaboracao.json (ultima conclusao)        │   │
│  │ │       ├── historico.jsonl (trilha de eventos)       │   │
│  │ │       ├── Arquivos/ (PDFs + .txt extraído)             │   │
│  │ │       └── NOTAS.md (anotacoes do defensor)          │   │
│  │ └── Peças Feitas/ (base de conhecimento)              │   │
│  │ └── CLAUDE.md (diretrizes do defensor)                │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  Integrações Externas:                                          │
│  • SISDPU (scrapeado via Playwright)                            │
│  • Claude Code CLI (elaboração de peças)                        │
│  • Google Calendar API (opcional: prazos)                       │
│  • Tesseract OCR (extração de texto)                            │
└─────────────────────────────────────────────────────────────────┘
```

### Fluxo Principal

```
1. Defensor acessa http://localhost:8001
   └─→ dashboard.py renderiza base.html + dashboard.html
       └─→ /api/pajs carrega lista de PAJs (paj_service.listar_pajs)

2. Defensor clica "Sincronizar caixa"
   └─→ GET /api/sync (EventSourceResponse com SSE)
       └─→ sync_service.rodar_sync() (async generator de logs)
           └─→ sincronizador.py coordena:
               ├─→ sisdpu_client.login() (Playwright)
               ├─→ sisdpu_client.listar_caixa_entrada() (scraping)
               └─→ para cada PAJ:
                   ├─→ sisdpu_client.carregar_detalhamento()
                   ├─→ sisdpu_client.listar_anexos_paj() (PDFs)
                   ├─→ ocr.py extrai texto (Tesseract/ocrmypdf)
                   ├─→ sincronizador._montar_sisdpu_txt() (formato canonico)
                   └─→ parser.montar_metadata() (sisdpu.txt → metadata.json)
       └─→ Frontend (SSE listener) renderiza logs em tempo real

3. Defensor acessa /paj/PAJ-2026-044-00123
   └─→ paj_service.ler_paj() carrega dados:
       ├─→ metadata.json (metadados estruturados)
       ├─→ sisdpu.txt (movimentações)
       ├─→ prompt_builder.gerar_prompt_max() (contexto para IA)
       ├─→ Arquivos/ (PDFs + .txt extraído)
       └─→ elaboracao.json (ultima conclusao, se houver)
   └─→ paj_detail.html renderiza abas (movs, peças, chat, notas)

4. Defensor clica "Elaborar" (chat skill-based)
   └─→ POST /api/chat (streaming)
       └─→ chat_service.processar_instrucao() invoca:
           ├─→ subprocess (Claude Code CLI) com skill especifica
           ├─→ Recebe stream JSON (logs + resultado)
           └─→ Armazena em historico.jsonl + elaboracao.json
       └─→ Frontend atualiza chat em tempo real (SSE/WebSocket)
```

---

## 🎯 Componentes Principais

### 1. **PAINEL WEB (Frontend)**

**Responsabilidade:** Exibir dados dos PAJs, facilitar navegação, iniciar operações.

**Páginas principais:**

| Página | Arquivo | Função |
|--------|---------|--------|
| **Dashboard** | `dashboard.html` | Lista todos os PAJs com filtros (foro, ofício, status) + watchlist |
| **Detalhe PAJ** | `paj_detail.html` | Abas: movimentações, peças, chat, notas, prazos |
| **Chat** | `chat.html` | Elaboração assistida (skill-based) com histórico |
| **Prazos** | `prazos.html` | Calendário de prazos processuais + sync Google Calendar |
| **Watchlist** | `watchlist.html` | PAJs marcados para acompanhamento prioritário |
| **Pipeline** | `pipeline.html` | Histórico de sincronizações (logs, erros, tempos) |

**Stack front:**
- **Jinja2**: Renderização server-side (templates + filtros customizados)
- **DaisyUI**: Componentes (botões, modais, cards, badges)
- **Alpine.js**: Reatividade lightweight (x-data, x-show, x-for, listeners)
- **HTMX**: AJAX sem boilerplate (hx-get, hx-post, hx-swap)
- **SSE (Server-Sent Events)**: Streaming de logs (sincronização, chat)

**Filtros Jinja2 customizados:**
- `formatar_area`: "civel" → "Cível" (normalização de display)
- `data_br`: ISO (2023-06-22) → BR (22/06/2023)
- `data_hora_br`: ISO datetime → BR (22/06/2023 14:30)

---

### 2. **BACKEND (FastAPI)**

**Responsabilidade:** Orquestrar lógica de negócio, expor APIs, comunicar com serviços.

**Routes (Controllers):**

```
app.py
├── dashboard_router (dashboard.py)
│   ├── GET / → renderiza dashboard.html
│   ├── GET /api/pajs → JSON array de PAJs
│   └── POST /api/elaborar-lote → inicia elaboração em batch
│
├── paj_router (paj.py)
│   ├── GET /paj/{paj_norm} → renderiza paj_detail.html
│   ├── GET /api/paj/{paj_norm} → metadata + movs
│   ├── GET/POST /api/paj/{paj_norm}/notas → CRUD de notas
│   ├── POST /api/paj/{paj_norm}/melhorar-ocr → Sonnet fallback
│   └── POST /api/paj/{paj_norm}/limpar-anexos → remove PDFs
│
├── sync_router (sync.py)
│   ├── GET /api/sync → sincroniza caixa completa (SSE)
│   ├── GET /api/sync/paj/{paj_norm} → sincroniza um PAJ
│   ├── GET /api/sync/paj/{paj_norm}/anexos-desde → download seletivo
│   ├── GET /api/sync/watchlist → sincroniza PAJs em watchlist
│   └── POST /api/sync/cancel → cancela sync em progresso
│
├── chat_router (chat.py)
│   ├── POST /api/chat/{paj_norm} → elaboração via skill
│   ├── POST /api/chat/{paj_norm}/historico → historico.jsonl
│   └── GET /api/chat/{paj_norm}/status → status da elaboracao
│
├── busca_router (busca.py)
│   └── GET /api/busca → busca full-text (movs + peças)
│
├── (mais 5 routers: chat_livre, files, docgen, calendar, prazos, watchlist, pipeline)
```

**Lifecycle:**
1. **Startup** (`@app.on_event("startup")`): 
   - Carrega estado de cota persistido
   - Inicia scheduler para re-disparo automático
   
2. **Request**: FastAPI route handler → service call → response

3. **Logging**: TimedRotatingFileHandler em `logs/app.log` (rotação diária, 7 dias)

---

### 3. **SERVIÇOS (Business Logic)**

**Services** encapsulam a lógica e estados:

#### **paj_service.py**
- `listar_pajs()` → retorna lista de PAJs com metadados
- `ler_paj(paj_norm)` → carrega metadata.json + sisdpu.txt + peças
- `ler_notas(paj_norm)` → NOTAS.md
- `salvar_notas(paj_norm, texto)` → persiste NOTAS.md
- `limpar_anexos_paj(paj_norm, dry_run=False)` → remove PDFs (com confirmação)

#### **sync_service.py**
- `rodar_sync(baixar_anexos)` → async generator (SSE)
- `rodar_sync_paj(paj_norm, baixar_anexos)` → sincroniza 1 PAJ
- `rodar_sync_anexos_desde_data(paj_norm, data)` → download seletivo
- `rodar_sync_watchlist(baixar_anexos)` → sincroniza watchlist
- `solicitar_cancelamento()` → sinaliza parada
- `esta_rodando()` → verifica se há sync em execução

**Serialização:** asyncio.Lock (garantir 1 sync por vez) + asyncio.Event (cancelamento)

#### **chat_service.py**
- `processar_instrucao(paj_norm, prompt, skill_slug)` → invoca Claude CLI via subprocess
- Monta PROMPT_MAX.md com contexto completo
- Stream-json bidirecional (logs + resultado)
- Persiste em `historico.jsonl` + `elaboracao.json`

#### **prompt_builder.py**
- `gerar_prompt_max(paj_norm)` → concatena:
  - Cabeçalho estruturado
  - Últimas movimentações
  - Peças anteriores do assistido (contexto)
  - Texto completo do sisdpu.txt

#### **prazo_processual.py**
- Detecta prazos do sisdpu.txt (regex)
- Calcula vencimento:
  - Dias úteis vs. corridos
  - Dobra DPU (+5 dias)
  - Recesso forense
  - Variação por rito (cível, JEF, penal)

#### **ocr_llm_service.py**
- Fallback OCR: quando Tesseract falha (texto vazio/ilegível)
- Invoca Claude Sonnet (low effort) com visão para transcrever PDF
- Processa em blocos (OCR_LLM_CHUNK_PAGINAS) para não explodir contexto
- Resultado cacheado em `Arquivos/*.txt`

#### **Mais:**
- `busca_service.py` → full-text em movs + peças
- `docgen_service.py` → invoca scripts do defensor (gerar_docx.py)
- `watchlist_service.py` → persistência JSON de PAJs em acompanhamento
- `historico.py` → CRUD de historico.jsonl (trilha de eventos)
- `cota_service.py` → re-disparo automático após cota de uso
- `ambiente_service.py` → verificação de dependências (Tesseract, etc)

---

### 4. **INGESTÃO / SINCRONIZAÇÃO**

**Pipeline:** SISDPU → Scraping → OCR → Parsing → Filesystem

#### **sisdpu_client.py** (Playwright headless)
```
Responsabilidades:
├─ login(usuario, senha) → autentica no SISDPU
├─ listar_caixa_entrada() → scrapi HTML da caixa
│  └─ Retorna: [{ paj, assistido, oficio, status, ... }, ...]
├─ carregar_detalhamento(paj) → abre tela de detalhe
│  └─ Extrai: movimentacoes, processo_judicial, pretensao, etc.
├─ listar_anexos_paj(paj) → retorna PDFs linkados
│  └─ Retorna: [{ nome, url, data }, ...]
└─ fechar() → fecha browser
```

**Detalhes técnicos:**
- Usa Chromium via Playwright (headless mode)
- Esperador: `_WAIT_PF_AJAX` (PrimeFaces AJAX completion)
- Rate limiting: RATE_LIMIT_SISDPU_SEG (default 2s/requisição)
- Tratamento de erro: `CredenciaisInvalidas` exception
- Debug: dump de screenshots + HTML em `logs/sisdpu_debug/` quando falhas

#### **sincronizador.py** (Orquestrador)
```
Fluxo:
1. sisdpu_client.login()
2. sisdpu_client.listar_caixa_entrada()
3. Para cada PAJ (com rate limiting):
   a. sisdpu_client.carregar_detalhamento()
   b. sisdpu_client.listar_anexos_paj()
   c. Baixa PDFs (com limite MAX_ANEXOS_POR_PAJ)
   d. ocr.extrair_texto() em cada PDF
   e. _montar_sisdpu_txt() (formato canonico)
   f. parser.montar_metadata() (sisdpu.txt → metadata.json)
   g. Grava em PAJ-*/
4. sisdpu_client.fechar()
5. Emite log de progresso via emit()
```

**Formato de sisdpu.txt (canonico):**
```
PAJ: 2026/044-00311
Assistido: NOME DO CLIENTE
Status: ATIVO
Pretensão: PREVIDENCIÁRIA
Data de Abertura: 30/06/2023
Ofício: 2º OFÍCIO GERAL
PROCESSO JUDICIAL VINCULADO: 0007123-45.2023.4.01.3500 (1ª Instância)

MOVIMENTAÇÕES RELEVANTES:
[seq=1] [30/06/2023 10:15] Abertura: Cadastro de novo PAJ
[seq=2] [15/08/2023] Julgamento: Sentença favorável...
```

#### **parser.py** (sisdpu.txt → metadata.json)
```
Funções:
├─ parsear_header() → extrai PAJ, assistido, pretensao, etc. via regex
├─ parsear_movimentacoes() → extrai lista estruturada de movs
│  └─ Suporta: [DD/MM/YYYY] formato, [seq=N] prefix, etc.
├─ derivar_foro() → primeira palavra da pretensao
└─ montar_metadata() → monta dict JSON com:
   ├─ identifiers (paj, paj_norm)
   ├─ dados processuais (assistido, pretensao, processo_judicial)
   ├─ prazos (prazo_critico, dias restantes)
   └─ detalhes_sisdpu.movimentacoes (array estruturado)
```

**Saída (metadata.json):**
```json
{
  "paj": "2026/044-00311",
  "paj_norm": "PAJ-2026-044-00311",
  "assistido_caixa": "NOME",
  "pretensao": "PREVIDENCIÁRIA",
  "foro_detectado": "Previdenciario",
  "processo_judicial": "0007123-45.2023.4.01.3500",
  "data_abertura": "2023-06-30",
  "detalhes_sisdpu": {
    "movimentacoes": [
      {
        "seq": 1,
        "data": "2023-06-30",
        "descricao": "Cadastro de novo PAJ",
        "fases": "Abertura"
      },
      ...
    ]
  },
  "prazos_abertos": [...]
}
```

#### **ocr.py** (Extração de texto)
```
Pipelines (em ordem de tentativa):
1. ocrmypdf (se disponível no PATH)
   └─ Ghostscript rasterize + deskew/clean + Tesseract
   └─ Timeout: OCR_PREPROC_TIMEOUT_SEG (default 600s)

2. Tesseract direto (página por página)
   └─ Timeout: TIMEOUT_OCR_POR_PAGINA_SEG (default 30s)

3. Claude Sonnet (visão) — fallback OCR-LLM
   └─ Se ocrmypdf + Tesseract falharem (texto vazio/muito curto)
   └─ Processa em blocos: OCR_LLM_CHUNK_PAGINAS (default 40 pages)
   └─ Timeout: OCR_LLM_TIMEOUT_SEG (default 900s por bloco)
   └─ Custo: Sonnet low-effort (barato, puro OCR, não análise)
   └─ Cacheado: resultado salvo em Arquivos/*.txt

Configuração via .env:
OCR_PREPROC_ATIVO=1              # ativa ocrmypdf
OCR_PREPROC_TIMEOUT_SEG=600      # timeout (segundos)
OCR_LLM_ATIVO=1                  # ativa fallback Claude
OCR_LLM_MODELO=sonnet            # modelo (barato)
OCR_LLM_CHUNK_PAGINAS=40         # páginas por chamada
OCR_LLM_MAX_PAGINAS=0            # 0=sem limite
```

---

### 5. **DATA LAYER (Workspace)**

**Localização:** Variável de ambiente `OFICIO_GERAL` (padrão: `~/Desktop/Ofício Geral`)

**Estrutura:**
```
~/Ofício Geral/
├── PAJs/                          # Diretório principal de casos
│   ├── .ultima_sync_caixa.json    # Marcador: última sincronização (timestamp)
│   ├── PAJ-YYYY-NNN-XXXXX/        # 1 diretório por PAJ
│   │   ├── sisdpu.txt             # Texto bruto (movimentações, cabeçalho)
│   │   ├── metadata.json          # Metadados estruturados (resultado parser)
│   │   ├── PROMPT_MAX.md          # Contexto dinâmico (para IA)
│   │   ├── NOTAS.md               # Anotações pessoais do defensor
│   │   ├── elaboracao.json        # Última conclusão + status
│   │   ├── historico.jsonl        # Trilha de eventos (1 evento/linha)
│   │   ├── Arquivos/                 # PDFs baixados + .txt extraído
│   │   │   ├── documento_1.pdf
│   │   │   ├── documento_1.txt    # OCR extraído
│   │   │   ├── documento_2.pdf
│   │   │   └── documento_2.txt
│   │   ├── despacho.txt           # Peça elaborada (exemplo)
│   │   ├── peticao.txt            # Peça elaborada (exemplo)
│   │   └── ... (mais peças geradas)
│   │
│   └── PAJ-2025-044-00999/        # Outro PAJ
│
├── Peças Feitas/                  # Base de conhecimento (contexto para IA)
│   ├── peticao_anterior_1.docx
│   ├── oficio_antigo.pdf
│   └── ... (acervo pessoal do defensor)
│
├── CLAUDE.md                      # Diretrizes do defensor (CLI + painel)
│   └─ Estilos, terminologia DPU, regras de elaboração
│
├── gerar_docx.py                  # Script local do defensor
│   └─ Converte .txt → .docx
│
├── gerar_peticao.py               # Script local do defensor
│   └─ Converte .txt → PDF peção/protocolo
│
└── painel_config.json             # Config persistida do painel
    └─ Theme, filtros salvos, watchlist, etc.
```

**Fluxo de Dados:**

```
SISDPU (online)
    ↓ (scraping)
sisdpu_client.py (Playwright)
    ↓
sincronizador.py (orquestrador)
    ├─→ ocr.py (extrai texto de PDFs)
    │
    └─→ parser.py (sisdpu.txt → metadata.json)
        ↓
~/OFICIO_GERAL/PAJs/PAJ-*/
    ├── sisdpu.txt ✓
    ├── metadata.json ✓
    ├── Arquivos/*.pdf + .txt ✓
    └── elaboracao.json ✓ (quando Claude processa)
```

---

## 📊 Fluxos de Dados

### A. Sincronização (Coleta de Dados)

**Ator:** Defensor clica "Sincronizar caixa"

**Fluxo:**
```
Frontend: Click "Sincronizar"
    ↓
Route: GET /api/sync?anexos=1
    ↓
sync_service.rodar_sync(baixar_anexos=True) [async generator]
    ├─ Adquire lock (asyncio.Lock)
    ├─ Cria asyncio.Queue para SSE
    ├─ Inicia sincronizador.rodar() em thread executor
    │
    └─→ sincronizador.rodar():
        ├─ sisdpu_client.login(user, pass)
        ├─ caixa = sisdpu_client.listar_caixa_entrada()
        │  └─ Retorna array: [{ paj, assistido, oficio, ... }, ...]
        │
        ├─ Para cada PAJ (com rate limit):
        │  ├─ det = sisdpu_client.carregar_detalhamento(paj)
        │  ├─ anexos = sisdpu_client.listar_anexos_paj(paj)
        │  │
        │  ├─ Para cada anexo (até MAX_ANEXOS_POR_PAJ):
        │  │  ├─ Baixa PDF
        │  │  ├─ ocr.extrair_texto(pdf)
        │  │  │  └─ tenta ocrmypdf (ghostscript)
        │  │  │  └─ cai em Tesseract página a página
        │  │  │  └─ se ainda vazio: Sonnet + visão
        │  │  └─ Salva em Arquivos/*.txt
        │  │
        │  ├─ txt = sincronizador._montar_sisdpu_txt(caixa[paj], det)
        │  ├─ metadata = parser.montar_metadata(paj_norm, txt)
        │  ├─ Grava PAJ-*/sisdpu.txt
        │  └─ Grava PAJ-*/metadata.json
        │
        ├─ Emite logs via emit() a cada passo
        └─ sisdpu_client.fechar()
    ↓
Logs fluem via asyncio.Queue → EventSourceResponse → SSE
    ↓
Frontend: SSE listener renderiza logs em tempo real
    ↓
Ao fim: yield "done"
    └─ Frontend mostra "Sincronização finalizada ✓"
```

---

### B. Elaboração de Peças (Chat com IA)

**Ator:** Defensor clica "Elaborar" em um PAJ

**Fluxo:**
```
Frontend: Click "Elaborar" com skill "peticoes-iniciais"
    ↓
POST /api/chat/PAJ-2026-044-00311
    body: { skill: "peticoes-iniciais", prompt: "..." }
    ↓
chat_service.processar_instrucao(paj_norm, prompt, skill_slug)
    ├─ paj_data = paj_service.ler_paj(paj_norm)
    ├─ prompt_max = prompt_builder.gerar_prompt_max(paj_norm)
    │  └─ Concatena:
    │     ├─ Metadados (paj, assistido, pretensao, prazos)
    │     ├─ Últimas 8 movimentações
    │     ├─ "Memória" (conclusões anteriores se houver)
    │     ├─ Lista de peças anteriores do assistido
    │     └─ Texto completo do sisdpu.txt + Arquivos/*.txt
    │
    ├─ Invoca subprocess (Claude Code CLI):
    │  command: ["claude", "eval", "--model", ELABORACAO_MODELO,
    │            "--effort", ELABORACAO_EFFORT, "--format", "json", ...]
    │  stdin: prompt_max + instruções
    │
    ├─ Lê stream JSON do CLI:
    │  {
    │    "type": "logs",
    │    "messages": [
    │      { "level": "debug", "text": "..." },
    │      { "level": "error", "text": "..." }
    │    ]
    │  }
    │  {
    │    "type": "result",
    │    "content": "[PETICAO]\n... texto ..."
    │  }
    │
    ├─ Persiste:
    │  ├─ PAJ-*/historico.jsonl (append)
    │  │  └─ { "tipo": "elaboracao", "skill": "...", "status": "ok", "concluido_em": "...", "summary": "..." }
    │  └─ PAJ-*/elaboracao.json (sobrescreve)
    │     └─ { "status": "ok", "concluido_em": "...", "summary": "...", "comando": "..." }
    │
    └─ Retorna resultado via SSE
        ↓
Frontend: Renderiza resultado no chat modal
    └─ Oferece: "Copiar para peticao.txt", "Gerar DOCX", etc.
```

**Status de Elaboração:**
- `"ok"` → peça gerada com sucesso
- `"timeout"` → limite de tokens ou tempo atingido (re-disparo automático)
- `"erro"` → erro na execução
- `"nao_aplicavel"` → skill rejeitou (ex: caso não elegível)

---

### C. Busca Textual

**Ator:** Defensor pressiona Ctrl+K (busca global)

**Fluxo:**
```
Frontend: Ctrl+K abre modal de busca
    ↓
Usuário digita termo (≥2 caracteres)
    ↓
GET /api/busca?termo=xyz
    ↓
busca_service.buscar(termo, tipos=["movimentacoes", "arquivos"])
    ├─ Carrega TODOS os metadata.json (cache em memória)
    ├─ Busca em:
    │  ├─ metadata.detalhes_sisdpu.movimentacoes[*].descricao (FTS)
    │  └─ Arquivos/*.txt (lê arquivo, busca substring)
    ├─ Retorna: [{ paj_norm, tipo, contexto, trecho }, ...]
    │  └─ Limite: 50 resultados
    │
    └─ Ordena por relevância (fuzzy match + position)
        ↓
Frontend: Renderiza resultados (máx 10, paginação via scroll)
    └─ Click no resultado → abre PAJ + destaca termo
```

---

### D. Detecção de Prazos

**Ator:** Sistema background + Aba "Prazos"

**Fluxo:**
```
Durante sincronização:
sincronizador.rodar()
    └─→ prazos.py detectar_prazos(sisdpu_txt, foro)
        ├─ Procura regex: "PRAZO CRÍTICO: DD/MM/YYYY"
        ├─ Calcula:
        │  ├─ dias_restantes = vencimento - hoje
        │  ├─ dias_úteis (excludes weekends + feriados judiciais)
        │  ├─ dobra_dpu (se defensor é réu: +5 dias)
        │  └─ recesso (judicado)
        │
        └─ Persiste em metadata.prazos_abertos (array)

Página de prazos (/prazos):
    ├─ carrega TODOS metadata.json
    ├─ filtra: prazos_abertos com dias_restantes < 30 (customizável)
    ├─ agrupa: por data de vencimento
    └─ renderiza: tabela ordenada (vencimento mais próximo primeiro)
        └─ cores: vermelho (<7d), amarelo (7-15d), verde (>15d)
```

---

## 🎨 Padrões e Convenções

### 1. **Nomenclatura de PAJ**

**Formato normalizado:** `PAJ-YYYY-NNN-XXXXX`
- `YYYY` = ano (4 dígitos)
- `NNN` = código da unidade (3 dígitos, ex: 044 = AP)
- `XXXXX` = número do PAJ (5 dígitos, ex: 00311)

**Exemplos:**
- `PAJ-2026-044-00311` ✓ (normalizado, usado em rotas)
- `2026/044-00311` (formato SISDPU, usado em sisdpu.txt)

**FastAPI Type Annotation:**
```python
PajNorm = Annotated[str, FastApiPath(pattern=r"^PAJ-\d{4}-\d{3}-\d{5}$")]

@router.get("/paj/{paj_norm}")
async def handler(paj_norm: PajNorm):
    # FastAPI valida automaticamente; retorna 422 se inválido
```

---

### 2. **Estrutura de Rotas**

**Convenção:**
- `GET /` ou `GET /pagina` → renderiza HTML (Jinja2 + DaisyUI)
- `GET /api/*` → retorna JSON
- `POST /api/*` → operação (cria/atualiza)
- Streaming: `EventSourceResponse` (SSE) para operações longas

**Exemplo:**
```python
# routes/paj.py

@router.get("/paj/{paj_norm}", response_class=HTMLResponse)
async def paj_detail(request: Request, paj_norm: PajNorm):
    """Renderiza HTML (página completa)."""
    dados = ler_paj(paj_norm)
    template = request.app.state.jinja.get_template("paj_detail.html")
    return HTMLResponse(template.render(dados=dados))

@router.get("/api/paj/{paj_norm}", response_class=JSONResponse)
async def api_paj(paj_norm: PajNorm):
    """Retorna JSON (dados brutos, sem PROMPT_MAX grande)."""
    dados = ler_paj(paj_norm)
    return {k: v for k, v in dados.items() if k != "prompt_max"}
```

---

### 3. **Services e Dependência**

**Padrão:**
- Services são **stateless** (ou com estado controlado via locks/caches)
- Importam `config.py` para constantes
- Usam `pathlib.Path` (multiplataforma)
- Supprimem exceções defensivamente via `contextlib.suppress(Exception)`

**Exemplo:**
```python
# services/paj_service.py

from config import PAJS_DIR

def ler_paj(paj_norm: PajNorm) -> dict | None:
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return None
    
    # Carrega metadados
    meta = _ler_json(pasta / "metadata.json") or {}
    sisdpu = (pasta / "sisdpu.txt").read_text() if (pasta / "sisdpu.txt").exists() else ""
    
    return {
        "metadata": meta,
        "sisdpu": sisdpu,
        ...
    }
```

---

### 4. **Logging**

**Estratégia:**
- `logging.getLogger("modulo.submodulo")` em cada arquivo
- Configurado em `app.py`: `TimedRotatingFileHandler` (logs/app.log)
- Rotação: diária, mantém 7 dias
- Print() antigos coexistem (não redirecionados)

**Exemplo:**
```python
# ingestao/sisdpu_client.py
import logging
_log = logging.getLogger("sisdpu.busca")

async def login(usuario, senha):
    _log.info(f"Tentando login: {usuario}")
    try:
        ...
    except Exception as e:
        _log.error(f"Falha no login: {e}")
        raise
```

---

### 5. **Tratamento de Erros**

**Estratégia:**
- Exceções custom: `CredenciaisInvalidas`
- Fallbacks defensivos: ocrmypdf → Tesseract → Claude Sonnet
- Timeouts em operações longas
- JSON responses com `status_code` apropriado

**Exemplo:**
```python
# routes/sync.py

if not SISDPU_USERNAME or not SISDPU_PASSWORD:
    yield "[ERRO] credenciais SISDPU ausentes — preencha .env"
    return

try:
    await asyncio.wait_for(coro, timeout=TIMEOUT_TOTAL)
except asyncio.TimeoutError:
    yield f"[ERRO] timeout de {TIMEOUT_TOTAL}s atingido"
except asyncio.CancelledError:
    yield "[CANCELADO] sincronização interrompida"
```

---

### 6. **Async/Await e Locks**

**Padrão:**
- Sync é **serializada** (1 por vez): `asyncio.Lock` em `sync_service.py`
- Cancelamento: `asyncio.Event` (sinalização limpa)
- FastAPI routes: `async def` (event loop), services chamam sync code em thread pool (`executor`)

**Exemplo:**
```python
# services/sync_service.py

_sync_lock = asyncio.Lock()

async def rodar_sync(...) -> AsyncGenerator[str, None]:
    async with _sync_lock:
        # Apenas 1 sync roda por vez
        yield await _worker()
```

---

### 7. **Filtros Jinja2**

**Convenção:** Normalizar display sem afetar busca/comparação

```python
# app.py

def _formatar_area(valor: str | None) -> str:
    """"civel" → "Cível" (apenas exibição)."""
    if not valor:
        return ""
    return _AREA_DISPLAY.get(str(valor).lower(), valor)

def _data_br(valor) -> str:
    """ISO "2023-06-22" → BR "22/06/2023"."""
    ...

app.state.jinja.filters["formatar_area"] = _formatar_area
app.state.jinja.filters["data_br"] = _data_br
```

**Uso em templates:**
```html
{{ metadata.foro_detectado | formatar_area }}
{{ metadata.data_abertura | data_br }}
```

---

## 🔧 Como Estender

### Adicionar Nova Rota

**Exemplo: Novo endpoint `/api/paj/{paj_norm}/favoritos`**

1. **Crie `routes/favoritos.py`:**
```python
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from services.paj_service import PajNorm

router = APIRouter()

@router.post("/api/paj/{paj_norm}/favoritos")
async def marcar_favorito(paj_norm: PajNorm, ativo: bool = True):
    # Implemente a lógica
    return {"ok": True, "paj_norm": paj_norm, "favorito": ativo}
```

2. **Registre em `app.py`:**
```python
from routes.favoritos import router as favoritos_router
app.include_router(favoritos_router)
```

3. **Crie o serviço `services/favoritos_service.py`** (se necessário lógica complexa):
```python
def marcar_favorito(paj_norm: str, ativo: bool) -> bool:
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return False
    favoritos_file = pasta / "FAVORITO"
    if ativo:
        favoritos_file.touch()
    else:
        favoritos_file.unlink(missing_ok=True)
    return True
```

---

### Adicionar Nova Página

**Exemplo: Página `/estatisticas`**

1. **Crie `routes/stats.py`:**
```python
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from services.paj_service import listar_pajs

router = APIRouter()

@router.get("/stats", response_class=HTMLResponse)
async def stats(request: Request):
    pajs = listar_pajs()
    stats = {
        "total": len(pajs),
        "por_foro": {},
    }
    for paj in pajs:
        foro = paj.get("foro_detectado", "?")
        stats["por_foro"][foro] = stats["por_foro"].get(foro, 0) + 1
    
    template = request.app.state.jinja.get_template("stats.html")
    return HTMLResponse(template.render(stats=stats))
```

2. **Crie template `templates/stats.html`:**
```html
{% extends "base.html" %}
{% block title %}Estatísticas{% endblock %}

{% block content %}
<h1>Estatísticas</h1>
<div class="grid gap-4">
    <div class="card">
        <div class="card-body">
            <h3>Total de PAJs</h3>
            <p class="text-3xl font-bold">{{ stats.total }}</p>
        </div>
    </div>
    {% for foro, qtd in stats.por_foro.items() %}
    <div>{{ foro }}: {{ qtd }}</div>
    {% endfor %}
</div>
{% endblock %}
```

3. **Registre em `app.py`:**
```python
from routes.stats import router as stats_router
app.include_router(stats_router)
```

4. **Adicione nav link em `templates/base.html`:**
```html
<li>
    <a href="/stats"
       class="{% if request.url.path == '/stats' %}active{% endif %}">
        <svg>...</svg>
        Estatísticas
    </a>
</li>
```

---

### Adicionar Nova Skill (Elaboração)

**Exemplo: Skill `/triagem-urgente`**

A skill já deve existir no workspace do defensor (`OFICIO_GERAL/...`).  
O painel apenas a invoca via CLI:

```python
# services/skills_catalog.py

SKILLS = {
    "triagem-urgente": {
        "descricao": "Triagem de casos urgentes para decisão rápida",
        "aplica_quando": "PAJs sem processo judicial ou pré-processuais",
    },
    ...
}
```

Quando o defensor seleciona a skill no modal de chat:
```python
# routes/chat.py

@router.post("/api/chat/{paj_norm}")
async def chat(paj_norm: PajNorm, skill_slug: str = None, ...):
    # chat_service invoca Claude CLI com a skill
    return EventSourceResponse(_stream_chat(paj_norm, skill_slug))
```

---

### Estender OCR com Novo Pipeline

**Exemplo: Usar Google Cloud Vision em vez de Tesseract**

1. **Crie `ingestao/ocr_gcv.py` (Google Cloud Vision):**
```python
async def extrair_texto_gcv(pdf_path: Path) -> str:
    """Fallback OCR usando Google Cloud Vision."""
    from google.cloud import vision
    client = vision.ImageAnnotatorClient()
    
    # Converta PDF em imagens
    images = pdf_to_images(pdf_path)
    textos = []
    for img in images:
        response = client.document_text_detection(image=img)
        textos.append(response.full_text_annotation.text)
    return "\n".join(textos)
```

2. **Integre em `ingestao/ocr.py`:**
```python
async def extrair_texto(pdf_path: Path, nome_paj: str) -> str:
    """Pipeline de OCR com fallback GCV."""
    # 1. Tenta ocrmypdf
    texto = await _tentar_ocrmypdf(pdf_path)
    if len(texto) > 100:
        return texto
    
    # 2. Tenta Tesseract
    texto = _tentar_tesseract(pdf_path)
    if len(texto) > 100:
        return texto
    
    # 3. Tenta Google Cloud Vision (novo)
    try:
        texto = await extrair_texto_gcv(pdf_path)
        if len(texto) > 100:
            return texto
    except Exception:
        pass
    
    # 4. Fallback Claude Sonnet (último recurso)
    return await ocr_llm_service.transcrever_pdf(pdf_path)
```

---

### Adicionar Integração Externa (Ex: Dropbox)

**Exemplo: Salvar DOCX gerado automaticamente em Dropbox**

1. **Instale cliente Dropbox:**
```bash
pip install dropbox
```

2. **Crie `services/dropbox_service.py`:**
```python
import dropbox
from config import DROPBOX_ACCESS_TOKEN

def fazer_upload_docx(arquivo_local: Path, nome_remoto: str) -> bool:
    """Faz upload do DOCX para Dropbox."""
    dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)
    with open(arquivo_local, "rb") as f:
        try:
            dbx.files_upload(f.read(), f"/DPU/{nome_remoto}")
            return True
        except Exception as e:
            _log.error(f"Erro Dropbox: {e}")
            return False
```

3. **Integre em `services/docgen_service.py`:**
```python
def gerar_docx(...):
    # ... gera arquivo
    arquivo_docx = pasta / "peticao.docx"
    
    # Faz upload para Dropbox
    if DROPBOX_ACCESS_TOKEN:
        dropbox_service.fazer_upload_docx(arquivo_docx, f"{paj_norm}/peticao.docx")
    
    return {"ok": True, "arquivo": arquivo_docx}
```

---

## 📚 Padrões de Código Recorrentes

### Pattern: Cache com Invalidação

```python
# services/paj_service.py

_cache_listagem = None
_cache_timestamp = 0

def listar_pajs(incluir_concluidos=False):
    global _cache_listagem, _cache_timestamp
    
    # Valida cache (refresca a cada 30s ou ao sync)
    agora = time.time()
    if _cache_listagem and (agora - _cache_timestamp) < 30:
        return _cache_listagem
    
    # Reconstrói
    _cache_listagem = _construir_listagem()
    _cache_timestamp = agora
    return _cache_listagem

def invalidar_cache_listagem():
    global _cache_listagem, _cache_timestamp
    _cache_listagem = None
    _cache_timestamp = 0
```

### Pattern: Async Generator com Cancelamento

```python
# services/sync_service.py

async def rodar_sync(...) -> AsyncGenerator[str, None]:
    async with _sync_lock:
        _cancel_event = asyncio.Event()
        
        async def _worker():
            # Loop da sincronização
            for paj in pajs:
                if deve_cancelar():
                    raise asyncio.CancelledError()
                yield f"Processando {paj}..."
        
        try:
            await asyncio.wait_for(_worker(), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            yield "[ERRO] Timeout"
        except asyncio.CancelledError:
            yield "[CANCELADO]"
```

### Pattern: Fallback em Cascata

```python
# ingestao/ocr.py

def extrair_texto(pdf_path):
    # 1. Tenta principal
    if ferramentaPrincipal.disponivel():
        return ferramentaPrincipal.processar()
    
    # 2. Fallback 1
    if ferramentaFallback1.disponivel():
        return ferramentaFallback1.processar()
    
    # 3. Fallback 2 (IA)
    return ferramentaIAFallback.processar()
```

---

## 🚀 Deployment e Operação

### Iniciar o Servidor

```bash
cd "/c/Claude/DPU/PAINEL SISDPU"
python app.py
# ou
.venv\Scripts\python.exe app.py
```

**O que acontece:**
1. Mata processos antigos na porta 8001
2. Grava PID file (.server.pid)
3. Inicia Uvicorn em http://127.0.0.1:8001
4. Aguarda requests

### Configuração (.env)

```env
OFICIO_GERAL=C:\Users\<seu_usuario>\Desktop\Ofício Geral
OFICIO_DESCRICAO=2ª Categoria - DPU/AP
SISDPU_USERNAME=seu.usuario
SISDPU_PASSWORD=sua.senha

# OCR
OCR_PREPROC_ATIVO=1
OCR_LLM_ATIVO=1

# Elaboração (Claude)
ELABORACAO_MODELO=opus[1m]
ELABORACAO_EFFORT=xhigh

# Timeouts
TIMEOUT_TOTAL_SEG=3600
RATE_LIMIT_SISDPU_SEG=2
TIMEOUT_OCR_POR_PAGINA_SEG=30
```

---

## 📖 Resumo da Arquitetura

| Camada | Componente | Responsabilidade |
|--------|-----------|------------------|
| **Frontend** | templates/ + static/ | Renderizar HTML, reatividade, SSE |
| **Backend** | routes/ | Expor endpoints HTTP, validação |
| **Business Logic** | services/ | Orquestração, cálculos, persistência |
| **Integração** | ingestao/ | Scraping, OCR, parsing, IA |
| **Data** | OFICIO_GERAL/PAJs/ | Armazenamento local em arquivos |

**Fluxo principal:**
```
Browser → Route (FastAPI) → Service → Ingestão/Data → Response → Browser
```

---

## 🎯 Próximos Passos para Dominar o Projeto

1. **Execute um sync** completo (clique "Sincronizar caixa") para entender o fluxo E2E
2. **Elabore um PAJ** via chat para ver a integração Claude Code CLI
3. **Explore logs** em `logs/app.log` e `logs/sisdpu_debug/` para diagnosticar
4. **Adicione um novo endpoint** seguindo o padrão (rota → service → template)
5. **Customize skills** em `OFICIO_GERAL/` para seu contexto jurídico
6. **Estenda OCR** com novos pipelines conforme necessário

---

**Fim da Análise**

Este documento cobre a arquitetura completa do PAINEL SISDPU, desde as camadas técnicas até padrões de extensão. Use como referência para desenvolvimento, debugging e documentação do projeto.
