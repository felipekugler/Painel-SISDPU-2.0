// Componente Alpine "Dossiê Seguro-Desemprego Web" — menu Consultas da página
// do PAJ (ver services/dossie_sd_service.py). One-click por subitem (Completo /
// Vínculos / Seguro-Desemprego); só faz polling de status até concluir, igual
// a consulta_rpv.js. Toast final curto, sem detalhes dos dados extraídos
// (pedido do usuário, 2026-10-02). Em sucesso, pergunta se junta o(s) PDF(s)
// ao SISDPU (modal em paj_detail.html); a pergunta reaparece após F5 enquanto
// não for respondida (`juntada` null no back).
function dossieSdApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        status: 'idle',
        rotulo: '',
        erro: '',
        log: [],
        arquivos: [],
        juntada: null,
        erroJuntada: '',
        mostrarSubmenu: false,
        perguntaJuntada: false,
        _pollTimer: null,

        async init() {
            await this.fetchStatus();
            if (this.status === 'running' || this.juntada === 'running') this.startPolling();
            else if (this.status === 'done' && this.juntada === null && this.arquivos.length) this.perguntaJuntada = true;
        },

        async fetchStatus() {
            try {
                const data = await (await fetch('/api/dossie-sd/status/' + this.pajNorm)).json();
                this.status = data.status || 'idle';
                this.rotulo = data.rotulo || '';
                this.erro = data.erro || '';
                this.log = data.log || [];
                this.arquivos = data.arquivos || [];
                this.juntada = data.juntada === undefined ? null : data.juntada;
                this.erroJuntada = data.erro_juntada || '';
            } catch (e) {
                this.status = 'error';
            }
        },

        ultimaMensagem() {
            const ultimo = this.log[this.log.length - 1];
            return ultimo ? ultimo.msg : '';
        },

        async iniciar(tipo) {
            if (this.status === 'running' || this.juntada === 'running') return;
            let data;
            try {
                const resp = await fetch('/api/dossie-sd/iniciar/' + this.pajNorm + '/' + tipo, { method: 'POST' });
                data = await resp.json().catch(() => ({}));
            } catch (e) {
                showToast('Falha de rede: ' + e, 'error');
                return;
            }
            if (!data.ok) {
                showToast('Erro: ' + (data.erro || 'falha ao iniciar o dossiê'), 'error');
                return;
            }
            this.status = 'running';
            this.juntada = null;
            this.log = [];
            this.startPolling();
        },

        async juntar() {
            this.perguntaJuntada = false;
            let data;
            try {
                const resp = await fetch('/api/dossie-sd/juntar/' + this.pajNorm, { method: 'POST' });
                data = await resp.json().catch(() => ({}));
            } catch (e) {
                showToast('Falha de rede: ' + e, 'error');
                return;
            }
            if (!data.ok) {
                showToast('Erro: ' + (data.erro || 'falha ao iniciar a juntada'), 'error');
                return;
            }
            this.juntada = 'running';
            this.startPolling();
        },

        async dispensarJuntada() {
            this.perguntaJuntada = false;
            this.juntada = 'dispensada';
            try { await fetch('/api/dossie-sd/dispensar-juntada/' + this.pajNorm, { method: 'POST' }); } catch (e) { /* só evita repetir a pergunta */ }
        },

        startPolling() {
            if (this._pollTimer) return;
            this._pollTimer = setInterval(async () => {
                const eraJuntada = this.juntada === 'running';
                await this.fetchStatus();
                if (eraJuntada) {
                    if (this.juntada === 'running') return;
                    clearInterval(this._pollTimer);
                    this._pollTimer = null;
                    if (this.juntada === 'ok') {
                        showToast('Dossiê juntado ao SISDPU com sucesso', 'success');
                    } else {
                        showToast('Erro ao juntar o dossiê ao SISDPU: ' + (this.erroJuntada || 'ver log do servidor'), 'error');
                    }
                    return;
                }
                if (this.status === 'done' || this.status === 'error') {
                    clearInterval(this._pollTimer);
                    this._pollTimer = null;
                    if (this.status === 'done') {
                        showToast((this.rotulo || 'Dossiê') + ' gerado com sucesso', 'success');
                        if (this.arquivos.length) this.perguntaJuntada = true;
                    } else {
                        showToast('Erro ao gerar ' + (this.rotulo || 'o dossiê') + ': ' + (this.erro || 'ver log do servidor'), 'error');
                    }
                }
            }, 3000);
        },
    };
}
