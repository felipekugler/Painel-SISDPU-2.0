# Opções de Extração - Número do Processo Judicial

## Requisitos Refinados

1. **Formatos aceitos:** O número pode estar:
   - Com pontos e traços: `5023417-54.2024.4.04.7000` (formato CNJ)
   - Sem formatação: `50564390620244047000` (apenas dígitos)
   - Outros formatos desconhecidos

2. **Pode estar vazio:** Nem todo PAJ tem processo cadastrado

3. **Escopo limitado:** Extração OBRIGATORIAMENTE dentro da seção de "Processo Judicial"

## Problema Atual

A extração via regex genérica captura QUALQUER número que apareça no texto, causando falsos positivos:
- Citações de precedentes em análises
- Números de processo em exemplos documentados
- Números mencionados casualmente em descrições
- Padrões CNJ e não-CNJ misturados indiscriminadamente

## Estrutura Correta da Seção no SISDPU

```html
<!-- Seção específica de Processo Judicial -->
<table class="epaj_sistema_formulario_tabela epaj_sistema_formulario_tabela_filtro">
  <tbody>
    <tr>
      <th>Processo Judicial</th>
      <th>Juízo/Orgão Julgador</th>
      <th>Data de Inclusão</th>
    </tr>
    <tr>
      <!-- PROCESSO -->
      <td style="white-space: nowrap;">
        <a href="...">
          <span style="font-weight:bold">50564390620244047000</span>
        </a>
      </td>
      <!-- JUÍZO -->
      <td>
        <span id="...juizo">VFCTB10 - 10.ª Vara Federal de Curitiba</span>
      </td>
      <!-- DATA -->
      <td><span style="text-align: center;">14/11/2024</span></td>
    </tr>
    <tr>
      <!-- OBSERVAÇÕES -->
      <td><strong>Observações:</strong></td>
      <td colspan="4">793593464924</td>
    </tr>
  </tbody>
</table>
```

---

## OPÇÕES DE EXTRAÇÃO

### OPÇÃO 1: Busca por Identificador de Elemento (ID) - ⭐ RECOMENDADA
**Nível de Confiança:** ⭐⭐⭐⭐⭐ (Mais seguro)

**Método:** Procura pelo `id` da célula do juízo e extrai o primeiro `span` da célula anterior.

**Código JavaScript:**
```javascript
// Buscar pela coluna de juízo que tem um ID específico
const juizoSpan = document.querySelector('span[id*="juizo"]');
if (juizoSpan) {
  // Juízo encontrado = processo está preenchido
  // Volta uma célula para trás (<td> anterior)
  const processoCelula = juizoSpan.closest('td').previousElementSibling;
  const numeroProcesso = processoCelula.querySelector('span[style*="font-weight"]')?.textContent;
}
```

**Vantagens:**
- ✓ Busca EXATAMENTE a célula correta
- ✓ Imune a outros números no documento
- ✓ Funciona mesmo se não há processo cadastrado (fica vazio)
- ✓ Identifica automaticamente qual processo está cadastrado (pode ter múltiplos)

**Desvantagens:**
- Requer que o SISDPU mantenha o padrão de ID `[id*="juizo"]`

---

### OPÇÃO 2: Busca por Contexto de Tabela Específica
**Nível de Confiança:** ⭐⭐⭐⭐ (Muito seguro)

**Método:** Identifica a tabela correta e extrai apenas números dentro dela.

**Código JavaScript:**
```javascript
// Buscar a tabela com classe específica
const tabelaProcessos = document.querySelector(
  'table.epaj_sistema_formulario_tabela.epaj_sistema_formulario_tabela_filtro'
);

if (tabelaProcessos) {
  // Primeira linha de dados (<tr> após <th>s)
  const firstRow = tabelaProcessos.querySelector('tbody tr:not(:first-child)');
  
  // Primeira célula contém o processo
  const processoCelula = firstRow.querySelector('td');
  const numeroProcesso = processoCelula
    .querySelector('span[style*="font-weight"]')
    ?.textContent || '';
}
```

