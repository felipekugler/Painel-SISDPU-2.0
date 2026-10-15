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
            '<div class="modal-box w-[70vw] max-w-[70vw] max-h-[88vh] flex flex-col p-0">'
            + '  <div class="px-6 pt-5 pb-3 border-b border-base-300 shrink-0">'
            + '    <h3 id="ficha-mcp-titulo" class="font-bold text-lg"></h3>'
            + '  </div>'
            + '  <div id="ficha-mcp-campos" class="px-6 py-4 overflow-y-auto flex-1 text-sm"></div>'
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
        return tipo === 'tramitacao' ? 'Tramitação'
            : tipo === 'processo_judicial' ? 'Cadastro do Processo Judicial'
            : tipo === 'sincronizacao' ? 'Sincronização do Processo Judicial'
            : tipo === 'etapa' ? 'Etapa do PAJ' : 'Movimentação';
    }

    function abrir(p) {
        montar();
        aberta = p.id;
        document.getElementById('ficha-mcp-titulo').textContent = 'Confirmar ' + rotulo(p.tipo) + ' no SISDPU' + (p.paj ? ' — PAJ ' + p.paj : '');
        document.getElementById('ficha-mcp-campos').innerHTML = htmlFicha(p.ficha, p.paj, p.contexto, { essencial: true });
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
        const campos = [], anexos = [];
        let faseAnexos = '', emAnexos = false;
        (ficha || '').split('\n').forEach(function (linha) {
            // itens de lista sob "Anexos a juntar:" ("    - nome, tipo, tamanho")
            const item = linha.match(/^\s{3,}[-•*]\s*(.+)$/);
            if (item && emAnexos) { anexos.push(item[1].trim()); return; }
            const m = linha.match(/^\s{2,}([^:]{2,60}):\s?(.*)$/);
            if (!m) return;
            let rotuloCampo = m[1].replace(/\?$/, '').trim(), valor = m[2];
            emAnexos = false;
            // Anexos: SEPARADOS, no fim, uma linha por anexo (pedido do Defensor, 11/10/2026)
            if (/^anexos? na fase/i.test(rotuloCampo)) { faseAnexos = valor; return; }
            if (/^anexos? a juntar/i.test(rotuloCampo)) {
                emAnexos = true;
                valor.split(/\s*;\s*/).forEach(function (v) { if (v.trim()) anexos.push(v.trim()); });
                return;
            }
            if (/^PAJ$/i.test(rotuloCampo) && pajNumero) valor = pajNumero;
            if (/acompanhamento p[úu]blico/i.test(rotuloCampo)) rotuloCampo = 'Acompanhamento público';
            if (/sai da caixa de origem/i.test(rotuloCampo)) rotuloCampo = 'Sai da caixa de origem';
            if (/^descri[çc][ãa]o,/i.test(rotuloCampo)) rotuloCampo = 'Descrição';
            // Respostas "NÃO. <explicação do servidor>" / "SIM. ..." viram só Não / Sim.
            // Só o padrão do servidor ("NÃO." / "SIM." + explicação) — "Não informado" fica como está.
            if (/^\s*N[ÃA]O\s*(\.|$)/.test(valor) && !/^descri/i.test(rotuloCampo)) valor = 'Não';
            else if (/^\s*SIM\s*(\.|$)/.test(valor) && !/^descri/i.test(rotuloCampo)) valor = 'Sim';
            campos.push([rotuloCampo, valor, /descri/i.test(rotuloCampo) && /<\w/.test(valor)]);
        });
        return { campos: campos, anexos: anexos, faseAnexos: faseAnexos };
    }

    // Tipo do anexo como extensão ("pdf"), pedido do Defensor: do nome do arquivo,
    // ou do tipo MIME quando o nome não tiver extensão.
    function extensao(nome, mime) {
        const m = (nome || '').match(/\.([A-Za-z0-9]{1,6})$/);
        if (m) return m[1].toLowerCase();
        const sub = (mime || '').split('/')[1] || '';
        return sub.replace(/^vnd\..*officedocument\.wordprocessingml.*$/, 'docx').replace(/^msword$/, 'doc')
                  .replace(/^jpeg$/, 'jpg').toLowerCase();
    }

    function tamanhoLegivel(b) {
        if (!b && b !== 0) return '';
        if (b < 1024) return b + ' B';
        if (b < 1048576) return (b / 1024).toFixed(1).replace('.', ',') + ' KB';
        return (b / 1048576).toFixed(1).replace('.', ',') + ' MB';
    }

    // A ficha em HTML (texto escapado): dados da gravação e, por último, a tabela
    // de anexos. Usada no modal e no resumo da movimentação conjunta.
    // `contexto.anexos` (do painel): [{nome, descricao, bytes}] — a descrição que o
    // Defensor deu a cada anexo, que a ficha do servidor não repete.
    // `opcoes.essencial` (resumo da conjunta, pedido do Defensor 11/10/2026): só o que
    // precisa de conferência — fase, descrição, caixa de destino, prazo e anexos.
    // Fora: PAJ (já é o da tela), tipo de movimentação, acompanhamento público e
    // "sai da caixa de origem" (sempre os mesmos).
    const OCULTOS_ESSENCIAL = /^(PAJ|Tipo de movimenta[çc][ãa]o|Acompanhamento público|Sai da caixa de origem)$/i;
    function htmlFicha(ficha, pajNumero, contexto, opcoes) {
        const esc = function (s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; };
        const f = camposDaFicha(ficha, pajNumero);
        if ((opcoes || {}).essencial) {
            f.campos = f.campos.filter(function (c) { return !OCULTOS_ESSENCIAL.test(c[0]); })
                .map(function (c) { return /^Fases$/i.test(c[0]) ? ['Fase', c[1], c[2]] : c; });
        }
        let html = '<table class="table table-sm w-full"><tbody>' + f.campos.map(function (c) {
            return '<tr><th class="align-top whitespace-nowrap text-base-content/70 font-medium w-1/4">' + esc(c[0])
                + '</th><td class="align-top break-words">'
                + (c[2] ? '<div class="bg-base-200 rounded p-2">' + htmlSeguro(c[1]) + '</div>' : esc(c[1]))
                + '</td></tr>';
        }).join('') + '</tbody></table>';

        const doPainel = ((contexto || {}).anexos || []).filter(function (a) { return a && typeof a === 'object'; });
        let linhas = f.anexos.map(function (txt) {
            // "nome.pdf, application/pdf[, 1,5 KB]" — o nome pode ter vírgula: o tipo é o trecho com "/"
            const partes = txt.split(/,\s*/);
            const iTipo = partes.findIndex(function (x) { return /^[\w.+-]+\/[\w.+-]+$/.test(x); });
            const nome = iTipo > 0 ? partes.slice(0, iTipo).join(', ') : txt;
            const tipo = iTipo > 0 ? partes[iTipo] : '';
            const tam = iTipo > 0 ? partes.slice(iTipo + 1).join(', ') : '';
            const meu = doPainel.find(function (a) { return a.nome === nome; }) || {};
            return [nome, meu.descricao || '', extensao(nome, tipo), tam || tamanhoLegivel(meu.bytes)];
        });
        if (!linhas.length && doPainel.length) {
            linhas = doPainel.map(function (a) { return [a.nome, a.descricao || '', extensao(a.nome, ''), tamanhoLegivel(a.bytes)]; });
        }
        if (linhas.length) {
            html += '<div class="font-semibold mt-4 mb-1">Anexos</div>'
                + '<table class="table table-sm table-zebra w-full"><thead><tr>'
                + '<th>Arquivo</th><th>Descrição</th><th>Tipo</th><th class="text-right">Tamanho</th></tr></thead><tbody>'
                + linhas.map(function (l) {
                    return '<tr><td class="break-words">' + esc(l[0]) + '</td><td class="break-words">' + esc(l[1])
                        + '</td><td class="whitespace-nowrap">' + esc(l[2]) + '</td><td class="text-right whitespace-nowrap">'
                        + esc(l[3]) + '</td></tr>';
                }).join('') + '</tbody></table>';
        }
        return html;
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
    // Campos essenciais da ficha como [{rotulo, html}] (html já seguro) — para
    // telas que montam a própria tabela (ex.: lápis "Alterar" na descrição do
    // modal de mera ciência).
    function camposEssenciais(ficha, pajNumero) {
        const esc = function (s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; };
        return camposDaFicha(ficha, pajNumero).campos
            .filter(function (c) { return !OCULTOS_ESSENCIAL.test(c[0]); })
            .map(function (c) {
                return { rotulo: /^Fases$/i.test(c[0]) ? 'Fase' : c[0], html: c[2] ? htmlSeguro(c[1]) : esc(c[1]) };
            });
    }

    window.fichaMcp = { rotulo: rotulo, previa: function (p) { abrir(p); }, htmlFicha: htmlFicha,
                        camposEssenciais: camposEssenciais };

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar);
    else iniciar();
})();
