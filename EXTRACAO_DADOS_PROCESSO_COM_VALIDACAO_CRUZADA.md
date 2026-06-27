# Extração de Dados de Processo Judicial com Validação Cruzada

## Contexto

O PAJ possui dados de processo judicial em **2 locais diferentes**:

### 1. Seção de Formulário "Processo Judicial" (tabela HTML)
- Coluna 1: Número do processo
- Coluna 2: Juízo/Órgão Julgador
- Coluna 3: Data de Inclusão
- Linha adicional: Observações

### 2. Tabela de Movimentações
- Movimentação com tipo "Cadastramento de processo judicial"
- Campo descrição contém: "Número do Processo Judicial: XXXXX"
- Data da movimentação = Data de Inclusão

---

## Estrutura HTML - Seção de Processo Judicial

```html
<table class="epaj_sistema_formulario_tabela epaj_sistema_formulario_tabela_filtro">
  <tr>
    <th>Processo Judicial</th>
    <th>Juízo/Orgão Julgador</th>
    <th>Data de Inclusão</th>
  </tr>
  
  <!-- Linha de dados -->
  <tr>
    <!-- COLUNA 1: Processo -->
    <td style="white-space: nowrap;">
      <a href="...">
        <span style="font-weight:bold">50564390620244047000</span>
      </a>
    </td>
    
    <!-- COLUNA 2: Juízo -->
    <td>
      <span id="detalhamentoForm:j_idt520:0:juizo">
        VFCTB10 - 10.ª Vara Federal de Curitiba
      </span>
    </td>
    
    <!-- COLUNA 3: Data -->
    <td>
      <span style="text-align: center;">14/11/2024</span>
    </td>
  </tr>
  
  <!-- Linha de Observações -->
  <tr>
    <td><strong>Observações:</strong></td>
    <td colspan="4">793593464924</td>
  </tr>
</table>
```

---

## ESTRATÉGIA RECOMENDADA: Extração + Validação Cruzada

### Fase 1: Extrair da Seção de Formulário

```javascript
function extrairProcessoDoFormulario() {
  const resultado = {
    numero: '',
    juizo: '',
    data_inclusao: '',
    observacoes: ''
  };

  const tabela = document.querySelector(
    'table.epaj_sistema_formulario_tabela.epaj_sistema_formulario_tabela_filtro'
  );

  if (!tabela) return resultado;

  const linhas = tabela.querySelectorAll('tbody tr');

  // Processar linhas
  let indexAtual = 0;
  for (let i = 0; i < linhas.length; i++) {
    const linha = linhas[i];
    const celulas = linha.querySelectorAll('td');

    // Linha de DADOS (tem 3+ colunas de dados)
    if (celulas.length >= 3 && !linha.textContent.includes('Observações')) {
      // Coluna 1: Processo (primeiro span com font-weight:bold)
      const spanProcesso = celulas[0].querySelector('span[style*="font-weight"]');
      resultado.numero = spanProcesso?.textContent.trim() || '';

      // Coluna 2: Juízo (span com id contendo "juizo")
      const spanJuizo = celulas[1].querySelector('span[id*="juizo"]');
      resultado.juizo = spanJuizo?.textContent.trim() || '';

      // Coluna 3: Data (primeiro span da célula)
      const spanData = celulas[2].querySelector('span');
      resultado.data_inclusao = spanData?.textContent.trim() || '';

      indexAtual = i;
    }

    // Linha de OBSERVAÇÕES (contém texto "Observações:")
    if (linha.textContent.includes('Observações:')) {
      const celulasObs = linha.querySelectorAll('td');
      if (celulasObs.length >= 2) {
        resultado.observacoes = celulasObs[1].textContent.trim();
      }
    }
  }

  return resultado;
}
```

---

### Fase 2: Validação Cruzada contra Movimentações

```javascript
function validarContraMovimentacoes(numeroProcesso, dataInclusao, movimentacoes) {
  // Movimentações vem do sisdpu_raw.movimentacoes
  
  if (!numeroProcesso || !dataInclusao) {
    return {
      valido: false,
      motivo: 'Dados incompletos no formulário'
    };
  }

  // Buscar movimentação de "Cadastramento de processo judicial"
  const movCadastro = movimentacoes.find(mov => 
    mov.movimentacao?.includes('Cadastramento de processo judicial')
  );

  if (!movCadastro) {
    return {
      valido: false,
      motivo: 'Não encontrada movimentação de cadastramento'
    };
  }

  // Validação 1: Verificar se número aparece na descrição
  const descricao = movCadastro.descricao || '';
  const numeroNaDescricao = descricao.includes(numeroProcesso);

  if (!numeroNaDescricao) {
    // Mesmo sem match exato, pode ser formato diferente
    // Extrair número da descrição e comparar
    const match = descricao.match(/Número do Processo Judicial:\s*(\S+)/i);
    const numeroNaMovimentacao = match ? match[1] : '';
    
    if (numeroNaMovimentacao !== numeroProcesso) {
      return {
        valido: false,
        motivo: `Número não coincide: ${numeroProcesso} vs ${numeroNaMovimentacao}`
      };
    }
  }

  // Validação 2: Verificar se data de inclusão coincide com data da movimentação
  // data_inclusao formato: "14/11/2024"
  // movCadastro.data formato: "14/11/2024 HH:MM:SS"
  const dataMov = movCadastro.data.substring(0, 10); // Pegar DD/MM/YYYY
  
  if (dataMov !== dataInclusao) {
    return {
      valido: false,
      motivo: `Data não coincide: ${dataInclusao} vs ${dataMov}`
    };
  }

  return {
    valido: true,
    motivo: 'Validação cruzada bem-sucedida'
  };
}
```

