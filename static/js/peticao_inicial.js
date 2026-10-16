// Botão "Preparar Petição Inicial no eproc" (página do PAJ) — fluxo de
// BENEFÍCIO POR INCAPACIDADE ensinado ao vivo pelo Defensor em 2026-10-08 (ver
// Estrutura de Construção/APRENDIZADO_PETICAO_INICIAL_EPROC.md).
//
// Fases do modal:
//   plano      → arquivos da pasta Ajuizamento com o TIPO do e-proc sugerido +
//                dados extraídos da inicial/PAJ, tudo editável;
//   preparando → automação percorre o assistente do e-proc e PARA antes de
//                "Finalizar" (nada distribuído);
//   preparado  → aviso "Petição preparada para conferência" + botão
//                "Prosseguir com ajuizamento";
//   pos        → ajuizamento real (nº + chave) → gerenciamento do processo
//                (procurador padrão) → movimentação "Petição inicial" no
//                SISDPU (docx anexo, tramita p/ AJUIZADOS) pelo próprio console
//                "Ações no SISDPU" da página (executar(true) — recarrega a
//                página ao final; o resumo volta pelo sessionStorage).
function peticaoInicialApp(pajNorm, pajLabel) {
    const CHAVE_FIM = 'painel_peticao_inicial_fim_' + pajNorm;
    return {
        aberto: false, fase: 'plano', carregando: false, erro: '',
        arquivos: [], dados: null, tiposTodos: [], estadosCivis: [], especialidadesConhecidas: [],
        localidades: [], jaAjuizada: null, novoAjuizamento: false, passos: [], etapaFinal: '', resultado: null, posLog: [], posErro: '',
        // Revisão UNIFICADA pós-ajuizamento (16/10/2026): todas as fichas do
        // SIS-DPU 2.0 num só modal, uma confirmação para tudo.
        revisao: { carregando: false, cadastro: [], jaVinculado: false, mov: [], erros: [] }, _decidir: null,

        init() {
            if (this._iniciado) return;
            this._iniciado = true;
            try {
                const salvo = sessionStorage.getItem(CHAVE_FIM);
                if (salvo) {
                    sessionStorage.removeItem(CHAVE_FIM);
                    const s = JSON.parse(salvo);
                    // nº + chave já estão no card — não repete no log (registros antigos traziam)
                    this.resultado = s.resultado;
                    this.posLog = (s.posLog || []).filter((l) => !/^Processo distribu[íi]do:/.test(l.msg || ''));
                    this.posLog.push({ ok: true, msg: 'Movimentação "Petição inicial" executada no SISDPU (ver Histórico/Movimentações).' });
                    this.fase = 'fim'; this.aberto = true;
                }
            } catch (e) {}
        },

        async abrir() {
            this.aberto = true; this.fase = 'plano'; this.erro = ''; this.carregando = true;
            this.passos = []; this.resultado = null; this.posLog = []; this.posErro = '';
            this.jaAjuizada = null; this.novoAjuizamento = false;
            try {
                const resp = await fetch('/api/peticao-inicial/plano/' + encodeURIComponent(pajNorm));
                const j = await resp.json();
                if (!j.ok) { this.erro = j.erro || 'Falha ao montar o plano.'; return; }
                this.arquivos = j.arquivos.map((a) => ({ ...a, incluir: true }));
                this.dados = j.dados;
                this.tiposTodos = j.tipos_todos || [];
                this.estadosCivis = j.estados_civis || [];
                this.especialidadesConhecidas = j.especialidades_conhecidas || [];
                this.localidades = j.localidades || [];
                this.jaAjuizada = j.ja_ajuizada || null;
            } catch (e) {
                this.erro = 'Erro de rede: ' + e;
            } finally {
                this.carregando = false;
            }
        },
        fechar() {
            if (this.fase === 'preparando' || this.fase === 'pos' || this.fase === 'revisao') return;
            this.aberto = false;
        },
        linkArquivo(a) { return '/files/' + pajNorm + '/' + a.caminho; },
        opcoesTipo(atual) {
            const l = this.tiposTodos.slice();
            if (atual && !l.includes(atual)) l.unshift(atual);
            return l;
        },
        trocarRito() {
            const jef = this.dados.rito === 'JUIZADO ESPECIAL FEDERAL';
            this.dados.classe = jef ? 'PROCEDIMENTO DO JUIZADO ESPECIAL CÍVEL' : 'PROCEDIMENTO COMUM';
            this.dados.renuncia_60sm = jef;
        },
        toggleEspecialidade(e) {
            const i = this.dados.especialidades.indexOf(e);
            if (i >= 0) this.dados.especialidades.splice(i, 1); else this.dados.especialidades.push(e);
        },
        selecionados() { return this.arquivos.filter((a) => a.incluir); },

        async preparar() {
            if (typeof window.garantirCofre === 'function' && !(await window.garantirCofre())) return;
            this.erro = ''; this.passos = [];
            // Sessão do e-proc no navegador ATIVA antes de começar (reconexão
            // automática se caiu); sem ela, não prossegue (16/10/2026).
            this.fase = 'conectando';
            if (!(await window.garantirEproc())) {
                this.fase = 'plano';
                this.erro = 'Não foi possível conectar ao e-proc pelo navegador (a reconexão automática falhou). '
                    + 'Entre pelo selo do e-proc, no topo do painel (login na janela do e-proc), e clique em preparar de novo.';
                showToast('e-proc não conectado — a petição não foi preparada.', 'error');
                return;
            }
            this.fase = 'preparando';
            try {
                const resp = await fetch('/api/peticao-inicial/preparar/' + encodeURIComponent(pajNorm), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        arquivos: this.selecionados().map((a) => ({ caminho: a.caminho, tipo: a.tipo })),
                        dados: this.dados, novo_ajuizamento: this.novoAjuizamento,
                    }),
                });
                const j = await resp.json();
                this.passos = j.passos || [];
                this.etapaFinal = j.etapa || '';
                if (!j.ok) { this.erro = j.erro || 'Falha ao preparar a petição.'; this.fase = 'plano'; return; }
                this.fase = 'preparado';
            } catch (e) {
                this.erro = 'Erro de rede: ' + e; this.fase = 'plano';
            }
        },

        async prosseguir() {
            if (!confirm('Distribuir o processo agora no e-proc?\n\n"Finalizar" + "Confirmar ajuizamento" — irreversível.\n'
                + 'Depois: gerenciamento do processo e, no SISDPU, UM resumo para conferir e confirmar de uma vez '
                + 'o cadastro do processo, a movimentação "Petição inicial" e a tramitação para AJUIZADOS.')) return;
            this.fase = 'pos'; this.posLog = []; this.posErro = '';
            // 1) ajuizamento
            let r;
            try {
                r = await (await fetch('/api/peticao-inicial/ajuizar/' + encodeURIComponent(pajNorm), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paj_label: pajLabel, novo_ajuizamento: this.novoAjuizamento }),
                })).json();
            } catch (e) { r = { ok: false, erro: 'Erro de rede: ' + e + ' — confira no e-proc se o processo foi distribuído.' }; }
            if (!r.ok) { this.posErro = r.erro || 'Falha no ajuizamento.'; this.fase = 'preparado'; return; }
            this.resultado = r;  // nº + chave já aparecem no card "Processo distribuído" (sem repetir no log)

            // 2) gerenciamento (procurador padrão de Configurações › e-proc)
            try {
                const prefs = await (await fetch('/api/acoes-eproc/preferencias')).json();
                const padrao = prefs.procurador_padrao;
                if (!padrao || !padrao.valor) {
                    this.posLog.push({ ok: false, msg: 'Gerenciamento NÃO executado: sem procurador padrão em Configurações › e-proc.' });
                } else {
                    this.posLog.push({ ok: null, msg: 'Gerenciando o processo para ' + padrao.rotulo + '…' });
                    const g = await (await fetch('/api/acoes-eproc/gerenciar', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ processo: r.numero, tipo: 'S', procurador_valor: padrao.valor, paj_norm: pajNorm }),
                    })).json();
                    this.posLog.pop();
                    const nPartes = (g.partes_marcadas || []).length;
                    this.posLog.push({ ok: !!(g.ok && g.provavel_sucesso),
                                       msg: !g.ok ? 'Gerenciamento falhou: ' + (g.erro || 'erro desconhecido') + ' — refaça pelo minimenu do processo.'
                                          : g.provavel_sucesso ? 'Gerenciamento executado para ' + nPartes + ' parte(s) → ' + padrao.rotulo
                                          : 'Gerenciamento sem confirmação automática — confira a aba do e-proc.' });
                }
            } catch (e) {
                this.posLog.push({ ok: false, msg: 'Gerenciamento falhou (rede): ' + e });
            }

            // 3) movimentação preparada no console "Ações no SISDPU" (rascunho)
            const prep = await this._prepararConsole(r);

            // 4) SIS-DPU 2.0: revisão UNIFICADA — fichas do cadastro do processo,
            //    da movimentação e da tramitação num só modal; uma confirmação
            //    vale para todas (a sincronização com o tribunal é autorizada
            //    junto com o cadastro). Depois grava tudo sem outras janelas.
            if (window.PAINEL_FONTE_SISDPU_MCP) {
                const ok = await this._revisarTudo(r, prep);
                if (!ok) return;
                await this._cadastrarProcessoJudicial(r);
            }
            if (prep) await this._executarConsole(prep);
            else this.fase = 'fim';  // movimentação não preparada — o log já explica
        },

        // Dry-run de tudo o que será gravado no SIS-DPU 2.0 e espera a decisão do
        // Defensor no próprio modal. true = confirmou (aprovações registradas).
        async _revisarTudo(r, prep) {
            this.revisao = { carregando: true, cadastro: [], jaVinculado: false, mov: [], erros: [] };
            this.fase = 'revisao';
            const post = async (url, corpo) => (await fetch(url, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(corpo),
            })).json();
            try {
                const cad = await post('/api/sisdpu2/processo-judicial/ficha',
                                       { paj_label: pajLabel, numero: r.numero, instancia: 1 });
                this.revisao.cadastro = cad.fichas || [];
                this.revisao.jaVinculado = !!cad.ja_vinculado;
                this.revisao.erros.push(...(cad.erros || []));
            } catch (e) { this.revisao.erros.push('Cadastro do processo: ' + e); }
            if (prep) {
                try {
                    const mov = await post('/api/sisdpu2/escrita/fichas-resumo', prep.c.corpoExecucaoMcp());
                    this.revisao.mov = mov.fichas || [];
                    this.revisao.erros.push(...(mov.erros || []));
                } catch (e) { this.revisao.erros.push('Movimentação: ' + e); }
            }
            this.revisao.carregando = false;
            const confirmou = await new Promise((resolve) => { this._decidir = resolve; });
            this._decidir = null;
            if (!confirmou) {
                this.posLog.push({ ok: false, msg: 'Gravações no SIS-DPU 2.0 canceladas — nada foi gravado no SISDPU. '
                                                   + 'A movimentação ficou como rascunho no "Ações no SISDPU".' });
                this.fase = 'fim';
                return false;
            }
            const todas = [...this.revisao.cadastro, ...this.revisao.mov];
            if (todas.length) {
                try { await post('/api/sisdpu2/aprovacoes-antecipadas', { fichas: todas }); } catch (e) {}
            }
            this.fase = 'pos';
            return true;
        },
        decidirRevisao(sim) { if (this._decidir) this._decidir(!!sim); },
        // Card do cadastro do processo, enxuto (pedido do Defensor, 16/10/2026):
        // só "Número informado" e "Instância" da ficha do SIS-DPU 2.0.
        cadastroCampos(f) {
            const out = [];
            (f && f.ficha || '').split('\n').forEach((linha) => {
                const m = linha.match(/^\s*(N[úu]mero informado|Inst[âa]ncia)\s*:\s*(.+)$/i);
                if (m) out.push([m[1], m[2].trim()]);
            });
            return out;
        },
        fichaDoTipo(tipo) { return (this.revisao.mov || []).find((f) => f.tipo === tipo) || null; },
        fichaHtml(f) {
            return window.fichaMcp ? window.fichaMcp.htmlFicha(f.ficha, f.paj, f.contexto, { essencial: true }) : '';
        },

        async _cadastrarProcessoJudicial(r) {
            this.posLog.push({ ok: null, msg: 'Cadastrando o processo ' + r.numero + ' no PAJ e sincronizando com o tribunal (SIS-DPU 2.0)…' });
            let c;
            try {
                c = await (await fetch('/api/sisdpu2/processo-judicial/cadastrar', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paj_label: pajLabel, numero: r.numero, instancia: 1 }),
                })).json();
            } catch (e) { c = { ok: false, detalhe: 'Erro de rede: ' + e }; }
            this.posLog.pop();
            this.posLog.push({ ok: !!c.ok,
                msg: c.ok ? 'Processo judicial ' + (c.cadastrado ? 'cadastrado e ' : '') + 'sincronizado no PAJ'
                            + (c.juizo ? ' — ' + c.juizo : '') + '. ' + (c.detalhe || '')
                          : 'Processo judicial no PAJ: ' + (c.detalhe || 'falhou') + ' — dá para cadastrar pela tela do SISDPU.' });
        },

        // Grava o rascunho da movimentação "Petição inicial" no console "Ações no
        // SISDPU" e o deixa pronto (editor montado) — sem executar.
        async _prepararConsole(r) {
            const d = this.dados;
            const texto = (d.descricao_sisdpu || '').replace('[PROCESSO]', r.numero).replace('[CHAVE]', r.chave);
            const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const html = texto.split('\n').map((l) => '<p>' + (l.trim() ? esc(l) : '<br>') + '</p>').join('');
            let fase = 'Petição inicial', grupo = '01- PREV_AJUIZADOS';
            try {
                const p = await (await fetch('/api/acoes-sisdpu/preferencias')).json();
                fase = (p.fases_todas || []).find((f) => f.toLowerCase() === 'petição inicial') || fase;
                grupo = (p.caixas_todas || []).find((c) => /AJUIZADOS/i.test(c)) || grupo;
            } catch (e) {}
            const anexos = d.docx_peticao ? [{
                origem: 'paj', ref: d.docx_peticao, nome: d.docx_peticao.split('/').pop(), tamanho: 0, tipo: 'docx',
                descricao: 'Petição inicial', tipoArquivo: 'Petições',
            }] : [];
            if (!anexos.length) this.posLog.push({ ok: false, msg: 'Petição inicial em .docx não encontrada em "Peças IA" — movimentação vai sem anexo.' });
            try {
                const g = await (await fetch('/api/acoes-sisdpu/rascunho/' + encodeURIComponent(pajNorm), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paj_label: pajLabel, fase, grupo, prazo: 0, prazo_data: '', concluir: true,
                                           descricao_mov_html: html,
                                           // descrição padrão da tramitação para AJUIZADOS (Defensor, 16/10/2026)
                                           descricao_tram: 'Processo ajuizado. Cadastrar chave. Comunicar assistido',
                                           anexos, fila: [] }),
                })).json();
                if (!g.ok) throw new Error(g.erro || 'falha ao gravar');
            } catch (e) {
                this.posLog.push({ ok: false, msg: 'Não consegui preparar a movimentação no SISDPU (' + e + ') — lance pelo "Ações no SISDPU".' });
                return null;
            }
            const el = document.getElementById('acoesSisdpuPaj');
            const c = el ? Alpine.$data(el) : null;
            if (!c || typeof c.executar !== 'function') {
                this.posLog.push({ ok: false, msg: 'Movimentação gravada como rascunho no "Ações no SISDPU" — execute por lá (console não encontrado nesta página).' });
                this.fase = 'fim'; return null;
            }
            await c.carregarRascunho(true);
            if (!c.aberto) await c.abrir();
            else if (c._editorMov) c._editorMov.setHTML(c._descMovHtmlRascunho || '');
            await new Promise((res) => setTimeout(res, 600));
            return { c, fase, grupo, r };
        },

        async _executarConsole(prep) {
            const { c, fase, grupo, r } = prep;
            this.posLog.push({ ok: null, msg: 'Lançando a movimentação "' + fase + '" no SISDPU (tramita para ' + grupo + ')…' });
            try { sessionStorage.setItem(CHAVE_FIM, JSON.stringify({ resultado: r, posLog: this.posLog.filter((x) => x.ok !== null) })); } catch (e) {}
            this.aberto = false;  // deixa o modal de execução do SISDPU visível
            await c.executar(true);
            // executar() só DISPARA o SSE — espera a execução de fato terminar.
            while (c.exec) await new Promise((res) => setTimeout(res, 1000));
            // Sucesso → o próprio console recarrega a página em seguida (o resumo
            // volta pelo sessionStorage, ver init()). Qualquer outro desfecho
            // (aviso, conexão perdida) → reabre este modal explicando.
            if (/executado ✓|já estava/.test(c.sseStatus || '')) return;
            try { sessionStorage.removeItem(CHAVE_FIM); } catch (e) {}
            this.posLog.pop();
            this.posLog.push({ ok: false, msg: 'A movimentação no SISDPU terminou ' + (c.sseStatus || 'sem confirmação')
                                              + ' — confira o console "Ações no SISDPU" (o rascunho continua lá se não executou).' });
            this.fase = 'fim'; this.aberto = true;
        },
    };
}
