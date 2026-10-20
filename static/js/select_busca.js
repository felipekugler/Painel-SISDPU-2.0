/* Select com busca (combobox) — 19/10/2026.
 *
 * Basta marcar um <select> com o atributo `data-busca`: ele ganha um botão com a
 * mesma aparência (as classes do select são copiadas) que abre uma lista com campo
 * de busca. O <select> original continua no DOM, escondido, e segue sendo a fonte
 * da verdade — x-model, @change, :class, x-show e :disabled continuam funcionando
 * sem mudar nada no componente Alpine:
 *   - escolher um item grava em `select.value` e dispara `change` (é o evento que
 *     o x-model de <select> escuta);
 *   - um laço leve (250 ms) espelha no botão o texto escolhido, desabilitado,
 *     visibilidade e classes — o Alpine troca a seleção mexendo direto nas
 *     <option>, sem evento, então não há como só escutar.
 * Busca sem acento e sem maiúsculas, em qualquer parte do texto; várias palavras
 * = todas precisam aparecer. Teclado: setas, Enter, Esc, Tab; digitar com o botão
 * focado já abre buscando. Respeita <optgroup>.
 */
(function () {
    'use strict';

    const CSS = `
.cbx-nativo{position:absolute!important;width:1px!important;height:1px!important;min-width:0!important;
  padding:0!important;margin:0!important;opacity:0!important;pointer-events:none!important;overflow:hidden!important}
.cbx-gatilho{text-align:left;font-weight:400;cursor:pointer}
.cbx-gatilho .cbx-texto{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cbx-gatilho[disabled]{cursor:not-allowed}
.cbx-pop{position:fixed;z-index:10000;display:flex;flex-direction:column;
  background:oklch(var(--b1));color:oklch(var(--bc));border:1px solid oklch(var(--bc)/.2);
  border-radius:var(--rounded-btn,.5rem);box-shadow:0 10px 30px rgba(0,0,0,.25);overflow:hidden}
.cbx-pop input{margin:.4rem;flex:none}
.cbx-lista{overflow-y:auto;padding:0 .25rem .3rem;font-size:.85rem}
.cbx-grupo{padding:.35rem .5rem .15rem;font-size:.72rem;font-weight:600;opacity:.55;text-transform:uppercase;letter-spacing:.02em}
.cbx-item{padding:.3rem .5rem;border-radius:.3rem;cursor:pointer;line-height:1.25}
.cbx-item.cbx-ativo{background:oklch(var(--bc)/.1)}
.cbx-item.cbx-escolhido{font-weight:600;color:oklch(var(--p))}
.cbx-item.cbx-vazio{font-style:italic;opacity:.7}
.cbx-item.cbx-desab{opacity:.4;cursor:not-allowed}
.cbx-nada{padding:.5rem;font-size:.8rem;opacity:.6;font-style:italic}
.cbx-marca{background:oklch(var(--wa)/.45);color:inherit;border-radius:2px;padding:0}
`;

    const norm = s => (s || '').normalize('NFC').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
    const ligados = new Set();
    let aberto = null;   // { sel, gatilho, pop, input, lista, itens, ativo }

    function injetarCss() {
        if (document.getElementById('cbx-css')) return;
        const st = document.createElement('style');
        st.id = 'cbx-css';
        st.textContent = CSS;
        document.head.appendChild(st);
    }

    function classesDoSelect(sel) {
        return Array.from(sel.classList).filter(c => c !== 'cbx-nativo').join(' ');
    }

    function textoEscolhido(sel) {
        const o = sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex] : null;
        return o ? o.text : '';
    }

    function sincronizar(sel) {
        const g = sel._cbxGatilho;
        if (!g) return;
        const cls = classesDoSelect(sel) + ' cbx-gatilho';
        if (g.className !== cls) g.className = cls;
        const t = textoEscolhido(sel);
        if (g._texto.textContent !== t) g._texto.textContent = t;
        if (g.title !== t) g.title = t;
        if (g.disabled !== sel.disabled) g.disabled = sel.disabled;
        const oculto = sel.style.display === 'none' || sel.hidden;
        const disp = oculto ? 'none' : '';
        if (g.style.display !== disp) g.style.display = disp;
    }

    function ligar(sel) {
        if (sel._cbxGatilho || sel.multiple) return;
        const g = document.createElement('button');
        g.type = 'button';
        g._texto = document.createElement('span');
        g._texto.className = 'cbx-texto';
        g.appendChild(g._texto);
        sel.parentNode.insertBefore(g, sel);   // antes: um <label> em volta aponta para o botão
        sel.classList.add('cbx-nativo');
        sel.tabIndex = -1;
        sel.setAttribute('aria-hidden', 'true');
        sel._cbxGatilho = g;
        g.addEventListener('click', () => {
            if (aberto && aberto.sel === sel) fechar(true);
            else abrir(sel, '');
        });
        g.addEventListener('keydown', ev => {
            if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
            if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(ev.key)) {
                ev.preventDefault();
                abrir(sel, '');
            } else if (ev.key.length === 1) {
                ev.preventDefault();
                abrir(sel, ev.key);
            }
        });
        ligados.add(sel);
        sincronizar(sel);
    }

    // ---- lista ------------------------------------------------------------------
    function coletar(sel) {
        const out = [];
        for (const filho of sel.children) {
            if (filho.tagName === 'OPTGROUP') {
                const ops = Array.from(filho.querySelectorAll('option'));
                if (ops.length) out.push({ grupo: filho.label });
                ops.forEach(o => out.push({ op: o }));
            } else if (filho.tagName === 'OPTION') {
                out.push({ op: filho });
            }
        }
        return out;
    }

    function realcar(el, texto, termos) {
        const n = norm(texto);
        const marcas = new Array(texto.length).fill(false);
        termos.forEach(t => {
            let i = n.indexOf(t);
            while (t && i >= 0) {
                for (let k = i; k < i + t.length; k++) marcas[k] = true;
                i = n.indexOf(t, i + t.length);
            }
        });
        let i = 0;
        while (i < texto.length) {
            let j = i;
            while (j < texto.length && marcas[j] === marcas[i]) j++;
            const pedaco = document.createTextNode(texto.slice(i, j));
            if (marcas[i]) {
                const m = document.createElement('mark');
                m.className = 'cbx-marca';
                m.appendChild(pedaco);
                el.appendChild(m);
            } else {
                el.appendChild(pedaco);
            }
            i = j;
        }
    }

    function renderizar() {
        const a = aberto;
        const termos = norm(a.input.value).split(/\s+/).filter(Boolean);
        a.lista.innerHTML = '';
        a.itens = [];
        let grupoPendente = null;
        for (const e of coletar(a.sel)) {
            if (e.grupo !== undefined) { grupoPendente = e.grupo; continue; }
            const o = e.op;
            const n = norm(o.text);
            if (termos.length && !termos.every(t => n.includes(t))) continue;
            if (grupoPendente !== null) {
                const gh = document.createElement('div');
                gh.className = 'cbx-grupo';
                gh.textContent = grupoPendente;
                a.lista.appendChild(gh);
                grupoPendente = null;
            }
            const li = document.createElement('div');
            li.className = 'cbx-item';
            if (o.value === '') li.classList.add('cbx-vazio');
            if (o.disabled) li.classList.add('cbx-desab');
            if (o.selected && a.sel.selectedIndex >= 0) li.classList.add('cbx-escolhido');
            if (o.title) li.title = o.title;
            realcar(li, o.text, termos);
            const idx = a.itens.length;
            li.addEventListener('mousedown', ev => { ev.preventDefault(); escolher(idx); });
            li.addEventListener('mousemove', () => { if (a.ativo !== idx) marcarAtivo(idx, false); });
            a.lista.appendChild(li);
            a.itens.push({ op: o, el: li });
        }
        if (!a.itens.length) {
            const v = document.createElement('div');
            v.className = 'cbx-nada';
            v.textContent = 'Nada encontrado.';
            a.lista.appendChild(v);
        }
        let ini = termos.length ? a.itens.findIndex(it => !it.op.disabled)
                                : a.itens.findIndex(it => it.op.selected);
        if (ini < 0) ini = a.itens.findIndex(it => !it.op.disabled);
        marcarAtivo(ini, true);
    }

    function marcarAtivo(idx, rolar) {
        const a = aberto;
        if (!a) return;
        if (a.itens[a.ativo]) a.itens[a.ativo].el.classList.remove('cbx-ativo');
        a.ativo = idx;
        const it = a.itens[idx];
        if (it) {
            it.el.classList.add('cbx-ativo');
            if (rolar) it.el.scrollIntoView({ block: 'nearest' });
        }
    }

    function mover(delta) {
        const a = aberto;
        if (!a || !a.itens.length) return;
        let i = a.ativo;
        for (let n = 0; n < a.itens.length; n++) {
            i = Math.min(a.itens.length - 1, Math.max(0, i + delta));
            if (!a.itens[i].op.disabled) break;
            if (i === 0 || i === a.itens.length - 1) break;
        }
        if (!a.itens[i].op.disabled) marcarAtivo(i, true);
    }

    function escolher(idx) {
        const a = aberto;
        const it = a && a.itens[idx];
        if (!it || it.op.disabled) return;
        const sel = a.sel;
        fechar(true);
        if (sel.value !== it.op.value || sel.selectedIndex !== it.op.index) {
            sel.value = it.op.value;
            sel.selectedIndex = it.op.index;
            sel.dispatchEvent(new Event('input', { bubbles: true }));
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        }
        sincronizar(sel);
    }

    function posicionar() {
        const a = aberto;
        if (!a) return;
        const r = a.gatilho.getBoundingClientRect();
        if (!r.width && !r.height) { fechar(false); return; }
        const largura = Math.min(Math.max(r.width, 380), window.innerWidth - 16);
        const abaixo = window.innerHeight - r.bottom - 8;
        const acima = r.top - 8;
        const paraCima = abaixo < 220 && acima > abaixo;
        const altura = Math.min(360, Math.max(140, paraCima ? acima : abaixo));
        a.pop.style.width = largura + 'px';
        a.pop.style.maxHeight = altura + 'px';
        a.pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - largura - 8)) + 'px';
        if (paraCima) {
            a.pop.style.top = '';
            a.pop.style.bottom = (window.innerHeight - r.top + 2) + 'px';
        } else {
            a.pop.style.bottom = '';
            a.pop.style.top = (r.bottom + 2) + 'px';
        }
    }

    function abrir(sel, textoInicial) {
        if (sel.disabled) return;
        if (aberto) fechar(false);
        const g = sel._cbxGatilho;
        const pop = document.createElement('div');
        pop.className = 'cbx-pop';
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'input input-bordered input-xs';
        input.placeholder = 'Digite para filtrar…';
        input.autocomplete = 'off';
        input.spellcheck = false;
        input.value = textoInicial || '';
        const lista = document.createElement('div');
        lista.className = 'cbx-lista';
        lista.setAttribute('role', 'listbox');
        pop.appendChild(input);
        pop.appendChild(lista);
        // Dentro de um <dialog> aberto com showModal() a lista precisa morar no
        // próprio dialog (camada de topo); fora dele, no body (escapa de overflow).
        (g.closest('dialog[open]') || document.body).appendChild(pop);
        aberto = { sel, gatilho: g, pop, input, lista, itens: [], ativo: -1 };
        input.addEventListener('input', renderizar);
        input.addEventListener('keydown', ev => {
            if (ev.key === 'ArrowDown') { ev.preventDefault(); mover(1); }
            else if (ev.key === 'ArrowUp') { ev.preventDefault(); mover(-1); }
            else if (ev.key === 'PageDown') { ev.preventDefault(); mover(8); }
            else if (ev.key === 'PageUp') { ev.preventDefault(); mover(-8); }
            else if (ev.key === 'Enter') { ev.preventDefault(); ev.stopPropagation(); escolher(aberto.ativo); }
            else if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); fechar(true); }
            else if (ev.key === 'Tab') { fechar(false); }
        });
        posicionar();
        renderizar();
        input.focus();
        const fim = input.value.length;
        input.setSelectionRange(fim, fim);
    }

    function fechar(focarGatilho) {
        const a = aberto;
        if (!a) return;
        aberto = null;
        a.pop.remove();
        if (focarGatilho && a.gatilho.isConnected) a.gatilho.focus();
    }

    // ---- ciclo de vida ----------------------------------------------------------
    function varrer(raiz) {
        if (!raiz || !raiz.querySelectorAll) return;
        if (raiz.matches && raiz.matches('select[data-busca]')) ligar(raiz);
        raiz.querySelectorAll('select[data-busca]').forEach(ligar);
    }

    function iniciar() {
        injetarCss();
        varrer(document.body);
        let pendente = false;
        new MutationObserver(() => {
            if (pendente) return;
            pendente = true;
            requestAnimationFrame(() => { pendente = false; varrer(document.body); });
        }).observe(document.body, { childList: true, subtree: true });

        setInterval(() => {
            for (const sel of ligados) {
                if (!sel.isConnected) {
                    if (sel._cbxGatilho) sel._cbxGatilho.remove();
                    ligados.delete(sel);
                    continue;
                }
                sincronizar(sel);
            }
            if (aberto && !aberto.gatilho.isConnected) fechar(false);
        }, 250);

        document.addEventListener('mousedown', ev => {
            if (aberto && !aberto.pop.contains(ev.target) && ev.target !== aberto.gatilho
                && !aberto.gatilho.contains(ev.target)) fechar(false);
        }, true);
        window.addEventListener('resize', posicionar);
        document.addEventListener('scroll', ev => {
            if (aberto && !aberto.pop.contains(ev.target)) posicionar();
        }, true);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar);
    else iniciar();
})();
