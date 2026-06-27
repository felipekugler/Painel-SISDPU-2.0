# PAINEL SISDPU - Exemplos Práticos e Case Studies

**Documento:** Walkthrough passo-a-passo de cenários reais

---

## 🎯 Caso 1: Sincronizar Caixa Completa

### Cenário
Defensor acessa o painel às 09:00 e quer sincronizar sua caixa SISDPU para revisar PAJs novos.

### Sequência de Ações

**1. Frontend (Browser)**
```
Clica botão "Sincronizar caixa"
    ↓
Abre modal com log em tempo real (SSE)
    ↓
JavaScript: fetch GET /api/sync (anexos=1)
```

**2. Backend (FastAPI)**
```python
# routes/sync.py

@router.get("/api/sync")
async def sync(anexos: int = 1):
    return EventSourceResponse(_stream(baixar_anexos=bool(anexos)))

async def _stream(baixar_anexos: bool):
    async for linha in rodar_sync(baixar_anexos=baixar_anexos):
        yield {"event": "log", "data": linha.rstrip("\n")}
    yield {"event": "done", "data": "Sincronização finalizada"}
```

**3. Service (Orquestração)**
```python
# services/sync_service.py

async def rodar_sync(baixar_anexos: bool) -> AsyncGenerator[str, None]:
    async with _sync_lock:  # Garante 1 sync por vez
        _cancel_event = asyncio.Event()
        
        # Inicia worker em thread pool
        async def _worker(emit):
            await sincronizador.rodar(
                emit=emit,
                baixar_anexos=baixar_anexos,
                deve_cancelar=deve_cancelar,
            )
        
        # Coleta logs em Queue e emia via SSE
        async for log_line in _worker(emit):
            yield log_line
```

**4. Ingestão (Sincronizador)**
```python
# ingestao/sincronizador.py

async def rodar(emit, baixar_anexos, deve_cancelar):
    """Orquestra: login → lista caixa → processa PAJs."""
    
    emit("[INFO] Conectando ao SISDPU...")
    try:
        await sisdpu_client.login(SISDPU_USERNAME, SISDPU_PASSWORD)
    except CredenciaisInvalidas:
        emit("[ERRO] Falha no login — verifique credenciais no .env")
        return
    
    emit("[INFO] Listando caixa de entrada...")
    caixa = await sisdpu_client.listar_caixa_entrada()
    emit(f"[INFO] {len(caixa)} PAJs encontrados na caixa")
    
    # Para cada PAJ
    for i, item_caixa in enumerate(caixa, 1):
        if deve_cancelar():
            emit("[CANCELADO] Sincronização abortada pelo usuário")
            break
        
        paj = item_caixa.get("paj")
        emit(f"[{i}/{len(caixa)}] Processando PAJ {paj}...")
        
        try:
            # Carrega detalhamento
            det = await sisdpu_client.carregar_detalhamento(paj)
            emit(f"  ├─ Assistido: {det.get('assistido')}")
            emit(f"  ├─ Pretensão: {det.get('pretensao')}")
            
            # Lista anexos
            if baixar_anexos:
                anexos = await sisdpu_client.listar_anexos_paj(paj)
                emit(f"  ├─ {len(anexos)} anexo(s)")
                
                # Baixa e OCR cada PDF
                for j, anexo in enumerate(anexos[:MAX_ANEXOS_POR_PAJ], 1):
                    nome = anexo.get("nome")
                    emit(f"  │  ├─ [{j}] {nome}...")
                    
                    try:
                        pdf_path = await _baixar_anexo(paj, anexo)
                        emit(f"  │  │  └─ Baixado OK")
                        
                        # OCR
                        emit(f"  │  └─ Extraindo texto (OCR)...")
                        texto = await ocr.extrair_texto(pdf_path)
                        
                        # Salva .txt
                        (pdf_path.parent / f"{pdf_path.stem}.txt").write_text(texto)
                        emit(f"  │     └─ {len(texto)} caracteres extraídos")
                    
                    except Exception as e:
                        emit(f"  │  └─ [AVISO] {e}")
            
            # Monta sisdpu.txt (canonico)
            txt = _montar_sisdpu_txt(item_caixa, det)
            
            # Parse → metadata.json
            metadata = parser.montar_metadata(paj_norm, txt)
            
            # Grava em disco
            pasta = PAJS_DIR / paj_norm
            pasta.mkdir(parents=True, exist_ok=True)
            (pasta / "sisdpu.txt").write_text(txt)
            (pasta / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
            
            emit(f"  └─ Gravado em PAJs/{paj_norm}/")
        
        except Exception as e:
            emit(f"  [ERRO] {e}")
        
        # Rate limit
        await asyncio.sleep(RATE_LIMIT_SISDPU)
    
    # Marca última sincronização
    (PAJS_DIR / ".ultima_sync_caixa.json").write_text(json.dumps({
        "em": datetime.now().isoformat(),
        "pajs": len(caixa),
    }))
    
    emit("[SUCESSO] Sincronização completa!")
    
    await sisdpu_client.fechar()
```

