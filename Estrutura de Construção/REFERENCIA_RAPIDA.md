# PAINEL SISDPU - Referência Rápida de Código

**Cheat Sheet:** Snippets mais usados, patterns e shortcuts

---

## 📋 Imports Comuns

```python
# Padrão em routes
from fastapi import APIRouter, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse, EventSourceResponse
from sse_starlette.sse import EventSourceResponse

# Padrão em services
from config import PAJS_DIR, OFICIO_GERAL, PAJS_DIR
from services.paj_service import PajNorm, ler_paj
from pathlib import Path
import json, asyncio, contextlib

# Padrão em templates
{% extends "base.html" %}
{% block title %}Seu Título{% endblock %}
{% block content %}
...
{% endblock %}
```

---

## 🎯 Snippets Frequentes

### Validar PAJ
```python
from services.paj_service import PajNorm

# Tipo alias garante validação automática FastAPI
def handler(paj_norm: PajNorm):
    # FastAPI retorna 422 se formato inválido
    pasta = PAJS_DIR / paj_norm
    ...
```

### Ler metadata.json
```python
import json

meta = None
meta_path = PAJS_DIR / paj_norm / "metadata.json"
if meta_path.exists():
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        meta = None

# Ou usar helper
def _ler_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
```

### Renderizar template
```python
from fastapi import Request
from fastapi.responses import HTMLResponse

@router.get("/minha-pagina/{paj_norm}", response_class=HTMLResponse)
async def handler(request: Request, paj_norm: PajNorm):
    dados = ler_paj(paj_norm)
    template = request.app.state.jinja.get_template("minha_pagina.html")
    html = template.render(
        request=request,
        dados=dados,
        paj_norm=paj_norm,
    )
    return HTMLResponse(html)
```

### SSE Streaming (Sync)
```python
from sse_starlette.sse import EventSourceResponse

@router.get("/api/minha-operacao-longa")
async def handler():
    async def _stream():
        async for linha in minha_operacao_longa():
            yield {"event": "log", "data": linha}
        yield {"event": "done", "data": "Pronto"}
    
    return EventSourceResponse(_stream())

async def minha_operacao_longa() -> AsyncGenerator[str, None]:
    yield "Iniciando..."
    await asyncio.sleep(1)
    yield "Processando..."
    await asyncio.sleep(1)
    yield "Finalizando..."
```

### Sync com Lock
```python
_meu_lock = asyncio.Lock()

async def minha_operacao_serializada():
    if _meu_lock.locked():
        yield "[ERRO] já está em execução"
        return
    
    async with _meu_lock:
        yield "Executando com lock..."
        # apenas 1 execução por vez
```

### Subprocess (Claude CLI)
```python
import subprocess
import json

cmd = ["claude", "eval", "--model", "opus[1m]", "--effort", "xhigh", "--format", "json"]
proc = subprocess.Popen(
    cmd,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)

stdout, stderr = proc.communicate(input=seu_prompt, timeout=3600)

for linha in stdout.split('\n'):
    if not linha.strip():
        continue
    obj = json.loads(linha)
    # processar obj
```

### Listar e iterar PAJs
```python
from services.paj_service import listar_pajs

pajs = listar_pajs(incluir_concluidos=False)
for paj in pajs:
    paj_norm = paj.get("paj_norm")
    print(f"PAJ: {paj_norm}, Assistido: {paj.get('assistido')}")
```

### Salvar JSON
```python
import json

dados = {
    "paj": "PAJ-2026-044-00311",
    "status": "ok",
    ...
}

arquivo = PAJS_DIR / paj_norm / "metadata.json"
arquivo.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
```

### Logging
```python
import logging

_log = logging.getLogger("meu_modulo")

_log.debug("Mensagem de debug")
_log.info("Informação")
_log.warning("Aviso")
_log.error("Erro crítico")
```

### Try/Except com Suppress
```python
import contextlib

# Suprime exception silenciosamente
with contextlib.suppress(Exception):
    resultado = operacao_que_pode_falhar()

# Ou em uma linha
with contextlib.suppress(FileNotFoundError):
    Path("arquivo.txt").unlink()
```

### Regex Simples
```python
import re

# Extrair valor
m = re.search(r"^PAJ\s*:\s*(.+)$", linha, re.IGNORECASE)
if m:
    valor = m.group(1).strip()

# Buscar em texto
if re.search(r"PRAZO\s+CR[IÍ]TICO", texto, re.IGNORECASE):
    # encontrou
```

