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

// ---------------------------------------------------------------------------
// Placeholders dos modelos (petição padrão, "Usar modelo" de Ações no e-proc e
// no SISDPU) — catálogo ÚNICO, agrupado por categoria (19/10/2026): usado pelos
// botões de Configurações e por `subsDoPaj` (quem preenche). Para incluir um
// novo: uma linha aqui + o valor em services/placeholders_service.py.
// `campo`: chave da resposta de /api/acoes-eproc/qualificacao/{paj};
// `extra`: consulta sob demanda que o campo exige ("pessoa" = ficha no SIS-DPU
// 2.0; "processo" = cabeçalho no e-proc).
const PLACEHOLDERS_CATALOGO = [
    { categoria: 'Assistido', itens: [
        { token: '[NOME]', campo: 'nome', legenda: 'Nome do assistido principal do PAJ' },
        { token: '[CPF]', campo: 'cpf', legenda: 'CPF do assistido principal' },
        { token: '[QUALIFICACAO]', campo: 'qualificacao', extra: 'pessoa', legenda: 'Frase de qualificação: nome, nacionalidade, CPF, representante (se houver) e endereço — concordância pelo sexo da ficha do SIS-DPU' },
        { token: '[ASSISTIDOS]', campo: 'assistidos', legenda: 'Todos os assistidos do PAJ ("A, B e C")' },
        { token: '[DATA_NASCIMENTO]', campo: 'data_nascimento', extra: 'pessoa', legenda: 'Data de nascimento (ficha do SIS-DPU)' },
        { token: '[IDADE]', campo: 'idade', extra: 'pessoa', legenda: 'Idade hoje, em anos (ficha do SIS-DPU)' },
        { token: '[NOME_MAE]', campo: 'nome_mae', extra: 'pessoa', legenda: 'Nome da mãe (ficha do SIS-DPU)' },
        { token: '[RG]', campo: 'rg', extra: 'pessoa', legenda: 'RG e órgão emissor (ficha do SIS-DPU)' },
        { token: '[NACIONALIDADE]', campo: 'nacionalidade', extra: 'pessoa', legenda: 'brasileiro(a)/estrangeiro(a) (ficha do SIS-DPU)' },
    ] },
    { categoria: 'Representante legal', itens: [
        { token: '[REPRESENTANTE]', campo: 'representante', legenda: 'Nome do(s) representante(s) legal(is) cadastrado(s) no PAJ' },
        { token: '[CPF_REPRESENTANTE]', campo: 'cpf_representante', legenda: 'CPF do(s) representante(s) legal(is)' },
    ] },
    { categoria: 'Contatos', itens: [
        { token: '[TELEFONES]', campo: 'telefones', legenda: 'TODOS os telefones sincronizados do PAJ (card Contatos)' },
        { token: '[EMAILS]', campo: 'emails', legenda: 'TODOS os e-mails sincronizados do PAJ' },
        { token: '[ENDERECOS]', campo: 'enderecos', legenda: 'TODOS os endereços sincronizados do PAJ' },
    ] },
    { categoria: 'PAJ', itens: [
        { token: '[PAJ]', legenda: 'Número do PAJ' },
        { token: '[PRETENSAO]', campo: 'pretensao', legenda: 'Pretensão do PAJ' },
        { token: '[DATA_ABERTURA_PAJ]', campo: 'data_abertura_paj', legenda: 'Data de abertura do PAJ' },
    ] },
    { categoria: 'Processo judicial', itens: [
        { token: '[PROCESSO]', campo: 'processo', legenda: 'Número do processo (formatado; só com exatamente 1 vinculado ao PAJ)' },
        { token: '[JUIZO]', campo: 'juizo', legenda: 'Juízo cadastrado no PAJ (SISDPU)' },
        { token: '[VARA]', campo: 'vara', extra: 'processo', legenda: 'Órgão julgador, lido no e-proc (TRF4 ou JMU)' },
        { token: '[CLASSE]', campo: 'classe', extra: 'processo', legenda: 'Classe processual, lida no e-proc' },
        { token: '[AUTOR]', campo: 'autor', extra: 'processo', legenda: 'Parte(s) do polo ativo, lidas no e-proc' },
        { token: '[REU]', campo: 'reu', extra: 'processo', legenda: 'Parte(s) do polo passivo, lidas no e-proc' },
    ] },
    { categoria: 'Intimação / prazo', itens: [
        { token: '[EVENTO_INTIMACAO]', campo: 'evento_intimacao', legenda: 'Evento da intimação com prazo mais próximo (Prazos sincronizados)' },
        { token: '[PRAZO_FINAL]', campo: 'prazo_final', legenda: 'Data final do prazo mais próximo (marca "estimado" se ainda não aberto)' },
    ] },
    { categoria: 'Defensoria / data', itens: [
        { token: '[DATA]', legenda: 'Data de hoje, por extenso' },
        { token: '[OFICIO]', legenda: 'Ofício (Configurações › Geral)' },
        { token: '[UNIDADE]', legenda: 'Unidade da DPU (Configurações › Geral)' },
        { token: '[DEFENSOR]', legenda: 'Nome do defensor (Configurações › Geral)' },
        { token: '[CARGO]', legenda: 'Cargo do defensor (Configurações › Geral)' },
    ] },
];
const PLACEHOLDERS_LISTA = PLACEHOLDERS_CATALOGO.flatMap((g) => g.itens);