**5. Resultado Final**
```
Diretório atualizado:
PAJs/
├── PAJ-2026-044-00311/
│   ├── sisdpu.txt (nova versão)
│   ├── metadata.json (nova versão)
│   └── pecas/
│       ├── documento_1.pdf (novo)
│       └── documento_1.txt (novo, OCR)
└── PAJ-2026-044-00312/
    └── ...
```

**Frontend recebe:**
```
event: log, data: [INFO] Conectando ao SISDPU...
event: log, data: [INFO] Listando caixa...
event: log, data: [INFO] 2 PAJs encontrados
event: log, data: [1/2] Processando PAJ 2026/044-00311...
event: log, data:   ├─ Assistido: JOÃO
event: log, data:   ├─ Pretensão: PREVIDENCIÁRIA
event: log, data:   ├─ 2 anexo(s)
event: log, data:   │  ├─ [1] documento.pdf...
event: log, data:   │  │  └─ Extraindo texto (OCR)...
event: log, data:   │  │     └─ 5234 caracteres extraídos
event: log, data:   │  └─ Gravado em PAJs/PAJ-2026-044-00311/
event: log, data: [2/2] Processando PAJ 2026/044-044-00312...
...
event: done, data: Sincronização finalizada
```

---

## 📝 Caso 2: Defensor Elabora Peça via Chat

### Cenário
Defensor abre PAJ-2026-044-00311 (caso previdenciário) e clica "Elaborar" para gerar uma petição inicial.

### Sequência de Ações

**1. Frontend (Modal de Chat)**
```html
<!-- templates/paj_detail.html -->
<div x-data="chatApp()">
    <button @click="iniciar_elaboracao('peticoes-iniciais')">
        Elaborar Petição
    </button>
    
    <div id="chat-log" class="prose">
        <!-- Logs aparecem aqui em tempo real via SSE -->
    </div>
</div>
```

```javascript
// static/js/app.js
function chatApp() {
    return {
        historico: [],
        
        async iniciar_elaboracao(skill) {
            const resp = await fetch(`/api/chat/PAJ-2026-044-00311`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    prompt: "Elabore a peça apropriada",
                    skill: skill,
                }),
            });
            
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            
            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                
                const linhas = decoder.decode(value).split('\n');
                linhas.forEach(linha => {
                    if (linha.startsWith('event:')) {
                        // parse SSE
                    }
                    this.historico.push(linha);
                });
            }
        }
    }
}
```

**2. Backend (Chat Route)**
```python
# routes/chat.py

@router.post("/api/chat/{paj_norm}")
async def chat(
    paj_norm: PajNorm,
    payload: dict = Body(...),
):
    """Elabora peça via Claude Code CLI."""
    skill_slug = payload.get("skill")
    prompt_user = payload.get("prompt", "")
    
    return EventSourceResponse(
        _stream_chat(paj_norm, skill_slug, prompt_user)
    )

async def _stream_chat(paj_norm, skill_slug, prompt_user):
    async for linha in chat_service.processar_instrucao(
        paj_norm, prompt_user, skill_slug
    ):
        yield {"event": "log", "data": linha}
    yield {"event": "done", "data": "Elaboração completa"}
```

