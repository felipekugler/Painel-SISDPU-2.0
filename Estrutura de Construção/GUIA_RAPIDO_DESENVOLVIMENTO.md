# PAINEL SISDPU - Guia Rápido para Desenvolvimento

**Documento Complementar:** Referência rápida para desenvolvedores

---

## 📋 Checklist Rápido

### Antes de Começar
- [ ] Clone o repositório: `git clone <repo>`
- [ ] Configure `.env` com credenciais SISDPU
- [ ] Crie venv: `python -m venv .venv`
- [ ] Instale deps: `pip install -r requirements.txt`
- [ ] Instale Playwright: `python -m playwright install chromium`
- [ ] Instale Tesseract OCR (Windows: exe installer, macOS: `brew install tesseract`)
- [ ] Inicie servidor: `python app.py`
- [ ] Acesse: http://localhost:8001

### Desenvolvimento
- [ ] Faça alteração no código
- [ ] **SEM reload automático** (reload=False em Uvicorn)
- [ ] Reinicie servidor (`stop.py` ou Ctrl+C)
- [ ] Teste em http://localhost:8001

---

## 🎯 Tarefas Comuns

### 1. Debugar Sincronização

**Problema:** Sync falha silenciosamente

**Solução:**
```bash
# Verifique o arquivo de log
tail -f logs/app.log

# Se há erro no SISDPU, veja screenshot em
ls logs/sisdpu_debug/
# → busca por HTML/screenshot mais recentes

# Teste credenciais
python -c "
from ingestao.sisdpu_client import login
import asyncio
asyncio.run(login('seu.usuario', 'sua.senha'))
"
```

---

### 2. Adicionar Novo Campo ao PAJ

**Fluxo:**

1. **Extraia do SISDPU** (em `sincronizador.py` ou `parser.py`):
```python
# ingestao/parser.py

HEADER_KEYS = {
    "paj": re.compile(r"^PAJ\s*:\s*(.+)$", re.IGNORECASE),
    "meu_novo_campo": re.compile(r"^Meu Campo\s*:\s*(.+)$", re.IGNORECASE),  # NOVO
}

def parsear_header(linhas):
    header = {}
    for linha in linhas:
        for key, regex in HEADER_KEYS.items():
            m = regex.match(linha.strip())
            if m and key not in header:
                header[key] = m.group(1).strip()
    return header
```

2. **Adicione ao metadata.json** (em `parser.py`):
```python
def montar_metadata(paj_norm, texto_sisdpu):
    # ...
    meta = {
        "paj": paj_id,
        "meu_novo_campo": header.get("meu_novo_campo", ""),  # NOVO
        # ... resto
    }
    return meta
```

3. **Exiba no template**:
```html
<!-- templates/paj_detail.html -->
<div class="badge">{{ dados.metadata.meu_novo_campo }}</div>
```

---

### 3. Adicionar Novo Filtro ao Dashboard

**Fluxo:**

1. **Adicione a lógica em `paj_service.py`**:
```python
def listar_pajs(incluir_concluidos=False, filtro_novo=None):
    # ... carrega PAJs ...
    
    if filtro_novo:
        pajs = [p for p in pajs if p.get("meu_novo_campo") == filtro_novo]
    
    return pajs
```

2. **Adicione rota em `routes/dashboard.py`**:
```python
@router.get("/api/pajs", response_class=JSONResponse)
async def api_pajs(concluidos: int = 0, filtro_novo: str = None):
    pajs = listar_pajs(
        incluir_concluidos=bool(concluidos),
        filtro_novo=filtro_novo  # NOVO
    )
    return {"pajs": pajs, ...}
```

3. **Adicione UI em `templates/dashboard.html`**:
```html
<select x-model="filtros.meu_novo" @change="carregar_pajs()">
    <option value="">Todos</option>
    <option value="valor1">Valor 1</option>
    <option value="valor2">Valor 2</option>
</select>

<!-- No endpoint da API -->
url: `/api/pajs?filtro_novo=${filtros.meu_novo}`
```

---

### 4. Integrar com API Externa (ex: Jurisprudência)

**Fluxo:**

