// Botão "Ver qualificação" da seção "Assistidos e Representantes Legais"
// (página do PAJ) — ver templates/partials/qualificacao_modal.html e
// services/qualificacao_service.py. Fonte única da qualificação do autor e do
// representante (pedido do Defensor, 30/10/2026).
function qualificacaoApp(pajNorm) {
    return {
        aberto: false, carregando: false, salvando: false, completando: false, erro: '', aviso: '',
        q: null, vinculos: [], eprocAutor: [], eprocRep: [],
        _timer: null,

        async abrir() {
            this.aberto = true; this.erro = ''; this.aviso = ''; this.carregando = true;
            try {
                const j = await (await fetch('/api/qualificacao/' + encodeURIComponent(pajNorm))).json();
                this.vinculos = j.vinculos || this.vinculos;
                this.eprocAutor = j.eproc_autor || this.eprocAutor;
                this.eprocRep = j.eproc_rep || this.eprocRep;
                if (!j.ok) { this.erro = j.erro || 'Falha ao montar a qualificação.'; return; }
                this.q = j.qualificacao;
            } catch (e) {
                this.erro = 'Erro de rede: ' + e;
            } finally {
                this.carregando = false;
            }
        },
        fechar() { if (!this.salvando) this.aberto = false; },

        async atualizar() {
            this.carregando = true; this.erro = ''; this.aviso = '';
            try {
                const j = await (await fetch('/api/qualificacao/' + encodeURIComponent(pajNorm) + '/atualizar', { method: 'POST' })).json();
                if (!j.ok) { this.erro = j.erro || 'Falha ao ler o SIS-DPU 2.0.'; return; }
                this.q = j.qualificacao;
                this.aviso = 'Cadastro relido do SIS-DPU 2.0 (o que você editou foi mantido).';
            } catch (e) {
                this.erro = 'Erro de rede: ' + e;
            } finally {
                this.carregando = false;
            }
        },

        async completarDocumentos() {
            this.completando = true; this.erro = ''; this.aviso = '';
            try {
                const j = await (await fetch('/api/qualificacao/' + encodeURIComponent(pajNorm) + '/completar-documentos', { method: 'POST' })).json();
                if (!j.ok) { this.erro = j.erro || 'Falha ao completar pelos documentos.'; return; }
                this.q = j.qualificacao;
                this.aviso = j.detalhe + ((j.preenchidos || []).length ? ' (' + j.preenchidos.join('; ') + ')' : '');
            } catch (e) {
                this.erro = 'Erro de rede: ' + e;
            } finally {
                this.completando = false;
            }
        },
        evidencia(p, campo) { return ((p.evidencias || {})[campo]) || ''; },
        // "IA: SOCIOECONOMICA.txt" -> "IA" no selo (arquivo e trecho ficam no title)
        fonteCurta(f) { return (f || '').startsWith('IA:') ? 'IA' : f; },
        corFonte(f) {
            if (f === 'editado') return 'badge-primary';
            if ((f || '').startsWith('IA:')) return 'badge-accent';
            return 'badge-ghost';
        },

        async salvar() {
            this.salvando = true; this.erro = '';
            try {
                const j = await (await fetch('/api/qualificacao/' + encodeURIComponent(pajNorm), {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.q),
                })).json();
                if (!j.ok) { this.erro = j.erro || 'Falha ao salvar.'; return; }
                this.q = j.qualificacao;
                showToast('Qualificação salva.', 'success');
            } catch (e) {
                this.erro = 'Erro de rede: ' + e;
            } finally {
                this.salvando = false;
            }
        },

        // Campo alterado à mão: marca a fonte como "editado" (Atualizar do
        // SIS 2 não sobrescreve) e recalcula representação/texto.
        editar(p, campo) {
            p.fontes = p.fontes || {};
            p.fontes[campo] = 'editado';
            this.previa();
        },
        editarRepresentacao() {
            this.q.representacao.editado = true;
            this.previa();
        },
        editarTexto() { this.q.texto_editado = true; },
        regenerarTexto() { this.q.texto_editado = false; this.previa(true); },

        previa(agora = false) {
            clearTimeout(this._timer);
            const rodar = async () => {
                try {
                    const j = await (await fetch('/api/qualificacao/' + encodeURIComponent(pajNorm) + '/previa', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.q),
                    })).json();
                    if (!j.ok) return;
                    const n = j.qualificacao;
                    this.q.representacao = n.representacao;
                    if (!this.q.texto_editado) this.q.texto = n.texto;
                    (n.pessoas || []).forEach((p, i) => { if (this.q.pessoas[i]) this.q.pessoas[i].lacunas = p.lacunas; });
                } catch (e) {}
            };
            if (agora) rodar(); else this._timer = setTimeout(rodar, 400);
        },

        adicionarRepresentante() {
            const autor = (this.q.pessoas || []).find((p) => p.papel === 'autor') || {};
            this.q.pessoas.push({
                id: '', papel: 'representante', representa: autor.id || '', vinculo: '', tipo_pessoa: 'PF',
                nome: '', cpf: '', rg: '', orgao_rg: '', nascimento: '', sexo: '', nacionalidade: 'brasileira',
                estado_civil: '', profissao: '', nome_mae: '', telefone: '', email: '', menor: false,
                endereco: { logradouro: '', numero: '', complemento: '', bairro: '', municipio: '', uf: '', cep: '' },
                faltando: [], lacunas: [], fontes: { nome: 'editado' },
            });
            this.previa();
        },
        removerPessoa(i) {
            if (!confirm('Remover esta pessoa da qualificação?')) return;
            this.q.pessoas.splice(i, 1);
            this.previa();
        },

        fonte(p, campo) { return (p.fontes || {})[campo] || ''; },
        copiarTexto() {
            navigator.clipboard.writeText(this.q.texto || '').then(() => showToast('Qualificação copiada.', 'success'));
        },
        rotuloPapel(p) {
            if (p.papel === 'autor') return 'Autor(a)' + (p.menor ? ' · menor' : '');
            return 'Representante' + (p.vinculo ? ' · ' + p.vinculo : '');
        },
    };
}
