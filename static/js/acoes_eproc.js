// Módulo Alpine reutilizável "Ações no Eproc" (peticionamento no processo judicial —
// EM CONSTRUÇÃO). Autocontido, mesmo padrão de acoes_sisdpu.js. Uso:
//   <div x-data="acoesEprocBlock({ processos: r.processos_judiciais, paj: r.paj,
//                                  pajNorm: r.paj_norm })">
//     {% include "partials/acoes_eproc.html" %}
//   </div>
//
// cfg:
//   processos (obrigatório em algum sentido — precisa de ao menos 1) — lista de
//             {numero,...} a movimentar/peticionar (para achar prazos e resolver
//             o evento). Pode ser um processo AVULSO, sem PAJ nenhum por trás —
//             ver `pajNorm` abaixo.
//   paj       (opcional) — rótulo do PAJ, só para exibição/mensagens.
//   pajNorm   (opcional) — PAJ REAL sincronizado localmente. Só é usado por 3
//             coisas que de fato precisam de uma pasta de PAJ em disco:
//             "Adicionar do SISDPU" e "Adicionar Petições IA" (puxam arquivo já
//             baixado) e o registro automático da movimentação pendente no
//             SISDPU depois de peticionar. SEM `pajNorm` (petição avulsa, só
//             pelo número do processo — ver `peticoes_pendentes.html` botão
//             "+ Adicionar petição" / minimenu "Movimentar no eproc"), essas 3
//             coisas ficam indisponíveis, mas o peticionamento em si funciona
//             normalmente — o e-proc nunca exigiu PAJ, só o Painel usava um pra
//             organizar rascunho/anexos (pedido do usuário 2026-09-11: mesma
//             lógica de "Adicionar movimentação" do módulo Ações no SISDPU).
//   aberto    (opcional) — estado inicial da seção retrátil (default: false).
//
// Favoritos/frequentes de EVENTO e TIPO DE DOCUMENTO, e o padrão de SIGILO, vêm de
// /api/acoes-eproc/preferencias (curadoria em Configurações › e-proc — mesmo padrão
// de favoritas/frequentes usado em Ações no SISDPU).
//
// Formatos/tamanhos aceitos pelo e-proc para anexos (informado pelo usuário). Extensão
// não listada aqui é BLOQUEADA ao tentar adicionar; tamanho acima do limite não bloqueia
// (o e-proc pode até aceitar dependendo do caso), só gera um aviso na linha do anexo.
const EPROC_FORMATOS_ACEITOS = {
    pdf: { categoria: 'Documentos', maxMB: 11 }, htm: { categoria: 'Documentos', maxMB: 11 },
    html: { categoria: 'Documentos', maxMB: 11 }, kml: { categoria: 'Documentos', maxMB: 11 },
    mp3: { categoria: 'Áudio', maxMB: 70 }, wma: { categoria: 'Áudio', maxMB: 70 }, wav: { categoria: 'Áudio', maxMB: 70 },
    jpeg: { categoria: 'Imagens', maxMB: 11 }, jpg: { categoria: 'Imagens', maxMB: 11 }, png: { categoria: 'Imagens', maxMB: 11 },
    mp4: { categoria: 'Vídeos', maxMB: 70 }, wmv: { categoria: 'Vídeos', maxMB: 70 },
    mpg: { categoria: 'Vídeos', maxMB: 70 }, mpeg: { categoria: 'Vídeos', maxMB: 70 },
};

