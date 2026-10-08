// Componente Alpine "Baixar Extrato de Processo CRPS" — menu Consultas da
// página do PAJ (ver services/extrato_crps_service.py). Modal com o nº do
// processo → "Obter extrato" (gera o Relatório de Andamento no e-Sisrec, sem
// janela, e salva em E-sisrec/ do PAJ) → "Ver o extrato" / "Juntar ao SISDPU".
// Juntada: mesmo polling de status do Dossiê SD (dossie_sd.js).
function extratoCrpsApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        aberto: false,
        numero: '',
        obtendo: false,
        erro: '',
        arquivo: '',
        juntada: null,
        erroJuntada: '',
        _pollTimer: null,

        async init() {
            await this.fetchStatus();
            if (this.juntada === 'running') this.startPolling();
        },

        abrir() {
            this.erro = '';
            this.aberto = true;
            this.$nextTick(() => { const el = this.$refs.numeroCrps; if (el) el.focus(); });
        },

        async fetchStatus() {
            try {
                const d = await (await fetch('/api/extrato-crps/status/' + this.pajNorm)).json();
                this.arquivo = d.arquivo || '';
                this.juntada = d.juntada === undefined ? null : d.juntada;
                this.erroJuntada = d.erro_juntada || '';
                if (d.numero && !this.numero) this.numero = d.numero;
            } catch (e) { /* mantém o estado atual */ }
        },

        urlArquivo() {
            return '/api/extrato-crps/arquivo/' + this.pajNorm + '?nome=' + encodeURIComponent(this.arquivo);
        },

        async obter() {
            const n = (this.numero || '').replace(/\D/g, '');
            if (!n) { this.erro = 'Informe o número do processo.'; return; }
            this.obtendo = true; this.erro = '';
            try {
                const resp = await fetch('/api/extrato-crps/obter/' + this.pajNorm + '?numero=' + encodeURIComponent(n), { method: 'POST' });
                const d = await resp.json().catch(() => ({}));
                if (d.ok) {
                    this.arquivo = d.arquivo;
                    this.juntada = null;
                    showToast('Extrato salvo na pasta E-sisrec do PAJ', 'success');
                } else {
                    this.erro = d.erro || 'Falha ao obter o extrato.';
                }
            } catch (e) {
                this.erro = 'Falha de rede: ' + e;
            } finally {
                this.obtendo = false;
            }
        },

        async juntar() {
            let d;
            try {
                const resp = await fetch('/api/extrato-crps/juntar/' + this.pajNorm, { method: 'POST' });
                d = await resp.json().catch(() => ({}));
            } catch (e) {
                showToast('Falha de rede: ' + e, 'error');
                return;
            }
            if (!d.ok) { showToast('Erro: ' + (d.erro || 'falha ao iniciar a juntada'), 'error'); return; }
            this.juntada = 'running';
            this.startPolling();
        },

        startPolling() {
            if (this._pollTimer) return;
            this._pollTimer = setInterval(async () => {
                await this.fetchStatus();
                if (this.juntada === 'running') return;
                clearInterval(this._pollTimer);
                this._pollTimer = null;
                if (this.juntada === 'ok') showToast('Extrato juntado ao SISDPU com sucesso', 'success');
                else showToast('Erro ao juntar o extrato ao SISDPU: ' + (this.erroJuntada || 'ver log do servidor'), 'error');
            }, 3000);
        },
    };
}
