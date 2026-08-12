// Módulo Alpine reutilizável "Ações no Eproc" (peticionamento no processo judicial —
// EM CONSTRUÇÃO). Autocontido, mesmo padrão de acoes_sisdpu.js. Uso:
//   <div x-data="acoesEprocBlock({ processos: r.processos_judiciais, paj: r.paj,
//                                  pajNorm: r.paj_norm })">
//     {% include "partials/acoes_eproc.html" %}
//   </div>
//
// cfg:
//   processos (opcional) — lista de {numero,...} vinculados ao PAJ (para achar prazos).
//   paj       (opcional) — rótulo do PAJ.
//   pajNorm   (opcional) — para carregar os documentos já anexados ao PAJ (anexos).
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

        prazos: [], prazosSelecionados: {}, carregandoPrazos: false, prazosCarregados: false,
        evento: '', peticaoTexto: '',
        // Editor de texto rico (Quill) da Petição — montado sob demanda em
        // _carregarAoAbrir() (mesmo padrão lazy de prazos/docsPaj: não faz sentido
        // montar um Quill por linha da Retornos se o console nunca for aberto).
        // `quillPeticaoOk` começa otimista; só vira false se o Quill não carregar.
        uidPeticao: 'qpet-' + Math.random().toString(36).slice(2, 9),
        _editorPeticao: null, quillPeticaoOk: true, _montandoEditorPeticao: false,
        docsPaj: [], docsCarregados: false,
        // tabela de anexos — itens numerados, adicionados um a um:
        // {origem:'paj'|'upload'|'rascunho', docPajKey, docLabelCache, arquivo,
        //  nomeArquivo, tipo, sigilo} — 'rascunho' é um "Externo" já persistido no
        // servidor (ver gravarRascunho/carregarRascunho): sobrevive a um reload,
        // diferente de 'upload' puro (só um `File` na memória da aba).
        anexos: [],
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

        // preferências (favoritos=dropdown, frequentes=badges) — cache local
        eventosFav: [], eventosFreq: [], eventosTodos: [],
        tiposFav: [], tiposFreq: [], tiposTodos: [],
        sigilosTodos: [], sigiloPadrao: '',

        async init() {
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
            } catch (e) {}
        },
        eventosDropdown() { return this.eventosFav.length ? this.eventosFav : this.eventosTodos; },
        tiposDropdown() { return this.tiposFav.length ? this.tiposFav : this.tiposTodos; },
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
            if (!this.pajNorm) return;
            try {
                const j = await (await fetch('/api/acoes-eproc/rascunho/' + encodeURIComponent(this.pajNorm))).json();
                const r = j.rascunho;
                if (!r) return;
                if (r.processo) this.processoSelecionado = r.processo;
                if (r.evento) this.evento = r.evento;
                this.prazosSelecionados = r.prazos_selecionados || {};
                this._peticaoHtmlRascunho = r.peticao_html || '';
                this.anexos = (r.anexos || []).map((a) => ({ ...a, _comprimindo: false }));
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
                            const resp = await fetch('/api/acoes-eproc/rascunho/' + encodeURIComponent(this.pajNorm) + '/upload', {
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
                const resp = await fetch('/api/acoes-eproc/rascunho/' + encodeURIComponent(this.pajNorm), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        paj_label: this.paj,
                        processo: this.processoSelecionado || (this.processos[0] && this.processos[0].numero) || '',
                        evento: this.evento,
                        peticao_html: this.peticaoEfetiva(),
                        prazos_selecionados: this.prazosSelecionados,
                        anexos,
                    }),
                });
                const j = await resp.json();
                if (!j.ok) { alert('Falha ao gravar rascunho: ' + (j.erro || 'erro desconhecido')); return; }
                this.rascunhoSalvoEm = j.salvo_em;
                // 'upload' recém-persistidos passam a ser tratados como 'rascunho' na
                // tela — evita reenviar o arquivo se "Gravar" for clicado de novo, e
                // usa o link do servidor em vez do blob local (que pode expirar).
                this.anexos = this.anexos.map((a) => (a.origem === 'upload' && a._rascunhoNome)
                    ? { origem: 'rascunho', nomeArquivo: a._rascunhoNome,
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
            const tipoPeticao = this.tiposTodos.find((t) => this._normalizarTexto(t).includes('peticao')) || 'Petição';
            const semSigilo = this.sigilosTodos.find((s) => /sem sigilo/i.test(s)) || 'Sem Sigilo';
            // docLabelCache = o próprio nome real do arquivo (já vem com ".pdf" do
            // backend, ex.: "Petição-12-08-2026.pdf") — extensaoDe()/validarAnexo()
            // leem a extensão do nome, não do campo `arquivo`; sem isso, o e-proc
            // "não reconhece o formato" mesmo o arquivo sendo um PDF de verdade.
            this.anexos.unshift({
                origem: 'rascunho', nomeArquivo, docLabelCache: nomeArquivo,
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
        prazoLabel(p) {
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
                    this.docsPaj = d.anexos_sisdpu || [];
                }
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
                ? ('/api/acoes-eproc/rascunho/' + this.pajNorm + '/arquivo/' + encodeURIComponent(a.nomeArquivo)) : '';
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
                    origem: 'paj', docPajKey: n.caminho, docLabelCache: n.nome, tamanho: n.tamanho,
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
                    origem: 'paj', docPajKey: k, docLabelCache: this.docLabel(d), tamanho: d.tamanho || 0,
                    arquivo: null, tipo: '', sigilo: this.sigiloPadraoEfetivo(),
                    _comprimindo: false,
                });
            });
            this.modalDocsAberto = false;
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
                    origem: 'upload', docPajKey: '', docLabelCache: '',
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
        async abrirModalExecutar() {
            if (!this.rascunhoSalvoEm) {
                alert('Grave o rascunho ("Gravar") antes de executar — a execução real parte do que estiver salvo em disco.');
                return;
            }
            this.execEtapa = 'resumo'; this.execResumo = null; this.execResultado = null; this.execErro = '';
            this.modalExecutarAberto = true;
            try {
                const j = await (await fetch('/api/acoes-eproc/executar/' + encodeURIComponent(this.pajNorm) + '/resumo')).json();
                if (!j.ok) { this.execErro = j.erro || 'Falha ao montar o resumo.'; return; }
                this.execResumo = j;
            } catch (e) {
                this.execErro = 'Erro de rede: ' + e;
            }
        },
        fecharModalExecutar() {
            if (this.execEtapa === 'preparando' || this.execEtapa === 'confirmando') return; // não fecha em operação
            this.modalExecutarAberto = false;
        },
        // Etapa 1 — preenche de verdade a tela do e-proc (evento + anexos),
        // sem protocolar. Pode demorar (upload real de cada anexo).
        async prepararNoEproc() {
            this.execErro = '';
            this.execEtapa = 'preparando';
            try {
                const resp = await fetch('/api/acoes-eproc/executar/' + encodeURIComponent(this.pajNorm), { method: 'POST' });
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
                const resp = await fetch('/api/acoes-eproc/executar/' + encodeURIComponent(this.pajNorm) + '/confirmar', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ processo: this.execResultado.processo }),
                });
                const j = await resp.json();
                if (!j.ok) { this.execErro = j.erro || 'Falha ao peticionar.'; this.execEtapa = 'preparado'; return; }
                this.execResultado = { ...this.execResultado, ...j };
                this.execEtapa = 'confirmado';
                if (j.provavel_sucesso) {
                    this.rascunhoSalvoEm = ''; // rascunho foi descartado no servidor
                    // Pergunta se quer registrar o mesmo ato no SISDPU — nunca automático
                    // (movimentar cria um registro novo a cada execução, então só dispara
                    // com confirmação explícita do usuário, mesma cautela do próprio módulo
                    // Ações no SISDPU ao executar).
                    const refEvento = j.numero_evento ? ('Evento ' + j.numero_evento) : '';
                    if (confirm('Petição protocolada com sucesso no e-proc'
                            + (refEvento ? ' (' + refEvento + ').' : '.')
                            + '\n\nDeseja movimentar o SISDPU agora para registrar este ato?')) {
                        await this._prepararMovimentacaoSisdpu(refEvento);
                    }
                }
            } catch (e) {
                this.execErro = 'Erro de rede: ' + e;
                this.execEtapa = 'preparado';
            }
        },
        // Monta o texto (IA, resumo curto) + anexo (a própria petição) e GRAVA
        // direto como rascunho do "Ações no SISDPU" (mesma persistência do botão
        // "Gravar" de lá — sobrevive a reload/fechar a aba, não depende do console
        // do SISDPU estar montado agora). Só depois avisa via evento — pra reagir
        // AO VIVO se o console já estiver na página (abrir e reler do disco), sem
        // duplicar o formato dos dados em dois lugares. Best-effort: se a IA falhar
        // no resumo, cai num texto determinístico (fase e anexo sempre entram).
        async _prepararMovimentacaoSisdpu(refEvento) {
            const docs = (this.execResumo && this.execResumo.documentos) || [];
            const nomesAnexos = docs.map((d) => d.nome);
            let descricaoTexto = 'Protocolada petição no e-proc' + (refEvento ? ' (' + refEvento + ')' : '') + '.'
                + (nomesAnexos.length ? ' Documentos anexados: ' + nomesAnexos.join(', ') + '.' : '');
            try {
                const resp = await fetch('/api/acoes-eproc/resumo-pos-peticionamento', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        peticao_html: this.peticaoEfetiva(), anexos_nomes: nomesAnexos,
                        evento_referencia: refEvento,
                    }),
                });
                const j = await resp.json();
                if (j.ok && j.resumo) descricaoTexto = j.resumo;
            } catch (e) { /* mantém o texto determinístico */ }

            // A petição em si (o documento cujo Tipo lembra "petição" — normalmente
            // o 1º, mas resolvido pelo Tipo pra não depender só da ordem) vem
            // pré-anexada na movimentação — os demais anexos (provas/documentos de
            // apoio) NÃO se repetem aqui, já estão no e-proc; o SISDPU só precisa do
            // registro da peça protocolada.
            const peticaoDoc = docs.find((d) => this._normalizarTexto(d.tipo).includes('peticao')) || docs[0];
            const anexosSisdpu = peticaoDoc ? [{
                origem: 'paj', ref: peticaoDoc.nome, nome: peticaoDoc.nome, tamanho: peticaoDoc.tamanho || 0,
                tipo: 'pdf', descricao: 'Petição', tipoArquivo: 'Petições',
            }] : [];
            const descricaoHtml = '<p>' + descricaoTexto.replace(/\n+/g, '</p><p>') + '</p>';

            try {
                const resp = await fetch('/api/acoes-sisdpu/rascunho/' + encodeURIComponent(this.pajNorm), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        paj_label: this.paj, fase: 'Petição diversa com conteúdo jurídico',
                        descricao_mov_html: descricaoHtml, anexos: anexosSisdpu,
                    }),
                });
                const j = await resp.json();
                if (!j.ok) { alert('Falha ao gravar rascunho de movimentação no SISDPU: ' + (j.erro || 'erro desconhecido')); return; }
            } catch (e) {
                alert('Erro de rede gravando o rascunho de movimentação no SISDPU: ' + e);
                return;
            }
            // Avisa o console "Ações no SISDPU" (se já estiver na página) pra se
            // abrir e reler o que acabou de ser gravado — sem carregar o payload
            // no próprio evento (fonte única: o rascunho no disco).
            window.dispatchEvent(new CustomEvent('eproc-peticionado', { detail: { pajNorm: this.pajNorm } }));
        },
    };
}
