// Página "Intimações" — Triagem Inteligente de Intimações.
//
// Consome os endpoints de routes/intimacoes.py. Todo o trabalho pesado
// (resumo/confronto/minuta) acontece no servidor (services/intimacao_ia_service.py)
// e continua rodando lá INDEPENDENTE da conexão do navegador — por isso o
// acompanhamento aqui é feito por um painel FLUTUANTE de "jobs" (não um modal
// bloqueante): cada análise disparada (por linha, "todos" ou "selecionados")
// vira um card que pode ser minimizado sem cancelar nada, permitindo
// continuar usando o resto da tela enquanto ela processa em segundo plano
// (pedido explícito do usuário — o modal antigo travava a tela até terminar).
//
// ⚠️ Análise/resumo/confronto/minuta nesta tela NUNCA tocam e-proc/SISDPU de
// verdade — o botão "Abrir PAJ" é só um link de navegação para a página do
// PAJ, onde os consoles "Ações no e-proc"/"Ações no SISDPU" (já existentes)
// permitem a execução manual. A ÚNICA EXCEÇÃO é o botão "Dar mera ciência"
// (2026-08-29, substitui o extinto "Gravar p/ mera ciência" + tela /lote-
// ciencia — "não me senti seguro em fazer em lote"): dispara, para ESTE
// único processo, as 3 ações reais e sempre iguais (ciência com renúncia ao
// prazo no e-proc + Gerenciamento de Processo + movimentação no SISDPU),
// só depois de um `confirm()` explícito e com a descrição da movimentação já
// revisada/editada pelo Defensor na caixa de texto ao lado — ver
// `services.mera_ciencia_service`.

