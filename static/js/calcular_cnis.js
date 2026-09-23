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
        arquivoPendente: null, // {caminho, nome} — aguardando escolha de modo (CNIS completo)
        sexo: '', // '' | 'Masculino' | 'Feminino' — informado pelo Defensor, nunca inferido pela IA
        der: '', // DD/MM/AAAA, informada pelo Defensor — vazia = "não informada" (usa a data do cálculo)
        orientacao: '', // capturada de orientacaoDiretiva (x-data pai) em escolherArquivo()
        baixandoSat: false,
        satStatus: '',
        _pollTimer: null,
        _satEs: null,

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
            // Reseta a cada abertura — evita que Sexo/DER digitados numa
            // sessão anterior (ou noutro PAJ) vazem para um cálculo novo.
            this.sexo = '';
            this.der = '';
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
            // Propositalmente NÃO fecha o EventSource do SAT aqui: a
            // conexão SSE é o que mantém a rotina de download rodando do
            // lado do servidor (sse-starlette encerra o generator ao
            // detectar o cliente desconectado) — fechar o modal não deve
            // interromper um download em andamento.
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

        // Data que baliza qual arquivo é o mais atual: a EMISSÃO impressa no
        // cabeçalho do próprio CNIS (lida pelo servidor), não a data de
        // modificação do arquivo em disco — um CNIS desatualizado pode ter
        // sido regravado hoje e isso não o torna mais recente de verdade
        // (pedido do usuário, 2026-09-19). A lista já vem ordenada da API
        // pela data de emissão (mais recente primeiro).
        rotuloData(arquivo) {
            if (arquivo.data_emissao_fmt) return 'Emitido em ' + arquivo.data_emissao_fmt;
            return 'Data de emissão não identificada (salvo em ' + this.formatarData(arquivo.modificado) + ')';
        },

        // Botão "Baixar CNIS Atualizado do SAT" — baixa direto pra Arquivos/
        // deste PAJ (não pra "Arquivos SAT") só os 2 documentos que interessam
        // pro cálculo (Vínculos + Remunerações). Mesmo motor da aba "Arquivos
        // SAT" (ver routes/calcular_cnis.py `_stream_baixar_sat`); usuário
        // resolve login/CAPTCHA na janela do SAT que /api/sat/abrir abre.
        async baixarDoSat() {
            if (this.baixandoSat) return;
            this.baixandoSat = true;
            this.satStatus = 'Abrindo o SAT…';
            try {
                const r = await (await fetch('/api/sat/abrir')).json();
                this.satStatus = r.logado
                    ? 'Baixando CNIS atualizado do SAT…'
                    : 'Resolva o login/CAPTCHA na janela do SAT que abriu — o download continua automaticamente.';
            } catch (e) {
                this.satStatus = 'Erro ao abrir o SAT: ' + e.message;
            }

            const es = new EventSource('/api/calcular-cnis/baixar-sat/' + this.pajNorm);
            this._satEs = es;
            es.addEventListener('log', (ev) => {
                try {
                    const j = JSON.parse(ev.data);
                    if (j.msg) this.satStatus = j.msg;
                } catch (e) { /* ignora linha de log que não vier em JSON */ }
            });
            es.addEventListener('done', async (ev) => {
                let res = {};
                try { res = JSON.parse(ev.data); } catch (e) {}
                es.close();
                this._satEs = null;
                this.baixandoSat = false;
                if (res.ok === false) {
                    this.satStatus = 'Erro: ' + (res.detalhe || 'ver log do servidor');
                    showToast('Falha ao baixar o CNIS do SAT: ' + (res.detalhe || 'ver log'), 'error');
                    return;
                }
                this.satStatus = 'Concluído ✓ (sessão do SAT fechada)';
                showToast('CNIS atualizado baixado do SAT — sessão fechada, atualizando a lista…', 'success');
                await this.abrirModal(); // recarrega arquivos (já reflete o novo download)
            });
            es.onerror = () => {
                es.close();
                this._satEs = null;
                this.baixandoSat = false;
                this.satStatus = 'Erro de conexão com o servidor.';
                showToast('Erro de conexão ao baixar o CNIS do SAT.', 'error');
            };
        },

        // true quando NENHUM arquivo da lista é nativo (todos escaneados) —
        // o modal usa isso pra reforçar a sugestão de baixar do SAT.
        nenhumNativo() {
            return this.arquivos.length > 0 && this.arquivos.every(a => !a.nativo);
        },

        // Clique num arquivo do modal: escaneado nunca é selecionável (botão
        // já vem desabilitado na tela, isto é so' defesa em profundidade).
        // Se for CNIS completo, pergunta o modo (simples/completo) num
        // segundo passo do MESMO modal antes de iniciar; se for simples, so'
        // sabe fazer o calculo simples, inicia direto. `orientacao` vem da
        // caixa compartilhada "Orientação diretiva para análise da IA" (x-data
        // do container pai, ver paj_detail.html) — capturada aqui (passo 1)
        // porque escolherModo (passo 2, só CNIS completo) não tem acesso
        // direto a essa variável de fora deste componente Alpine.
        escolherArquivo(arquivo, orientacao) {
            if (!arquivo.nativo) return;
            this.orientacao = orientacao || '';
            if (arquivo.tipo === 'completo') {
                this.arquivoPendente = arquivo;
                return;
            }
            this.iniciarCalculo(arquivo.caminho, 'simples');
        },

        escolherModo(modo) {
            this.iniciarCalculo(this.arquivoPendente.caminho, modo);
        },

        // Botão "Considerar data de hoje" ao lado do campo DER.
        preencherDerHoje() {
            const hoje = new Date();
            const dd = String(hoje.getDate()).padStart(2, '0');
            const mm = String(hoje.getMonth() + 1).padStart(2, '0');
            this.der = `${dd}/${mm}/${hoje.getFullYear()}`;
        },

        // DER em branco é válida (= "não informada"); se preenchida, tem
        // que estar no formato DD/MM/AAAA — validação simples, o backend
        // valida de novo antes de iniciar o cálculo.
        derValida() {
            return !this.der.trim() || /^\d{2}\/\d{2}\/\d{4}$/.test(this.der.trim());
        },

        async iniciarCalculo(caminho, modo) {
            if (!this.derValida()) {
                showToast('DER inválida — use o formato DD/MM/AAAA ou deixe em branco.', 'error');
                return;
            }
            let resp, data;
            try {
                resp = await fetch('/api/calcular-cnis/iniciar/' + this.pajNorm, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        arquivo: caminho,
                        modo: modo,
                        sexo: this.sexo,
                        der: this.der.trim(),
                        orientacao: this.orientacao.trim(),
                    }),
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
