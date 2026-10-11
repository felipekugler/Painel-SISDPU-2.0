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
            '<div class="modal-box max-w-3xl max-h-[88vh] flex flex-col p-0">'
            + '  <div class="px-6 pt-5 pb-3 border-b border-base-300 shrink-0">'
            + '    <h3 class="font-bold text-lg">Confirmar gravação no SISDPU</h3>'
            + '    <div id="ficha-mcp-sub" class="text-sm text-base-content/70 mt-1"></div>'
            + '  </div>'
            + '  <div class="px-6 py-4 overflow-y-auto flex-1 text-sm">'
            + '    <p class="mb-3">Será gravado no SISDPU <strong>exatamente</strong> isto:</p>'
            + '    <table class="table table-sm w-full"><tbody id="ficha-mcp-campos"></tbody></table>'
            + '    <details class="mt-3">'
            + '      <summary class="cursor-pointer text-xs text-base-content/60">Ver a ficha como o SIS-DPU 2.0 a devolveu (texto integral)</summary>'
            + '      <pre id="ficha-mcp-corpo" class="bg-base-200 rounded-lg p-3 mt-2 text-xs whitespace-pre-wrap break-words"></pre>'
            + '    </details>'
            + '  </div>'
            + '  <div class="px-6 pt-3 pb-5 border-t border-base-300 flex items-center gap-3 flex-wrap shrink-0">'
            + '    <div id="ficha-mcp-aviso" class="text-xs text-base-content/70 flex-1 min-w-[220px]"></div>'
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
        document.getElementById('ficha-mcp-sub').textContent =
            rotulo(p.tipo) + ' — PAJ ' + p.paj + ' · SIS-DPU 2.0 (MCP)';
        document.getElementById('ficha-mcp-corpo').textContent = p.ficha;
        preencherCampos(p.ficha);
        let resta = p.restam_seg;
        const aviso = document.getElementById('ficha-mcp-aviso');
        const pintar = function () {
            const m = Math.floor(Math.max(0, resta) / 60), s = Math.max(0, resta) % 60;
            aviso.textContent = 'Leia a ficha: é exatamente o que vai ao prontuário. Ela vence em '
                + m + ':' + String(s).padStart(2, '0') + ' — sem resposta, nada é gravado.';
        };
        pintar();
        clearInterval(relogio);
        relogio = setInterval(function () { resta -= 1; pintar(); if (resta <= 0) fechar(); }, 1000);
        const dlg = document.getElementById('ficha-mcp-fundo');
        if (!dlg.open) dlg.showModal();
    }

    function fechar() {
        clearInterval(relogio);
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

    // "  Rótulo: valor" de cada linha da ficha -> linha da tabela.
    function preencherCampos(ficha) {
        const tb = document.getElementById('ficha-mcp-campos');
        tb.innerHTML = '';
        (ficha || '').split('\n').forEach(function (linha) {
            const m = linha.match(/^\s{2,}([^:]{2,60}):\s?(.*)$/);
            if (!m) return;
            const tr = document.createElement('tr');
            const th = document.createElement('th');
            th.className = 'align-top whitespace-nowrap text-base-content/70 font-medium w-1/4';
            th.textContent = m[1].replace(/\?$/, '');
            const td = document.createElement('td');
            td.className = 'align-top break-words';
            if (/descri/i.test(m[1]) && /<\w/.test(m[2])) {
                const box = document.createElement('div');
                box.className = 'bg-base-200 rounded p-2';
                box.innerHTML = htmlSeguro(m[2]);
                td.appendChild(box);
            } else {
                td.textContent = m[2];
            }
            tr.append(th, td);
            tb.appendChild(tr);
        });
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
    window.fichaMcp = { rotulo: rotulo, previa: function (p) { abrir(p); } };

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar);
    else iniciar();
})();
