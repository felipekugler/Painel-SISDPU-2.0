/* Leitura ao vivo pelo MCP do SIS-DPU 2.0 (services/ao_vivo_service.py).
 *
 * Só age quando a fonte de dados é "SIS-DPU 2.0 (MCP)" e o painel está
 * conectado. Na página do PAJ: relê o PAJ ao abrir e recarrega a página se algo
 * mudou. No dashboard: atualiza a caixa (o servidor limita a 1x a cada 3 min) e
 * avisa a lista para recarregar. Um selo discreto no canto mostra o andamento.
 */
(function () {
    'use strict';

    function selo(texto, tom) {
        let el = document.getElementById('ao-vivo-selo');
        if (!el) {
            el = document.createElement('div');
            el.id = 'ao-vivo-selo';
            el.style.cssText = 'position:fixed;left:16px;bottom:16px;z-index:60;padding:6px 12px;'
                + 'border-radius:999px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.25);'
                + 'background:var(--surface-2,#1f2937);color:var(--text,#e5e7eb);opacity:.92;'
                + 'transition:opacity .6s';
            document.body.appendChild(el);
        }
        el.textContent = texto;
        el.style.borderLeft = '4px solid ' + (tom === 'ok' ? '#22c55e' : tom === 'erro' ? '#ef4444' : '#38bdf8');
        el.style.opacity = '.92';
        if (tom === 'ok') setTimeout(function () { el.style.opacity = '0'; }, 4000);
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
        const usado = 'Hoje já foram lidos ~' + (c.registros || 0) + ' de ' + (c.teto || 5000) + ' registros.';
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

    /* Baixa pelo MCP os anexos que faltam na pasta do PAJ (em segundo plano no
     * servidor) e acompanha o andamento. Terminando com arquivo novo, recarrega
     * a página uma vez para a aba de anexos mostrá-los. */
    async function baixarAnexos(pajNorm) {
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
                    + (pr.total ? ' ' + (pr.feitos || 0) + '/' + pr.total : '') + '…');
                continue;
            }
            const res = p.resultado || {};
            if ((res.erros || []).length) {
                selo('SIS-DPU 2.0 — anexos: ' + (res.baixados || 0) + ' baixado(s), ' + res.erros.length + ' com erro', 'erro');
            } else {
                selo('SIS-DPU 2.0 — anexos em dia', 'ok');
            }
            const recem = Date.now() - Number(sessionStorage.getItem(chave) || 0) < 60000;
            if ((res.baixados || 0) > 0 && !recem) {
                sessionStorage.setItem(chave, String(Date.now()));
                setTimeout(function () { window.location.reload(); }, 1200);
            }
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
                await baixarAnexos(pajNorm);
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
            selo('SIS-DPU 2.0 — conferindo a caixa…');
            try {
                const r = await postar('/api/sisdpu2/ao-vivo/caixa');
                if (r.ok && r.mudou) {
                    window.dispatchEvent(new CustomEvent('caixa-ao-vivo-atualizada'));
                    const p = (r.resultado || {});
                    selo('SIS-DPU 2.0 — caixa atualizada (' + (p.processados || 0) + ' PAJ(s) com novidade)', 'ok');
                } else if (r.ok) {
                    selo('SIS-DPU 2.0 — caixa em dia', 'ok');
                } else if (r.motivo === 'sincronizacao_em_andamento') {
                    selo('SIS-DPU 2.0 — sincronização em andamento', 'ok');
                } else if (r.motivo === 'teto_diario_quase_atingido') {
                    selo('SIS-DPU 2.0 — limite diário de leitura quase no fim; atualização pausada', 'erro');
                } else {
                    selo('SIS-DPU 2.0 — não foi possível conferir a caixa: ' + (r.motivo || 'erro'), 'erro');
                }
            } catch (e) {
                selo('SIS-DPU 2.0 — falha de rede ao conferir a caixa', 'erro');
            }
        }
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar);
    else iniciar();
})();
