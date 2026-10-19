/* "Movimentar Eproc + SISDPU" — orquestra os dois cards (Ações no Eproc +
   Ações no SISDPU), o resumo combinado e a execução conjunta.

   Usado em DOIS lugares (19/10/2026):
   - página própria /movimentar-conjunto (templates/movimentar_conjunto.html),
     quem chega pelo minimenu de processo/PAJ: PAJ e processo vêm da URL e se
     resolvem um pelo outro;
   - EMBUTIDO na página do PAJ (seta dupla entre "Ações no SISDPU" e "Ações no
     e-proc"): `cfg.pajNorm` já vem da página e o processo sai dos vinculados
     ao PAJ — o Defensor segue rolando a página enquanto minuta.
   O bloco de tela (cards + "Revisar e executar" + modal) é o partial
   templates/partials/movimentar_conjunto_painel.html. */

function movimentarConjuntoApp(cfg) {
  cfg = cfg || {};
  return {
    pajInput: '', pajNorm: '', pajLabel: '', pajCandidatos: [], pajResolvendo: false,
    processoInput: '', processoConfirmado: '', processoCandidatos: [], processoResolvendo: false,

    montandoResumo: false, resumoErro: '',
    modalResumoAberto: false, etapaConjunta: 'resumo',
    resumoEproc: null, resumoSisdpu: null,
    // Fonte SIS-DPU 2.0 (MCP): fichas integrais do SISDPU mostradas no resumo,
    // ANTES do e-proc — aprovadas junto com "Confirmar e executar tudo".
    fichasMcp: [], fichasMcpErros: [], fichasMcpCarregando: false, resumoFonteMcp: false,
    // Um bloco por item da fila (sem fila, um só): ficha da movimentação e da
    // tramitação do item (o servidor marca `item` em cada ficha) e os trâmites
    // que o item conclui.
    blocosRevisao() {
      const r = this.resumoSisdpu || { fila: [], formularioAtual: null };
      const n = Math.max(r.fila.length || 1, ...this.fichasMcp.map((f) => (f.item || 0) + 1));
      const blocos = [];
      for (let i = 0; i < n; i++) {
        let conclusoes = [];
        if (r.fila.length) {
          conclusoes = (r.fila[i] && r.fila[i].concluir_detalhes) || [];
        } else if (r.formularioAtual && r.formularioAtual.concluir) {
          conclusoes = (r.formularioAtual.concluir_detalhes || []).length ? r.formularioAtual.concluir_detalhes
            : [{ descricao: 'Concluir o trâmite da caixa de entrada', remetente: '', data_envio: '' }];
        }
        blocos.push({ movimentacao: null, tramitacao: null, etapa: null, conclusoes });
      }
      this.fichasMcp.forEach((f) => {
        const b = blocos[f.item || 0];
        if (b) b[f.tipo === 'tramitacao' ? 'tramitacao' : f.tipo === 'etapa' ? 'etapa' : 'movimentacao'] = f;
      });
      return blocos;
    },
    // Número CNJ com pontos e hífen: 5055128-09.2026.4.04.7000
    fmtProcesso(n) {
      const d = String(n || '').replace(/\D/g, '');
      if (d.length !== 20) return n || '';
      return d.slice(0, 7) + '-' + d.slice(7, 9) + '.' + d.slice(9, 13) + '.' + d.slice(13, 14)
        + '.' + d.slice(14, 16) + '.' + d.slice(16);
    },
    // Trâmites marcados para concluir (formulário atual ou itens da fila) — card próprio no resumo.
    conclusoesResumo() {
      const r = this.resumoSisdpu;
      if (!r) return [];
      const doForm = (r.formularioAtual && r.formularioAtual.concluir && !r.fila.length)
        ? ((r.formularioAtual.concluir_detalhes || []).length ? r.formularioAtual.concluir_detalhes
           : [{ descricao: 'Concluir o trâmite da caixa de entrada', remetente: '', data_envio: '' }]) : [];
      return doForm.concat(...r.fila.map((it) => it.concluir_detalhes || []));
    },
    resultadoEproc: null, resultadoGerenciamento: null, erroExecucao: '',

    async init() {
      // Embutido na página do PAJ: o PAJ já é conhecido — só resolve o processo.
      if (cfg.pajNorm) {
        this.pajNorm = cfg.pajNorm;
        this.pajLabel = cfg.pajLabel || this.pajNormParaLabel(cfg.pajNorm);
        this.pajInput = this.pajLabel;
        await this.resolverProcessosDoPaj();
        return;
      }
      const params = new URLSearchParams(window.location.search);
      const paj = params.get('paj') || '';
      const processo = params.get('processo') || '';
      if (paj) { this.pajInput = paj; await this.confirmarPaj(); }
      if (processo) { this.processoInput = processo; await this.confirmarProcesso(); }
      // Só um dos dois veio (caso comum — minimenu conhece só um identificador):
      // tenta resolver o outro automaticamente, cruzando intimações/prazos/cadastro
      // do PAJ (ver services.paj_processo_link_service).
      if (this.pajNorm && !this.processoConfirmado) await this.resolverProcessosDoPaj();
      if (this.processoConfirmado && !this.pajNorm) await this.resolverPajsDoProcesso();
    },

    // --- PAJ ---
    // Aceita tanto "AAAA/UUU-NNNNN" (digitado à mão) quanto já normalizado
    // "PAJ-AAAA-UUU-NNNNN" (vindo do minimenu, via movimentarConjunto() —
    // ver static/js/app.js) — o rótulo bonito é sempre DERIVADO do normalizado,
    // nunca o contrário, pra nunca ficar com os dois fora de sincronia.
    pajNormParaLabel(norm) {
      const m = (norm || '').match(/^PAJ-(\d{4})-(\d{3})-(\d{5})$/);
      return m ? (m[1] + '/' + m[2] + '-' + m[3]) : '';
    },
    async confirmarPaj() {
      const raw = (this.pajInput || '').trim();
      let norm = '';
      const mBarra = raw.match(/^(\d{4})\/(\d{3})-(\d{5})$/);
      if (mBarra) norm = 'PAJ-' + mBarra[1] + '-' + mBarra[2] + '-' + mBarra[3];
      else if (/^PAJ-\d{4}-\d{3}-\d{5}$/.test(raw)) norm = raw;
      if (!norm) { if (raw) alert('Formato de PAJ inválido — use AAAA/UUU-NNNNN.'); return; }
      this.pajNorm = norm;
      this.pajLabel = this.pajNormParaLabel(norm);
      this.pajInput = this.pajLabel;
      this.pajCandidatos = [];
      if (!this.processoConfirmado) await this.resolverProcessosDoPaj();
    },
    async resolverProcessosDoPaj() {
      if (!this.pajNorm) return;
      this.processoResolvendo = true;
      try {
        const j = await (await fetch('/api/movimentar-conjunto/resolver-processos?paj=' + encodeURIComponent(this.pajNorm))).json();
        const cand = j.candidatos || [];
        this.processoCandidatos = cand;
        if (cand.length === 1) { this.processoConfirmado = cand[0].numero; this.processoInput = cand[0].numero; }
      } catch (e) {}
      finally { this.processoResolvendo = false; }
    },

    // --- Processo ---
    async confirmarProcesso() {
      const numero = (this.processoInput || '').trim();
      if (!numero) return;
      this.processoConfirmado = numero;
      this.processoCandidatos = [];
      if (!this.pajNorm) await this.resolverPajsDoProcesso();
    },
    async resolverPajsDoProcesso() {
      if (!this.processoConfirmado) return;
      this.pajResolvendo = true;
      try {
        const j = await (await fetch('/api/movimentar-conjunto/resolver-pajs?processo=' + encodeURIComponent(this.processoConfirmado))).json();
        const cand = j.candidatos || [];
        this.pajCandidatos = cand;
        if (cand.length === 1) { this.pajNorm = cand[0].paj_norm; this.pajLabel = cand[0].paj_label; this.pajInput = cand[0].paj_label; }
      } catch (e) {}
      finally { this.pajResolvendo = false; }
    },
    onEscolherPajCandidato() {
      const c = this.pajCandidatos.find((x) => x.paj_norm === this.pajNorm);
      this.pajLabel = c ? c.paj_label : '';
      this.pajInput = this.pajLabel;
    },

    // --- Execução conjunta ---
    // Pega os dados reativos de cada card DE FORA do seu próprio x-data — via
    // `document.getElementById` (não `$refs`): `x-ref` no MESMO elemento que
    // `x-data` registra o ref dentro do escopo do PRÓPRIO componente filho, não
    // no escopo desta página (bug real 2026-09-11: "Cannot read properties of
    // undefined (reading '_x_dataStack')" — `this.$refs.eprocCard` vinha
    // indefinido). `id` + `getElementById` é DOM puro, sem essa ambiguidade.
    eprocDados() {
      const el = document.getElementById('eprocCard');
      return el ? Alpine.$data(el) : null;
    },
    sisdpuDados() {
      const el = this.pajNorm ? document.getElementById('sisdpuCard') : null;
      return el ? Alpine.$data(el) : null;
    },
    async prepararResumoConjunto() {
      this.resumoErro = '';
      this.montandoResumo = true;
      try {
        const eprocData = this.eprocDados();
        if (!eprocData) { this.resumoErro = 'Card "Ações no Eproc" ainda não está pronto — informe o processo acima.'; return; }
        await eprocData.gravarRascunho();
        const sisdpuData = this.sisdpuDados();
        if (sisdpuData) await sisdpuData.gravarRascunho();
        const j = await (await fetch('/api/acoes-eproc/executar/' + encodeURIComponent(eprocData.chaveRascunho()) + '/resumo')).json();
        if (!j.ok) { this.resumoErro = j.erro || 'Falha ao montar o resumo do e-proc.'; return; }
        this.resumoEproc = j;
        // Rótulo legível do tipo de gerenciamento (o backend só devolve a CHAVE —
        // "S"/"C"/"D"/"T" — ver TIPOS_GERENCIAMENTO no eproc_client; o catálogo com
        // os rótulos só existe no card do e-proc) — resolvido aqui, uma vez, pra o
        // template de revisão poder mostrar "Substituir procurador" em vez da sigla.
        if (this.resumoEproc.gerenciamento && this.resumoEproc.gerenciamento.ativo) {
            const g = this.resumoEproc.gerenciamento;
            g.tipo_label = (eprocData.tiposGerenciamento.find((t) => t.valor === g.tipo) || {}).rotulo || g.tipo;
        }
        // Reflete a FILA de movimentações (2+ em sequência — "Adicionar à fila"
        // no card, ver static/js/acoes_sisdpu.js:executarFila), não só o
        // formulário único de antes — sem isso, um SISDPU só com itens na fila
        // (fase/grupo do formulário atual vazios) aparecia como "sem
        // movimentação configurada" mesmo tendo o que executar (bug real
        // 2026-09-11: usuário com 1 petição + 1 arquivamento na fila).
        const temFormularioAtual = !!(sisdpuData && (sisdpuData.fase || sisdpuData.grupo || sisdpuData.concluirEfetivo()));
        // Descrição de verdade do(s) trâmite(s) marcado(s) pra concluir (não só um
        // "vai concluir" genérico) — cruza ids (do formulário atual via
        // idsTramiteSelecionadosArr, ou de cada item da fila via seu próprio
        // id_tramites) com `tramitacoesCaixa` (a mesma lista mostrada no card, com
        // descricao/remetente/data_envio) pra achar o objeto completo de cada um.
        // Reusado pelo formulário atual E por cada item da fila (2026-09-25) — o
        // card já tinha o texto certo (é o mesmo do checkbox), só faltava repassar.
        const tramitacoesPorId = {};
        ((sisdpuData && sisdpuData.tramitacoesCaixa) || []).forEach((t) => { tramitacoesPorId[t.id_tramite] = t; });
        const detalhesConcluir = (ids) => (ids || []).map((id) => tramitacoesPorId[id]).filter(Boolean)
          .map((t) => ({ descricao: t.descricao || '—', remetente: t.remetente || '', data_envio: t.data_envio || '' }));
        const idsConcluirSel = (sisdpuData && sisdpuData.idsTramiteSelecionadosArr) ? sisdpuData.idsTramiteSelecionadosArr() : [];
        this.resumoSisdpu = sisdpuData ? {
          fila: (sisdpuData.fila || []).map((item) => ({
            fase: item.fase, grupo: item.grupo, anexos: item.anexos || [],
            concluir_detalhes: detalhesConcluir(item.id_tramites),
          })),
          formularioAtual: temFormularioAtual ? {
            fase: sisdpuData.fase, grupo: sisdpuData.grupo,
            anexos: sisdpuData.anexosStagedParaEnviar(), concluir: sisdpuData.concluirEfetivo(),
            concluir_detalhes: detalhesConcluir(idsConcluirSel),
          } : null,
        } : null;
        this.resultadoEproc = null; this.resultadoGerenciamento = null; this.erroExecucao = '';
        this.fichasMcp = []; this.fichasMcpErros = []; this.resumoFonteMcp = false;
        this.etapaConjunta = 'resumo';
        this.modalResumoAberto = true;
        // Fonte SIS-DPU 2.0: pede ao servidor as fichas do que o card do SISDPU vai
        // gravar (nada é gravado aqui) e as mostra no resumo.
        if (sisdpuData && this.resumoSisdpu) {
          let fonte = 'sisdpu';
          try { fonte = (await (await fetch('/api/sisdpu2/fonte')).json()).fonte; } catch (e) {}
          if (fonte === 'mcp') {
            this.resumoFonteMcp = true;
            this.fichasMcpCarregando = true;
            try {
              const rf = await (await fetch('/api/sisdpu2/escrita/fichas-resumo', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(sisdpuData.corpoExecucaoMcp()),
              })).json();
              this.fichasMcp = rf.fichas || [];
              this.fichasMcpErros = rf.erros || [];
            } catch (e) {
              this.fichasMcpErros = ['Falha de rede ao montar as fichas do SIS-DPU 2.0: ' + e];
            } finally {
              this.fichasMcpCarregando = false;
            }
          }
        }
      } catch (e) {
        this.resumoErro = 'Erro de rede: ' + e;
      } finally {
        this.montandoResumo = false;
      }
    },

    async confirmarExecucaoConjunta() {
      this.etapaConjunta = 'executando';
      this.erroExecucao = '';
      const eprocData = this.eprocDados();
      const sisdpuData = this.sisdpuDados();
      if (!eprocData) { this.erroExecucao = 'Card "Ações no Eproc" ainda não está pronto.'; this.etapaConjunta = 'resumo'; return; }
      if (this.fichasMcpErros.length) {
        // O SIS-DPU 2.0 recusou montar a ficha: não protocolar no e-proc (irreversível)
        // sabendo que o SISDPU não vai gravar.
        this.erroExecucao = 'O SIS-DPU 2.0 recusou a gravação no SISDPU — corrija antes de executar: ' + this.fichasMcpErros[0];
        this.etapaConjunta = 'resumo'; return;
      }
      // Sessão do SIS-DPU 2.0 com pouco tempo: reconectar ANTES do e-proc (irreversível),
      // para o SISDPU não ficar sem gravar depois (static/js/sessao_mcp.js).
      if (this.resumoFonteMcp && typeof window.garantirSessaoMcp === 'function' && !(await window.garantirSessaoMcp())) {
        this.erroExecucao = 'Sessão do SIS-DPU 2.0 não renovada — nada foi executado.';
        this.etapaConjunta = 'resumo'; return;
      }
      // Conclusão de trâmite vai pelo SISDPU antigo (navegador), por último: o cofre é
      // destrancado AGORA, antes do protocolo irreversível no e-proc — não no meio.
      if (this.resumoFonteMcp && this.conclusoesResumo().length
          && typeof window.garantirCofre === 'function' && !(await window.garantirCofre())) {
        this.erroExecucao = 'A conclusão do trâmite precisa do cofre destrancado — nada foi executado.';
        this.etapaConjunta = 'resumo'; return;
      }
      try {
        if (this.fichasMcp.length) {
          // As fichas lidas acima ficam aprovadas: quando o card do SISDPU gravar,
          // um preparo com o MESMO conteúdo segue sem perguntar de novo.
          await fetch('/api/sisdpu2/aprovacoes-antecipadas', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ fichas: this.fichasMcp }),
          });
        }
        const chave = eprocData.chaveRascunho();
        const respPrep = await fetch('/api/acoes-eproc/executar/' + encodeURIComponent(chave), { method: 'POST' });
        const jPrep = await respPrep.json();
        if (!jPrep.ok) { this.erroExecucao = jPrep.erro || 'Falha ao preparar no e-proc.'; this.etapaConjunta = 'resumo'; return; }
        // Espelha o que prepararNoEproc()/confirmarPeticionamentoReal() do
        // PRÓPRIO card fazem com `execResultado` — como aqui chamamos as rotas
        // direto (pra não reabrir o modal do card dentro desta página),
        // `eprocData.execResultado` nunca era populado, e `_executarGerenciamento()`
        // (chamado abaixo) lê `this.execResultado.processo` internamente — sem
        // isso, quebrava com "Cannot read properties of null" (bug real
        // 2026-09-11, gerenciamento configurado numa movimentação conjunta).
        eprocData.execResultado = jPrep;

        const respConf = await fetch('/api/acoes-eproc/executar/' + encodeURIComponent(chave) + '/confirmar', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ processo: jPrep.processo }),
        });
        const jConf = await respConf.json();
        this.resultadoEproc = jConf;
        eprocData.execResultado = { ...eprocData.execResultado, ...jConf };
        if (!jConf.ok) { this.erroExecucao = jConf.erro || 'Falha ao peticionar.'; this.etapaConjunta = 'resumo'; return; }

        if (jConf.provavel_sucesso) {
          const ger = (this.resumoEproc && this.resumoEproc.gerenciamento) || {};
          if (ger.ativo) {
            const tipoLabel = (eprocData.tiposGerenciamento.find((t) => t.valor === ger.tipo) || {}).rotulo || ger.tipo;
            this.resultadoGerenciamento = { ...(await eprocData._executarGerenciamento(ger)), tipoLabel };
            // O bloco 'concluido' abaixo SÓ aparece quando não há SISDPU pra
            // executar (senão o resumo fecha e a página navega pra outra —
            // ver comentário logo abaixo). Sem isso, um gerenciamento que
            // falhou ou foi bloqueado (ex.: prazo residual no Painel do
            // Procurador) simplesmente sumia sem nenhum aviso — bug real
            // 2026-09-22, PAJ 2026/029-01856. alert() nativo garante que o
            // usuário vê antes de qualquer navegação acontecer.
            if (!this.resultadoGerenciamento.ok) {
              alert('Gerenciamento de processo NÃO executado: ' + (this.resultadoGerenciamento.mensagem || 'erro desconhecido') +
                    '\n\nO peticionamento e a movimentação no SISDPU seguem normalmente.');
            } else if (!this.resultadoGerenciamento.provavel_sucesso) {
              alert('Gerenciamento de processo executado, mas o resultado não pôde ser confirmado automaticamente — confira a aba do e-proc.');
            }
          }
          let sisdpuIniciou = false;
          if (sisdpuData) {
            // Fecha o resumo ENQUANTO o SISDPU executa — o progresso usa o
            // modal/SSE PRÓPRIO do card à direita (mesma UI de sempre, sem
            // duplicar); `true` pula o confirm() dele, já revisamos tudo
            // junto acima. `executar()` é fire-and-forget (abre o EventSource
            // e retorna — não espera o SSE terminar), então `sisdpuData.exec`
            // logo depois já reflete se algo de fato foi disparado (havia
            // fase/tramitação/conclusão/fila configurado) ou se não havia nada
            // a fazer (guarda interna do próprio executar(), sem erro). Só no
            // primeiro caso a navegação pra página do PAJ (aposExecutarIrPara,
            // configurado no cfg do card) vai acontecer sozinha ao terminar —
            // por isso o resumo desta página NÃO reabre nesse caso (evita
            // "flashar" e some quando a navegação ocorrer).
            this.modalResumoAberto = false;
            await sisdpuData.executar(true);
            sisdpuIniciou = sisdpuData.exec;
          }
          if (sisdpuIniciou) return;
        }
        this.etapaConjunta = 'concluido';
        this.modalResumoAberto = true;
      } catch (e) {
        this.erroExecucao = 'Erro de rede: ' + e;
        this.etapaConjunta = 'resumo';
      }
    },
  };
}