---

## 🎨 Frontend Snippets

### Alpine.js (Reatividade)
```html
<div x-data="{ count: 0, nome: '' }">
    <button @click="count++">Contar</button>
    <span x-text="count"></span>
    
    <input x-model="nome" placeholder="Seu nome" />
    <p x-show="nome.length > 0">Olá, <span x-text="nome"></span>!</p>
</div>
```

### HTMX (AJAX)
```html
<button hx-get="/api/dados"
        hx-target="#resultado"
        hx-swap="innerHTML">
    Carregar Dados
</button>
<div id="resultado"></div>
```

### SSE Listener (JavaScript)
```javascript
const eventSource = new EventSource('/api/sync');

eventSource.addEventListener('log', (event) => {
    console.log(event.data);
    document.getElementById('logs').innerHTML += event.data + '<br>';
});

eventSource.addEventListener('done', () => {
    eventSource.close();
    console.log('Pronto!');
});
```

### Filtros Jinja2
```html
<!-- Usar filtros customizados do painel -->
{{ dados.foro_detectado | formatar_area }}
{{ dados.data_abertura | data_br }}
{{ dados.data_mov_caixa | data_hora_br }}
```

### Modal DaisyUI
```html
<dialog id="meu-modal" class="modal">
    <div class="modal-box">
        <h3 class="font-bold text-lg">Meu Modal</h3>
        <p class="py-4">Conteúdo aqui</p>
        <div class="modal-action">
            <form method="dialog">
                <button class="btn">Fechar</button>
            </form>
        </div>
    </div>
</dialog>

<!-- Abrir -->
<button onclick="document.getElementById('meu-modal').showModal()">
    Abrir
</button>
```

---

## 📊 Estruturas de Dados

### metadata.json (Essencial)
```python
{
    "paj": "2026/044-00311",
    "paj_norm": "PAJ-2026-044-00311",
    "assistido_caixa": "NOME",
    "pretensao": "PREVIDENCIÁRIA",
    "foro_detectado": "Previdenciario",
    "processo_judicial": "0001234-56.2023.4.01.3500",
    "data_abertura": "2023-06-30",
    "detalhes_sisdpu": {
        "movimentacoes": [
            {
                "seq": 1,
                "data": "2023-06-30",
                "descricao": "Abertura",
                "fases": "Abertura"
            }
        ]
    },
    "prazos_abertos": [
        {
            "data_final": "2023-09-15",
            "dias": 47,
            "parte": "DPU",
            "descricao": "Prazo para recurso"
        }
    ]
}
```

### elaboracao.json
```python
{
    "status": "ok",  # ou "timeout", "erro", "nao_aplicavel"
    "concluido_em": "2023-09-01T14:30:00",
    "skill": "peticoes-iniciais",
    "summary": "Texto resumido da conclusão...",
    "tokens_usados": 45000,
    "tempo_execucao_seg": 120
}
```

### historico.jsonl
```
{"tipo": "sync", "data": "2023-06-30T10:00:00", "resultado": "ok"}
{"tipo": "elaboracao", "skill": "peticoes-iniciais", "status": "ok", "concluido_em": "2023-09-01T14:30:00"}
{"tipo": "nota", "texto": "Cliente ligou..."}
```

---

## 🔧 Config (.env)

```env
# Workspace
OFICIO_GERAL=/Users/defensor/Ofício Geral
OFICIO_DESCRICAO=1ª Categoria - DPU/SP

# SISDPU
SISDPU_USERNAME=seu.usuario
SISDPU_PASSWORD=sua.senha
RATE_LIMIT_SISDPU_SEG=2
TIMEOUT_TOTAL_SEG=3600

# OCR
OCR_PREPROC_ATIVO=1
OCR_PREPROC_TIMEOUT_SEG=600
OCR_LLM_ATIVO=1
OCR_LLM_MODELO=sonnet
OCR_LLM_CHUNK_PAGINAS=40

# Claude
ELABORACAO_MODELO=opus[1m]
ELABORACAO_EFFORT=xhigh
SKILL_COM_PROCESSO=analisar-processo
SKILL_SEM_PROCESSO=firac-triagem

# Outros
MAX_ANEXOS_POR_PAJ=0  # 0 = sem limite
```

---

## 🧪 Testes Rápidos

