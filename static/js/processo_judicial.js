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
        resultados: {},        // idx -> resposta de /api/eproc-mcp/triagem (intimacoes + eventos)
        prazosProcurador: {},  // idx -> prazos do MEU Painel do Procurador (cache da aba Prazos) | null = sem info
        carregando: {},   // idx -> bool
        erro: {},         // idx -> string
        baixando: {},     // "idx:numeroEvento" -> bool

        async init() {
            await Promise.all(this.processos.map((_, idx) => this.carregarIntimacoes(idx)));
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
                const ref = it.evento_referenciado;
                if (ref && !ref.fora_da_janela) {
                    const chave = idx + ':' + ref.numero;
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
        // litisconsórcio automaticamente, sem heurística de nome ou de polo).
        //
        // Por isso o PAINEL é a fonte PRINCIPAL do que aparece aqui — não um
        // filtro aplicado sobre a triagem. A triagem MCP só ENRIQUECE cada
        // prazo do Painel (evento_referenciado, documentos), quando dá pra casar
        // por (mesmo dia de envio + mesmo nº de dias, ainda não usado por outro
        // prazo do Painel — um-para-um, evita casar o mesmo evento da triagem
        // com dois prazos do Painel). Sem casar, o prazo do Painel aparece do
        // mesmo jeito, só sem link de documento.
        //
        // (Tentei antes usar a triagem como universo e "confirmar" contra o
        // Painel, com desempate por polo ativo/passivo quando duas intimações
        // tinham timing idêntico — falha quando a DPU representa o polo passivo
        // (réu), caso comum. Invertido: o Painel já É o universo, não precisa
        // desempate nenhum.)
        //
        // Papéis que NUNCA são a parte representada pela DPU — perito,
        // testemunha, terceiro. Diferente de Autor/Réu (a DPU pode estar em
        // QUALQUER polo dependendo do caso — por isso não dá pra usar polo pra
        // decidir "é meu"), esses papéis são estruturalmente sempre de outra
        // pessoa; excluir é seguro em qualquer caso, não uma adivinhação.
        // Sem isso, quando o Painel não tem dado nenhum pro processo (fail-
        // open) e a única intimação ativa na janela é a de um perito (ex.:
        // prazo pra entregar laudo), ela aparecia como se fosse da DPU.
        _ehTerceiroNuncaDpu(it) {
            const papel = (it.parte_intimada || '').split(/[-–]/)[0].trim().toUpperCase();
            return ['PERITO', 'PERITA', 'TESTEMUNHA', 'TERCEIRO', 'ASSISTENTE TÉCNICO', 'ASSISTENTE TECNICO']
                .includes(papel);
        },
        // SEM fail-open (removido — era o problema real): sem prazo confirmado
        // no Painel pra este processo, o universo é VAZIO. Nunca cai pra
        // mostrar a triagem MCP crua (que pode trazer INSS, perito, etc.) só
        // porque o Painel não tem dado — nesse caso `temInfoIncompleta(idx)`
        // sinaliza a situação na tela (ver partials/processo_judicial.html),
        // orientando a sincronizar a aba Prazos, em vez de arriscar mostrar
        // algo que nunca esteve confirmado como sendo da DPU.
        temInfoIncompleta(idx) {
            const lista = this.prazosProcurador[idx];
            return !lista || !lista.length;
        },
        intimacoesAtivas(idx) {
            const lista = this.prazosProcurador[idx];
            if (!lista || !lista.length) return [];

            const r = this.resultados[idx];
            const triagem = (r ? (r.intimacoes || []) : [])
                .filter((it) => it.ativa && !this._ehTerceiroNuncaDpu(it));
            const usadas = new Set();
            return lista.map((p) => {
                const diaP = this._soData(p.data_envio);
                const diasP = parseInt(p.prazo_dias, 10);
                const iMatch = triagem.findIndex((it, i) => {
                    if (usadas.has(i)) return false;
                    const diaIt = this._soData(it.enviado_em);
                    if (!diaP || !diaIt || diaP !== diaIt) return false;
                    const diasIt = parseInt(it.prazo_dias, 10);
                    if (!isNaN(diasP) && !isNaN(diasIt) && diasP !== diasIt) return false;
                    return true;
                });
                if (iMatch !== -1) {
                    usadas.add(iMatch);
                    return triagem[iMatch]; // item da triagem: já tem evento_referenciado/docs
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
                };
            });
        },
        totalIntimacoesAtivas() {
            return this.processos.reduce((acc, _, idx) => acc + this.intimacoesAtivas(idx).length, 0);
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
        docsDisponiveis(idx, it) {
            const ref = it.evento_referenciado;
            if (!ref) return [];
            const ev = this.eventoDe(idx, ref.numero);
            return (ev && ev.arquivos_baixados) || [];
        },
        temDocPendente(idx, it) {
            const ref = it.evento_referenciado;
            if (!ref || ref.fora_da_janela) return false;
            const ev = this.eventoDe(idx, ref.numero);
            return !!(ev && (ev.documentos || []).length && !(ev.arquivos_baixados || []).length);
        },
        // Documentos do evento referenciado AINDA NÃO baixados — um badge por
        // documento (mesmo padrão da coluna "Documentos" da página "Consultar
        // processo"), clicar em qualquer um baixa todos de uma vez (mesmo
        // request de sempre — ver baixarDocIntimacao).
        docsPendentes(idx, it) {
            const ref = it.evento_referenciado;
            if (!ref) return [];
            const ev = this.eventoDe(idx, ref.numero);
            return (ev && ev.documentos) || [];
        },
        linkArquivo(idx, arq) {
            const numero = this.processos[idx].processo_judicial;
            return '/api/eproc-mcp/arquivo?' + new URLSearchParams({ numero: numero, arquivo: arq.arquivo }).toString();
        },

        // Baixa o(s) documento(s) do evento referenciado por UMA intimação — sob demanda,
        // só quando o defensor clica no badge do documento pendente (não baixa tudo
        // automaticamente). Clicar em qualquer badge do mesmo evento baixa todos de uma vez.
        async baixarDocIntimacao(idx, it) {
            const ref = it.evento_referenciado;
            if (!ref) return;
            const chave = idx + ':' + ref.numero;
            if (this.baixando[chave]) return;
            this.baixando[chave] = true;
            const numero = this.processos[idx].processo_judicial;
            try {
                const resp = await fetch('/api/eproc-mcp/baixar-documentos', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ numero: numero, instancia: 1, evento_ids: [String(ref.numero)] }),
                });
                const r = await resp.json();
                if (!resp.ok || r.erro) {
                    showToast(r.erro || 'Falha ao baixar o documento da intimação.', 'error');
                    return;
                }
                const arqs = (r.arquivos_por_evento || {})[String(ref.numero)];
                const ev = this.eventoDe(idx, ref.numero);
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