**3. Service (Integração Claude CLI)**
```python
# services/chat_service.py

async def processar_instrucao(
    paj_norm: str,
    prompt_user: str,
    skill_slug: str | None,
) -> AsyncGenerator[str, None]:
    """Invoca Claude Code CLI com contexto do PAJ."""
    
    # Carrega dados do PAJ
    paj_data = paj_service.ler_paj(paj_norm)
    if not paj_data:
        yield "[ERRO] PAJ não encontrado"
        return
    
    # Gera contexto máximo (PROMPT_MAX.md)
    emit("[INFO] Preparando contexto...")
    prompt_max = prompt_builder.gerar_prompt_max(paj_norm)
    yield f"[INFO] Contexto: {len(prompt_max)} caracteres"
    
    # Monta instrução com skill (se houver)
    emit("[INFO] Montando instrução...")
    instrucao = _montar_instrucao(
        pasta=PAJS_DIR / paj_norm,
        prompt_content=prompt_max,
        skill_slug=skill_slug,
    )
    
    # Invoca Claude Code CLI
    emit(f"[INFO] Invocando Claude CLI (modelo: {ELABORACAO_MODELO})...")
    
    cmd = CLAUDE_CMD + [
        "eval",
        "--model", ELABORACAO_MODELO,
        "--effort", ELABORACAO_EFFORT,
        "--format", "json",
    ]
    
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        
        stdout, stderr = proc.communicate(input=instrucao, timeout=7200)
        
        # Parse stream JSON
        for linha in stdout.split('\n'):
            if not linha.strip():
                continue
            
            try:
                obj = json.loads(linha)
                if obj.get("type") == "logs":
                    for msg in obj.get("messages", []):
                        emit(f"[{msg['level']}] {msg['text']}")
                
                elif obj.get("type") == "result":
                    conteudo = obj.get("content", "")
                    emit(f"[RESULTADO] {len(conteudo)} caracteres gerados")
                    
                    # Persiste em arquivo
                    tipo_peca = _extrair_tipo(conteudo)
                    nome_arquivo = f"{tipo_peca.lower()}.txt"
                    
                    arquivo = PAJS_DIR / paj_norm / nome_arquivo
                    arquivo.write_text(conteudo, encoding="utf-8")
                    emit(f"[SUCESSO] Salvo em {nome_arquivo}")
                    
                    # Atualiza elaboracao.json
                    elab_json = {
                        "status": "ok",
                        "concluido_em": datetime.now().isoformat(),
                        "skill": skill_slug or "autonomo",
                        "summary": conteudo[:500],  # digest
                        "tokens_usados": obj.get("tokens", 0),
                    }
                    (PAJS_DIR / paj_norm / "elaboracao.json").write_text(
                        json.dumps(elab_json, indent=2)
                    )
                    
                    # Append historico
                    historico.append(paj_norm, {
                        "tipo": "elaboracao",
                        "skill": skill_slug,
                        "status": "ok",
                        "concluido_em": datetime.now().isoformat(),
                        "summary": conteudo[:200],
                    })
            
            except json.JSONDecodeError:
                emit(f"[DEBUG] {linha[:100]}")
    
    except subprocess.TimeoutExpired:
        emit("[ERRO] Timeout ao executar Claude CLI (limite 2h)")
        proc.kill()
    except Exception as e:
        emit(f"[ERRO] {type(e).__name__}: {e}")
```

