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
            } catch (e) { /* silencioso */ }
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
            if (this._editorCorpo) this._editorCorpo.setHTML('');
            this.corpoTexto = '';
        },

        async conectarSei() {
            this.conectando = true;
            this.erroCriar = '';
            try {
                const j = await (await fetch('/api/sei/login')).json();
                if (!j.ok) { this.erroCriar = j.erro; this.conectando = false; return; }
                window.open(j.url, '_blank');
                // Fallback se o postMessage da aba de retorno não chegar.
                const t0 = Date.now();
                const timer = setInterval(async () => {
                    const s = await (await fetch('/api/sei/status')).json();
                    if (s.conectado || Date.now() - t0 > 300000) {
                        clearInterval(timer);
                        this.seiConectado = !!s.conectado;
                        this.conectando = false;
                    }
                }, 3000);
            } catch (e) {
                this.erroCriar = 'Erro de rede: ' + e; this.conectando = false;
            }
        },

        pedirConfirmacao() {
            this.erroCriar = '';
            if (!this.seiConectado) { this.erroCriar = 'Conecte o painel ao SEI antes de criar o ofício.'; return; }
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
