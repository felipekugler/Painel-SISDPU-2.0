// Módulo Alpine reutilizável "Ações no SISDPU" (movimentar → tramitar → concluir).
// Autocontido — não depende de variáveis do escopo pai (padrão do tabelaMovimentacoes()
// em movimentacoes.js). Uso:
//   <div x-data="acoesSisdpuBlock({ idProcesso: r.id_processo, paj: r.paj,
//                                   idTramite: r.id_tramite, getDescricaoMov: () => r._sug })">
//     {% include "partials/acoes_sisdpu.html" %}
//   </div>
//
// cfg:
//   idProcesso      (obrigatório) — id do processo no SISDPU.
//   paj             (opcional) — rótulo do PAJ para mensagens/confirmação.
//   pajNorm         (opcional) — para carregar os "Anexos SISDPU" já baixados do PAJ
//                   (tabela de seleção de documentos a incluir na movimentação — mesma
//                   fonte/rota usada pela aba do PAJ e por acoes_eproc.js).
//   idTramite       (opcional) — id do trâmite da caixa de entrada a concluir; sem ele,
//                   o bloco "Concluir trâmite" não aparece (nada a concluir).
//   getDescricaoMov (opcional) — função síncrona que devolve o texto do despacho da
//                   MOVIMENTAÇÃO no momento de Executar (ex.: a textarea de Sugestão IA
//                   da Retornos). Quando informado, o módulo NÃO desenha textarea própria
//                   para a movimentação — evita duplicar campo com a fonte do chamador.
//                   Sem ele, o módulo mostra sua própria textarea simples (estado interno
//                   `descMov`). Aceita também `getDescricao` (nome legado) como alias.
//   getDescricaoTram (opcional) — mesma ideia, para o despacho da TRAMITAÇÃO. Nenhuma
//                   página tem hoje uma fonte de sugestão para tramitação, então na
//                   prática o módulo sempre desenha sua textarea própria (`descTram`).
//   faseInicial / grupoInicial (opcional) — pré-seleção antes de carregar as prefs.
//   aberto          (opcional) — estado inicial da seção retrátil (default: false).
//   onExecutado     (opcional) — callback(resultado) chamado após a execução (SSE "done").
function acoesSisdpuBlock(cfg) {
    cfg = cfg || {};
    const getDescricaoMovFn = typeof cfg.getDescricaoMov === 'function' ? cfg.getDescricaoMov
                             : (typeof cfg.getDescricao === 'function' ? cfg.getDescricao : null);
    const getDescricaoTramFn = typeof cfg.getDescricaoTram === 'function' ? cfg.getDescricaoTram : null;
    return {
        aberto: !!cfg.aberto,
        idProcesso: cfg.idProcesso || '',
        paj: cfg.paj || '',
        pajNorm: cfg.pajNorm || '',
        idTramite: cfg.idTramite || '',
        getDescricaoMov: getDescricaoMovFn,
        getDescricaoTram: getDescricaoTramFn,
        onExecutado: typeof cfg.onExecutado === 'function' ? cfg.onExecutado : () => {},

        fasesFav: [], fasesFreq: [], fasesTodas: [],
        caixasFav: [], caixasFreq: [], caixasTodas: [],
        fase: cfg.faseInicial || '', grupo: cfg.grupoInicial || '',
        descMov: '', descTram: '',
        prazo: 0, prazoData: '', concluir: true, exec: false,

        // Anexos SISDPU do PAJ (checkbox pra incluir na movimentação) — mesma fonte/rota
        // e mesmos helpers de docsPaj do módulo "Ações no Eproc" (static/js/acoes_eproc.js).
        docsPaj: [], docsCarregados: false, anexosSelecionados: {},

        sseAberto: false, sseLog: [], sseStatus: '', sseTitulo: '',

        async init() {
            try {
                const j = await (await fetch('/api/acoes-sisdpu/preferencias')).json();
                this.fasesFav = j.fases_favoritas || [];
                this.fasesFreq = j.fases_frequentes || [];
                this.fasesTodas = j.fases_todas || [];
                this.caixasFav = j.caixas_favoritas || [];
                this.caixasFreq = j.caixas_frequentes || [];
                this.caixasTodas = j.caixas_todas || [];
                if (!this.fase) this.fase = j.fase_padrao || '';
                if (!this.grupo) this.grupo = j.caixa_padrao || '';
            } catch (e) {}
            await this.carregarDocsPaj();
        },
        fasesDropdown() { return this.fasesFav.length ? this.fasesFav : this.fasesTodas; },
        caixasDropdown() { return this.caixasFav.length ? this.caixasFav : this.caixasTodas; },
        // Texto efetivo de cada despacho: prioriza a fonte injetada pelo chamador
        // (Sugestão IA etc.); sem ela, usa a textarea própria do módulo.
        descMovEfetiva() { return this.getDescricaoMov ? (this.getDescricaoMov() || '') : this.descMov; },
        descTramEfetiva() { return this.getDescricaoTram ? (this.getDescricaoTram() || '') : this.descTram; },
        copiarMovParaTram() { this.descTram = this.descMovEfetiva(); },

        // --- Anexos SISDPU (tabela de seleção — mesma fonte/rota de acoes_eproc.js) ---
        async carregarDocsPaj() {
            this.docsCarregados = true;
            if (!this.pajNorm) return;
            try {
                const resp = await fetch('/api/paj/' + encodeURIComponent(this.pajNorm) + '/movimentacoes');
                if (resp.ok) { const d = await resp.json(); this.docsPaj = d.anexos_sisdpu || []; }
            } catch (e) {}
        },
        // `anexos_sisdpu` (mesma fonte da seção "Anexos SISDPU" da aba do PAJ):
        // {nome, tipo, tamanho, tem_ocr, caminho, caminho_ocr}.
        docKey(d) { return d.caminho || d.nome; },
        docLabel(d) { return d.nome || d.caminho || 'documento'; },
        linkArquivoPaj(d) { return '/files/' + this.pajNorm + '/' + (d.caminho || ''); },
        corBadgeTipo(tipo) {
            const t = (tipo || '').toLowerCase();
            if (t === 'pdf') return 'badge-error';
            if (t === 'doc' || t === 'docx') return 'badge-info';
            if (['jpg', 'jpeg', 'png', 'gif'].includes(t)) return 'badge-warning';
            return 'bg-base-200 text-base-content/60';
        },
        // Informativo — KB abaixo de 1MB, MB a partir daí (evita "28672 KB").
        formatarTamanho(bytes) {
            if (!bytes) return '—';
            if (bytes < 1024 * 1024) return Math.round(bytes / 1024 * 10) / 10 + ' KB';
            return Math.round(bytes / 1024 / 1024 * 10) / 10 + ' MB';
        },
        anexoToggle(k) { this.anexosSelecionados[k] = !this.anexosSelecionados[k]; },
        anexosTodosSelecionados() {
            return this.docsPaj.length > 0 && this.docsPaj.every(d => this.anexosSelecionados[this.docKey(d)]);
        },
        toggleTodosAnexos() {
            const marcar = !this.anexosTodosSelecionados();
            this.docsPaj.forEach(d => { this.anexosSelecionados[this.docKey(d)] = marcar; });
        },
        anexosSelecionadosCaminhos() {
            return this.docsPaj.filter(d => this.anexosSelecionados[this.docKey(d)]).map(d => this.docKey(d));
        },
        async executar() {
            const concluirEfetivo = !!(this.idTramite && this.concluir);
            if (this.exec || (!this.fase && !this.grupo && !concluirEfetivo)) return;
            const prazoTxt = this.grupo ? (this.prazoData ? ' (prazo ' + this.prazoData + ')' : (this.prazo > 0 ? ' (prazo ' + this.prazo + ' dias)' : '')) : '';
            const anexosSel = this.fase ? this.anexosSelecionadosCaminhos() : [];
            if (!confirm('Executar no SISDPU — ' + (this.paj || this.idProcesso) + '?\n\n'
                + (this.fase ? 'Movimentar (fase: ' + this.fase + ')' : 'Sem movimentação')
                + (anexosSel.length ? '\n+ ' + anexosSel.length + ' anexo(s) do PAJ' : '')
                + (this.grupo ? '\nTramitar para: ' + this.grupo + prazoTxt : '\nSem tramitação')
                + (concluirEfetivo ? '\nConcluir o trâmite (retira da caixa)' : '\nSem conclusão'))) return;
            // Garante o cofre desbloqueado ANTES de acionar o SISDPU.
            if (typeof window.garantirCofre === 'function' && !(await window.garantirCofre())) return;
            this.exec = true;
            this.sseTitulo = 'Executando no SISDPU — ' + (this.paj || this.idProcesso);
            this.sseLog = []; this.sseStatus = ''; this.sseAberto = true;
            const qs = new URLSearchParams({
                id_processo: this.idProcesso, paj: this.paj || '', paj_norm: this.pajNorm || '',
                fase: this.fase, descricao_mov: this.descMovEfetiva(),
                grupo: this.grupo || '', descricao_tram: this.descTramEfetiva(),
                prazo: String(this.prazo > 0 ? this.prazo : 0),
                prazo_data: this.prazoData || '', id_tramite: this.idTramite || '',
                concluir: this.concluir ? '1' : '0',
                anexos: JSON.stringify(anexosSel),
            });
            const es = new EventSource('/api/acoes-sisdpu/executar?' + qs.toString());
            es.addEventListener('log', (ev) => {
                try { this.sseLog.push(JSON.parse(ev.data)); } catch (e) {}
                this.$nextTick(() => { const b = this.$refs.acoesSisdpuSseBox; if (b) b.scrollTop = b.scrollHeight; });
            });
            es.addEventListener('done', (ev) => {
                let res = {}; try { res = JSON.parse(ev.data); } catch (e) {}
                this.sseStatus = res.status === 'ja_ausente' ? '— já estava concluído (nada foi feito agora)'
                                : (res.ok ? '— executado ✓' : '— com aviso');
                this.exec = false; es.close();
                if (res.ok) this.anexosSelecionados = {};  // limpa a seleção após êxito
                // Enriquece o resultado com o que FOI de fato executado (fase/grupo/despachos) —
                // o chamador (onExecutado) não tem acesso ao estado interno deste bloco, e páginas
                // como a Retornos precisam disso para registrar no Histórico do painel.
                this.onExecutado({...res, fase: this.fase, grupo: this.grupo,
                                   descricao_mov: this.descMovEfetiva(), descricao_tram: this.descTramEfetiva()});
            });
            es.onerror = () => {
                this.exec = false; es.close();
                this.sseStatus = '— conexão perdida (verifique o PAJ)';
                this.sseLog.push({ level: 'erro', msg: this._avisoConexaoPerdida() });
                this.$nextTick(() => { const b = this.$refs.acoesSisdpuSseBox; if (b) b.scrollTop = b.scrollHeight; });
            };
        },

        // A conexão SSE caiu antes do fluxo mov→tram→conclui terminar (rede instável —
        // não é uma falha do SISDPU nem da aplicação: o servidor cancela a execução
        // assim que percebe o cliente desconectado). O risco real: a etapa que estava
        // "em andamento" no momento da queda PODE já ter sido efetivada no SISDPU
        // mesmo sem a confirmação ("ok") ter chegado até aqui — foi exatamente o que
        // aconteceu no PAJ 2026/029-02567 em 2026-08-07 (Movimentação executada, log
        // cortado antes da confirmação, Tramitação/Conclusão nunca rodaram). Por isso
        // o aviso é conservador: some etapa com "ok" confirmado no log vira certeza;
        // a última etapa "em andamento" (sem "ok" nem "erro" registrado depois dela)
        // vira alerta de possível duplicação — sobretudo a Movimentação, que cria um
        // novo registro a cada chamada (não é idempotente).
        _avisoConexaoPerdida() {
            const okRe = { 'Movimentação': /^Movimenta[çc][aã]o:/, 'Tramitação': /^Tramita[çc][aã]o:/, 'Conclusão': /^Conclus[aã]o:/ };
            const emCursoRe = { 'Movimentação': /^Movimentando/, 'Tramitação': /^Tramitando/, 'Conclusão': /^Concluindo/ };
            const confirmadas = Object.keys(okRe).filter(k =>
                this.sseLog.some(l => l.level === 'ok' && okRe[k].test(l.msg || '')));
            let emAndamento = '';
            for (let i = this.sseLog.length - 1; i >= 0; i--) {
                const m = this.sseLog[i].msg || '';
                if (Object.values(okRe).some(re => re.test(m))) break;  // já tinha resultado (ok ou erro) — não ficou em aberto
                const etapa = Object.keys(emCursoRe).find(k => emCursoRe[k].test(m));
                if (etapa) { emAndamento = etapa; break; }
            }
            let msg = 'Conexão com o painel perdida durante a execução (provável instabilidade de rede — o SISDPU não foi consultado sobre isso).';
            if (confirmadas.length) msg += ' Já confirmado nesta execução: ' + confirmadas.join(', ') + '.';
            if (emAndamento && !confirmadas.includes(emAndamento)) {
                msg += ' A etapa "' + emAndamento + '" estava em andamento no momento da queda — ela PODE já ter sido efetivada no SISDPU mesmo sem a confirmação aparecer aqui.';
            }
            msg += ' Verifique o PAJ no SISDPU antes de executar de novo, e desmarque no console (deixe "Sem movimentação"/"Sem tramitação") o que já foi feito — a Movimentação em especial cria um novo registro a cada execução.';
            return msg;
        },
    };
}
