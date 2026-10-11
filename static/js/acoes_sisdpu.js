// Módulo Alpine reutilizável "Ações no SISDPU" (movimentar → tramitar → concluir).
// Autocontido — não depende de variáveis do escopo pai (padrão do tabelaMovimentacoes()
// em movimentacoes.js). Uso:
//   <div x-data="acoesSisdpuBlock({ idProcesso: r.id_processo, paj: r.paj,
//                                   idTramite: r.id_tramite, getDescricaoMov: () => r._sug })">
//     {% include "partials/acoes_sisdpu.html" %}
//   </div>
//
// cfg:
//   idProcesso      (obrigatório) — id do processo no SISDPU.
//   paj             (opcional) — rótulo do PAJ para mensagens/confirmação.
//   pajNorm         (opcional) — para carregar os "Anexos SISDPU" já baixados do PAJ
//                   (tabela de seleção de documentos a incluir na movimentação — mesma
//                   fonte/rota usada pela aba do PAJ e por acoes_eproc.js).
//   idTramite       (opcional) — id do "trâmite de trabalho" (o que originou esta
//                   tela — ex.: o Atendimento de Retorno) a concluir. Junto de
//                   `concluir` (toggle Sim/Não), é o modelo LEGADO de conclusão —
//                   um único trâmite. Ignorado para fins de conclusão quando
//                   `tramitacoesCaixa` (abaixo) é passado.
//   tramitacoesCaixa (opcional, array) — NOVO modelo: TODAS as tramitações hoje
//                   abertas na caixa de entrada deste PAJ ({id_tramite, descricao,
//                   remetente, data_envio, ...} — mesma fonte de
//                   window._paj_tramitacoes_caixa/meta.tramitacoes_caixa). Quando
//                   informado (mesmo que vazio `[]`), o bloco "Concluir trâmite"
//                   troca o toggle Sim/Não por uma lista de checkboxes (0, 1 ou N
//                   selecionáveis) e mostra "não há trâmite aberto" se vier vazia.
//                   `idTramite`, se bater com um item da lista, vem pré-marcado e
//                   rotulado "trâmite de trabalho" (mesmo padrão do modal
//                   "Concluir PAJ da Caixa de Entrada" em app.js). Movimentar/
//                   tramitar/concluir são AÇÕES INDEPENDENTES — não haver nenhum
//                   trâmite pra concluir NUNCA desabilita o restante do console.
//   getDescricaoMov (opcional) — função síncrona que devolve o texto do despacho da
//                   MOVIMENTAÇÃO no momento de Executar, e faz o módulo NÃO desenhar
//                   editor próprio (evita duplicar campo com a fonte do chamador) — uso
//                   raro hoje; prefira getValorInicialMov (abaixo) na maioria dos casos.
//                   Sem ele, o módulo mostra seu próprio editor de texto RICO (Quill, via
//                   static/js/rich_editor.js — negrito/itálico/sublinhado, cor, citação,
//                   listas, recuo, alinhamento) e manda o HTML pro CKEditor real do
//                   despacho no SISDPU (descricao_mov_html=1). Cai pra uma textarea
//                   simples se o Quill não carregar (CDN fora do ar). Aceita também
//                   `getDescricao` (nome legado) como alias.
//   getValorInicialMov (opcional) — função síncrona que devolve um texto PURO (não
//                   HTML) pra semear o editor rico da movimentação na PRIMEIRA vez que o
//                   painel é aberto (ex.: a Sugestão IA da Retornos, sem alterá-la nem
//                   duplicá-la — ela continua existindo/editável/salvável como antes).
//                   Depois da carga inicial, o texto do Quill segue independente — pra
//                   trazer uma atualização da fonte (ex.: após reanalisar a Sugestão IA)
//                   é preciso clicar em "Atualizar da Sugestão IA" (nunca automático, pra
//                   não apagar formatação que o usuário já tenha feito sem avisar).
//   getDescricaoTram (opcional) — mesma ideia, para o despacho da TRAMITAÇÃO — mas
//                   SEMPRE texto puro (o campo de tramitação no SISDPU não é CKEditor,
//                   é um input de texto normal; HTML lá apareceria como tags literais).
//                   Nenhuma página tem hoje uma fonte de sugestão para tramitação, então
//                   na prática o módulo sempre desenha sua própria textarea (`descTram`).
//   faseInicial / grupoInicial (opcional) — pré-seleção antes de carregar as prefs.
//   aberto          (opcional) — estado inicial da seção retrátil (default: false).
//   onExecutado     (opcional) — callback(resultado) chamado após a execução (SSE "done").

// Catálogo real do "Tipo de Arquivo" do SISDPU (dropdown do popup de anexo da
// movimentação) — confirmado pelo usuário na tela do SISDPU, não inventado. "Não
// informado" é sempre o padrão de cada anexo; o usuário troca se quiser.
const TIPOS_ARQUIVO_ANEXO = [
    'Não informado', 'Ofício', 'Correspondência', 'Autos Processuais',
    'Documentos do Assistido', 'Intimações', 'Decisões Judiciais',
    'Despachos do Defensor', 'Parecer', 'Petições',
];

