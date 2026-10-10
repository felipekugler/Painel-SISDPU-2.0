/* Componente COMPARTILHADO da tabela de movimentações do PAJ.
 *
 * Fonte única de renderização das movimentações — usado tanto pela aba
 * "Movimentações" da página do PAJ (paj_detail.html) quanto pelo modal de
 * movimentações da aba Retornos. Garante que as duas telas fiquem IDÊNTICAS
 * (mesmas colunas, cores via corLinhaMov, filtros, badges de documento).
 *
 * Os dados entram por `carregar(cfg)` — direto (x-init) ou via o evento
 * global `mov-tabela-carregar` (usado pelo modal da Retornos ao abrir um PAJ):
 *   window.dispatchEvent(new CustomEvent('mov-tabela-carregar', { detail: {
 *       movs: [...], anexos: [...], pajNorm: 'PAJ-...', maxSeq: -1 } }));
 *
 * Schema esperado de cada movimentação (CANÔNICO, de metadata.detalhes_sisdpu.movimentacoes):
 *   { seq, data, data_original, descricao(HTML), movimentacao, fases, usuario,
 *     links_decisao[], anexo, tem_anexo_icone, descricao_retorno, documentos[] }
 *
 * `tem_anexo_icone` (novo): indicador barato (sem popup) de que a coluna
 * "Anexo" do SISDPU tem um ícone clicável — calculado JÁ na sincronização
 * levíssima (ver sisdpu_client.py). Quando true e `documentos` ainda vazio
 * (a relação de verdade nunca foi obtida), a coluna Documentos mostra um
 * clipe em vez do "-" — clique abre o minimenu (ver abrirMenuAnexoSeq()
 * abaixo) com "Obter relação de documentos"/"Baixar documentos" pra AQUELE
 * seq só, sem precisar sincronizar o PAJ inteiro.
 */
