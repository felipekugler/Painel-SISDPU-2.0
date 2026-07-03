# PAINEL SISDPU - Resumo Executivo para Especialistas

**Leitura Rápida (5 minutos):** O que você precisa saber para trabalhar eficientemente

---

## 🎯 Propósito do Projeto

**Sistema web de produtividade jurídica** para Defensores Públicos Federais.

Centraliza em um painel unificado:
- Sincronização automática da caixa SISDPU
- Extração de metadados e prazos
- OCR de documentos escaneados
- Elaboração assistida de peças (via Claude IA)
- Histórico e memória de casos

---

## 📚 Stack em Uma Linha

**Backend:** FastAPI + Uvicorn | **Frontend:** Jinja2 + DaisyUI + Alpine | **Ingestão:** Playwright + Tesseract + PyMuPDF | **IA:** Claude Code CLI

---

## 📁 Estrutura Mínima para Entender

```
app.py (entry point: FastAPI + Uvicorn 8001)
  ├─ routes/ (15 endpoints HTTP + SSE)
  ├─ services/ (22 módulos de lógica)
  ├─ ingestao/ (5 módulos: scraping, OCR, parsing)
  ├─ templates/ (15 páginas HTML, Jinja2)
  └─ static/ (CSS + JavaScript)

Data: ~/OFICIO_GERAL/PAJs/PAJ-*/
  ├─ metadata.json (estruturado)
  ├─ sisdpu.txt (bruto)
  ├─ PROMPT_MAX.md (contexto para IA)
  └─ Arquivos/*.txt (extraído com OCR)
```

---

## 🔄 Fluxo Principal (Visão de Helicóptero)

```
Clica "Sincronizar"
    ↓
Playwright loga no SISDPU
    ↓
Scrape caixa (lista PAJs)
    ↓
Para cada PAJ: detalhamento + PDFs
    ↓
OCR (Tesseract → Sonnet fallback)
    ↓
Parse → metadata.json
    ↓
[PAJ-*/] atualizado localmente
```

---

## 🛠️ Componentes Chave

| Componente | Arquivo | Responsabilidade |
|-----------|---------|-----------------|
| **Frontend** | templates/ + static/ | HTML (Jinja2) + CSS (Tailwind) + JS (Alpine) |
| **Routes** | routes/*.py | HTTP endpoints (CRUD, SSE) |
| **Services** | services/*.py | Lógica de negócio, orquestração |
| **Sync** | ingestao/sincronizador.py | Coordena scraping + OCR + parsing |
| **Client** | ingestao/sisdpu_client.py | Playwright automation (login, scrape) |
| **OCR** | ingestao/ocr.py | Tesseract + ocrmypdf + fallback Claude |
| **Parser** | ingestao/parser.py | sisdpu.txt → metadata.json (regex) |
| **Chat** | services/chat_service.py | Invoca Claude Code CLI |
| **Data** | OFICIO_GERAL/PAJs/ | Filesystem local (JSON + TXT) |

---

## 🚀 Como Começar

```bash
cd "/c/Claude/DPU/PAINEL SISDPU"

# 1. Ambiente
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. Config
copy .env.example .env
# Edite: OFICIO_GERAL, SISDPU_USERNAME, SISDPU_PASSWORD

# 3. Inicie
python app.py
# Acesse: http://localhost:8001
```

---

## 🎨 Padrões Recorrentes

### Validação de PAJ (FastAPI)
```python
from services.paj_service import PajNorm  # type alias = pattern validado

@router.get("/paj/{paj_norm}")
async def handler(paj_norm: PajNorm):  # FastAPI valida format automaticamente
    ...
```

### Async + Lock (Serialização)
```python
_sync_lock = asyncio.Lock()
async with _sync_lock:
    # Apenas 1 sync roda por vez
```

### Fallback em Cascata
```python
# OCR: ocrmypdf → Tesseract → Sonnet (visão)
def extrair_texto(pdf):
    texto = _tentar_ocrmypdf(pdf)
    if len(texto) > 100: return texto
    
    texto = _tentar_tesseract(pdf)
    if len(texto) > 100: return texto
    
    return await transcrever_com_sonnet(pdf)  # último recurso
```

### Logs em Cascata (Debugging)
```python
# 1. app.py: TimedRotatingFileHandler → logs/app.log
# 2. Cada módulo: logging.getLogger("sisdpu.busca")
# 3. Sync fails: screenshot + HTML em logs/sisdpu_debug/
```

---

## 📊 Fluxos Principais

### A. Sincronização
```
GET /api/sync (SSE)
  → sync_service.rodar_sync()
    → sincronizador.py coordena:
      - sisdpu_client.login()
      - Para cada PAJ: detalhamento + anexos
      - ocr.extrair_texto() em PDFs
      - parser.montar_metadata()
      - Grava em PAJ-*/
