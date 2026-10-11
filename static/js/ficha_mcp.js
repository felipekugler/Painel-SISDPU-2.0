/* Modal padrão de confirmação de gravação no SISDPU (MCP do SIS-DPU 2.0).
 *
 * Toda gravação pelo MCP (movimentar, tramitar) PAUSA no servidor até o
 * Defensor ler a ficha integral e responder (services/aprovacao_mcp_service.py).
 * Este script, presente em todas as páginas, procura fichas aguardando e abre o
 * modal — venha a gravação do console de ações, da conjunta, do SAT, dos
 * dossiês, do CRPS, da RPV ou da mera ciência. Sem resposta até a ficha vencer,
 * nada é gravado.
 */
(function () {
    'use strict';

    let ativo = false;
    let aberta = null;      // id da ficha no modal
    let relogio = null;

    // Mesmos componentes (daisyUI <dialog class="modal">) e cores do tema dos demais
    // modais do painel (ex.: o do cofre). <dialog>.showModal() põe a ficha na
    // camada do topo: aberta depois do pedido de senha do cofre, fica POR CIMA dele
    // (com <div class="modal"> ela ficava escurecida atrás — Felipe, 11/10/2026).
    function montar() {
        if (document.getElementById('ficha-mcp-fundo')) return;
        const fundo = document.createElement('dialog');
        fundo.id = 'ficha-mcp-fundo';
        fundo.className = 'modal';
        fundo.addEventListener('cancel', function (e) { e.preventDefault(); });  // Esc não responde por você
        fundo.innerHTML =
            // Só os dados que serão gravados (pedido do Defensor, 11/10/2026).
            '<div class="modal-box max-w-3xl max-h-[88vh] flex flex-col p-0">'
            + '  <div class="px-6 pt-5 pb-3 border-b border-base-300 shrink-0">'
            + '    <h3 id="ficha-mcp-titulo" class="font-bold text-lg"></h3>'
            + '  </div>'
            + '  <div class="px-6 py-4 overflow-y-auto flex-1 text-sm">'
            + '    <table class="table table-sm w-full"><tbody id="ficha-mcp-campos"></tbody></table>'
            + '  </div>'
            + '  <div class="px-6 pt-3 pb-5 border-t border-base-300 flex items-center justify-end gap-3 shrink-0">'
            + '    <button type="button" id="ficha-mcp-nao" class="btn btn-sm btn-ghost">Não gravar</button>'
            + '    <button type="button" id="ficha-mcp-sim" class="btn btn-sm btn-success">Confirmar gravação</button>'
            + '  </div>'
            + '</div>';
        document.body.appendChild(fundo);
        document.getElementById('ficha-mcp-nao').onclick = function () { responder(false); };
        document.getElementById('ficha-mcp-sim').onclick = function () { responder(true); };
    }

    function rotulo(tipo) {
        return tipo === 'tramitacao' ? 'Tramitação' : 'Movimentação';
    }

    function abrir(p) {
        montar();
        aberta = p.id;
        document.getElementById('ficha-mcp-titulo').textContent = 'Confirmar ' + rotulo(p.tipo) + ' no SISDPU';
        preencherCampos(p.ficha, p.paj);
        // A ficha vence no servidor (~10 min): o modal fecha sozinho junto, sem gravar.
        clearInterval(relogio);
        relogio = setTimeout(fechar, Math.max(1, p.restam_seg) * 1000);
        const dlg = document.getElementById('ficha-mcp-fundo');
        if (!dlg.open) dlg.showModal();
    }

    function fechar() {
        clearTimeout(relogio);
        aberta = null;
        const f = document.getElementById('ficha-mcp-fundo');
        if (f && f.open) f.close();
    }

    // HTML da descrição (é o que vai ao prontuário) mostrado formatado, só com
    // marcação de texto — nada de script, link ou estilo.
    const PERMITIDAS = { P: 1, BR: 1, STRONG: 1, B: 1, EM: 1, I: 1, U: 1, UL: 1, OL: 1, LI: 1, SPAN: 1 };
    function htmlSeguro(html) {
        const doc = new DOMParser().parseFromString('<div>' + html + '</div>', 'text/html');
        const limpar = function (no) {
            Array.from(no.childNodes).forEach(function (f) {
                if (f.nodeType === 1) {
                    if (!PERMITIDAS[f.tagName]) { f.replaceWith(document.createTextNode(f.textContent)); return; }
                    Array.from(f.attributes).forEach(function (a) { f.removeAttribute(a.name); });
                    limpar(f);
                }
            });
        };
        const raiz = doc.body.firstChild;
        limpar(raiz);
        return raiz.innerHTML;
    }

    // "  Rótulo: valor" de cada linha da ficha -> [rótulo, valor, ehHtml], com os
    // ajustes de leitura pedidos pelo Defensor (11/10/2026): o PAJ pelo NÚMERO
    // (AAAA/UUU-NNNNN), não pelo id interno; "Acompanhamento público: Sim/Não".
    function camposDaFicha(ficha, pajNumero) {
        const campos = [];
        (ficha || '').split('\n').forEach(function (linha) {
            const m = linha.match(/^\s{2,}([^:]{2,60}):\s?(.*)$/);
            if (!m) return;
            let rotuloCampo = m[1].replace(/\?$/, '').trim(), valor = m[2];
            if (/^PAJ$/i.test(rotuloCampo) && pajNumero) valor = pajNumero;
            if (/acompanhamento p[úu]blico/i.test(rotuloCampo)) rotuloCampo = 'Acompanhamento público';
            if (/sai da caixa de origem/i.test(rotuloCampo)) rotuloCampo = 'Sai da caixa de origem';
            if (/^descri[çc][ãa]o,/i.test(rotuloCampo)) rotuloCampo = 'Descrição';
            // Respostas "NÃO. <explicação do servidor>" / "SIM. ..." viram só Não / Sim.
            if (/^\s*N[ÃA]O\b[.:]?/i.test(valor) && !/^descri/i.test(rotuloCampo)) valor = 'Não';
            else if (/^\s*SIM\b[.:]?/i.test(valor) && !/^descri/i.test(rotuloCampo)) valor = 'Sim';
            campos.push([rotuloCampo, valor, /descri/i.test(rotuloCampo) && /<\w/.test(valor)]);
        });
        return campos;
    }

    function preencherCampos(ficha, pajNumero) {
        const tb = document.getElementById('ficha-mcp-campos');
        tb.innerHTML = '';
        camposDaFicha(ficha, pajNumero).forEach(function (c) {
            const tr = document.createElement('tr');
            const th = document.createElement('th');
            th.className = 'align-top whitespace-nowrap text-base-content/70 font-medium w-1/4';
            th.textContent = c[0];
            const td = document.createElement('td');
            td.className = 'align-top break-words';
            if (c[2]) {
                const box = document.createElement('div');
                box.className = 'bg-base-200 rounded p-2';
                box.innerHTML = htmlSeguro(c[1]);
                td.appendChild(box);
            } else {
                td.textContent = c[1];
            }
            tr.append(th, td);
            tb.appendChild(tr);
        });
    }

    // A mesma tabela em HTML (texto escapado), para o resumo da movimentação conjunta.
    function htmlFicha(ficha, pajNumero) {
        const esc = function (s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
        return '<table class="table table-sm w-full"><tbody>' + camposDaFicha(ficha, pajNumero).map(function (c) {
            return '<tr><th class="align-top whitespace-nowrap text-base-content/70 font-medium w-1/4">' + esc(c[0])
                + '</th><td class="align-top break-words">'
                + (c[2] ? '<div class="bg-base-200 rounded p-2">' + htmlSeguro(c[1]) + '</div>' : esc(c[1]))
                + '</td></tr>';
        }).join('') + '</tbody></table>';
    }

    async function responder(aprovado) {
        const id = aberta;
        if (!id) return;
        fechar();
        try {
            await fetch('/api/sisdpu2/aprovacoes/' + encodeURIComponent(id), {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ aprovado: !!aprovado }),
            });
        } catch (e) { /* a gravação expira sozinha no servidor */ }
    }

    async function verificar() {
        if (!ativo || aberta) return;
        try {
            const d = await (await fetch('/api/sisdpu2/aprovacoes/pendentes')).json();
            const p = (d.pendentes || [])[0];
            if (p && p.restam_seg > 5) abrir(p);
        } catch (e) { /* tenta de novo no próximo ciclo */ }
    }

    async function iniciar() {
        try {
            const f = await (await fetch('/api/sisdpu2/fonte')).json();
            ativo = f.fonte === 'mcp';
        } catch (e) { ativo = false; }
        if (!ativo) return;
        verificar();
        setInterval(verificar, 1500);
    }

    // Exposto para o resumo da movimentação conjunta (aprovação antecipada).
    // `previa` só MOSTRA uma ficha (conferência visual); responder a ela não
    // grava nada, porque o servidor não conhece o id.
    window.fichaMcp = { rotulo: rotulo, previa: function (p) { abrir(p); }, htmlFicha: htmlFicha };

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar);
    else iniciar();
})();
