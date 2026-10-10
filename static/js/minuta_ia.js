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
//   <div x-data="minutaIaApp('{{ paj_norm }}', '{{ meta.paj }}')" x-init="init(window._paj_processos_judiciais_completo)">
//     {% include "partials/minuta_ia.html" %}
//   </div>
//
// A leitura da seleção nos dois componentes-filho (tabProcesso/tabPaj) é
// feita sob demanda via Alpine.$data($refs.*) — não há bridging por evento
// nem estado duplicado aqui; este componente só LÊ o que o filho já mantém.
function minutaIaApp(pajNorm, pajLabel) {
    return {
        pajNorm: pajNorm,
        pajLabel: pajLabel || '',
        tipos: [],
        tipoSlug: '',
        // Submenu "Petição com IA" (pedido do usuário, 2026-10-02 — era 3
        // botões fixos direto no menu; virou submenu dinâmico, mesmo padrão
        // de `mostrarSubmenuPadrao` abaixo). Fechado por padrão.
        mostrarSubmenuIa: false,
        // Rótulo mostrado no topo da caixa quando a origem NÃO é um dos
        // `tipos` da IA (ver "Petição padrão" abaixo) — os botões de tipo de
        // IA continuam usando o lookup em `tipos` (não mexi neles).
        labelAtivo: '',

        // --- "Petição padrão" — insere um modelo já cadastrado (Config-
        // urações › e-proc › Modelos de petição) direto na caixa, com os
        // MESMOS placeholders/mecanismo de "Usar modelo" (ver
        // acoes_eproc.js/rich_editor.js) — sem chamar IA.
        modelosPadrao: [],
        mostrarSubmenuPadrao: false,
        usandoModeloPadrao: false,

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
            await Promise.all([this.carregarTipos(), this.carregarModelosPadrao(), this.fetchStatus()]);
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

        // Agrupa `tipos` por categoria (cadastrada em Configurações › e-proc
        // › Petição com IA), PRESERVANDO a ordem cadastrada — tanto a ordem
        // das categorias (1ª aparição) quanto dos itens dentro delas; não
        // reordena por nome nem nada — a ordem de verdade é a da lista (ver
        // botões ▲/▼ na tela de Configurações). Tipo sem categoria cai num
        // grupo "" (sem cabeçalho visível, ver partials/minuta_ia.html —
        // não, é no paj_detail.html mesmo).
        tiposAgrupados() {
            const grupos = [];
            const indice = {};
            for (const t of this.tipos) {
                const cat = t.categoria || '';
                if (!(cat in indice)) {
                    indice[cat] = grupos.length;
                    grupos.push({ categoria: cat, itens: [] });
                }
                grupos[indice[cat]].itens.push(t);
            }
            return grupos;
        },

        async carregarModelosPadrao() {
            try {
                const resp = await fetch('/api/minutas/modelos-padrao');
                const data = await resp.json();
                this.modelosPadrao = data.modelos || [];
            } catch (e) {
                console.warn('Falha ao carregar modelos de petição padrão:', e);
            }
        },

        // Insere o modelo escolhido NA POSIÇÃO DO CURSOR da caixa de edição
        // da minuta — mesmo gesto/mesma resolução de placeholders de "Usar
        // modelo" (Ações no Eproc — ver acoes_eproc.js::usarModeloPeticao),
        // sem apagar o que já estiver escrito: dá pra compor mais de um
        // modelo, ou complementar com texto próprio antes/depois, antes de
        // "Gerar arquivo". Sem IA: depois de cada inserção, persiste o texto
        // JÁ COMPOSTO (não só o fragmento novo) como "done" no backend (ver
        // usar_modelo_padrao em minuta_ia_service.py) — pra sobreviver a um
        // reload igual o rascunho da IA já sobrevive via "Gravar".
        async usarModeloPadrao(modeloId) {
            const modelo = this.modelosPadrao.find((m) => m.id === modeloId);
            if (!modelo) return;
            this.mostrarSubmenuPadrao = false;
            this.usandoModeloPadrao = true;
            this.erro = '';
            try {
                const org = window._identificacaoOrgao || {};
                const subs = {
                    '[PROCESSO]': this.processoSelecionado || '',
                    '[PAJ]': this.pajLabel || '',
                    '[DATA]': dataPorExtenso(),
                    '[OFICIO]': org.oficio || '',
                    '[UNIDADE]': org.unidade || '',
                    '[DEFENSOR]': org.defensor_nome || '',
                    '[CARGO]': org.defensor_cargo || '',
                };
                try {
                    const j = await (await fetch('/api/acoes-eproc/qualificacao/' + encodeURIComponent(this.pajNorm))).json();
                    if (j.nome) subs['[NOME]'] = j.nome;
                    if (j.cpf) subs['[CPF]'] = j.cpf;
                } catch (e) { /* segue sem — modelo fica com [NOME]/[CPF] literais */ }

                const htmlModelo = paraHtmlSeTextoPuro(modelo.texto);
                const textoResolvido = resolverPlaceholders(htmlModelo, subs);

                this.tipoSlug = 'padrao';
                this.labelAtivo = 'Petição padrão';
                this.arquivos = [];
                this.erroArquivo = '';
                this.status = 'done';
                await this._montarEditorSeNecessario();
                if (this._editorMinuta) {
                    this._editorMinuta.insertHTML(textoResolvido);
                } else {
                    this.minutaTexto = (this.minutaTexto ? this.minutaTexto + '\n\n' : '') + htmlParaTextoSimples(textoResolvido);
                }
                this.gravado = false;

                const resp = await fetch('/api/minutas/usar-modelo/' + encodeURIComponent(this.pajNorm), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ peticao_html: this.minutaEfetiva() }),
                });
                const j2 = await resp.json();
                if (!j2.ok) this.erro = j2.erro || 'Falha ao gravar o modelo inserido.';
            } finally {
                this.usandoModeloPadrao = false;
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

        // Caixa compartilhada "Orientação diretiva para análise da IA" —
        // mora no x-data do container "Ações com IA" (elemento ANCESTOR
        // deste componente, não filho — por isso sobe com closest() em vez
        // de usar $refs, mesmo motivo/mesma técnica de _dataProcesso()/
        // _dataPaj() acima, só que na direção oposta). Mesma variável usada
        // por "Analisar"/"Calcular Tempo de Contribuição"/"Calcular
        // Qualidade de Segurado" (ver paj_detail.html).
        _orientacaoDiretiva() {
            const container = this.$el.parentElement && this.$el.parentElement.closest('[x-data]');
            if (!container) return '';
            const d = window.Alpine.$data(container);
            return (d && d.orientacaoDiretiva) || '';
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
            // SEM bloqueio incondicional aqui — pedido do usuário, 2026-09-29:
            // "Petição genérica" não exige documento nenhum (a Orientação
            // Diretiva sozinha já pode bastar); quem decide isso por tipo é o
            // back-end (`documentos_obrigatorios` em TIPOS_MINUTA, ver
            // minuta_ia_service.iniciar), não uma checagem fixa aqui — senão
            // essa exigência nunca dependia do tipo escolhido, bug real
            // relatado pelo usuário (bloqueava mesmo a "Petição genérica").
            // Sem documentos E sem orientação, o back-end recusa e devolve
            // `erro` explicando o que falta — tratado abaixo, mesmo caminho
            // de sempre.
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
                        orientacao: this._orientacaoDiretiva(),
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
