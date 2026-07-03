/**
 * SCRIPT DE EXTRAÇÃO DE DOCUMENTOS - VERSÃO FINAL
 * Procura dynamicamente pela tabela de movimentações
 * Verifica REALMENTE se há anexos antes de processar
 */

(async function() {
  const resultado = {
    paj: '',
    assistido: '',
    movimentacoes_com_documentos: [],
    total_documentos: 0,
    debug: []
  };

  try {
    // ═════════════════════════════════════════════════════════════════
    // PASSO 1: Extrair informações básicas do PAJ
    // ═════════════════════════════════════════════════════════════════
    const pageText = document.body.innerText;

    const pajMatch = pageText.match(/Número\s+do\s+PAJ\s+(\d{4}\/\d{3}-\d+)/);
    resultado.paj = pajMatch ? pajMatch[1] : 'Desconhecido';

    const assistidoMatch = pageText.match(/Assistido\(s\)\s+([A-Z\sÀ-ÿ]+?)(?:\n|Data|Decurso|CPF)/);
    resultado.assistido = assistidoMatch ? assistidoMatch[1].trim() : 'Desconhecido';

    resultado.debug.push(`PAJ: ${resultado.paj}`);
    resultado.debug.push(`Assistido: ${resultado.assistido}`);

    // ═════════════════════════════════════════════════════════════════
    // PASSO 2: ENCONTRAR A TABELA DE MOVIMENTAÇÕES
    // Procurar pela tabela que tem AMBAS: "Seq." E "Data/Hora"
    // ═════════════════════════════════════════════════════════════════
    const tables = document.querySelectorAll('table');
    let tableMovimentacoes = null;
    let tableIndex = -1;

    for (let i = 0; i < tables.length; i++) {
      const headers = Array.from(tables[i].querySelectorAll('thead th, tbody tr:first-child td'))
        .map(h => h.textContent.trim())
        .filter(h => h.length > 0);

      if (headers.includes('Seq.') && headers.includes('Data/Hora')) {
        tableMovimentacoes = tables[i];
        tableIndex = i;
        resultado.debug.push(`✓ Tabela de movimentações encontrada no índice ${i}`);
        break;
      }
    }

    if (!tableMovimentacoes) {
      resultado.debug.push('✗ Tabela de movimentações não encontrada');
      return resultado;
    }

    // ═════════════════════════════════════════════════════════════════
    // PASSO 3: VERIFICAR SE REALMENTE HÁ MOVIMENTAÇÕES COM ANEXO
    // ═════════════════════════════════════════════════════════════════
    const rowsMovimentacoes = tableMovimentacoes.querySelectorAll('tbody tr');
    resultado.debug.push(`Total de linhas na tabela: ${rowsMovimentacoes.length}`);

    const seqsComAnexo = [];

    for (let i = 0; i < rowsMovimentacoes.length; i++) {
      const row = rowsMovimentacoes[i];
      const cells = row.querySelectorAll('td');

      if (cells.length < 3) continue;

      const seq = cells[0]?.textContent.trim();
      const anexoCell = cells[1];
      const dataHora = cells[2]?.textContent.trim();
      const movimentacao = cells[3]?.textContent.trim();

      // VERIFICAR se realmente tem um elemento clicável (a, button, img, etc)
      const temClicavel = !!anexoCell.querySelector('a, button, [role="button"], [onclick], img[onclick], span[onclick]');

      if (temClicavel && seq && /^\d+$/.test(seq)) {
        seqsComAnexo.push({
          seq: seq,
          dataHora: dataHora,
          movimentacao: movimentacao,
          rowIndex: i
        });
      }
    }

    resultado.debug.push(`Movimentações com ANEXO VERIFICADO: ${seqsComAnexo.length}`);

    if (seqsComAnexo.length === 0) {
      resultado.debug.push('⚠ Nenhuma movimentação com anexo detectada neste PAJ');
      return resultado;
    }

    // ═════════════════════════════════════════════════════════════════
    // PASSO 4: Para cada movimentação com anexo, extrair documentos
    // ═════════════════════════════════════════════════════════════════
    for (let idx = 0; idx < seqsComAnexo.length; idx++) {
      const mov = seqsComAnexo[idx];
      resultado.debug.push(`\n[${idx + 1}/${seqsComAnexo.length}] Processando SEQ ${mov.seq}`);

      // Clicar no anexo
      const row = rowsMovimentacoes[mov.rowIndex];
      const anexoCell = row.querySelectorAll('td')[1];
      const clickable = anexoCell.querySelector('a, button, [role="button"], [onclick], img[onclick], span[onclick]');

      if (!clickable) {
        resultado.debug.push(`  ✗ Não conseguiu encontrar elemento clicável`);
        continue;
      }

      clickable.click();
      resultado.debug.push(`  ✓ Clicou no anexo`);

      await new Promise(r => setTimeout(r, 1500));

      // ═════════════════════════════════════════════════════════════════
      // PASSO 5: Encontrar tabela de documentos (procura de trás para frente)
      // ═════════════════════════════════════════════════════════════════
      const allTables = document.querySelectorAll('table');
      let tableDoc = null;

      for (let j = allTables.length - 1; j >= Math.max(0, allTables.length - 40); j--) {
        const t = allTables[j];
        const headers = Array.from(t.querySelectorAll('thead th, tbody tr:first-child td'))
          .map(h => h.textContent.trim())
          .filter(h => h.length > 0);

        if (headers.includes('Arquivo')) {
          tableDoc = t;
          resultado.debug.push(`  ✓ Tabela de documentos encontrada (índice ${j})`);
          break;
        }
      }

      if (!tableDoc) {
        resultado.debug.push(`  ✗ Tabela de documentos não encontrada`);
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        await new Promise(r => setTimeout(r, 600));
        continue;
      }

      // ═════════════════════════════════════════════════════════════════
      // PASSO 6: Extrair documentos
      // NOTA: Ordem das colunas pode variar:
      // - Padrão A: [0] Tipo | [1] Descrição | [2] Arquivo
      // - Padrão B: [0] Descrição | [1] Tipo | [2] Arquivo
      // Detectar qual padrão está sendo usado
      // ═════════════════════════════════════════════════════════════════
      const documentos = [];
      const docRows = tableDoc.querySelectorAll('tbody tr');

      // Detectar padrão olhando para a primeira linha com dados
      let padraoDescricaoEmPrimeiro = false;
      if (docRows.length > 0) {
        const firstRow = docRows[0];
        const firstRowCells = firstRow.querySelectorAll('td');
        if (firstRowCells.length >= 3) {
          const cell0 = firstRowCells[0]?.textContent.trim().toLowerCase();
          const cell1 = firstRowCells[1]?.textContent.trim().toLowerCase();

          // Se célula 0 contém "documento" ou "assistido", é descrição
          // Se célula 1 contém termos técnicos (pdf, etc), é o tipo
          if (cell0.includes('documento') || cell0.includes('assistido')) {
            padraoDescricaoEmPrimeiro = true;
            resultado.debug.push(`  ℹ Padrão detectado: Descrição em [0], Tipo em [1]`);
          } else {
            resultado.debug.push(`  ℹ Padrão detectado: Tipo em [0], Descrição em [1]`);
          }
        }
      }

      for (const docRow of docRows) {
        const docCells = docRow.querySelectorAll('td');
        if (docCells.length < 3) continue;

        // Extrair na ordem correta detectada
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

        const linkVis = docRow.querySelector('a');
        const urlDownload = linkVis?.href || '';

        // Validar: deve ter pelo menos nome de arquivo
        if (nomeArquivo && nomeArquivo !== 'Nenhum arquivo encontrado.') {
          documentos.push({
            tipoArquivo: tipoArquivo,
            descricao: descricao,
            nomeArquivo: nomeArquivo,
            urlDownload: urlDownload
          });
        }
      }

      resultado.debug.push(`  ✓ Documentos extraídos: ${documentos.length}`);

      if (documentos.length > 0) {
        resultado.movimentacoes_com_documentos.push({
          seq: mov.seq,
          dataHora: mov.dataHora,
          movimentacao: mov.movimentacao,
          documentos: documentos
        });
        resultado.total_documentos += documentos.length;

        for (const doc of documentos) {
          resultado.debug.push(`    📄 ${doc.nomeArquivo}`);
        }
      }

      // Fechar popup
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await new Promise(r => setTimeout(r, 700));
    }

    resultado.debug.push(`\n✅ EXTRAÇÃO CONCLUÍDA`);
    resultado.debug.push(`Total de documentos: ${resultado.total_documentos}`);

  } catch (e) {
    resultado.debug.push(`❌ ERRO: ${e.message}`);
  }

  return resultado;
})()
