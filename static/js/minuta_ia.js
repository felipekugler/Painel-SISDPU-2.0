// Módulo Alpine "Gerar Minuta" (barra de ações do PAJ) — dropdown de tipo de
// minuta + modal de seleção de documentos-fonte que REAPROVEITA as tabelas
// compartilhadas do painel (eventos do processo / movimentações do PAJ) em
// "modo seleção", em vez de uma tabela custom — ver eventos_processo.js e
// movimentacoes.js (toggleSelecaoIA/estaSelecionadoIA/modoSelecao).
//
// Fluxo em 2 etapas (mesmo padrão de "Gerar petição com IA" + "Incluir
// petição nos anexos" em acoes_eproc.js): a IA só devolve TEXTO (rascunho,
// editável numa caixa Quill — ver criarEditorRico em rich_editor.js); nada é
// salvo em arquivo até o Defensor clicar "Gerar arquivo (DOC/PDF)", que
// formata o texto ATUAL da caixa (já revisado) sem chamar IA de novo.
//
// Uso:
//   <div x-data="minutaIaApp('{{ paj_norm }}')" x-init="init(window._paj_processos_judiciais_completo)">
//     {% include "partials/minuta_ia.html" %}
//   </div>
//
// A leitura da seleção nos dois componentes-filho (tabProcesso/tabPaj) é
// feita sob demanda via Alpine.$data($refs.*) — não há bridging por evento
// nem estado duplicado aqui; este componente só LÊ o que o filho já mantém.
function minutaIaApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        tipos: [],
        tipoSlug: '',

        processos: [],
        processoSelecionado: '',

        modalAberto: false,
        abaAtiva: 'processo', // 'processo' | 'paj'
        _processoCarregadoPara: '',
        _pajCarregado: false,

        status: 'idle', // idle|running|done|error
        gerando: false,
        erro: '',
        _pollTimer: null,

        // Rascunho editável (Quill; cai pra textarea simples se o Quill não
        // montar) — mesmo padrão de acoes_eproc.js.
        uidMinuta: 'qminuta-' + Math.random().toString(36).slice(2, 9),
        _editorMinuta: null, quillMinutaOk: true, _montandoEditor: false,
        minutaTexto: '',

        // "Gravar" — persiste o texto editado no backend (sem gerar arquivo),
        // pra não perder a edição manual num reload da página (o Quill só
        // vive na memória do navegador).
        gravando: false,
        gravado: false,
        erroGravar: '',

        // "Gerar arquivo (DOC/PDF)" — formatação pura, sem IA.
        gerandoArquivo: false,
        erroArquivo: '',
        arquivos: [],

        async init(processosJudiciais) {
            this.processos = (processosJudiciais || []).map((p) => p.processo_judicial).filter(Boolean);
            if (this.processos.length) {
                this.processoSelecionado = this.processos[0];
            } else {
                // PAJ sem processo judicial vinculado — a aba "Processo
                // Judicial" não tem o que mostrar; abre direto na "PAJ".
                this.abaAtiva = 'paj';
            }
            await Promise.all([this.carregarTipos(), this.fetchStatus()]);
            if (this.status === 'running') this._iniciarPolling();
        },

        async carregarTipos() {
            try {
                const resp = await fetch('/api/minutas/tipos');
                const data = await resp.json();
                this.tipos = data.tipos || [];
                if (this.tipos.length) this.tipoSlug = this.tipos[0].slug;
            } catch (e) {
                console.warn('Falha ao carregar tipos de minuta:', e);
            }
        },

        async fetchStatus() {
            try {
                const resp = await fetch('/api/minutas/gerar/' + encodeURIComponent(this.pajNorm) + '/status');
                const data = await resp.json();
                const statusAnterior = this.status;
                this.status = data.status || 'idle';
                this.erro = data.erro || '';
                // Só popula a caixa na TRANSIÇÃO pra "done" (nunca durante o
                // polling já em 'done' — senão sobrescreveria edição manual
                // do Defensor a cada 5s).
                if (this.status === 'done' && data.peticao_html && statusAnterior !== 'done') {
                    await this._popularMinuta(data.peticao_html);
                }
            } catch (e) {
                // silencioso — não trava o carregamento da página
            }
        },

        // --- Caixa de edição do rascunho ---
        async _montarEditorSeNecessario() {
            if (!this._editorMinuta && this.quillMinutaOk && !this._montandoEditor) {
                this._montandoEditor = true;
                await this.$nextTick();
                this._editorMinuta = criarEditorRico(this.uidMinuta, {
                    placeholder: 'Texto da minuta.',
                });
                this.quillMinutaOk = !!this._editorMinuta;
                // "Gravado ✓" some assim que o Defensor mexer de novo no texto —
                // sinaliza que a edição atual ainda não foi persistida.
                if (this._editorMinuta) {
                    this._editorMinuta.quill.on('text-change', (delta, oldDelta, source) => {
                        if (source === 'user') this.gravado = false;
                    });
                }
            }
        },
        async _popularMinuta(html) {
            await this._montarEditorSeNecessario();
            if (this._editorMinuta) this._editorMinuta.setHTML(html);
            else this.minutaTexto = (html || '').replace(/<[^>]+>/g, '\n').trim();
            this.gravado = false;
        },
        minutaEfetiva() {
            return this._editorMinuta ? this._editorMinuta.getHTML() : this.minutaTexto;
        },

        // --- Acesso aos componentes-filho (tabelas compartilhadas) ---
        // IMPORTANTE: o x-ref fica no WRAPPER (x-show="abaAtiva===..."), não no
        // elemento que declara x-data — Alpine NÃO expõe em $refs um ref que
        // esteja no MESMO elemento onde um x-data aninhado começa (testado
        // empiricamente: o ref simplesmente não aparece no $refs do pai nesse
        // caso). Por isso pegamos o wrapper primeiro e procuramos o elemento
        // [x-data] dentro dele pra then pedir os dados via Alpine.$data().
        _dataProcesso() {
            const wrap = this.$refs.tabProcessoWrap;
            const el = wrap && wrap.querySelector('[x-data]');
            return el ? window.Alpine.$data(el) : null;
        },
        _dataPaj() {
            const wrap = this.$refs.tabPajWrap;
            const el = wrap && wrap.querySelector('[x-data]');
            return el ? window.Alpine.$data(el) : null;
        },
        totalSelecionadosProcesso() {
            const d = this._dataProcesso();
            return d ? d.selecionadosIA.size : 0;
        },
        totalSelecionadosPaj() {
            const d = this._dataPaj();
            return d ? d.selecionadosIA.size : 0;
        },

        abrirModal() {
            this.modalAberto = true;
            this.erro = '';
            if (this.processoSelecionado && this._processoCarregadoPara !== this.processoSelecionado) {
                const d = this._dataProcesso();
                if (d) { d.carregar(this.processoSelecionado); this._processoCarregadoPara = this.processoSelecionado; }
            }
            if (!this._pajCarregado) {
                const d = this._dataPaj();
                if (d) {
                    // Seta modoSelecao DIRETO no componente real (não via x-data
                    // wrapper por fora — ver comentário em movimentacoes_tabela.html
                    // sobre por que isso sombrearia o default modoSelecao:false).
                    d.modoSelecao = true;
                    d.carregar({ movs: window._paj_movimentacoes, anexos: window._paj_anexos_sisdpu, pajNorm: this.pajNorm });
                    this._pajCarregado = true;
                }
            }
        },
        fecharModal() {
            this.modalAberto = false;
        },

        trocarProcesso(numero) {
            this.processoSelecionado = numero;
            const d = this._dataProcesso();
            if (d) { d.carregar(numero); this._processoCarregadoPara = numero; }
        },

        async gerar() {
            const dProcesso = this._dataProcesso();
            const dPaj = this._dataPaj();
            const documentos = [
                ...(dProcesso ? Array.from(dProcesso.selecionadosIA.values()) : []),
                ...(dPaj ? Array.from(dPaj.selecionadosIA.values()) : []),
            ];
            if (!documentos.length) {
                this.erro = 'Selecione ao menos um documento (processo ou PAJ) antes de gerar a minuta.';
                return;
            }
            this.gerando = true;
            this.erro = '';
            this.erroArquivo = '';
            this.arquivos = [];
            try {
                const resp = await fetch('/api/minutas/gerar/' + encodeURIComponent(this.pajNorm), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        processo: this.processoSelecionado,
                        tipo_slug: this.tipoSlug,
                        documentos: documentos,
                    }),
                });
                const j = await resp.json();
                if (!j.ok) { this.erro = j.erro || 'Falha ao iniciar a geração.'; this.gerando = false; return; }
                this.status = 'running';
                this.modalAberto = false;
                this._iniciarPolling();
            } catch (e) {
                this.erro = 'Erro de rede: ' + e;
                this.gerando = false;
            }
        },

        _iniciarPolling() {
            if (this._pollTimer) return;
            this.gerando = true;
            this._pollTimer = setInterval(() => this._checarStatus(), 5000);
        },

        async _checarStatus() {
            await this.fetchStatus();
            if (this.status === 'running') return;
            clearInterval(this._pollTimer);
            this._pollTimer = null;
            this.gerando = false;
        },

        // "Gravar" — persiste o texto ATUAL da caixa no backend, sem gerar
        // arquivo. Evita perder a edição manual se a página recarregar (o
        // Quill só existe na memória do navegador até isso ser salvo).
        async gravarRascunho() {
            const html = this.minutaEfetiva();
            this.gravando = true;
            this.erroGravar = '';
            try {
                const resp = await fetch('/api/minutas/salvar-rascunho/' + encodeURIComponent(this.pajNorm), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ peticao_html: html }),
                });
                const j = await resp.json();
                if (!j.ok) { this.erroGravar = j.erro || 'Falha ao gravar.'; return; }
                this.gravado = true;
            } catch (e) {
                this.erroGravar = 'Erro de rede: ' + e;
            } finally {
                this.gravando = false;
            }
        },

        // "Gerar arquivo (DOC/PDF)" — formata o texto ATUAL da caixa (com os
        // ajustes manuais do Defensor) e salva em "Peças IA", sem chamar IA.
        async gerarArquivo() {
            const html = this.minutaEfetiva();
            if (!html || !html.replace(/<[^>]+>/g, '').trim()) {
                this.erroArquivo = 'A caixa de texto está vazia — escreva ou gere o texto antes de gerar o arquivo.';
                return;
            }
            this.gerandoArquivo = true;
            this.erroArquivo = '';
            try {
                const resp = await fetch('/api/minutas/formatar/' + encodeURIComponent(this.pajNorm), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ tipo_slug: this.tipoSlug, peticao_html: html }),
                });
                const j = await resp.json();
                if (!j.ok) { this.erroArquivo = j.erro || 'Falha ao gerar o arquivo.'; return; }
                this.arquivos = j.arquivos || [];
            } catch (e) {
                this.erroArquivo = 'Erro de rede: ' + e;
            } finally {
                this.gerandoArquivo = false;
            }
        },
    };
}
