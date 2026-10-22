// Módulo Alpine "Minutar Ofício" (barra "Ações com IA" do PAJ) — ver
// services/oficio_sei_service.py. Três etapas, cada uma disparada pelo
// Defensor: (1) IA redige a partir da Orientação Diretiva → (2) revisão na
// tela → "Criar no SEI" (com confirmação) → (3) "Abrir para assinar", que abre
// o SEI já no documento. O painel não assina nem envia — envio é da assessoria.
//
// Uso:
//   <div x-data="oficioSeiApp('{{ paj_norm }}')" x-init="init()">
//     {% include "partials/oficio_sei.html" %}
//   </div>
function oficioSeiApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        status: 'idle', // idle|running|done|error
        erro: '',
        etapa: '',   // o que a IA está consultando agora (ex.: "Lendo EMAIL 1.pdf")
        _pollTimer: null,

        seiConectado: false,
        conectando: false,

        // Rascunho estruturado (seções do modelo de Ofício da COFE CTB)
        destinatario: '',
        assunto: '',
        referencia: '',
        vocativo: '',
        fechamento: '',
        observacoes: '',
        orientacaoUsada: '',
        uidCorpo: 'qoficio-' + Math.random().toString(36).slice(2, 9),
        _editorCorpo: null, quillOk: true, _montando: false,
        corpoTexto: '',

        gravando: false, gravado: false,
        confirmando: false,
        criando: false,
        erroCriar: '',
        criados: [],
        aguardandoSei: false,   // "Criar no SEI" esperando o login para continuar

        // Restrito (LGPD — "Informação pessoal"): sugerido pela IA, decidido aqui.
        restrito: false, motivoRestrito: '',
        // Edição de minuta já existente ("Abrir ofício nº") e reiteração.
        edicao: null,           // {documento_id, numero_sei, processo, versao}
        reiteracaoDe: '',
        abrir: { numero: '', processo: '', carregando: false, erro: '', assinado: null },
        _restringindoId: null,
        leitura: { aberta: false, carregando: false, titulo: '', html: '', erro: '', assinaturas: [] },

        async init() {
            await this.fetchStatus(true);
            if (this.status === 'running') this._iniciarPolling();
            window.addEventListener('message', (ev) => {
                if (ev.data === 'sei-login-ok') { this.seiConectado = true; this.conectando = false; }
            });
        },

        async fetchStatus(popular) {
            try {
                const resp = await fetch('/api/oficio-sei/' + encodeURIComponent(this.pajNorm) + '/status');
                const d = await resp.json();
                const anterior = this.status;
                this.status = d.status || 'idle';
                this.erro = d.erro || '';
                this.etapa = d.etapa || '';
                this.criados = d.criados || [];
                this.seiConectado = !!d.sei_conectado;
                // Só popula os campos no carregamento ou na TRANSIÇÃO pra done —
                // nunca durante polling já em done (sobrescreveria edição manual).
                if (d.rascunho && (popular || (this.status === 'done' && anterior !== 'done'))) {
                    await this._popular(d.rascunho);
                }
                if (this.criados.length || this.status !== 'idle') {
                    Alpine.store('acoesIaMenu').mostrarOficio = true;
                }
                if (popular) this._atualizarSituacao();
                if (this.criados.some((o) => o.juntada === 'running')) this._acompanharJuntada();
            } catch (e) { /* silencioso */ }
        },

        // Ofício já criado e ainda não visto como assinado: confere no SEI (só
        // leitura). Assinado, some o "conferir"/"Abrir para assinar".
        async _atualizarSituacao() {
            // Também os já assinados (22/10/2026): o servidor confere se o ofício
            // foi excluído/cancelado no SEI ou perdeu a assinatura.
            if (!this.seiConectado || !this.criados.some((o) => !o.excluido_no_sei && !o.cancelado_no_sei)) return;
            try {
                const j = await (await fetch('/api/oficio-sei/' + encodeURIComponent(this.pajNorm) + '/atualizar',
                                             { method: 'POST' })).json();
                if (j.criados) {
                    const antes = Object.fromEntries(this.criados.map((o) => [o.documento_id, o]));
                    const novosExcluidos = j.criados.filter((o) => (o.excluido_no_sei || o.cancelado_no_sei)
                        && !(antes[o.documento_id] || {}).excluido_no_sei && !(antes[o.documento_id] || {}).cancelado_no_sei);
                    this.criados = j.criados;
                    if (novosExcluidos.length) {
                        const retirou = novosExcluidos.some((o) => o.juntada_retirada_em);
                        showToast('Ofício ' + novosExcluidos.map((o) => 'nº ' + o.numero_sei).join(', ')
                                  + ' não está mais no SEI (excluído/cancelado)'
                                  + (retirou ? ' — o PDF dele saiu do rascunho de "Ações no SISDPU".' : '.'), 'warning');
                        if (retirou) window.dispatchEvent(new CustomEvent('acoes-sisdpu-recarregar', { detail: { pajNorm: this.pajNorm } }));
                    }
                }
                if (j.login) this.seiConectado = false;
            } catch (e) { /* silencioso */ }
        },

        // "Anexar ao PAJ" (21/10/2026): baixa o PDF do ofício assinado e deixa o
        // console "Ações no SISDPU" PRÉ-PREENCHIDO (fase "Juntada de documento",
        // descrição e o ofício como 1º anexo) — o Defensor junta os demais
        // anexos (PAJ, Peças IA, externos) e executa lá. Mesmo aviso que o
        // "Peticionar" do e-proc usa para o console reler o rascunho e abrir.
        async anexarAoPaj(o) {
            this._preparandoId = o.documento_id;
            try {
                const j = await (await fetch('/api/oficio-sei/' + encodeURIComponent(this.pajNorm) + '/preparar-juntada/'
                                             + encodeURIComponent(o.documento_id), { method: 'POST' })).json();
                if (!j.ok) {
                    if (j.login) this.seiConectado = false;
                    showToast(j.erro || 'Não foi possível preparar a juntada do ofício.', 'error');
                    return;
                }
                o.juntada = 'preparada'; o.erro_juntada = ''; o.juntada_arquivo = j.arquivo;
                window.dispatchEvent(new CustomEvent('acoes-sisdpu-recarregar', { detail: { pajNorm: this.pajNorm } }));
                const alvo = document.getElementById('acoesSisdpuPaj');
                const visivel = alvo && (alvo.firstElementChild || alvo);
                if (visivel) visivel.scrollIntoView({ behavior: 'smooth', block: 'start' });
                showToast((j.tinha_rascunho ? 'Ofício acrescentado ao rascunho que já estava em "Ações no SISDPU"'
                                            : 'Juntada preparada em "Ações no SISDPU"')
                          + ' — acrescente os demais anexos e clique em Executar.', 'success');
            } catch (e) {
                showToast('Erro de rede: ' + e, 'error');
            } finally {
                this._preparandoId = null;
            }
        },
        _preparandoId: null,
        _acompanharJuntada() {
            if (this._pollJuntada) return;
            this._pollJuntada = setInterval(async () => {
                await this.fetchStatus(false);
                if (!this.criados.some((o) => o.juntada === 'running')) {
                    clearInterval(this._pollJuntada); this._pollJuntada = null;
                    // Ficha recusada: a juntada volta a vazio, sem aviso (só cancela).
                    const feito = this.criados.find((o) => o.documento_id === this._anexandoId && o.juntada === 'ok');
                    this._anexandoId = null;
                    if (feito) {
                        showToast('Ofício anexado ao PAJ' + (feito.juntada_mov_seq ? ' — Mov ' + feito.juntada_mov_seq : '') + '.', 'success');
                        if (typeof window.atualizarMovimentacoesPajMcp === 'function') window.atualizarMovimentacoesPajMcp(this.pajNorm);
                    }
                }
            }, 3000);
        },

        // "Abrir no SEI" / "Abrir para assinar" (22/10/2026): abre na janela do
        // SEI controlada pelo painel, que entra sozinha com o usuário do SISDPU
        // (cofre). Cofre trancado: pede a senha-mestra; sem ela, a janela abre
        // no login para o login manual. Falha da janela: abre no navegador.
        _abrindoSeiId: null,
        async abrirNoSei(o) {
            if (!o || !o.link || this._abrindoSeiId) return;
            this._abrindoSeiId = o.documento_id;
            try {
                if (typeof window.garantirCofre === 'function') await window.garantirCofre();
                const j = await (await fetch('/api/sei/abrir-documento', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: o.link }),
                })).json();
                if (!j.ok) {
                    showToast((j.erro || 'Não consegui abrir a janela do SEI') + ' — abrindo no navegador.', 'warning');
                    window.open(o.link, '_blank', 'noopener');
                } else if (j.login_manual) {
                    showToast(j.aviso || 'Janela do SEI aberta na tela de login — entre por lá (cofre trancado).', 'info');
                } else {
                    showToast('Documento aberto na janela do SEI.', 'success');
                }
            } catch (e) {
                window.open(o.link, '_blank', 'noopener');
            } finally {
                this._abrindoSeiId = null;
            }
        },
        async lerOficio(o) {
            this.leitura = { aberta: true, carregando: true, html: '', erro: '', assinaturas: [],
                             titulo: 'Ofício ' + (o.numero_sei || '') + ' — ' + (o.destinatario || '') };
            try {
                const j = await (await fetch('/api/oficio-sei/' + encodeURIComponent(this.pajNorm) + '/ler/'
                                             + encodeURIComponent(o.documento_id))).json();
                if (!j.ok) { this.leitura.erro = j.erro || 'Não foi possível ler o ofício.'; if (j.login) this.seiConectado = false; }
                else { this.leitura.html = j.html; this.leitura.assinaturas = j.assinaturas || []; }
            } catch (e) {
                this.leitura.erro = 'Erro de rede: ' + e;
            } finally { this.leitura.carregando = false; }
        },

        async _montarEditor() {
            if (!this._editorCorpo && this.quillOk && !this._montando) {
                this._montando = true;
                await this.$nextTick();
                this._editorCorpo = criarEditorRico(this.uidCorpo, { placeholder: 'Corpo do ofício.' });
                this.quillOk = !!this._editorCorpo;
                if (this._editorCorpo) {
                    this._editorCorpo.quill.on('text-change', (d, o, source) => { if (source === 'user') this.gravado = false; });
                }
            }
        },
        async _popular(r) {
            this.destinatario = r.destinatario || '';
            this.assunto = r.assunto || '';
            this.referencia = r.referencia || '';
            this.vocativo = r.vocativo || 'Prezado(a) Senhor(a),';
            this.fechamento = r.fechamento || 'Atenciosamente,';
            this.observacoes = r.observacoes || '';
            this.orientacaoUsada = r.orientacao || '';
            this.restrito = !!r.restrito;
            this.motivoRestrito = r.motivo_restrito || '';
            this.edicao = r.edicao || null;
            this.reiteracaoDe = r.reiteracao_de || '';
            await this._montarEditor();
            if (this._editorCorpo) this._editorCorpo.setHTML(r.corpo_html || '');
            else this.corpoTexto = htmlParaTextoSimples(r.corpo_html || '');
            this.gravado = false;
        },
        _corpoHtml() {
            if (this._editorCorpo) return this._editorCorpo.getHTML();
            return paraHtmlSeTextoPuro(this.corpoTexto || '');
        },
        _rascunho() {
            return {
                destinatario: this.destinatario, assunto: this.assunto, referencia: this.referencia,
                vocativo: this.vocativo, fechamento: this.fechamento,
                corpo_html: this._corpoHtml(), observacoes: this.observacoes, orientacao: this.orientacaoUsada,
                restrito: this.restrito, motivo_restrito: this.motivoRestrito,
                edicao: this.edicao, reiteracao_de: this.reiteracaoDe,
            };
        },
        _limparCampos() {
            this.destinatario = this.assunto = this.referencia = this.observacoes = '';
            this.vocativo = this.fechamento = '';
            this.restrito = false; this.motivoRestrito = '';
            this.edicao = null; this.reiteracaoDe = '';
            if (this._editorCorpo) this._editorCorpo.setHTML('');
            this.corpoTexto = '';
        },

        // --- "Abrir ofício nº" (qualquer processo de ofícios da COFE CTB) ---
        // Minuta NÃO assinada → entra em edição (Salvar no SEI grava no mesmo
        // documento). Assinado → oferece "Reiterar" / "Usar como base".
        async abrirOficio() {
            const numero = (this.abrir.numero || '').trim();
            if (!numero) { this.abrir.erro = 'Informe o número SEI do ofício.'; return; }
            if (this.status === 'done' && !confirm('Há um rascunho de ofício na tela. Abrir outro ofício substitui o rascunho atual. Continuar?')) return;
            this.abrir.carregando = true; this.abrir.erro = ''; this.abrir.assinado = null;
            Alpine.store('acoesIaMenu').mostrarOficio = true;
            try {
                const j = await (await fetch('/api/oficio-sei/' + encodeURIComponent(this.pajNorm) + '/abrir', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ numero, processo: (this.abrir.processo || '').trim() }),
                })).json();
                if (!j.ok) { if (j.login) this.seiConectado = false; this.abrir.erro = j.erro || 'Não foi possível abrir o ofício.'; return; }
                if (j.assinado) { this.abrir.assinado = j.oficio; return; }
                this.status = 'done'; this.erro = '';
                await this._popular(j.rascunho);
                this.abrir.numero = ''; this.abrir.processo = '';
            } catch (e) {
                this.abrir.erro = 'Erro de rede: ' + e;
            } finally { this.abrir.carregando = false; }
        },

        // Reiterar (ofício assinado, sem resposta) / Usar como base: a IA redige um
        // NOVO ofício a partir do existente. Reiterar não exige orientação.
        async redigirComBase(oficio, modo) {
            const orientacao = this._orientacaoDiretiva();
            if (modo === 'base' && !orientacao.trim()) {
                this.status = 'error';
                this.erro = 'Para "Usar como base", escreva na Orientação diretiva o que muda no novo ofício (destinatário, pedido…).';
                return;
            }
            if (this.status === 'done' && !confirm('Há um rascunho de ofício na tela. Substituir pelo novo?')) return;
            this.abrir.assinado = null;
            await this._iniciarRedacao({ orientacao, base: { documento_id: oficio.documento_id, modo } });
        },

        // Restringir um ofício já criado (com confirmação — altera o SEI).
        async restringirOficio(o) {
            if (!confirm('Tornar restrito no SEI o Ofício ' + (o.numero_sei || '') + '?\n\nHipótese legal: Informação pessoal '
                         + '(art. 31, § 1º, I, da Lei 12.527/2011).')) return;
            this._restringindoId = o.documento_id;
            try {
                const j = await (await fetch('/api/oficio-sei/' + encodeURIComponent(this.pajNorm) + '/restringir/'
                    + encodeURIComponent(o.documento_id), {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ confirmado: true }),
                })).json();
                if (!j.ok) { if (j.login) this.seiConectado = false; showToast(j.erro || 'Não foi possível restringir.', 'error'); return; }
                o.nivel_acesso = 'restrito';
                showToast('Ofício ' + (o.numero_sei || '') + ' agora é restrito no SEI.', 'success');
            } catch (e) {
                showToast('Erro de rede: ' + e, 'error');
            } finally { this._restringindoId = null; }
        },

        // Caixa compartilhada "Orientação diretiva para análise da IA" — mesmo
        // acesso de minuta_ia.js::_orientacaoDiretiva (x-data ancestral).
        _orientacaoDiretiva() {
            const container = this.$el.parentElement && this.$el.parentElement.closest('[x-data]');
            if (!container) return '';
            const d = window.Alpine.$data(container);
            return (d && d.orientacaoDiretiva) || '';
        },

        async redigir() {
            const orientacao = this._orientacaoDiretiva();
            if (!orientacao.trim()) {
                Alpine.store('acoesIaMenu').mostrarOficio = true;
                this.status = 'error';
                this.erro = this.edicao
                    ? 'Escreva na caixa "Orientação diretiva" o que deve mudar neste ofício (ex.: "troque o prazo para 30 dias").'
                    : 'Escreva na caixa "Orientação diretiva" o que o ofício deve pedir e a quem '
                      + '(ex.: "oficiar a APS Cândido Lopes pedindo cópia do processo administrativo do NB ...").';
                return;
            }
            // Em edição, "Redigir de novo" REVISA o ofício aberto (continua salvando no mesmo documento).
            const corpo = this.edicao
                ? { orientacao, base: { documento_id: this.edicao.documento_id, modo: 'revisar' }, rascunho: this._rascunho() }
                : { orientacao };
            await this._iniciarRedacao(corpo);
        },
        async _iniciarRedacao(corpo) {
            Alpine.store('acoesIaMenu').mostrarOficio = true;
            this.erro = '';
            this.erroCriar = '';
            try {
                const resp = await fetch('/api/oficio-sei/' + encodeURIComponent(this.pajNorm) + '/redigir', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(corpo),
                });
                const j = await resp.json();
                if (!j.ok) { this.status = 'error'; this.erro = j.erro || 'Falha ao iniciar a redação.'; return; }
                this.status = 'running';
                this._iniciarPolling();
            } catch (e) {
                this.status = 'error'; this.erro = 'Erro de rede: ' + e;
            }
        },

        _iniciarPolling() {
            if (this._pollTimer) return;
            this._pollTimer = setInterval(async () => {
                await this.fetchStatus(false);
                if (this.status !== 'running') { clearInterval(this._pollTimer); this._pollTimer = null; }
            }, 4000);
        },

        async gravar() {
            this.gravando = true;
            try {
                const resp = await fetch('/api/oficio-sei/' + encodeURIComponent(this.pajNorm) + '/rascunho', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ rascunho: this._rascunho() }),
                });
                const j = await resp.json();
                this.gravado = !!j.ok;
                if (!j.ok) this.erroCriar = j.erro || 'Falha ao gravar.';
            } finally { this.gravando = false; }
        },

        async descartar() {
            if (!confirm('Descartar este rascunho de ofício?')) return;
            await fetch('/api/oficio-sei/' + encodeURIComponent(this.pajNorm) + '/descartar', { method: 'POST' });
            this.status = 'idle';
            this._limparCampos();
        },

        // Login no SEI (página da DPU) — mesmo fluxo do selo "SEI" do topo
        // (window.conectarSei, base.html). Se um "Criar no SEI" estava esperando
        // a conexão, segue direto para a confirmação.
        async conectarSei() {
            this.conectando = true;
            this.erroCriar = '';
            const ok = await window.conectarSei();
            this.conectando = false;
            this.seiConectado = ok;
            if (!ok) { this.erroCriar = 'O painel não ficou conectado ao SEI — tente "Conectar ao SEI" de novo.'; return; }
            if (this.aguardandoSei) { this.aguardandoSei = false; this.confirmando = true; }
            this._atualizarSituacao();
        },

        // Antes de criar: o painel CONFERE a conexão com o SEI (não só o token
        // guardado) e, se não houver, pede o login antes de seguir (16/10/2026).
        async pedirConfirmacao() {
            this.erroCriar = '';
            if (!this.seiConectado) {
                // Sem token: abre o login já neste clique (pop-up permitido) e segue sozinho.
                this.aguardandoSei = true;
                await this.conectarSei();
                return;
            }
            this.conectando = true;
            const ok = await window.garantirSei();
            this.conectando = false;
            this.seiConectado = ok;
            if (!ok) {
                this.aguardandoSei = true;
                this.erroCriar = 'A conexão com o SEI caiu — clique em "Conectar ao SEI"; o ofício segue para a criação logo depois do login.';
                return;
            }
            this.confirmando = true;
        },

        async criarNoSei() {
            this.confirmando = false;
            this.criando = true;
            this.erroCriar = '';
            try {
                const resp = await fetch('/api/oficio-sei/' + encodeURIComponent(this.pajNorm) + '/criar', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ rascunho: this._rascunho(), confirmado: true }),
                });
                const j = await resp.json();
                if (!j.ok) {
                    if (j.login) this.seiConectado = false;
                    this.erroCriar = j.erro || 'Falha ao criar no SEI.';
                    return;
                }
                this.criados = [...this.criados.filter((o) => o.documento_id !== j.oficio.documento_id), j.oficio];
                if (j.editado) showToast('Ofício ' + (j.oficio.numero_sei || '') + ' atualizado no SEI.', 'success');
                if (j.oficio.avisos && j.oficio.avisos.length) showToast(j.oficio.avisos.join(' | '), 'warning');
                this.status = 'idle';
                this._limparCampos();
            } catch (e) {
                this.erroCriar = 'Erro de rede: ' + e + ' — confira no SEI se o ofício foi criado antes de tentar de novo.';
            } finally { this.criando = false; }
        },

        dataHora(iso) {
            if (!iso) return '';
            const d = new Date(iso);
            return d.toLocaleDateString('pt-BR') + ' ' + d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
        },
    };
}