function acoesSisdpuBlock(cfg) {
    cfg = cfg || {};
    const getDescricaoMovFn = typeof cfg.getDescricaoMov === 'function' ? cfg.getDescricaoMov
                             : (typeof cfg.getDescricao === 'function' ? cfg.getDescricao : null);
    const getDescricaoTramFn = typeof cfg.getDescricaoTram === 'function' ? cfg.getDescricaoTram : null;
    const getValorInicialMovFn = typeof cfg.getValorInicialMov === 'function' ? cfg.getValorInicialMov : null;
    // NOVO modelo de conclusão (lista) — null quando o chamador não passa
    // `tramitacoesCaixa` (ex.: Retornos), mantendo o modelo LEGADO (idTramite +
    // toggle Sim/Não) intacto pra quem não se beneficia da seleção múltipla.
    // `modoListaTramitacoes`: true quando o chamador OPTOU pelo modelo novo —
    // a CHAVE `tramitacoesCaixa` está presente no cfg, mesmo que o valor seja
    // `null` (PAJ cujo campo nunca foi calculado — ver nota em paj_detail.html).
    // Diferente de checar só `tramitacoesCaixa !== null`: sem esta distinção,
    // um `null` de "nunca calculado" (paj_detail.html) e um `null` de "esta
    // página nem usa isso" (retornos.html, que nunca passa a chave) ficariam
    // indistinguíveis — e o refresh automático em init() (abaixo) dispararia
    // à toa pra CADA linha da Retornos (que tem pajNorm mas não deve reconferir
    // ao vivo — aquele trâmite é próprio, fora de tramitacoes_caixa).
    const modoListaTramitacoes = "tramitacoesCaixa" in cfg;
    const tramitacoesCaixa = Array.isArray(cfg.tramitacoesCaixa) ? cfg.tramitacoesCaixa : null;
    // Pré-marca o "trâmite de trabalho" (idTramite), se ele estiver entre as
    // tramitações abertas — mesmo padrão do modal "Concluir PAJ da Caixa de
    // Entrada" (concluirPajModalApp em app.js). O usuário pode desmarcar/marcar
    // outros livremente antes de Executar.
    const idsTramiteSelecionadosInit = {};
    if (tramitacoesCaixa) {
        tramitacoesCaixa.forEach((t) => {
            idsTramiteSelecionadosInit[t.id_tramite] = (t.id_tramite === (cfg.idTramite || ''));
        });
    }
    return {
        aberto: !!cfg.aberto,
        idProcesso: cfg.idProcesso || '',
        paj: cfg.paj || '',
        pajNorm: cfg.pajNorm || '',
        idTramite: cfg.idTramite || '',
        tramitacoesCaixa,
        idsTramiteSelecionados: idsTramiteSelecionadosInit,
        _refrescandoTramitacoes: false,
        getDescricaoMov: getDescricaoMovFn,
        getDescricaoTram: getDescricaoTramFn,
        getValorInicialMov: getValorInicialMovFn,
        onExecutado: typeof cfg.onExecutado === 'function' ? cfg.onExecutado : () => {},
        // (opcional) URL pra navegar em vez de recarregar a página ATUAL após
        // execução bem-sucedida (ver reload no fim de executar()/executarFila())
        // — usado por movimentar_conjunto.html pra abrir a página do PAJ já
        // com as movimentações atualizadas, em vez de ficar na própria tela
        // combinada (pedido do usuário 2026-09-14). Vazio (padrão) preserva o
        // comportamento de sempre em todo outro lugar que usa este módulo.
        aposExecutarIrPara: cfg.aposExecutarIrPara || '',

        fasesFav: [], fasesFreq: [], fasesTodas: [],
        caixasFav: [], caixasFreq: [], caixasTodas: [],
        fase: cfg.faseInicial || '', grupo: cfg.grupoInicial || '',
        descMov: '', descTram: '',
        // Editor rico da MOVIMENTAÇÃO (Quill) — id único por instância (várias linhas
        // da Retornos montam este bloco em paralelo, cada uma precisa do seu próprio
        // elemento). `quillMovOk` começa otimista (true): o container fica visível
        // desde o início pra o Quill medir certo ao montar; só vira false se
        // criarEditorRico() de fato falhar (Quill não carregou), revelando a textarea
        // simples como fallback.
        uidMov: 'qmov-' + Math.random().toString(36).slice(2, 9),
        _editorMov: null, quillMovOk: true, _montandoEditorMov: false,
        // "Usar modelo" — despachos padrão cadastrados em Configurações › SISDPU
        // (texto padrão + placeholders [NOME]/[CPF]/[PROCESSO]/[PAJ]/[DATA]),
        // carregados junto das demais preferências (ver init() abaixo). Mesmo
        // mecanismo de "Modelos de petição" em Ações no Eproc — ver
        // usarModeloDespacho() mais abaixo e os helpers compartilhados em
        // static/js/rich_editor.js (dataPorExtenso/paraHtmlSeTextoPuro/
        // resolverPlaceholders). [PROCESSO]: este módulo não tem um seletor de
        // "processo judicial ativo" como o "processoSelecionado" de Ações no
        // Eproc (aqui o processo, se houver, vem resolvido pelo backend a
        // partir do PAJ — só quando há exatamente 1 vinculado; com 0 ou 2+,
        // fica vazio de propósito, sem escolher um arbitrariamente — pedido
        // do usuário 2026-09-16).
        modelosDespacho: [], usandoModeloDespacho: false, erroUsarModeloDespacho: '',
        // "Gerar Síntese do PAJ" — botão ao lado de "Usar modelo": dispara
        // análise por IA (PROMPT_MAX do PAJ) e insere o resultado no cursor do
        // editor da movimentação. Só existe quando há PAJ local (pajNorm).
        gerandoSinteseIA: false, erroSinteseIA: '',
        prazo: 0, prazoData: '', concluir: true, exec: false,

        // Anexos da MOVIMENTAÇÃO — lista staged única, misturando origem 'externo'
        // (upload do computador do usuário — o caso comum) e 'paj' (documento já
        // baixado do SISDPU — raro, reanexar algo que já está lá). Cada item:
        // {origem, ref, nome, tamanho, tipo, descricao, tipoArquivo}. `descricao` nasce
        // igual ao nome do arquivo (editável); `tipoArquivo` nasce "Não informado"
        // (editável) — catálogo real do SISDPU, ver TIPOS_ARQUIVO_ANEXO abaixo.
        // `docsPaj` é só o catálogo pro MODAL de seleção "Adicionar do SISDPU" — mesma
        // fonte/rota/helpers/rótulo do "Ações no Eproc".
        anexosStaged: [], enviandoAnexo: false,
        tiposArquivoAnexo: TIPOS_ARQUIVO_ANEXO,  // exposto p/ o <select> no HTML (x-for)
        docsPaj: [], docsCarregados: false,
        modalPajAberto: false, modalPajSelecionados: {},
        // "Adicionar Petições IA" — mesma tabela/modal de "Adicionar do SISDPU"
        // acima, mas listando a subpasta "Peças IA" do PAJ (petições/peças geradas
        // pela análise de pretensão) em vez de `Arquivos/` — mesmo padrão de
        // "Ações no Eproc" (acoes_eproc.js/carregarPecasIa).
        pecasIa: [], pecasIaCarregadas: false,
        modalPecasIaAberto: false, modalPecasIaSelecionados: {},

        sseAberto: false, sseLog: [], sseStatus: '', sseTitulo: '',

        // Rascunho ("Gravar" p/ executar mais tarde) — mesmo padrão do "Ações no
        // Eproc": 1 por PAJ, auto-restaurado ao abrir. Também gravado
        // AUTOMATICAMENTE por "Ações no Eproc" depois de um "Peticionar" real
        // confirmado (ver _receberPeticaoEproc) — o usuário não precisa estar
        // com este console aberto no momento pra não perder o registro.
        rascunhoCarregado: false, rascunhoSalvoEm: '', gravando: false, avisoGravacao: '',
        _rascunhoPromise: null,
        _descMovHtmlRascunho: '',

        // Fila de movimentações sequenciais (2026-09-11) — permite montar VÁRIAS
        // movimentações (cada uma com sua própria fase/despacho/tramitação/conclusão/
        // anexos) pro MESMO PAJ e executá-las em sequência, na mesma sessão do
        // SISDPU. Cada item: {fase, descricao_mov, descricao_mov_html (bool),
        // grupo, descricao_tram, prazo, prazo_data, id_tramites (lista), anexos
        // (shape completo de anexosStaged, pra reeditar com fidelidade)}.
        // ADITIVO: quando `fila` está vazia, tudo funciona EXATAMENTE como antes —
        // "Executar" roda o formulário atual diretamente (nenhuma mudança pra quem
        // nunca usa fila, ex.: paj_detail.html isolado, retornos.html).
        // `editandoFilaIndice`: != null quando "Editar" carregou um item de volta
        // no formulário — "Adicionar à fila" vira "Salvar alteração" (atualiza no
        // lugar em vez de acrescentar).
        fila: [], editandoFilaIndice: null,

        async init() {
            try {
                const j = await (await fetch('/api/acoes-sisdpu/preferencias')).json();
                this.fasesFav = j.fases_favoritas || [];
                this.fasesFreq = j.fases_frequentes || [];
                this.fasesTodas = j.fases_todas || [];
                this.caixasFav = j.caixas_favoritas || [];
                this.caixasFreq = j.caixas_frequentes || [];
                this.caixasTodas = j.caixas_todas || [];
                if (!this.fase) this.fase = j.fase_padrao || '';
                if (!this.grupo) this.grupo = j.caixa_padrao || '';
                this.modelosDespacho = j.modelos_despacho || [];
            } catch (e) {}
            await this.carregarDocsPaj();
            // Só quando o chamador optou pelo modelo lista (`modoListaTramitacoes`) E o
            // valor ainda não foi calculado (`null` — PAJ sincronizado só por caminho
            // avulso, nunca passou pela sync COMPLETA da caixa, que é quem preenche
            // isto) — reconfere ao vivo, mesmo mecanismo já usado por
            // concluirPajModalApp (app.js) antes de abrir o modal "Concluir PAJ da
            // Caixa de Entrada". SEM esta checagem, `null` era silenciosamente tratado
            // como "zero trâmites" (ver nota em paj_detail.html), mostrando "Não há
            // trâmite aberto" com falsa certeza — bug real 2026-09-24 (PAJ
            // 2026/029-03759). Guarda `_refrescandoTramitacoes` evita disparar 2x
            // (Alpine chama init() duas vezes por instância).
            if (modoListaTramitacoes && this.tramitacoesCaixa === null && this.pajNorm && !this._refrescandoTramitacoes) {
                this._refrescandoTramitacoes = true;
                await this._refrescarTramitacoesCaixa();
            }
            // Antes de montar o editor — precisa do HTML salvo (se houver) pra passar
            // como valorInicial já na criação do Quill (mesmo padrão do "Ações no Eproc").
            // SEMPRE chama (sem gate por `rascunhoCarregado` aqui) — carregarRascunho()
            // agora cacheia sua PRÓPRIA promise (`_rascunhoPromise`), então a 2ª chamada
            // concorrente (Alpine chama init() DUAS VEZES por instância — ver nota em
            // _montarEditorMov) reaproveita e ESPERA a mesma promise em vez de só ver um
            // flag booleano já "true" e seguir em frente sem os dados terem chegado —
            // era exatamente esse gap (2ª chamada de init() pulava o await porque
            // `rascunhoCarregado` já estava true, setado pela 1ª ANTES do fetch dela
            // terminar) que causava _montarEditorMov() ler `_descMovHtmlRascunho` ainda
            // vazio e montar o Quill sem o texto salvo — bug real 2026-09-11
            // (Movimentações Pendentes: descrição da movimentação sumia ao reabrir,
            // só a da tramitação — que não depende do Quill — sobrevivia).
            await this.carregarRascunho();
            // "Gravar movimentação para o SISDPU" no resumo final de Ações no Eproc
            // recarrega a página de propósito (ver acoes_eproc.js:gravarMovimentacaoSisdpuNoResumo)
            // e usa sessionStorage (sobrevive ao reload, ao contrário de uma variável
            // Alpine) pra avisar "abra e mostre o que acabou de ser gravado" — pedido
            // do usuário 2026-08-20: sem isso, o usuário tinha que reabrir o console
            // manualmente depois do refresh pra conferir/executar.
            let vindoDoEproc = false;
            try {
                if (sessionStorage.getItem('painel_abrir_acoes_sisdpu') === this.pajNorm) {
                    sessionStorage.removeItem('painel_abrir_acoes_sisdpu');
                    vindoDoEproc = true;
                    this.aberto = true;
                }
            } catch (e) {}
            // O Quill só é montado quando o painel está de fato ABERTO (senão o
            // container fica dentro de um ancestral display:none — o x-show="aberto"
            // do painel inteiro — e o Quill mede a largura errado ao montar). Ver
            // abrir() abaixo, que faz a mesma chamada quando o usuário expande depois.
            if (this.aberto) await this._montarEditorMov();
            // Rola até o console já aberto — ele fica abaixo de várias outras seções
            // na página do PAJ, o usuário não veria que abriu sem isso.
            if (vindoDoEproc) {
                await this.$nextTick();
                this.$el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
            // "Ações no Eproc" (sibling na página do PAJ) dispara este evento depois
            // de um "Peticionar" real confirmado, perguntando ao usuário se quer
            // registrar o mesmo ato aqui — ver acoes_eproc.js:_prepararMovimentacaoSisdpu.
            // GUARD: Alpine chama init() DUAS VEZES por instância (x-init + auto-init —
            // ver nota em _montarEditorMov). Sem esta trava, o listener era registrado
            // 2x, e as duas execuções concorrentes de _receberPeticaoEproc podiam montar
            // o Quill com o valor ANTIGO de _descMovHtmlRascunho antes do carregarRascunho()
            // (buscando o valor certo) terminar — bug real 2026-08-13: a descrição não
            // aparecia na movimentação após "Peticionar", só os anexos.
            if (!this._listenerEprocLigado) {
                this._listenerEprocLigado = true;
                window.addEventListener('eproc-peticionado', (ev) => this._receberPeticaoEproc(ev.detail || {}));
            }
        },
        // Carrega o rascunho salvo do PAJ (se houver) — fase, despachos, anexos,
        // grupo/prazo. Silencioso se não houver rascunho ou a chamada falhar. Mesmo
        // padrão de "Ações no Eproc". Cacheia a PRÓPRIA promise em `_rascunhoPromise`
        // (não só um flag booleano): chamadas concorrentes (Alpine roda init() DUAS
        // VEZES por instância) devolvem a MESMA promise em vez de disparar um 2º
        // fetch e/ou seguir adiante achando que já terminou — sem isso, a 2ª chamada
        // podia ler `_descMovHtmlRascunho` ainda vazio (fetch da 1ª ainda em voo) e
        // `_montarEditorMov()` montava o Quill sem o texto salvo (bug real
        // 2026-09-11, ver nota em init()). `forcar=true` (usado por
        // _receberPeticaoEproc) descarta qualquer promise em cache e busca de novo.
        async carregarRascunho(forcar) {
            if (forcar) this._rascunhoPromise = null;
            if (this._rascunhoPromise) return this._rascunhoPromise;
            this.rascunhoCarregado = true;
            this._rascunhoPromise = (async () => {
                if (!this.pajNorm) return;
                try {
                    const j = await (await fetch('/api/acoes-sisdpu/rascunho/' + encodeURIComponent(this.pajNorm))).json();
                    const r = j.rascunho;
                    if (!r) return;
                    if (r.fase) this.fase = r.fase;
                    if (r.grupo) this.grupo = r.grupo;
                    if (r.prazo) this.prazo = r.prazo;
                    if (r.prazo_data) this.prazoData = r.prazo_data;
                    if (typeof r.concluir === 'boolean') this.concluir = r.concluir;
                    this._descMovHtmlRascunho = r.descricao_mov_html || '';
                    if (!this.getDescricaoTram) this.descTram = r.descricao_tram || this.descTram;
                    this.anexosStaged = r.anexos || [];
                    this.fila = r.fila || [];
                    this.rascunhoSalvoEm = r.salvo_em || '';
                } catch (e) {}
            })();
            return this._rascunhoPromise;
        },
        // "Gravar" — persiste o estado atual do console pra executar mais tarde
        // (ver botão ao lado de "Executar no SISDPU"). Mesmo padrão de "Ações no
        // Eproc": o HTML da movimentação (Quill) é salvo como está, sem reformatar.
        async gravarRascunho(validarDescricao) {
            if (validarDescricao && this._bloqueiaDescricaoCurta()) return;
            this.gravando = true;
            try {
                const resp = await fetch('/api/acoes-sisdpu/rascunho/' + encodeURIComponent(this.pajNorm), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        paj_label: this.paj,
                        fase: this.fase, grupo: this.grupo,
                        prazo: this.prazo, prazo_data: this.prazoData, concluir: this.concluir,
                        descricao_mov_html: this.getDescricaoMov ? '' : this.descMovEfetiva(),
                        descricao_tram: this.getDescricaoTram ? '' : this.descTramEfetiva(),
                        anexos: this.anexosStaged,
                        fila: this.fila,
                    }),
                });
                const j = await resp.json();
                if (!j.ok) { alert('Falha ao gravar rascunho: ' + (j.erro || 'erro desconhecido')); return; }
                this.rascunhoSalvoEm = j.salvo_em;
            } catch (e) {
                alert('Erro ao gravar rascunho: ' + e);
            } finally {
                this.gravando = false;
            }
        },
        // "Ações no Eproc" (sibling na página) já GRAVOU o rascunho no disco (fase +
        // descrição + a petição pré-anexada) depois de um "Peticionar" real
        // confirmado — este evento é só o aviso pra reagir AO VIVO se este console
        // já estiver montado: relê do disco (fonte única de verdade, sem duplicar o
        // payload no próprio evento) e só DEPOIS abre/atualiza o editor.
        // ORDEM IMPORTA: `carregarRascunho()` roda ANTES de abrir()/montar o Quill —
        // assim `_descMovHtmlRascunho` já está com o valor CERTO quando o editor for
        // criado (via opts.valorInicial em _montarEditorMov), em vez de montar vazio
        // e torcer pra corrigir depois com setHTML() (dependia de timing — bug real
        // 2026-08-13, descrição não aparecia, só os anexos). Se o painel já estava
        // aberto (editor já existe), ainda assim empurra o HTML pra dentro dele.
        async _receberPeticaoEproc(d) {
            if (!d.pajNorm || d.pajNorm !== this.pajNorm) return;
            await this.carregarRascunho(true);
            if (!this.aberto) { await this.abrir(); return; }
            if (this._editorMov) this._editorMov.setHTML(this._descMovHtmlRascunho || '');
        },
        // Alterna o painel e, na primeira vez que abre, monta o Quill da movimentação
        // (mesmo padrão "carrega só quando abre" do módulo "Ações no Eproc").
        async abrir() {
            this.aberto = !this.aberto;
            if (this.aberto) await this._montarEditorMov();
        },
        async _montarEditorMov() {
            // init() pode ser chamado DUAS VEZES por instância (x-init="init()" no HTML
            // + o auto-init do próprio Alpine pra métodos chamados "init" — redundância
            // antiga do projeto, inofensiva pros outros fetches idempotentes) — e como
            // esta função tem um `await` antes de criar o Quill, as duas chamadas podem
            // passar pela guarda `!this._editorMov` ANTES de qualquer uma delas montar
            // (nenhuma ainda setou `_editorMov`), duplicando a toolbar. Por isso a
            // trava (`_montandoEditorMov`) é setada de forma SÍNCRONA, antes do
            // primeiro `await` — a segunda chamada, seja qual for a ordem real de
            // execução, sempre vê a trava já ligada e desiste.
            if (this.getDescricaoMov || this._editorMov || this._montandoEditorMov) return;
            this._montandoEditorMov = true;
            await this.$nextTick();
            const opts = { placeholder: 'Descrição da movimentação (opcional)' };
            // Rascunho salvo (HTML) tem prioridade sobre a fonte externa (Sugestão
            // IA etc.) — é uma escolha explícita mais recente (Gravar, ou o que
            // "Ações no Eproc" gravou automaticamente).
            if (this._descMovHtmlRascunho) opts.valorInicial = this._descMovHtmlRascunho;
            else if (this.getValorInicialMov) opts.valorInicialTexto = this.getValorInicialMov() || '';
            this._editorMov = criarEditorRico(this.uidMov, opts);
            this.quillMovOk = !!this._editorMov;
        },
        // "Atualizar da Sugestão IA" (ou outra fonte de valor inicial) — busca sob
        // demanda, nunca automático: reanalisar a Sugestão IA depois de já ter aberto
        // e formatado o texto no Quill NÃO sobrescreve nada sem o usuário clicar aqui.
        atualizarMovDaFonte() {
            if (this._editorMov && this.getValorInicialMov) this._editorMov.setText(this.getValorInicialMov() || '');
        },
        // "Usar modelo" (despachos padrão, Configurações › SISDPU) — insere no
        // CURSOR do editor da Movimentação, NÃO substitui o que já estiver
        // escrito (mesmo comportamento de "Usar modelo" em Ações no Eproc — ver
        // static/js/acoes_eproc.js:usarModeloPeticao). Indisponível quando
        // `getDescricaoMov` está configurado (ex.: Sugestão IA da Retornos) —
        // nesse modo o módulo não desenha editor próprio, não há onde inserir.
        async usarModeloDespacho(modeloId) {
            const modelo = this.modelosDespacho.find((m) => m.id === modeloId);
            if (!modelo || this.getDescricaoMov) return;
            this.erroUsarModeloDespacho = '';
            this.usandoModeloDespacho = true;
            try {
                const org = window._identificacaoOrgao || {};
                const subs = {
                    '[PAJ]': this.paj || '',
                    // Não dependem de PAJ nenhum — sempre resolvem (ver
                    // Configurações › Geral).
                    '[DATA]': dataPorExtenso(),
                    '[OFICIO]': org.oficio || '',
                    '[UNIDADE]': org.unidade || '',
                    '[DEFENSOR]': org.defensor_nome || '',
                    '[CARGO]': org.defensor_cargo || '',
                };
                if (this.pajNorm) {
                    try {
                        const j = await (await fetch('/api/acoes-eproc/qualificacao/' + encodeURIComponent(this.pajNorm))).json();
                        if (j.nome) subs['[NOME]'] = j.nome;
                        if (j.cpf) subs['[CPF]'] = j.cpf;
                        // Plural de propósito — TODOS os itens sincronizados do
                        // PAJ, sem religar a um assistido específico (pedido do
                        // usuário 2026-10-06: a sincronização não faz essa
                        // ligação, então expõe a lista inteira em vez de
                        // arriscar escolher o item errado).
                        if (j.telefones) subs['[TELEFONES]'] = j.telefones;
                        if (j.emails) subs['[EMAILS]'] = j.emails;
                        if (j.enderecos) subs['[ENDERECOS]'] = j.enderecos;
                        // Só resolve quando há EXATAMENTE 1 processo judicial
                        // vinculado ao PAJ — com 0 ou 2+, o backend já devolve
                        // vazio de propósito (nunca escolhe um arbitrariamente
                        // entre vários); o placeholder fica literal na tela
                        // pro usuário completar à mão nesse caso (pedido do
                        // usuário 2026-09-16: manter [PROCESSO] mesmo sem
                        // "processoSelecionado" próprio, como Ações no Eproc
                        // tem — aqui não há esse seletor).
                        if (j.processo) subs['[PROCESSO]'] = j.processo;
                    } catch (e) {}
                }
                const htmlModelo = paraHtmlSeTextoPuro(modelo.texto);
                const textoResolvido = resolverPlaceholders(htmlModelo, subs);
                if (this._editorMov) {
                    this._editorMov.insertHTML(textoResolvido);
                } else {
                    // Sem Quill (CDN fora do ar) — cai na textarea simples, só
                    // acrescenta ao final (não dá pra saber a posição do cursor
                    // sem o editor rico); tira as tags, é texto puro ali.
                    this.descMov = (this.descMov ? this.descMov + '\n\n' : '') + htmlParaTextoSimples(textoResolvido);
                }
            } finally {
                this.usandoModeloDespacho = false;
            }
        },
        // "Gerar Síntese do PAJ" — analisa o PROMPT_MAX do PAJ por IA e insere
        // o texto no CURSOR do editor da Movimentação, NÃO substitui o que já
        // estiver escrito (mesmo comportamento de usarModeloDespacho() acima).
        // Chamada bloqueante (pode levar dezenas de segundos) — sem confirm(),
        // o botão fica desabilitado com spinner enquanto roda.
        async gerarSinteseIA() {
            if (this.gerandoSinteseIA || this.getDescricaoMov || !this.pajNorm) return;
            this.erroSinteseIA = '';
            this.gerandoSinteseIA = true;
            try {
                const r = await fetch('/api/acoes-sisdpu/sintese-paj/' + encodeURIComponent(this.pajNorm), { method: 'POST' });
                const d = await r.json().catch(() => ({}));
                if (!d.ok) { this.erroSinteseIA = d.detalhe || 'Falha ao gerar a síntese.'; return; }
                const html = paraHtmlSeTextoPuro(d.texto || '');
                if (this._editorMov) {
                    this._editorMov.insertHTML(html);
                } else {
                    // Sem Quill (CDN fora do ar) — mesmo fallback de usarModeloDespacho().
                    this.descMov = (this.descMov ? this.descMov + '\n\n' : '') + (d.texto || '');
                }
            } catch (e) {
                this.erroSinteseIA = 'Falha de conexão ao gerar a síntese.';
            } finally {
                this.gerandoSinteseIA = false;
            }
        },
        fasesDropdown() { return this.fasesFav.length ? this.fasesFav : this.fasesTodas; },
        // Garante que a fase ATUAL sempre apareça como opção do <select>, favorita
        // ou não — sem isso, uma fase pré-preenchida de fora (ver
        // _receberPeticaoEproc) que não esteja entre as favoritas/frequentes some do
        // dropdown (mesmo padrão de opcoesTipoPara/opcoesSigiloPara em acoes_eproc.js).
        fasesDropdownEfetiva() {
            const base = this.fasesDropdown();
            return (this.fase && !base.includes(this.fase)) ? [this.fase, ...base] : base;
        },
        caixasDropdown() { return this.caixasFav.length ? this.caixasFav : this.caixasTodas; },
        // "Urgente": tramita para o MESMO DIA da movimentação — reaproveita o campo de
        // data explícita (prazoData), que já tem precedência sobre o prazo em dias.
        hojeISO() {
            const d = new Date();
            const mm = String(d.getMonth() + 1).padStart(2, '0');
            const dd = String(d.getDate()).padStart(2, '0');
            return d.getFullYear() + '-' + mm + '-' + dd;
        },
        urgenteSelecionado() { return this.prazoData === this.hojeISO(); },
        marcarUrgente() { this.prazoData = this.hojeISO(); this.prazo = -1; },
        // Texto efetivo de cada despacho: prioriza a fonte injetada pelo chamador
        // (Sugestão IA etc. — sempre texto puro); sem ela, a movimentação lê do Quill
        // (HTML) ou cai na textarea simples se o Quill não montou, e a tramitação usa
        // sua textarea própria (sempre texto puro — ver nota no cabeçalho do arquivo).
        descMovEfetiva() {
            if (this.getDescricaoMov) return this.getDescricaoMov() || '';
            return this._editorMov ? this._editorMov.getHTML() : this.descMov;
        },
        // True quando descMovEfetiva() é HTML de verdade (o Quill deste módulo) — o
        // backend usa isso pra decidir se escreve raw no CKEditor da movimentação ou
        // escapa como texto puro (getDescricaoMov externo, ex.: Sugestão IA da Retornos).
        descMovEhHtml() { return !this.getDescricaoMov && !!this._editorMov; },
        // O SIS-DPU 2.0 recusa descrição de movimentação com menos de 10 caracteres —
        // o painel não grava nem executa nesse caso (mesma conta do servidor:
        // ingestao/sisdpu2_movtram.aviso_descricao_curta). Sem fase, não há movimentação.
        avisoDescricaoCurta(fase, descricao, ehHtml) {
            if (!fase) return '';
            let texto = descricao || '';
            if (ehHtml) { const d = document.createElement('div'); d.innerHTML = texto; texto = d.textContent || ''; }
            const n = texto.replace(/ /g, ' ').trim().length;
            return n >= 10 ? '' : 'Gravação vedada: Descrição precisa do mínimo de 10 caracteres';
        },
        // O aviso aparece na própria linha dos botões (onde fica "Rascunho gravado"),
        // sem janela do navegador; some na próxima ação que passar na checagem.
        // Linhas de andamento do pós-execução (releitura/anexos) no log do modal.
        _progressoPos() {
            return (msg) => {
                this.sseLog.push({ level: 'info', msg });
                this.$nextTick(() => { const b = this.$refs.acoesSisdpuSseBox; if (b) b.scrollTop = b.scrollHeight; });
            };
        },
        _bloqueiaDescricaoCurta() {
            this.avisoGravacao = this.avisoDescricaoCurta(this.fase, this.descMovEfetiva(), this.descMovEhHtml());
            return !!this.avisoGravacao;
        },
        descTramEfetiva() { return this.getDescricaoTram ? (this.getDescricaoTram() || '') : this.descTram; },
        // A tramitação é texto puro no SISDPU — desformata o HTML da movimentação antes
        // de copiar (senão apareceriam tags "<b>" literais no campo de destino).
        copiarMovParaTram() { this.descTram = htmlParaTextoSimples(this.descMovEfetiva()); },

        // --- Concluir trâmite(s) da caixa de entrada ---
        // Reconfere `tramitacoesCaixa` ao vivo (POST /atualizar-tramitacoes-caixa —
        // mesma rota que concluirPajModalApp/app.js já usa) — chamado por init()
        // só quando o valor chegou `null` (nunca calculado). Silencioso se falhar
        // (sessão SISDPU fora do ar, sync ocupada etc.): melhor o console continuar
        // usável (nesse caso ele fica mostrando "Não há trâmite aberto", que pode
        // estar errado, mas o defensor sempre pode conferir no cabeçalho — ver
        // "Concluir PAJ da Caixa de Entrada" — que tem sua própria reconferência).
        async _refrescarTramitacoesCaixa() {
            try {
                const r = await fetch('/api/paj/' + encodeURIComponent(this.pajNorm) + '/atualizar-tramitacoes-caixa',
                                       { method: 'POST' });
                const d = await r.json().catch(() => ({}));
                if (d && d.ok && Array.isArray(d.tramitacoes_caixa)) {
                    this.tramitacoesCaixa = d.tramitacoes_caixa;
                    const novos = {};
                    this.tramitacoesCaixa.forEach((t) => {
                        novos[t.id_tramite] = (t.id_tramite === (this.idTramite || ''));
                    });
                    this.idsTramiteSelecionados = novos;
                }
            } catch (e) {}
        },
        // Ids marcados no modelo NOVO (lista de checkboxes) — vazio se
        // `tramitacoesCaixa` não foi passado (modelo legado não usa isto).
        idsTramiteSelecionadosArr() {
            if (!this.tramitacoesCaixa) return [];
            return this.tramitacoesCaixa.map((t) => t.id_tramite).filter((id) => this.idsTramiteSelecionados[id]);
        },
        // True quando há algo a concluir — no modelo novo, ao menos 1 checkbox
        // marcado; no legado, o toggle Sim/Não com idTramite presente. Usado tanto
        // pra habilitar "Executar" (junto com fase/grupo — AÇÕES INDEPENDENTES,
        // nenhuma delas trava as outras) quanto pra montar o payload da execução.
        concluirEfetivo() {
            return this.tramitacoesCaixa !== null
                ? this.idsTramiteSelecionadosArr().length > 0
                : !!(this.idTramite && this.concluir);
        },

        // --- Fila de movimentações sequenciais ---
        // Ids-a-concluir EFETIVOS do formulário atual, já reduzidos a uma lista
        // simples (mesmo critério de concluirEfetivo(), mas devolvendo os ids em
        // vez de só um booleano) — usado tanto pra capturar um item da fila quanto
        // (indiretamente) pra saber se há algo preenchido antes de adicionar.
        _idsConcluirFormulario() {
            return this.tramitacoesCaixa !== null
                ? this.idsTramiteSelecionadosArr()
                : (this.concluir && this.idTramite ? [this.idTramite] : []);
        },
        // Snapshot do formulário atual num item de fila — auto-contido (guarda o
        // HTML/texto já resolvido dos despachos, não uma referência ao editor Quill
        // vivo, que vai ser reaproveitado/limpo pro PRÓXIMO item).
        _capturarItemFormulario() {
            return {
                fase: this.fase,
                descricao_mov: this.descMovEfetiva(),
                descricao_mov_html: this.descMovEhHtml(),
                grupo: this.grupo,
                descricao_tram: this.descTramEfetiva(),
                prazo: this.prazo,
                prazo_data: this.prazoData,
                id_tramites: this._idsConcluirFormulario(),
                anexos: this.anexosStaged.map((a) => ({ ...a })),
            };
        },
        // Limpa o formulário (fase/despachos/tramitação/conclusão/anexos) pra
        // começar a próxima movimentação da fila do zero. NÃO mexe em `fila` nem
        // em `editandoFilaIndice` — quem chama decide isso antes/depois.
        _limparFormulario() {
            this.fase = '';
            if (this._editorMov) this._editorMov.setHTML('');
            this.descMov = '';
            this.grupo = '';
            this.prazo = 0;
            this.prazoData = '';
            this.descTram = '';
            this.concluir = true;
            if (this.tramitacoesCaixa !== null) {
                this.tramitacoesCaixa.forEach((t) => { this.idsTramiteSelecionados[t.id_tramite] = false; });
            }
            // NÃO apaga os arquivos externos já enviados (removerAnexoStaged() faria
            // isso) — eles continuam referenciados pelo item recém-adicionado à fila
            // e só serão removidos do disco quando aquele item de fato executar.
            this.anexosStaged = [];
        },
        // "Adicionar à fila" (ou "Salvar alteração", se `editandoFilaIndice` estiver
        // setado — ver editarItemFila()): captura o formulário atual como um item,
        // limpa o formulário pro próximo, e persiste a fila no rascunho do PAJ
        // (mesmo endpoint de sempre — `fila` é só mais um campo do rascunho).
        async adicionarItemFila() {
            const item = this._capturarItemFormulario();
            if (!item.fase && !item.grupo && !item.id_tramites.length) {
                alert('Preencha ao menos uma ação (movimentar, tramitar ou concluir) antes de adicionar à fila.');
                return;
            }
            if (this._bloqueiaDescricaoCurta()) return;
            if (this.editandoFilaIndice !== null) {
                this.fila.splice(this.editandoFilaIndice, 1, item);
                this.editandoFilaIndice = null;
            } else {
                this.fila.push(item);
            }
            this._limparFormulario();
            await this.gravarRascunho();
        },
        // Carrega um item da fila DE VOLTA no formulário pra ajustar — o item
        // continua na fila (na mesma posição) até "Adicionar à fila" confirmar a
        // alteração (vira "Salvar alteração" enquanto `editandoFilaIndice` != null).
        editarItemFila(i) {
            const item = this.fila[i];
            if (!item) return;
            this.fase = item.fase || '';
            if (this._editorMov) this._editorMov.setHTML(item.descricao_mov || '');
            else this.descMov = item.descricao_mov || '';
            this.grupo = item.grupo || '';
            this.prazo = item.prazo || 0;
            this.prazoData = item.prazo_data || '';
            this.descTram = item.descricao_tram || '';
            const idsItem = new Set(item.id_tramites || []);
            if (this.tramitacoesCaixa !== null) {
                this.tramitacoesCaixa.forEach((t) => { this.idsTramiteSelecionados[t.id_tramite] = idsItem.has(t.id_tramite); });
            } else {
                this.concluir = idsItem.size > 0;
                if (item.id_tramites && item.id_tramites[0]) this.idTramite = item.id_tramites[0];
            }
            this.anexosStaged = (item.anexos || []).map((a) => ({ ...a }));
            this.editandoFilaIndice = i;
        },
        // Descarta a edição em curso (NÃO mexe na fila — o item editado continua
        // como estava antes de "Editar" ser clicado) e limpa o formulário.
        cancelarEdicaoFila() {
            this.editandoFilaIndice = null;
            this._limparFormulario();
        },
        async removerItemFila(i) {
            this.fila.splice(i, 1);
            if (this.editandoFilaIndice === i) {
                this.editandoFilaIndice = null;
                this._limparFormulario();
            } else if (this.editandoFilaIndice !== null && this.editandoFilaIndice > i) {
                this.editandoFilaIndice -= 1;
            }
            await this.gravarRascunho();
        },

        // --- Anexos da movimentação: upload externo (comum) + do PAJ via modal (raro) ---
        // Extensões aceitas pelo SISDPU nos anexos de movimentação — mesma lista do
        // backend (services/acoes_sisdpu_service.TIPOS_ANEXO_MOV_OK); valida aqui
        // também pra avisar ANTES de subir o arquivo, não só depois.
        _tiposAnexoOk: ['doc', 'docx', 'odt', 'png', 'jpg', 'jpeg', 'gif', 'pdf'],
        extensaoDe(nome) {
            const m = /\.([a-z0-9]+)$/i.exec(nome || '');
            return m ? m[1].toLowerCase() : '';
        },
        // Descrição nasce a partir do nome, mas SEM a extensão (a coluna "Arquivo",
        // ao lado, já mostra o nome completo — a descrição é só o rótulo legível).
        semExtensao(nome) {
            return (nome || '').replace(/\.[a-z0-9]+$/i, '');
        },
        // "Adicionar Externo" — abre o seletor de arquivo do sistema (o caso comum).
        adicionarAnexoExterno() {
            if (this.$refs.inputAnexoExterno) this.$refs.inputAnexoExterno.click();
        },
        async onArquivosExternosEscolhidos(ev) {
            const arquivos = Array.from((ev.target && ev.target.files) || []);
            ev.target.value = ''; // permite escolher o mesmo arquivo de novo depois
            if (!arquivos.length) return;
            const rejeitados = [];
            this.enviandoAnexo = true;
            try {
                for (const f of arquivos) {
                    const ext = this.extensaoDe(f.name);
                    if (!this._tiposAnexoOk.includes(ext)) { rejeitados.push(f.name); continue; }
                    const fd = new FormData();
                    fd.append('paj_norm', this.pajNorm || '');
                    fd.append('arquivo', f);
                    try {
                        const resp = await fetch('/api/acoes-sisdpu/anexo-externo', { method: 'POST', body: fd });
                        const j = await resp.json();
                        if (j.ok) {
                            this.anexosStaged.push({ origem: 'externo', ref: j.ref, nome: j.nome, tamanho: j.tamanho, tipo: j.tipo,
                                                      descricao: this.semExtensao(j.nome), tipoArquivo: 'Não informado' });
                        } else {
                            alert('Falha ao anexar "' + f.name + '": ' + (j.detalhe || 'erro desconhecido'));
                        }
                    } catch (e) {
                        alert('Erro ao enviar "' + f.name + '": ' + e);
                    }
                }
            } finally {
                this.enviandoAnexo = false;
            }
            if (rejeitados.length) {
                alert('Formato não aceito pelo SISDPU — não enviado(s):\n' + rejeitados.join('\n')
                    + '\n\nAceitos: ' + this._tiposAnexoOk.join(', ').toUpperCase());
            }
        },
        // "Adicionar do SISDPU" (raro): documentos já baixados do SISDPU pra este PAJ.
        async carregarDocsPaj() {
            this.docsCarregados = true;
            if (!this.pajNorm) return;
            try {
                const resp = await fetch('/api/paj/' + encodeURIComponent(this.pajNorm) + '/movimentacoes');
                // Mais recente primeiro (maior sequencial no início do nome, ex.:
                // "seq072 - 42532229 - envio de wpp.pdf" — ver sisdpu_client.py) no
                // modal "Adicionar do SISDPU" — mesmo pedido/motivo do módulo Ações
                // no Eproc (2026-10-06): os anexos que interessam normalmente são os
                // últimos juntados ao PAJ, e a ordem crua (alfabética por nome de
                // arquivo) exigia rolar a lista toda pra achá-los.
                if (resp.ok) {
                    const d = await resp.json();
                    this.docsPaj = (d.anexos_sisdpu || []).slice().sort((a, b) => this._seqDe(b) - this._seqDe(a));
                }
            } catch (e) {}
        },
        // Extrai o nº de sequência do prefixo "seqNNN - ..." do nome do arquivo
        // — -1 (sempre por último) se não achar, pra nunca quebrar a ordenação
        // por causa de um arquivo fora do padrão.
        _seqDe(d) {
            const m = /^seq(\d+)/.exec((d && d.nome) || '');
            return m ? parseInt(m[1], 10) : -1;
        },
        async carregarPecasIa() {
            this.pecasIaCarregadas = true;
            if (!this.pajNorm) return;
            try {
                const j = await (await fetch('/api/acoes-sisdpu/pecas-ia/' + encodeURIComponent(this.pajNorm))).json();
                this.pecasIa = j.itens || [];
            } catch (e) {}
        },
        // `anexos_sisdpu` (mesma fonte da seção "Anexos SISDPU" da aba do PAJ):
        // {nome, tipo, tamanho, tem_ocr, caminho, caminho_ocr}.
        docKey(d) { return d.caminho || d.nome; },
        docLabel(d) { return d.nome || d.caminho || 'documento'; },
        linkArquivoPaj(d) { return '/files/' + this.pajNorm + '/' + (d.caminho || ''); },
        corBadgeTipo(tipo) {
            const t = (tipo || '').toLowerCase();
            if (t === 'pdf') return 'badge-error';
            if (t === 'doc' || t === 'docx') return 'badge-info';
            if (['jpg', 'jpeg', 'png', 'gif'].includes(t)) return 'badge-warning';
            return 'bg-base-200 text-base-content/60';
        },
        // Informativo — KB abaixo de 1MB, MB a partir daí (evita "28672 KB").
        formatarTamanho(bytes) {
            if (!bytes) return '—';
            if (bytes < 1024 * 1024) return Math.round(bytes / 1024 * 10) / 10 + ' KB';
            return Math.round(bytes / 1024 / 1024 * 10) / 10 + ' MB';
        },
        async abrirModalAnexosPaj() {
            if (!this.docsCarregados) await this.carregarDocsPaj();
            this.modalPajSelecionados = {};
            this.modalPajAberto = true;
        },
        fecharModalAnexosPaj() { this.modalPajAberto = false; },
        confirmarAnexosPaj() {
            const jaStaged = new Set(this.anexosStaged.filter(a => a.origem === 'paj').map(a => a.ref));
            this.docsPaj.forEach((d) => {
                const k = this.docKey(d);
                if (!this.modalPajSelecionados[k] || jaStaged.has(k)) return;
                this.anexosStaged.push({ origem: 'paj', ref: k, nome: this.docLabel(d), tamanho: d.tamanho || 0, tipo: d.tipo || '',
                                          descricao: this.semExtensao(this.docLabel(d)), tipoArquivo: 'Não informado' });
            });
            this.modalPajAberto = false;
        },
        // --- modal "Adicionar Petições IA" (subpasta "Peças IA" do PAJ) ---
        async abrirModalPecasIa() {
            if (!this.pecasIaCarregadas) await this.carregarPecasIa();
            this.modalPecasIaSelecionados = {};
            this.modalPecasIaAberto = true;
        },
        fecharModalPecasIa() { this.modalPecasIaAberto = false; },
        confirmarPecasIa() {
            // Mesmo `ref`/origem 'paj' de confirmarAnexosPaj() acima — "Peças IA/<arquivo>"
            // é só mais um caminho relativo dentro do PAJ, sem tratamento especial daqui
            // pra frente (upload/execução tratam qualquer origem 'paj' igual).
            const jaStaged = new Set(this.anexosStaged.filter(a => a.origem === 'paj').map(a => a.ref));
            this.pecasIa.forEach((d) => {
                const k = this.docKey(d);
                if (!this.modalPecasIaSelecionados[k] || jaStaged.has(k)) return;
                this.anexosStaged.push({ origem: 'paj', ref: k, nome: this.docLabel(d), tamanho: d.tamanho || 0, tipo: d.tipo || '',
                                          descricao: this.semExtensao(this.docLabel(d)), tipoArquivo: 'Não informado' });
            });
            this.modalPecasIaAberto = false;
        },
        // Remove um anexo staged. Se for externo (upload), descarta no servidor também
        // (evita lixo acumulando na pasta temporária — ver rota /anexo-externo/remover).
        removerAnexoStaged(item) {
            const i = this.anexosStaged.indexOf(item);
            if (i >= 0) this.anexosStaged.splice(i, 1);
            if (item.origem === 'externo') {
                const fd = new FormData();
                fd.append('paj_norm', this.pajNorm || '');
                fd.append('ref', item.ref);
                fetch('/api/acoes-sisdpu/anexo-externo/remover', { method: 'POST', body: fd }).catch(() => {});
            }
        },
        // Reordena a tabela de anexos staged (setas ▲/▼) — a ordem da lista é a ordem
        // em que os documentos são anexados na movimentação real. `direcao` é -1 (sobe)
        // ou +1 (desce); splice+splice (em vez de troca por índice) garante que o
        // Alpine detecte a mudança de posição — mesmo padrão de "Ações no Eproc".
        moverAnexoStaged(i, direcao) {
            const j = i + direcao;
            if (j < 0 || j >= this.anexosStaged.length) return;
            const item = this.anexosStaged[i];
            this.anexosStaged.splice(i, 1);
            this.anexosStaged.splice(j, 0, item);
        },
        anexosStagedParaEnviar() {
            return this.anexosStaged.map((a) => ({
                origem: a.origem, ref: a.ref,
                descricao: a.descricao || a.nome, tipo_arquivo: a.tipoArquivo || 'Não informado',
            }));
        },
        // `pularConfirmacao` (opcional, default false): true quando quem chama já
        // revisou tudo antes (ex.: resumo combinado da página "Movimentar Eproc +
        // SISDPU" — ver movimentar_conjunto.html) — evita perguntar de novo o que
        // já foi confirmado ali, mesmo espírito de outras confirmações redundantes
        // já removidas neste projeto (pedido do usuário 2026-09-11).
        // Mesmo conteúdo que executar()/executarFila() mandariam, como corpo JSON —
        // usado pelo resumo da movimentação conjunta para pedir ao SIS-DPU 2.0 (MCP)
        // as fichas ANTES de executar (POST /api/sisdpu2/escrita/fichas-resumo).
        corpoExecucaoMcp() {
            const base = { id_processo: this.idProcesso, paj: this.paj || '', paj_norm: this.pajNorm || '' };
            if (this.fila.length > 0) {
                return { ...base, itens: this.fila.map((it) => ({
                    fase: it.fase, descricao_mov: it.descricao_mov,
                    descricao_mov_html: it.descricao_mov_html ? '1' : '0',
                    grupo: it.grupo, descricao_tram: it.descricao_tram,
                    prazo: it.prazo, prazo_data: it.prazo_data, id_tramites: [],  // conclusão: legado, fora da ficha
                    anexos: it.anexos.map((a) => ({ origem: a.origem, ref: a.ref,
                        descricao: a.descricao || a.nome, tipo_arquivo: a.tipoArquivo || 'Não informado' })),
                })) };
            }
            return { ...base, fase: this.fase, descricao_mov: this.descMovEfetiva(),
                descricao_mov_html: this.descMovEhHtml() ? '1' : '0',
                grupo: this.grupo || '', descricao_tram: this.descTramEfetiva(),
                prazo: this.prazo > 0 ? this.prazo : 0, prazo_data: this.prazoData || '',
                id_tramites: [], anexos: this.fase ? this.anexosStagedParaEnviar() : [] };
        },
        async executar(pularConfirmacao) {
            // Fila não-vazia — desvia pra executarFila() (roda os itens da fila em
            // sequência, na mesma sessão do SISDPU) em vez do fluxo de execução
            // única abaixo. O formulário atual (se houver algo digitado mas ainda
            // não "Adicionado à fila") NÃO entra na execução — ver aviso na UI.
            if (this.fila.length > 0) { await this.executarFila(pularConfirmacao); return; }
            const concluirEfetivo = this.concluirEfetivo();
            const idsSelecionados = this.idsTramiteSelecionadosArr();
            if (this.exec || (!this.fase && !this.grupo && !concluirEfetivo)) return;
            if (this._bloqueiaDescricaoCurta()) return;
            const prazoTxt = this.grupo
                ? (this.urgenteSelecionado() ? ' (URGENTE — mesmo dia da movimentação)'
                   : this.prazoData ? ' (prazo ' + this.prazoData + ')'
                   : this.prazo > 0 ? ' (prazo ' + this.prazo + ' dias)' : '')
                : '';
            const anexosSel = this.fase ? this.anexosStagedParaEnviar() : [];
            const concluirTxt = !concluirEfetivo ? '\nSem conclusão'
                : this.tramitacoesCaixa !== null
                    ? '\nConcluir ' + idsSelecionados.length + ' trâmite(s) selecionado(s) (retira(m) da caixa)'
                    : '\nConcluir o trâmite (retira da caixa)';
            if (!pularConfirmacao && !confirm('Executar no SISDPU — ' + (this.paj || this.idProcesso) + '?\n\n'
                + (this.fase ? 'Movimentar (fase: ' + this.fase + ')' : 'Sem movimentação')
                + (anexosSel.length ? '\n+ ' + anexosSel.length + ' anexo(s)' : '')
                + (this.grupo ? '\nTramitar para: ' + this.grupo + prazoTxt : '\nSem tramitação')
                + concluirTxt)) return;
            // Garante o cofre desbloqueado ANTES de acionar o SISDPU — com a fonte
            // SIS-DPU 2.0 (MCP) só quando há conclusão (vedada no MCP, vai pelo legado).
            const precisaCofre = !window.PAINEL_FONTE_SISDPU_MCP || concluirEfetivo;
            if (precisaCofre && typeof window.garantirCofre === 'function' && !(await window.garantirCofre())) return;
            this.exec = true;
            this.sseTitulo = 'Executando no SISDPU — ' + (this.paj || this.idProcesso);
            this.sseLog = []; this.sseStatus = ''; this.sseAberto = true;
            const qs = new URLSearchParams({
                id_processo: this.idProcesso, paj: this.paj || '', paj_norm: this.pajNorm || '',
                fase: this.fase, descricao_mov: this.descMovEfetiva(),
                descricao_mov_html: this.descMovEhHtml() ? '1' : '0',
                grupo: this.grupo || '', descricao_tram: this.descTramEfetiva(),
                prazo: String(this.prazo > 0 ? this.prazo : 0),
                prazo_data: this.prazoData || '', id_tramite: this.idTramite || '',
                concluir: this.concluir ? '1' : '0',
                // Modelo novo (lista) tem precedência no backend sobre o par legado acima —
                // só é enviado quando o chamador passou tramitacoesCaixa (ex.: página do PAJ).
                id_tramites: this.tramitacoesCaixa !== null ? JSON.stringify(idsSelecionados) : '',
                anexos: JSON.stringify(anexosSel),
            });
            const es = new EventSource('/api/acoes-sisdpu/executar?' + qs.toString());
            es.addEventListener('log', (ev) => {
                try { this.sseLog.push(JSON.parse(ev.data)); } catch (e) {}
                this.$nextTick(() => { const b = this.$refs.acoesSisdpuSseBox; if (b) b.scrollTop = b.scrollHeight; });
            });
            es.addEventListener('done', async (ev) => {
                let res = {}; try { res = JSON.parse(ev.data); } catch (e) {}
                this.sseStatus = res.status === 'ja_ausente' ? '— já estava concluído (nada foi feito agora)'
                                : (res.ok ? '— executado ✓' : '— com aviso');
                this.exec = false; es.close();
                if (res.ok) {
                    this.anexosStaged = [];  // limpa a lista após êxito (externos já foram removidos no backend)
                    // Rascunho (se havia) já foi executado — não há mais o que retomar.
                    if (this.rascunhoSalvoEm) {
                        this.rascunhoSalvoEm = '';
                        fetch('/api/acoes-sisdpu/rascunho/' + encodeURIComponent(this.pajNorm), { method: 'DELETE' }).catch(() => {});
                    }
                }
                // Enriquece o resultado com o que FOI de fato executado (fase/grupo/despachos) —
                // o chamador (onExecutado) não tem acesso ao estado interno deste bloco, e páginas
                // como a Retornos precisam disso para registrar no Histórico do painel.
                // `descricao_mov_texto`: versão em texto puro (sem tags) — o Histórico do
                // painel guarda um resumo textual, não HTML.
                const descMovHtml = this.descMovEfetiva();
                const resultadoEnriquecido = {...res, fase: this.fase, grupo: this.grupo, idTramite: this.idTramite,
                                   descricao_mov: descMovHtml, descricao_tram: this.descTramEfetiva(),
                                   descricao_mov_texto: this.descMovEhHtml() ? htmlParaTextoSimples(descMovHtml) : descMovHtml};
                if (res.ok) {
                    // Pequeno atraso só pra deixar a mensagem final ("— executado ✓")
                    // visível antes de fechar este modal (o de sincronização, se houver,
                    // abre em seguida — ver posExecucaoPaj() em app.js).
                    await new Promise((r) => setTimeout(r, 1200));
                    // Fonte SIS-DPU 2.0: o modal fica aberto mostrando a releitura do PAJ
                    // e o download dos anexos novos (posExecucaoPaj) — a página do PAJ
                    // abre uma vez só, já em dia.
                    if (!window.PAINEL_FONTE_SISDPU_MCP) this.sseAberto = false;
                }
                // ESPERA onExecutado() terminar ANTES de recarregar — na página do PAJ,
                // onExecutado (posExecucaoPaj em app.js) já inclui esperar a
                // sincronização LEVE das movimentações. Atualiza a página INTEIRA depois
                // (pedido do usuário 2026-08-13): garante que qualquer aba/seção que
                // dependa do estado do PAJ (prazos, badges, caixa) reflita o que acabou
                // de ser feito no SISDPU, sem depender do usuário lembrar de dar F5.
                // SEM esperar onExecutado, o reload derrubava a sincronização de
                // movimentações no meio do caminho e a tabela continuava desatualizada.
                await this.onExecutado(resultadoEnriquecido, this._progressoPos());
                if (res.ok) { if (res.paj_na_lixeira) window.location.href = '/'; else if (this.aposExecutarIrPara) window.location.href = this.aposExecutarIrPara; else window.location.reload(); }
            });
            es.onerror = () => {
                this.exec = false; es.close();
                this.sseStatus = '— conexão perdida (verifique o PAJ)';
                this.sseLog.push({ level: 'erro', msg: this._avisoConexaoPerdida() });
                this.$nextTick(() => { const b = this.$refs.acoesSisdpuSseBox; if (b) b.scrollTop = b.scrollHeight; });
                // A movimentação em si quase sempre já foi efetivada no SISDPU quando a
                // conexão cai (ver comentário de _avisoConexaoPerdida acima) — mesmo sem
                // saber se tramitação/conclusão também rodaram, tenta atualizar a tabela
                // de Movimentações (só leitura, sem marcar nada como concluído/Histórico)
                // pra não deixá-la desatualizada esperando um F5 manual que nem sempre
                // vem. Bug real 2026-09-23: 'done' nunca chegou (conexão caiu ou o
                // handler travou em algum lugar) numa fila de 2 itens que TINHA
                // executado os 2 no SISDPU — tabela ficou 2 movimentações atrasada até
                // um refresh manual.
                if (this.pajNorm && typeof window.atualizarMovimentacoesPaj === 'function') {
                    window.atualizarMovimentacoesPaj(this.pajNorm).catch(() => {});
                }
            };
        },
        // Executa a FILA inteira em sequência (mesma sessão do SISDPU) — chamado por
        // executar() quando `fila.length > 0`. Cada item concluído com sucesso é
        // removido da fila (e a fila persistida no rascunho é atualizada) ASSIM QUE
        // o evento `item_done` chega — não só no final — pra uma queda de conexão no
        // meio não perder o progresso já feito: o que restou no rascunho é só o que
        // ainda falta executar.
        async executarFila(pularConfirmacao) {
            if (this.exec) return;
            const curto = this.fila.findIndex((it) => this.avisoDescricaoCurta(it.fase, it.descricao_mov, !!it.descricao_mov_html));
            if (curto >= 0) {
                this.avisoGravacao = 'Gravação vedada: Descrição da movimentação ' + (curto + 1)
                    + ' da fila precisa do mínimo de 10 caracteres';
                return;
            }
            this.avisoGravacao = '';
            const lote = this.fila.slice();  // snapshot — mesmas referências de objeto,
            // usado pra mapear o índice ESTÁVEL que o backend reporta (posição no
            // lote enviado, que não muda durante a execução) de volta pro item real
            // em `this.fila` (que vai encolhendo conforme os itens terminam).
            const resumo = lote.map((it, i) => (i + 1) + '. ' + (it.fase || 'sem movimentação')
                + (it.grupo ? ' → ' + it.grupo : '')
                + (it.id_tramites.length ? ' (conclui ' + it.id_tramites.length + ' trâmite(s))' : '')).join('\n');
            if (!pularConfirmacao && !confirm('Executar a fila (' + lote.length + ' movimentação(ões)) no SISDPU — '
                + (this.paj || this.idProcesso) + '?\n\n' + resumo)) return;
            // Fonte SIS-DPU 2.0 (MCP): o cofre só é pedido se algum item conclui trâmite.
            const precisaCofre = !window.PAINEL_FONTE_SISDPU_MCP || lote.some((it) => (it.id_tramites || []).length);
            if (precisaCofre && typeof window.garantirCofre === 'function' && !(await window.garantirCofre())) return;
            this.exec = true;
            this.sseTitulo = 'Executando fila (' + lote.length + ') no SISDPU — ' + (this.paj || this.idProcesso);
            this.sseLog = []; this.sseStatus = ''; this.sseAberto = true;
            const qs = new URLSearchParams({
                id_processo: this.idProcesso, paj: this.paj || '', paj_norm: this.pajNorm || '',
                itens: JSON.stringify(lote.map((it) => ({
                    fase: it.fase, descricao_mov: it.descricao_mov,
                    descricao_mov_html: it.descricao_mov_html ? '1' : '0',
                    grupo: it.grupo, descricao_tram: it.descricao_tram,
                    prazo: it.prazo, prazo_data: it.prazo_data,
                    id_tramites: it.id_tramites,
                    anexos: it.anexos.map((a) => ({
                        origem: a.origem, ref: a.ref,
                        descricao: a.descricao || a.nome, tipo_arquivo: a.tipoArquivo || 'Não informado',
                    })),
                }))),
            });
            const es = new EventSource('/api/acoes-sisdpu/executar-fila?' + qs.toString());
            es.addEventListener('log', (ev) => {
                try { this.sseLog.push(JSON.parse(ev.data)); } catch (e) {}
                this.$nextTick(() => { const b = this.$refs.acoesSisdpuSseBox; if (b) b.scrollTop = b.scrollHeight; });
            });
            es.addEventListener('item_done', async (ev) => {
                let d = {}; try { d = JSON.parse(ev.data); } catch (e) {}
                if (d.ok && typeof d.index === 'number' && lote[d.index]) {
                    const pos = this.fila.indexOf(lote[d.index]);
                    if (pos >= 0) this.fila.splice(pos, 1);
                    await this.gravarRascunho();
                }
            });
            es.addEventListener('done', async (ev) => {
                let res = {}; try { res = JSON.parse(ev.data); } catch (e) {}
                this.sseStatus = res.ok ? '— fila executada ✓' : '— parou com aviso (veja o log)';
                this.exec = false; es.close();
                if (res.ok) {
                    if (this.rascunhoSalvoEm) {
                        this.rascunhoSalvoEm = '';
                        fetch('/api/acoes-sisdpu/rascunho/' + encodeURIComponent(this.pajNorm), { method: 'DELETE' }).catch(() => {});
                    }
                    await new Promise((r) => setTimeout(r, 1200));
                    // Fonte SIS-DPU 2.0: o modal fica aberto mostrando a releitura do PAJ
                    // e o download dos anexos novos (posExecucaoPaj) — a página do PAJ
                    // abre uma vez só, já em dia.
                    if (!window.PAINEL_FONTE_SISDPU_MCP) this.sseAberto = false;
                }
                await this.onExecutado({ ...res, fila: true }, this._progressoPos());
                if (res.ok) { if (res.paj_na_lixeira) window.location.href = '/'; else if (this.aposExecutarIrPara) window.location.href = this.aposExecutarIrPara; else window.location.reload(); }
            });
            es.onerror = () => {
                this.exec = false; es.close();
                this.sseStatus = '— conexão perdida (verifique o PAJ)';
                this.sseLog.push({ level: 'erro', msg: 'Conexão perdida durante a execução da fila — os itens já '
                    + 'removidos da lista acima foram executados com sucesso; os que restaram continuam salvos '
                    + 'pra tentar de novo. Verifique o PAJ no SISDPU antes de executar de novo.' });
                this.$nextTick(() => { const b = this.$refs.acoesSisdpuSseBox; if (b) b.scrollTop = b.scrollHeight; });
                // Mesmo raciocínio do onerror de executar() acima — os itens já
                // removidos da fila (item_done recebido) com certeza já foram
                // efetivados no SISDPU; atualiza a tabela pra não ficar esperando um
                // F5 manual (só leitura — não marca Histórico/concluído).
                if (this.pajNorm && typeof window.atualizarMovimentacoesPaj === 'function') {
                    window.atualizarMovimentacoesPaj(this.pajNorm).catch(() => {});
                }
            };
        },

        // A conexão SSE caiu antes do fluxo mov→tram→conclui terminar (rede instável —
        // não é uma falha do SISDPU nem da aplicação: o servidor cancela a execução
        // assim que percebe o cliente desconectado). O risco real: a etapa que estava
        // "em andamento" no momento da queda PODE já ter sido efetivada no SISDPU
        // mesmo sem a confirmação ("ok") ter chegado até aqui — foi exatamente o que
        // aconteceu no PAJ 2026/029-02567 em 2026-08-07 (Movimentação executada, log
        // cortado antes da confirmação, Tramitação/Conclusão nunca rodaram). Por isso
        // o aviso é conservador: some etapa com "ok" confirmado no log vira certeza;
        // a última etapa "em andamento" (sem "ok" nem "erro" registrado depois dela)
        // vira alerta de possível duplicação — sobretudo a Movimentação, que cria um
        // novo registro a cada chamada (não é idempotente).
        _avisoConexaoPerdida() {
            const okRe = { 'Movimentação': /^Movimenta[çc][aã]o:/, 'Tramitação': /^Tramita[çc][aã]o:/, 'Conclusão': /^Conclus[aã]o:/ };
            const emCursoRe = { 'Movimentação': /^Movimentando/, 'Tramitação': /^Tramitando/, 'Conclusão': /^Concluindo/ };
            const confirmadas = Object.keys(okRe).filter(k =>
                this.sseLog.some(l => l.level === 'ok' && okRe[k].test(l.msg || '')));
            let emAndamento = '';
            for (let i = this.sseLog.length - 1; i >= 0; i--) {
                const m = this.sseLog[i].msg || '';
                if (Object.values(okRe).some(re => re.test(m))) break;  // já tinha resultado (ok ou erro) — não ficou em aberto
                const etapa = Object.keys(emCursoRe).find(k => emCursoRe[k].test(m));
                if (etapa) { emAndamento = etapa; break; }
            }
            let msg = 'Conexão com o painel perdida durante a execução (provável instabilidade de rede — o SISDPU não foi consultado sobre isso).';
            if (confirmadas.length) msg += ' Já confirmado nesta execução: ' + confirmadas.join(', ') + '.';
            if (emAndamento && !confirmadas.includes(emAndamento)) {
                msg += ' A etapa "' + emAndamento + '" estava em andamento no momento da queda — ela PODE já ter sido efetivada no SISDPU mesmo sem a confirmação aparecer aqui.';
            }
            msg += ' Verifique o PAJ no SISDPU antes de executar de novo, e desmarque no console (deixe "Sem movimentação"/"Sem tramitação") o que já foi feito — a Movimentação em especial cria um novo registro a cada execução.';
            return msg;
        },
    };
}