// Número CNJ com pontuação: 5045930-45.2026.4.04.7000.
function formatarCnj(n) {
    const d = String(n || '').replace(/\D/g, '');
    if (d.length !== 20) return n || '';
    return d.slice(0, 7) + '-' + d.slice(7, 9) + '.' + d.slice(9, 13) + '.' + d.slice(13, 14)
        + '.' + d.slice(14, 16) + '.' + d.slice(16);
}

// Placeholders que não dependem de PAJ (data + Configurações › Geral).
function subsFixos() {
    const org = window._identificacaoOrgao || {};
    return {
        '[DATA]': dataPorExtenso(),
        '[OFICIO]': org.oficio || '',
        '[UNIDADE]': org.unidade || '',
        '[DEFENSOR]': org.defensor_nome || '',
        '[CARGO]': org.defensor_cargo || '',
    };
}

// Placeholders de DADOS do PAJ usados no modelo `html` — 1 chamada ao servidor,
// pedindo as consultas sob demanda (ficha/e-proc) só se o modelo as usar.
// Sem PAJ ou sem dado: o placeholder fica literal no texto.
async function subsDoPaj(pajNorm, html) {
    const usados = PLACEHOLDERS_LISTA.filter((p) => p.campo && (html || '').includes(p.token));
    if (!pajNorm || !usados.length) return {};
    const extras = [...new Set(usados.map((p) => p.extra).filter(Boolean))];
    try {
        const qs = extras.length ? ('?extras=' + encodeURIComponent(extras.join(','))) : '';
        const j = await (await fetch('/api/acoes-eproc/qualificacao/' + encodeURIComponent(pajNorm) + qs)).json();
        const subs = {};
        usados.forEach((p) => { if (j[p.campo]) subs[p.token] = j[p.campo]; });
        return subs;
    } catch (e) { return {}; }
}

// ---- Banco de teses: "Inserir Tese" (20/10/2026) ---------------------------------
// Mesmo gesto de "Usar modelo": escolhe uma tese do Banco de teses e o texto
// INTEGRAL cadastrado entra no cursor da caixa, em azul (a mesma cor que marca
// os trechos do banco nas petições geradas com IA — ver
// services/banco_teses_service.py). Lista relida a cada abertura do menu, para
// refletir o que foi cadastrado/editado na página Banco de teses.
const COR_TESE_HEX = '#0066cc';

async function listarTesesBanco() {
    try {
        const j = await (await fetch('/api/banco-teses')).json();
        return (j.teses || []).slice().sort((a, b) =>
            (b.ativa ? 1 : 0) - (a.ativa ? 1 : 0)
            || (a.titulo || '').localeCompare(b.titulo || '', 'pt-BR', { sensitivity: 'base' }));
    } catch (e) { return []; }
}

// Pinta de azul o conteúdo de cada parágrafo/citação/item da tese (espelho de
// `_azul` em banco_teses_service.py) — o texto em si não muda.
function htmlTeseAzul(html) {
    // Cores embutidas no cadastro (cinza/branco herdados do Word) sobreporiam o
    // azul — só a cor sai, o texto não muda.
    html = (html || '').replace(/(?<![-\w])color\s*:\s*[^;"']+;?\s*/gi, '');
    const span = (s) => `<span style="color: ${COR_TESE_HEX};">${s}</span>`;
    const novo = (html || '').replace(/(<(?:p|li|blockquote)\b[^>]*>)([\s\S]*?)(<\/(?:p|li|blockquote)>)/gi,
        (m, a, meio, f) => a + span(meio) + f);
    return novo === html && (html || '').replace(/<[^>]+>/g, '').trim() ? span(html) : novo;
}