1. **Crie novo serviço**:
```python
# services/jurisprudencia_service.py

import httpx

async def buscar_jurisprudencia(paj_norm: str, termos: list[str]) -> list[dict]:
    """Busca jurisprudência relacionada ao PAJ."""
    async with httpx.AsyncClient() as client:
        # Ex: busca em STF, TRF, etc
        resultados = []
        for termo in termos:
            resp = await client.get(
                "https://api.stf.jus.br/jurisprudencia",
                params={"q": termo}
            )
            resultados.extend(resp.json()["resultados"])
        return resultados
```

2. **Exponha via rota**:
```python
# routes/paj.py

@router.get("/api/paj/{paj_norm}/jurisprudencia")
async def api_jurisprudencia(paj_norm: PajNorm):
    from services import jurisprudencia_service
    dados = ler_paj(paj_norm)
    termos = [dados["metadata"]["pretensao"]]  # ex
    resultados = await jurisprudencia_service.buscar_jurisprudencia(paj_norm, termos)
    return {"resultados": resultados}
```

3. **Consume no frontend**:
```javascript
// static/js/app.js
async function carregar_jurisprudencia(paj_norm) {
    const resp = await fetch(`/api/paj/${paj_norm}/jurisprudencia`);
    const data = await resp.json();
    // renderiza na aba
}
```

---

### 5. Customizar Prompt de Elaboração

**Fluxo:**

1. **Edite diretrizes em `services/chat_service.py`**:
```python
_DIRETRIZES_PROFUNDIDADE = (
    "## Como trabalhar este PAJ\n"
    "1. Analise a fundo\n"
    "2. Seu critério novo aqui\n"  # NOVO
    "3. Leia TODOS os anexos\n"
)
```

2. **Ou customize via `OFICIO_GERAL/CLAUDE.md`**:
```markdown
# Diretrizes para Elaboração

## Estilo
- Use linguagem formal
- Respeite convenções da DPU
- Cite jurisprudência quando relevante

## Seu customization aqui
...
```

O prompt_builder carrega CLAUDE.md e o injeta no contexto.

---

## 🔍 Debugging Avançado

### SSE Streaming (Sincronização em Tempo Real)

**Verifique conexão no navegador:**
```javascript
// Dev Tools Console
fetch('/api/sync')
    .then(resp => {
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        return reader.read().then(({value}) => 
            console.log(decoder.decode(value))
        );
    });
```

**Ou via CLI:**
```bash
curl -N http://localhost:8001/api/sync
# -N = sem buffer (streaming)
```

### Inspecionar metadata.json

```bash
# Ver estrutura de um PAJ
cat "PAJs/PAJ-2026-044-00311/metadata.json" | python -m json.tool
```

### Testar Parser Diretamente

```python
from ingestao.parser import montar_metadata

sisdpu_txt = open("PAJs/PAJ-2026-044-00311/sisdpu.txt").read()
meta = montar_metadata("PAJ-2026-044-00311", sisdpu_txt)
print(meta)
```

### Simular Sync de 1 PAJ

```python
import asyncio
from ingestao.sincronizador import rodar_sync_paj

async def teste():
    logs = []
    async for linha in rodar_sync_paj("2026/044-00311", baixar_anexos=True):
        logs.append(linha)
    return logs

asyncio.run(teste())
```

---

## 🧪 Testes

### Setup Testes

```bash
pip install -r requirements-dev.txt  # pytest, etc
```

### Executar Testes

```bash
pytest tests/  -v
# ou rodar teste específico
pytest tests/test_parser.py::test_parsear_header -v
```

### Exemplo de Teste

```python
# tests/test_parser.py

import pytest
from ingestao.parser import montar_metadata

def test_montar_metadata_simples():
    sisdpu = """PAJ: 2026/044-00311
Assistido: JOÃO
Pretensão: PREVIDENCIÁRIA

MOVIMENTAÇÕES RELEVANTES:
[30/06/2023] Abertura
"""
    meta = montar_metadata("PAJ-2026-044-00311", sisdpu)
    assert meta["paj"] == "2026/044-00311"
    assert meta["assistido_caixa"] == "JOÃO"
    assert len(meta["detalhes_sisdpu"]["movimentacoes"]) == 1
```