**4. Prompt Builder (Contexto Máximo)**
```python
# services/prompt_builder.py

def gerar_prompt_max(paj_norm: str) -> str:
    """Concatena contexto completo para o Claude."""
    pasta = PAJS_DIR / paj_norm
    lines = []
    
    # 1. Metadados
    meta = _ler_json(pasta / "metadata.json") or {}
    lines += [
        "# Identificação do PAJ",
        f"- PAJ: {meta.get('paj')}",
        f"- Assistido: {meta.get('assistido_caixa')}",
        f"- Pretensão: {meta.get('pretensao')}",
        f"- Foro: {meta.get('foro_detalhado')} ({meta.get('foro_detectado')})",
        f"- Status: {'Ativo' if meta.get('detalhes_sisdpu', {}).get('status_paj') == 'Ativo' else 'Concluído'}",
        "",
    ]
    
    # 2. Prazos
    if meta.get("prazos_abertos"):
        lines += ["## Prazos em Aberto", ""]
        for prazo in meta["prazos_abertos"]:
            lines.append(f"- {prazo['descricao']}: {prazo['data_final']} ({prazo['dias']} dias)")
        lines.append("")
    
    # 3. Últimas movimentações
    lines += ["## Últimas Movimentações", ""]
    movs = meta.get("detalhes_sisdpu", {}).get("movimentacoes", [])[-8:]
    for mov in movs:
        lines.append(f"- [{mov['data']}] {mov.get('fases', '')}: {mov['descricao'][:200]}")
    lines.append("")
    
    # 4. Memória (conclusões anteriores)
    mem = _secao_memoria(paj_norm)
    if mem:
        lines.append(mem)
        lines.append("")
    
    # 5. Texto completo do SISDPU
    sisdpu = (pasta / "sisdpu.txt").read_text() if (pasta / "sisdpu.txt").exists() else ""
    lines += [
        "## Texto Completo do SISDPU",
        "```",
        sisdpu,
        "```",
        "",
    ]
    
    # 6. Peças anexadas
    pecas_dir = pasta / "pecas"
    if pecas_dir.exists():
        lines += ["## Anexos Baixados do SIS", ""]
        for txt_file in sorted(pecas_dir.glob("*.txt")):
            conteudo = txt_file.read_text()
            lines.append(f"### {txt_file.name}")
            lines.append("```")
            lines.append(conteudo[:1000])  # primeiros 1000 chars
            if len(conteudo) > 1000:
                lines.append("[...truncado...]")
            lines.append("```")
        lines.append("")
    
    return "\n".join(lines)
```

**5. Resultado Final**

```
Frontend recebe via SSE:
event: log, data: [INFO] Preparando contexto...
event: log, data: [INFO] Contexto: 87543 caracteres
event: log, data: [INFO] Montando instrução...
event: log, data: [INFO] Invocando Claude CLI (modelo: opus[1m])...
event: log, data: [debug] Usando skill peticoes-iniciais
event: log, data: [debug] Analisando fatos do caso...
event: log, data: [debug] Verificando jurisprudência...
event: log, data: [debug] Redigindo petição...
event: log, data: [RESULTADO] 4532 caracteres gerados
event: log, data: [SUCESSO] Salvo em peticao.txt

PAJ-2026-044-00311/peticao.txt criado com conteúdo pronto para protocolo
PAJ-2026-044-00311/elaboracao.json atualizado com status "ok"
PAJ-2026-044-00311/historico.jsonl appended com evento "elaboracao"
```

---

## 🔍 Caso 3: Busca Textual em Acervo

### Cenário
Defensor quer buscar PAJs relacionados a "benefício por incapacidade".

### Sequência de Ações

**1. Frontend (Busca Global)**
```javascript
// Ctrl+K abre modal
document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.key === "k") {
        document.getElementById("busca-modal").showModal();
        document.getElementById("busca-input").focus();
    }
});

// Escuta input
document.getElementById("busca-input").addEventListener("input", (e) => {
    const termo = e.target.value;
    if (termo.length < 2) {
        document.getElementById("busca-status").textContent = "digite pelo menos 2 caracteres";
        return;
    }
    
    fetch(`/api/busca?termo=${encodeURIComponent(termo)}`)
        .then(r => r.json())
        .then(data => {
            // Renderiza resultados
            const html = data.resultados.map(r => `
                <div class="p-2 hover:bg-base-300 cursor-pointer" onclick="abrir_paj('${r.paj_norm}')">
                    <div class="font-bold">${r.paj_norm}</div>
                    <div class="text-xs text-base-content/60">${r.tipo}</div>
                    <div class="text-sm">${r.trecho.substring(0, 100)}...</div>
                </div>
            `).join("");
            document.getElementById("busca-results").innerHTML = html;
        });
});
```

**2. Backend (Busca Service)**
```python
# routes/busca.py

