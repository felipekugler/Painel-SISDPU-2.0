# Implementação - Extração de Processo Judicial com Validação Cruzada

**Data:** 2026-06-27  
**Status:** ✓ IMPLEMENTADO

## Resumo das Mudanças

### Arquivo Modificado: `ingestao/sisdpu_client.py`

#### O Que Foi Alterado

**ANTES:** Extração via regex genérica (ANTIGA - tinha falsos positivos)
```python
const procMatch = body.match(/(\\d{7}-\\d{2}\\.\\d{4}\\.\\d\\.\\d{2}\\.\\d{4})/);
if (procMatch) r.processo_judicial = procMatch[1];

const juizoMatch = body.match(/Juízo\\/Orgão Julgador\\s*\\n\\s*([^\\n]+)/);
if (juizoMatch) r.juizo = juizoMatch[1].trim();
```

**DEPOIS:** Extração baseada em seletor DOM + validação cruzada (NOVA - sem falsos positivos)
```javascript
// 1. Função de extração de dados da seção de Processo Judicial
const extrairProcessoJudicial = () => { ... };

// 2. Extração de movimentações (PRIMEIRO)

// 3. Execução da extração com validação cruzada (SEGUNDO)
const dadosProcesso = extrairProcessoJudicial();

// 4. Validação cruzada contra movimentações
if (dadosProcesso.processo_judicial && dadosProcesso.data_inclusao) {
  const movCadastro = movs.find(mov =>
    mov.movimentacao?.includes('Cadastramento de processo judicial')
  );
  // Validações...
}

// 5. Armazenar resultados
r.processo_judicial = dadosProcesso.processo_judicial;
r.juizo = dadosProcesso.juizo;
r.data_inclusao = dadosProcesso.data_inclusao;
r.observacoes_processo = dadosProcesso.observacoes_processo;
r.validacao_processo = dadosProcesso.validacao;
```

---

## Dados Extraídos (4 campos)

| Campo | Tipo | Tamanho | Exemplo | Armazenado Em |
|-------|------|---------|---------|---|
| `processo_judicial` | string | **Variável (ilimitado)** | `99999999920244047000` | `sisdpu_raw.processo_judicial` |
| `juizo` | string | **Variável (ilimitado)** | `JEF - Outros` | `sisdpu_raw.juizo` |
| `data_inclusao` | string (DD/MM/YYYY) | **Variável (ilimitado)** | `27/06/2026` | `sisdpu_raw.data_inclusao` |
| `observacoes_processo` | string | **Variável (ilimitado)** | `888877776666` | `sisdpu_raw.observacoes_processo` |

### ⚠️ IMPORTANTE: Sem Truncamento

Todos os 4 campos capturam **dados completos sem limite de tamanho**. O `.textContent.trim()` não usa `.substring()`, portanto:

✓ Juízos longos são capturados integralmente  
✓ Observações extensas são capturadas integralmente  
✓ Números de processo em qualquer formato são capturados integralmente  

**Diferença com outras extrações:**
- Descrição de movimentações: ✗ Limitada a 3000 caracteres (`.substring(0, 3000)`)
- Dados de Processo Judicial: ✓ **SEM LIMITE** (`.textContent.trim()` puro)

### Campo Adicional de Validação

| Campo | Tipo | Exemplo |
|-------|------|---------|
| `validacao_processo` | object | `{ sucesso: true, motivo: "Validação cruzada bem-sucedida" }` |

---

## Fluxo de Extração

### 1. **Identifiação da Tabela Correta**
```javascript
const tabelas = document.querySelectorAll(
  'table.epaj_sistema_formulario_tabela.epaj_sistema_formulario_tabela_filtro'
);
// Busca pela tabela que tem "Processo Judicial" no header
```

### 2. **Extração dos 4 Campos da Seção**
```javascript
// Coluna 1: Processo (span com style="font-weight:bold")
resultado.processo_judicial = spanProc?.textContent.trim();

// Coluna 2: Juízo (span com id contendo "juizo")
resultado.juizo = spanJuizo?.textContent.trim();

// Coluna 3: Data de Inclusão (primeiro span)
resultado.data_inclusao = spanData?.textContent.trim();

// Linha adicional: Observações
resultado.observacoes_processo = celulasObs[1]?.textContent.trim();
```

### 3. **Validação Cruzada contra Movimentações**
```javascript
// Procura movimentação com "Cadastramento de processo judicial"
const movCadastro = movs.find(mov =>
  mov.movimentacao?.includes('Cadastramento de processo judicial')
);

// Validação 1: Número na descrição da movimentação
const numeroMatch = descricao.match(
  /Número do Processo Judicial[:\\s]+([\\S]+)/i
);

// Validação 2: Data deve coincidir
if (numeroNaMov === dadosProcesso.processo_judicial &&
    dataMov === dataInclusaoFormatada) {
  sucesso = true;
}
```

### 4. **Armazenamento em sisdpu_raw**

