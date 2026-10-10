// Componente Alpine "Dossiê CadÚnico" — menu Consultas da página do PAJ (ver
// services/dossie_cadunico_service.py). Submenu Completo / Só Folha Resumo /
// Só Histórico da Família, one-click cada; polling de status até
// concluir, igual a dossie_sd.js. Enquanto espera o login MANUAL no CadÚnico
// (usuário, senha, código do e-mail e 2FA), o spinner mostra o aviso para o
// Defensor logar na janela que abriu. Em sucesso, pergunta se junta o(s)
// PDF(s) ao SISDPU (modal em paj_detail.html).
function dossieCadunicoApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        status: 'idle',
        rotulo: '',
        erro: '',
        log: [],
        arquivos: [],
        juntada: null,
        erroJuntada: '',
        aguardandoLogin: false,
        movDescricao: '',
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
                const data = await (await fetch('/api/dossie-cadunico/status/' + this.pajNorm)).json();
                this.status = data.status || 'idle';
                this.rotulo = data.rotulo || '';
                this.movDescricao = data.mov_descricao || '';
                this.erro = data.erro || '';
                this.log = data.log || [];
                this.arquivos = data.arquivos || [];
                this.juntada = data.juntada === undefined ? null : data.juntada;
                this.erroJuntada = data.erro_juntada || '';
                this.aguardandoLogin = !!data.aguardando_login
                    && this.log.some(l => l.level === 'aviso' && /login/i.test(l.msg));
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
                const resp = await fetch('/api/dossie-cadunico/iniciar/' + this.pajNorm + '/' + tipo, { method: 'POST' });
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
                const resp = await fetch('/api/dossie-cadunico/juntar/' + this.pajNorm, { method: 'POST' });
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
            try { await fetch('/api/dossie-cadunico/dispensar-juntada/' + this.pajNorm, { method: 'POST' }); } catch (e) { /* só evita repetir a pergunta */ }
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
