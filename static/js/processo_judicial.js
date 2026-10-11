// Módulo Alpine reutilizável "Processo Judicial" — dados do(s) processo(s) vinculado(s)
// ao PAJ + intimações PENDENTES de cada um, consultadas ao vivo via MCP e-proc (mesma
// fonte/estrutura da página "Consultar processo" — ver eproc_consultar.html e
// /api/eproc-mcp/triagem), com acesso direto ao(s) documento(s) do evento referenciado
// por cada intimação — sem precisar sair da página do PAJ. Uso:
//   <div x-data="processoJudicialBlock({ processos: meta.sisdpu_raw.processos_judiciais })">
//     {% include "partials/processo_judicial.html" %}
//   </div>
//
// cfg:
//   processos — lista de {processo_judicial, juizo, data_inclusao, observacoes_processo,
//               validacao:{sucesso, motivo}} vindos direto de sisdpu_raw.processos_judiciais
//               (metadata.json) — sem reparse, sem chamada extra pra montar essa parte.
//   audiencias — lista de {data_hora, local, observacao} vinda direto de
//                sisdpu_raw.audiencias (extraída em ingestao/sisdpu_client.py, tabela
//                dedicada do SISDPU) — nível do PAJ, não por processo individual.
//
// A busca de intimações é 100% ao vivo (headless, sem navegador — mesmo backend MCP da
// página "Consultar processo") e roda em paralelo pra cada processo, assim que a seção
// abre (ela nasce sempre aberta quando há processo — ver partials/processo_judicial.html).
function processoJudicialBlock(cfg) {
    cfg = cfg || {};
    return {
        processos: cfg.processos || [],
        audiencias: cfg.audiencias || [],
        resultados: {},        // idx -> resposta de /api/eproc-mcp/triagem (intimacoes + eventos + cabecalho)
        prazosProcurador: {},  // idx -> prazos do MEU Painel do Procurador (cache da aba Prazos) | null = sem info
        carregando: {},   // idx -> bool
        erro: {},         // idx -> string
        baixando: {},     // "idx:numeroEvento" -> bool
        // Identificador do procurador logado (reaproveita a credencial de LOGIN
        // do e-proc já cadastrada no cofre — ver services/eproc_procurador_
        // service.py) — usado pra saber a quais partes do processo EU estou
        // associado, sem adivinhar por polo. Vazio quando o cofre está
        // trancado/sem credencial "eproc" salva: ver `temPrazosSemIdentidade`.
        meuIdentificador: '',
        atosPorProcesso: {},     // idx -> [{chave_caso, ato, paj_norm, prazos:[...]}] | null (ainda não carregado)
        meraCienciaBusy: {},     // chave (idx + ':' + evento) -> bool
        // Mesma lista estática de `static/js/acoes_eproc.js:tiposGerenciamento`
        // (espelha `eproc_client.TIPOS_GERENCIAMENTO`) — reaproveitada aqui pro
        // dropdown "tipo" do item 2 do modal (pedido do usuário 2026-09-24:
        // trazer os MESMOS 2 dropdowns de "Ações no Eproc" em vez de texto).
        tiposGerenciamento: [
            { valor: 'S', rotulo: 'Substituir procurador' },
            { valor: 'C', rotulo: 'Associar procurador' },
            { valor: 'D', rotulo: 'Desassociar procurador' },
            { valor: 'T', rotulo: 'Substituição Transitória' },
        ],
        // Modal de confirmação (substitui o confirm() nativo do navegador —
        // pedido do usuário 2026-09-24) antes de "Dar mera ciência".
        // `descricaoAto`: dado fixo deste caso, só exibição/montagem da
        // sugestão. `prazosDisponiveis`: [{evento_intimacao, label}] — TODOS
        // os candidatos identificados (por identidade); `prazosSelecionados`:
        // evento_intimacao -> bool, o checklist "Selecione o(s) prazo(s) a
        // ser(em) fechado(s)" (mesmo estilo de "Ações no Eproc") — o Defensor
        // pode desmarcar algum antes de confirmar (ex.: litisconsórcio em que
        // só parte deve fechar agora). `gerenciamentoTipo`/
        // `gerenciamentoProcuradorValor`/`Rotulo` e `procuradoresDropdown`:
        // mesmos 2 dropdowns de "Ações no Eproc" (pedido do usuário
        // 2026-09-24), pré-preenchidos com o padrão configurado mas
        // editáveis aqui — só pra ESTE disparo, não altera a configuração
        // salva. `descricaoMov`/`descricaoMovEditada`: texto sugerido da
        // movimentação no SISDPU, agora em caixa EDITÁVEL (pedido do usuário
        // 2026-09-24) — recalculado ao vivo conforme o checklist muda, MAS
        // só enquanto o Defensor não tiver editado à mão (mesma convenção já
        // usada em `static/js/intimacoes.js:_descricaoEditada`). `resolver` é
        // a função `resolve` da Promise devolvida por `_confirmarModal`.
        // `fase`: 'confirmando' (checklist + dropdowns + Cancelar/Confirmar)
        // -> 'executando' (spinner, `status` mostra o andamento ao vivo — o
        // modal FICA ABERTO durante a execução real, nunca fecha sozinho no
        // meio) -> 'concluido' (só então aparece "Fechar").
        confirmDialog: {
            show: false, resolver: null, fase: 'confirmando', status: '', numeroProcesso: '',
            descricaoAto: '', prazosDisponiveis: [], prazosSelecionados: {},
            gerenciamentoTipo: 'S', gerenciamentoProcuradorValor: '', gerenciamentoProcuradorRotulo: '',
            procuradoresDropdown: [],
            descricaoMov: '', descricaoMovEditada: false,
        },

        // Minimenu "Consultar processo (MCP) / Abrir no navegador" do número
        // do processo — mesmo padrão de posicionamento usado em prazos.html/
        // historico.html/sat.html/watchlist.html: um único elemento
        // teleportado pro <body> (ver partials/processo_judicial.html),
        // reposicionado via posMenu a cada clique. Sem isso, o dropdown nativo
        // ficava cortado pelo overflow:hidden do card .collapse (o número do
        // processo pode aparecer mais de uma vez na mesma seção — daí a chave
        // ser o número do processo, não um booleano único).
        menuAbertoProc: null,
        posMenu: { top: 0, left: 0 },

        async init() {
            await Promise.all([
                this.carregarIdentidade(),
                ...this.processos.map((_, idx) => this.carregarIntimacoes(idx)),
            ]);
        },

        // QUEM é o procurador logado (nunca a senha): usuário do login do MCP do
        // e-proc em 1º lugar, cofre só como reserva (pedido do Defensor,
        // 11/10/2026 — o cofre não é mais a 1ª opção). Vazio = identidade
        // indisponível, ver `temPrazosSemIdentidade`.
        async carregarIdentidade() {
            try {
                const r = await fetch('/api/eproc-mcp/meu-identificador');
                const d = await r.json();
                this.meuIdentificador = (d && d.usuario) || '';
            } catch (e) {
                this.meuIdentificador = '';
            }
        },

        async carregarIntimacoes(idx) {
            const p = this.processos[idx];
            const numero = p && p.processo_judicial;
            if (!numero) return;
            this.carregando[idx] = true;
            this.erro[idx] = '';
            try {
                const qs = new URLSearchParams({ numero: numero, instancia: 1, eventos: 30 });
                const resp = await fetch('/api/eproc-mcp/triagem?' + qs.toString());
                const d = await resp.json();
                if (!resp.ok || d.erro) {
                    this.erro[idx] = d.erro || 'Não foi possível consultar o e-proc para este processo.';
                    return;
                }
                this.resultados[idx] = d;
            } catch (e) {
                this.erro[idx] = 'Falha de rede ao consultar o e-proc.';
            } finally {
                this.carregando[idx] = false;
            }
            // Prazos do MEU Painel do Procurador (cache) — usados pra confirmar quais
            // intimações da triagem (processo INTEIRO) são realmente da DPU (ver
            // _confirmadaNoPainel). Busca em paralelo, sem bloquear a triagem acima.
            try {
                const r2 = await fetch('/api/eproc-mcp/prazos-procurador?numero=' + encodeURIComponent(numero));
                const d2 = await r2.json();
                this.prazosProcurador[idx] = d2.encontrado ? (d2.prazos || []) : null;
            } catch (e) {
                this.prazosProcurador[idx] = null;
            }
            // Pré-inicializa `baixando[idx:numeroEvento] = false` pra cada intimação com
            // documento pendente — sem isso, o binding `:disabled="baixando[chave]"` do
            // botão "Ler intimação" lê uma chave que NUNCA existiu no objeto reativo na
            // primeira renderização (o item só passa a existir quando os dois fetches
            // acima terminam, no mesmo tick em que o <template x-if> monta o botão pela
            // primeira vez) e o Alpine não reage corretamente depois — o botão nasce
            // com `disabled=true` preso, mesmo com o documento disponível pra baixar.
            for (const it of this.intimacoesAtivas(idx)) {
                for (const numeroEvento of this.eventosReferenciadosDe(idx, it)) {
                    const chave = idx + ':' + numeroEvento;
                    if (!(chave in this.baixando)) this.baixando[chave] = false;
                }
            }
        },

        // Pega só "dd/mm/aaaa" de uma string de data/hora (formatos batem: a
        // triagem MCP dá "24/07/2026 03:50:17", o Painel dá "27/07/2026 17:17:38").
        _soData(s) {
            const m = (s || '').match(/(\d{2}\/\d{2}\/\d{4})/);
            return m ? m[1] : '';
        },
        // A triagem MCP lê o processo INTEIRO — traz intimação de TODAS as partes
        // (autor via DPU E réu, tipicamente o INSS, com procurador próprio). O
        // MEU Painel do Procurador (cache da aba Prazos), ao contrário, NUNCA
        // traz intimação de quem eu não sou procurador — não precisa adivinhar
        // nada, é o universo certo por definição (cobre representante legal e
        // litisconsórcio automaticamente).
        //
        // Por isso o PAINEL é a fonte PRINCIPAL do que aparece aqui — não um
        // filtro aplicado sobre a triagem. A triagem MCP só ENRIQUECE cada
        // prazo do Painel (evento_referenciado, documentos), quando dá pra casar
        // por (mesmo dia de envio + mesmo nº de dias + MESMA parte associada a
        // mim, ainda não usado por outro prazo do Painel — um-para-um, evita
        // casar o mesmo evento da triagem com dois prazos do Painel). Sem casar,
        // o prazo do Painel aparece do mesmo jeito, só sem link de documento.
        //
        // "Mesma parte associada a mim" (2026-09-23) substituiu por completo a
        // antiga heurística de "polo ativo" (assumia a DPU sempre autora/
        // requerente, contra o INSS) — ela escolhia a parte ERRADA sempre que a
        // DPU representava o polo passivo. Caso real que expôs isso: PAJ
        // 2021/029-02882/processo 5011174-83.2021.4.04.7000 (Curadoria — DPU
        // representa uma RÉ numa disputa de dependentes de pensão por morte; a
        // heurística escolhia a intimação da autora, já fechada, e escondia a
        // da assistida real, ainda aberta). Não há mais adivinhação de papel/
        // polo nenhuma: `cabecalho.partes[*].representantes[*].identificador`
        // (já vem de graça na mesma resposta da triagem) diz exatamente a quais
        // partes o procurador logado está associado.
        _normalizarNome(s) {
            return (s || '').replace(/\s+/g, ' ').trim().toUpperCase();
        },
        _partesDoProcurador(idx) {
            const alvo = this._normalizarNome(this.meuIdentificador);
            const nomes = new Set();
            if (!alvo) return nomes;
            const r = this.resultados[idx];
            const partes = (r && r.cabecalho && r.cabecalho.partes) || [];
            partes.forEach((p) => {
                const reps = p.representantes || [];
                if (reps.some((rep) => this._normalizarNome(rep.identificador) === alvo)) {
                    nomes.add(this._normalizarNome(p.nome));
                }
            });
            return nomes;
        },
        _parteIntimadaEhMinha(parteIntimada, minhasPartes) {
            if (!minhasPartes.size) return false;
            const s = parteIntimada || '';
            const i = s.indexOf(' - ');
            const nome = i === -1 ? s : s.slice(i + 3);
            return minhasPartes.has(this._normalizarNome(nome));
        },
        // Sem identificador do procurador (cofre trancado/sem credencial "eproc"
        // salva), NÃO há como saber a quais partes associar — nunca arrisca
        // mostrar (poderia ser de outra parte, ex. INSS) nem esconder (poderia
        // ser real e nossa). Ver `temPrazosSemIdentidade` — mostra um aviso
        // pedindo pra destrancar, em vez de uma seção vazia sem explicação.
        //
        // Com identificador disponível: sem prazo confirmado no Painel pra este
        // processo, o universo é VAZIO — nunca cai pra mostrar a triagem MCP
        // crua (poderia trazer INSS/perito/etc.) só porque o Painel não tem
        // dado (ver `temPrazosSemIdentidade`/x-show no partial).
        intimacoesAtivas(idx) {
            const lista = this.prazosProcurador[idx];
            if (!lista || !lista.length || !this.meuIdentificador) return [];

            const r = this.resultados[idx];
            // TODAS as intimações da triagem (ativas E já fechadas) — precisamos
            // das FECHADAS também: um prazo do Painel do Procurador cujo
            // correspondente na triagem já fechou (ex.: recurso protocolado,
            // ciência dada) precisa DESAPARECER daqui — não continuar aparecendo
            // com dados crus/desatualizados do cache. Bug real confirmado
            // 2026-08-22 (PAJ 2026/029-02440: Recurso Inominado já protocolado
            // fechando a intimação do assistido; a tela continuava "pendente"
            // mostrando a intimação — ainda aberta — do INSS no lugar).
            const todas = r ? (r.intimacoes || []) : [];
            const minhasPartes = this._partesDoProcurador(idx);
            const usadas = new Set();
            return lista.map((p) => {
                const diaP = this._soData(p.data_envio);
                const diasP = parseInt(p.prazo_dias, 10);
                // Junta TODOS os candidatos do mesmo dia+dias que sejam de uma
                // parte associada a mim — pode haver mais de um só quando eu
                // represento mais de uma parte no MESMO ato (litisconsórcio).
                const candidatos = [];
                todas.forEach((it, i) => {
                    if (usadas.has(i)) return;
                    if (!this._parteIntimadaEhMinha(it.parte_intimada, minhasPartes)) return;
                    // data_hora = expedição da PRÓPRIA intimação (o que casa com o
                    // "data_envio" do Painel do Procurador) — NÃO enviado_em (sub-dado
                    // "Domicílio Judicial Eletrônico: Enviado em...", momento distinto
                    // da entrega eletrônica, sempre um pouco posterior).
                    const diaIt = this._soData(it.data_hora);
                    if (!diaP || !diaIt || diaP !== diaIt) return;
                    const diasIt = parseInt(it.prazo_dias, 10);
                    if (!isNaN(diasP) && !isNaN(diasIt) && diasP !== diasIt) return;
                    candidatos.push(i);
                });
                if (candidatos.length) {
                    // Todos os candidatos aqui já são MINHAS partes (nome batido
                    // via cabecalho.partes) — não sobra ambiguidade de polo pra
                    // desempatar; prefere só o ainda ATIVO entre eles (evita pegar
                    // por engano uma intimação ANTIGA/já fechada da mesma parte
                    // que, por coincidência, tem o mesmo dia+prazo de uma outra
                    // intimação — de ato diferente — ainda em curso). Bug real
                    // confirmado 2026-08-24 (PAJ 2026/029-01909: JADIRO tinha 2
                    // intimações de 5 dias no mesmo dia — uma antiga já encerrada
                    // por petição, outra da decisão de tutela, ainda aberta).
                    candidatos.sort((a, b) => (todas[a].ativa ? 0 : 1) - (todas[b].ativa ? 0 : 1));
                    const iMatch = candidatos[0];
                    usadas.add(iMatch);
                    const it = todas[iMatch];
                    // Achou o correspondente exato (ao vivo) deste prazo do Painel —
                    // se ele já fechou no e-proc, o prazo NÃO está mais pendente,
                    // mesmo que o cache do Painel (desatualizado até a próxima
                    // sincronização) ainda o liste; null aqui = removido do resultado
                    // (ver .filter(Boolean) no fim). Se ainda ativo, mostra normal
                    // (já tem evento_referenciado/docs).
                    // `_painelEvento`/`_painelDataEnvio`/`_painelPrazoDias`: os 3
                    // campos CRUS do Painel do Procurador (nunca os da triagem, que
                    // têm nomes/formatos diferentes) — únicos que `intimacao_service.
                    // _hash_ato` usa pra identificar o ato; preservados aqui pra
                    // `_chaveCasoDe` conseguir cruzar esta linha com `/api/intimacoes/
                    // {processo}/atos` sem precisar reimplementar o hash no front.
                    return it.ativa
                        ? { ...it, _painelEvento: p.evento, _painelDataEnvio: p.data_envio, _painelPrazoDias: p.prazo_dias }
                        : null;
                }
                // Sem correspondência na triagem (fora da janela de eventos, p.ex.)
                // — mostra só com os dados do próprio Painel, sem link de documento.
                return {
                    parte_intimada: p.parte || '',
                    prazo_dias: p.prazo_dias,
                    status: p.inicio_prazo ? 'ABERTO' : 'AGUARD. ABERTURA',
                    ativa: true,
                    enviado_em: p.data_envio,
                    data_inicial_prazo: p.inicio_prazo || '',
                    data_final_prazo: p.final_prazo || '',
                    prazo_aberto: !!p.inicio_prazo,
                    previsao: !p.inicio_prazo,
                    previsao_ciencia_ficta: p.inicio_estimado || '',
                    evento_referenciado: null,
                    dias_restantes: null,
                    _painelEvento: p.evento, _painelDataEnvio: p.data_envio, _painelPrazoDias: p.prazo_dias,
                };
            }).filter(Boolean);
        },
        totalIntimacoesAtivas() {
            return this.processos.reduce((acc, _, idx) => acc + this.intimacoesAtivas(idx).length, 0);
        },
        // Há prazo no Painel pra este processo, mas sem identificador do
        // procurador não dá pra saber a quais partes associar — mostra um
        // aviso pedindo pra destrancar o cofre, em vez de uma tabela vazia
        // sem explicação (opção escolhida pelo usuário 2026-09-23: nunca
        // arriscar mostrar intimação de outra parte só pra não ficar vazio).
        temPrazosSemIdentidade(idx) {
            const lista = this.prazosProcurador[idx];
            return !this.meuIdentificador && !!(lista && lista.length);
        },

        // --- "Dar mera ciência" / atalho de petição — badges na coluna ao lado
        // de "Ato referenciado" (pedido do usuário 2026-09-23). Reaproveita 100%
        // do backend já usado pela página Intimações (`services.mera_ciencia_
        // service`, endpoint SSE `/api/intimacoes/{processo}/dar-mera-ciencia`)
        // — só precisa resolver, aqui, a qual `chave_caso`/`ato` esta LINHA
        // corresponde (a página Intimações já recebe isso pronto de
        // `listar_casos()`; aqui a origem do dado é outra, a triagem ao vivo +
        // Painel, sem esse hash calculado). "1 clique puro" (pedido explícito
        // do usuário 2026-09-24): sem caixa de revisão — a descrição é
        // resolvida e disparada no mesmo clique, só com um confirm() nativo
        // mostrando o texto antes de protocolar de verdade.

        // Busca (1x por processo, sob demanda) os atos já conhecidos por
        // `intimacao_service` pra este processo — cada um com seus prazos
        // crus (evento/data_envio/prazo_dias), usados só pra cruzar com
        // `_painelEvento`/`_painelDataEnvio`/`_painelPrazoDias` da linha.
        async _carregarAtosDoProcesso(idx) {
            if (this.atosPorProcesso[idx]) return this.atosPorProcesso[idx];
            const numero = this.processos[idx].processo_judicial;
            try {
                const r = await (await fetch(`/api/intimacoes/${encodeURIComponent(numero)}/atos`)).json();
                this.atosPorProcesso[idx] = (r && r.ok) ? (r.casos || []) : [];
            } catch (e) {
                this.atosPorProcesso[idx] = [];
            }
            return this.atosPorProcesso[idx];
        },
        // Casa esta linha (pelos 3 campos crus preservados em `intimacoesAtivas`)
        // contra os atos do processo — mesma identidade (evento+data_envio+
        // prazo_dias) que `intimacao_service._hash_ato` usa pra gerar o
        // `chave_caso`, só que comparada aqui, nunca recalculada (o hash em si
        // é implementação interna do backend, não precisa ser replicado).
        _acharAto(it, casos) {
            for (const caso of casos || []) {
                const bate = (caso.prazos || []).some((p) =>
                    p.evento === it._painelEvento && p.data_envio === it._painelDataEnvio
                    && String(p.prazo_dias) === String(it._painelPrazoDias));
                if (bate) return caso;
            }
            return null;
        },
        _chaveMeraCiencia(idx, it) {
            return idx + ':' + (it._painelEvento || '') + '|' + (it._painelDataEnvio || '') + '|' + (it._painelPrazoDias || '');
        },
        meraCienciaRodando(idx, it) {
            return !!this.meraCienciaBusy[this._chaveMeraCiencia(idx, it)];
        },
        // Sugestão da descrição da movimentação no SISDPU, montada a partir
        // dos eventos MARCADOS no checklist (não mais todos os disponíveis —
        // pedido do usuário 2026-09-24: poder desmarcar algum evento do
        // litisconsórcio antes de fechar). Só uma SUGESTÃO agora — o texto
        // final vem de `confirmDialog.descricaoMov` (caixa editável, pedido
        // do usuário 2026-09-24), ver `_atualizarDescricaoSugerida`.
        // Função PURA (não lê `this.confirmDialog` — recebe tudo por
        // parâmetro) pra poder ser chamada tanto ANTES do modal existir
        // (sugestão inicial, ver `darMeraCienciaPaj`) quanto DEPOIS (recálculo
        // ao vivo, ver `_atualizarDescricaoSugerida`), sem duplicar a lógica.
        _descricaoMeraCiencia(prazosDisponiveis, prazosSelecionados, descricaoAto) {
            const eventos = (prazosDisponiveis || [])
                .filter((p) => prazosSelecionados[p.evento_intimacao])
                .map((p) => String(p.evento_intimacao));
            if (!eventos.length) return '';
            const plural = eventos.length > 1;
            return `Ciente da intimação do${plural ? 's' : ''} evento${plural ? 's' : ''} `
                 + `(${eventos.join(', ')}), referente a ${descricaoAto || 'ato judicial'}.`;
        },
        // Chamada a cada clique num checkbox do checklist — só recalcula a
        // sugestão enquanto o Defensor não tiver editado a caixa de texto à
        // mão (mesma convenção de `static/js/intimacoes.js:_descricaoEditada`
        // — nunca sobrescreve uma edição manual).
        _atualizarDescricaoSugerida() {
            const cd = this.confirmDialog;
            if (cd.descricaoMovEditada) return;
            cd.descricaoMov = this._descricaoMeraCiencia(cd.prazosDisponiveis, cd.prazosSelecionados, cd.descricaoAto);
        },
        _algumPrazoSelecionado() {
            return Object.values(this.confirmDialog.prazosSelecionados || {}).some(Boolean);
        },
        // "Desassociar procurador" (tipo "D") não tem procurador destino —
        // só os demais tipos exigem um escolhido (mesma regra de "Ações no
        // Eproc"/do backend, ver `mera_ciencia_service.dar_mera_ciencia`).
        _gerenciamentoValido() {
            const cd = this.confirmDialog;
            return cd.gerenciamentoTipo === 'D' || !!cd.gerenciamentoProcuradorValor;
        },
        // Guarda o rótulo junto do valor pro log/histórico ficarem legíveis
        // (mesmo padrão de `static/js/acoes_eproc.js:onTrocarProcuradorDestino`).
        _onTrocarGerenciamentoProcurador() {
            const cd = this.confirmDialog;
            const p = (cd.procuradoresDropdown || []).find((p) => p.valor === cd.gerenciamentoProcuradorValor);
            cd.gerenciamentoProcuradorRotulo = p ? p.rotulo : '';
        },
        // Modal de confirmação simples (substitui confirm() nativo) — devolve
        // uma Promise<boolean>, resolvida pelos botões do modal teleportado
        // (ver templates/partials/processo_judicial.html). `dados`:
        // {descricaoAto, prazosDisponiveis, prazosSelecionados,
        // gerenciamentoTipo, gerenciamentoProcuradorValor,
        // gerenciamentoProcuradorRotulo, procuradoresDropdown, descricaoMov}.
        _confirmarModal(dados) {
            return new Promise((resolve) => {
                this.confirmDialog = {
                    show: true, resolver: resolve, fase: 'confirmando', status: '',
                    numeroProcesso: dados.numeroProcesso,
                    descricaoAto: dados.descricaoAto,
                    prazosDisponiveis: dados.prazosDisponiveis,
                    prazosSelecionados: dados.prazosSelecionados,
                    gerenciamentoTipo: dados.gerenciamentoTipo,
                    gerenciamentoProcuradorValor: dados.gerenciamentoProcuradorValor,
                    gerenciamentoProcuradorRotulo: dados.gerenciamentoProcuradorRotulo,
                    procuradoresDropdown: dados.procuradoresDropdown,
                    descricaoMov: dados.descricaoMov, descricaoMovEditada: false,
                };
            });
        },
        _responderConfirmModal(ok) {
            // Só faz algo na fase "confirmando" — clique no backdrop/Cancelar
            // durante a execução real NUNCA fecha o modal (ver `_fecharConfirmModal`,
            // só habilitada depois de concluído). Confirmar sem prazo marcado,
            // sem descrição ou com gerenciamento incompleto tampouco faz nada
            // (botão já fica desabilitado nesses casos, isto aqui é só a
            // segunda trava).
            if (this.confirmDialog.fase !== 'confirmando') return;
            if (ok && (!this._algumPrazoSelecionado() || !this.confirmDialog.descricaoMov.trim()
                       || !this._gerenciamentoValido())) return;
            const resolver = this.confirmDialog.resolver;
            if (ok) {
                this.confirmDialog.fase = 'executando';
                this.confirmDialog.resolver = null;
            } else {
                this._fecharConfirmModal();
            }
            if (resolver) resolver(ok);
        },
        _fecharConfirmModal() {
            this.confirmDialog = {
                show: false, resolver: null, fase: 'confirmando', status: '', numeroProcesso: '',
                descricaoAto: '', prazosDisponiveis: [], prazosSelecionados: {},
                gerenciamentoTipo: 'S', gerenciamentoProcuradorValor: '', gerenciamentoProcuradorRotulo: '',
                procuradoresDropdown: [], descricaoMov: '', descricaoMovEditada: false,
            };
        },
        async darMeraCienciaPaj(idx, it) {
            const chaveBusy = this._chaveMeraCiencia(idx, it);
            if (this.meraCienciaBusy[chaveBusy]) return;
            const numero = this.processos[idx].processo_judicial;

            this.meraCienciaBusy[chaveBusy] = true;
            // Sem texto de status aqui (pedido do usuário 2026-09-24) — só o
            // spinner do botão (via `meraCienciaRodando`) enquanto resolve o ato
            // e a descrição por trás; a 1ª coisa visível pro Defensor é o modal
            // de confirmação. `_executarMeraCienciaSSE` assume o controle de `meraCienciaBusy`
            // (zera sozinha ao terminar, sucesso ou erro) só depois de disparada
            // — `entregue` marca esse ponto pra este `finally` só zerar `busy` nos
            // caminhos de saída ANTES dela (ato não encontrado, sem descrição,
            // cancelado no confirm()).
            let entregue = false;
            try {
                const casos = await this._carregarAtosDoProcesso(idx);
                const caso = this._acharAto(it, casos);
                if (!caso) {
                    alert('Não foi possível localizar este ato na Triagem Inteligente de Intimações '
                        + '— abra a página "Intimações" para tratar este caso.');
                    return;
                }
                if (caso.paj_norm && window._paj_norm && caso.paj_norm !== window._paj_norm) {
                    alert('Este processo está vinculado a outro PAJ (' + caso.paj_norm + ') na página '
                        + 'Intimações — corrija o vínculo lá antes de continuar por aqui.');
                    return;
                }

                // Checklist "Selecione o(s) prazo(s) a ser(em) fechado(s)" — mesmo
                // estilo/pedido do usuário 2026-09-24 de "Ações no Eproc". Todos os
                // candidatos identificados (por identidade — ver `eproc_procurador_
                // service`) vêm pré-marcados; o Defensor pode desmarcar algum antes
                // de confirmar (ex.: litisconsórcio em que só um coautor deve
                // fechar agora).
                const eventosResp = await (await fetch(`/api/intimacoes/${encodeURIComponent(numero)}/eventos-do-ato?ato=${encodeURIComponent(caso.ato)}`)).json();
                const prazosDisponiveis = (eventosResp && eventosResp.ok) ? (eventosResp.itens || []) : [];
                if (!prazosDisponiveis.length) {
                    alert('Não foi possível identificar automaticamente o evento de intimação — '
                        + 'use a página "Intimações" para tratar este caso.');
                    return;
                }
                const prazosSelecionados = {};
                prazosDisponiveis.forEach((p) => { prazosSelecionados[p.evento_intimacao] = true; });

                // Dropdowns "Gerenciar processo depois de peticionar
                // (substabelecer)?" — mesmos 2 elementos de "Ações no Eproc"
                // (pedido do usuário 2026-09-24), pré-preenchidos com o padrão
                // configurado mas editáveis só pra ESTE disparo.
                let procuradoresDropdown = [];
                let gerenciamentoProcuradorValor = '';
                let gerenciamentoProcuradorRotulo = '';
                try {
                    const cfg = await (await fetch('/api/config/eproc-procuradores')).json();
                    procuradoresDropdown = (cfg.procuradores_favoritos && cfg.procuradores_favoritos.length)
                        ? cfg.procuradores_favoritos : (cfg.procuradores_todos || []);
                    const padrao = cfg && cfg.procurador_padrao;
                    if (padrao) {
                        gerenciamentoProcuradorValor = padrao.valor || '';
                        gerenciamentoProcuradorRotulo = padrao.rotulo || '';
                    }
                } catch (e) { /* segue sem padrão — o Defensor escolhe na hora */ }

                const descricaoSugerida = this._descricaoMeraCiencia(
                    prazosDisponiveis, prazosSelecionados, eventosResp.descricao_ato || '');

                const confirmado = await this._confirmarModal({
                    descricaoAto: eventosResp.descricao_ato || '',
                    prazosDisponiveis, prazosSelecionados,
                    numeroProcesso: numero,
                    gerenciamentoTipo: 'S', gerenciamentoProcuradorValor, gerenciamentoProcuradorRotulo,
                    procuradoresDropdown,
                    descricaoMov: descricaoSugerida,
                });
                if (!confirmado) return;

                const cd = this.confirmDialog;
                const eventosSelecionados = prazosDisponiveis
                    .filter((p) => prazosSelecionados[p.evento_intimacao])
                    .map((p) => String(p.evento_intimacao));
                const descricao = cd.descricaoMov.trim();
                const gerTipo = cd.gerenciamentoTipo;
                const gerValor = gerTipo === 'D' ? '' : cd.gerenciamentoProcuradorValor;
                const gerRotulo = gerTipo === 'D' ? '' : cd.gerenciamentoProcuradorRotulo;

                entregue = true;
                await this._executarMeraCienciaSSE(idx, chaveBusy, numero, caso.ato, descricao, eventosSelecionados,
                                                    gerTipo, gerValor, gerRotulo);
            } finally {
                if (!entregue) this.meraCienciaBusy[chaveBusy] = false;
            }
        },
        _executarMeraCienciaSSE(idx, chaveBusy, numero, ato, descricao, eventosSelecionados,
                                 gerenciamentoTipo, gerenciamentoProcuradorValor, gerenciamentoProcuradorRotulo) {
            return new Promise((resolve) => {
                this.confirmDialog.status = 'Executando…';
                const qs = new URLSearchParams({
                    descricao, ato, eventos: (eventosSelecionados || []).join(','),
                    gerenciamento_tipo: gerenciamentoTipo || '',
                    gerenciamento_procurador_valor: gerenciamentoProcuradorValor || '',
                    gerenciamento_procurador_rotulo: gerenciamentoProcuradorRotulo || '',
                });
                const es = new EventSource(`/api/intimacoes/${encodeURIComponent(numero)}/dar-mera-ciencia?${qs}`);
                es.addEventListener('log', (ev) => {
                    try { this.confirmDialog.status = JSON.parse(ev.data).msg; } catch (e) { /* ignore */ }
                });
                es.addEventListener('done', async (ev) => {
                    let res = {};
                    try { res = JSON.parse(ev.data); } catch (e) { /* ignore */ }
                    this.meraCienciaBusy[chaveBusy] = false;
                    es.close();
                    if (res.ok) {
                        // Sucesso (pedido do usuário 2026-09-24): atualiza as
                        // movimentações do PAJ (mesma função global já usada pelo
                        // ícone de atualizar da tabela de movimentações) e recarrega
                        // a página inteira — nunca fica só no "Concluído ✓" sem dar
                        // pro Defensor a conferência visual do que foi lançado no
                        // SISDPU. Nunca resolve/chega no `finally` daqui pra baixo —
                        // a própria navegação encerra este fluxo.
                        this.confirmDialog.status = 'Concluído ✓ — atualizando movimentações do PAJ…';
                        if (typeof window.atualizarMovimentacoesPaj === 'function' && window._paj_norm) {
                            try { await window.atualizarMovimentacoesPaj(window._paj_norm); } catch (e) { /* ignore */ }
                        }
                        window.location.reload();
                        return;
                    }
                    this.confirmDialog.status = 'Erro: ' + (res.erro || 'falha desconhecida');
                    this.confirmDialog.fase = 'concluido';
                    await this.carregarIntimacoes(idx);
                    resolve();
                });
                es.onerror = () => {
                    this.confirmDialog.status = 'Erro de conexão com o servidor';
                    this.confirmDialog.fase = 'concluido';
                    this.meraCienciaBusy[chaveBusy] = false;
                    es.close();
                    resolve();
                };
            });
        },

        // Atalho "petição" — expande e rola até "Ações no Eproc" (já embutida
        // mais abaixo na própria página do PAJ), pré-selecionando o processo
        // quando o PAJ tem mais de um vinculado. Módulo separado (x-data
        // próprio, `acoesEprocBlock`) — acessado via `Alpine.$data()` sobre o
        // elemento com `id="acoes-eproc-block"` (ver nota em templates/
        // paj_detail.html), nunca por `$refs` (quebra quando x-ref e x-data
        // ficam no mesmo elemento — ver memória "Alpine x-ref on same element
        // as x-data").
        async abrirPeticaoPara(idx) {
            const numero = this.processos[idx].processo_judicial;
            const el = document.getElementById('acoes-eproc-block');
            if (!el) return;
            const bloco = Alpine.$data(el);
            if (!bloco) return;
            try {
                // NUNCA `bloco.abrir()` direto — é um TOGGLE (fecharia se já
                // estivesse aberto). Garante aberto+carregado de forma
                // idempotente, replicando só a metade "abrir" do toggle.
                if (!bloco.aberto) {
                    bloco.aberto = true;
                    await bloco._carregarAoAbrir();
                }
                if (bloco.processos && bloco.processos.length > 1 && bloco.processoSelecionado !== numero) {
                    bloco.processoSelecionado = numero;
                    await bloco.trocarProcesso();
                }
            } finally {
                // `finally`: rola mesmo se abrir/trocar processo falhar no meio
                // (ex.: falha de rede ao carregar prazos) — sem isso, uma
                // exceção ali impedia a linha de baixo de rodar, e o clique
                // parecia "não fazer nada" além de abrir o cabeçalho. `$nextTick`
                // (não `requestAnimationFrame`) espera o Alpine terminar de
                // desocultar o conteúdo (`x-show="aberto"`) — sem isso, o
                // `scrollIntoView` podia medir a altura ainda colapsada (bug
                // real relatado 2026-09-24: "abre mas não rola").
                await new Promise((resolve) => this.$nextTick(resolve));
                el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        },

        // Mesma paleta usada em prazos.html/eproc_consultar.html: amarelo=aberta,
        // vermelho claro=aguardando abertura. Só chega aqui intimação ativa.
        corIntimacao(it) {
            return it.prazo_aberto ? 'var(--cor-intim-aberta)' : 'var(--cor-intim-aguardando)';
        },

        eventoDe(idx, numeroEvento) {
            const r = this.resultados[idx];
            if (!r || !numeroEvento) return null;
            return (r.eventos || []).find((ev) => String(ev.numero) === String(numeroEvento)) || null;
        },
        // TODOS os eventos referenciados por esta intimação — não só o 1º
        // resolvido automaticamente pelo MCP em `evento_referenciado`: o
        // e-proc admite MAIS DE UM ("Refer. ao Evento N e ao Evento M"), e os
        // demais vêm soltos em `outros_eventos_referenciados` (só o número,
        // sem descrição/documentos resolvidos pelo MCP — por isso cada um
        // ainda precisa passar por `eventoDe`, que já busca na timeline
        // completa já carregada). Mesma lógica que `services.
        // intimacao_ia_service.obter_evento_referenciado_leve` já usa no
        // backend (Triagem de Intimações) há meses — nunca tinha sido
        // trazida pra este módulo da página do PAJ até agora. Achado real do
        // usuário (2026-10-02, processo 5039332-75.2026.4.04.7000): a
        // intimação referenciava os eventos 21 E 25, só o 21 aparecia aqui.
        // Devolve `[]` se não houver nenhum (ou se o único estiver fora da
        // janela de eventos carregada — mesmo critério de sempre).
        eventosReferenciadosDe(idx, it) {
            const ref = it.evento_referenciado;
            if (!ref || ref.fora_da_janela) return [];
            const numeros = [String(ref.numero)];
            for (const extra of it.outros_eventos_referenciados || []) {
                const n = String(extra).trim();
                if (n && !numeros.includes(n)) numeros.push(n);
            }
            return numeros;
        },
        docsDisponiveis(idx, numeroEvento) {
            const ev = this.eventoDe(idx, numeroEvento);
            return (ev && ev.arquivos_baixados) || [];
        },
        temDocPendente(idx, numeroEvento) {
            const ev = this.eventoDe(idx, numeroEvento);
            return !!(ev && (ev.documentos || []).length && !(ev.arquivos_baixados || []).length);
        },
        // Documentos do evento referenciado AINDA NÃO baixados — um badge por
        // documento (mesmo padrão da coluna "Documentos" da página "Consultar
        // processo"), clicar em qualquer um baixa todos de uma vez (mesmo
        // request de sempre — ver baixarDocEvento).
        docsPendentes(idx, numeroEvento) {
            const ev = this.eventoDe(idx, numeroEvento);
            return (ev && ev.documentos) || [];
        },
        linkArquivo(idx, arq) {
            const numero = this.processos[idx].processo_judicial;
            return '/api/eproc-mcp/arquivo?' + new URLSearchParams({ numero: numero, arquivo: arq.arquivo }).toString();
        },

        // Baixa o(s) documento(s) de UM evento referenciado — sob demanda, só
        // quando o defensor clica no badge do documento pendente (não baixa
        // tudo automaticamente). Clicar em qualquer badge do mesmo evento
        // baixa todos de uma vez. Recebe o NÚMERO do evento diretamente
        // (antes recebia a intimação inteira e só sabia tratar o 1º evento
        // referenciado — ver `eventosReferenciadosDe`), pra funcionar igual
        // pro evento principal e pra cada "outro evento referenciado".
        async baixarDocEvento(idx, numeroEvento) {
            const chave = idx + ':' + numeroEvento;
            if (this.baixando[chave]) return;
            this.baixando[chave] = true;
            const numero = this.processos[idx].processo_judicial;
            try {
                const resp = await fetch('/api/eproc-mcp/baixar-documentos', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ numero: numero, instancia: 1, evento_ids: [String(numeroEvento)] }),
                });
                const r = await resp.json();
                if (!resp.ok || r.erro) {
                    showToast(r.erro || 'Falha ao baixar o documento da intimação.', 'error');
                    return;
                }
                const arqs = (r.arquivos_por_evento || {})[String(numeroEvento)];
                const ev = this.eventoDe(idx, numeroEvento);
                if (arqs && ev) ev.arquivos_baixados = arqs;
                if (r.erros && r.erros.length) {
                    showToast('Documento não pôde ser baixado (acesso restrito ou indisponível).', 'warning');
                }
            } catch (e) {
                showToast('Falha de rede ao baixar o documento.', 'error');
            } finally {
                this.baixando[chave] = false;
            }
        },

        // Mesma linguagem visual da coluna "Documentos" da página "Consultar processo"
        // (ver eproc_consultar.html) — nome do badge = sigla/nome do e-proc SEM
        // extensão, em qualquer estado (antes ou depois de baixado); a extensão só
        // decide a COR do badge, nunca o texto exibido.
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

        // Campos livres de Audiências (Local/Observação) às vezes trazem o link da
        // sala virtual como TEXTO PURO (não como <a href> de verdade) — detecta URLs
        // e as torna clicáveis. Escapa tudo antes (nunca confia em texto vindo do
        // SISDPU) e preserva quebras de linha reais como <br>. Usar com x-html.
        linkify(texto) {
            if (!texto) return '';
            const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const urlRe = /(https?:\/\/[^\s<]+)/g;
            return esc(texto)
                .split('\n')
                .map((linha) => linha.replace(urlRe, (m) => `<a href="${m}" target="_blank" rel="noopener" class="link">${m}</a>`))
                .join('<br>');
        },
    };
}
