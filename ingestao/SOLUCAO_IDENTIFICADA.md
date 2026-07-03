# ✅ SOLUÇÃO IDENTIFICADA - Variação de Estrutura no PAJ 2026/029-01486

## Problema Original
O script funcionava perfeitamente para:
- ✅ PAJ 2025/029-03896 (40 documentos)
- ✅ PAJ 2026/029-01279 (13 documentos)

Mas retornava 0 documentos para:
- ❌ PAJ 2026/029-01486

## Causa Raiz: ORDEM DAS COLUNAS INVERTIDA

### Padrão A (PAJs 2025/029-03896 e 2026/029-01279)
```
[0] Tipo de Arquivo
[1] Descrição
[2] Nome do Arquivo
[3+] Link/Operações
```

**Exemplo:**
```
PDF             | Receituário Médico | documento.pdf | Visualizar
```

### Padrão B (PAJ 2026/029-01486)
```
[0] Descrição (ex: "Documentos do Assistido")
[1] Tipo de Arquivo (ex: "REGISTROS FOTOGRÁFICOS")
[2] Nome do Arquivo (ex: "Registros fotograficos.pdf")
[3] Link "Visualizar"
```

**Exemplo:**
```
Documentos do Assistido | REGISTROS FOTOGRÁFICOS | Registros fotograficos.pdf | Visualizar
```

## Solução Implementada

### 1. Detecção Automática de Padrão
```javascript
// Detectar qual padrão está sendo usado olhando para a primeira linha
let padraoDescricaoEmPrimeiro = false;
if (docRows.length > 0) {
  const firstRow = docRows[0];
  const firstRowCells = firstRow.querySelectorAll('td');
  if (firstRowCells.length >= 3) {
    const cell0 = firstRowCells[0]?.textContent.trim().toLowerCase();
    
    // Se célula 0 contém "documento" ou "assistido", é descrição
    if (cell0.includes('documento') || cell0.includes('assistido')) {
      padraoDescricaoEmPrimeiro = true;
    }
  }
}
```

### 2. Extração Condicional
```javascript
let tipoArquivo, descricao, nomeArquivo;

if (padraoDescricaoEmPrimeiro) {
  // Padrão B: [0] Descrição | [1] Tipo | [2] Arquivo
  descricao = docCells[0]?.textContent.trim() || '';
  tipoArquivo = docCells[1]?.textContent.trim() || '';
  nomeArquivo = docCells[2]?.textContent.trim() || '';
} else {
  // Padrão A: [0] Tipo | [1] Descrição | [2] Arquivo
  tipoArquivo = docCells[0]?.textContent.trim() || '';
  descricao = docCells[1]?.textContent.trim() || '';
  nomeArquivo = docCells[2]?.textContent.trim() || '';
}
```

### 3. Filtro de Linhas Vazias
```javascript
// Validar: deve ter pelo menos nome de arquivo
if (nomeArquivo && nomeArquivo !== 'Nenhum arquivo encontrado.') {
  documentos.push({
    tipoArquivo: tipoArquivo,
    descricao: descricao,
    nomeArquivo: nomeArquivo,
    urlDownload: urlDownload
  });
}
```

## Resultado Esperado

Com a solução aplicada, o PAJ 2026/029-01486 deve extrair:

### SEQ 10 (2 documentos)
1. **Tipo:** REGISTROS FOTOGRÁFICOS  
   **Descrição:** Documentos do Assistido  
   **Arquivo:** Registros fotograficos.pdf  
   **URL:** `https://sisdpu.dpu.def.br/sisdpu/rest/download/pdf/42205982/Registros%20fotograficos.pdf`

2. **Tipo:** CADÚNICO  
   **Descrição:** Documentos do Assistido  
   **Arquivo:** Cadunico.pdf  
   **URL:** `https://sisdpu.dpu.def.br/sisdpu/rest/download/pdf/[ID]/Cadunico.pdf`

### SEQ 5 (1 ou mais documentos)
_Estrutura similar_

### SEQ 3 (1 ou mais documentos)
_Estrutura similar_

## Variações Identificadas no SISDPU

Esta análise revelou que **PAJs diferentes podem ter estruturas HTML ligeiramente diferentes**. Outros pontos de variação possíveis:

1. ✅ **Ordem das colunas** (RESOLVIDO)
2. ⚠️ Número de colunas (pode ter mais colunas de "operações")
3. ⚠️ Headers podem ter espaços ou capitalização variável
4. ⚠️ Tempo de carregamento do popup (pode precisar de 2000-3000ms)
5. ⚠️ Tipo de elemento clicável (sempre `<a>`, mas pode ter classes diferentes)

## Próximos Testes

Para validar a solução, execute:
```javascript
// Script em extract_documents_final.js
// Com as mudanças aplicadas na PASSO 6
```

O resultado deve ser um objeto como:
```javascript
{
  paj: "2026/029-01486",
  assistido: "LIA MARA ALVES",
  movimentacoes_com_documentos: [
    {
      seq: "10",
      dataHora: "25/05/2026 11:46:14",
      movimentacao: "MOVIMENTAÇÃO MANUAL",
      documentos: [
        {
          tipoArquivo: "REGISTROS FOTOGRÁFICOS",
          descricao: "Documentos do Assistido",
          nomeArquivo: "Registros fotograficos.pdf",
          urlDownload: "https://sisdpu.dpu.def.br/sisdpu/rest/download/pdf/42205982/Registros%20fotograficos.pdf"
        },
        {
          tipoArquivo: "CADÚNICO",
          descricao: "Documentos do Assistido",
          nomeArquivo: "Cadunico.pdf",
          urlDownload: "https://sisdpu.dpu.def.br/sisdpu/rest/download/pdf/[ID]/Cadunico.pdf"
        }
      ]
    },
    // SEQ 5...
    // SEQ 3...
  ],
  total_documentos: 6
}
```
