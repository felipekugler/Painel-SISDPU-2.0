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
        getDescricaoMov: getDescricaoMovFn,
        getDescricaoTram: getDescricaoTramFn,
        getValorInicialMov: getValorInicialMovFn,
        onExecutado: typeof cfg.onExecutado === 'function' ? cfg.onExecutado : () => {},

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

        sseAberto: false, sseLog: [], sseStatus: '', sseTitulo: '',

        // Rascunho ("Gravar" p/ executar mais tarde) — mesmo padrão do "Ações no
        // Eproc": 1 por PAJ, auto-restaurado ao abrir. Também gravado
        // AUTOMATICAMENTE por "Ações no Eproc" depois de um "Peticionar" real
        // confirmado (ver _receberPeticaoEproc) — o usuário não precisa estar
        // com este console aberto no momento pra não perder o registro.
        rascunhoCarregado: false, rascunhoSalvoEm: '', gravando: false,
        _descMovHtmlRascunho: '',

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
            } catch (e) {}
            await this.carregarDocsPaj();
            // Antes de montar o editor — precisa do HTML salvo (se houver) pra passar
            // como valorInicial já na criação do Quill (mesmo padrão do "Ações no Eproc").
            if (!this.rascunhoCarregado) await this.carregarRascunho();
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
        // grupo/prazo. Roda uma vez por instância; silencioso se não houver
        // rascunho ou a chamada falhar. Mesmo padrão de "Ações no Eproc".
        async carregarRascunho() {
            this.rascunhoCarregado = true;
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
                this.rascunhoSalvoEm = r.salvo_em || '';
            } catch (e) {}
        },
        // "Gravar" — persiste o estado atual do console pra executar mais tarde
        // (ver botão ao lado de "Executar no SISDPU"). Mesmo padrão de "Ações no
        // Eproc": o HTML da movimentação (Quill) é salvo como está, sem reformatar.
        async gravarRascunho() {
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
            this.rascunhoCarregado = false;
            await this.carregarRascunho();
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
        descTramEfetiva() { return this.getDescricaoTram ? (this.getDescricaoTram() || '') : this.descTram; },
        // A tramitação é texto puro no SISDPU — desformata o HTML da movimentação antes
        // de copiar (senão apareceriam tags "<b>" literais no campo de destino).
        copiarMovParaTram() { this.descTram = htmlParaTextoSimples(this.descMovEfetiva()); },

        // --- Concluir trâmite(s) da caixa de entrada ---
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
                if (resp.ok) { const d = await resp.json(); this.docsPaj = d.anexos_sisdpu || []; }
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
        async executar() {
            const concluirEfetivo = this.concluirEfetivo();
            const idsSelecionados = this.idsTramiteSelecionadosArr();
            if (this.exec || (!this.fase && !this.grupo && !concluirEfetivo)) return;
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
            if (!confirm('Executar no SISDPU — ' + (this.paj || this.idProcesso) + '?\n\n'
                + (this.fase ? 'Movimentar (fase: ' + this.fase + ')' : 'Sem movimentação')
                + (anexosSel.length ? '\n+ ' + anexosSel.length + ' anexo(s)' : '')
                + (this.grupo ? '\nTramitar para: ' + this.grupo + prazoTxt : '\nSem tramitação')
                + concluirTxt)) return;
            // Garante o cofre desbloqueado ANTES de acionar o SISDPU.
            if (typeof window.garantirCofre === 'function' && !(await window.garantirCofre())) return;
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
                    this.sseAberto = false;
                }
                // ESPERA onExecutado() terminar ANTES de recarregar — na página do PAJ,
                // onExecutado (posExecucaoPaj em app.js) já inclui esperar a
                // sincronização LEVE das movimentações. Atualiza a página INTEIRA depois
                // (pedido do usuário 2026-08-13): garante que qualquer aba/seção que
                // dependa do estado do PAJ (prazos, badges, caixa) reflita o que acabou
                // de ser feito no SISDPU, sem depender do usuário lembrar de dar F5.
                // SEM esperar onExecutado, o reload derrubava a sincronização de
                // movimentações no meio do caminho e a tabela continuava desatualizada.
                await this.onExecutado(resultadoEnriquecido);
                if (res.ok) window.location.reload();
            });
            es.onerror = () => {
                this.exec = false; es.close();
                this.sseStatus = '— conexão perdida (verifique o PAJ)';
                this.sseLog.push({ level: 'erro', msg: this._avisoConexaoPerdida() });
                this.$nextTick(() => { const b = this.$refs.acoesSisdpuSseBox; if (b) b.scrollTop = b.scrollHeight; });
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