**Vantagens:**
- ✓ Busca na tabela específica (classe única)
- ✓ Evita números em outras seções do documento
- ✓ Mais robusta que regex genérica
- ✓ Funciona mesmo se há múltiplos números em seções diferentes

**Desvantagens:**
- Requer que a classe CSS da tabela permaneça estável

---

### OPÇÃO 3: Busca por Padrão de Célula com Atributos
**Status:** ❌ NÃO VIÁVEL

**Razão:** Rejeita números sem pontos/traços, rejeitando formatos válidos como `50564390620244047000`.

```javascript
// ❌ DESCARTADO - não funciona com números sem pontos/traços
if (/^\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}$/.test(text)) {
  // Falha para: 50564390620244047000 (válido mas sem formatação)
}
```

---

### OPÇÃO 4: Busca por Posição Estrutural (XPath-like)
**Nível de Confiança:** ⭐⭐⭐⭐ (Muito seguro)

**Método:** Usa lógica de "primeira célula > primeiro span bold em tabela de classe X".

**Código JavaScript:**
```javascript
// Identifica tabela e busca estruturalmente
const processoElement = document.querySelector(
  'table.epaj_sistema_formulario_tabela_filtro ' +  // tabela correta
  'tbody tr:nth-child(2) ' +                        // segunda linha (primeira de dados)
  'td:first-child ' +                               // primeira célula
  'span[style*="font-weight"]'                      // span em negrito
);

if (processoElement) {
  const numeroProcesso = processoElement.textContent.trim();
}
```

**Vantagens:**
- ✓ Extremamente específico (posição exata)
- ✓ Não depende de IDs dinâmicos
- ✓ Documentação clara da estrutura esperada

**Desvantagens:**
- Quebraria se a ordem das linhas mudasse

---

### OPÇÃO 5: Busca por Data de Inclusão (Validação Dupla)
**Nível de Confiança:** ⭐⭐⭐⭐⭐ (Mais seguro)

**Método:** Busca a célula com a data de inclusão (formato DD/MM/YYYY) e extrai o número da mesma linha.

**Código JavaScript:**
```javascript
// Buscar célula que contém data no formato DD/MM/YYYY (coluna de Data)
const tabelaProcessos = document.querySelector(
  'table.epaj_sistema_formulario_tabela_filtro tbody'
);

let numeroProcesso = '';

if (tabelaProcessos) {
  const rows = tabelaProcessos.querySelectorAll('tr');
  
  for (const row of rows) {
    const cells = row.querySelectorAll('td');
    
    // Terceira célula contém data de inclusão
    if (cells.length >= 3) {
      const dataCelula = cells[2]?.textContent || '';
      
      // Se encontrou data no formato DD/MM/YYYY, prócessado está preenchido
      if (/\d{2}\/\d{2}\/\d{4}/.test(dataCelula)) {
        // Primeira célula contém o processo
        const procSpan = cells[0].querySelector('span[style*="font-weight"]');
        numeroProcesso = procSpan?.textContent.trim() || '';
        break;
      }
    }
  }
}
```

**Vantagens:**
- ✓ Validação dupla (data + processo)
- ✓ Confirma que processo foi realmente cadastrado
- ✓ Se não há data, não há processo válido
- ✓ Evita falsos positivos completamente

**Desvantagens:**
- Ligeiramente mais código

---

## TABELA COMPARATIVA