---

## 📊 Estrutura de Dados em Detalhes

### metadata.json (Completo)

```json
{
  "paj": "2026/044-00311",
  "paj_norm": "PAJ-2026-044-00311",
  "assistido_caixa": "JOÃO DOS SANTOS",
  "oficio_caixa": "2º OFÍCIO GERAL",
  "pretensao": "PREVIDENCIÁRIA",
  "foro_detectado": "Previdenciario",
  "foro_detalhado": "1ª Instância",
  "classificacao": "PREVIDENCIÁRIA",
  "processo_judicial": "0007123-45.2023.4.01.3500",
  "data_mov_caixa": "2023-08-15",
  "desc_mov_caixa": "Sentença desfavorável...",
  "data_abertura": "2023-06-30",
  "decurso": "30 dias",
  "primeira_deteccao": "2023-06-30T10:00:00",
  "classe_evento": "",
  "prazos_abertos": [
    {
      "data_final": "2023-09-15",
      "dias": 47,
      "parte": "DPU",
      "seq": 0,
      "descricao": "Prazo para recurso"
    }
  ],
  "detalhes_sisdpu": {
    "status_paj": "Ativo",
    "movimentacoes": [
      {
        "seq": 1,
        "data": "2023-06-30",
        "data_original": "30/06/2023 10:15",
        "descricao": "Cadastro de novo PAJ",
        "movimentacao": "",
        "fases": "Abertura"
      },
      {
        "seq": 2,
        "data": "2023-08-15",
        "data_original": "15/08/2023",
        "descricao": "Sentença desfavorável...",
        "movimentacao": "",
        "fases": "Julgamento"
      }
    ]
  },
  "migrado_em": "2023-06-30T10:00:00"
}
```

### elaboracao.json (Última Conclusão)

```json
{
  "status": "ok",
  "concluido_em": "2023-09-01T14:30:00",
  "skill": "peticoes-iniciais",
  "summary": "Peça elaborada com êxito. Recomendado protocolo imediato...",
  "comando": "claude eval --model opus[1m] --effort xhigh --format json ...",
  "tokens_usados": 45000,
  "tempo_execucao_seg": 120
}
```

### historico.jsonl (Trilha de Eventos)

```jsonl
{"tipo": "sync", "data": "2023-06-30T10:00:00", "resultado": "ok", "pajs_sincronizados": 1}
{"tipo": "elaboracao", "skill": "peticoes-iniciais", "paj_norm": "PAJ-2026-044-00311", "status": "ok", "concluido_em": "2023-09-01T14:30:00"}
{"tipo": "nota", "paj_norm": "PAJ-2026-044-00311", "texto": "Cliente ligou para confirmar dados"}
{"tipo": "watchlist_add", "paj_norm": "PAJ-2026-044-00311", "motivo": "Prazo crítico em 5 dias"}
```

---

## 🚀 Performance e Otimizações

### Caching de Metadados

Ao listar muitos PAJs (100+), o parsing é lento. Solução:

```python
# services/paj_service.py

import pickle
from pathlib import Path

CACHE_FILE = PAJS_DIR / ".cache_lista.pkl"
CACHE_TIMEOUT = 300  # 5 minutos

def listar_pajs(usar_cache=True):
    if usar_cache and CACHE_FILE.exists():
        mtime = CACHE_FILE.stat().st_mtime
        if time.time() - mtime < CACHE_TIMEOUT:
            return pickle.load(open(CACHE_FILE, "rb"))
    
    pajs = [...]  # constrói normalmente
    pickle.dump(pajs, open(CACHE_FILE, "wb"))
    return pajs
```

### Limitar Anexos por PAJ

Config em `.env`:
```env
MAX_ANEXOS_POR_PAJ=20  # só baixa os 20 mais recentes
```

Permite completar depois via `/api/sync/paj/{paj}/anexos-desde?data=YYYY-MM-DD`

### Timeout de OCR

PDfs gigantes congelam o OCR:
```env
TIMEOUT_OCR_POR_PAGINA_SEG=30  # timeout por página
```