function intimacoesPage() {
    return {
        itens: [],
        carregando: false,
        expandido: {},
        vinculoInput: {},
        // Todos os mapas abaixo são chaveados por `it.chave_caso`
        // ("<processo>:<ato>", ver módulo services/intimacao_service.py),
        // NUNCA por `it.processo` — um processo pode ter mais de um item na
        // lista (atos autônomos), e usar só `processo` como chave faria 2
        // linhas diferentes do MESMO processo compartilharem seleção/
        // descrição/estado de job por engano (mudança 2026-09-XX, pedido
        // explícito do usuário: "o foco é a intimação, não o processo"). A
        // ÚNICA exceção é `vinculoInput`, abaixo — vínculo de PAJ é por
        // processo mesmo (de propósito).
        selecionados: {},   // { [chave_caso]: true } — checkboxes da análise coletiva
        baixandoAto: {},    // { [chave_caso + ':' + numero_evento]: true } — feedback do badge "Baixar" do ato referenciado
        descricaoMov: {},   // { [chave_caso]: string } — texto (editável) da movimentação no SISDPU p/ "Dar mera ciência"
        _descricaoEditada: {},  // { [chave_caso]: true } — o Defensor já editou à mão; não sobrescrever mais com o padrão

        // Filtro por tipo de intimação (chips acima da tabela) — pedido do
        // usuário (2026-08-28): "facilitar a triagem e o disparo de análises
        // de casos semelhantes". Array vazio = sem filtro (mostra tudo).
        // Múltiplos tipos podem ficar ativos ao mesmo tempo.
        tipoFiltro: [],

        // Ordenação da tabela (client-side — os dados já vêm todos de /api/intimacoes/casos).
        // Default = prazo_final asc, mesmo critério do backend (services/intimacao_service.py),
        // mas aqui o usuário pode alternar clicando no cabeçalho da coluna.
        sortBy: 'prazo_final',
        sortDir: 'asc',

        // Análises em andamento/concluídas — painel flutuante, não modal.
        // Cada job: {id, processo (null p/ fila "todos"/"selecionados"),
        // titulo, log: [], status: 'rodando'|'ok'|'erro', statusMsg,
        // minimizado, _es}.
        jobs: [],
        _jobSeq: 0,

        // Tooltip do lembrete nativo do e-proc (post-it) — mesmo padrão de
        // templates/prazos.html.
        lembreteTip: { show: false, x: 0, y: 0, itens: [] },

        init() {
            this.carregarLista();
            this._reatarJobsAtivos();
        },

        // Reata sozinho nos jobs que já estavam rodando no SERVIDOR antes
        // desta página carregar (ex.: voltou de outra aba do painel no meio
        // de uma análise) — sem isso, o card só reaparecia se o usuário
        // clicasse em "Analisar" de novo (o servidor já reata sozinho desde
        // 2026-08-29, "à prova de navegação"; isso aqui só faz a TELA
        // acompanhar de volta automaticamente, sem precisar desse clique).
        async _reatarJobsAtivos() {
            try {
                const resp = await fetch('/api/intimacoes/jobs-ativos');
                const r = await resp.json();
                for (const j of (r && r.jobs) || []) {
                    const chaveCaso = j.ato ? `${j.processo}:${j.ato}` : null;
                    if (j.tipo === 'individual' && chaveCaso && !this.jobRodando(chaveCaso)) {
                        const qs = new URLSearchParams({ ato: j.ato });
                        this._criarJob(`/api/intimacoes/${encodeURIComponent(j.processo)}/analisar?${qs}`,
                            `Analisando ${j.processo}`, chaveCaso);
                    } else if (j.tipo === 'fila' && !this.filaRodando()) {
                        this._criarJob('/api/intimacoes/analisar-todos', 'Retomando análise em fila…');
                    } else if (j.tipo === 'mera_ciencia' && chaveCaso && !this.jobRodando(chaveCaso)) {
                        // Reata sem `descricao` na URL — o job já existe no servidor
                        // (`_JOBS`), a rota ignora o parâmetro numa reconexão.
                        const qs = new URLSearchParams({ ato: j.ato });
                        this._criarJob(`/api/intimacoes/${encodeURIComponent(j.processo)}/dar-mera-ciencia?${qs}`,
                            `Dando mera ciência — ${j.processo}`, chaveCaso);
                    }
                }
            } catch (e) { /* melhor não travar a página por causa disso */ }
        },

        // Ato(s) referenciado(s) pela intimação — pedido explícito do
        // usuário (2026-08-29): "esse ato referenciado deve estar
        // disponível direto, independentemente de qualquer análise", pra
        // poder abrir o documento e decidir manualmente (inclusive se é
        // caso de mera ciência) SEM precisar rodar "Analisar" antes. Mesmo
        // padrão visual/mecanismo já usado na página do PAJ em "Intimações
        // pendentes" (`templates/partials/processo_judicial.html` +
        // `static/js/processo_judicial.js`), reaproveitando os MESMOS 2
        // endpoints genéricos de lá (`/api/eproc-mcp/arquivo` pra abrir,
        // `/api/eproc-mcp/baixar-documentos` pra baixar sob demanda) — só
        // a origem do "qual processo/evento" é que muda (aqui vem de
        // `/api/intimacoes/{processo}/evento-referenciado`, sem IA).
        //
        // Dispara em segundo plano, com um teto de requisições simultâneas
        // (não é raro ter 30-40 casos na lista — uma rajada de 40 chamadas
        // ao MCP de uma vez seria deselegante com o e-proc).
        async _carregarAtosReferenciados() {
            const CONCORRENCIA = 6;
            const fila = [...this.itens];
            const worker = async () => {
                while (fila.length) {
                    const it = fila.shift();
                    try {
                        const ato = it.chave_caso.split(':')[1];
                        const qs = new URLSearchParams({ ato });
                        const resp = await fetch(`/api/intimacoes/${encodeURIComponent(it.processo)}/evento-referenciado?${qs}`);
                        const r = await resp.json();
                        it._atoRef = (r && r.ok) ? r.dados : null;
                        // Pré-semeia `baixandoAto` PRA CADA ato ANTES do badge
                        // existir na tela — mesmo motivo do pré-semeio de
                        // `_atoRef` em `carregarLista` ("Alpine :disabled
                        // stuck on unset key"): a chave só é conhecida depois
                        // desta resposta chegar, então tem que nascer aqui,
                        // não antes.
                        for (const atoRef of (it._atoRef || {}).atos_referenciados || []) {
                            const chave = it.chave_caso + ':' + atoRef.numero;
                            if (!(chave in this.baixandoAto)) this.baixandoAto[chave] = false;
                        }
                        // Descrição padrão de "Dar mera ciência" — só agora
                        // (com `_atoRef` carregado) dá pra saber qual evento
                        // fechar, SEM depender de "Analisar" já ter rodado
                        // (pedido explícito do usuário 2026-08-29: "quero que
                        // o botão esteja disponível sempre, independentemente
                        // de ter sido feita a análise"). Nunca sobrescreve se
                        // o Defensor já editou à mão.
                        if (it._atoRef && it._atoRef.evento_intimacao && !this._descricaoEditada[it.chave_caso]) {
                            this.descricaoMov[it.chave_caso] = this.descricaoMovPadrao(it);
                        }
                    } catch (e) {
                        it._atoRef = null;
                    }
                }
            };
            await Promise.all(Array.from({ length: CONCORRENCIA }, worker));
        },

        // Mesmo padrão visual da coluna "Documentos" da página "Consultar
        // processo"/"Intimações pendentes" do PAJ (ver
        // static/js/processo_judicial.js) — nome do badge = sigla/nome do
        // e-proc SEM extensão; a extensão só decide a COR do badge.
        extDocumentoAto(nomeArquivo) {
            const ext = (nomeArquivo || '').split('.').pop();
            return (ext && ext.length <= 5 ? ext : 'doc').toUpperCase();
        },
        nomeDocumentoAto(nomeArquivo) {
            return (nomeArquivo || '').replace(/\.[^.]+$/, '') || nomeArquivo;
        },
        corBadgeDocumentoAto(nomeArquivo) {
            const ext = this.extDocumentoAto(nomeArquivo).toLowerCase();
            if (ext === 'pdf') return 'badge-error';
            if (['doc', 'docx', 'html'].includes(ext)) return 'badge-info';
            if (['jpg', 'jpeg', 'png', 'gif'].includes(ext)) return 'badge-warning';
            return 'badge-ghost';
        },

        // Link pra ABRIR um documento do ato JÁ BAIXADO — mesmo endpoint
        // genérico da página do PAJ (`/api/eproc-mcp/arquivo`), nenhum
        // endpoint novo.
        linkArquivoAto(it, arq) {
            const qs = new URLSearchParams({ numero: it.processo, arquivo: arq.arquivo });
            return `/api/eproc-mcp/arquivo?${qs}`;
        },

        // Baixa o(s) documento(s) de UM ato/evento ainda não baixado(s) —
        // mesmo endpoint genérico da página do PAJ
        // (`/api/eproc-mcp/baixar-documentos`), clicar em qualquer badge do
        // mesmo evento baixa todos de uma vez.
        async baixarDocAto(it, ato) {
            const chave = it.chave_caso + ':' + ato.numero;
            if (this.baixandoAto[chave]) return;
            this.baixandoAto[chave] = true;
            try {
                const instancia = (it._atoRef || {}).instancia || 1;
                const resp = await fetch('/api/eproc-mcp/baixar-documentos', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ numero: it.processo, instancia, evento_ids: [String(ato.numero)] }),
                });
                const r = await resp.json();
                if (!resp.ok || r.erro) {
                    alert(r.erro || 'Falha ao baixar o documento do ato.');
                    return;
                }
                const arqs = (r.arquivos_por_evento || {})[String(ato.numero)];
                if (arqs) ato.arquivos_baixados = arqs;
                if (r.erros && r.erros.length) {
                    alert('Documento não pôde ser baixado (acesso restrito ou indisponível).');
                }
            } catch (e) {
                alert('Falha de rede ao baixar o documento.');
            } finally {
                this.baixandoAto[chave] = false;
            }
        },

        async carregarLista() {
            this.carregando = true;
            try {
                const resp = await fetch('/api/intimacoes/casos');
                const r = await resp.json();
                this.itens = (r && r.itens) || [];
                for (const it of this.itens) {
                    // Pré-semeia a chave reativa ANTES do 1º render — mesmo bug
                    // real já documentado (memória "Alpine :disabled stuck on an
                    // unset object key"): `:disabled`/`x-show` presos porque a
                    // chave nunca existiu no objeto reativo (undefined não
                    // dispara o binding do jeito que um valor explícito dispara).
                    if (!('_atoRef' in it)) it._atoRef = null;
                    // Descrição (editável) da movimentação no SISDPU p/ "Dar mera
                    // ciência" — só um pré-semeio vazio aqui; o valor PADRÃO de
                    // verdade é calculado em `_carregarAtosReferenciados` (assim
                    // que `_atoRef` chegar, o que NÃO depende de "Analisar" já
                    // ter rodado — pedido explícito do usuário 2026-08-29).
                    // Chaveado por `chave_caso` (não `processo` — ver comentário
                    // no topo do objeto): 2 itens do MESMO processo (atos
                    // autônomos) nunca compartilham esse estado.
                    if (!(it.chave_caso in this.descricaoMov)) this.descricaoMov[it.chave_caso] = '';
                }
            } catch (e) {
                this.itens = [];
            } finally {
                this.carregando = false;
            }
            this._carregarAtosReferenciados();
        },

        toggle(it) {
            this.expandido[it.chave_caso] = !this.expandido[it.chave_caso];
        },

        // Tooltip do lembrete (post-it) — posiciona ao lado do ícone, mesmo
        // padrão de templates/prazos.html (mostrarLembrete/lembreteTip).
        mostrarLembrete(ev, it) {
            const r = ev.currentTarget.getBoundingClientRect();
            let x = r.left, y = r.bottom + 6;
            const limite = window.innerWidth - 500;
            if (x > limite) x = Math.max(8, limite);
            this.lembreteTip = { show: true, x, y, itens: it.lembrete_itens || [] };
        },

        // Copiar processo/PAJ — mesmo padrão de retornos.html/dashboard.html/
        // paj_detail.html (cada página reimplementa com seu próprio campo de
        // feedback no item; não há uma função global compartilhada pra isso).
        copiarProcesso(it) {
            navigator.clipboard.writeText(it.processo);
            it._copiado = true;
            setTimeout(() => { it._copiado = false; }, 1500);
        },

        copiarPaj(it) {
            navigator.clipboard.writeText(it.paj);
            it._pajCopiado = true;
            setTimeout(() => { it._pajCopiado = false; }, 1500);
        },

        qtdSelecionados() {
            return Object.values(this.selecionados).filter(Boolean).length;
        },

        // Tipo de intimação (extraído no backend a partir do texto bruto do
        // evento raspado em Prazos e-proc — ver services.intimacao_service.
        // _tipo_intimacao_de_evento) — disponível ANTES de qualquer análise.
        tipoLabel(it) {
            return it.tipo_intimacao || 'Geral';
        },

        tipoClasse(it) {
            const mapa = {
                'Sentença': 'tipo-sentenca',
                'Audiência': 'tipo-audiencia',
                'Contrarrazões': 'tipo-contrarrazoes',
                'Despacho/Decisão': 'tipo-despacho',
                'Requisição de Pagamento Expedida para Conferência': 'tipo-rpv',
                'Regularizar representação processual': 'tipo-regularizar',
                'Geral': 'tipo-geral',
            };
            return mapa[this.tipoLabel(it)] || 'tipo-outro';
        },

        // Nome do assistido (polo ativo/autor) a partir de `partes` — mesmo
        // formato usado em templates/prazos.html ("Autor NOME (CPF) X Réu
        // ..."), aqui sem o CPF pra caber melhor numa coluna estreita.
        nomeAssistido(it) {
            const p = it.partes || '';
            const i = p.indexOf(' X ');
            const ativo = (i < 0 ? p : p.slice(0, i)).trim();
            const sp = ativo.indexOf(' ');
            let nome = sp < 0 ? ativo : ativo.slice(sp + 1);
            nome = nome.replace(/\s*\([^)]*\)\s*$/, '').trim();
            return nome || '—';
        },

        // Tipos distintos presentes em `itens` (não no filtrado — os chips
        // continuam todos visíveis mesmo com outro filtro já ativo), com
        // contagem, ordenados do mais frequente pro menos frequente.
        tiposDisponiveis() {
            const contagem = new Map();
            for (const it of this.itens) {
                const tipo = this.tipoLabel(it);
                contagem.set(tipo, (contagem.get(tipo) || 0) + 1);
            }
            return [...contagem.entries()]
                .map(([tipo, count]) => ({ tipo, count }))
                .sort((a, b) => b.count - a.count || a.tipo.localeCompare(b.tipo, 'pt-BR'));
        },

        toggleTipoFiltro(tipo) {
            const i = this.tipoFiltro.indexOf(tipo);
            if (i < 0) this.tipoFiltro.push(tipo);
            else this.tipoFiltro.splice(i, 1);
        },

        limparFiltroTipo() {
            this.tipoFiltro = [];
        },

        // Lista efetivamente "visível" na tela — base de todas as ações em
        // lote (selecionar todos, Analisar todos os pendentes) e da própria
        // ordenação, pra respeitar o filtro de tipo quando ativo.
        itensFiltrados() {
            if (!this.tipoFiltro.length) return this.itens;
            return this.itens.filter(it => this.tipoFiltro.includes(this.tipoLabel(it)));
        },

        todosSelecionados() {
            const vis = this.itensFiltrados();
            return vis.length > 0 && vis.every(it => this.selecionados[it.chave_caso]);
        },

        alternarSelecionarTodos() {
            const vis = this.itensFiltrados();
            const marcar = !this.todosSelecionados();
            vis.forEach(it => { this.selecionados[it.chave_caso] = marcar; });
        },

        limparSelecao() {
            this.selecionados = {};
        },

        // Chamado pelo clique no cabeçalho da coluna. Clicar de novo na mesma
        // coluna inverte a direção; trocar de coluna volta para asc.
        ordenarPor(campo) {
            if (this.sortBy === campo) {
                this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
            } else {
                this.sortBy = campo;
                this.sortDir = 'asc';
            }
        },

        _valorOrdenacao(it, campo) {
            if (campo === 'prazo_final') {
                // Usa SEMPRE a forma ISO (AAAA-MM-DD, calculada no backend em
                // services/intimacao_service.py) pra comparar — o campo de
                // EXIBIÇÃO (it.prazo_final) é 'DD/MM/AAAA', e comparar essa
                // string diretamente ordena pelo DIA primeiro, não pela data
                // (bug real: '01/09/2026' vinha antes de '31/08/2026').  Sem
                // prazo (—) sempre vai pro final, independente da direção.
                return it.prazo_final_ordenacao || '9999-99-99';
            }
            if (campo === 'classificacao') return this.classifLabel(it).toLowerCase();
            if (campo === 'status_geral') return this.statusColunaLabel(it).toLowerCase();
            return (it[campo] || '').toString().toLowerCase();
        },

        itensOrdenados() {
            const campo = this.sortBy;
            const dir = this.sortDir === 'desc' ? -1 : 1;
            return [...this.itensFiltrados()].sort((a, b) => {
                const va = this._valorOrdenacao(a, campo);
                const vb = this._valorOrdenacao(b, campo);
                if (va < vb) return -1 * dir;
                if (va > vb) return 1 * dir;
                return 0;
            });
        },

        temPendente() {
            return this.itensFiltrados().some(it => it.vinculo_status !== 'pendente' &&
                ['novo', 'erro'].includes(it.status_geral));
        },

        // 'leve'/'completo' = modos ANTIGOS (registros analisados antes de
        // 2026-08-29, quando o confronto ainda sincronizava/analisava o
        // PAJ) — mantidos aqui só pra exibir corretamente análises já
        // feitas; nenhuma análise NOVA produz mais esses modos (pedido do
        // usuário: "não deveremos mais analisar o PAJ em nenhuma
        // situação" — o padrão agora é 'somente_processo').
        escopoLabel(it) {
            const modo = (it.escopo_verificacao || {}).modo;
            if (modo === 'leve') return 'Verificação leve do PAJ (análise antiga)';
            if (modo === 'completo') return 'Análise completa do PAJ (análise antiga)';
            if (modo === 'especializado_laudo_pericial') return 'Fluxo especializado (laudo pericial)';
            if (modo === 'especializado_rpv') return 'Fluxo especializado (RPV/precatório)';
            if (modo === 'especializado_sentenca_homologatoria') return 'Fluxo especializado (sentença homologatória)';
            return 'Somente processo judicial (sem análise do PAJ)';
        },

        escopoClasse(it) {
            const modo = (it.escopo_verificacao || {}).modo;
            if (modo === 'leve' || modo === 'completo') return 'pendente';
            if (modo === 'especializado_laudo_pericial') return 'laudo';
            if (modo === 'especializado_rpv') return 'rpv';
            if (modo === 'especializado_sentenca_homologatoria') return 'sentenca';
            return 'ciencia';
        },

        classifLabel(it) {
            if (it.vinculo_status === 'pendente') return 'Aguardando vínculo de PAJ';
            const c = (it.confronto || {}).classificacao;
            const labels = {
                CUMPRIMENTO_IMEDIATO_CIENCIA: 'Só ciência',
                CUMPRIMENTO_IMEDIATO_PETICIONAR: 'Cabe petição',
                DILIGENCIA_NECESSARIA: 'Diligência necessária',
                REQUER_ANALISE_HUMANA: 'Requer análise humana',
                IMPUGNACAO_LAUDO_PERICIAL: 'Laudo pericial (fluxo especializado)',
                CIENCIA_RPV: 'RPV conferida — só ciência',
                RETIFICACAO_RPV: 'RPV — cabe retificação',
                CIENCIA_SENTENCA_HOMOLOGATORIA: 'Sentença conferida — só ciência',
                // Orientação de 3 vias (2026-08-29) — substituiu o antigo par
                // CIENCIA/REQUER_ANALISE_HUMANA só deste fluxo específico.
                SENTENCA_HOMOLOGATORIA_ANALISAR: 'Sentença — analisar detalhadamente',
                SENTENCA_HOMOLOGATORIA_PETICIONAR: 'Sentença — cabe petição (analisar providência)',
            };
            return labels[c] || '— não classificado —';
        },

        classifClasse(it) {
            if (it.vinculo_status === 'pendente') return 'pendente';
            const c = (it.confronto || {}).classificacao;
            const classes = {
                CUMPRIMENTO_IMEDIATO_CIENCIA: 'ciencia',
                CUMPRIMENTO_IMEDIATO_PETICIONAR: 'peticionar',
                DILIGENCIA_NECESSARIA: 'diligencia',
                REQUER_ANALISE_HUMANA: 'humana',
                IMPUGNACAO_LAUDO_PERICIAL: 'laudo',
                CIENCIA_RPV: 'ciencia',
                RETIFICACAO_RPV: 'rpv',
                CIENCIA_SENTENCA_HOMOLOGATORIA: 'ciencia',
                SENTENCA_HOMOLOGATORIA_ANALISAR: 'humana',
                SENTENCA_HOMOLOGATORIA_PETICIONAR: 'peticionar',
            };
            return classes[c] || 'pendente';
        },

        statusLabel(s) {
            const labels = {
                novo: 'Novo', analisando: 'Analisando…', aguardando_revisao: 'Aguardando revisão',
                concluido: 'Concluído', descartado: 'Descartado', erro: 'Erro',
            };
            return labels[s] || s || '';
        },

        // Coluna "Status" da tabela — pedido do usuário (2026-08-28): mostrar
        // o RESULTADO da análise ali (ex.: "Sentença conferida — só ciência",
        // "RPV conferida — só ciência", "Cabe petição"), não só o andamento
        // genérico (Novo/Analisando/Concluído). Prioridade: se o caso ainda
        // não tem resultado utilizável (não iniciado, em andamento, com erro
        // na última rodada, ou descartado pelo Defensor), mostra o andamento
        // de sempre — não daria pra mostrar um resultado que não existe (ou
        // que existe mas é de uma rodada ANTERIOR, já superada por um erro/
        // descarte mais recente). Só quando há mesmo um resultado atual
        // (aguardando_revisao/concluído) é que mostra a classificação —
        // reaproveitando `classifLabel`/`classifClasse`, que já são
        // tipo-cientes (ex.: rótulo diferente pra Sentença/RPV) — ver ali.
        statusColunaLabel(it) {
            if (['novo', 'analisando', 'erro', 'descartado'].includes(it.status_geral)) {
                return this.statusLabel(it.status_geral);
            }
            return this.classifLabel(it);
        },

        statusColunaClasse(it) {
            if (it.status_geral === 'erro') return 'humana';
            if (['novo', 'analisando', 'descartado'].includes(it.status_geral)) return 'pendente';
            return this.classifClasse(it);
        },

        async vincular(it) {
            const paj = (this.vinculoInput[it.processo] || '').trim();
            if (!paj) return;
            try {
                const resp = await fetch(`/api/intimacoes/${encodeURIComponent(it.processo)}/vincular-paj`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paj }),
                });
                const r = await resp.json();
                if (!r.ok) { alert(r.erro || 'Falha ao vincular o PAJ.'); return; }
                await this.carregarLista();
            } catch (e) {
                alert('Falha de rede ao vincular o PAJ.');
            }
        },

        async descartar(it) {
            if (!confirm(`Descartar a triagem do processo ${it.processo}? O histórico (resumo/classificação/minuta) fica salvo, só sai da lista de pendentes.`)) return;
            try {
                const ato = it.chave_caso.split(':')[1];
                const qs = new URLSearchParams({ ato });
                await fetch(`/api/intimacoes/${encodeURIComponent(it.processo)}/descartar?${qs}`, { method: 'POST' });
                await this.carregarLista();
            } catch (e) {
                alert('Falha de rede ao descartar.');
            }
        },

        // Texto PADRÃO (sugestão, sempre editável antes de disparar) da
        // movimentação a registrar no SISDPU — mesmo texto que o extinto
        // `lote_ciencia_service.confirmar_um` gerava às cegas; agora só serve
        // de PONTO DE PARTIDA pro Defensor revisar/ajustar na caixa de texto
        // ao lado do botão "Dar mera ciência" antes de qualquer disparo real.
        //
        // Fonte primária: `it._atoRef` (carregado por `_carregarAtosReferenciados`,
        // disponível SEM "Analisar" ter rodado — pedido explícito do usuário
        // 2026-08-29: "quero que o botão esteja disponível sempre"). Cai pra
        // `it.evento_referenciado` (só existe depois de uma análise) como
        // fallback — mesmo casamento de evento por trás dos dois, nunca
        // deveriam divergir, mas não custa ter os dois.
        descricaoMovPadrao(it) {
            const ar = it._atoRef || {};
            const er = it.evento_referenciado || {};
            const evento = ar.evento_intimacao || er.evento_intimacao;
            const coautores = (ar.evento_intimacao_coautores && ar.evento_intimacao_coautores.length)
                ? ar.evento_intimacao_coautores : (er.evento_intimacao_coautores || []);
            const eventos = [evento, ...coautores].filter(Boolean);
            if (!eventos.length) return '';
            const plural = eventos.length > 1;
            const descricaoAto = ar.evento_descricao || ar.rotulo || er.evento_descricao || er.rotulo || 'ato judicial';
            return `Ciente da intimação do${plural ? 's' : ''} evento${plural ? 's' : ''} `
                 + `(${eventos.join(', ')}), referente a ${descricaoAto}.`;
        },

        // "Dar mera ciência" (2026-08-29, substitui o extinto "Gravar p/ mera
        // ciência" + tela /lote-ciencia — pedido explícito do usuário: "não
        // me senti seguro em fazer em lote"): dispara, SÓ para este processo
        // e só depois de confirmação explícita, as 3 ações reais e sempre
        // iguais (ciência com renúncia ao prazo no e-proc + Gerenciamento de
        // Processo + movimentação no SISDPU com a descrição já revisada na
        // caixa de texto ao lado). IRREVERSÍVEL — ver
        // `services.mera_ciencia_service`.
        darMeraCiencia(it) {
            if (this.jobRodando(it.chave_caso)) return;
            const descricao = (this.descricaoMov[it.chave_caso] || '').trim();
            if (!descricao) {
                alert('Preencha a descrição da movimentação no SISDPU antes de continuar.');
                return;
            }
            if (!confirm(`Dar mera ciência ao processo ${it.processo}?\n\n`
                       + 'Isso executa DE VERDADE, agora:\n'
                       + '1) Ciência, com renúncia ao prazo, no e-proc;\n'
                       + '2) Gerenciamento de Processo (substabelecimento);\n'
                       + '3) Movimentação no SISDPU com a descrição ao lado.\n\n'
                       + 'IRREVERSÍVEL — protocola de verdade na vara e move o PAJ no SISDPU.')) return;
            const ato = it.chave_caso.split(':')[1];
            const qs = new URLSearchParams({ descricao, ato });
            this._criarJob(`/api/intimacoes/${encodeURIComponent(it.processo)}/dar-mera-ciencia?${qs}`,
                `Dando mera ciência — ${it.processo}`, it.chave_caso);
        },

        // Cria um job novo (1 EventSource) e o adiciona ao painel flutuante.
        // `processo` fica null pra jobs de fila ("todos"/"selecionados") —
        // usado só por `jobRodando`/`filaRodando` pra decidir quais botões
        // desabilitar enquanto algo já está em andamento.
        _criarJob(url, titulo, processo) {
            // `Alpine.reactive(...)` — SEM isso, `job` é um objeto puro; ao dar
            // `this.jobs.push(job)`, o Alpine passa a observar uma cópia/proxy
            // reativo DENTRO do array, mas os `addEventListener` abaixo seguem
            // mutando a referência ORIGINAL (não observada) — o card fica
            // "congelado"/cinza até QUALQUER outra interação forçar um
            // re-render (ex.: minimizar/maximizar), que aí sim lê o valor já
            // atualizado. Envolvendo o objeto em `Alpine.reactive()` ANTES do
            // push, a mesma referência que os listeners mutam é a que o Alpine
            // observa — atualiza a tela em tempo real (bug real relatado pelo
            // usuário: "só mostra algo quando minimizo e maximizo").
            const job = Alpine.reactive({
                id: ++this._jobSeq, processo: processo || null, titulo,
                log: [], status: 'rodando', statusMsg: '', minimizado: false, _es: null,
            });
            this.jobs.push(job);
            const es = new EventSource(url);
            job._es = es;
            es.addEventListener('log', (ev) => {
                try { job.log.push(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
                this._scrollJobLog(job.id);
            });
            es.addEventListener('done', async (ev) => {
                let res = {};
                try { res = JSON.parse(ev.data); } catch (e) { /* ignore */ }
                job.status = res.ok ? 'ok' : 'erro';
                job.statusMsg = res.ok ? 'Concluído ✓' : ('Erro: ' + (res.erro || 'falha desconhecida'));
                es.close();
                await this.carregarLista();
            });
            es.onerror = () => {
                job.status = 'erro';
                job.statusMsg = 'Erro de conexão com o servidor';
                es.close();
            };
            return job;
        },

        _scrollJobLog(id) {
            requestAnimationFrame(() => {
                const el = document.getElementById('job-log-' + id);
                if (el) el.scrollTop = el.scrollHeight;
            });
        },

        // Se já há um job "rodando" pra este ATO (`chaveCaso` = `it.chave_caso`,
        // job individual — o campo `job.processo` guarda essa MESMA chave pra
        // jobs individuais/mera-ciência, nunca só o número do processo, ver
        // `_criarJob`) — usado pra desabilitar o botão "Analisar" da linha
        // correspondente.
        jobRodando(chaveCaso) {
            return this.jobs.some(j => j.status === 'rodando' && j.processo === chaveCaso);
        },

        // Se já há uma fila ("todos"/"selecionados", processo === null) rodando.
        filaRodando() {
            return this.jobs.some(j => j.status === 'rodando' && j.processo === null);
        },

        toggleJobMinimizado(job) {
            job.minimizado = !job.minimizado;
        },

        // "Fechar" NUNCA cancela a análise em si — ela já roda no servidor,
        // independente desta conexão SSE (fechá-la só para de acompanhar o
        // log ao vivo). Enquanto ainda "rodando", só minimiza; remove da
        // lista de fato só depois de terminado (ok/erro).
        fecharJob(job) {
            if (job.status === 'rodando') {
                job.minimizado = true;
                return;
            }
            if (job._es) { job._es.close(); }
            this.jobs = this.jobs.filter(j => j.id !== job.id);
        },

        analisar(it) {
            if (this.jobRodando(it.chave_caso)) return;
            const ato = it.chave_caso.split(':')[1];
            const qs = new URLSearchParams({ ato });
            this._criarJob(`/api/intimacoes/${encodeURIComponent(it.processo)}/analisar?${qs}`,
                `Analisando ${it.processo}`, it.chave_caso);
        },

        // Usa o MESMO endpoint de "selecionados" (lista explícita de
        // processos), não o antigo `/api/intimacoes/analisar-todos` — assim
        // respeita o filtro por tipo quando ativo (pedido do usuário:
        // "disparo de análises de casos semelhantes"). Sem filtro, o
        // resultado é idêntico a "todos os pendentes" de sempre.
        analisarTodos() {
            if (this.filaRodando()) return;
            const pendentes = this.itensFiltrados().filter(it => it.vinculo_status !== 'pendente' &&
                ['novo', 'erro'].includes(it.status_geral));
            if (!pendentes.length) return;
            const casosSel = pendentes.map(it => it.chave_caso);
            const qs = new URLSearchParams({ casos_sel: casosSel.join(',') });
            const rotulo = this.tipoFiltro.length ? ` (${this.tipoFiltro.join(', ')})` : '';
            this._criarJob(`/api/intimacoes/analisar-selecionados?${qs}`,
                `Analisando ${casosSel.length} pendente(s)${rotulo}`);
        },

        analisarSelecionados() {
            if (this.filaRodando()) return;
            const casosSel = Object.keys(this.selecionados).filter(k => this.selecionados[k]);
            if (!casosSel.length) return;
            const qs = new URLSearchParams({ casos_sel: casosSel.join(',') });
            this._criarJob(`/api/intimacoes/analisar-selecionados?${qs}`,
                `Analisando ${casosSel.length} selecionado(s)`);
            this.limparSelecao();
        },
    };
}
