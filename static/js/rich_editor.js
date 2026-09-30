// Fábrica de editor de texto rico (Quill, tema "snow") compartilhada pelos módulos
// "Ações no SISDPU" e "Ações no Eproc" — negrito/itálico/sublinhado, cor de texto/
// fundo, citação, listas, recuo e alinhamento. Quill é carregado via CDN em
// base.html; este arquivo não depende de mais nada, só encapsula a fábrica +
// leitura/escrita de conteúdo pra não duplicar essa lógica nos dois módulos.
//
// criarEditorRico(elId, opts?) -> {quill, getHTML, setHTML, insertHTML, setText, isEmpty, focus} | null
//   elId               — id de um elemento já existente no DOM e VISÍVEL (não
//                         display:none — Quill mede o container ao montar).
//   opts.toolbar        — array de grupos de botões (default: RICH_EDITOR_TOOLBAR_PADRAO).
//   opts.placeholder    — texto de placeholder.
//   opts.valorInicial   — HTML inicial (opcional).
//   opts.valorInicialTexto — TEXTO PURO inicial (opcional; ignorado se valorInicial
//                         também for passado). Usa quill.setText() em vez de inserir
//                         como HTML — não precisa escapar nada, e quebras de linha
//                         (\n) virão certas. É o certo pra semear com texto que não é
//                         HTML de verdade (ex.: uma sugestão da IA em texto puro).
// Devolve null se o Quill não carregou (CDN fora do ar, elemento inexistente etc.)
// — quem chama deve ter um fallback (textarea simples) pronto pra esse caso.
const RICH_EDITOR_TOOLBAR_PADRAO = [
    ['bold', 'italic', 'underline'],
    [{ color: [] }, { background: [] }],
    ['blockquote'],
    [{ list: 'ordered' }, { list: 'bullet' }],
    [{ indent: '-1' }, { indent: '+1' }],
    [{ align: [] }],
];

function criarEditorRico(elId, opts) {
    opts = opts || {};
    if (!window.Quill) return null;
    const el = document.getElementById(elId);
    if (!el) return null;
    let quill;
    try {
        quill = new Quill(el, {
            theme: 'snow',
            placeholder: opts.placeholder || '',
            modules: { toolbar: opts.toolbar || RICH_EDITOR_TOOLBAR_PADRAO },
        });
    } catch (e) {
        return null;
    }
    if (opts.valorInicial) quill.clipboard.dangerouslyPasteHTML(opts.valorInicial);
    else if (opts.valorInicialTexto) quill.setText(opts.valorInicialTexto);
    _ativarListaAlfabetica(quill);
    return {
        quill: quill,
        // Um editor "vazio" no Quill ainda emite '<p><br></p>' — normaliza pra
        // string vazia (mais limpo pra enviar/checar em cima).
        getHTML() {
            const html = quill.root.innerHTML;
            return html === '<p><br></p>' ? '' : html;
        },
        setHTML(html) {
            quill.setContents([]);
            if (html) quill.clipboard.dangerouslyPasteHTML(html);
        },
        // Insere HTML na posição do CURSOR (ou no fim, se o editor não estiver
        // focado/sem seleção) — diferente de setHTML(), NÃO apaga o que já
        // estava lá. Usado por "Usar modelo" (Ações no Eproc): o usuário pode
        // compor mais de um modelo, ou complementar com texto próprio antes.
        //
        // O `insertText('\n')` no final é NECESSÁRIO, não cosmético: o Quill
        // funde a ÚLTIMA linha do HTML colado com o que já vinha DEPOIS do
        // cursor, mesmo quando o HTML colado termina num bloco `<p>` fechado
        // (bug real 2026-09-16, confirmado isolado: colar "...</p>" bem antes
        // de um texto pré-existente vira "...texto pré-existente" na MESMA
        // linha, sem quebra — só o meio do HTML colado preserva parágrafos
        // em branco corretamente). Mede quanto foi inserido (comprimento do
        // documento antes/depois do paste) pra saber onde inserir a quebra.
        insertHTML(html) {
            if (!html) return;
            const sel = quill.getSelection(true); // true = foca o editor antes de ler a seleção
            const index = sel ? sel.index : quill.getLength();
            const lenAntes = quill.getLength();
            quill.clipboard.dangerouslyPasteHTML(index, html);
            const inserido = quill.getLength() - lenAntes;
            quill.insertText(index + inserido, '\n', 'user');
        },
        // Substitui TUDO por texto puro (sem HTML) — usado pra "puxar" uma fonte
        // externa (ex.: botão "Atualizar da Sugestão IA") de forma explícita, a
        // pedido do usuário — nunca automático depois da primeira carga.
        setText(texto) { quill.setText(texto || ''); },
        isEmpty() { return quill.getText().trim().length === 0 && !quill.root.querySelector('img'); },
        focus() { quill.focus(); },
    };
}