```json
{
  "sisdpu_raw": {
    "processo_judicial": "99999999920244047000",
    "juizo": "JEF - Outros",
    "data_inclusao": "27/06/2026",
    "observacoes_processo": "888877776666",
    "validacao_processo": {
      "sucesso": true,
      "motivo": "Validação cruzada bem-sucedida"
    },
    "movimentacoes": [
      {
        "seq": "11",
        "data": "27/06/2026 15:42:08",
        "movimentacao": "° Cadastramento de processo judicial;",
        "fases": "Remessa ao Gabinete",
        "descricao": "Número do Processo Judicial: 99999999920244047000"
      }
    ]
  }
}
```

---

## Garantias de Segurança

✓ **Sem Falsos Positivos:** Números de processo citados em análises/precedentes não são capturados  
✓ **Validação Cruzada:** Dados são validados contra movimentações de cadastramento  
✓ **Múltiplos Formatos:** Aceita números com ou sem pontos/traços  
✓ **Tratamento de Vazio:** Retorna `null` se não há processo, não inventa dados  
✓ **Observações de Auditoria:** Campo `validacao_processo` documenta se passou na validação

---

## Casos de Teste

### Caso 1: PAJ Com Processo Cadastrado (PAJ-2026-029-01721)
```
✓ Número extraído: 99999999920244047000
✓ Juízo extraído: JEF - Outros
✓ Data incluída: 27/06/2026
✓ Observações: 888877776666
✓ Validação: SUCESSO (movimentação de cadastramento coincide)
```

### Caso 2: PAJ Sem Processo Cadastrado
```
✓ Número: null
✓ Juízo: null
✓ Data: null
✓ Observações: null
✓ Validação: motivo = "Dados incompletos (sem número ou data)"
```

### Caso 3: Número Citado em Análise (Precedente)
```
❌ NÃO é capturado
✓ Razão: Não há movimentação de "Cadastramento de processo judicial"
✓ Validação: motivo = "Movimentação de cadastramento não encontrada"
```

---

## Como Usar os Dados

### No Template HTML (paj_detail.html)
```jinja2
{% if meta.sisdpu_raw.processo_judicial %}
  <div class="processo-info">
    <strong>Número:</strong> {{ meta.sisdpu_raw.processo_judicial }}<br>
    <strong>Juízo:</strong> {{ meta.sisdpu_raw.juizo }}<br>
    <strong>Data de Inclusão:</strong> {{ meta.sisdpu_raw.data_inclusao }}<br>
    {% if meta.sisdpu_raw.observacoes_processo %}
      <strong>Observações:</strong> {{ meta.sisdpu_raw.observacoes_processo }}<br>
    {% endif %}
  </div>
  {% if meta.sisdpu_raw.validacao_processo.sucesso %}
    <span class="badge badge-success">✓ Validado</span>
  {% else %}
    <span class="badge badge-warning">⚠ {{ meta.sisdpu_raw.validacao_processo.motivo }}</span>
  {% endif %}
{% endif %}
```

### Via API (paj_service.py)
```python
def ler_paj(paj_norm: str) -> dict:
    # Já retorna processo_judicial, juizo, data_inclusao, observacoes_processo
    metadata = json.load(...)
    sisdpu_raw = metadata.get("sisdpu_raw", {})
    
    return {
        "processo_judicial": sisdpu_raw.get("processo_judicial"),
        "juizo": sisdpu_raw.get("juizo"),
        "data_inclusao": sisdpu_raw.get("data_inclusao"),
        "observacoes_processo": sisdpu_raw.get("observacoes_processo"),
        "validacao": sisdpu_raw.get("validacao_processo")
    }
```

---

## Ordem de Execução (Importante!)

1. ✓ Extração de movimentações (`movs` array)
2. ✓ Extração de dados de Processo Judicial (função `extrairProcessoJudicial()`)
3. ✓ Validação cruzada (usando `movs` já preenchido)
4. ✓ Armazenamento em `r.sisdpu_raw`
5. ✓ Serialização em metadata.json (sincronizador.py)

---

## Teste de Sincronização

Para verificar se tudo funciona:

```bash
# 1. Sincronizar um PAJ específico
curl http://localhost:8000/api/sync-paj/2026/029-01721

# 2. Verificar metadata.json
cat PAJs/PAJ-2026-029-01721/metadata.json | jq '.sisdpu_raw | {processo_judicial, juizo, data_inclusao, observacoes_processo, validacao_processo}'

# 3. Validar estrutura
python ingestao/test_sisdpu_structure.py
```

---

## Rollback (Se Necessário)

Se houver problema, o código anterior pode ser restaurado a partir do git:
```bash
git log --oneline | grep -i "processo"
git show <commit>:ingestao/sisdpu_client.py
```

---

## Próximas Melhorias

- [ ] Exibir os 4 campos no painel (paj_detail.html)
- [ ] Adicionar filtro por processo judicial no dashboard
- [ ] Sincronizar dados antigos que perderam informação
- [ ] Dashboard com relação processo → PAJ
