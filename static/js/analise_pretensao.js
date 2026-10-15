// Componente Alpine "Análise feita pela IA" — diagnóstico de PAJ por rota de
// pretensão escolhida no dropdown da barra de ações: "bpc" (skill dedicada
// SISDPU-AnalisarPAJ-BPC, playbook teses-dpu), "bi" (skill dedicada
// SISDPU-AnalisarPAJ-BI, parecer de viabilidade de benefício por incapacidade),
// "pensao_morte" (skill dedicada SISDPU-AnalisarPAJ-Pensao, playbook teses-dpu),
// "salario_maternidade" (skill dedicada SISDPU-AnalisarPAJ-SalarioMaternidade,
// playbook teses-dpu), "auxilio_reclusao" (skill dedicada
// SISDPU-AnalisarPAJ-AuxilioReclusao, playbook teses-dpu) ou "generica"
// (diagnóstico direto via PROMPT_MAX, sem playbook de área —
// cobre pretensões sem skill dedicada, ou PAJs cadastrados no SISDPU com a
// pretensão errada) + reenvio (ajustes na análise, ou pedido de redação da
// petição inicial). Ver CONFIG_TIPOS em analise_pretensao_service.py para a
// lista completa/atual de rotas — este componente é agnóstico ao "tipo",
// só repassa a string escolhida no dropdown para o backend.
//
// Uso:
//   <div x-data="analiseIaApp('{{ paj_norm }}', '{{ tipo_ia_padrao }}')" x-init="init()"
//        @iniciar-analise-ia.window="iniciar($event.detail && $event.detail.tipo, $event.detail && $event.detail.orientacao)">
//     ...
//   </div>
//
// O botão "Analisar" (+ dropdown de tipo + campo "Orientação diretiva para
// análise da IA") na barra de ações dispara
// `window.dispatchEvent(new CustomEvent('iniciar-analise-ia', {detail:{tipo: tipoEscolhido, orientacao: textoLivre}}))`
// — assim o componente não precisa ficar acoplado à barra de ações.
// `orientacao` é opcional (texto livre do Defensor, direcionando a análise
// quando já há uma linha definida pro caso) — repassada ao backend e
// injetada na instrução real mandada pra IA (ver
// services/analise_pretensao_service.py:_montar_instrucao_diagnostico).
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
                return marked.parse(this.textoFinal());
            } catch (e) {
                return this.summary;
            }
        },

        // Corta o "pensamento em voz alta" da IA — mensagens de progresso
        // (carregando playbook, lendo documentos etc.) que vêm concatenadas
        // ANTES do diagnóstico final no mesmo campo `summary` (pedido do
        // usuário, 2026-09-29: "o pensamento em voz alta não nos interessa").
        // O diagnóstico final sempre abre com uma heading nível 2 ("##
        // Diagnóstico ...", convenção das skills de teses-dpu e do prompt
        // genérico — ver _montar_instrucao_diagnostico em
        // analise_pretensao_service.py); a narração de progresso é só prosa,
        // sem heading nenhuma. Se não achar nenhuma "## " (formato
        // inesperado), mostra o texto completo — fallback seguro pra nunca
        // esconder conteúdo de verdade.
        textoFinal() {
            const m = this.summary.match(/^##\s+.+$/m);
            return m ? this.summary.slice(m.index) : this.summary;
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

        async iniciar(tipoForcado, orientacao) {
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
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ orientacao: (orientacao || '').trim() }),
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

        // "Minutar Petição Inicial" (15/10/2026): manda a instrução de minuta —
        // o texto digitado entra como parâmetros — e a etapa do PAJ passa a
        // "Elaborando minuta de inicial" (services/etapa_paj_service.py).
        async minutarInicial() {
            const params = (this.mensagemReenvio || '').trim();
            const msg = 'Minute a petição inicial deste PAJ, com base na análise já feita. '
                + 'Salve a minuta em .docx com "peticao_inicial" no nome do arquivo.'
                + (params ? '\n\nParâmetros do Defensor para a minuta:\n' + params : '');
            const ok = await this.enviarReenvio(msg);
            if (!ok) return;
            try {
                await fetch('/api/paj/' + encodeURIComponent(this.pajNorm) + '/etapa/minuta-solicitada', { method: 'POST' });
                const barra = document.querySelector('[data-etapa]');
                if (barra) setTimeout(function () { window.location.reload(); }, 800);
            } catch (e) { /* a etapa é só indicativa — a minuta segue */ }
        },

        async enviarReenvio(mensagemPronta) {
            const msg = (typeof mensagemPronta === 'string' ? mensagemPronta : (this.mensagemReenvio || '')).trim();
            if (!msg) {
                showToast('Digite uma mensagem primeiro', 'warning');
                return;
            }
            if (this.status === 'running') {
                showToast('Aguarde a operação atual terminar', 'warning');
                return false;
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
                    return false;
                }
                this.mensagemReenvio = '';
                showToast('Mensagem enviada — Claude está processando', 'info');
                this.startPolling();
                return true;
            } catch (e) {
                this.status = 'error';
                this.error = 'Falha ao enviar: ' + e.message;
                return false;
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
