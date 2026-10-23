/* Leitura ao vivo pelo MCP do SIS-DPU 2.0 (services/ao_vivo_service.py).
 *
 * Só age quando a fonte de dados é "SIS-DPU 2.0 (MCP)" e o painel está
 * conectado. Na página do PAJ: relê o PAJ ao abrir e recarrega a página se algo
 * mudou. No dashboard: atualiza a caixa ao abrir e a cada 3 min com a aba visível
 * (o servidor limita a 1x por minuto) e avisa a lista para recarregar. O andamento aparece num toast no padrão do
 * painel (canto inferior direito, mesmo #toast-container de showToast).
 */
(function () {
    'use strict';

    let sumir = null;

    /* Toast único e atualizável (não empilha um por etapa): azul com indicador
     * girando enquanto lê, verde ao terminar (some em 5 s), vermelho em erro
     * (fica até o próximo status). */
    // `acao` (opcional): {rotulo, fn} — botão no próprio toast (ex.: "Interromper").
    function selo(texto, tom, acao) {
        const container = document.getElementById('toast-container');
        if (!container) return;
        let el = document.getElementById('ao-vivo-selo');
        if (!el) {
            el = document.createElement('div');
            el.id = 'ao-vivo-selo';
            el.style.transition = 'opacity .3s';
            container.appendChild(el);
        }
        const classe = tom === 'ok' ? 'alert-success' : tom === 'erro' ? 'alert-error' : 'alert-info';
        el.className = 'alert ' + classe + ' text-sm py-2 px-4 shadow-lg';
        el.innerHTML = (tom ? '' : '<span class="loading loading-spinner loading-xs"></span>') + '<span></span>';
        el.lastChild.textContent = texto;
        if (acao) {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'btn btn-xs';
            b.textContent = acao.rotulo;
            b.onclick = function () { b.disabled = true; acao.fn(); };
            el.appendChild(b);
        }
        el.style.opacity = '1';
        clearTimeout(sumir);
        if (tom === 'ok') {
            sumir = setTimeout(function () {
                el.style.opacity = '0';
                setTimeout(function () { if (el.style.opacity === '0') el.remove(); }, 300);
            }, 5000);
        }
    }

    async function postar(url) {
        const r = await fetch(url, { method: 'POST' });
        return r.json();
    }

    /* Chamado por sincronizarCaixa/sincronizarPaj (app.js) antes de começar.
     * Com a fonte SIS-DPU 2.0, as sincronizações manuais leem o histórico
     * INTEIRO — e o servidor tem teto de registros lidos por dia. */
    let estadoAtual = null;
    window.confirmarCustoMcp = function (tipo, nivel) {
        if (!estadoAtual || estadoAtual.fonte !== 'mcp') return true;
        const c = estadoAtual.consumo_hoje || {};
        const usado = (c.registros !== null && c.registros !== undefined)
            ? 'Hoje já foram lidos ' + c.registros + ' de ' + (c.teto || 5000) + ' registros (contador do servidor).'
            : 'Não consegui consultar o contador do servidor agora.';
        if (tipo === 'caixa') {
            return window.confirm('Fonte de dados: SIS-DPU 2.0 (MCP).\n\n'
                + 'A sincronização manual da caixa relê o histórico COMPLETO de cada PAJ com trâmite novo, '
                + 'e cada movimentação conta no limite diário do servidor (5.000 registros por pessoa, '
                + 'somando o painel e o uso do MCP pelo Claude). Uma caixa inteira pode consumir quase todo o limite.\n\n'
                + usado + '\n\nA leitura automática (ao abrir a caixa e cada PAJ) já mantém o painel atualizado. '
                + 'Continuar mesmo assim?'
                + (nivel === 'completa' ? '\n\n(Os PDFs de todos os PAJs também serão baixados pelo MCP.)' : ''));
        }
        return window.confirm('Fonte de dados: SIS-DPU 2.0 (MCP).\n\nA sincronização manual relê o histórico '
            + 'COMPLETO deste PAJ (~1 registro por movimentação do limite diário). ' + usado + '\n\nContinuar?');
    };

    /* Abrir o PAJ baixa pelo MCP TODOS os documentos que faltam na pasta (em
     * segundo plano no servidor) — o PAJ fica sempre em dia para as análises
     * (decisão do Defensor, 13/10/2026, revendo a de 11/10). Acompanha o
     * andamento; terminando com arquivo novo, atualiza a tabela de documentos. */
    async function baixarAnexos(pajNorm) {
        try { if (sessionStorage.getItem('ao-vivo-anexos-interrompido-' + pajNorm)) return; } catch (e) {}
        const base = '/api/sisdpu2/ao-vivo/paj/' + encodeURIComponent(pajNorm) + '/anexos';
        let r;
        try { r = await postar(base); } catch (e) { return; }
        if (!r || !r.ok || (!r.iniciado && r.motivo !== 'em_andamento')) return;
        const chave = 'ao-vivo-anexos-' + pajNorm;
        for (let i = 0; i < 900; i++) {  // até ~30 min
            await new Promise(function (ok) { setTimeout(ok, 2000); });
            let p;
            try { p = await (await fetch(base)).json(); } catch (e) { continue; }
            const pr = p.progresso || {};
            if (p.rodando) {
                selo('SIS-DPU 2.0 — baixando anexos'
                    + (pr.total ? ' ' + (pr.feitos || 0) + '/' + pr.total : '') + '…', null,
                    { rotulo: 'Interromper', fn: function () {
                        // Nesta aba, reabrir o PAJ não recomeça o download sozinho; a
                        // análise com IA ainda baixa o que faltar antes de rodar.
                        try { sessionStorage.setItem('ao-vivo-anexos-interrompido-' + pajNorm, '1'); } catch (e) {}
                        postar(base + '/interromper').catch(function () {});
                    } });
                continue;
            }
            if (p.interrompido) {
                selo('SIS-DPU 2.0 — download interrompido (' + (pr.baixados || 0) + ' baixado(s)); '
                    + 'o restante é baixado antes de uma análise com IA', 'ok');
                if ((pr.baixados || 0) > 0) window.dispatchEvent(new CustomEvent('anexos-paj-atualizados'));
                return;
            }
            const res = p.resultado || {};
            const novos = pr.baixados != null ? pr.baixados : (res.baixados || 0);  // progresso = só os novos
            if ((res.erros || []).length) {
                selo('SIS-DPU 2.0 — anexos: ' + novos + ' baixado(s), ' + res.erros.length + ' com erro', 'erro');
            } else {
                selo('SIS-DPU 2.0 — anexos em dia', 'ok');
            }
            // Arquivos novos: a tabela de documentos se atualiza no lugar (sem
            // recarregar a página inteira) — static/js/movimentacoes.js.
            if (novos > 0) window.dispatchEvent(new CustomEvent('anexos-paj-atualizados'));
            return;
        }
    }

    async function iniciar() {
        let estado;
        try { estado = await (await fetch('/api/sisdpu2/ao-vivo/estado')).json(); } catch (e) { return; }
        estadoAtual = estado;
        if (!estado || !estado.ativo) return;

        const caminho = window.location.pathname;
        const m = caminho.match(/^\/paj\/(PAJ-\d{4}-\d{3}-\d+)/);
        if (m) {
            const pajNorm = m[1];
            // Evita laço: depois de recarregar por mudança, não relê de novo nesta visita.
            const chave = 'ao-vivo-recarregado-' + pajNorm;
            const recemRecarregado = Date.now() - Number(sessionStorage.getItem(chave) || 0) < 60000;
            if (recemRecarregado) {
                selo('SIS-DPU 2.0 — PAJ atualizado', 'ok');
                // Anexos já baixados logo antes de abrir (pós-gravação, app.js:
                // releituraPosGravacaoMcp) — não confere de novo.
                const anexosEmDia = Date.now() - Number(sessionStorage.getItem('ao-vivo-anexos-em-dia-' + pajNorm) || 0) < 60000;
                if (!anexosEmDia) await baixarAnexos(pajNorm);
                return;
            }
            selo('SIS-DPU 2.0 — atualizando o PAJ…');
            try {
                const r = await postar('/api/sisdpu2/ao-vivo/paj/' + encodeURIComponent(pajNorm));
                if (r.ok && r.mudou) {
                    sessionStorage.setItem(chave, String(Date.now()));
                    selo('SIS-DPU 2.0 — novidades no PAJ, recarregando…');
                    setTimeout(function () { window.location.reload(); }, 600);
                    return;  // os anexos são baixados depois do recarregamento
                } else if (r.ok) {
                    selo('SIS-DPU 2.0 — PAJ em dia', 'ok');
                } else if (r.motivo === 'sincronizacao_em_andamento') {
                    selo('SIS-DPU 2.0 — sincronização em andamento; atualização ao vivo adiada', 'ok');
                } else {
                    selo('SIS-DPU 2.0 — não foi possível atualizar: ' + (r.motivo || 'erro'), 'erro');
                }
            } catch (e) {
                selo('SIS-DPU 2.0 — falha de rede ao atualizar', 'erro');
            }
            await baixarAnexos(pajNorm);
            return;
        }

        if (caminho === '/' || caminho === '') {
            await conferirCaixa(false);
            // Com a caixa aberta, confere de novo a cada INTERVALO_TELA_MS (1 registro
            // do teto diário por conferência; só relê a lista se algo mudou) — sem
            // isso, um trâmite novo só aparecia ao reabrir a tela ou no laço de
            // fundo de 10 min (14/10/2026, PAJ 2026/029-02674). Aba escondida não confere.
            setInterval(function () {
                if (document.visibilityState === 'visible') conferirCaixa(true);
            }, INTERVALO_TELA_MS);
        }
    }

    const INTERVALO_TELA_MS = 3 * 60 * 1000;
    let caixaMudouEm = null;

    /* `silencioso`: conferência periódica — só mostra o toast se a caixa mudou
     * ou se o limite diário estiver no fim. */
    async function conferirCaixa(silencioso) {
        if (!silencioso) selo('SIS-DPU 2.0 — conferindo a caixa…');
        try {
            const r = await postar('/api/sisdpu2/ao-vivo/caixa');
            // A caixa pode ter sido relida pelo laço de fundo do servidor desde a
            // última conferência desta tela — aí `mudou` vem false, mas a lista daqui
            // está velha.
            const relidaPorFora = r.ok && r.mudou_em && caixaMudouEm && r.mudou_em !== caixaMudouEm;
            if (r.mudou_em) caixaMudouEm = r.mudou_em;
            if (r.ok && r.mudou) {
                window.dispatchEvent(new CustomEvent('caixa-ao-vivo-atualizada'));
                const p = (r.resultado || {});
                selo('SIS-DPU 2.0 — caixa atualizada (' + (p.processados || 0) + ' PAJ(s) com novidade)', 'ok');
            } else if (relidaPorFora) {
                window.dispatchEvent(new CustomEvent('caixa-ao-vivo-atualizada'));
                if (!silencioso) selo('SIS-DPU 2.0 — caixa atualizada', 'ok');
            } else if (r.ok) {
                if (!silencioso) selo('SIS-DPU 2.0 — caixa em dia', 'ok');
            } else if (r.motivo === 'sincronizacao_em_andamento') {
                if (!silencioso) selo('SIS-DPU 2.0 — sincronização em andamento', 'ok');
            } else if (r.motivo === 'teto_diario_quase_atingido') {
                selo('SIS-DPU 2.0 — limite diário de leitura quase no fim; atualização pausada', 'erro');
            } else if (!silencioso) {
                selo('SIS-DPU 2.0 — não foi possível conferir a caixa: ' + (r.motivo || 'erro'), 'erro');
            }
        } catch (e) {
            if (!silencioso) selo('SIS-DPU 2.0 — falha de rede ao conferir a caixa', 'erro');
        }
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar);
    else iniciar();
})();
