// Componente Alpine "Calcular Qualidade de Segurado" — botão próprio na
// barra "Ações com IA" da página do PAJ, ao lado de "Calcular Tempo de
// Contribuição" (ver services/calcular_qs_service.py). Deliberadamente
// separado: aqui é preciso escolher o TIPO DE BENEFÍCIO (o que define o
// rótulo da data-alvo/fato gerador — DII, data do óbito, data do parto/
// adoção, data da prisão) e informar essa data manualmente, além do arquivo
// de CNIS a usar. É uma análise rápida (só período de graça, art. 15 da
// LBPS) — não confundir com o botão "Calcular Tempo de Contribuição", que
// lê o CNIS competência a competência e é bem mais lento.
function calcularQsApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        modalAberto: false,
        carregandoArquivos: false,
        arquivos: [],
        erroArquivos: '',
        status: 'idle',
        error: '',
        tiposBeneficio: [
            { valor: 'bi', rotulo: 'Benefício por Incapacidade (DII)' },
            { valor: 'pensao_morte', rotulo: 'Pensão por Morte (data do óbito)' },
            { valor: 'salario_maternidade', rotulo: 'Salário-Maternidade (parto/adoção/guarda)' },
            { valor: 'auxilio_reclusao', rotulo: 'Auxílio-Reclusão (data da prisão)' },
            { valor: 'outro', rotulo: 'Outro (informar a data-alvo manualmente)' },
        ],
        tipoBeneficio: '', // '' até o Defensor escolher — evita cálculo com rótulo errado por padrão
        dataAlvoRotulo: '', // só usado/exibido quando tipoBeneficio === 'outro'
        dataAlvo: '', // DD/MM/AAAA — informada pelo Defensor, nunca inferida pela IA
        dataAlvoFonte: '', // texto livre, ex.: "Certidão de óbito"
        idadeInstituidor: '', // opcional, só refina a sugestão de direito adquirido

        async init() {
            await this.fetchStatus();
            if (this.status === 'running') {
                this.startPolling();
            }
        },

        async fetchStatus() {
            try {
                const resp = await fetch('/api/calcular-qs/status/' + this.pajNorm);
                const data = await resp.json();
                this.status = data.status || 'idle';
                this.error = data.error || '';
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
            // Reseta a cada abertura — mesmo motivo de calcular_cnis.js
            // (evita dados de uma sessão/PAJ anterior vazarem pro cálculo novo).
            this.tipoBeneficio = '';
            this.dataAlvoRotulo = '';
            this.dataAlvo = '';
            this.dataAlvoFonte = '';
            this.idadeInstituidor = '';
            try {
                // Reaproveita o mesmo endpoint de listagem do botão "Calcular
                // Tempo de Contribuição" — ver routes/calcular_qs.py, que por
                // sua vez delega a services.calcular_cnis_service.
                const resp = await fetch('/api/calcular-qs/arquivos/' + this.pajNorm);
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

        rotuloData(arquivo) {
            if (arquivo.data_emissao_fmt) return 'Emitido em ' + arquivo.data_emissao_fmt;
            return 'Data de emissão não identificada (salvo em ' + this.formatarData(arquivo.modificado) + ')';
        },

        // Rótulo de exibição para o campo "data-alvo", conforme o tipo de
        // benefício escolhido — só cosmético, o texto que de fato vai pro
        // parecer é decidido pela skill.
        rotuloDataAlvo() {
            const mapa = {
                bi: 'Data de Início da Incapacidade (DII)',
                pensao_morte: 'Data do óbito do instituidor',
                salario_maternidade: 'Data do parto, adoção ou guarda judicial',
                auxilio_reclusao: 'Data do recolhimento à prisão',
            };
            return mapa[this.tipoBeneficio] || (this.dataAlvoRotulo || 'Data-alvo (fato gerador)');
        },

        dataAlvoValida() {
            return /^\d{2}\/\d{2}\/\d{4}$/.test(this.dataAlvo.trim());
        },

        podeIniciar() {
            if (!this.tipoBeneficio) return false;
            if (this.tipoBeneficio === 'outro' && !this.dataAlvoRotulo.trim()) return false;
            if (!this.dataAlvoValida()) return false;
            return true;
        },

        nenhumNativo() {
            return this.arquivos.length > 0 && this.arquivos.every(a => !a.nativo);
        },

        async iniciarCalculo(caminhoArquivo, orientacao) {
            if (!this.podeIniciar()) {
                showToast('Preencha o tipo de benefício e a data-alvo (formato DD/MM/AAAA) antes de continuar.', 'error');
                return;
            }
            let resp, data;
            try {
                resp = await fetch('/api/calcular-qs/iniciar/' + this.pajNorm, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        arquivo: caminhoArquivo,
                        tipo_beneficio: this.tipoBeneficio,
                        data_alvo_rotulo: this.dataAlvoRotulo.trim(),
                        data_alvo: this.dataAlvo.trim(),
                        data_alvo_fonte: this.dataAlvoFonte.trim(),
                        idade_instituidor: this.idadeInstituidor.trim(),
                        orientacao: (orientacao || '').trim(),
                    }),
                });
                data = await resp.json().catch(() => ({}));
            } catch (e) {
                showToast('Falha de rede: ' + e, 'error');
                return;
            }
            if (!data.ok) {
                showToast('Erro: ' + (data.erro || 'falha ao iniciar a análise'), 'error');
                return;
            }
            this.modalAberto = false;
            this.status = 'running';
            this.error = '';
            showToast('Analisando qualidade de segurado a partir de "' + caminhoArquivo.replace(/^Arquivos\//, '') + '"…', 'info');
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
                        showToast('Análise concluída — PDF disponível na aba "Peças". Recarregando…', 'success');
                        setTimeout(() => window.location.reload(), 1200);
                    } else {
                        showToast('Erro ao analisar: ' + (this.error || 'ver log no servidor'), 'error');
                    }
                }
            }, 3000);
        },
    };
}
