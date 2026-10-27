// Componente Alpine "Calcular Valor da Causa" — botão próprio na barra "Ações
// com IA" > "Cálculos" da página do PAJ. Fluxo em 3 fases, todas com
// confirmação explícita do Defensor:
//   1. Escolhe a petição inicial (modal de seleção).
//   2. A IA lê a petição (SÓ LEITURA, nunca altera) e os documentos do PAJ,
//      extrai os parâmetros; o modal "Confira os parâmetros" abre sempre
//      (pré-preenchido, campos não encontrados pela IA destacados) — o
//      cálculo não roda sem confirmação explícita.
//   3. Calculado, o modal de resultado mostra as premissas e o PDF, com o
//      botão "Inserir Valor da Causa na Inicial" (grava uma CÓPIA da
//      petição, nunca a original).
// Ver services/calcular_valor_causa_service.py para o fluxo completo do lado
// do servidor.
function calcularValorCausaApp(pajNorm) {
    return {
        pajNorm: pajNorm,

        // Seleção da petição
        modalPeticaoAberto: false,
        carregandoPeticoes: false,
        peticoes: [],
        erroPeticoes: '',

        // Estado geral (espelha calcular_valor_causa_service.get_status)
        status: 'idle',
        error: '',
        faltando: [],
        parametros: {},
        resultadoCalc: null,
        resultadoInsercao: null,
        inserindo: false,

        // Listas auxiliares para edição de periodos/recebido/sem_13 no modal
        // de confirmação (o esquema usa dict/lista; aqui viram arrays
        // editáveis e só voltam pro formato do esquema ao confirmar).
        periodosLista: [],
        recebidoLista: [],
        sem13Texto: '',

        _pollTimer: null,

        async init() {
            await this.fetchStatus();
            this._sincronizarListasAuxiliares();
            if (this.emAndamento()) {
                this.startPolling();
            }
        },

        emAndamento() {
            return this.status === 'extraindo' || this.status === 'calculando';
        },

        ehFaltando(campo) {
            return this.faltando.includes(campo);
        },

        async fetchStatus() {
            try {
                const resp = await fetch('/api/calcular-valor-causa/status/' + this.pajNorm);
                const data = await resp.json();
                this.status = data.status || 'idle';
                this.error = data.error || '';
                this.faltando = data.faltando || [];
                this.parametros = data.parametros || {};
                this.resultadoCalc = data.resultado_calc || null;
                this.resultadoInsercao = data.resultado_insercao || null;
            } catch (e) {
                this.error = 'Erro ao consultar status: ' + e.message;
                this.status = 'erro';
            }
        },

        _sincronizarListasAuxiliares() {
            const p = this.parametros || {};
            this.periodosLista = (p.periodos || []).map(x => ({
                inicio: x.inicio || '', fim: x.fim || '',
                valor_mensal: x.valor_mensal ?? null,
            }));
            const rec = p.recebido || {};
            this.recebidoLista = Object.entries(rec).map(([competencia, valor]) => ({ competencia, valor }));
            this.sem13Texto = (p.sem_13 || []).join(', ');
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

        // ── Fase 1: escolher a petição ──────────────────────────────────────
        async abrirModalPeticao() {
            this.modalPeticaoAberto = true;
            this.carregandoPeticoes = true;
            this.erroPeticoes = '';
            this.peticoes = [];
            try {
                const resp = await fetch('/api/calcular-valor-causa/peticoes/' + this.pajNorm);
                const data = await resp.json();
                this.peticoes = data.peticoes || [];
                if (this.peticoes.length === 0) {
                    this.erroPeticoes = 'Nenhuma petição inicial encontrada em "Peças IA" deste PAJ.';
                }
            } catch (e) {
                this.erroPeticoes = 'Falha de rede: ' + e.message;
            } finally {
                this.carregandoPeticoes = false;
            }
        },

        fecharModalPeticao() {
            this.modalPeticaoAberto = false;
        },

        async escolherPeticao(peticao) {
            let resp, data;
            try {
                resp = await fetch('/api/calcular-valor-causa/iniciar/' + this.pajNorm, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ arquivo: peticao.caminho }),
                });
                data = await resp.json().catch(() => ({}));
            } catch (e) {
                showToast('Falha de rede: ' + e, 'error');
                return;
            }
            if (!data.ok) {
                showToast('Erro: ' + (data.erro || 'falha ao iniciar a leitura'), 'error');
                return;
            }
            this.modalPeticaoAberto = false;
            this.status = 'extraindo';
            this.error = '';
            showToast('Lendo a petição "' + peticao.nome + '" e os documentos do PAJ (somente leitura)…', 'info');
            this.startPolling();
        },

        // ── Fase 2: conferir/completar os parâmetros ────────────────────────
        adicionarPeriodo() {
            this.periodosLista.push({ inicio: '', fim: '', valor_mensal: null });
        },
        removerPeriodo(i) {
            this.periodosLista.splice(i, 1);
        },
        adicionarRecebido() {
            this.recebidoLista.push({ competencia: '', valor: null });
        },
        removerRecebido(i) {
            this.recebidoLista.splice(i, 1);
        },

        podeCalcular() {
            const p = this.parametros || {};
            if (!p.tipo) return false;
            const temValor = !!p.valor_mensal || !!p.piso_sm;
            if (!temValor) return false;
            const periodosValidos = this.periodosLista.length > 0 && this.periodosLista.every(x => x.inicio && x.fim);
            const temMarco = !!p.marco && !!p.data_marco;
            if (!temMarco && !periodosValidos) return false;
            if (p.meses_vincendos === null || p.meses_vincendos === undefined || p.meses_vincendos === '') return false;
            return true;
        },

        _montarParametrosFinais() {
            const p = { ...this.parametros };
            if (p.valor_mensal) p.valor_mensal = Number(p.valor_mensal);
            if (p.meses_vincendos !== '' && p.meses_vincendos !== null && p.meses_vincendos !== undefined) {
                p.meses_vincendos = Number(p.meses_vincendos);
            }
            const periodosValidos = this.periodosLista.filter(x => x.inicio && x.fim);
            if (periodosValidos.length) {
                p.periodos = periodosValidos.map(x => {
                    const item = { inicio: x.inicio, fim: x.fim };
                    if (x.valor_mensal) item.valor_mensal = Number(x.valor_mensal);
                    return item;
                });
            } else {
                delete p.periodos;
            }
            const recebidoValido = this.recebidoLista.filter(x => x.competencia && x.valor);
            if (recebidoValido.length) {
                p.recebido = {};
                for (const r of recebidoValido) p.recebido[r.competencia] = Number(r.valor);
            } else {
                delete p.recebido;
            }
            const anos = this.sem13Texto.split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n));
            if (anos.length) p.sem_13 = anos; else delete p.sem_13;
            return p;
        },

        async confirmarCalculo() {
            if (!this.podeCalcular()) {
                showToast('Preencha os campos obrigatórios antes de calcular.', 'error');
                return;
            }
            const params = this._montarParametrosFinais();
            let resp, data;
            try {
                resp = await fetch('/api/calcular-valor-causa/confirmar/' + this.pajNorm, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(params),
                });
                data = await resp.json().catch(() => ({}));
            } catch (e) {
                showToast('Falha de rede: ' + e, 'error');
                return;
            }
            if (!data.ok) {
                showToast('Erro: ' + (data.erro || 'falha ao confirmar os parâmetros'), 'error');
                return;
            }
            this.status = 'calculando';
            showToast('Calculando o valor da causa…', 'info');
            this.startPolling();
        },

        async cancelarConfirmacao() {
            await fetch('/api/calcular-valor-causa/cancelar/' + this.pajNorm, { method: 'POST' }).catch(() => {});
            this.status = 'idle';
            this.parametros = {};
            this.faltando = [];
        },

        // ── Fase 3: revisão do resultado + inserir na petição ───────────────
        async inserirValorCausa() {
            if (this.inserindo) return;
            this.inserindo = true;
            try {
                const resp = await fetch('/api/calcular-valor-causa/inserir/' + this.pajNorm, { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (!data.ok) {
                    showToast('Erro: ' + (data.erro || 'falha ao inserir o valor na petição'), 'error');
                    return;
                }
                this.resultadoInsercao = data;
                showToast('Valor inserido em "' + data.docx + '" — a petição original não foi alterada.', 'success');
            } catch (e) {
                showToast('Falha de rede: ' + e, 'error');
            } finally {
                this.inserindo = false;
            }
        },

        async fecharRevisao() {
            await fetch('/api/calcular-valor-causa/fechar/' + this.pajNorm, { method: 'POST' }).catch(() => {});
            window.location.reload();
        },

        startPolling() {
            if (this._pollTimer) return;
            this._pollTimer = setInterval(async () => {
                await this.fetchStatus();
                if (!this.emAndamento()) {
                    clearInterval(this._pollTimer);
                    this._pollTimer = null;
                    if (this.status === 'aguardando_confirmacao') {
                        this._sincronizarListasAuxiliares();
                        showToast('Leitura concluída — confira os parâmetros do cálculo.', 'info');
                    } else if (this.status === 'aguardando_revisao') {
                        showToast('Valor da causa calculado — confira os parâmetros e o PDF.', 'success');
                    } else if (this.status === 'erro') {
                        showToast('Erro: ' + (this.error || 'ver log no servidor'), 'error');
                    }
                }
            }, 3000);
        },
    };
}
