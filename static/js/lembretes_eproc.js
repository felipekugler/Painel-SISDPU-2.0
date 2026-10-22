// Lembretes NATIVOS do e-proc de um processo judicial — componente Alpine
// reutilizável (partials/lembretes_eproc.html): página "Consultar processo" e
// bloco "Processo Judicial" da página do PAJ (um por processo vinculado).
//
// Leitura: /api/eproc-mcp/lembretes — ao vivo da página do processo (MCP, sem
// navegador), com a cor real de cada post-it; cache da aba Prazos como reserva.
// Criar/alterar/excluir: /api/eproc-mcp/lembretes/{criar,editar,excluir} — pelo
// navegador do e-proc (formulários "Novo/Alterar Lembrete", "Desativar
// Lembrete"); o backend devolve a lista atualizada e grava no cache.
//
function lembretesEprocApp(numero) {
    return {
        numeroLembretes: numero || '',
        lembretes: [], carregandoLembretes: false, fonteLembretes: '',
        excluindoLembrete: '',
        editandoLembrete: '', textoEdicaoLembrete: '', corEdicaoLembrete: '', salvandoLembrete: '',
        novoLembreteAberto: false, textoNovoLembrete: '', destinoNovoLembrete: 'todos',
        corNovoLembrete: '#efef8f', criandoLembrete: false,
        // Cores do post-it no e-proc (mesma tabela de `eproc_client.CORES_LEMBRETE`).
        coresLembrete: [
            {hex: '#efef8f', nome: 'Amarelo'}, {hex: '#87adcd', nome: 'Azul'}, {hex: '#f5b574', nome: 'Laranja'},
            {hex: '#a7eda7', nome: 'Verde'}, {hex: '#db8080', nome: 'Vermelho'},
        ],

        init() {
            if (this.numeroLembretes) this.carregar(this.numeroLembretes);
        },

        get ocupadoLembretes() {
            return this.criandoLembrete || !!this.salvandoLembrete || !!this.excluindoLembrete;
        },

        async carregar(numero) {
            if (numero !== undefined) this.numeroLembretes = (numero || '').trim();
            if (!this.numeroLembretes) return;
            this.novoLembreteAberto = false; this.editandoLembrete = '';
            this.carregandoLembretes = true;
            try {
                const r = await (await fetch('/api/eproc-mcp/lembretes?numero=' + encodeURIComponent(this.numeroLembretes))).json();
                this.lembretes = r.lembrete_itens || [];
                this.fonteLembretes = r.fonte || '';
            } catch (e) { this.lembretes = []; this.fonteLembretes = ''; }
            finally { this.carregandoLembretes = false; }
        },

        async _postLembrete(acao, corpo, msgFalha) {
            try {
                const resp = await fetch('/api/eproc-mcp/lembretes/' + acao, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({numero: this.numeroLembretes, ...corpo}),
                });
                const r = await resp.json();
                if (!resp.ok || !r.ok) { alert(r.erro || msgFalha); return false; }
                return true;
            } catch (e) { alert('Falha de rede ao falar com o e-proc.'); return false; }
        },

        abrirNovoLembrete() {
            this.novoLembreteAberto = true; this.textoNovoLembrete = '';
            this.destinoNovoLembrete = 'todos'; this.corNovoLembrete = '#efef8f';
        },

        async criarLembrete() {
            const texto = (this.textoNovoLembrete || '').trim();
            if (!texto || this.ocupadoLembretes) return;
            this.criandoLembrete = true;
            try {
                const ok = await this._postLembrete('criar',
                    {texto, destino: this.destinoNovoLembrete, cor: this.corNovoLembrete},
                    'Falha ao criar o lembrete no e-proc.');
                if (ok) { this.novoLembreteAberto = false; await this.carregar(); }
            } finally { this.criandoLembrete = false; }
        },

        abrirEdicaoLembrete(l) {
            this.editandoLembrete = l.texto; this.textoEdicaoLembrete = l.texto;
            this.corEdicaoLembrete = l.cor || '#efef8f';
        },

        async salvarLembrete(l) {
            const novo = (this.textoEdicaoLembrete || '').trim();
            if (!novo || this.ocupadoLembretes) return;
            if (novo === l.texto && this.corEdicaoLembrete === (l.cor || '#efef8f')) { this.editandoLembrete = ''; return; }
            this.salvandoLembrete = l.texto;
            try {
                const ok = await this._postLembrete('editar',
                    {texto: l.texto, texto_novo: novo, cor: this.corEdicaoLembrete},
                    'Falha ao alterar o lembrete no e-proc.');
                if (ok) { this.editandoLembrete = ''; await this.carregar(); }
            } finally { this.salvandoLembrete = ''; }
        },

        async excluirLembrete(l) {
            if (this.ocupadoLembretes) return;
            if (!confirm('Excluir este lembrete no e-proc?\n\n"' + l.texto + '"\n\nA exclusão não pode ser desfeita.')) return;
            this.excluindoLembrete = l.texto;
            try {
                await this._postLembrete('excluir', {texto: l.texto}, 'Falha ao excluir o lembrete no e-proc.');
            } finally {
                this.excluindoLembrete = '';
                await this.carregar();
            }
        },
    };
}
