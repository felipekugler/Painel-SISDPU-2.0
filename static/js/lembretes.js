// Lembretes do PAJ — cadastro pessoal de anotações coloridas, nos moldes da
// ferramenta "Lembretes" do e-proc (mini-painel de cards + modal único de
// criar/alterar). Backend: services/lembretes_service.py + rotas em
// routes/paj.py (/api/paj/<paj>/lembretes).
function lembretesApp(pajNorm) {
    return {
        pajNorm,
        lembretes: [],
        carregando: false,
        salvando: false,
        erro: '',
        editandoId: null,
        form: { descricao: '', cor: 'amarelo' },
        // Mesma paleta do modal "Cadastro de Lembretes Externos" do e-proc.
        cores: [
            { nome: 'amarelo', hex: '#efef8f' },
            { nome: 'azul', hex: '#87adcd' },
            { nome: 'laranja', hex: '#f5b574' },
            { nome: 'verde', hex: '#a7eda7' },
            { nome: 'vermelho', hex: '#db8080' },
        ],

        async init() {
            await this.carregar();
        },

        async carregar() {
            this.carregando = true;
            try {
                const r = await fetch(`/api/paj/${this.pajNorm}/lembretes`);
                const data = await r.json();
                this.lembretes = data.lembretes || [];
            } catch (e) {
                this.lembretes = [];
            } finally {
                this.carregando = false;
            }
        },

        corHex(nome) {
            const c = this.cores.find((c) => c.nome === nome);
            return c ? c.hex : '#efef8f';
        },

        abrirNovo() {
            this.editandoId = null;
            this.form = { descricao: '', cor: 'amarelo' };
            this.erro = '';
            this.$refs.dialogLembrete.showModal();
        },

        abrirEditar(l) {
            this.editandoId = l.id;
            this.form = { descricao: l.descricao, cor: l.cor };
            this.erro = '';
            this.$refs.dialogLembrete.showModal();
        },

        fecharModal() {
            this.$refs.dialogLembrete.close();
        },

        async salvar() {
            const descricao = (this.form.descricao || '').trim();
            if (!descricao) { this.erro = 'Descrição é obrigatória.'; return; }
            this.salvando = true;
            this.erro = '';
            try {
                const url = this.editandoId
                    ? `/api/paj/${this.pajNorm}/lembretes/${this.editandoId}`
                    : `/api/paj/${this.pajNorm}/lembretes`;
                const method = this.editandoId ? 'PUT' : 'POST';
                const r = await fetch(url, {
                    method,
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ descricao, cor: this.form.cor }),
                });
                const data = await r.json();
                if (!r.ok || data.ok === false) {
                    this.erro = data.erro || 'Falha ao salvar o lembrete.';
                    return;
                }
                this.fecharModal();
                await this.carregar();
                showToast('Lembrete salvo', 'success');
            } catch (e) {
                this.erro = 'Falha de rede ao salvar.';
            } finally {
                this.salvando = false;
            }
        },

        async remover(l) {
            if (!confirm('Excluir este lembrete? Esta ação não pode ser desfeita.')) return;
            try {
                const r = await fetch(`/api/paj/${this.pajNorm}/lembretes/${l.id}`, { method: 'DELETE' });
                const data = await r.json();
                if (!r.ok || data.ok === false) {
                    showToast('Falha ao excluir lembrete', 'error');
                    return;
                }
                await this.carregar();
                showToast('Lembrete excluído', 'info');
            } catch (e) {
                showToast('Falha de rede ao excluir', 'error');
            }
        },

        formatarDataHora(iso) {
            if (!iso) return '';
            const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
            return m ? `${m[3]}/${m[2]}/${m[1]} ${m[4]}:${m[5]}` : iso;
        },
    };
}
