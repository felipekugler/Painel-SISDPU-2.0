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
            if (!this.seiConectado || !this.criados.some((o) => !o.assinado)) return;
            try {
                const j = await (await fetch('/api/oficio-sei/' + encodeURIComponent(this.pajNorm) + '/atualizar',
                                             { method: 'POST' })).json();
                if (j.criados) this.criados = j.criados;
                if (j.login) this.seiConectado = false;
            } catch (e) { /* silencioso */ }
        },

        // "Anexar ao PAJ": juntada do ofício assinado (PDF) no SISDPU — fase
        // "Juntada de documento", com a ficha de confirmação (modal global).
        async anexarAoPaj(o) {
            try {
                const j = await (await fetch('/api/oficio-sei/' + encodeURIComponent(this.pajNorm) + '/anexar/'
                                             + encodeURIComponent(o.documento_id), { method: 'POST' })).json();
                if (!j.ok) { showToast(j.erro || 'Não foi possível anexar o ofício.', 'error'); return; }
                o.juntada = 'running'; o.erro_juntada = '';
                this._anexandoId = o.documento_id;
                showToast('Anexando o ofício ao PAJ — confirme a ficha do SIS-DPU 2.0.', 'info');
                this._acompanharJuntada();
            } catch (e) { showToast('Erro de rede: ' + e, 'error'); }
        },
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
            };
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
            Alpine.store('acoesIaMenu').mostrarOficio = true;
            this.erro = '';
            this.erroCriar = '';
            const orientacao = this._orientacaoDiretiva();
            if (!orientacao.trim()) {
                this.status = 'error';
                this.erro = 'Escreva na caixa "Orientação diretiva" o que o ofício deve pedir e a quem '
                    + '(ex.: "oficiar a APS Cândido Lopes pedindo cópia do processo administrativo do NB ...").';
                return;
            }
            try {
                const resp = await fetch('/api/oficio-sei/' + encodeURIComponent(this.pajNorm) + '/redigir', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ orientacao }),
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
            this.destinatario = this.assunto = this.referencia = this.observacoes = '';
            this.vocativo = this.fechamento = '';
            if (this._editorCorpo) this._editorCorpo.setHTML('');
            this.corpoTexto = '';
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
                this.criados = [...this.criados, j.oficio];
                this.status = 'idle';
                this.destinatario = this.assunto = this.referencia = this.observacoes = '';
                this.vocativo = this.fechamento = '';
                if (this._editorCorpo) this._editorCorpo.setHTML('');
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
