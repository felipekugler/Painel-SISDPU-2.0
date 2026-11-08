// Componentes Alpine do submenu Consultas › E-sisrec da página do PAJ:
// "Extrato de processo CRPS" (services/extrato_crps_service.py) e "Baixar
// íntegra do processo" (services/esisrec_integra_service.py). Modal com o nº
// do processo → gera o PDF e salva em E-sisrec/ do PAJ → mesma pergunta de
// juntada ao SISDPU dos demais dossiês (dossie_juntada.js +
// partials/dossie_juntada.html, 05/11/2026). Como nos dossiês SD/CadÚnico, a
// pergunta reaparece após F5 enquanto não for respondida.
function extratoCrpsApp(pajNorm) {
    return {
        ...juntadaDossie('/api/extrato-crps', 'Extrato'),
        pajNorm: pajNorm,
        aberto: false,
        numero: '',
        obtendo: false,
        erro: '',
        arquivo: '',

        async init() {
            await this.fetchStatus();
            if (this.juntada === 'running') this._acompanharJuntada();
            else if (this.arquivo && this.juntada === null) this.aberto = true;
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
                this.arquivos = d.arquivos || (d.arquivo ? [d.arquivo] : []);
                this.juntada = d.juntada === undefined ? null : d.juntada;
                this.erroJuntada = d.erro_juntada || '';
                if (d.numero && !this.numero) this.numero = d.numero;
            } catch (e) { /* mantém o estado atual */ }
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
                    this.arquivos = [d.arquivo];
                    this.juntada = null;
                    this.erroJuntada = '';
                    showToast('Extrato gerado com sucesso', 'success');
                } else {
                    this.erro = d.erro || 'Falha ao obter o extrato.';
                }
            } catch (e) {
                this.erro = 'Falha de rede: ' + e;
            } finally {
                this.obtendo = false;
            }
        },
    };
}

function esisrecIntegraApp(pajNorm) {
    return {
        ...juntadaDossie('/api/esisrec-integra', 'Íntegra do processo'),
        pajNorm: pajNorm,
        aberto: false,
        numero: '',
        estado: null,     // null | 'running' | 'ok' | 'erro'
        etapa: '',
        erro: '',
        arquivo: '',
        _pollTimer: null,

        async init() {
            await this.fetchStatus();
            if (this.estado === 'running') this.startPolling();
            else if (this.juntada === 'running') this._acompanharJuntada();
            else if (this.estado === 'ok' && this.arquivo && this.juntada === null) this.aberto = true;
        },

        abrir() {
            this.aberto = true;
            if (this.estado === 'erro') this.erro = '';
            this.$nextTick(() => { const el = this.$refs.numeroIntegra; if (el) el.focus(); });
        },

        async fetchStatus() {
            try {
                const d = await (await fetch('/api/esisrec-integra/status/' + this.pajNorm)).json();
                this.estado = d.estado || null;
                this.etapa = d.etapa || '';
                this.arquivo = d.arquivo || '';
                this.arquivos = d.arquivos || (d.arquivo ? [d.arquivo] : []);
                this.juntada = d.juntada === undefined ? null : d.juntada;
                this.erroJuntada = d.erro_juntada || '';
                if (d.estado === 'erro') this.erro = d.erro || 'Falha ao montar a íntegra.';
                if (d.numero && !this.numero) this.numero = d.numero;
            } catch (e) { /* mantém o estado atual */ }
        },

        async iniciar() {
            const n = (this.numero || '').replace(/\D/g, '');
            if (!n) { this.erro = 'Informe o número do processo.'; return; }
            this.erro = '';
            try {
                const resp = await fetch('/api/esisrec-integra/iniciar/' + this.pajNorm + '?numero=' + encodeURIComponent(n), { method: 'POST' });
                const d = await resp.json().catch(() => ({}));
                if (!d.ok) { this.erro = d.erro || 'Falha ao iniciar.'; return; }
                this.estado = 'running'; this.etapa = 'Consultando o processo no e-Sisrec…';
                this.arquivo = ''; this.arquivos = []; this.juntada = null; this.erroJuntada = '';
                this.startPolling();
            } catch (e) {
                this.erro = 'Falha de rede: ' + e;
            }
        },

        startPolling() {
            if (this._pollTimer) return;
            this._pollTimer = setInterval(async () => {
                await this.fetchStatus();
                if (this.estado === 'running') return;
                clearInterval(this._pollTimer);
                this._pollTimer = null;
                if (this.estado === 'ok') {
                    showToast('Íntegra do processo gerada com sucesso', 'success');
                    this.aberto = true;  // mesma pergunta de juntada dos demais dossiês
                } else {
                    showToast('Erro ao montar a íntegra: ' + (this.erro || 'ver log do servidor'), 'error');
                }
            }, 2500);
        },
    };
}