Se ainda falhar, recai em Claude Sonnet (fallback OCR-LLM).

---

## 🐛 Troubleshooting

| Problema | Causa | Solução |
|----------|-------|---------|
| Sincronização trava | Rate limit / timeout | Aumente RATE_LIMIT_SISDPU_SEG, TIMEOUT_TOTAL_SEG |
| OCR vazio | PDF corrupto ou escaneado mal | Ative OCR_LLM (fallback Sonnet) |
| Claude CLI não encontrado | Não instalado ou fora do PATH | `npm install -g @anthropic-ai/claude` |
| "Credenciais inválidas" | Erro no login SISDPU | Verifique .env + mude senha no SISDPU |
| Template não carrega | Caminho relativo errado | Use `BASE_DIR / "templates"` em app.py |
| CORS error | AJAX de origem diferente | FastAPI não trata, mas dev usa localhost |

---

## 📝 Commit Message Format

Siga convenção Conventional Commits:

```
feat: adiciona novo filtro ao dashboard
^--^  ^--------- mensagem concisa

fix: corrige OCR Tesseract com timeout
docs: atualiza README com exemplo de .env
refactor: simplifica chat_service.py
test: adiciona testes para parser
```

---

## 🔐 Segurança

### Dados Sensíveis

**NÃO versionem:**
- `.env` (credenciais SISDPU)
- `PAJs/` (dados de assistidos)
- `logs/` (pode conter informações pessoais)

**Verifique `.gitignore`:**
```
.env
PAJs/
logs/
__pycache__/
*.pyc
.venv/
```

### Validação de Input

Sempre valide paj_norm:
```python
from services.paj_service import PajNorm

# FastAPI valida automaticamente
@router.get("/paj/{paj_norm}")
async def handler(paj_norm: PajNorm):  # PajNorm = pattern validado
    ...
```

### Timeout de Sessão

Claude CLI sessions são de curta duração. O painel **não mantém sessão persistente**—cada chamada é nova.

---

## 📚 Referências Rápidas

### FastAPI
- Docs: http://localhost:8001/docs (Swagger)
- ReDoc: http://localhost:8001/redoc
- SSE: `EventSourceResponse` (sse-starlette)

### Playwright
- Docs: https://playwright.dev/python/
- Debugger: `python -m playwright install && python -m playwright codegen https://sisdpu.dpu.def.br`

### Jinja2
- Docs: https://jinja.palletsprojects.com/
- Filtros: definidos em `app.py`

### DaisyUI
- Componentes: https://daisyui.com/components/
- Temas: modifique `data-theme` em base.html

### Alpine.js
- Docs: https://alpinejs.dev/
- x-data, x-show, x-for, @click, etc

---

## 🎯 Checklist de Code Review

Antes de fazer PR:

- [ ] Testado localmente (no mínimo a feature principal)
- [ ] Sem arquivos `.env` ou dados de assistidos
- [ ] Seguir naming convention (snake_case functions, PascalCase classes)
- [ ] Adicionar docstring em função pública
- [ ] Tratar exceções defensivamente
- [ ] Usar `contextlib.suppress(Exception)` onde apropriado
- [ ] Logs com `_log.info/error/debug`
- [ ] Commit messages em Conventional Commits
- [ ] Ruff lint passa: `ruff check .`

---

## 🚀 Deploy em Produção

*Não é escopo deste projeto (single-user, local), mas se fosse...*

```bash
# 1. Crie venv isolado
python -m venv /opt/painel/.venv
source /opt/painel/.venv/bin/activate

# 2. Instale deps
pip install -r requirements.txt

# 3. Inicie com supervisor/systemd
# (Uvicorn nativo, reload=False)

# 4. Configure reverse proxy (nginx)
upstream painel {
    server 127.0.0.1:8001;
}
server {
    listen 443 ssl;
    location / {
        proxy_pass http://painel;
    }
}

# 5. HTTPS + certificado
```

---

**Fim do Guia Rápido**

Use este documento em conjunto com `ANALISE_ARQUITETURA.md` para desenvolvimento eficiente.