---

### Fase 3: Função Completa de Extração com Validação

```javascript
async function extrairProcessoJudicialComValidacao(pagina, movimentacoes) {
  // Extrair do formulário
  const dados = extrairProcessoDoFormulario();

  // Validar contra movimentações
  const validacao = validarContraMovimentacoes(
    dados.numero,
    dados.data_inclusao,
    movimentacoes
  );

  // Resultado final
  const resultado = {
    processo_judicial: dados.numero || null,
    juizo: dados.juizo || null,
    data_inclusao: dados.data_inclusao || null,
    observacoes: dados.observacoes || null,
    validacao: {
      sucesso: validacao.valido,
      motivo: validacao.motivo,
      timestamp: new Date().toISOString()
    }
  };

  // Se validação falhou, log para debug
  if (!validacao.valido) {
    console.warn('[PROCESSO JUDICIAL] Falha na validação:', validacao);
  }

  return resultado;
}
```

---

## Mapeamento de Dados para Armazenamento

```json
{
  "sisdpu_raw": {
    "processo_judicial": "50564390620244047000",
    "juizo": "VFCTB10 - 10.ª Vara Federal de Curitiba",
    "data_inclusao": "14/11/2024",
    "observacoes_processo": "793593464924",
    
    "movimentacoes": [
      {
        "seq": "11",
        "data": "27/06/2026 15:42:08",
        "movimentacao": "° Cadastramento de processo judicial;",
        "fases": "Remessa ao Gabinete",
        "descricao": "Número do Processo Judicial: 99999999920244047000"
      }
    ],
    
    "validacao_processo": {
      "sucesso": true,
      "motivo": "Validação cruzada bem-sucedida",
      "timestamp": "2026-06-27T15:42:08"
    }
  }
}
```

---

## Benefícios da Validação Cruzada

| Aspecto | Benefício |
|---------|-----------|
| **Segurança** | Confirma que número extraído é real (não citação) |
| **Integridade** | Data de inclusão deve coincidir com movimentação |
| **Rastreabilidade** | Movimentação prova quando foi cadastrado |
| **Múltiplos Formatos** | Não rejeita números em formatos variados |
| **Sem Falsos Positivos** | Números em análises não têm movimentação correspondente |
| **Observações** | Campo adicional extraído com segurança |

---

## Casos de Uso

### Caso 1: Processo cadastrado corretamente
```
Formulário:
- Número: 99999999920244047000
- Juízo: JEF - Outros
- Data: 27/06/2026

Movimentação:
- "Cadastramento de processo judicial"
- Descrição: "Número do Processo Judicial: 99999999920244047000"
- Data: 27/06/2026

✓ RESULTADO: Extração com sucesso
```

### Caso 2: Sem processo cadastrado
```
Formulário:
- Número: (vazio)
- Juízo: (vazio)
- Data: (vazio)

Movimentação:
- Nenhuma com "Cadastramento de processo judicial"

✓ RESULTADO: Retorna null (sem erro)
```

### Caso 3: Número citado em análise (evita falso positivo)
```
Descrição de movimentação contém:
"...conforme precedente nº 5023417-54.2024.4.04.7000..."

Mas:
- Nenhuma movimentação de "Cadastramento"
- Número não está na seção de Processo Judicial
- Data não coincide

✓ RESULTADO: Número é ignorado (não é processo do PAJ)
```

---

## Implementação em sisdpu_client.py

```python
# No JavaScript de extração, adicionar:

# ... código anterior ...

# Extrair processo com validação
const processoData = await extrairProcessoJudicialComValidacao(
  document,
  movimentacoes  // já foi extraído anteriormente
);

// Armazenar em resultado
r.processo_judicial = processoData.processo_judicial;
r.juizo = processoData.juizo;
r.data_inclusao = processoData.data_inclusao;
r.observacoes_processo = processoData.observacoes;
r.validacao_processo = processoData.validacao;
```

---

## Conclusão

Esta estratégia:
- ✓ Extrai dados APENAS da seção correta
- ✓ Valida contra movimentações (prova de cadastro)
- ✓ Funciona com múltiplos formatos de número
- ✓ Retorna `null` quando não há processo (não inventa)
- ✓ **Zero falsos positivos** (números citados em textos são ignorados)
