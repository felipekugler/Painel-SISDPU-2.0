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
//
// A busca de intimações é 100% ao vivo (headless, sem navegador — mesmo backend MCP da
// página "Consultar processo") e roda em paralelo pra cada processo, assim que a seção
// abre (ela nasce sempre aberta quando há processo — ver partials/processo_judicial.html).
function processoJudicialBlock(cfg) {
    cfg = cfg || {};
    return {
        processos: cfg.processos || [],
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
        },

        // Pega só "dd/mm/aaaa" de uma string de data/hora (formatos batem: a
        // triagem MCP dá "24/07/2026 03:50:17", o Painel dá "27/07/2026 17:17:38").
        _soData(s) {
            const m = (s || '').match(/(\d{2}\/\d{2}\/\d{4})/);
            return m ? m[1] : '';
        },
        // A triagem MCP lê o processo INTEIRO — traz intimação de TODAS as partes
        // (autor via DPU E réu, tipicamente o INSS, com procurador próprio). Sem
        // filtro, a seção "Processo Judicial" do PAJ mostrava também os prazos do
        // INSS, que não são responsabilidade da DPU.
        //
        // NÃO filtra por nome do assistido (era a 1ª versão) — nome é frágil:
        // representante legal cadastrado separado, litisconsórcio ativo com mais
        // de um assistido, ou nome diferente entre SISDPU e e-proc geram FALSO
        // NEGATIVO (omite intimação real, muito pior que mostrar a do INSS).
        //
        // Em vez disso, confirma contra o MEU Painel do Procurador (cache da aba
        // Prazos): o e-proc já resolve isso pelo PROCURADOR REGISTRADO, não por
        // nome — cobre representante legal e litisconsórcio automaticamente, sem
        // heurística nenhuma da nossa parte. Casa por (mesmo processo já garantido
        // pelo idx) + mesmo dia de envio + mesmo nº de dias do prazo.
        //
        // FAIL-OPEN: sem sync do Painel para este processo (nunca sincronizado,
        // ou sem prazo ativo nele agora), não filtra — mostra tudo. Prefere
        // mostrar demais (ex.: INSS) a arriscar omitir uma intimação real por
        // falta de informação ou dessincronia entre as duas fontes.
        _confirmadaNoPainel(idx, it) {
            const lista = this.prazosProcurador[idx];
            if (!lista || !lista.length) return true;
            const diaIt = this._soData(it.enviado_em);
            const diasIt = parseInt(it.prazo_dias, 10);
            return lista.some((p) => {
                const diaP = this._soData(p.data_envio);
                if (!diaIt || !diaP || diaIt !== diaP) return false;
                const diasP = parseInt(p.prazo_dias, 10);
                if (!isNaN(diasIt) && !isNaN(diasP) && diasIt !== diasP) return false;
                return true;
            });
        },
        // Só as ATIVAS (pendentes/abertas) confirmadas como sendo da DPU — nunca as
        // fechadas, nem as de outras partes (ex.: INSS) quando confirmável.
        intimacoesAtivas(idx) {
            const r = this.resultados[idx];
            return r ? (r.intimacoes || []).filter((it) => it.ativa && this._confirmadaNoPainel(idx, it)) : [];
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
        linkArquivo(idx, arq) {
            const numero = this.processos[idx].processo_judicial;
            return '/api/eproc-mcp/arquivo?' + new URLSearchParams({ numero: numero, arquivo: arq.arquivo }).toString();
        },

        // Baixa o(s) documento(s) do evento referenciado por UMA intimação — sob demanda,
        // só quando o defensor clica "Ler intimação" (não baixa tudo automaticamente).
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
    };
}
