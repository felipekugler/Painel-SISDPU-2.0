// Módulo Alpine reutilizável "Ações no SISDPU" (movimentar → tramitar → concluir).
// Autocontido — não depende de variáveis do escopo pai (padrão do tabelaMovimentacoes()
// em movimentacoes.js). Uso:
//   <div x-data="acoesSisdpuBlock({ idProcesso: r.id_processo, paj: r.paj,
//                                   idTramite: r.id_tramite, getDescricao: () => r._sug })">
//     {% include "partials/acoes_sisdpu.html" %}
//   </div>
//
// cfg:
//   idProcesso   (obrigatório) — id do processo no SISDPU.
//   paj          (opcional) — rótulo do PAJ para mensagens/confirmação.
//   idTramite    (opcional) — id do trâmite da caixa de entrada a concluir; sem ele,
//                o bloco "Concluir trâmite" não aparece (nada a concluir).
//   getDescricao (opcional) — função síncrona que devolve o texto do despacho no
//                momento de Executar (default: string vazia).
//   faseInicial / grupoInicial (opcional) — pré-seleção antes de carregar as prefs.
//   aberto       (opcional) — estado inicial da seção retrátil (default: false).
//   onExecutado  (opcional) — callback(resultado) chamado após a execução (SSE "done").
function acoesSisdpuBlock(cfg) {
    cfg = cfg || {};
    return {
        aberto: !!cfg.aberto,
        idProcesso: cfg.idProcesso || '',
        paj: cfg.paj || '',
        idTramite: cfg.idTramite || '',
        getDescricao: typeof cfg.getDescricao === 'function' ? cfg.getDescricao : () => '',
        onExecutado: typeof cfg.onExecutado === 'function' ? cfg.onExecutado : () => {},

        fasesFav: [], fasesFreq: [], fasesTodas: [],
        caixasFav: [], caixasFreq: [], caixasTodas: [],
        fase: cfg.faseInicial || '', grupo: cfg.grupoInicial || '',
        prazo: 0, prazoData: '', concluir: true, exec: false,

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
        },
        fasesDropdown() { return this.fasesFav.length ? this.fasesFav : this.fasesTodas; },
        caixasDropdown() { return this.caixasFav.length ? this.caixasFav : this.caixasTodas; },
        async executar() {
            const concluirEfetivo = !!(this.idTramite && this.concluir);
            if (this.exec || (!this.fase && !this.grupo && !concluirEfetivo)) return;
            const prazoTxt = this.grupo ? (this.prazoData ? ' (prazo ' + this.prazoData + ')' : (this.prazo > 0 ? ' (prazo ' + this.prazo + ' dias)' : '')) : '';
            if (!confirm('Executar no SISDPU — ' + (this.paj || this.idProcesso) + '?\n\n'
                + (this.fase ? 'Movimentar (fase: ' + this.fase + ')' : 'Sem movimentação')
                + (this.grupo ? '\nTramitar para: ' + this.grupo + prazoTxt : '\nSem tramitação')
                + (concluirEfetivo ? '\nConcluir o trâmite (retira da caixa)' : '\nSem conclusão'))) return;
            // Garante o cofre desbloqueado ANTES de acionar o SISDPU.
            if (typeof window.garantirCofre === 'function' && !(await window.garantirCofre())) return;
            this.exec = true;
            this.sseTitulo = 'Executando no SISDPU — ' + (this.paj || this.idProcesso);
            this.sseLog = []; this.sseStatus = ''; this.sseAberto = true;
            const qs = new URLSearchParams({
                id_processo: this.idProcesso, paj: this.paj || '',
                fase: this.fase, descricao: this.getDescricao() || '',
                grupo: this.grupo || '', prazo: String(this.prazo > 0 ? this.prazo : 0),
                prazo_data: this.prazoData || '', id_tramite: this.idTramite || '',
                concluir: this.concluir ? '1' : '0',
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
                this.onExecutado(res);
            });
            es.onerror = () => { this.sseStatus = '— erro de conexão'; this.exec = false; es.close(); };
        },
    };
}
