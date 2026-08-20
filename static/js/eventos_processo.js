// Componente Alpine "tabela de eventos do processo" (timeline via MCP e-proc),
// standalone — usado pelo modal de seleção de documentos de "Gerar Minuta"
// (minuta_ia.html), em MODO SELEÇÃO (ver templates/partials/
// eventos_processo_tabela.html). Implementa o MESMO contrato de dados que a
// página "Consultar processo" (eproc_consultar.html) já usa com essa tabela
// (resultado/corEvento/eventosComDocPendente/baixarDocumentosEventos/
// linkArquivo/corBadgeDocumento/nomeDocumento/mostrarMaisEventos/carregando)
// — ver essa página pro componente "completo" (com lembretes, chave de
// acesso etc., que aqui não são necessários pro modal de seleção).
//
// Uso:
//   <div x-data="eventosProcessoBlock({ modoSelecao: true })" x-init="carregar(numero)">
//     {% include "partials/eventos_processo_tabela.html" %}
//   </div>
function eventosProcessoBlock(cfg) {
    cfg = cfg || {};
    return {
        numero: '',
        instancia: 1,
        todosEventos: false,
        carregando: false,
        erro: '',
        resultado: null,
        eventosLimit: 30,
        baixandoDocs: false,

        // Modo seleção (ver eventos_processo_tabela.html): true aqui —
        // clicar num badge de documento seleciona pra IA em vez de abrir/
        // baixar na hora. `selecionadosIA`: chave -> payload já pronto pro
        // body de POST /api/minutas/gerar (ver minuta_ia.js).
        modoSelecao: !!cfg.modoSelecao,
        selecionadosIA: new Map(),

        async carregar(numero, instancia) {
            this.numero = numero || this.numero;
            this.instancia = instancia || this.instancia || 1;
            this.eventosLimit = 30;
            this.selecionadosIA = new Map();
            await this.consultar();
        },

        async consultar() {
            const num = (this.numero || '').trim();
            if (!num) return;
            this.carregando = true;
            this.erro = '';
            try {
                const qs = new URLSearchParams({
                    numero: num, instancia: this.instancia, eventos: this.eventosLimit,
                    todos_eventos: this.todosEventos ? 'true' : 'false',
                });
                const resp = await fetch('/api/eproc-mcp/triagem?' + qs.toString());
                const r = await resp.json();
                if (!resp.ok || r.erro) { this.erro = r.erro || 'Falha ao consultar o processo.'; return; }
                this.resultado = r;
            } catch (e) {
                this.erro = 'Falha de rede ao consultar o e-proc (MCP).';
            } finally {
                this.carregando = false;
            }
        },

        async mostrarMaisEventos() {
            this.eventosLimit += 30;
            await this.consultar();
        },

        // Cor da linha do evento que originou uma intimação ativa — mesma
        // lógica de eproc_consultar.html (a triagem sempre devolve
        // `intimacoes` junto de `eventos`, mesmo endpoint).
        corEvento(ev) {
            if (!this.resultado) return '';
            const it = (this.resultado.intimacoes || []).find(
                (i) => i.ativa && String(i.evento_intimacao) === String(ev.numero)
            );
            if (!it) return '';
            return it.prazo_aberto ? 'var(--cor-intim-aberta)' : 'var(--cor-intim-aguardando)';
        },

        extDocumento(nomeArquivo) {
            const ext = (nomeArquivo || '').split('.').pop();
            return (ext && ext.length <= 5 ? ext : 'doc').toUpperCase();
        },
        nomeDocumento(nomeArquivo) {
            return (nomeArquivo || '').replace(/\.[^.]+$/, '') || nomeArquivo;
        },
        corBadgeDocumento(nomeArquivo) {
            const ext = this.extDocumento(nomeArquivo).toLowerCase();
            if (ext === 'pdf') return 'badge-error';
            if (['doc', 'docx', 'html'].includes(ext)) return 'badge-info';
            if (['jpg', 'jpeg', 'png', 'gif'].includes(ext)) return 'badge-warning';
            return 'badge-ghost';
        },
        linkArquivo(arq) {
            return '/api/eproc-mcp/arquivo?' + new URLSearchParams({
                numero: this.numero, arquivo: arq.arquivo,
            }).toString();
        },

        eventosComDocPendente() {
            return (this.resultado && this.resultado.eventos || []).filter(
                (ev) => (ev.documentos || []).length && !(ev.arquivos_baixados || []).length
            );
        },
        async baixarDocumentosEventos(evs) {
            if (!evs || !evs.length || this.baixandoDocs) return;
            const eventoIds = evs.map((ev) => String(ev.numero));
            this.baixandoDocs = true;
            try {
                const resp = await fetch('/api/eproc-mcp/baixar-documentos', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ numero: this.numero, instancia: this.instancia, evento_ids: eventoIds }),
                });
                const r = await resp.json();
                if (!resp.ok || r.erro) { this.erro = r.erro || 'Falha ao baixar documento(s).'; return; }
                const arquivosPorEvento = r.arquivos_por_evento || {};
                for (const ev of (this.resultado.eventos || [])) {
                    const arqs = arquivosPorEvento[String(ev.numero)];
                    if (arqs) ev.arquivos_baixados = arqs;
                }
            } catch (e) {
                this.erro = 'Falha de rede ao baixar documento(s).';
            } finally {
                this.baixandoDocs = false;
            }
        },

        // --- Seleção pra IA (modoSelecao) ---
        toggleSelecaoIA(chave, payload) {
            if (this.selecionadosIA.has(chave)) this.selecionadosIA.delete(chave);
            else this.selecionadosIA.set(chave, payload);
        },
        estaSelecionadoIA(chave) {
            return this.selecionadosIA.has(chave);
        },
    };
}