function tabelaMovimentacoes() {
    return {
        // dados (preenchidos por carregar())
        movs: [],
        anexos: [],           // anexos_sisdpu p/ casar documento↔arquivo local
        pajNorm: '',
        maxSeqAnterior: -1,    // -1 = não destacar "novas"
        __mapaArquivoLocal: null,

        // filtros/ordenação (mesmos defaults da aba do PAJ)
        filtroFases: '',
        filtroUsuarioTodos: true,
        filtroUsuarioServidor: false,
        filtroUsuarioDefensor: false,
        filtroUsuarioSistema: false,
        filtroSoComDocumentos: false,
        filtroApenasPeticoes: false,
        colunhaOrdenacao: 'seq',
        direcaoOrdenacao: 'desc',

        // Download seletivo por documento (sincronizacao parcial) — ver
        // toggleSelecao/baixarDocumentos abaixo.
        selecionados: new Set(),
        baixando: false,

        // Modo seleção pra IA (ver "Gerar Minuta" — minuta_ia.js): quando
        // true, clicar num documento JÁ SINCRONIZADO seleciona em vez de
        // abrir (documento ainda não sincronizado continua indo pro fluxo
        // antigo de toggleSelecao/baixarDocumentos — não dá pra mandar pra
        // IA o que ainda não existe em disco). Default false — não muda em
        // nada o comportamento das outras duas telas que usam este
        // componente (aba Movimentações do PAJ, modal da Retornos).
        modoSelecao: false,
        selecionadosIA: new Map(), // _idDocumento(doc) -> {origem:'paj', caminho}

        // Atualizacao exclusiva das movimentacoes (icone de refresh no
        // cabecalho da tabela) — ver atualizarMovimentacoes() abaixo. O botao
        // "Atualizar movimentacoes" que roda isso (dentro do partial, abaixo
        // dos filtros) e' condicional NO TEMPLATE (Jinja `mostrar_botao_
        // atualizar_movs`, ver movimentacoes_tabela.html) — nao aqui no JS,
        // porque este mesmo componente e' reaproveitado em mais de um lugar
        // na MESMA pagina (aba Movimentacoes E o seletor de documentos do
        // "Gerar Minuta"), todos lendo o MESMO window.__movInicial via
        // bootstrap() — uma flag aqui vazaria pro seletor da minuta tambem.
        atualizando: false,

        // Minimenu do clipe de anexo (coluna Documentos, ver
        // movimentacoes_tabela.html) — mesmo padrão "menu único teleportado,
        // reposicionado por linha" de prazos.html/processo_judicial.html.
        // `menuAnexoSeq` guarda so' o seq (pra saber SE/QUAL linha esta com o
        // menu aberto — comparado no x-show do template); `menuAnexoMov`
        // guarda a movimentacao INTEIRA (as acoes do menu precisam do seq
        // pra chamar a API, mas comparar por objeto seria fragil apos um
        // refresh de `movs`).
        menuAnexoSeq: null,
        menuAnexoMov: null,
        posMenuAnexo: { top: 0, left: 0 },
        processandoAnexoSeq: false,

        // Hook de ciclo de vida do Alpine (chamado automaticamente ao
        // inicializar o componente, antes de x-init). O icone de atualizar
        // fica FORA deste componente (no cabecalho retratil do PAJ, no
        // titulo do modal da Retornos) — nao tem acesso direto a `atualizando`.
        // Retransmite toda mudanca via evento global pro icone externo
        // girar/desabilitar durante a sincronizacao (ver movimentacoes_tabela.html).
        init() {
            this.$watch('atualizando', (v) => {
                window.dispatchEvent(new CustomEvent('mov-tabela-atualizando', {
                    detail: { pajNorm: this.pajNorm, atualizando: v },
                }));
            });
        },

        bootstrap() {
            // Carga inicial (página do PAJ passa window.__movInicial).
            if (window.__movInicial) this.carregar(window.__movInicial);
        },

        carregar(cfg) {
            cfg = cfg || {};
            this.movs = cfg.movs || [];
            this.anexos = cfg.anexos || [];
            this.pajNorm = cfg.pajNorm || '';
            this.maxSeqAnterior = (cfg.maxSeq === undefined || cfg.maxSeq === null) ? -1 : cfg.maxSeq;
            this.__mapaArquivoLocal = null;  // invalida o cache do mapa de arquivos
        },

        get movimentacoes() { return this.movs || []; },

        // Cor de fundo da linha: verde p/ Atendimento de retorno, azul escuro p/
        // Petição inicial, azul claro p/ demais Fases "Petição ..." (ambas
        // prevalecem sobre o laranja de Defensor — pedido do usuário,
        // 2026-10-09: checadas ANTES dele), laranja p/ Defensor; nas demais,
        // listra alternada. Usa variáveis CSS (claro/escuro).
        //
        // SINCRONIA OBRIGATORIA (padrão de formatação uniforme do painel,
        // pedido do usuário 2026-10-09): esta classificação é espelhada em
        // `_categoria_mov()` de services/download_completo_service.py (usada
        // pra colorir as páginas separadoras do PDF "baixar tudo" do PAJ).
        // Qualquer mudança na CLASSIFICAÇÃO aqui (quais Fases/usuário caem em
        // qual categoria, e a ordem de prioridade) precisa ser replicada lá
        // também — as cores em si não precisam ser idênticas (lá são tons
        // PDF-apropriados fixos, aqui são variáveis CSS com variante de
        // tema), só a classificação.
        corLinhaMov(mov, idx) {
            const style = getComputedStyle(document.documentElement);
            const fases = (mov.fases || '').trim().toLowerCase();
            if (fases.includes('atendimento de retorno')) {
                const cor = style.getPropertyValue('--cor-retorno').trim();
                const bg = style.getPropertyValue('--cor-retorno-bg').trim();
                return `background-color: ${bg}; border-left: 4px solid ${cor};`;
            }
            if (fases.includes('petição inicial')) {
                const cor = style.getPropertyValue('--cor-peticao-inicial').trim();
                const bg = style.getPropertyValue('--cor-peticao-inicial-bg').trim();
                return `background-color: ${bg}; border-left: 4px solid ${cor};`;
            }
            if (fases.includes('petição')) {
                const cor = style.getPropertyValue('--cor-peticionamento').trim();
                const bg = style.getPropertyValue('--cor-peticionamento-bg').trim();
                return `background-color: ${bg}; border-left: 4px solid ${cor};`;
            }
            if (mov.fases && mov.usuario && (mov.usuario.includes('DEFENSOR') || mov.usuario.includes('(d)'))) {
                const cor = style.getPropertyValue('--cor-defensor').trim();
                const bg = style.getPropertyValue('--cor-defensor-bg').trim();
                return `background-color: ${bg}; border-left: 4px solid ${cor};`;
            }
            // color-mix com currentColor (não branco fixo) — se auto-ajusta ao tema:
            // escurece um pouco no claro (texto escuro) e clareia no escuro (texto
            // claro). Branco fixo era invisível no tema claro (fundo já quase branco).
            return idx % 2 === 0
                ? 'background-color: color-mix(in srgb, currentColor 3%, transparent);'
                : 'background-color: color-mix(in srgb, currentColor 7%, transparent);';
        },

        // Mapa {id_do_arquivo -> caminho local} dos anexos já baixados em Arquivos/
        // (nome: "seqNNN - ID - nome.ext"). Vazio quando não há anexos (ex.: Retornos).
        // ID aceita [A-Za-z0-9_-] (não só dígitos) — documentos sem link direto
        // (ver _idDocumento) usam o id do elemento sanitizado como ID, que tem
        // letras e underscore (ex: "detalhamentoForm_j_idt945_0_j_idt957_0_...").
        get _mapaArquivoLocalPorId() {
            if (!this.__mapaArquivoLocal) {
                const mapa = {};
                (this.anexos || []).forEach(a => {
                    const m = (a.nome || '').match(/^seq\d+\s*-\s*([A-Za-z0-9_-]+)\s*-/);
                    if (m) mapa[m[1]] = a.caminho;
                });
                this.__mapaArquivoLocal = mapa;
            }
            return this.__mapaArquivoLocal;
        },

        // Espelha ingestao/sisdpu_client.py::extrair_id_arquivo() — documentos
        // sem link de download direto (imagens via <p:commandLink> do
        // PrimeFaces) chegam como o marcador 'sisdpu-commandlink:<id>' em vez
        // de uma URL de verdade (ver extrair_documentos_movimentacoes.js).
        _idDocumento(doc) {
            const url = doc.urlDownload || '';
            if (url.startsWith('sisdpu-commandlink:')) {
                const bruto = url.slice('sisdpu-commandlink:'.length);
                return bruto ? bruto.replace(/[^A-Za-z0-9_-]/g, '_') : null;
            }
            const m = url.match(/\/(\d+)\/[^/]*$/);
            return m ? m[1] : null;
        },

        temArquivoLocal(doc) {
            const id = this._idDocumento(doc);
            return !!(id && this._mapaArquivoLocalPorId[id]);
        },

        // Caminho RELATIVO (dentro da pasta do PAJ, ex.: "Arquivos/xxx.pdf") —
        // extraído de linkDocumento() pra reaproveitar em toggleSelecaoIA
        // (que precisa do caminho puro, não da URL /files/... completa).
        _caminhoRelativoLocal(doc) {
            const id = this._idDocumento(doc);
            return id ? this._mapaArquivoLocalPorId[id] : null;
        },

        linkDocumento(doc) {
            const caminhoLocal = this._caminhoRelativoLocal(doc);
            return caminhoLocal ? `/files/${this.pajNorm}/${caminhoLocal}` : null;
        },

        // Preferencia de exibicao da coluna Documentos (Configurações > Geral
        // > Exibição de documentos) — "tipo" (badge com extensão, padrão) ou
        // "nome" (nome do arquivo truncado, um abaixo do outro). Por navegador.
        get mostrarNomeArquivo() {
            try { return localStorage.getItem('docsExibicao') === 'nome'; } catch (e) { return false; }
        },

        // Documentos da seq ainda nao baixados (candidatos a "Baixar todos").
        naoBaixados(mov) {
            return (mov.documentos || []).filter(d => !this.temArquivoLocal(d));
        },

        estaSelecionado(doc) {
            const id = this._idDocumento(doc);
            return !!(id && this.selecionados.has(id));
        },

        // Clique num badge ainda nao baixado alterna selecao (em vez de nao
        // fazer nada). Documento ja baixado ignora — o clique dele abre o
        // arquivo normalmente.
        toggleSelecao(doc) {
            if (this.temArquivoLocal(doc)) return;
            const id = this._idDocumento(doc);
            if (!id) return;
            if (this.selecionados.has(id)) this.selecionados.delete(id);
            else this.selecionados.add(id);
        },

        // Dentre os nao-baixados da seq, quantos/quais estao selecionados —
        // usado pelo botao "Baixar selecionados".
        selecionadosNaSeq(mov) {
            return this.naoBaixados(mov).filter(d => this.estaSelecionado(d));
        },

        // --- Seleção pra IA (modoSelecao — ver comentário de selecionadosIA
        // acima). Só aceita documento JÁ sincronizado; documento pendente
        // não pode ser selecionado aqui (não existe em disco pra enviar). ---
        toggleSelecaoIA(doc) {
            if (!this.temArquivoLocal(doc)) return;
            const id = this._idDocumento(doc);
            if (!id) return;
            if (this.selecionadosIA.has(id)) this.selecionadosIA.delete(id);
            else this.selecionadosIA.set(id, { origem: 'paj', caminho: this._caminhoRelativoLocal(doc) });
        },
        estaSelecionadoIA(doc) {
            const id = this._idDocumento(doc);
            return !!(id && this.selecionadosIA.has(id));
        },

        // Baixa uma lista de documentos (sincronizacao parcial — so os IDs
        // pedidos, sem ressincronizar o PAJ). `docs` vem de naoBaixados(mov)
        // ou selecionadosNaSeq(mov). Ao concluir, atualiza so os badges
        // (refaz o mapa de arquivo local), sem recarregar a pagina.
        async baixarDocumentos(docs) {
            if (!docs || !docs.length || this.baixando) return;
            const ids = docs.map(d => this._idDocumento(d)).filter(Boolean);
            if (!ids.length) return;
            this.baixando = true;
            try {
                const novosAnexos = await baixarDocumentosPaj(this.pajNorm, ids);
                if (novosAnexos) {
                    this.anexos = novosAnexos;
                    this.__mapaArquivoLocal = null;
                }
                ids.forEach(id => this.selecionados.delete(id));
            } finally {
                this.baixando = false;
            }
        },

        // Sincronizacao EXCLUSIVA e INCREMENTAL das movimentacoes deste PAJ
        // com o SISDPU — releitura completa da tabela, mas so' extrai
        // documentos das movimentacoes que ainda nao eram conhecidas (custo
        // proporcional so' ao que mudou desde a ultima sync); sem baixar
        // PDF, sem metadata extra. Ao concluir, substitui movs/anexos no
        // lugar (sem recarregar a pagina) — funciona tanto na aba do PAJ
        // quanto no modal da Retornos, pois os dois usam este mesmo
        // componente.
        async atualizarMovimentacoes() {
            if (this.atualizando || !this.pajNorm) return;
            this.atualizando = true;
            try {
                const dados = await atualizarMovimentacoesPaj(this.pajNorm);
                if (dados) {
                    this.movs = dados.movimentacoes || [];
                    this.anexos = dados.anexos_sisdpu || [];
                    this.__mapaArquivoLocal = null;
                }
            } finally {
                this.atualizando = false;
            }
        },

        // Abre/fecha o minimenu do clipe de anexo pra `mov` (chamado pelo
        // @click do botão-clipe, ver movimentacoes_tabela.html). Reposiciona
        // sempre no `$el` do botão clicado — mesmo padrão de posMenu em
        // prazos.html/processo_judicial.html.
        abrirMenuAnexoSeq(mov, ev) {
            const el = (ev && (ev.currentTarget || ev.target)) || null;
            if (this.menuAnexoSeq === mov.seq) {
                this.menuAnexoSeq = null;
                return;
            }
            this.menuAnexoMov = mov;
            this.menuAnexoSeq = mov.seq;
            if (el) {
                const rect = el.getBoundingClientRect();
                this.posMenuAnexo = { top: rect.bottom, left: rect.left };
            }
        },

        // "Obter relação de documentos" do minimenu — só descobre os
        // documentos daquele seq (sem baixar nada). Ao concluir, o seq
        // pedido já vem com `documentos` preenchido na releitura de
        // movimentações — substitui `movs`/`anexos` no lugar, sem recarregar
        // a página (mesmo padrão de atualizarMovimentacoes() acima).
        async obterRelacaoDocumentosSeq(mov) {
            if (!mov || !this.pajNorm || this.processandoAnexoSeq) return;
            this.processandoAnexoSeq = true;
            try {
                const dados = await obterDocumentosSeqPaj(this.pajNorm, mov.seq);
                if (dados) {
                    this.movs = dados.movimentacoes || [];
                    this.anexos = dados.anexos_sisdpu || [];
                    this.__mapaArquivoLocal = null;
                }
            } finally {
                this.processandoAnexoSeq = false;
            }
        },

        // "Baixar documentos" do minimenu — um clique só: obtém a relação
        // (se ainda não conhecida) e já baixa tudo daquele seq.
        async baixarDocumentosSeq(mov) {
            if (!mov || !this.pajNorm || this.processandoAnexoSeq) return;
            this.processandoAnexoSeq = true;
            try {
                const dados = await baixarDocumentosSeqPaj(this.pajNorm, mov.seq);
                if (dados) {
                    this.movs = dados.movimentacoes || [];
                    this.anexos = dados.anexos_sisdpu || [];
                    this.__mapaArquivoLocal = null;
                }
            } finally {
                this.processandoAnexoSeq = false;
            }
        },

        extDocumento(nomeArquivo) {
            const ext = (nomeArquivo || '').split('.').pop();
            return (ext && ext.length <= 5 ? ext : 'doc').toUpperCase();
        },

        corBadgeDocumento(nomeArquivo) {
            const ext = this.extDocumento(nomeArquivo).toLowerCase();
            if (ext === 'pdf') return 'badge-error';
            if (['doc', 'docx'].includes(ext)) return 'badge-info';
            if (['jpg', 'jpeg', 'png', 'gif'].includes(ext)) return 'badge-warning';
            return 'badge-ghost';
        },

        get fases_unicas() {
            const fases = new Set();
            this.movimentacoes.forEach(mov => {
                if (mov.fases && mov.fases.trim()) fases.add(mov.fases.trim());
            });
            return Array.from(fases).sort();
        },

        get movimentacoesFiltradas() {
            let resultado = this.movimentacoes;

            if (this.filtroFases) {
                resultado = resultado.filter(mov => mov.fases && mov.fases.includes(this.filtroFases));
            }
            if (this.filtroSoComDocumentos) {
                resultado = resultado.filter(mov => mov.documentos && mov.documentos.length > 0);
            }
            if (this.filtroApenasPeticoes) {
                // Mesmo critério de "Petição ..." usado em corLinhaMov() (ver
                // nota de sincronia ali) — cobre Petição inicial e as demais
                // Fases de petição num filtro só.
                resultado = resultado.filter(mov => mov.fases && mov.fases.trim().toLowerCase().includes('petição'));
            }
            if (!this.filtroUsuarioTodos) {
                resultado = resultado.filter(mov => {
                    if (!mov.usuario) return false;
                    const regexServidor = /servidor|terceirizado|estagiário|SERVIDOR|TERCEIRIZADO|ESTAGIÁRIO/i;
                    const regexDefensor = /defensor|Defensor/i;
                    const regexSistema = /sisdpu|sistema|SISDPU|SISTEMA/i;
                    const isServidor = regexServidor.test(mov.usuario);
                    const isDefensor = regexDefensor.test(mov.usuario);
                    const isSistema = regexSistema.test(mov.usuario);
                    if (this.filtroUsuarioServidor && isServidor) return true;
                    if (this.filtroUsuarioDefensor && isDefensor) return true;
                    if (this.filtroUsuarioSistema && isSistema) return true;
                    return false;
                });
            }

            resultado = [...resultado].sort((a, b) => {
                let valA = a[this.colunhaOrdenacao] || '';
                let valB = b[this.colunhaOrdenacao] || '';
                if (this.colunhaOrdenacao === 'seq') {
                    valA = parseInt(valA) || 0;
                    valB = parseInt(valB) || 0;
                }
                if (this.colunhaOrdenacao === 'data') {
                    valA = new Date(valA).getTime() || 0;
                    valB = new Date(valB).getTime() || 0;
                }
                if (valA === valB) return 0;
                const cmp = valA < valB ? -1 : 1;
                return this.direcaoOrdenacao === 'asc' ? cmp : -cmp;
            });

            return resultado;
        },

        ordenarPor(coluna) {
            if (this.colunhaOrdenacao === coluna) {
                this.direcaoOrdenacao = this.direcaoOrdenacao === 'asc' ? 'desc' : 'asc';
            } else {
                this.colunhaOrdenacao = coluna;
                this.direcaoOrdenacao = 'asc';
            }
        },

        selecionarTodos() {
            if (this.filtroUsuarioTodos) {
                this.filtroUsuarioServidor = false;
                this.filtroUsuarioDefensor = false;
                this.filtroUsuarioSistema = false;
            }
        },

        selecionarIndividual() {
            if (this.filtroUsuarioServidor || this.filtroUsuarioDefensor || this.filtroUsuarioSistema) {
                this.filtroUsuarioTodos = false;
            } else {
                this.filtroUsuarioTodos = true;
            }
        },
    };
}
