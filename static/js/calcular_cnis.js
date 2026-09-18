// Componente Alpine "Calcular Tempo de Contribuição" — botão próprio na
// barra "Ações com IA" da página do PAJ (deliberadamente separado do
// dropdown "Analisar": aqui é preciso escolher qual arquivo de CNIS usar,
// já que um PAJ pode ter mais de um extrato baixado em datas diferentes —
// ver services/calcular_cnis_service.py).
function calcularCnisApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        modalAberto: false,
        carregandoArquivos: false,
        arquivos: [],
        erroArquivos: '',
        status: 'idle',
        error: '',
        arquivoEmUso: '',
        arquivoPendente: null, // {caminho, nome} — aguardando escolha de modo (CNIS completo)
        _pollTimer: null,

        async init() {
            await this.fetchStatus();
            if (this.status === 'running') {
                this.startPolling();
            }
        },

        async fetchStatus() {
            try {
                const resp = await fetch('/api/calcular-cnis/status/' + this.pajNorm);
                const data = await resp.json();
                this.status = data.status || 'idle';
                this.error = data.error || '';
                this.arquivoEmUso = data.arquivo_cnis || '';
            } catch (e) {
                this.error = 'Erro ao consultar status: ' + e.message;
                this.status = 'error';
            }
        },

        async abrirModal() {
            this.modalAberto = true;
            this.carregandoArquivos = true;
            this.erroArquivos = '';
            this.arquivos = [];
            try {
                const resp = await fetch('/api/calcular-cnis/arquivos/' + this.pajNorm);
                const data = await resp.json();
                this.arquivos = data.arquivos || [];
                if (this.arquivos.length === 0) {
                    this.erroArquivos = 'Nenhum arquivo com "CNIS" no nome encontrado em Arquivos/ deste PAJ.';
                }
            } catch (e) {
                this.erroArquivos = 'Falha de rede: ' + e.message;
            } finally {
                this.carregandoArquivos = false;
            }
        },

        fecharModal() {
            this.modalAberto = false;
            this.arquivoPendente = null;
        },

        formatarTamanho(bytes) {
            if (!bytes) return '';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
            return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        },

        formatarData(epochSeconds) {
            if (!epochSeconds) return '';
            const d = new Date(epochSeconds * 1000);
            return d.toLocaleDateString('pt-BR') + ' ' + d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
        },

        // Clique num arquivo do modal: se for CNIS completo, pergunta o modo
        // (simples/completo) num segundo passo do MESMO modal antes de
        // iniciar; se for simples, so' sabe fazer o calculo simples, inicia
        // direto.
        escolherArquivo(arquivo) {
            if (arquivo.tipo === 'completo') {
                this.arquivoPendente = arquivo;
                return;
            }
            this.iniciarCalculo(arquivo.caminho, 'simples');
        },

        escolherModo(modo) {
            this.iniciarCalculo(this.arquivoPendente.caminho, modo);
        },

        async iniciarCalculo(caminho, modo) {
            let resp, data;
            try {
                resp = await fetch('/api/calcular-cnis/iniciar/' + this.pajNorm, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ arquivo: caminho, modo: modo }),
                });
                data = await resp.json().catch(() => ({}));
            } catch (e) {
                showToast('Falha de rede: ' + e, 'error');
                return;
            }
            if (!data.ok) {
                showToast('Erro: ' + (data.erro || 'falha ao iniciar o cálculo'), 'error');
                return;
            }
            this.modalAberto = false;
            this.arquivoPendente = null;
            this.status = 'running';
            this.error = '';
            this.arquivoEmUso = caminho;
            const rotulo = modo === 'completo' ? 'completo (com remunerações)' : 'simples';
            showToast('Calculando tempo de contribuição (modo ' + rotulo + ') a partir de "' + caminho.replace(/^Arquivos\//, '') + '"…', 'info');
            this.startPolling();
        },

        startPolling() {
            if (this._pollTimer) return;
            this._pollTimer = setInterval(async () => {
                await this.fetchStatus();
                if (this.status === 'done' || this.status === 'error') {
                    clearInterval(this._pollTimer);
                    this._pollTimer = null;
                    if (this.status === 'done') {
                        showToast('Cálculo concluído — PDF disponível na aba "Peças". Recarregando…', 'success');
                        setTimeout(() => window.location.reload(), 1200);
                    } else {
                        showToast('Erro ao calcular: ' + (this.error || 'ver log no servidor'), 'error');
                    }
                }
            }, 3000);
        },
    };
}