```

### B. Elaboração (Chat)
```
POST /api/chat/PAJ-2026-044-00311
  → chat_service.processar_instrucao()
    → prompt_builder.gerar_prompt_max()
    → subprocess invoke Claude CLI
    → Lê stream JSON (logs + resultado)
    → Persiste em PAJ-*/elaboracao.json + historico.jsonl
```

### C. Busca
```
GET /api/busca?termo=xyz
  → busca_service.buscar()
    → Carrega TODOS metadata.json
    → Busca em movimentações + Arquivos/*.txt
    → Ordena por relevância
    → Retorna top 50
```

---

## 🔐 Dados Sensíveis

**Git-ignored (IMPORTANTE):**
```
.env (credenciais)
PAJs/ (dados de assistidos)
logs/ (pode ter info pessoal)
```

Verificar `.gitignore` sempre!

---

## 🐛 Debugging Rápido

| Problema | Debug |
|----------|-------|
| Sync falha | `tail -f logs/app.log` + `ls logs/sisdpu_debug/` |
| OCR vazio | Ative `OCR_LLM_ATIVO=1` em .env |
| Claude CLI não encontrado | `npm install -g @anthropic-ai/claude` |
| "Credenciais inválidas" | Verifique SISDPU_USERNAME/PASSWORD em .env |

---

## 📈 Próximas Extensões Naturais

1. **API REST para bots externos** (telegeram, slack, etc)
2. **Integração Jurisprudência** (STF, TRF, Justiça)
3. **Notificações push** (prazos críticos)
4. **Relatórios em PDF** (estatísticas, workload)
5. **Multi-defensor** (painel compartilhado com auth)

---

## 📚 Documentação Completa

Leia na ordem:

1. **Este resumo** (você está aqui) — 5 min
2. **ANALISE_ARQUITETURA.md** — 30 min (completo)
3. **GUIA_RAPIDO_DESENVOLVIMENTO.md** — 20 min (prático)
4. **EXEMPLOS_PRATICOS.md** — 15 min (casos reais)

**Total:** ~70 minutos para dominar completamente o projeto

---

## ✅ Checklist de Competência

Você é especialista quando conseguir:

- [ ] Rodar servidor e sincronizar um PAJ
- [ ] Adicionar novo campo ao metadata.json
- [ ] Criar novo endpoint e rota
- [ ] Debugar erro de OCR usando logs
- [ ] Customizar prompt de elaboração
- [ ] Entender o fluxo completo (sync → chat → resultado)
- [ ] Adicionar novo filtro ao dashboard
- [ ] Integrar API externa (exemplo: jurisprudência)
- [ ] Escrever novo serviço seguindo padrões
- [ ] Debugar SSE streaming

---

## 🎯 Regras de Ouro

1. **Uma sincronização por vez** (asyncio.Lock em sync_service)
2. **Fallback em cascata** (ocrmypdf → Tesseract → Sonnet)
3. **Logs em cada passo** (debugging eficiente)
4. **Timeouts em operações longas** (saldeprot)
5. **Validação de input** (PajNorm type alias)
6. **Sem API paga** (Claude via CLI, sem token)
7. **Dados locais** (OFICIO_GERAL filesystem, sem DB remoto)

---

## 🚀 Ready to Code?

```bash
# Setup rápido (5 min)
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Edite .env com suas credenciais
python app.py

# Pronto! Acesse http://localhost:8001
```

---

**Você agora é um especialista no PAINEL SISDPU. Bom desenvolvimento! 🎉**

Para aprofundar: leia `ANALISE_ARQUITETURA.md` e `EXEMPLOS_PRATICOS.md`.