| Opção | Método | Status | Segurança | Complexidade | Formatos | Vazio |
|-------|--------|--------|-----------|--------------|----------|-------|
| 1 | ID de elemento | ✓ VIÁVEL | ⭐⭐⭐⭐⭐ | Baixa | ✓ Qualquer | ✓ Sim |
| 2 | Classe da tabela | ✓ VIÁVEL | ⭐⭐⭐⭐ | Baixa | ✓ Qualquer | ✓ Sim |
| 3 | Padrão CNJ | ❌ NÃO | — | — | ✗ Só CNJ | — |
| 4 | Posição estrutural | ✓ VIÁVEL | ⭐⭐⭐⭐ | Baixa | ✓ Qualquer | ✓ Sim |
| 5 | Data como validação | ✓ VIÁVEL | ⭐⭐⭐⭐⭐ | Média | ✓ Qualquer | ✓ Sim |

---

## MÉTODOS VIÁVEIS (após refinamento)

### ✓ OPÇÃO 1: ID de Elemento - RECOMENDADA
- Funciona com qualquer formato numérico
- Retorna vazio se não há processo
- Limitado à seção por ID HTML

### ✓ OPÇÃO 2: Classe da Tabela - EXCELENTE (fallback)
- Funciona com qualquer formato numérico
- Retorna vazio se não há processo
- Limitado à seção por classe CSS

### ✓ OPÇÃO 4: Posição Estrutural - VIÁVEL
- Funciona com qualquer formato numérico
- Retorna vazio se não há processo
- Limitado à seção por seletor nth-child

### ✓ OPÇÃO 5: Validação pela Data - MAIS SEGURA
- Funciona com qualquer formato numérico
- Confirmação dupla (data + número)
- Se não há data, não há processo cadastrado
- Limitado à seção por lógica de linha

### ❌ OPÇÃO 3: Padrão CNJ - DESCARTADA
- Rejeita números sem pontos/traços
- Rejeitaria `50564390620244047000` (válido)
- NÃO VIÁVEL com novos requisitos

---

## RECOMENDAÇÃO FINAL: Implementar OPÇÃO 1 + OPÇÃO 5

**Razão:** Combinação de especificidade máxima + validação de dados.

```javascript
// ESTRATÉGIA: ID específico + Validação pela data

let numeroProcesso = '';

// Método 1: Buscar por ID (mais específico)
const juizoSpan = document.querySelector('span[id*="juizo"]');
if (juizoSpan) {
  const processoCelula = juizoSpan.closest('td')?.previousElementSibling;
  const span = processoCelula?.querySelector('span[style*="font-weight"]');
  numeroProcesso = span?.textContent.trim() || '';
}

// Método 2: Validação dupla (se Método 1 falhou)
// Se encontrar data de inclusão, confirma que processo está preenchido
if (!numeroProcesso) {
  const tabelaProcessos = document.querySelector(
    'table.epaj_sistema_formulario_tabela_filtro tbody'
  );
  
  if (tabelaProcessos) {
    const rows = tabelaProcessos.querySelectorAll('tr');
    
    for (const row of rows) {
      const cells = row.querySelectorAll('td');
      
      // Terceira célula contém data (formato DD/MM/YYYY)
      if (cells.length >= 3) {
        const dataCelula = cells[2]?.textContent || '';
        
        // Se há data válida, processo está cadastrado
        if (/\d{2}\/\d{2}\/\d{4}/.test(dataCelula)) {
          const procSpan = cells[0]?.querySelector('span[style*="font-weight"]');
          numeroProcesso = procSpan?.textContent.trim() || '';
          break;
        }
      }
    }
  }
}

// Retorna número ou null (NUNCA usa regex genérica)
r.processo_judicial = numeroProcesso || null;
```

**Benefícios desta combinação:**
- ✓ Método 1: Busca direta no elemento ID (máxima especificidade)
- ✓ Método 2: Validação dupla pela data (evita falsos positivos)
- ✓ Qualquer formato numérico aceito (com ou sem pontos/traços)
- ✓ Retorna `null` se não houver processo (não inventa dados)
- ✓ NUNCA captura números de outras seções do documento
- ✓ Limitado exclusivamente à seção de "Processo Judicial"