// "Lista" alfabética (a) b) c)...) — pedido do usuário, 2026-09-28. Quill só
// tem lista numérica/com marcadores nativas (sem opção de estilo "a, b, c");
// criar um formato de verdade pra isso mexeria no modelo interno do Quill
// (Parchment/Attributor), frágil e sensível à versão carregada via CDN. Em
// vez disso: reconhece o padrão "x) " já digitado no INÍCIO da linha e, ao
// apertar Enter com o cursor no FIM dela, prossegue pra próxima letra
// automaticamente — é só texto comum no HTML final (mesmo tratamento que
// qualquer parágrafo já recebe em `_html_para_docx`, sem precisar reconhecer
// nenhum marcador especial na hora de gerar o .docx). Enter numa linha
// "x) " ainda vazia sai da sequência (não insere mais nenhuma letra) —
// mesmo gesto de sair de uma lista de verdade.
//
// Ouve em CAPTURE no `document` (não em `quill.root`) pra rodar ANTES do
// tratamento padrão de Enter que o próprio Quill já registra na construção
// (na mesma fase/mesmo elemento, quem registrou primeiro roda primeiro —
// nosso `criarEditorRico` só chama isto DEPOIS de `new Quill(...)`, então um
// listener nosso no próprio elemento sempre rodaria depois do do Quill).
const _RE_ITEM_LISTA_ALFA = /^([a-zA-Z])\)\s(.*)$/;

function _ativarListaAlfabetica(quill) {
    document.addEventListener('keydown', function(ev) {
        if (ev.key !== 'Enter' || ev.shiftKey || ev.ctrlKey || ev.metaKey || ev.altKey) return;
        if (!quill.root.contains(ev.target)) return;
        const sel = quill.getSelection();
        if (!sel || sel.length > 0) return;

        const [linha] = quill.getLine(sel.index);
        if (!linha) return;
        const inicioLinha = quill.getIndex(linha);
        const fimLinha = inicioLinha + linha.length() - 1; // -1: exclui o '\n' do próprio blot
        if (sel.index !== fimLinha) return; // Enter no MEIO da linha — comportamento normal

        const textoLinha = quill.getText(inicioLinha, fimLinha - inicioLinha);
        const m = textoLinha.match(_RE_ITEM_LISTA_ALFA);
        if (!m) return;

        ev.preventDefault();
        ev.stopPropagation();

        if (!m[2].trim()) {
            // "x) " ainda vazia + Enter — sai da sequência: só apaga o
            // marcador solto (a linha em si já existe, não precisa de outra
            // quebra — inserir mais um '\n' aqui duplicava a linha em branco).
            quill.deleteText(inicioLinha, textoLinha.length, 'user');
            quill.setSelection(inicioLinha, 0, 'user');
            return;
        }

        const letra = m[1];
        const letraMinuscula = letra.toLowerCase();
        let inserir;
        if (letraMinuscula === 'z') {
            // esgotou o alfabeto — Enter normal, sem mais nenhuma letra.
            inserir = '\n';
        } else {
            let proxima = String.fromCharCode(letraMinuscula.charCodeAt(0) + 1);
            if (letra === letra.toUpperCase()) proxima = proxima.toUpperCase();
            inserir = `\n${proxima}) `;
        }
        quill.insertText(sel.index, inserir, 'user');
        quill.setSelection(sel.index + inserir.length, 0, 'user');
    }, true);
}

// Reduz HTML (de um editor rico) a texto simples com quebras de linha razoáveis.
// Necessário quando o CAMPO DE DESTINO não é rico (ex.: a tramitação no SISDPU é
// texto puro — HTML lá apareceria como tags literais, não como formatação).
function htmlParaTextoSimples(html) {
    if (!html) return '';
    const div = document.createElement('div');
    div.innerHTML = html;
    div.querySelectorAll('p, div, li, blockquote, br, h1, h2, h3, h4, h5, h6').forEach((elx) => {
        elx.insertAdjacentText('afterend', '\n');
    });
    return (div.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
}

// --- "Usar modelo" — compartilhado por "Modelos de petição" (Ações no Eproc)
// e "Despachos padrão" (Ações no SISDPU): 3 helpers puros (sem estado, sem
// `this`) usados na hora de inserir um modelo cadastrado na caixa de texto
// certa, com os placeholders resolvidos. ----------------------------------

// "18 de agosto de 2026" — mesmo formato usado no fecho das petições geradas
// por IA (ver services/minuta_ia_service._data_por_extenso).
function dataPorExtenso() {
    const meses = ['janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho',
                   'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro'];
    const agora = new Date();
    return agora.getDate() + ' de ' + meses[agora.getMonth()] + ' de ' + agora.getFullYear();
}

// Modelo cadastrado ANTES da caixa de cadastro virar editor rico (texto puro,
// sem tags) — embrulha em <p> pra preservar quebra de linha ao inserir no
// Quill; um modelo já em HTML (tem alguma tag) passa direto.
function paraHtmlSeTextoPuro(texto) {
    const t = texto || '';
    return /<[a-z][\s\S]*>/i.test(t) ? t : ('<p>' + t.replace(/\n+/g, '</p><p>') + '</p>');
}

// Substitui cada chave literal (ex.: "[NOME]") pelo valor correspondente em
// `subs` — chave sem valor resolvido (ex.: sem PAJ vinculado) fica como está
// no texto, visível, pro usuário completar.
function resolverPlaceholders(texto, subs) {
    let out = texto || '';
    for (const [chave, valor] of Object.entries(subs)) {
        if (!valor) continue;
        out = out.split(chave).join(valor);
    }
    return out;
}