@router.get("/api/busca", response_class=JSONResponse)
async def busca(termo: str, limite: int = 50):
    return busca_service.buscar(termo, limite=limite)
```

```python
# services/busca_service.py

def buscar(termo: str, tipos: list[str] | None = None, limite: int = 50) -> dict:
    """Busca full-text em PAJs."""
    if not termo or len(termo) < 2:
        return {"resultados": []}
    
    termo_lower = termo.lower()
    resultados = []
    
    # Carrega TODOS os metadata.json (cache em memória é OK pra uso local)
    for pasta_paj in PAJS_DIR.glob("PAJ-*/"):
        meta = _ler_json(pasta_paj / "metadata.json")
        if not meta:
            continue
        
        paj_norm = meta.get("paj_norm")
        
        # 1. Busca em movimentações
        for mov in meta.get("detalhes_sisdpu", {}).get("movimentacoes", []):
            desc = mov.get("descricao", "").lower()
            if termo_lower in desc:
                resultados.append({
                    "paj_norm": paj_norm,
                    "tipo": "movimentacao",
                    "data": mov.get("data"),
                    "trecho": desc,
                    "score": desc.count(termo_lower),  # relevância
                })
        
        # 2. Busca em peças (*.txt)
        pecas_dir = pasta_paj / "pecas"
        if pecas_dir.exists():
            for txt_file in pecas_dir.glob("*.txt"):
                conteudo = txt_file.read_text()
                if termo_lower in conteudo.lower():
                    # Extrai contexto (50 chars antes e depois)
                    idx = conteudo.lower().find(termo_lower)
                    inicio = max(0, idx - 50)
                    fim = min(len(conteudo), idx + len(termo_lower) + 50)
                    contexto = conteudo[inicio:fim]
                    
                    resultados.append({
                        "paj_norm": paj_norm,
                        "tipo": f"peça ({txt_file.stem})",
                        "trecho": contexto,
                        "score": conteudo.lower().count(termo_lower),
                    })
    
    # Ordena por score (relevância)
    resultados.sort(key=lambda x: x["score"], reverse=True)
    
    return {
        "resultados": resultados[:limite],
        "total": len(resultados),
    }
```

**3. Resultado**
```json
{
  "resultados": [
    {
      "paj_norm": "PAJ-2026-044-00311",
      "tipo": "movimentacao",
      "data": "2023-08-15",
      "trecho": "Julgamento: Sentença reconhecendo o direito ao benefício por incapacidade permanente...",
      "score": 2
    },
    {
      "paj_norm": "PAJ-2026-044-00312",
      "tipo": "peça (despacho)",
      "trecho": "...segundo entendimento pacífico sobre benefício por incapacidade temporária, aplicável...",
      "score": 1
    }
  ],
  "total": 2
}
```

---

## ⏱️ Caso 4: Monitoramento de Prazos Críticos

### Cenário
Sistema detecta que 3 PAJs têm prazos vencendo em <5 dias e avisa o defensor.

### Fluxo

**1. Sincronização (Detecta Prazos)**
```python
# ingestao/prazos.py