```bash
# Test parser
python -c "
from ingestao.parser import montar_metadata
txt = open('PAJs/PAJ-2026-044-00311/sisdpu.txt').read()
meta = montar_metadata('PAJ-2026-044-00311', txt)
print(meta)
"

# Test API
curl http://localhost:8001/api/pajs | python -m json.tool

# Test sync (1 PAJ)
python -c "
import asyncio
from ingestao.sincronizador import rodar_sync_paj
async def teste():
    async for linha in rodar_sync_paj('2026/044-00311', baixar_anexos=True):
        print(linha.rstrip())
asyncio.run(teste())
"

# Listar PAJs
ls PAJs/ | grep PAJ- | wc -l
```

---

## 🐛 Debug Avançado

```python
# Inspect metadata
import json
meta = json.loads(open('PAJs/PAJ-2026-044-00311/metadata.json').read())
print(json.dumps(meta, indent=2))

# Inspect prompt_max
print(open('PAJs/PAJ-2026-044-00311/PROMPT_MAX.md').read())

# Contar movimentações
meta = json.loads(open('PAJs/PAJ-2026-044-00311/metadata.json').read())
print(f"Movimentações: {len(meta['detalhes_sisdpu']['movimentacoes'])}")
```

---

## 🎯 Linha de Comando

```bash
# Iniciar servidor
python app.py

# Parar servidor (Windows)
python stop.py

# Ruff lint
ruff check .

# Formatar código
ruff format .

# Pytest
pytest tests/ -v

# Limpar cache
find . -type d -name __pycache__ -exec rm -rf {} +
find . -name "*.pyc" -delete
```

---

## 📚 Estrutura de Pastas Cheat Sheet

```
.
├── app.py                    # Entry point FastAPI
├── config.py                 # Config central (lê .env)
├── requirements.txt          # Deps pip
├── .env                      # Git-ignored, credenciais
│
├── routes/                   # Endpoints HTTP
│   ├── dashboard.py
│   ├── paj.py
│   ├── sync.py
│   ├── chat.py
│   └── ... (mais 11 routers)
│
├── services/                 # Lógica (state machines)
│   ├── paj_service.py        # CRUD de PAJs
│   ├── sync_service.py       # Orquestração sync
│   ├── chat_service.py       # Claude CLI
│   ├── prompt_builder.py     # Contexto para IA
│   └── ... (mais 18 serviços)
│
├── ingestao/                 # Scraping + OCR
│   ├── sisdpu_client.py      # Playwright
│   ├── sincronizador.py      # Orquestrador
│   ├── parser.py             # sisdpu.txt → JSON
│   ├── ocr.py                # Tesseract
│   └── prazos.py             # Extração de prazos
│
├── templates/                # HTML Jinja2
│   ├── base.html             # Layout base
│   ├── dashboard.html
│   ├── paj_detail.html
│   └── ... (mais 12 templates)
│
├── static/                   # CSS + JS
│   ├── css/custom.css
│   └── js/app.js
│
├── logs/                     # Logs (app.log + debug)
│   ├── app.log
│   └── sisdpu_debug/
│
└── PAJs/ (em OFICIO_GERAL)
    ├── PAJ-YYYY-NNN-XXXXX/
    │   ├── metadata.json
    │   ├── sisdpu.txt
    │   ├── PROMPT_MAX.md
    │   ├── Arquivos/
    │   │   └── *.pdf + *.txt
    │   └── elaboracao.json
    └── ... (mais PAJs)
```

---

## 🚀 Workflow Típico de Desenvolvimento

```bash
# 1. Faça alteração em arquivo Python/HTML/CSS
nano routes/minha_nova_rota.py

# 2. Teste localmente (sem reload automático)
# Encerre servidor (Ctrl+C) e reinicie
python app.py

# 3. Verifique nos logs
tail -f logs/app.log

# 4. Teste manualmente no navegador ou curl
curl http://localhost:8001/api/novo-endpoint

# 5. Commit
git add routes/minha_nova_rota.py
git commit -m "feat: adiciona novo endpoint"

# 6. Push
git push origin feature-branch
```

---

## 📝 Checklist Antes de Commitar

```
- [ ] Código testado localmente
- [ ] Sem arquivos .env no commit
- [ ] Sem dados de assistidos no commit
- [ ] Ruff lint passou: ruff check .
- [ ] Docstring em funções públicas
- [ ] Logging em pontos críticos
- [ ] Tratamento de exceções defensivo
- [ ] Commit message em Conventional Commits
- [ ] Nenhum import não utilizado
```

---

**Fim da Referência Rápida**

Imprima este documento ou mantenha aberto para consultas rápidas durante desenvolvimento.