function acoesEprocBlock(cfg) {
    cfg = cfg || {};
    return {
        aberto: !!cfg.aberto,
        processos: cfg.processos || [],
        paj: cfg.paj || '',
        pajNorm: cfg.pajNorm || '',
        // Processo judicial escolhido para a ação (relevante só quando o PAJ tem
        // MAIS DE UM vinculado — evita movimentar/peticionar no processo errado).
        // Com só 1 (ou 0), fica vazio e carregarPrazos() usa todos os vinculados.
        processoSelecionado: '',
        // Classe processual de cada processo (numero -> classe, ex.: "Cumprimento
        // de Sentença contra Fazenda Pública (JEF)") — só buscada quando há MAIS
        // DE UM processo vinculado (o <select> te ajuda a escolher o certo; com
        // só 1, não há o que escolher). Consulta leve via MCP (sem navegador —
        // ver resolver_classe_processo), carregada uma vez por abertura do
        // console. Pedido do usuário 2026-09-29: número + juízo sozinhos eram
        // pouco pra diferenciar processos do mesmo PAJ à primeira vista.
        classesProcesso: {}, carregandoClasses: false,

        // Chave de armazenamento do RASCUNHO desta petição (evento/anexos/prazos
        // marcados) — usada em TODAS as URLs `/api/acoes-eproc/rascunho/...` e na
        // execução real. É o `pajNorm` quando existir (comportamento de sempre);
        // sem PAJ (petição avulsa — ver nota de `pajNorm` acima), vira
        // "PROC-<dígitos do processo>" — prefixo diferente de "PAJ-" garante que
        // nunca colida com um paj_norm de verdade. Nunca usar `this.pajNorm`
        // diretamente nessas chamadas — só esta função (as 3 funcionalidades que
        // exigem PAJ de verdade continuam usando `this.pajNorm` puro).
        chaveRascunho() {
            if (this.pajNorm) return this.pajNorm;
            const numero = this.processoSelecionado
                || (this.processos[0] && this.processos[0].numero) || '';
            const digitos = String(numero).replace(/\D/g, '');
            return digitos ? ('PROC-' + digitos) : '';
        },

        prazos: [], prazosSelecionados: {}, carregandoPrazos: false, prazosCarregados: false,
        evento: '', peticaoTexto: '',
        // Editor de texto rico (Quill) da Petição — montado sob demanda em
        // _carregarAoAbrir() (mesmo padrão lazy de prazos/docsPaj: não faz sentido
        // montar um Quill por linha da Retornos se o console nunca for aberto).
        // `quillPeticaoOk` começa otimista; só vira false se o Quill não carregar.
        uidPeticao: 'qpet-' + Math.random().toString(36).slice(2, 9),
        _editorPeticao: null, quillPeticaoOk: true, _montandoEditorPeticao: false,
        docsPaj: [], docsCarregados: false,
        // "Adicionar Petições IA" — mesma tabela/modal de "Adicionar do SISDPU",
        // mas listando a subpasta "Peças IA" do PAJ (petições/peças geradas pela
        // análise de pretensão) em vez de `Arquivos/` (ver listar_pecas_ia).
        pecasIa: [], pecasIaCarregadas: false,
        modalPecasIaAberto: false, modalPecasIaSelecionados: {},
        // tabela de anexos — itens numerados, adicionados um a um:
        // {_id, origem:'paj'|'upload'|'rascunho', docPajKey, docLabelCache, arquivo,
        //  nomeArquivo, tipo, sigilo} — 'rascunho' é um "Externo" já persistido no
        // servidor (ver gravarRascunho/carregarRascunho): sobrevive a um reload,
        // diferente de 'upload' puro (só um `File` na memória da aba).
        // `_id` é uma chave ESTÁVEL (nunca o índice `i` do array) pro `:key` do
        // x-for da tabela — bug real 2026-08-19: com `:key="i"`, um `unshift()`
        // (petição sempre entra no TOPO da lista — ver _inserirOuAtualizarAnexoPeticao)
        // ou o reordenar ▲/▼ (moverAnexo) faz o Alpine REAPROVEITAR o <select> de
        // "Tipo" que já existia noutra posição em vez de criar um novo — e como
        // `x-init="$nextTick(...)"` só roda na criação do nó, o valor pré-selecionado
        // (ex.: "PETIÇÃO") nunca chegava a aparecer no <select>, mesmo com o dado
        // certo no anexo (mesma classe de bug de alpine-select-xmodel-xfor-race,
        // aqui causada por reordenar/inserir no meio em vez de só carregar assíncrono).
        anexos: [],
        _anexoIdSeq: 0,
        _novoAnexoId() { this._anexoIdSeq += 1; return this._anexoIdSeq; },
        modalDocsAberto: false, modalSelecionados: {},

        // Rascunho ("Gravar" p/ peticionar mais tarde — ver "Petições pendentes")
        rascunhoCarregado: false, rascunhoSalvoEm: '', gravando: false,
        _peticaoHtmlRascunho: '',

        // "Gerar petição com IA" — responde ao(s) ato(s) referenciado(s) do(s)
        // prazo(s) marcados em "Selecione o(s) prazo(s)..." (ver peticao_ia_service).
        gerandoIA: false, erroGerarIA: '', _pollGerarIA: null,
        // "Incluir petição nos anexos" — formata o texto ATUAL da caixa (sem IA)
        // e (re)anexa como PDF. Útil depois de ajustar manualmente o texto gerado.
        incluindoAnexo: false, erroIncluirAnexo: '',
        // "Usar modelo" — modelos de petição cadastrados em Configurações › e-proc
        // (texto padrão + placeholders [NOME]/[CPF]/[PROCESSO]/[PAJ]), carregados
        // junto das demais preferências (ver carregarPreferencias). Ver
        // usarModeloPeticao() mais abaixo.
        modelosPeticao: [], usandoModelo: false, erroUsarModelo: '',

        // preferências (favoritos=dropdown, frequentes=badges) — cache local
        eventosFav: [], eventosFreq: [], eventosTodos: [],
        tiposFav: [], tiposFreq: [], tiposTodos: [],
        sigilosTodos: [], sigiloPadrao: '',
        procuradoresFav: [], procuradoresTodos: [],

        // --- Gerenciamento do processo (substabelecimento) — SEMPRE a última coisa
        // feita: só configurado aqui (persistido no rascunho, ver gravarRascunho()/
        // carregarRascunho()); a execução real acontece AUTOMATICAMENTE, sem
        // perguntar de novo, logo depois do peticionamento confirmado (ver
        // confirmarPeticionamentoReal()) — a decisão de gerenciar já foi tomada
        // aqui, ao ativar/configurar, então confirmar outra vez seria redundante
        // (pedido do usuário 2026-08-19). Tipos são estáticos (mesma lista de
        // `eproc_client.TIPOS_GERENCIAMENTO`); procurador destino vem das
        // preferências (a lista institucional completa tem centenas de nomes —
        // curadoria de favoritos em Configurações › e-proc).
        gerenciarAtivo: false, gerenciarTipo: 'S', gerenciarProcuradorValor: '', gerenciarProcuradorRotulo: '',
        tiposGerenciamento: [
            { valor: 'S', rotulo: 'Substituir procurador' },
            { valor: 'C', rotulo: 'Associar procurador' },
            { valor: 'D', rotulo: 'Desassociar procurador' },
            { valor: 'T', rotulo: 'Substituição Transitória' },
        ],

        async init() {
            // Avisa qualquer página-mãe que precise saber qual processo está
            // ativo AGORA (ex.: "Movimentar Eproc + SISDPU" — o próprio módulo
            // resolve a ambiguidade de mais de um processo vinculado, e a
            // página-mãe só escuta pra manter seu `processoConfirmado` em
            // sincronia; ver movimentar_conjunto.html). Registrado ANTES de
            // qualquer coisa poder mudar `processoSelecionado` (pré-seleção
            // automática em _carregarAoAbrir() OU troca manual no <select>) —
            // $watch só dispara em mudanças DEPOIS de registrado. Página que
            // não escuta o evento (a maioria) não é afetada em nada.
            this.$watch('processoSelecionado', (numero) => {
                window.dispatchEvent(new CustomEvent('processo-eproc-selecionado', { detail: { numero, pajNorm: this.pajNorm } }));
            });
            await this.carregarPreferencias();
            // Consumidores que já nascem com aberto:true no cfg (ex.: a página do
            // PAJ, que pula o clique no cabeçalho para já mostrar o console aberto)
            // não passam pelo toggle de abrir() — dispara aqui a mesma carga inicial
            // (seleção padrão de processo + prazos + docs) para não ficar em branco.
            if (this.aberto) await this._carregarAoAbrir();
        },
        async carregarPreferencias() {
            try {
                const j = await (await fetch('/api/acoes-eproc/preferencias')).json();
                this.eventosFav = j.eventos_favoritos || [];
                this.eventosFreq = j.eventos_frequentes || [];
                this.eventosTodos = j.eventos_todos || [];
                this.tiposFav = j.tipos_favoritos || [];
                this.tiposFreq = j.tipos_frequentes || [];
                this.tiposTodos = j.tipos_todos || [];
                this.sigilosTodos = j.sigilos_todos || [];
                // padrão: o configurado em Configurações › e-proc; na ausência, "sem
                // sigilo" é sempre a escolha certa (documento público por padrão).
                this.sigiloPadrao = j.sigilo_padrao
                    || this.sigilosTodos.find(s => /sem sigilo/i.test(s)) || '';
                if (!this.evento) this.evento = j.evento_padrao || '';
                this.procuradoresFav = j.procuradores_favoritos || [];
                this.procuradoresTodos = j.procuradores_todos || [];
                this.modelosPeticao = j.modelos_peticao || [];
                // Pré-preenche o procurador destino padrão (Configurações › e-proc) só se
                // ainda não houver nada escolhido — carregarRascunho() roda DEPOIS e
                // sobrescreve com o que foi de fato salvo, se houver rascunho.
                if (!this.gerenciarProcuradorValor && j.procurador_padrao) {
                    this.gerenciarProcuradorValor = j.procurador_padrao.valor || '';
                    this.gerenciarProcuradorRotulo = j.procurador_padrao.rotulo || '';
                }
            } catch (e) {}
        },
        eventosDropdown() { return this.eventosFav.length ? this.eventosFav : this.eventosTodos; },
        tiposDropdown() { return this.tiposFav.length ? this.tiposFav : this.tiposTodos; },
        procuradoresDropdown() { return this.procuradoresFav.length ? this.procuradoresFav : this.procuradoresTodos; },
        // Guarda o rótulo junto do valor (não dá pra recuperar só do `value` depois
        // que o rascunho é descartado no servidor — ver `montar_resumo_execucao`).
        onTrocarProcuradorDestino() {
            const p = this.procuradoresDropdown().find(p => p.valor === this.gerenciarProcuradorValor);
            this.gerenciarProcuradorRotulo = p ? p.rotulo : '';
        },
        // tiposDropdown()/sigilosTodos priorizam favoritos (subconjunto curado) —
        // certo pra ESCOLHER um tipo novo, mas errado pra EXIBIR um anexo
        // restaurado do rascunho: se o tipo/sigilo salvo não é favorito, ele
        // simplesmente não aparece como opção no <select>, que aí mostra o
        // primeiro item da lista (placeholder "Tipo…" ou o 1º sigilo) — parece
        // "não gravou", mas na real o valor certo tá lá, só não tem onde
        // aparecer. Estas duas garantem que o valor ATUAL do anexo sempre
        // apareça como opção, favorito ou não.
        opcoesTipoPara(a) {
            const base = this.tiposDropdown();
            return (a.tipo && !base.includes(a.tipo)) ? [a.tipo, ...base] : base;
        },
        opcoesSigiloPara(a) {
            const base = this.sigilosTodos;
            return (a.sigilo && !base.includes(a.sigilo)) ? [a.sigilo, ...base] : base;
        },
        _normalizarTexto(s) {
            return (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();
        },
        // O e-proc não aceita anexo neste evento — confirmado pelo usuário. Comparação
        // tolerante a acento/caixa (o texto exato vem do catálogo importado do e-proc).
        eventoSemAnexo() {
            return this._normalizarTexto(this.evento) === this._normalizarTexto('Ciência, com renúncia ao prazo');
        },
        async abrir() {
            this.aberto = !this.aberto;
            if (!this.aberto) return;
            await this._carregarAoAbrir();
        },
        // Carga feita a primeira vez que o console fica visível (seja pelo clique
        // no cabeçalho via abrir(), seja por já nascer aberto:true — ver init()).
        async _carregarAoAbrir() {
            // Mais de um processo vinculado: pré-seleciona o ÚLTIMO (mesma convenção
            // do SISDPU para os campos singulares — o mais recente é o "ativo"),
            // mas exige confirmação visual do usuário via o select antes de agir.
            if (!this.processoSelecionado && this.processos.length) {
                this.processoSelecionado = this.processos[this.processos.length - 1].numero;
            }
            // Antes de montar o editor — precisa do HTML salvo (se houver) pra
            // passar como valorInicial já na criação do Quill.
            if (!this.rascunhoCarregado) await this.carregarRascunho();
            if (!this.prazosCarregados) await this.carregarPrazos();
            if (!this.docsCarregados) await this.carregarDocsPaj();
            if (this.processos.length > 1) this.carregarClassesProcessos();  // não bloqueia — enche o rótulo quando chegar
            // Trava síncrona ANTES do primeiro `await` (mesmo motivo/padrão do
            // _montarEditorMov() em acoes_sisdpu.js): init() chamado duas vezes por
            // instância (redundância x-init + auto-init do Alpine) + um `await` antes
            // de montar o Quill = janela real pra duplicar a toolbar sem esta trava.
            if (!this._editorPeticao && this.quillPeticaoOk && !this._montandoEditorPeticao) {
                this._montandoEditorPeticao = true;
                await this.$nextTick();
                this._editorPeticao = criarEditorRico(this.uidPeticao, {
                    placeholder: 'Texto da petição ou referência a um PDF já pronto (formato em definição — próxima fase).',
                    valorInicial: this._peticaoHtmlRascunho || '',
                });
                this.quillPeticaoOk = !!this._editorPeticao;
            }
        },
        // Carrega o rascunho salvo do PAJ (se houver) — evento, prazos marcados,
        // texto da petição e anexos (ver `gravarRascunho`). Roda uma vez por
        // abertura do console; silencioso se não houver rascunho ou a chamada falhar.
        async carregarRascunho() {
            this.rascunhoCarregado = true;
            if (!this.chaveRascunho()) return;
            try {
                const j = await (await fetch('/api/acoes-eproc/rascunho/' + encodeURIComponent(this.chaveRascunho()))).json();
                const r = j.rascunho;
                if (!r) return;
                if (r.processo) this.processoSelecionado = r.processo;
                if (r.evento) this.evento = r.evento;
                this.prazosSelecionados = r.prazos_selecionados || {};
                this._peticaoHtmlRascunho = r.peticao_html || '';
                this.anexos = (r.anexos || []).map((a) => ({ ...a, _id: this._novoAnexoId(), _comprimindo: false }));
                const ger = r.gerenciamento || {};
                this.gerenciarAtivo = !!ger.ativo;
                this.gerenciarTipo = ger.tipo || 'S';
                this.gerenciarProcuradorValor = ger.procurador_valor || '';
                this.gerenciarProcuradorRotulo = ger.procurador_rotulo || '';
                this.rascunhoSalvoEm = r.salvo_em || '';
            } catch (e) {}
        },
        // "Gravar" — persiste o estado atual do console pra continuar depois (ver
        // página "Petições pendentes"). Anexos "Externo" ainda não enviados ao
        // servidor sobem primeiro (senão o `File` se perde ao recarregar a página).
        async gravarRascunho() {
            this.gravando = true;
            try {
                for (const a of this.anexos) {
                    if (a.origem === 'upload' && a.arquivo && !a._rascunhoNome) {
                        const fd = new FormData();
                        fd.append('arquivo', a.arquivo);
                        try {
                            const resp = await fetch('/api/acoes-eproc/rascunho/' + encodeURIComponent(this.chaveRascunho()) + '/upload', {
                                method: 'POST', body: fd,
                            });
                            const j = await resp.json();
                            if (j.ok) { a._rascunhoNome = j.nome_arquivo; a._rascunhoTamanho = j.tamanho; }
                        } catch (e) {}
                    }
                }
                const anexos = this.anexos.map((a) => {
                    if (a.origem === 'paj') {
                        return { origem: 'paj', docPajKey: a.docPajKey, docLabelCache: a.docLabelCache,
                                 tamanho: a.tamanho, tipo: a.tipo, sigilo: a.sigilo };
                    }
                    if (a.origem === 'rascunho') {
                        return { origem: 'rascunho', nomeArquivo: a.nomeArquivo, docLabelCache: a.docLabelCache,
                                 tamanho: a.tamanho, tipo: a.tipo, sigilo: a.sigilo };
                    }
                    // 'upload' — usa o nome já persistido (ou vazio, se o envio falhou)
                    return { origem: 'rascunho', nomeArquivo: a._rascunhoNome || '',
                             docLabelCache: a.arquivo ? a.arquivo.name : (a.docLabelCache || ''),
                             tamanho: a._rascunhoTamanho || (a.arquivo ? a.arquivo.size : 0),
                             tipo: a.tipo, sigilo: a.sigilo };
                });
                const resp = await fetch('/api/acoes-eproc/rascunho/' + encodeURIComponent(this.chaveRascunho()), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        paj_label: this.paj,
                        // PAJ REAL vinculado (pode ser vazio — petição avulsa, só pelo nº do
                        // processo) — distinto da chave de armazenamento acima (que também
                        // pode ser um "PROC-..." sem PAJ nenhum). "Petições pendentes" usa
                        // este campo pra decidir se mostra link de "Abrir PAJ" ou não.
                        paj_norm_vinculado: this.pajNorm || '',
                        processo: this.processoSelecionado || (this.processos[0] && this.processos[0].numero) || '',
                        evento: this.evento,
                        peticao_html: this.peticaoEfetiva(),
                        prazos_selecionados: this.prazosSelecionados,
                        anexos,
                        gerenciamento: {
                            ativo: this.gerenciarAtivo,
                            tipo: this.gerenciarTipo,
                            procurador_valor: this.gerenciarTipo === 'D' ? '' : this.gerenciarProcuradorValor,
                            procurador_rotulo: this.gerenciarTipo === 'D' ? '' : this.gerenciarProcuradorRotulo,
                        },
                    }),
                });
                const j = await resp.json();
                if (!j.ok) { alert('Falha ao gravar rascunho: ' + (j.erro || 'erro desconhecido')); return; }
                this.rascunhoSalvoEm = j.salvo_em;
                // 'upload' recém-persistidos passam a ser tratados como 'rascunho' na
                // tela — evita reenviar o arquivo se "Gravar" for clicado de novo, e
                // usa o link do servidor em vez do blob local (que pode expirar).
                this.anexos = this.anexos.map((a) => (a.origem === 'upload' && a._rascunhoNome)
                    ? { _id: a._id, origem: 'rascunho', nomeArquivo: a._rascunhoNome,
                        docLabelCache: a.arquivo ? a.arquivo.name : a.docLabelCache,
                        tamanho: a._rascunhoTamanho || (a.arquivo ? a.arquivo.size : 0),
                        tipo: a.tipo, sigilo: a.sigilo, _comprimindo: false }
                    : a);
            } catch (e) {
                alert('Erro ao gravar rascunho: ' + e);
            } finally {
                this.gravando = false;
            }
        },
        // Texto/HTML efetivo da petição — usado quando o peticionamento real for
        // implementado (Fase 4). Por ora nada consome isto ainda.
        peticaoEfetiva() { return this._editorPeticao ? this._editorPeticao.getHTML() : this.peticaoTexto; },

        // --- "Usar modelo" (Modelos de petição, cadastrados em Configurações ›
        // e-proc) --- Insere o texto do modelo NA POSIÇÃO DO CURSOR (não
        // substitui o que já estiver escrito — pedido do usuário 2026-09-16,
        // diferente de "Gerar petição com IA", que sobrescreve). Placeholders
        // [PROCESSO]/[PAJ] resolvem localmente (já estão no estado do console);
        // [NOME]/[CPF] exigem 1 chamada ao servidor (assistido PRINCIPAL do PAJ
        // — ver services.peticao_ia_service.obter_qualificacao_principal) e só
        // resolvem quando há PAJ vinculado (petição avulsa: ficam literais na
        // tela, o usuário completa à mão). NÃO existe mais [LOCAL] (removido
        // 2026-09-16 — vinha errado, "Previdenciário" em vez da cidade; não
        // há hoje dado de cidade/comarca confiável no painel pra isso).
        async usarModeloPeticao(modeloId) {
            const modelo = this.modelosPeticao.find((m) => m.id === modeloId);
            if (!modelo) return;
            this.erroUsarModelo = '';
            this.usandoModelo = true;
            try {
                const org = window._identificacaoOrgao || {};
                const subs = {
                    '[PROCESSO]': this.processoSelecionado
                        || (this.processos[0] && this.processos[0].numero) || '',
                    '[PAJ]': this.paj || '',
                    // Não dependem de PAJ nenhum — sempre resolvem, mesmo em
                    // petição avulsa (ver Configurações › Geral).
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
                    } catch (e) {}
                }
                // Modelos cadastrados no editor rico (Configurações › e-proc) já
                // vêm em HTML — se algum modelo mais antigo ainda estiver em texto
                // puro (sem tags), embrulha em <p> igual antes, pra não perder as
                // quebras de linha ao inserir no Quill.
                const htmlModelo = paraHtmlSeTextoPuro(modelo.texto);
                const textoResolvido = resolverPlaceholders(htmlModelo, subs);
                if (this._editorPeticao) {
                    this._editorPeticao.insertHTML(textoResolvido);
                } else {
                    // Sem Quill (CDN fora do ar) — cai na textarea simples, só
                    // acrescenta ao final (não dá pra saber a posição do cursor
                    // sem o editor rico); tira as tags, é texto puro ali.
                    this.peticaoTexto = (this.peticaoTexto ? this.peticaoTexto + '\n\n' : '') + htmlParaTextoSimples(textoResolvido);
                }
            } finally {
                this.usandoModelo = false;
            }
        },
        // dataPorExtenso()/paraHtmlSeTextoPuro()/resolverPlaceholders() — ver
        // static/js/rich_editor.js (compartilhados com "Despachos padrão" de
        // Ações no SISDPU, mesmo mecanismo de "Usar modelo").

        // --- "Gerar petição com IA" ---
        prazoIdsSelecionados() {
            return Object.keys(this.prazosSelecionados).filter((id) => this.prazosSelecionados[id]);
        },
        async gerarPeticaoIA() {
            this.erroGerarIA = '';
            const prazoIds = this.prazoIdsSelecionados();
            if (!prazoIds.length) {
                this.erroGerarIA = 'Selecione ao menos um prazo em "Selecione o(s) prazo(s) a ser(em) '
                    + 'fechado(s)" antes de gerar a petição com IA — é o que ela vai responder.';
                return;
            }
            this.gerandoIA = true;
            try {
                const resp = await fetch('/api/acoes-eproc/gerar-peticao-ia/' + encodeURIComponent(this.pajNorm), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        processo: this.processoSelecionado || (this.processos[0] && this.processos[0].numero) || '',
                        prazo_ids: prazoIds,
                    }),
                });
                const j = await resp.json();
                if (!j.ok) { this.erroGerarIA = j.erro || 'Falha ao iniciar a geração.'; this.gerandoIA = false; return; }
                this._pollGerarIA = setInterval(() => this._checarGerarIA(), 4000);
            } catch (e) {
                this.erroGerarIA = 'Erro de rede: ' + e;
                this.gerandoIA = false;
            }
        },
        async _checarGerarIA() {
            try {
                const j = await (await fetch('/api/acoes-eproc/gerar-peticao-ia/' + encodeURIComponent(this.pajNorm) + '/status')).json();
                if (j.status === 'running') return;
                clearInterval(this._pollGerarIA);
                this._pollGerarIA = null;
                this.gerandoIA = false;
                if (j.status === 'error') { this.erroGerarIA = j.erro || 'Falha ao gerar a petição.'; return; }
                if (j.status === 'done' && j.arquivo) {
                    this._inserirOuAtualizarAnexoPeticao(j.arquivo, j.tamanho);
                    // Coloca o texto gerado na caixa de Petição — sobrescreve o que
                    // estava lá (a IA responde exatamente ao que foi pedido; se o Quill
                    // não montou, cai no textarea simples via peticaoTexto).
                    if (j.peticao_html) {
                        if (this._editorPeticao) this._editorPeticao.setHTML(j.peticao_html);
                        else this.peticaoTexto = j.peticao_html.replace(/<[^>]+>/g, '\n').trim();
                    }
                }
            } catch (e) {}
        },
        // "Incluir petição nos anexos" — formata o texto ATUAL da caixa (com os
        // ajustes manuais que o usuário tenha feito) e (re)anexa como PDF, sem
        // chamar IA. Mesmo arquivo lógico da geração por IA (peticao_ia.pdf) —
        // se já existir um anexo apontando pra ele, só atualiza o tamanho em vez
        // de duplicar a linha na tabela.
        async incluirPeticaoAnexo() {
            this.erroIncluirAnexo = '';
            const html = this.peticaoEfetiva();
            if (!html || !html.replace(/<[^>]+>/g, '').trim()) {
                this.erroIncluirAnexo = 'A caixa de Petição está vazia — escreva ou gere o texto antes de incluir nos anexos.';
                return;
            }
            this.incluindoAnexo = true;
            try {
                const resp = await fetch('/api/acoes-eproc/incluir-peticao-anexo/' + encodeURIComponent(this.pajNorm), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ peticao_html: html }),
                });
                const j = await resp.json();
                if (!j.ok) { this.erroIncluirAnexo = j.erro || 'Falha ao formatar a petição.'; return; }
                this._inserirOuAtualizarAnexoPeticao(j.arquivo, j.tamanho);
            } catch (e) {
                this.erroIncluirAnexo = 'Erro de rede: ' + e;
            } finally {
                this.incluindoAnexo = false;
            }
        },
        // Compartilhado por "Gerar petição com IA" e "Incluir petição nos anexos"
        // — os dois escrevem no MESMO arquivo lógico (peticao_ia.pdf); se já
        // houver uma linha apontando pra ele na tabela, só atualiza o tamanho
        // em vez de duplicar. Tipo "Petição"/sigilo "Sem Sigilo" casam com o
        // catálogo importado do e-proc quando disponível (case/acento tolerante),
        // senão usa o texto literal mesmo (funciona igual no submit, só não fica
        // pré-marcado num item exato do dropdown).
        _inserirOuAtualizarAnexoPeticao(nomeArquivo, tamanho) {
            const existente = this.anexos.find((a) => a.origem === 'rascunho' && a.nomeArquivo === nomeArquivo);
            if (existente) { existente.tamanho = tamanho || 0; return; }
            // Match EXATO ("PETIÇÃO", ignorando acento/caixa) — NUNCA aproximado
            // (.includes solto pegava "ANEXOS DA PETIÇÃO INICIAL", que vem antes
            // na ordem alfabética do catálogo e também contém a palavra,
            // classificando errado o que é sempre e só a petição em si — bug real
            // 2026-08-20). Sem um tipo "Petição" exato no catálogo importado
            // deste e-proc, cai direto no literal — nunca num tipo aproximado/errado.
            const tipoPeticao = this.tiposTodos.find((t) => this._normalizarTexto(t) === 'peticao') || 'Petição';
            const semSigilo = this.sigilosTodos.find((s) => /sem sigilo/i.test(s)) || 'Sem Sigilo';
            // docLabelCache = o próprio nome real do arquivo (já vem com ".pdf" do
            // backend, ex.: "Petição-12-08-2026.pdf") — extensaoDe()/validarAnexo()
            // leem a extensão do nome, não do campo `arquivo`; sem isso, o e-proc
            // "não reconhece o formato" mesmo o arquivo sendo um PDF de verdade.
            this.anexos.unshift({
                _id: this._novoAnexoId(), origem: 'rascunho', nomeArquivo, docLabelCache: nomeArquivo,
                tamanho: tamanho || 0, tipo: tipoPeticao, sigilo: semSigilo, _comprimindo: false,
            });
        },
        // Chamado ao trocar a seleção no dropdown "Processo judicial" — os prazos
        // são recarregados escopados ao novo processo (evita misturar/confundir
        // prazos de processos diferentes vinculados ao mesmo PAJ).
        async trocarProcesso() {
            this.prazosSelecionados = {};
            await this.carregarPrazos();
        },
        async carregarPrazos() {
            // Com mais de um processo vinculado, restringe ao selecionado (impede
            // agir no processo errado); com só 1 (ou nenhum selecionado ainda),
            // usa todos os vinculados ao PAJ.
            const numeros = this.processoSelecionado
                ? [this.processoSelecionado]
                : (this.processos || []).map(p => p.numero).filter(Boolean);
            this.prazosCarregados = true;
            if (!numeros.length) { this.prazos = []; return; }
            this.carregandoPrazos = true;
            try {
                const qs = new URLSearchParams();
                numeros.forEach(n => qs.append('processo', n));
                const j = await (await fetch('/api/acoes-eproc/prazos?' + qs.toString())).json();
                this.prazos = j.itens || [];
            } catch (e) { this.prazos = []; }
            finally { this.carregandoPrazos = false; }
        },
        // Busca a classe processual de cada processo (numero -> classe) — uma
        // requisição leve por processo, em paralelo, sem bloquear o resto do
        // console (chamado sem `await` em _carregarAoAbrir). Silenciosa por
        // processo: se um falhar (ex.: processo não encontrado via MCP), o
        // rótulo do <select> só cai pro texto sem classe pra aquele item —
        // nunca impede escolher/usar os demais.
        async carregarClassesProcessos() {
            this.carregandoClasses = true;
            try {
                await Promise.all((this.processos || []).map(async (p) => {
                    if (!p.numero || this.classesProcesso[p.numero]) return;
                    try {
                        const j = await (await fetch('/api/acoes-eproc/classe-processo?numero=' + encodeURIComponent(p.numero))).json();
                        if (j.ok && j.classe) this.classesProcesso[p.numero] = j.classe;
                    } catch (e) {}
                }));
            } finally {
                this.carregandoClasses = false;
            }
        },
        // Rótulo do <select> "Processo judicial a movimentar": número — classe
        // (se já carregada) — juízo (se houver). Ex.: "5014914-73.2026.4.04.7000
        // — Cumprimento de Sentença contra Fazenda Pública (JEF) — Juízo D...".
        labelProcesso(p) {
            const classe = this.classesProcesso[p.numero];
            return p.numero + (classe ? (' — ' + classe) : '') + (p.juizo ? (' — ' + p.juizo) : '');
        },
        // Prioriza o rótulo OFICIAL do e-proc (`label`, montado no backend a partir
        // da triagem MCP — mesmo texto que aparece na tela de peticionamento, ex.:
        // "Expedida/certificada a intimação eletrônica - Sentença (21/07/2026
        // 13:58:05) - Ref. Evento 22 - EVERALDO COLACO"). Sem casamento (processo
        // sem triagem disponível), cai no formato aproximado antigo.
        prazoLabel(p) {
            if (p.label) return p.label;
            return (p.evento || 'Evento não identificado')
                 + (p.prazo_dias ? (' — ' + p.prazo_dias + ' dias') : '')
                 + (p.data_final ? (' — final ' + p.data_final + (p.estimado ? ' (estim.)' : '')) : '');
        },
        async carregarDocsPaj() {
            this.docsCarregados = true;
            if (!this.pajNorm) return;
            try {
                const resp = await fetch('/api/paj/' + encodeURIComponent(this.pajNorm) + '/movimentacoes');
                if (resp.ok) {
                    const d = await resp.json();
                    // Mais recente primeiro (maior sequencial no início do nome, ex.:
                    // "seq072 - 42532229 - envio de wpp.pdf" — ver sisdpu_client.py)
                    // no modal "Adicionar do SISDPU" — pedido do usuário 2026-10-06:
                    // os anexos que interessam normalmente são os últimos juntados ao
                    // PAJ, e a ordem crua (alfabética por nome de arquivo) exigia rolar
                    // a lista toda pra achá-los.
                    this.docsPaj = (d.anexos_sisdpu || []).slice().sort((a, b) => this._seqDe(b) - this._seqDe(a));
                }
            } catch (e) {}
        },
        // Extrai o nº de sequência do prefixo "seqNNN - ..." do nome do arquivo
        // — -1 (sempre por último) se não achar, pra nunca quebrar a ordenação
        // por causa de um arquivo fora do padrão (ex.: algo adicionado fora do
        // fluxo normal de sincronização do SISDPU).
        _seqDe(d) {
            const m = /^seq(\d+)/.exec((d && d.nome) || '');
            return m ? parseInt(m[1], 10) : -1;
        },
        async carregarPecasIa() {
            this.pecasIaCarregadas = true;
            if (!this.pajNorm) return;
            try {
                const j = await (await fetch('/api/acoes-eproc/pecas-ia/' + encodeURIComponent(this.pajNorm))).json();
                this.pecasIa = j.itens || [];
            } catch (e) {}
        },
        // `anexos_sisdpu` (mesma fonte da seção "Anexos SISDPU" da aba do PAJ):
        // {nome, tipo, tamanho, tem_ocr, caminho, caminho_ocr}.
        docKey(d) { return d.caminho || d.nome; },
        docLabel(d) { return d.nome || d.caminho || 'documento'; },
        linkArquivoPaj(d) { return '/files/' + this.pajNorm + '/' + (d.caminho || ''); },
        // Visualizar um anexo JÁ NA TABELA (não o documento do modal — ver
        // linkArquivoPaj): "Do PAJ" é um arquivo real no servidor (mesmo link do
        // modal, útil pra conferir o resultado depois de Comprimir/Dividir);
        // "Externo" ainda não foi enviado a lugar nenhum, então visualiza um blob
        // local do próprio arquivo escolhido (sem precisar subir nada ao servidor).
        linkVisualizarAnexo(a) {
            if (a.origem === 'paj') return a.docPajKey ? ('/files/' + this.pajNorm + '/' + a.docPajKey) : '';
            if (a.origem === 'rascunho') return a.nomeArquivo
                ? ('/api/acoes-eproc/rascunho/' + this.chaveRascunho() + '/arquivo/' + encodeURIComponent(a.nomeArquivo)) : '';
            if (a.origem === 'upload' && a.arquivo) {
                if (!a._blobUrl) a._blobUrl = URL.createObjectURL(a.arquivo);
                return a._blobUrl;
            }
            return '';
        },
        corBadgeTipo(tipo) {
            const t = (tipo || '').toLowerCase();
            if (t === 'pdf') return 'badge-error';
            if (t === 'doc' || t === 'docx') return 'badge-info';
            if (['jpg', 'jpeg', 'png', 'gif'].includes(t)) return 'badge-warning';
            return 'bg-base-200 text-base-content/60';
        },
        // Sigilo não tem opção "em branco" no select (não há item "Sigilo…") — todo
        // anexo já nasce com um valor real. Usa o padrão configurado SE ainda for
        // válido (existir na lista importada); senão cai no primeiro item da lista.
        sigiloPadraoEfetivo() {
            if (this.sigiloPadrao && this.sigilosTodos.includes(this.sigiloPadrao)) return this.sigiloPadrao;
            return this.sigilosTodos[0] || '';
        },

        // --- formatos/tamanhos aceitos pelo e-proc ---
        extensaoDe(nome) {
            const m = /\.([a-z0-9]+)$/i.exec(nome || '');
            return m ? m[1].toLowerCase() : '';
        },
        formatoAceito(nome) { return !!EPROC_FORMATOS_ACEITOS[this.extensaoDe(nome)]; },
        // texto curto p/ legenda: "Documentos (PDF, HTM, HTML, KML) até 11MB · ..."
        formatosLegenda() {
            const porCategoria = {};
            Object.entries(EPROC_FORMATOS_ACEITOS).forEach(([ext, spec]) => {
                (porCategoria[spec.categoria] = porCategoria[spec.categoria] || { exts: [], maxMB: spec.maxMB })
                    .exts.push(ext.toUpperCase());
            });
            return Object.entries(porCategoria)
                .map(([cat, v]) => cat + ' (' + v.exts.join(', ') + ') até ' + v.maxMB + 'MB')
                .join(' · ');
        },
        nomeAnexo(a) {
            if (a.origem === 'paj' || a.origem === 'rascunho') return a.docLabelCache;
            return a.arquivo ? a.arquivo.name : '';
        },
        tamanhoAnexo(a) {
            if (a.origem === 'paj' || a.origem === 'rascunho') return a.tamanho || 0;
            return a.arquivo ? a.arquivo.size : 0;
        },
        // Informativo — KB abaixo de 1MB, MB a partir daí (evita "28672 KB").
        formatarTamanho(bytes) {
            if (!bytes) return '—';
            if (bytes < 1024 * 1024) return Math.round(bytes / 1024 * 10) / 10 + ' KB';
            return Math.round(bytes / 1024 / 1024 * 10) / 10 + ' MB';
        },
        // {ok, msg} — null quando ainda não há arquivo pra validar (linha "Externo" vazia).
        validarAnexo(a) {
            const nome = this.nomeAnexo(a);
            if (!nome) return null;
            const ext = this.extensaoDe(nome);
            const spec = EPROC_FORMATOS_ACEITOS[ext];
            if (!spec) return { ok: false, msg: 'Formato ".' + (ext || '?') + '" não é aceito pelo e-proc.' };
            const tamanho = this.tamanhoAnexo(a);
            const limiteBytes = spec.maxMB * 1024 * 1024;
            if (tamanho && tamanho > limiteBytes) {
                const tamanhoMB = Math.round(tamanho / 1024 / 1024 * 10) / 10;
                return { ok: false, msg: 'Arquivo com ' + tamanhoMB + 'MB excede o limite de ' + spec.maxMB
                                        + 'MB para ' + spec.categoria + '.' };
            }
            return { ok: true };
        },
        // Algum anexo com tamanho acima do limite (ou formato inválido, defensivo —
        // já é bloqueado ao adicionar), OU sem Tipo selecionado (campo obrigatório pro
        // e-proc, nunca é sugerido automaticamente — ver nota em confirmarAdicaoDocs)
        // — em qualquer um desses casos, "Executar no e-proc" fica bloqueado.
        // Exceção: se o evento não aceita anexo (eventoSemAnexo), a lista fica só
        // guardada na tela mas NUNCA é considerada na execução (ver anexosParaEnviar)
        // — então um anexo inválido aqui não impede executar (ele nem vai ser usado).
        temAnexoInvalido() {
            if (this.eventoSemAnexo()) return false;
            return this.anexos.some(a => {
                const v = this.validarAnexo(a);
                if (v && !v.ok) return true;
                return !a.tipo;
            });
        },
        // Lista efetivamente enviada ao e-proc na execução real (Fase 4, ainda não
        // implementada): vazia sempre que o evento não aceita anexo, mesmo que a
        // tabela ainda mostre itens — eles ficam só "guardados" na tela pro caso do
        // usuário trocar de volta para um evento que aceite, mas NUNCA são
        // enviados/tentados junto com um evento que os rejeitaria (evita erro no e-proc).
        anexosParaEnviar() {
            return this.eventoSemAnexo() ? [] : this.anexos;
        },
        // "Comprimir/Dividir" só é possível para PDF de um documento JÁ NO PAJ (o
        // servidor precisa do arquivo em disco — um "Externo" ainda não foi enviado
        // a lugar nenhum) e só faz sentido quando o problema é TAMANHO (formato
        // inválido não se resolve comprimindo).
        podeComprimir(a) {
            if (a.origem !== 'paj') return false;
            const v = this.validarAnexo(a);
            return !!v && !v.ok && this.extensaoDe(this.nomeAnexo(a)) === 'pdf';
        },
        async comprimirEDividir(i) {
            const a = this.anexos[i];
            if (!a || a._comprimindo) return;
            a._comprimindo = true;
            try {
                const resp = await fetch('/api/acoes-eproc/comprimir-anexo', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paj_norm: this.pajNorm, caminho: a.docPajKey }),
                });
                const j = await resp.json();
                if (!j.ok) { alert('Falha ao comprimir: ' + (j.erro || 'erro desconhecido')); return; }
                await this.carregarDocsPaj(); // recarrega — já inclui os novos arquivos
                // Substitui o item original PELOS resultados NA MESMA POSIÇÃO (1 item
                // comprimido, ou várias "partes" em sequência se ainda não coubesse) —
                // preserva a ordem dos demais anexos (não empurra tudo pro fim da lista).
                const novosItens = (j.novos || []).map(n => ({
                    _id: this._novoAnexoId(), origem: 'paj', docPajKey: n.caminho, docLabelCache: n.nome, tamanho: n.tamanho,
                    arquivo: null, tipo: a.tipo, sigilo: a.sigilo, _comprimindo: false,
                }));
                const idxAtual = this.anexos.indexOf(a);
                if (idxAtual >= 0) this.anexos.splice(idxAtual, 1, ...novosItens);
                else this.anexos.push(...novosItens);
            } catch (e) {
                alert('Erro ao comprimir: ' + e);
            } finally {
                a._comprimindo = false;
            }
        },

        // --- modal "Adicionar do SISDPU" (mesma tabela da seção Anexos SISDPU do PAJ) ---
        async abrirModalDocs() {
            if (!this.docsCarregados) await this.carregarDocsPaj();
            this.modalSelecionados = {};
            this.modalDocsAberto = true;
        },
        fecharModalDocs() { this.modalDocsAberto = false; },
        confirmarAdicaoDocs() {
            const jaAdicionados = new Set(
                this.anexos.filter(a => a.origem === 'paj').map(a => a.docPajKey));
            const rejeitados = [];
            this.docsPaj.forEach(d => {
                const k = this.docKey(d);
                if (!this.modalSelecionados[k] || jaAdicionados.has(k)) return;
                if (!this.formatoAceito(this.docLabel(d))) { rejeitados.push(this.docLabel(d)); return; }
                this.anexos.push({
                    // Tipo NUNCA vem pré-preenchido — é uma classificação do CONTEÚDO do
                    // documento (ex.: "CÁLCULOS", "ATESTADO MÉDICO"), não algo que dê pra
                    // adivinhar por um "favorito" genérico. Usuário sempre escolhe explícito.
                    _id: this._novoAnexoId(), origem: 'paj', docPajKey: k, docLabelCache: this.docLabel(d), tamanho: d.tamanho || 0,
                    arquivo: null, tipo: '', sigilo: this.sigiloPadraoEfetivo(),
                    _comprimindo: false,
                });
            });
            this.modalDocsAberto = false;
            if (rejeitados.length) {
                alert('Formato não aceito pelo e-proc — não adicionado(s):\n' + rejeitados.join('\n'));
            }
        },

        // --- modal "Adicionar Petições IA" (subpasta "Peças IA" do PAJ) ---
        async abrirModalPecasIa() {
            if (!this.pecasIaCarregadas) await this.carregarPecasIa();
            this.modalPecasIaSelecionados = {};
            this.modalPecasIaAberto = true;
        },
        fecharModalPecasIa() { this.modalPecasIaAberto = false; },
        confirmarAdicaoPecasIa() {
            const jaAdicionados = new Set(
                this.anexos.filter(a => a.origem === 'paj').map(a => a.docPajKey));
            const rejeitados = [];
            this.pecasIa.forEach(d => {
                const k = this.docKey(d);
                if (!this.modalPecasIaSelecionados[k] || jaAdicionados.has(k)) return;
                if (!this.formatoAceito(this.docLabel(d))) { rejeitados.push(this.docLabel(d)); return; }
                this.anexos.push({
                    // Tipo NUNCA vem pré-preenchido — mesmo motivo de confirmarAdicaoDocs.
                    _id: this._novoAnexoId(), origem: 'paj', docPajKey: k, docLabelCache: this.docLabel(d), tamanho: d.tamanho || 0,
                    arquivo: null, tipo: '', sigilo: this.sigiloPadraoEfetivo(),
                    _comprimindo: false,
                });
            });
            this.modalPecasIaAberto = false;
            if (rejeitados.length) {
                alert('Formato não aceito pelo e-proc — não adicionado(s):\n' + rejeitados.join('\n'));
            }
        },

        // --- tabela de anexos (itens adicionáveis, numerados) ---
        // "Adicionar Externo" abre direto a janela de seleção de arquivos do sistema
        // (input file oculto, disparado por clique) — cada arquivo escolhido vira um
        // item na lista, já com o nome preenchido (sem precisar reabrir nada depois).
        // O atributo `accept` do input já sugere ao SO só os formatos aceitos; ainda
        // assim revalida aqui (o usuário pode escolher "todos os arquivos" no diálogo).
        adicionarExterno() {
            if (this.$refs.inputExterno) this.$refs.inputExterno.click();
        },
        onArquivosExternos(ev) {
            const arquivos = Array.from((ev.target && ev.target.files) || []);
            const rejeitados = [];
            arquivos.forEach(f => {
                if (!this.formatoAceito(f.name)) { rejeitados.push(f.name); return; }
                this.anexos.push({
                    _id: this._novoAnexoId(), origem: 'upload', docPajKey: '', docLabelCache: '',
                    arquivo: f, tipo: '', sigilo: this.sigiloPadraoEfetivo(),
                    _comprimindo: false,
                });
            });
            ev.target.value = ''; // permite escolher o mesmo arquivo de novo depois
            if (rejeitados.length) {
                alert('Formato não aceito pelo e-proc — não adicionado(s):\n' + rejeitados.join('\n'));
            }
        },
        removerAnexo(i) { this.anexos.splice(i, 1); },
        // Reordena a tabela de anexos (setas ▲/▼) — a ordem da lista é a ordem em que
        // os documentos entrarão na petição real (Fase 4). `direcao` é -1 (sobe) ou +1
        // (desce); splice+splice em vez de troca de índice por índice pra garantir que
        // o Alpine detecte a mudança de posição (mesmo padrão de comprimirEDividir).
        moverAnexo(i, direcao) {
            const j = i + direcao;
            if (j < 0 || j >= this.anexos.length) return;
            const item = this.anexos[i];
            this.anexos.splice(i, 1);
            this.anexos.splice(j, 0, item);
        },

        // --- "Executar no e-proc" (peticionamento real) — fluxo em 2 etapas: ---
        // 1) "Preparar no e-proc" — abre a tela e preenche evento+anexos de
        //    verdade (upload, Tipo, Sigilo, "Confirmar seleção de documentos"),
        //    SEM protocolar. 2) Só depois de revisar o resultado, "Peticionar
        //    de verdade" — clique real e IRREVERSÍVEL em "Peticionar" no e-proc.
        // Exige "Gravar" antes — a execução parte do rascunho salvo em disco
        // (garante que o que foi preenchido na tela é exatamente o que será
        // enviado, mesmo se a aba for recarregada entre os dois cliques).
        modalExecutarAberto: false, execEtapa: 'resumo', execResumo: null,
        execResultado: null, execErro: '',
        // Resultado do Gerenciamento de Processo (executado automaticamente, sem
        // perguntar, quando já veio configurado/ativado ao preparar a petição) e da
        // gravação da movimentação pendente no SISDPU (agora sob demanda, via botão
        // "Gravar movimentação para o SISDPU" no próprio resumo final, em vez de um
        // confirm() no meio do fluxo) — capturados aqui (em vez de só um alert() de
        // passagem) pra aparecerem no resumo final da etapa "confirmado" ("Executado
        // no eproc"), com sucesso/falha de cada ação (pedido do usuário 2026-08-19:
        // menos confirmações, mais rápido). null = ainda não aconteceu (gerenciamento
        // não configurado / peticionamento sem sucesso / botão de movimentação ainda
        // não clicado).
        execGerenciamento: null, execMovimentacaoSisdpu: null, gravandoMovSisdpu: false,
        _refEventoAtual: '',
        async abrirModalExecutar() {
            if (!this.rascunhoSalvoEm) {
                alert('Grave o rascunho ("Gravar") antes de executar — a execução real parte do que estiver salvo em disco.');
                return;
            }
            this.execEtapa = 'resumo'; this.execResumo = null; this.execResultado = null; this.execErro = '';
            this.execGerenciamento = null; this.execMovimentacaoSisdpu = null; this.gravandoMovSisdpu = false;
            this._refEventoAtual = '';
            this.modalExecutarAberto = true;
            try {
                const j = await (await fetch('/api/acoes-eproc/executar/' + encodeURIComponent(this.chaveRascunho()) + '/resumo')).json();
                if (!j.ok) { this.execErro = j.erro || 'Falha ao montar o resumo.'; return; }
                this.execResumo = j;
            } catch (e) {
                this.execErro = 'Erro de rede: ' + e;
            }
        },
        fecharModalExecutar() {
            if (this.execEtapa === 'preparando' || this.execEtapa === 'confirmando') return; // não fecha em operação
            const eraConfirmado = this.execEtapa === 'confirmado';
            this.modalExecutarAberto = false;
            // A página só recarrega quando o usuário FECHA o modal por conta própria,
            // depois de conferir o resumo final (peticionamento + gerenciamento +
            // movimentação pendente no SISDPU) — antes recarregava sozinha 1.2s após
            // "confirmado", o que apagava o resumo antes de dar tempo de ler, ainda
            // mais agora que ele tem 3 partes (pedido do usuário 2026-08-19). Esta é a
            // etapa de encerramento da execução — só ela dispara o reload.
            if (eraConfirmado) window.location.reload();
        },
        // Etapa 1 — preenche de verdade a tela do e-proc (evento + anexos),
        // sem protocolar. Pode demorar (upload real de cada anexo).
        async prepararNoEproc() {
            this.execErro = '';
            this.execEtapa = 'preparando';
            try {
                const resp = await fetch('/api/acoes-eproc/executar/' + encodeURIComponent(this.chaveRascunho()), { method: 'POST' });
                const j = await resp.json();
                if (!j.ok) { this.execErro = j.erro || 'Falha ao preparar no e-proc.'; this.execEtapa = 'resumo'; return; }
                this.execResultado = j;
                this.execEtapa = 'preparado';
            } catch (e) {
                this.execErro = 'Erro de rede: ' + e;
                this.execEtapa = 'resumo';
            }
        },
        // Etapa 2 — clique real em "Peticionar". Irreversível.
        async confirmarPeticionamentoReal() {
            this.execErro = '';
            this.execEtapa = 'confirmando';
            try {
                const resp = await fetch('/api/acoes-eproc/executar/' + encodeURIComponent(this.chaveRascunho()) + '/confirmar', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ processo: this.execResultado.processo }),
                });
                const j = await resp.json();
                if (!j.ok) { this.execErro = j.erro || 'Falha ao peticionar.'; this.execEtapa = 'preparado'; return; }
                this.execResultado = { ...this.execResultado, ...j };
                if (j.provavel_sucesso) {
                    this.rascunhoSalvoEm = ''; // rascunho foi descartado no servidor
                    // Guardado pro botão "Gravar movimentação para o SISDPU" do resumo
                    // final poder usar sob demanda (ver gravarMovimentacaoSisdpuNoResumo).
                    this._refEventoAtual = j.numero_evento ? ('Evento ' + j.numero_evento) : '';
                    // Gerenciamento de Processo: executa AUTOMATICAMENTE se já estava
                    // configurado/ativado ao preparar a petição ("Gerenciar processo
                    // depois de peticionar" + tipo/procurador, gravados no rascunho) —
                    // SEM perguntar de novo aqui. Essa decisão já foi tomada explicitamente
                    // pelo usuário antes de executar; confirmar outra vez é redundante e só
                    // atrasa o fluxo (pedido do usuário 2026-08-19: menos confirmações).
                    const ger = (this.execResumo && this.execResumo.gerenciamento) || {};
                    if (ger.ativo) {
                        const tipoLabel = (this.tiposGerenciamento.find(t => t.valor === ger.tipo) || {}).rotulo || ger.tipo;
                        this.execGerenciamento = { ...await this._executarGerenciamento(ger), tipoLabel };
                    }
                }
                // Etapa final do modal ("Executado no eproc") — mostra o que foi feito e
                // se houve sucesso/falha em cada ação já executada até aqui (peticionamento
                // + gerenciamento automático, se configurado). A movimentação no SISDPU
                // NÃO é mais perguntada/gravada aqui — o próprio resumo final tem um botão
                // "Gravar movimentação para o SISDPU" pra isso, sob demanda (pedido do
                // usuário 2026-08-19). A página só é atualizada (badges, prazos,
                // movimentações) quando o usuário FECHA este modal — ver fecharModalExecutar().
                this.execEtapa = 'confirmado';
            } catch (e) {
                this.execErro = 'Erro de rede: ' + e;
                this.execEtapa = 'preparado';
            }
        },
        // Botão "Gravar movimentação para o SISDPU" do resumo final ("Executado no
        // eproc") — sob demanda, chamado só quando o usuário clica (nada de
        // confirm() no meio do fluxo). Permite tentar de novo se a primeira
        // gravação falhar (o botão reaparece nesse caso — ver template).
        //
        // Em caso de SUCESSO, fecha este modal e recarrega a página inteira já
        // avisando (via sessionStorage — sobrevive ao reload) o console "Ações no
        // SISDPU" pra se abrir sozinho com o que acabou de ser gravado, pronto pra
        // conferir e executar — pedido do usuário 2026-08-20: antes o usuário tinha
        // que reabrir aquele console manualmente depois do refresh. Em caso de
        // FALHA, o modal continua aberto mostrando o erro (ver template) — só o
        // sucesso encerra o fluxo.
        async gravarMovimentacaoSisdpuNoResumo() {
            this.gravandoMovSisdpu = true;
            try {
                this.execMovimentacaoSisdpu = await this._prepararMovimentacaoSisdpu(this._refEventoAtual || '');
            } finally {
                this.gravandoMovSisdpu = false;
            }
            if (this.execMovimentacaoSisdpu && this.execMovimentacaoSisdpu.ok) {
                try { sessionStorage.setItem('painel_abrir_acoes_sisdpu', this.pajNorm); } catch (e) {}
                this.modalExecutarAberto = false;
                window.location.reload();
            }
        },
        // Monta a descrição da movimentação SÓ com dados OBJETIVOS do peticionamento
        // — SEM IA (removida a pedido do usuário 2026-08-13: a geração por IA
        // dependia do campo Petição estar preenchido com TEXTO, e falhava
        // silenciosamente — caindo num texto genérico, sem avisar — sempre que a
        // peça era um PDF pronto anexado em vez de texto digitado ali, como no caso
        // real que motivou a troca).
        //
        // 3 blocos, nesta ordem (pedido do usuário 2026-09-16, cada um separado
        // por linha em branco — o \n\n vira <p> próprio mais abaixo):
        //   1) conteúdo da petição protocolada (texto puro, sem a formatação rica
        //      da caixa "Petição" — o campo de movimentação do SISDPU não é
        //      CKEditor);
        //   2) dados do peticionamento — nº do evento do protocolo + nome do
        //      evento lançado (ex.: "Petição protocolada no e-proc — Evento 4
        //      (PETIÇÃO).");
        //   3) nome dos anexos da petição, se houver (evento de mera ciência
        //      nunca tem — eventoSemAnexo() já zera `nomesAnexos` antes de
        //      chegar aqui, ver _prepararMovimentacaoSisdpu).
        _montarDescricaoPosPeticionamento(refEvento, nomesAnexos) {
            const conteudoPeticao = htmlParaTextoSimples(this.peticaoEfetiva());
            const linhas = [];
            if (conteudoPeticao) linhas.push(conteudoPeticao);
            linhas.push('Petição protocolada no e-proc'
                + (refEvento ? (' — ' + refEvento) : '')
                + (this.evento ? (' (' + this.evento + ')') : '') + '.');
            if (nomesAnexos.length) linhas.push('Documentos anexados: ' + nomesAnexos.join(', ') + '.');
            return linhas.join('\n\n');
        },
        // Monta a descrição (ver acima) + todos os documentos protocolados como
        // anexo e GRAVA direto como rascunho do "Ações no SISDPU" (mesma persistência do botão "Gravar"
        // de lá — sobrevive a reload/fechar a aba, não depende do console do SISDPU
        // estar montado agora). Só depois avisa via evento — pra reagir AO VIVO se o
        // console já estiver na página (abrir e reler do disco), sem duplicar o
        // formato dos dados em dois lugares.
        async _prepararMovimentacaoSisdpu(refEvento) {
            // Defensivo — o botão que chama isto já fica oculto sem `pajNorm` (ver
            // template), mas sem PAJ não há "movimentação no SISDPU" possível
            // (petição avulsa, só pelo número do processo).
            if (!this.pajNorm) return { ok: false, mensagem: 'Esta petição não está vinculada a um PAJ — nada a gravar no SISDPU.' };
            const docs = (this.execResumo && this.execResumo.documentos) || [];
            const nomesAnexos = docs.map((d) => d.nome);
            const descricaoTexto = this._montarDescricaoPosPeticionamento(refEvento, nomesAnexos);

            // TODOS os documentos desta petição entram como anexo da movimentação no
            // SISDPU, já com Tipo de Arquivo pré-estabelecido como "Petições" (pedido
            // do usuário 2026-08-19 — antes só a petição em si entrava aqui; os
            // demais, embora já protocolados no e-proc, ficavam de fora e exigiam
            // reclassificar um por um se o usuário quisesse registrá-los também no
            // SISDPU). Resolvido no SERVIDOR (preparar-anexos-sisdpu) porque um
            // anexo "Externo" pode só existir na pasta de rascunhos de Ações no
            // Eproc (fora da pasta do PAJ) — precisa ser copiado pra dentro dela
            // antes de virar uma referência 'paj' válida pro SISDPU.
            // IMPORTANTE: erro aqui NÃO pode ficar em silêncio — era exatamente
            // esse `catch` vazio que escondia o bug real (2026-09-16): o PDF da
            // petição protocolada não chegava como anexo, sem aviso nenhum,
            // porque copiar o arquivo pra dentro da pasta do PAJ falhava (ex.:
            // antivírus/firewall bloqueando escrita) e a rota devolvia um 500
            // puro (texto, não JSON) — o `.json()` abaixo lançava, caía no
            // catch, e a função seguia como se nada tivesse acontecido.
            let anexosSisdpu = [];
            let avisoAnexos = '';
            try {
                const respAnexos = await fetch('/api/acoes-eproc/preparar-anexos-sisdpu/' + encodeURIComponent(this.pajNorm), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ documentos: docs }),
                });
                const jAnexos = await respAnexos.json();
                if (!jAnexos.ok) {
                    return { ok: false, mensagem: 'Falha ao preparar os anexos da petição para o SISDPU: ' + (jAnexos.erro || 'erro desconhecido') };
                }
                anexosSisdpu = jAnexos.anexos || [];
                const falhas = jAnexos.falhas || [];
                if (falhas.length) {
                    avisoAnexos = falhas.length + ' anexo(s) não puderam ser incluídos: '
                        + falhas.map((f) => f.nome + ' (' + f.motivo + ')').join('; ') + '.';
                }
            } catch (e) {
                return { ok: false, mensagem: 'Erro de rede ao preparar os anexos da petição para o SISDPU: ' + e };
            }
            const descricaoHtml = '<p>' + descricaoTexto.replace(/\n+/g, '</p><p>') + '</p>';

            // Fase da movimentação no SISDPU depende do evento lançado no e-proc: um
            // ato de mera ciência (eventoSemAnexo — "Ciência, com renúncia ao prazo")
            // é "Petição simples, De mera ciência ou por cota nos autos" no SISDPU,
            // não "Petição diversa com conteúdo jurídico" (essa é só pra petição de
            // conteúdo/mérito de verdade) — pedido do usuário 2026-08-19.
            const faseMov = this.eventoSemAnexo()
                ? 'Petição simples, De mera ciência ou por cota nos autos'
                : 'Petição diversa com conteúdo jurídico';

            try {
                const resp = await fetch('/api/acoes-sisdpu/rascunho/' + encodeURIComponent(this.pajNorm), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        paj_label: this.paj, fase: faseMov,
                        descricao_mov_html: descricaoHtml, anexos: anexosSisdpu,
                    }),
                });
                const j = await resp.json();
                if (!j.ok) return { ok: false, mensagem: j.erro || 'Falha ao gravar rascunho de movimentação no SISDPU: erro desconhecido' };
            } catch (e) {
                return { ok: false, mensagem: 'Erro de rede gravando o rascunho de movimentação no SISDPU: ' + e };
            }
            // Avisa o console "Ações no SISDPU" (se já estiver na página) pra se
            // abrir e reler o que acabou de ser gravado — sem carregar o payload
            // no próprio evento (fonte única: o rascunho no disco).
            window.dispatchEvent(new CustomEvent('eproc-peticionado', { detail: { pajNorm: this.pajNorm } }));
            // "ok" aqui é só "o RASCUNHO da movimentação foi gravado com sucesso" —
            // a movimentação em si ainda depende de "Executar no SISDPU" no console
            // ao lado (ou na página, após o reload). O resumo final deixa isso claro
            // (ver template do modal) pra não parecer que já foi executada de fato.
            // `aviso` (opcional): sucesso, mas 1+ anexo não pôde ser incluído —
            // exibido junto no resumo, nunca escondido.
            return { ok: true, fase: faseMov, aviso: avisoAnexos || '' };
        },

        // Executa de fato o Gerenciamento de Processo (substabelecimento) — chamado
        // só depois do Sim explícito em confirmarPeticionamentoReal(). IRREVERSÍVEL.
        // Devolve um resultado estruturado (em vez de um alert() de passagem) — o
        // resumo final da etapa "confirmado" mostra sucesso/falha/parcial de todas
        // as ações desta petição juntas (pedido do usuário 2026-08-19).
        async _executarGerenciamento(ger) {
            try {
                const resp = await fetch('/api/acoes-eproc/gerenciar', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        processo: this.execResultado.processo, tipo: ger.tipo,
                        procurador_valor: ger.procurador_valor || '',
                    }),
                });
                const j = await resp.json();
                if (!j.ok) return { ok: false, mensagem: j.erro || 'erro desconhecido' };
                // provavel_sucesso só é True se TODAS as partes marcadas mostrarem a
                // mensagem exata "Procurador cadastrado com sucesso no(s) processo(s)"
                // (checado por parte no backend — ver eproc_client.gerenciar_processo).
                // Qualquer parte com mensagem diferente entra em partes_com_erro e
                // precisa aparecer pro usuário, mesmo que outras partes do mesmo lote
                // tenham dado certo — nunca mascarar erro parcial atrás de sucesso.
                return {
                    ok: true, provavel_sucesso: !!j.provavel_sucesso,
                    partes_marcadas: j.partes_marcadas || [], partes_com_erro: j.partes_com_erro || [],
                };
            } catch (e) {
                return { ok: false, mensagem: 'Erro de rede: ' + e };
            }
        },
    };
}
