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
        _editorPeticao: null, quillPeticaoOk: true,
        docsPaj: [], docsCarregados: false,
        // tabela de anexos — itens numerados, adicionados um a um:
        // {origem:'paj'|'upload', docPajKey, docLabelCache, arquivo, tipo, sigilo}
        anexos: [],
        modalDocsAberto: false, modalSelecionados: {},

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
            if (!this.prazosCarregados) await this.carregarPrazos();
            if (!this.docsCarregados) await this.carregarDocsPaj();
            if (!this._editorPeticao && this.quillPeticaoOk) {
                await this.$nextTick();
                this._editorPeticao = criarEditorRico(this.uidPeticao, {
                    placeholder: 'Texto da petição ou referência a um PDF já pronto (formato em definição — próxima fase).',
                });
                this.quillPeticaoOk = !!this._editorPeticao;
            }
        },
        // Texto/HTML efetivo da petição — usado quando o peticionamento real for
        // implementado (Fase 4). Por ora nada consome isto ainda.
        peticaoEfetiva() { return this._editorPeticao ? this._editorPeticao.getHTML() : this.peticaoTexto; },
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
        nomeAnexo(a) { return a.origem === 'paj' ? a.docLabelCache : (a.arquivo ? a.arquivo.name : ''); },
        tamanhoAnexo(a) { return a.origem === 'paj' ? (a.tamanho || 0) : (a.arquivo ? a.arquivo.size : 0); },
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
    };
}
