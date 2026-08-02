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
 *     links_decisao[], anexo, descricao_retorno, documentos[] }
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
        colunhaOrdenacao: 'seq',
        direcaoOrdenacao: 'desc',

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

        // Cor de fundo da linha: verde p/ Atendimento de retorno, laranja p/
        // Defensor; nas demais, listra alternada. Usa variáveis CSS (claro/escuro).
        corLinhaMov(mov, idx) {
            const style = getComputedStyle(document.documentElement);
            if (mov.fases && mov.fases.trim().toLowerCase().includes('atendimento de retorno')) {
                const cor = style.getPropertyValue('--cor-retorno').trim();
                const bg = style.getPropertyValue('--cor-retorno-bg').trim();
                return `background-color: ${bg}; border-left: 4px solid ${cor};`;
            }
            if (mov.fases && mov.usuario && (mov.usuario.includes('DEFENSOR') || mov.usuario.includes('(d)'))) {
                const cor = style.getPropertyValue('--cor-defensor').trim();
                const bg = style.getPropertyValue('--cor-defensor-bg').trim();
                return `background-color: ${bg}; border-left: 4px solid ${cor};`;
            }
            return idx % 2 === 0
                ? 'background-color: rgba(255, 255, 255, 0.025);'
                : 'background-color: rgba(255, 255, 255, 0.055);';
        },

        // Mapa {id_do_arquivo -> caminho local} dos anexos já baixados em Arquivos/
        // (nome: "seqNNN - ID - nome.ext"). Vazio quando não há anexos (ex.: Retornos).
        get _mapaArquivoLocalPorId() {
            if (!this.__mapaArquivoLocal) {
                const mapa = {};
                (this.anexos || []).forEach(a => {
                    const m = (a.nome || '').match(/^seq\d+\s*-\s*(\d+)\s*-/);
                    if (m) mapa[m[1]] = a.caminho;
                });
                this.__mapaArquivoLocal = mapa;
            }
            return this.__mapaArquivoLocal;
        },

        _idDocumento(doc) {
            const m = (doc.urlDownload || '').match(/\/(\d+)\/[^/]*$/);
            return m ? m[1] : null;
        },

        temArquivoLocal(doc) {
            const id = this._idDocumento(doc);
            return !!(id && this._mapaArquivoLocalPorId[id]);
        },

        linkDocumento(doc) {
            const id = this._idDocumento(doc);
            const caminhoLocal = id ? this._mapaArquivoLocalPorId[id] : null;
            return caminhoLocal ? `/files/${this.pajNorm}/${caminhoLocal}` : null;
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