def detectar_prazos_criticos(sisdpu_txt: str, foro: str) -> list[dict]:
    """Extrai prazos do sisdpu.txt e calcula vencimento."""
    prazos = []
    
    # Procura "PRAZO CRÍTICO: DD/MM/YYYY"
    m = re.search(r"PRAZO\s+CR[IÍ]TICO\s*:\s*(\d{2}/\d{2}/\d{4})", sisdpu_txt)
    if not m:
        return prazos
    
    data_venc_br = m.group(1)
    data_venc_iso = parse_data_br(data_venc_br)
    
    # Calcula dias
    try:
        d_venc = datetime.fromisoformat(data_venc_iso)
        dias = (d_venc - datetime.now().date()).days
    except:
        dias = "?"
    
    # Aplica dobra DPU se somos réus (heurística: "DPU" no texto)
    dobra_dpu = 5 if "DPU" in sisdpu_txt else 0
    dias_ajustado = dias + dobra_dpu if isinstance(dias, int) else dias
    
    # Exclui fins de semana/feriados (simplificado)
    dias_uteis = _contar_dias_uteis(datetime.now().date(), d_venc)
    
    prazos.append({
        "data_final": data_venc_iso,
        "dias": dias_ajustado,
        "dias_uteis": dias_uteis,
        "dobra_dpu": dobra_dpu,
        "parte": "DPU",
        "descricao": "Prazo crítico",
    })
    
    return prazos
```

**2. Persistência**
```python
# Durante sincronização, em sincronizador.py:
metadata = parser.montar_metadata(paj_norm, txt)
# metadata contém: prazos_abertos = [{ data_final, dias, ... }]
```

**3. Dashboard Alerta**
```html
<!-- templates/paj_detail.html -->
<div x-show="dados.metadata.prazos_abertos.length > 0">
    <div x-data="{
        prazos: $el.dataset.prazos | JSON.parse
    }">
        <template x-for="prazo in prazos" :key="prazo.data_final">
            <div :class="prazo.dias < 5 ? 'alert alert-error' : 'alert alert-warning'">
                <span x-text="`Prazo: ${prazo.data_final} (${prazo.dias} dias)`"></span>
            </div>
        </template>
    </div>
</div>
```

**4. Página de Prazos**
```python
# routes/prazos.py

@router.get("/prazos", response_class=HTMLResponse)
async def prazos(request: Request):
    """Lista todos os prazos em aberto, ordenados por urgência."""
    pajs = listar_pajs()
    todos_prazos = []
    
    for paj in pajs:
        paj_norm = paj.get("paj_norm")
        pasta = PAJS_DIR / paj_norm
        meta = _ler_json(pasta / "metadata.json") or {}
        
        for prazo in meta.get("prazos_abertos", []):
            todos_prazos.append({
                "paj_norm": paj_norm,
                "assistido": paj.get("assistido"),
                "data_final": prazo.get("data_final"),
                "dias": prazo.get("dias"),
                "descricao": prazo.get("descricao"),
            })
    
    # Ordena por dias (crescente = mais urgente primeiro)
    todos_prazos.sort(key=lambda x: x.get("dias", 999))
    
    # Filtra (opcional): mostrar apenas <30 dias
    criticos = [p for p in todos_prazos if isinstance(p["dias"], int) and p["dias"] < 30]
    
    template = request.app.state.jinja.get_template("prazos.html")
    return HTMLResponse(template.render(
        request=request,
        prazos=criticos,
        total=len(todos_prazos),
    ))
```

**5. Resultado**
```
Página /prazos mostra:

Prazos Críticos (< 5 dias)
┌─────────────────────────────┐
│ PAJ-2026-044-00311          │ ← 2 dias (VERMELHO)
│ JOÃO DOS SANTOS             │
│ Vencimento: 29/06/2026      │
│ Prazo para recurso          │
└─────────────────────────────┘

┌─────────────────────────────┐
│ PAJ-2026-044-00312          │ ← 4 dias (VERMELHO)
│ MARIA SILVA                 │
│ Vencimento: 01/07/2026      │
│ Prazo para manifestação     │
└─────────────────────────────┘

Prazos Moderados (5-15 dias)
┌─────────────────────────────┐
│ PAJ-2026-044-00313          │ ← 10 dias (AMARELO)
│ CARLOS SANTOS               │
│ Vencimento: 07/07/2026      │
│ Prazo para contestação      │
└─────────────────────────────┘
```

---

## 📱 Caso 5: Customização Defensoria

### Cenário
Defensor quer customizar o painel para seu contexto (unidade, estilo, diretrizes).

### Arquivos Customizáveis

**1. `.env` (Configuração)**
```env
OFICIO_GERAL=/Users/defensor/Ofício Geral
OFICIO_DESCRICAO=1ª Categoria - DPU/SP

