// Componente Alpine "Consulta RPV/Precatório" — botão ONE-CLICK na barra
// "Ações com IA" da página do PAJ (pedido explícito do usuário, 2026-09-22:
// nenhum dado digitado, tudo já está no processo/PAJ — ver
// services/consulta_rpv_service.py). Sem modal: o clique já dispara a rotina;
// só faz polling de status até concluir, igual ao padrão de calcular_cnis.js
// (menos o passo de escolha de arquivo, que aqui não existe).
function consultaRpvApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        status: 'idle',
        log: [],
        _pollTimer: null,

        async init() {
            await this.fetchStatus();
            if (this.status === 'running') {
                this.startPolling();
            }
        },

        async fetchStatus() {
            try {
                const resp = await fetch('/api/consulta-rpv/status/' + this.pajNorm);
                const data = await resp.json();
                this.status = data.status || 'idle';
                this.log = data.log || [];
            } catch (e) {
                this.status = 'error';
            }
        },

        async iniciar() {
            if (this.status === 'running') return;
            let resp, data;
            try {
                resp = await fetch('/api/consulta-rpv/iniciar/' + this.pajNorm, { method: 'POST' });
                data = await resp.json().catch(() => ({}));
            } catch (e) {
                showToast('Falha de rede: ' + e, 'error');
                return;
            }
            if (!data.ok) {
                showToast('Erro: ' + (data.erro || 'falha ao iniciar a consulta'), 'error');
                return;
            }
            this.status = 'running';
            this.log = [];
            showToast('Consultando pagamento de RPV/Precatório — verificando o(s) processo(s) vinculado(s)…', 'info');
            this.startPolling();
        },

        // Resumo curto pro toast final — conta quantas movimentações entraram
        // (pagas/aguardando) a partir das linhas de log "ok"/"muted" mais
        // recentes, sem precisar expor a estrutura interna de `resultados`.
        resumoFinal() {
            const ultimo = this.log[this.log.length - 1];
            return ultimo ? ultimo.msg : '';
        },

        startPolling() {
            if (this._pollTimer) return;
            this._pollTimer = setInterval(async () => {
                await this.fetchStatus();
                if (this.status === 'done' || this.status === 'error') {
                    clearInterval(this._pollTimer);
                    this._pollTimer = null;
                    if (this.status === 'done') {
                        showToast('Consulta de RPV/Precatório concluída — ' + this.resumoFinal(), 'success');
                        setTimeout(() => window.location.reload(), 1500);
                    } else {
                        showToast('Erro na consulta de RPV/Precatório: ' + this.resumoFinal(), 'error');
                    }
                }
            }, 3000);
        },
    };
}
