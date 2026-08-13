// Componente Alpine "Análise feita pela IA" — diagnóstico de PAJ por rota de
// pretensão escolhida no dropdown da barra de ações: "bpc" (skill dedicada
// SISDPU-AnalisarPAJ-BPC, playbook teses-dpu) ou "generica" (diagnóstico
// direto via PROMPT_MAX, sem playbook de área — cobre pretensões sem skill
// dedicada, ou PAJs cadastrados no SISDPU com a pretensão errada) + reenvio
// (ajustes na análise, ou pedido de redação da petição inicial).
//
// Uso:
//   <div x-data="analiseIaApp('{{ paj_norm }}', '{{ tipo_ia_padrao }}')" x-init="init()"
//        @iniciar-analise-ia.window="iniciar($event.detail && $event.detail.tipo)">
//     ...
//   </div>
//
// O botão "Analisar" (+ dropdown de tipo) na barra de ações dispara
// `window.dispatchEvent(new CustomEvent('iniciar-analise-ia', {detail:{tipo: tipoEscolhido}}))`
// — assim o componente não precisa ficar acoplado à barra de ações.
function analiseIaApp(pajNorm, tipoPadrao) {
    return {
        pajNorm: pajNorm,
        tipo: tipoPadrao || '',
        status: 'idle',
        lastAction: '',
        summary: '',
        error: '',
        mensagemReenvio: '',
        enviando: false,
        _pollTimer: null,

        async init() {
            await this.fetchStatus();
            if (this.status === 'running') this.startPolling();
        },

        // Resposta da IA vem em Markdown (títulos, tabelas, listas) — converte
        // pra HTML (lib `marked`, carregada em base.html) pra renderizar de
        // verdade em vez de mostrar a sintaxe crua. Combinar com a classe
        // `prose` no elemento (ver base.html: Tailwind CDN com
        // `?plugins=typography`) pra tabelas/listas ficarem legíveis.
        renderedSummary() {
            if (!this.summary) return '';
            try {
                return marked.parse(this.summary);
            } catch (e) {
                return this.summary;
            }
        },

        async fetchStatus() {
            try {
                const r = await fetch('/api/analise-pretensao/status/' + this.pajNorm);
                const d = await r.json();
                this.status = d.status || 'idle';
                this.lastAction = d.last_action || '';
                this.summary = d.summary || '';
                this.error = d.error || '';
                if (d.tipo) this.tipo = d.tipo;
            } catch (e) {
                this.error = 'Erro ao consultar status: ' + e.message;
            }
        },

        async iniciar(tipoForcado) {
            const tipo = tipoForcado || this.tipo;
            if (!tipo) {
                showToast('Nenhuma skill de diagnóstico aplicável a este PAJ', 'warning');
                return;
            }
            if (this.status === 'running') {
                showToast('Já há uma análise em andamento', 'warning');
                return;
            }
            this.tipo = tipo;
            this.status = 'running';
            this.lastAction = 'iniciando diagnóstico…';
            this.error = '';
            this.summary = '';
            try {
                const r = await fetch('/api/analise-pretensao/' + tipo + '/start/' + this.pajNorm, {
                    method: 'POST',
                });
                const d = await r.json();
                if (!d.ok) {
                    // Se o backend recusou porque JÁ HÁ uma análise rodando
                    // (corrida entre abas/reloads — este cliente achava que
                    // estava idle), não é erro real: sincroniza com o status
                    // verdadeiro do servidor e volta a acompanhar em vez de
                    // mascarar uma análise legítima como "erro".
                    if ((d.erro || '').toLowerCase().includes('em andamento')) {
                        await this.fetchStatus();
                        if (this.status === 'running') {
                            showToast('Já havia uma análise em andamento — acompanhando…', 'info');
                            this.startPolling();
                            return;
                        }
                    }
                    this.status = 'error';
                    this.error = d.erro || 'Falha ao iniciar.';
                    showToast(this.error, 'error');
                    return;
                }
                showToast('Diagnóstico iniciado — pode navegar livremente', 'info');
                this.startPolling();
            } catch (e) {
                this.status = 'error';
                this.error = 'Falha ao iniciar: ' + e.message;
            }
        },

        async enviarReenvio() {
            const msg = (this.mensagemReenvio || '').trim();
            if (!msg) {
                showToast('Digite uma mensagem primeiro', 'warning');
                return;
            }
            if (this.status === 'running') {
                showToast('Aguarde a operação atual terminar', 'warning');
                return;
            }
            this.enviando = true;
            this.status = 'running';
            this.lastAction = 'processando sua mensagem…';
            try {
                const r = await fetch('/api/analise-pretensao/reenviar/' + this.pajNorm, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mensagem: msg }),
                });
                const d = await r.json();
                if (!d.ok) {
                    this.status = 'error';
                    this.error = d.erro || 'Falha ao enviar.';
                    showToast(this.error, 'error');
                    return;
                }
                this.mensagemReenvio = '';
                showToast('Mensagem enviada — Claude está processando', 'info');
                this.startPolling();
            } catch (e) {
                this.status = 'error';
                this.error = 'Falha ao enviar: ' + e.message;
            } finally {
                this.enviando = false;
            }
        },

        startPolling() {
            if (this._pollTimer) return;
            this._pollTimer = setInterval(async () => {
                await this.fetchStatus();
                if (this.status !== 'running') {
                    clearInterval(this._pollTimer);
                    this._pollTimer = null;
                    if (this.status === 'done') {
                        showToast('Análise concluída', 'success');
                        // Recarrega pra refletir eventual peticao_inicial gerada
                        // (habilita o botão "Organizar Documentos para Ajuizamento").
                        setTimeout(() => window.location.reload(), 1200);
                    }
                }
            }, 2000);
        },
    };
}