# Claude Code
ELABORACAO_MODELO=opus[1m]
ELABORACAO_EFFORT=xhigh

# OCR
OCR_PREPROC_ATIVO=1
OCR_LLM_ATIVO=1
OCR_LLM_MODELO=sonnet
```

**2. `CLAUDE.md` (Diretrizes Jurídicas)**
```markdown
# Diretrizes do Defensor

## Estilo
- Linguagem formal e técnica
- Respeite termos da DPU
- Cite jurisprudência pacífica

## Estrutura de Peças
- Cabeçalho: Nome, CPF, capacidade
- Fundamentos: Lei + jurisprudência
- Fundamentação: 3-4 parágrafos máximo
- Pedido: claro e objetivo
- Encerramento: data + assinatura

## Bases de Conhecimento
- Use memoriais anteriores em `Peças Feitas/`
- Siga precedentes de seu próprio tribunal
- Atualize jurisprudência em 6 meses
```

**3. `painel_config.json` (Config UI)**
```json
{
  "theme": "dark",
  "filtros_salvos": {
    "previdenciarios": {
      "foro": "Previdenciario",
      "status": "Ativo"
    }
  },
  "watchlist": [
    {
      "paj_norm": "PAJ-2026-044-00311",
      "motivo": "Prazo crítico em 3 dias"
    }
  ],
  "columns_dashboard": ["paj_norm", "assistido", "pretensao", "data_abertura"],
  "google_calendar_sync": true,
  "google_calendar_id": "seu_email@gmail.com"
}
```

**4. Template Custom (opcional)**
```html
<!-- templates/paj_detail_custom.html -->
<!-- Override do padrão para sua unidade -->

<div class="custom-header">
    <img src="/logo-unidade.png" />
    <h1>{{ dados.metadata.assistido }}</h1>
</div>
```

---

## 🔧 Caso 6: Estender com Nova Funcionalidade

### Cenário
Defensor quer adicionar campo "Valor da Causa" ao PAJ.

### Passos

**1. Extrair do SISDPU**
```python
# ingestao/parser.py - adicione regex

HEADER_KEYS = {
    ...
    "valor_causa": re.compile(r"^Valor\s+da\s+Causa\s*:\s*R\$\s*([\d.,]+)", re.IGNORECASE),
}
```

**2. Persistir em metadata.json**
```python
# ingestao/parser.py - no montar_metadata()

meta = {
    ...
    "valor_causa": header.get("valor_causa", ""),
}
```

**3. Exibir no painel**
```html
<!-- templates/paj_detail.html -->

<div class="card">
    <div class="card-body">
        <h3>Valor da Causa</h3>
        <p class="text-2xl font-bold">{{ dados.metadata.valor_causa or "N/A" }}</p>
    </div>
</div>
```

**4. Filtrar no dashboard**
```python
# routes/dashboard.py - adicione query param

@router.get("/api/pajs")
async def api_pajs(
    concluidos: int = 0,
    valor_minimo: str = None,
):
    pajs = listar_pajs(incluir_concluidos=bool(concluidos))
    
    if valor_minimo:
        try:
            valor = float(valor_minimo.replace(",", "."))
            pajs = [p for p in pajs 
                    if float(p.get("valor_causa", "0").replace(".", "").replace(",", ".")) >= valor]
        except:
            pass
    
    return {"pajs": pajs, ...}
```

**5. Testar**
```bash
# Ver se apareceu
curl http://localhost:8001/api/pajs | grep valor_causa

# Filtrar
curl "http://localhost:8001/api/pajs?valor_minimo=10000" | jq .
```

---

**Fim dos Exemplos Práticos**

Use estes casos como templates para suas extensões próprias.
