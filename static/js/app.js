/* oficio-geral-ui — utilidades globais */

/* Formato oficial de exibicao do numero do PAJ: AAAA/UUU-NNNNN (ano/unidade-numero).
   Usado em toda tela do sistema (watchlist, prazos, dashboard, detalhes, etc.).
   Internamente o paj_norm canonico continua sendo PAJ-YYYY-NNN-NNNNN (URLs,
   nomes de pasta, chaves de JSON) — essas funcoes traduzem entre os formatos. */

/* PAJ-2026-044-00187 -> "2026/044-00187". Defensivo: devolve a string original
   se nao bater o regex (assim renderiza algo em vez de quebrar a tela). */
function formatarPajNorm(paj_norm) {
    if (!paj_norm) return '';
    const m = String(paj_norm).match(/^PAJ-(\d{4})-(\d{3})-(\d{5})$/);
    return m ? `${m[1]}/${m[2]}-${m[3]}` : paj_norm;
}

/* Auto-formata o que o usuario digitou no input do PAJ. Aceita qualquer entrada
   (so digitos, com separadores diferentes, com prefixo PAJ-): extrai os 12
   digitos e remonta no padrao AAAA/UUU-NNNNN conforme o usuario digita. */
function formatarInputPaj(raw) {
    const digits = String(raw || '').replace(/\D/g, '').slice(0, 12);
    if (digits.length <= 4) return digits;
    if (digits.length <= 7) return `${digits.slice(0, 4)}/${digits.slice(4)}`;
    return `${digits.slice(0, 4)}/${digits.slice(4, 7)}-${digits.slice(7)}`;
}

/* Mascara de numero de processo judicial (NPU/CNJ) pro campo "Consultar
   processo" do topo da pagina (base.html) — mesmo espirito de
   formatarInputPaj acima: extrai so os 20 digitos e remonta no padrao
   NNNNNNN-DD.AAAA.J.TR.OOOO conforme o usuario digita. */
function formatarInputProcesso(raw) {
    const d = String(raw || '').replace(/\D/g, '').slice(0, 20);
    let out = d.slice(0, 7);
    if (d.length > 7) out += '-' + d.slice(7, 9);
    if (d.length > 9) out += '.' + d.slice(9, 13);
    if (d.length > 13) out += '.' + d.slice(13, 14);
    if (d.length > 14) out += '.' + d.slice(14, 16);
    if (d.length > 16) out += '.' + d.slice(16, 20);
    return out;
}

/* "2026/044-00187" -> "PAJ-2026-044-00187". Retorna null se a string nao bater
   o formato oficial (use para validar input antes de enviar para a API). */
function pajExibicaoParaNorm(exibicao) {
    const m = String(exibicao || '').trim().match(/^(\d{4})\/(\d{3})-(\d{5})$/);
    return m ? `PAJ-${m[1]}-${m[2]}-${m[3]}` : null;
}

/* Watchlist toggle — usado pelo paj_detail.html como x-data="watchlistToggle('PAJ-...')".
   Verifica se o PAJ esta na watchlist e expoe acoes adicionar/remover. */
function watchlistToggle(pajNorm) {
    return {
        watched: false,
        item: null,
        loading: false,

        async init() {
            await this.refresh();
        },

        async refresh() {
            try {
                const r = await fetch('/api/watchlist/check/' + encodeURIComponent(pajNorm));
                const data = await r.json();
                this.watched = !!data.watched;
                this.item = data.item || null;
            } catch (e) {
                this.watched = false;
                this.item = null;
            }
        },

        async adicionar() {
            const modal = document.getElementById('modal-watchlist-motivo');
            if (modal) {
                modal.showModal();
            }
        },

        async confirmarAdicionar(motivo) {
            this.loading = true;
            try {
                const r = await fetch('/api/watchlist', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paj_norm: pajNorm, motivo: motivo || '' }),
                });
                if (!r.ok) {
                    const err = await r.json().catch(() => ({}));
                    showToast('Erro: ' + (err.detail || 'falha ao adicionar'), 'error');
                    return;
                }
                const modal = document.getElementById('modal-watchlist-motivo');
                if (modal) modal.close();
                showToast('Adicionado à watchlist', 'success');
                await this.refresh();
            } catch (e) {
                showToast('Falha de rede', 'error');
            } finally {
                this.loading = false;
            }
        },

        async remover() {
            if (!confirm('Remover ' + pajNorm + ' da watchlist?')) return;
            this.loading = true;
            try {
                const r = await fetch('/api/watchlist/' + encodeURIComponent(pajNorm), { method: 'DELETE' });
                if (!r.ok) {
                    showToast('Erro ao remover', 'error');
                    return;
                }
                showToast('Removido da watchlist', 'info');
                await this.refresh();
            } catch (e) {
                showToast('Falha de rede', 'error');
            } finally {
                this.loading = false;
            }
        },
    };
}

/* Garante a sessão do e-proc NO NAVEGADOR do painel antes de uma ação que
 * depende dela (ex.: "Preparar petição inicial no e-proc"): confere a sessão
 * de verdade e, se caiu, tenta a reconexão automática (login com as
 * credenciais do MCP do e-proc / cofre + 2FA). Promise<bool> — false = não
 * conectou; quem chama NÃO prossegue e avisa (pedido do Defensor 16/10/2026). */
window.garantirEproc = async function () {
    const logado = async (url) => {
        try { return !!((await (await fetch(url)).json()) || {}).logado; } catch (e) { return false; }
    };
    if (await logado('/api/prazos/sessao')) return true;
    showToast('e-proc desconectado — tentando reconectar automaticamente…', 'info');
    const ok = await logado('/api/prazos/abrir');
    if (typeof window.atualizarSelos === 'function') window.atualizarSelos();
    if (ok) showToast('e-proc reconectado.', 'success');
    return ok;
};

function showToast(msg, type) {
    type = type || 'info';
    const container = document.getElementById('toast-container');
    if (!container) return;

    const alertClass = {
        'success': 'alert-success',
        'error': 'alert-error',
        'warning': 'alert-warning',
        'info': 'alert-info'
    }[type] || 'alert-info';

    let el = document.createElement('div');
    el.className = 'alert ' + alertClass + ' text-sm py-2 px-4 shadow-lg';
    el.innerHTML = '<span>' + msg + '</span>';
    container.appendChild(el);

    setTimeout(function() {
        el.style.opacity = '0';
        el.style.transition = 'opacity 0.3s';
        setTimeout(function() { el.remove(); }, 300);
    }, 3000);
}

/* HTMX global event handlers */
document.addEventListener('htmx:responseError', function(evt) {
    showToast('Erro na requisição: ' + evt.detail.xhr.status, 'error');
});

/* Sincronizacao da caixa SISDPU — o widget flutuante compartilhado
 * (static/js/sync_widget.js, objeto global `SyncWidget`) cuida da conexao
 * SSE, do log, do cancelar e de sobreviver a navegacao entre paginas. As
 * funcoes abaixo so' montam a URL/titulo de cada tipo de sincronizacao e
 * decidem o que fazer quando ela termina (recarregar a pagina atual, so'
 * avisar por toast, devolver dados frescos pra um componente Alpine etc). */

/* Preferencia persistente "OCR simples" (padrao: desabilitado — so baixa os
 * anexos, sem rodar OCR de baixa qualidade na sincronizacao). Controlada por
 * um checkbox replicado nos dropdowns de sync; usa localStorage pra nao
 * precisar existir na tela exata onde o clique acontece. */
function _ocrSimplesAtivo() {
    return localStorage.getItem('ocrSimplesAtivo') === 'true';
}

function _syncUrlComOcr(url, temQuery) {
    if (!_ocrSimplesAtivo()) return url;
    return url + (temQuery ? '&' : '?') + 'ocr=1';
}

// Mantem todos os checkboxes ".ocr-simples-checkbox" da pagina (pode haver
// mais de um dropdown de sync na mesma tela) sincronizados entre si e com
// localStorage.
document.addEventListener('DOMContentLoaded', function() {
    const checkboxes = document.querySelectorAll('.ocr-simples-checkbox');
    const ativo = _ocrSimplesAtivo();
    checkboxes.forEach(function(cb) {
        cb.checked = ativo;
        cb.addEventListener('change', function() {
            localStorage.setItem('ocrSimplesAtivo', cb.checked ? 'true' : 'false');
            checkboxes.forEach(function(other) { other.checked = cb.checked; });
        });
    });
});

/* Preferencia persistente "usar sincronização Leve para PAJs com prazo
 * judicial aberto" (padrao: DESABILITADO — pedido do usuario 2026-09-18: a
 * 2a etapa da sincronizacao da caixa so' deve rodar se marcada
 * expressamente, nunca por padrao). Mesmo padrao de localStorage/checkbox
 * replicado do OCR simples acima. */
function _prazosAbertosAtivo() {
    return localStorage.getItem('prazosAbertosAtivo') === 'true';
}

document.addEventListener('DOMContentLoaded', function() {
    const checkboxes = document.querySelectorAll('.prazos-abertos-checkbox');
    const ativo = _prazosAbertosAtivo();
    checkboxes.forEach(function(cb) {
        cb.checked = ativo;
        cb.addEventListener('change', function() {
            localStorage.setItem('prazosAbertosAtivo', cb.checked ? 'true' : 'false');
            checkboxes.forEach(function(other) { other.checked = cb.checked; });
        });
    });
});

/* `nivel`: 'levissima' | 'leve' | 'completa' (ver terminologia do projeto).
 * Aceita tambem os booleanos antigos (true/false) por compat com quem ainda
 * chama sincronizarCaixa(true) direto (ex.: botao de sync vazio do
 * pipeline.html). Pedido do usuario 2026-09-18: a sincronizacao da caixa
 * sempre roda em 1 UNICA etapa agora — a 2a etapa (prazos em aberto) so'
 * roda se o toggle "Usar sincronização Leve para PAJs com prazo judicial
 * aberto" estiver marcado (ver _prazosAbertosAtivo acima). */
function sincronizarCaixa(nivel) {
    if (typeof nivel === 'undefined' || nivel === true) nivel = 'completa';
    if (nivel === false) nivel = 'levissima';
    // Fonte SIS-DPU 2.0: avisa o custo no teto diário antes (static/js/ao_vivo.js).
    if (window.confirmarCustoMcp && !window.confirmarCustoMcp('caixa', nivel)) return;

    let anexos, documentos;
    if (nivel === 'levissima') { anexos = 0; documentos = 0; }
    else if (nivel === 'leve') { anexos = 0; documentos = 1; }
    else { anexos = 1; documentos = 1; }

    let url = '/api/sync?anexos=' + anexos + '&documentos=' + documentos;
    url = _syncUrlComOcr(url, true);

    const usarPrazosAbertos = _prazosAbertosAtivo();

    SyncWidget.iniciar(url, {
        titulo: 'Sincronizar caixa SISDPU',
        onDone: function(status) {
            // Se o usuário cancelou, não dispara a 2ª etapa — ela iniciaria
            // um trabalho novo bem depois do pedido de parar.
            const cancelado = status && status.log && status.log.some(function(l) {
                return l.indexOf('[CANCELADO]') !== -1;
            });
            if (cancelado) {
                showToast('Sincronização cancelada', 'warning');
                return;
            }
            if (status && status.ok === false) {
                showToast('Falha ao sincronizar a caixa — veja o log', 'error');
                return;
            }
            // 2ª etapa opcional (leve, só PAJs com prazo em aberto), em modo
            // "fire-and-forget" — SEM abrir SSE atado a esta página e SEM
            // condicionar o reload do dashboard a ela. Motivo (2026-09-18):
            // antes o reload ficava preso ao onDone dessa 2ª fase, e se a
            // conexão SSE dela caísse (ex.: reinício do servidor no meio do
            // stream, como aconteceu ao vivo), o front recebia `desconectado`
            // e o dashboard nunca recarregava — mesmo com a caixa já salva em
            // disco. Como `checarEmAndamento()` reconecta sozinha a qualquer
            // sincronização em andamento em TODA página nova, a fase de
            // prazos (quando disparada) continua visível no widget mesmo
            // após o reload.
            if (usarPrazosAbertos) {
                fetch('/api/sync/prazos-abertos', { method: 'POST' }).catch(function() {});
            }
            showToast('Caixa sincronizada — recarregando dashboard', 'success');
            setTimeout(function() { window.location.reload(); }, 500);
        },
    });
}

/* Atualizar caixa — reconciliacao rapida (1 pagina, sem abrir cada PAJ):
 * remove do dashboard os PAJs ja sincronizados que saíram da caixa de
 * entrada, e AVISA se ha PAJs na caixa que ainda nao tem pasta local (nunca
 * sincronizados). NAO adiciona PAJs novos por si so' — isso continua sendo
 * papel do "Sincronizar caixa" completo; aqui e' so' deteccao + aviso (badge
 * "X PAJs a sincronizar" no cabecalho, ver dashboardApp() em dashboard.html).
 * Sem modal/SSE — endpoint responde direto. */
function atualizarCaixa() {
    const btn = document.getElementById('atualizar-caixa-btn');
    if (btn) { btn.disabled = true; }
    showToast('Verificando caixa de entrada...', 'info');

    fetch('/api/sync/atualizar-caixa', { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (!d.ok) {
                showToast('Erro ao atualizar: ' + (d.mensagem || 'falha desconhecida'), 'error');
                return;
            }
            window.dispatchEvent(new CustomEvent('pendentes-sync-atualizado', { detail: d.novos_pendentes || 0 }));
            if (d.removidos > 0) {
                showToast(d.removidos + ' PAJ(s) saíram da caixa e foram removidos do dashboard', 'success');
                window.location.reload();
                return;
            }
            if (d.novos_pendentes > 0) {
                showToast(d.novos_pendentes + ' PAJ(s) na caixa de entrada ainda não sincronizados — use "Sincronizar caixa" para trazê-los', 'warning');
            } else {
                showToast('Nenhum PAJ saiu ou entrou na caixa — tudo em dia', 'info');
            }
        })
        .catch(function(e) { showToast('Falha de rede: ' + e.message, 'error'); })
        .finally(function() {
            if (btn) { btn.disabled = false; }
        });
}

/* Sincronizacao da watchlist inteira — mesmo modal, endpoint /api/sync/watchlist.
 * Itera todos os PAJs ativos via busca global do SIS-DPU (nao depende deles
 * estarem na caixa de entrada). */
function sincronizarWatchlist(baixarAnexos) {
    if (typeof baixarAnexos === 'undefined') baixarAnexos = true;

    let url = '/api/sync/watchlist';
    if (!baixarAnexos) url += '?anexos=0';
    url = _syncUrlComOcr(url, !baixarAnexos);

    SyncWidget.iniciar(url, {
        titulo: 'Sincronizar watchlist',
        onDone: function(status) {
            if (status && status.ok === false) {
                showToast('Falha ao sincronizar a watchlist — veja o log', 'error');
                return;
            }
            showToast('Watchlist sincronizada — recarregando', 'success');
            setTimeout(function() { window.location.reload(); }, 500);
        },
    });
}

/* Sincronizacao de UM unico PAJ — mesmo modal, endpoint /api/sync/paj/{paj}.
 * `nivel`: 'levissima' | 'leve' | 'completa' (mesma terminologia/3 hipoteses
 * do botao "Sincronizar caixa" do dashboard — ver sincronizarCaixa() acima).
 * Aceita tambem os booleanos antigos (true/false) por compat. */
function sincronizarPaj(pajNorm, nivel) {
    if (typeof nivel === 'undefined' || nivel === true) nivel = 'completa';
    if (nivel === false) nivel = 'levissima';
    if (window.confirmarCustoMcp && !window.confirmarCustoMcp('paj', nivel)) return;

    let anexos, documentos;
    if (nivel === 'levissima') { anexos = 0; documentos = 0; }
    else if (nivel === 'leve') { anexos = 0; documentos = 1; }
    else { anexos = 1; documentos = 1; }

    let url = '/api/sync/paj/' + encodeURIComponent(pajNorm) + '?anexos=' + anexos + '&documentos=' + documentos;
    url = _syncUrlComOcr(url, true);

    SyncWidget.iniciar(url, {
        titulo: 'Sincronizar ' + pajNorm,
        onDone: function(status) {
            if (status && status.ok === false) {
                showToast('Falha ao sincronizar ' + pajNorm + ' — veja o log', 'error');
                return;
            }
            showToast('PAJ sincronizado', 'success');
            // So' recarrega se ainda estivermos na pagina desse PAJ — se o
            // usuario navegou pra outro lugar no meio, este callback nem
            // chega a rodar (o contexto JS da pagina anterior foi destruido);
            // esta checagem e' so' pro caso de ele ter voltado pra ca antes
            // de terminar, ou simplesmente nunca saido.
            if (window.location.pathname.indexOf(pajNorm) !== -1) {
                window.location.reload();
            }
        },
    });
}

/* Download seletivo de documentos (sincronizacao parcial por seq) — usado
 * pelos botoes "Baixar todos"/"Baixar selecionados" da coluna Documentos
 * (static/js/movimentacoes.js). Diferente das demais syncs: NAO recarrega a
 * pagina ao concluir — busca só os anexos atualizados e devolve pro
 * componente Alpine atualizar os badges no lugar. Retorna a lista
 * `anexos_sisdpu` atualizada, ou null se deu erro/cancelou. */
function baixarDocumentosPaj(pajNorm, ids) {
    return new Promise(function(resolve) {
        let url = '/api/sync/paj/' + encodeURIComponent(pajNorm)
                + '/baixar-documentos?ids=' + encodeURIComponent(ids.join(','));

        SyncWidget.iniciar(url, {
            titulo: 'Baixar documento(s) — ' + pajNorm,
            minimizado: true,
            onDone: function(status) {
                if (status && status.ok === false) {
                    showToast('Falha ao baixar documento(s) — veja o log', 'error');
                    resolve(null);
                    return;
                }
                showToast('Documento(s) baixado(s)', 'success');
                fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/movimentacoes')
                    .then(function(r) { return r.json(); })
                    .then(function(data) { resolve(data.anexos_sisdpu || []); })
                    .catch(function() { resolve(null); });
            },
        }).then(function(iniciou) {
            if (!iniciou) resolve(null);
        });
    });
}

/* Obtem a relação de documentos de UM seq específico, sem baixar nada —
 * usado pelo minimenu do clipe de anexo na coluna Documentos (ver
 * movimentacoes_tabela.html/movimentacoes.js), quando o ícone
 * (`mov.tem_anexo_icone`) sinaliza que aquele seq pode ter documento mas a
 * relação ainda não foi obtida. Bem mais barato que "Atualizar
 * movimentações" (reprocessa TODA seq nova, não só a pedida). Ao concluir,
 * busca as movimentações frescas (o seq pedido já vem com `documentos`
 * preenchido) e devolve pro componente Alpine substituir no lugar — mesmo
 * padrão de atualizarMovimentacoesPaj()/baixarDocumentosPaj() acima. */
function obterDocumentosSeqPaj(pajNorm, seq) {
    return new Promise(function(resolve) {
        let url = '/api/sync/paj/' + encodeURIComponent(pajNorm)
                + '/seq/' + encodeURIComponent(seq) + '/documentos';

        SyncWidget.iniciar(url, {
            titulo: 'Obter documentos — ' + pajNorm + ' (seq ' + seq + ')',
            minimizado: true,
            onDone: function(status) {
                if (status && status.ok === false) {
                    showToast('Falha ao obter a relação de documentos — veja o log', 'error');
                    resolve(null);
                    return;
                }
                showToast('Relação de documentos obtida', 'success');
                fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/movimentacoes')
                    .then(function(r) { return r.json(); })
                    .then(function(data) { resolve(data.ok === false ? null : data); })
                    .catch(function() { resolve(null); });
            },
        }).then(function(iniciou) {
            if (!iniciou) resolve(null);
        });
    });
}

/* Baixa os documentos de UM seq específico — um clique só: se a relação
 * ainda não for conhecida, o backend obtém primeiro (mesmo passo da função
 * acima) e já encadeia pro download, tudo numa única sincronização em
 * segundo plano (ver sincronizador.rodar_baixar_documentos_seq). Segunda
 * opção do minimenu do clipe de anexo ("Baixar documentos"). */
function baixarDocumentosSeqPaj(pajNorm, seq) {
    return new Promise(function(resolve) {
        let url = '/api/sync/paj/' + encodeURIComponent(pajNorm)
                + '/seq/' + encodeURIComponent(seq) + '/baixar-documentos';

        SyncWidget.iniciar(url, {
            titulo: 'Baixar documentos — ' + pajNorm + ' (seq ' + seq + ')',
            minimizado: true,
            onDone: function(status) {
                if (status && status.ok === false) {
                    showToast('Falha ao baixar documentos desta seq — veja o log', 'error');
                    resolve(null);
                    return;
                }
                showToast('Documentos da seq baixados', 'success');
                fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/movimentacoes')
                    .then(function(r) { return r.json(); })
                    .then(function(data) { resolve(data.ok === false ? null : data); })
                    .catch(function() { resolve(null); });
            },
        }).then(function(iniciou) {
            if (!iniciou) resolve(null);
        });
    });
}

/* Sincronizacao EXCLUSIVA e INCREMENTAL das movimentacoes (icone de atualizar
 * da tabela compartilhada, static/js/movimentacoes.js) — relê a lista de
 * movimentacoes no SISDPU, mas so' abre popup de anexo pras movimentacoes
 * REALMENTE novas (as ja conhecidas mantem os documentos ja indexados); nao
 * baixa PDF, nao recarrega a pagina. Mesmo padrao de baixarDocumentosPaj:
 * reusa o widget flutuante de sync e devolve os dados frescos pro componente
 * Alpine trocar so' o conteudo da tabela. Resolve {movimentacoes, anexos_sisdpu,
 * max_seq} ou null se deu erro/cancelou.
 *
 * Fonte SIS-DPU 2.0 (MCP): sem modal de sincronização — releitura incremental
 * silenciosa pelo MCP (a mesma da abertura da página do PAJ). Vale para todos os
 * chamadores: pós-gravação, mera ciência, queda de conexão e o ícone da tabela. */
async function atualizarMovimentacoesPajMcp(pajNorm) {
    try {
        const r = await fetch('/api/sisdpu2/ao-vivo/paj/' + encodeURIComponent(pajNorm) + '?forcar=true', { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        if (!d.ok) {
            showToast('Não foi possível atualizar as movimentações: ' + (d.motivo || 'erro'), 'error');
            return null;
        }
        // A página do PAJ já está em dia — o recarregamento seguinte não precisa reler.
        try { sessionStorage.setItem('ao-vivo-recarregado-' + pajNorm, String(Date.now())); } catch (e) {}
        const data = await (await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/movimentacoes')).json();
        return data.ok === false ? null : data;
    } catch (e) {
        showToast('Falha de rede ao atualizar as movimentações', 'error');
        return null;
    }
}

/* Fonte SIS-DPU 2.0, logo depois de gravar: relê o PAJ (forçado) e baixa os
 * anexos novos ESPERANDO terminar, com o andamento em `progresso(msg)` (o modal
 * de execução do card) — a página do PAJ abre uma vez só, já em dia, sem reler
 * nem recarregar sozinha depois (marcas em sessionStorage lidas por ao_vivo.js). */
async function releituraPosGravacaoMcp(pajNorm, progresso) {
    const avisar = (m) => { try { if (progresso) progresso(m); } catch (e) {} };
    const marcar = (k) => { try { sessionStorage.setItem(k + pajNorm, String(Date.now())); } catch (e) {} };
    const base = '/api/sisdpu2/ao-vivo/paj/' + encodeURIComponent(pajNorm);
    avisar('Atualizando as movimentações do PAJ pelo SIS-DPU 2.0…');
    const r = await (await fetch(base + '?forcar=true', { method: 'POST' })).json().catch(() => ({}));
    if (!r.ok) { avisar('Não foi possível reler o PAJ agora (' + (r.motivo || 'erro') + ') — a página relê ao abrir.'); return; }
    marcar('ao-vivo-recarregado-');
    const a = await (await fetch(base + '/anexos', { method: 'POST' })).json().catch(() => ({}));
    if (!a.ok || (!a.iniciado && a.motivo !== 'em_andamento')) {
        if (a.ok) marcar('ao-vivo-anexos-em-dia-');
        return;
    }
    avisar('Baixando os anexos novos do PAJ…');
    for (let i = 0; i < 900; i++) {  // até ~30 min
        await new Promise((ok) => setTimeout(ok, 1500));
        const p = await (await fetch(base + '/anexos')).json().catch(() => null);
        if (!p) continue;
        const pr = p.progresso || {};
        if (p.rodando) {
            if (pr.total) avisar('Baixando os anexos novos do PAJ ' + (pr.feitos || 0) + '/' + pr.total + '…');
            continue;
        }
        const res = p.resultado || {};
        const novos = pr.baixados != null ? pr.baixados : (res.baixados || 0);  // progresso = só os novos
        avisar('Anexos: ' + novos + ' baixado(s)'
            + ((res.erros || []).length ? ', ' + res.erros.length + ' com erro' : '') + '.');
        marcar('ao-vivo-anexos-em-dia-');
        return;
    }
}

function atualizarMovimentacoesPaj(pajNorm) {
    if (window.PAINEL_FONTE_SISDPU_MCP) return atualizarMovimentacoesPajMcp(pajNorm);
    return new Promise(function(resolve) {
        let url = '/api/sync/paj/' + encodeURIComponent(pajNorm) + '/movimentacoes';

        SyncWidget.iniciar(url, {
            titulo: 'Atualizar movimentações — ' + pajNorm,
            minimizado: true,
            onDone: function(status) {
                if (status && status.ok === false) {
                    showToast('Falha ao atualizar movimentações', 'error');
                    resolve(null);
                    return;
                }
                showToast('Movimentações atualizadas', 'success');
                fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/movimentacoes')
                    .then(function(r) { return r.json(); })
                    .then(function(data) { resolve(data.ok === false ? null : data); })
                    .catch(function() { resolve(null); });
            },
        }).then(function(iniciou) {
            if (!iniciou) resolve(null);
        });
    });
}

/* Troca o icone de um botao por um spinner amarelo (padrao "loading
   loading-spinner ... text-warning" ja usado no badge de Analise IA desta
   mesma pagina) enquanto uma tarefa roda, e devolve uma funcao pra restaurar
   o icone original — usar em finally, pra restaurar mesmo se a tarefa falhar. */
function trocarIconePorSpinner(btn) {
    if (!btn) return () => {};
    const alvo = btn.querySelector('.btn-icone') || btn;
    const original = alvo.innerHTML;
    alvo.innerHTML = '<span class="loading loading-spinner loading-xs text-warning"></span>';
    btn.disabled = true;
    return () => {
        alvo.innerHTML = original;
        btn.disabled = false;
    };
}

/* Baixar PAJ Completo — antes de gerar o PDF, confirma que a sincronizacao
   completa (com download de anexos) ja rodou com sucesso. Sem isso o PDF
   sairia "completo" faltando os documentos que nunca foram baixados.

   Usa fetch+blob (em vez de so' trocar window.location.href) especificamente
   pra ter um sinal real de "terminou" — e' o que permite o spinner do botao
   ficar ligado durante toda a geracao (que pode levar alguns segundos num PAJ
   com muitos anexos) e apagar exatamente quando o PDF estiver pronto. */
async function baixarPajCompleto(pajNorm, btn) {
    let status;
    try {
        const resp = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/sincronizacao-completa');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        status = await resp.json();
    } catch (e) {
        showToast('Não foi possível verificar a sincronização do PAJ', 'error');
        return;
    }

    if (!status.completo) {
        const querSincronizar = confirm(
            'Não é possível gerar arquivo completo do PAJ sem a sincronização completa ' +
            'com download dos documentos. Deseja fazer a sincronização completa agora?'
        );
        if (querSincronizar) {
            sincronizarPaj(pajNorm, 'completa');
        }
        return;
    }

    const restaurarIcone = trocarIconePorSpinner(btn);
    try {
        const resp = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/download-completo');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = pajNorm + ' - Download Completo.pdf';
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
    } catch (e) {
        showToast('Não foi possível gerar o PDF completo do PAJ', 'error');
    } finally {
        restaurarIcone();
    }
}

/* Baixar anexos antigos a partir de uma data — completa overflow do sync padrao.
   Reusa o widget flutuante de sync para o log SSE. */
function baixarAnexosDesde(pajNorm) {
    let input = document.getElementById('anexos-desde-data');
    let data = (input && input.value || '').trim();
    if (!data) {
        showToast('Escolha uma data de corte', 'warning');
        return;
    }

    let url = '/api/sync/paj/' + encodeURIComponent(pajNorm)
            + '/anexos-desde?data=' + encodeURIComponent(data);
    url = _syncUrlComOcr(url, true);

    SyncWidget.iniciar(url, {
        titulo: 'Baixar anexos a partir de ' + data + ' — ' + pajNorm,
        semCancelar: true,  // este endpoint nao suporta cancel — esconde o botao
        onDone: function(status) {
            if (status && status.ok === false) {
                showToast('Falha ao baixar anexos — veja o log', 'error');
                return;
            }
            showToast('Anexos baixados', 'success');
            if (window.location.pathname.indexOf(pajNorm) !== -1) {
                window.location.reload();
            }
        },
    });
}

/** Chamado após o console genérico "Ações no SISDPU" (movimentar/tramitar/
 * concluir) terminar de executar NA PÁGINA DO PAJ — seja pelo próprio console
 * ou pelo atalho "Concluir PAJ da Caixa de Entrada" do cabeçalho: registra a
 * atividade no Histórico do painel e, se o trâmite foi concluído, marca o PAJ
 * como fora da caixa (some do dashboard sem esperar a próxima sincronização).
 * Mesmo efeito que posExecucaoRetorno() tem na Retornos — ver
 * services/paj_service.registrar_pos_execucao_sisdpu().
 *
 * Também roda aqui, e ESPERA terminar, a sincronização LEVE (só movimentações,
 * sem anexos) da tabela de Movimentações — reusa a MESMA função do ícone de
 * atualizar (`atualizarMovimentacoesPaj()`, com seu modal de progresso), só
 * quando uma MOVIMENTAÇÃO de fato foi registrada com sucesso (`resultado.mov_seq`
 * truthy — SÓ vem preenchido pelo backend quando a etapa de movimentar rodou E
 * deu certo, ver services/acoes_sisdpu_service.py linha ~588; tramitação/
 * conclusão que falharem DEPOIS ainda deixam a movimentação valendo, por isso
 * "parcial"/"ja_ausente" também disparam a atualização — só filtra "falha").
 *
 * BUG CORRIGIDO 2026-09-23: esta checagem usava `resultado.fase` (não
 * `mov_seq`) — só que o backend NUNCA devolve uma chave "fase" no resultado
 * (só "concluido"/"tramitado_para"/"mov_seq"/"concluidos", ver o mesmo trecho
 * acima), então a condição era SEMPRE falsa e a atualização de movimentações
 * nunca rodava, pra NENHUM chamador — inclusive "Desarquivar PAJ", que lança
 * uma fase nova e por isso mais dependia disto (relatado pelo usuário
 * 2026-09-23: página recarregava mas continuava com status/movimentações
 * antigos, porque o reload sempre corria ANTES de qualquer dado novo existir
 * pra recarregar).
 *
 * IMPORTANTE: isto precisa ser ESPERADO (não só disparado) por quem chama —
 * static/js/acoes_sisdpu.js recarrega a página inteira logo depois de chamar
 * onExecutado(); se a sincronização real no SISDPU (que leva alguns segundos)
 * não tivesse terminado de escrever em metadata.json ANTES do reload, a
 * página recarregava mostrando as movimentações ainda desatualizadas — foi
 * exatamente o bug relatado em 2026-08-13 com o disparo por evento antigo. */
async function posExecucaoPaj(pajNorm, resultado, progresso) {
    // PAJ arquivado: a pasta já foi para a Lixeira — reler/baixar recriaria a pasta.
    if (resultado && resultado.paj_na_lixeira) {
        if (progresso) progresso('PAJ arquivado — pasta enviada para a Lixeira do Windows.');
    } else if (resultado && resultado.mov_seq && resultado.status !== 'falha') {
        if (window.PAINEL_FONTE_SISDPU_MCP) {
            try { await releituraPosGravacaoMcp(pajNorm, progresso); } catch (e) {}
        } else {
            try { await atualizarMovimentacoesPaj(pajNorm); } catch (e) {}
        }
    }
    try {
        const r = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/pos-execucao-sisdpu', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ resultado }),
        });
        return await r.json();
    } catch (e) {
        return { ok: false };
    }
}

/* Concluir PAJ da Caixa de Entrada — atalho no cabeçalho do PAJ pra retirar o
   trâmite da caixa sem precisar abrir o console completo "Ações no SISDPU".
   Reusa o mesmo endpoint (/api/acoes-sisdpu/executar, so' com concluir=1 —
   sem movimentar/tramitar) e o widget flutuante de sync pro log SSE, mesmo
   padrão de baixarAnexosDesde() acima. */
/** Alpine do botão "Concluir PAJ da Caixa de Entrada" do cabeçalho — decide
 * entre o caminho simples (0-1 trâmite, usa concluirPajCaixa() abaixo, com
 * log SSE) e o modal de escolha múltipla (>1 trâmite pendente), que conclui
 * os selecionados de uma vez via /api/paj/<paj>/concluir-tramites
 * (services/paj_service.concluir_tramites_caixa).
 *
 * `window._paj_tramitacoes_caixa` (do carregamento da página) é só o CHUTE
 * INICIAL — pode estar desatualizado ou `null`/nunca calculado (esse campo só
 * é preenchido pela sync COMPLETA da caixa; PAJs sincronizados por caminhos
 * avulsos/busca global nunca passam por ali). Por isso abrir() SEMPRE
 * reconfere ao vivo (/api/paj/<paj>/atualizar-tramitacoes-caixa) antes de
 * decidir — sem isso, um PAJ com >1 trâmite pendente de verdade mas
 * `tramitacoes_caixa=null` seria tratado como se tivesse só um, concluindo
 * apenas o "trâmite de trabalho" e deixando os demais abertos sem avisar
 * (bug real, relatado em 2026-08-17 no PAJ-2024-029-01535). */
function concluirPajModalApp(pajNorm, idProcesso, paj, idTramite) {
    return {
        pajNorm: pajNorm, idProcesso: idProcesso, paj: paj, idTramite: idTramite,
        tramitacoes: window._paj_tramitacoes_caixa || [],
        modalAberto: false,
        selecionados: [],
        concluindo: false,
        verificando: false,

        async abrir() {
            this.verificando = true;
            try {
                const r = await fetch(
                    '/api/paj/' + encodeURIComponent(this.pajNorm) + '/atualizar-tramitacoes-caixa',
                    { method: 'POST' }
                );
                const d = await r.json().catch(() => ({}));
                if (d && d.ok) {
                    this.tramitacoes = d.tramitacoes_caixa || [];
                } else {
                    // Não deu pra confirmar ao vivo (sync ocupada, sessão etc.) —
                    // avisa e segue com o que já tinha (chute inicial da página),
                    // em vez de travar o usuário sem nenhuma opção.
                    showToast('Não foi possível confirmar os trâmites pendentes ao vivo ('
                        + (d.mensagem || 'erro') + ') — usando os dados já carregados.', 'warning');
                }
            } catch (e) {
                showToast('Falha de rede ao confirmar trâmites pendentes — usando os dados já carregados.', 'warning');
            } finally {
                this.verificando = false;
            }

            if (this.tramitacoes.length <= 1) {
                // O trâmite que a conferência ao vivo ACABOU de ler (o "trâmite de
                // trabalho" da página pode estar vazio/desatualizado — bug real
                // 13/10/2026, PAJ 2026/029-02727: aviso "não está com trâmite
                // pendente" com o trâmite aberto na caixa).
                const idLido = (this.tramitacoes[0] || {}).id_tramite || this.idTramite;
                concluirPajCaixa(this.pajNorm, this.idProcesso, this.paj, idLido);
                return;
            }
            // Pré-marca o "trâmite de trabalho" (o mesmo usado por Ações no
            // SISDPU/e-proc) — o usuário pode desmarcar ou marcar mais.
            this.selecionados = this.idTramite ? [this.idTramite] : [];
            this.modalAberto = true;
        },
        async concluirSelecionados() {
            if (!this.selecionados.length) return;
            if (typeof window.garantirCofre === 'function' && !(await window.garantirCofre())) return;
            this.concluindo = true;
            try {
                const r = await fetch('/api/paj/' + encodeURIComponent(this.pajNorm) + '/concluir-tramites', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id_tramites: this.selecionados }),
                });
                const data = await r.json();
                if (!data.ok && !(data.concluidos && data.concluidos.length)) {
                    showToast(data.detalhe || 'Falha ao concluir trâmite(s)', 'error');
                    return;
                }
                this.modalAberto = false;
                const msg = data.saiu_da_caixa
                    ? 'PAJ concluído na caixa de entrada — recarregando'
                    : (data.concluidos.length + ' trâmite(s) concluído(s) — ainda restam '
                       + (data.outros_tramites_abertos || 0) + ' — recarregando');
                showToast(msg, 'success');
                setTimeout(() => window.location.reload(), 800);
            } catch (e) {
                showToast('Falha ao concluir trâmite(s)', 'error');
            } finally {
                this.concluindo = false;
            }
        },
    };
}

async function concluirPajCaixa(pajNorm, idProcesso, paj, idTramite) {
    if (!idTramite) {
        showToast('Este PAJ não está com trâmite pendente na caixa de entrada', 'warning');
        return;
    }
    if (typeof window.garantirCofre === 'function' && !(await window.garantirCofre())) return;

    const qs = new URLSearchParams({
        id_processo: idProcesso || '', paj: paj || '', paj_norm: pajNorm || '',
        fase: '', descricao_mov: '', descricao_mov_html: '0',
        grupo: '', descricao_tram: '', prazo: '0', prazo_data: '',
        id_tramite: idTramite, concluir: '1', anexos: '[]',
    });
    const es = new EventSource('/api/acoes-sisdpu/executar?' + qs.toString());
    let ultimoRes = {};

    SyncWidget.conectar({
        es: es,
        titulo: 'Concluir PAJ da Caixa de Entrada — ' + (paj || pajNorm),
        semCancelar: true,
        transformarLinha: function (dadoCru) {
            try {
                const d = JSON.parse(dadoCru);
                ultimoRes = d;
                return d.msg || dadoCru;
            } catch (err) {
                return dadoCru;
            }
        },
        onDone: async function () {
            const res = ultimoRes;
            const ok = res.ok !== false;
            // Registra no Histórico + tira da caixa (em_caixa_atual=false) — SÓ se não
            // restar nenhum outro trâmite aberto deste PAJ (comum ter mais de um ao
            // mesmo tempo; concluir aqui só afeta o trâmite específico deste botão).
            const pos = await posExecucaoPaj(pajNorm, { ...res, idTramite: idTramite });
            let msg = ok ? 'PAJ concluído na caixa de entrada — recarregando' : 'Concluído com aviso — recarregando';
            if (ok && pos && pos.outros_tramites_abertos) {
                msg = 'Trâmite concluído — mas o PAJ ainda tem ' + pos.outros_tramites_abertos
                    + ' outro(s) trâmite(s) aberto(s) na caixa — recarregando';
            }
            showToast(msg, ok ? 'success' : 'warning');
            setTimeout(function () { window.location.reload(); }, 500);
        },
    });
}

/* Etapa do PAJ (desenho aprovado pelo Defensor 15/10/2026 — ver
 * services/etapa_paj_service.py). Escolha manual = movimentação "Etapa: …" no
 * SIS-DPU 2.0, com a ficha no modal de confirmação global (ficha_mcp.js). */
function _escolherEtapaModal(titulo, texto, opcoes, rotuloBotao) {
    return new Promise(function (resolve) {
        const dlg = document.createElement('dialog');
        dlg.className = 'modal';
        const opts = opcoes.map(function (o) {
            return '<option value="' + _htmlEscape(o.chave) + '">' + _htmlEscape(o.rotulo) + '</option>';
        }).join('');
        dlg.innerHTML = '<div class="modal-box max-w-md">'
            + '<h3 class="font-bold text-lg mb-2">' + _htmlEscape(titulo) + '</h3>'
            + '<p class="text-sm opacity-80 mb-3">' + _htmlEscape(texto) + '</p>'
            + '<select class="select select-bordered select-sm w-full" data-etapa>'
            + '<option value="" disabled selected>Selecione a etapa…</option>' + opts + '</select>'
            + '<p class="text-xs text-error mt-2 hidden" data-aviso>Selecione a etapa em que o PAJ se encontra.</p>'
            + '<div class="modal-action"><button type="button" class="btn btn-sm btn-ghost" data-cancelar>Cancelar</button>'
            + '<button type="button" class="btn btn-sm btn-primary" data-ok>' + _htmlEscape(rotuloBotao) + '</button></div>'
            + '</div>';
        document.body.appendChild(dlg);
        let valor = null;
        dlg.querySelector('[data-cancelar]').onclick = function () { dlg.close(); };
        dlg.querySelector('[data-ok]').onclick = function () {
            const v = dlg.querySelector('[data-etapa]').value;
            if (!v) { dlg.querySelector('[data-aviso]').classList.remove('hidden'); return; }
            valor = v;
            dlg.close();
        };
        dlg.addEventListener('close', function () { dlg.remove(); resolve(valor); });
        dlg.showModal();
    });
}

async function alterarEtapaPaj(pajNorm, chave, rotulo, btn) {
    if (btn) { btn.disabled = true; }
    const el = document.getElementById('etapa-paj-status');
    if (el) el.textContent = 'Gravando "Etapa: ' + rotulo + '" — confirme a ficha do SIS-DPU 2.0…';
    try {
        const r = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/etapa', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ etapa: chave }),
        });
        const d = await r.json().catch(function () { return {}; });
        if (!d.ok) {
            if (el) el.textContent = d.erro || 'A etapa não foi gravada.';
            showToast(d.erro || 'A etapa não foi gravada.', 'error');
            return;
        }
        showToast('Etapa registrada: ' + rotulo + ' — recarregando', 'success');
        setTimeout(function () { window.location.reload(); }, 500);
    } catch (e) {
        if (el) el.textContent = 'Falha ao gravar a etapa: ' + e;
    } finally {
        if (btn) { btn.disabled = false; }
    }
}

/* Desarquivar PAJ — botão do banner "PAJ arquivado" no cabeçalho (ver
 * templates/paj_detail.html). Lança a fase "Paj desarquivado" e, na mesma
 * execução, a etapa em que o PAJ volta (escolha OBRIGATÓRIA — pedido do
 * Defensor 15/10/2026), sem tramitação nem conclusão. */
async function desarquivarPaj(pajNorm, idProcesso, paj) {
    let todas = [];
    try {
        const r = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/etapa');
        todas = ((await r.json()) || {}).todas || [];
    } catch (e) { /* sem lista: não dá para desarquivar sem a etapa */ }
    if (!todas.length) { showToast('Não foi possível carregar as etapas do PAJ.', 'error'); return; }
    const etapa = await _escolherEtapaModal(
        'Desarquivar ' + (paj || pajNorm),
        'Lança a fase "Paj desarquivado" e registra a etapa em que o PAJ se encontra (duas movimentações, cada uma com sua ficha).',
        todas, 'Desarquivar');
    if (!etapa) return;
    // Fonte SIS-DPU 2.0 (MCP): é uma movimentação, gravada pelo MCP — sem cofre.
    if (!window.PAINEL_FONTE_SISDPU_MCP && typeof window.garantirCofre === 'function' && !(await window.garantirCofre())) return;

    const qs = new URLSearchParams({
        id_processo: idProcesso || '', paj: paj || '', paj_norm: pajNorm || '',
        fase: 'Paj desarquivado', descricao_mov: 'PAJ desarquivado.', descricao_mov_html: '0',
        grupo: '', descricao_tram: '', prazo: '0', prazo_data: '',
        id_tramite: '', concluir: '0', anexos: '[]', etapa: etapa,
    });
    const es = new EventSource('/api/acoes-sisdpu/executar?' + qs.toString());
    let ultimoRes = {};

    // Garante o refresh "ao final" MESMO se a conexão SSE cair antes do
    // evento `done` (ex.: instabilidade de rede/servidor) — sem isso o
    // usuário fica com o banner "PAJ Arquivado" na tela sem saber se a
    // ação realmente aconteceu (pedido do usuário 2026-09-18; mesma classe
    // de bug encontrada e corrigida em sincronizarCaixa()/prazos-abertos).
    // `SyncWidget.conectar` reatribui `es.onerror` internamente, então o
    // fallback abaixo usa `addEventListener` (listener adicional, não
    // sobrescreve o do widget) — os dois disparam juntos no mesmo evento.
    let jaFinalizado = false;
    function finalizarComReload(msg, tipo) {
        if (jaFinalizado) return;
        jaFinalizado = true;
        showToast(msg, tipo);
        setTimeout(function () { window.location.reload(); }, 500);
    }
    es.addEventListener('error', function () {
        finalizarComReload('Conexão perdida ao desarquivar — recarregando para confirmar', 'warning');
    });

    SyncWidget.conectar({
        es: es,
        titulo: 'Desarquivar PAJ — ' + (paj || pajNorm),
        semCancelar: true,
        transformarLinha: function (dadoCru) {
            try {
                const d = JSON.parse(dadoCru);
                ultimoRes = d;
                return d.msg || dadoCru;
            } catch (err) {
                return dadoCru;
            }
        },
        onDone: async function () {
            const res = ultimoRes;
            const ok = res.ok !== false;
            // Atualiza movimentações + registra no Histórico (mesmo passo de
            // concluirPajCaixa) — sem isso a tabela de Movimentações e o
            // banner do cabeçalho só refletiriam a fase nova depois de outra
            // sync manual.
            await posExecucaoPaj(pajNorm, res);
            finalizarComReload(ok ? 'PAJ desarquivado — recarregando' : 'Concluído com aviso — recarregando', ok ? 'success' : 'warning');
        },
    });
}

/* Docgen — streaming SSE do gerar_docx.py / gerar_peticao.py */
let _docgenSource = null;

function runDocgen(pajNorm, arquivo, formato, titulo) {
    let modal = document.getElementById('docgen-modal');
    let logEl = document.getElementById('docgen-log');
    let statusEl = document.getElementById('docgen-status');
    let titleEl = document.getElementById('docgen-title');
    let closeBtn = document.getElementById('docgen-close-btn');

    if (!modal || !logEl) return;

    // Reset
    logEl.textContent = '';
    titleEl.textContent = titulo || ('Gerar ' + formato.toUpperCase());
    statusEl.textContent = 'gerando...';
    statusEl.className = 'badge badge-sm badge-warning';
    closeBtn.disabled = true;
    modal.showModal();

    if (_docgenSource) {
        _docgenSource.close();
        _docgenSource = null;
    }

    let url = '/api/gerar/' + encodeURIComponent(pajNorm)
              + '?arquivo=' + encodeURIComponent(arquivo)
              + '&formato=' + encodeURIComponent(formato);

    _docgenSource = new EventSource(url);

    _docgenSource.addEventListener('log', function(e) {
        logEl.textContent += e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
    });

    _docgenSource.addEventListener('done', function(e) {
        logEl.textContent += '\n' + e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
        statusEl.textContent = 'concluído';
        statusEl.className = 'badge badge-sm badge-success';
        closeBtn.disabled = false;
        _docgenSource.close();
        _docgenSource = null;
        showToast(formato.toUpperCase() + ' gerado com sucesso', 'success');
    });

    _docgenSource.onerror = function() {
        statusEl.textContent = 'erro/desconectado';
        statusEl.className = 'badge badge-sm badge-error';
        closeBtn.disabled = false;
        if (_docgenSource) {
            _docgenSource.close();
            _docgenSource = null;
        }
    };
}

/* Elaborar Peca (background + polling + modal global) */

// Guarda o PAJ atual do modal pra funcoes auxiliares
let _resumoPajAtual = null;

async function abrirResumo(pajNorm) {
    _resumoPajAtual = pajNorm;
    const modal = document.getElementById('resumo-modal');
    const content = document.getElementById('resumo-content');
    const label = document.getElementById('resumo-paj-label');
    const linkPaj = document.getElementById('resumo-abrir-paj');
    if (!modal || !content) return;

    if (label) label.textContent = pajNorm;
    if (linkPaj) linkPaj.href = '/paj/' + pajNorm;

    try {
        const resp = await fetch('/api/elaborar/status/' + pajNorm);
        const data = await resp.json();
        // Resumo vem em Markdown — renderiza de verdade (lib `marked`, base.html)
        // em vez de mostrar a sintaxe crua (##, |tabelas|, **negrito**...).
        content.innerHTML = data.summary
            ? marked.parse(data.summary)
            : '<p>(resumo vazio — elaboração ainda não concluída?)</p>';
    } catch (e) {
        content.textContent = 'Erro ao carregar resumo: ' + e.message;
    }

    modal.showModal();
}

async function enviarCorrecaoAtual() {
    if (!_resumoPajAtual) {
        showToast('Nenhum PAJ ativo', 'warning');
        return;
    }
    await enviarCorrecao(_resumoPajAtual);
}

/** Módulo "Contatos" do cabeçalho do PAJ — App SISDPU/Telefone/E-mail/Endereço
 * vêm de window._paj_contatos (extraídos na sincronização, ver
 * ingestao/sisdpu_client.py). WhatsApp é escolha manual (o SISDPU não
 * distingue "este telefone é WhatsApp"), persistida via POST
 * /api/paj/<paj>/whatsapp. */
/** "Caixas onde o PAJ está aberto" (igual à Retornos, ver .ret-caixas em
 * base.html) + badge de "últimas tramitações" (mesmo padrão do badge "N
 * movimentações" do card Resumo IA da Retornos — abre modal com tabela).
 * Uma ÚNICA chamada a /api/paj/<paj>/tramitacoes traz os dois: caixas abertas
 * agora e as últimas N tramitações do histórico completo. */
function caixasAbertasApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        carregando: false,
        caixas: [],
        tramitacoes: [],
        modalHistorico: false,
        // `false` = já checou e realmente não há nada (ou dados válidos).
        // `true` = a checagem em si falhou (conflito de sessão do SISDPU,
        // timeout...) — NUNCA mostrar "nenhuma tramitação" nesse caso, senão
        // parece que o PAJ está vazio quando na verdade não foi verificado.
        falhouVerificacao: false,
        erroVerificacao: '',

        async init() {
            // Tempo real (16/10/2026): toda abertura/refresh consulta o SISDPU na
            // hora — inclusive quando o navegador devolve a página do PAJ pelo
            // "Voltar" (bfcache), que não roda o init de novo.
            window.addEventListener('pageshow', (ev) => { if (ev.persisted) this.carregar(); });
            await this.carregar();
        },
        async carregar() {
            this.carregando = true;
            this.falhouVerificacao = false;
            this.erroVerificacao = '';
            try {
                const j = await (await fetch('/api/paj/' + encodeURIComponent(this.pajNorm) + '/tramitacoes?limite=10',
                                             { cache: 'no-store' })).json();
                if (j.ok === false) {
                    this.falhouVerificacao = true;
                    this.erroVerificacao = j.erro || 'Não foi possível verificar agora.';
                    this.caixas = [];
                    this.tramitacoes = [];
                } else {
                    this.caixas = j.abertas || [];
                    this.tramitacoes = j.tramitacoes || [];
                }
            } catch (e) {
                this.falhouVerificacao = true;
                this.erroVerificacao = 'Falha de conexão ao verificar.';
                this.caixas = [];
                this.tramitacoes = [];
            } finally {
                this.carregando = false;
            }
        },
        abrirHistorico() {
            this.modalHistorico = true;
        },
    };
}

function contatosApp(pajNorm) {
    const d = window._paj_contatos || {};
    return {
        pajNorm: pajNorm,
        temDados: !!d.temDados,
        contatos: d.contatos || {},
        telefones: d.telefones || [],   // [{numero, obs}]
        emails: d.emails || [],
        enderecosFmt: d.enderecosFmt || [],
        whatsappNumeros: d.whatsappNumeros || [],
        copied: null,
        popoverWhatsapp: false,
        selecaoWhatsapp: [],
        salvandoWhatsapp: false,

        tituloTelefone() {
            if (!this.telefones.length) return 'Telefone — não cadastrado';
            return 'Telefone (clique para copiar):\n' + this.telefones.map((t) => t.numero).join(', ');
        },
        tituloEmail() {
            if (!this.emails.length) return 'E-mail — não cadastrado';
            return 'E-mail (clique para copiar):\n' + this.emails.join(', ');
        },
        tituloEndereco() {
            if (!this.enderecosFmt.length) return 'Correio/Endereço — não cadastrado';
            return 'Correio/Endereço (clique para copiar):\n' + this.enderecosFmt.join('\n');
        },
        tituloWhatsapp() {
            if (!this.telefones.length) return 'WhatsApp — nenhum telefone cadastrado para escolher';
            if (this.whatsappNumeros.length) return 'WhatsApp (clique para alterar):\n' + this.whatsappNumeros.join(', ');
            return 'WhatsApp — clique para escolher o(s) número(s)';
        },

        copiar(texto, campo) {
            if (!texto) return;
            navigator.clipboard.writeText(texto);
            this.copied = campo;
            setTimeout(() => { this.copied = null; }, 2000);
        },
        copiarTelefones() { this.copiar(this.telefones.map((t) => t.numero).join(', '), 'telefone'); },
        copiarEmails() { this.copiar(this.emails.join(', '), 'email'); },
        copiarEnderecos() { this.copiar(this.enderecosFmt.join(' | '), 'endereco'); },

        abrirWhatsapp() {
            if (!this.telefones.length) return;
            this.selecaoWhatsapp = [...this.whatsappNumeros];
            this.popoverWhatsapp = true;
        },
        toggleSelecao(numero) {
            const i = this.selecaoWhatsapp.indexOf(numero);
            if (i >= 0) this.selecaoWhatsapp.splice(i, 1);
            else this.selecaoWhatsapp.push(numero);
        },
        async salvarWhatsapp() {
            this.salvandoWhatsapp = true;
            try {
                const r = await fetch('/api/paj/' + encodeURIComponent(this.pajNorm) + '/whatsapp', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ numeros: this.selecaoWhatsapp }),
                });
                const data = await r.json();
                if (data.ok) {
                    this.whatsappNumeros = this.selecaoWhatsapp;
                    this.contatos.whatsapp = this.whatsappNumeros.length > 0;
                    this.popoverWhatsapp = false;
                } else {
                    showToast(data.erro || 'Falha ao salvar WhatsApp', 'error');
                }
            } catch (e) {
                showToast('Falha ao salvar WhatsApp', 'error');
            } finally {
                this.salvandoWhatsapp = false;
            }
        },
    };
}

function elaborarApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        status: 'idle',
        lastAction: '',
        summary: '',
        error: '',
        _pollTimer: null,
        // Skills disponiveis no Oficio Geral (carregadas via /api/skills)
        skills: [],
        grupos: [],
        skillSlug: '',        // '' = "Claude decide"
        skillsLoading: true,

        async init() {
            await Promise.all([this.fetchStatus(), this.carregarSkills()]);
            if (this.status === 'running') {
                this.startPolling();
            }
        },

        async carregarSkills() {
            try {
                const resp = await fetch('/api/skills?paj=' + encodeURIComponent(this.pajNorm));
                const data = await resp.json();
                this.skills = data.skills || [];
                // grupos na ordem em que aparecem, preservando unicidade
                const vistos = new Set();
                this.grupos = [];
                for (const s of this.skills) {
                    if (!vistos.has(s.grupo)) {
                        vistos.add(s.grupo);
                        this.grupos.push(s.grupo);
                    }
                }
                // Pre-seleciona a primeira skill destacada (da area do PAJ)
                const destacada = this.skills.find(s => s.destaque);
                if (destacada) {
                    this.skillSlug = destacada.slug;
                }
            } catch (e) {
                console.warn('Falha ao carregar skills:', e);
            } finally {
                this.skillsLoading = false;
            }
        },

        skillsDoGrupo(grupo) {
            // Destacadas primeiro dentro do grupo, resto depois
            const todasDoGrupo = this.skills.filter(s => s.grupo === grupo);
            return todasDoGrupo.sort((a, b) => (b.destaque ? 1 : 0) - (a.destaque ? 1 : 0));
        },

        async fetchStatus() {
            try {
                const resp = await fetch('/api/elaborar/status/' + this.pajNorm);
                const data = await resp.json();
                this.status = data.status || 'idle';
                this.lastAction = data.last_action || '';
                this.summary = data.summary || '';
                this.error = data.error || '';
            } catch (e) {
                this.error = 'Erro ao consultar status: ' + e.message;
                this.status = 'error';
            }
        },

        async iniciar() {
            this.status = 'running';
            this.lastAction = this.skillSlug ? ('invocando /' + this.skillSlug) : 'iniciando...';
            this.error = '';
            try {
                await fetch('/api/elaborar/start/' + this.pajNorm, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({skill: this.skillSlug || ''}),
                });
                const msg = this.skillSlug
                    ? ('Claude iniciou — skill /' + this.skillSlug)
                    : 'Claude iniciou a elaboração';
                showToast(msg + ' — pode navegar livremente', 'info');
                this.startPolling();
            } catch (e) {
                this.status = 'error';
                this.error = 'Falha ao iniciar: ' + e.message;
            }
        },

        startPolling() {
            if (this._pollTimer) return;
            this._pollTimer = setInterval(async () => {
                await this.fetchStatus();
                if (this.status === 'done' || this.status === 'error' || this.status === 'idle') {
                    clearInterval(this._pollTimer);
                    this._pollTimer = null;
                    if (this.status === 'done') {
                        // startPolling() so' roda quando a elaboracao foi
                        // iniciada nesta sessao — transicao running -> done
                        // significa peca recem-gerada. Recarrega pra atualizar
                        // as abas Pecas/Despachos (SSR) com os novos arquivos.
                        showToast('Peça elaborada — recarregando para exibir arquivos gerados', 'success');
                        setTimeout(() => window.location.reload(), 1200);
                    }
                }
            }, 2000);
        },

        verResumo() {
            abrirResumo(this.pajNorm);
        }
    };
}

// Botão "Organizar Documentos para Ajuizamento" — mesmo padrão de
// elaborarApp() acima: init() consulta o status já no CARREGAMENTO da página
// e retoma o spinner/polling se uma organização estiver rodando (ex.: F5 no
// meio da tarefa, ou aberta em outra aba). A tarefa roda em background via
// sessão de agente (não mais um script instantâneo — ver
// services/organizar_ajuizamento_service.py) e sem esse init a página não
// dava NENHUM sinal de que algo estava em andamento: parecia que a pasta
// "Ajuizamento" mudava sozinha enquanto o Defensor mexia nela manualmente ao
// mesmo tempo (relatado no PAJ 2026/029-01658, 2026-08-21) — na verdade era a
// organização em background ainda escrevendo. Ao concluir com sucesso, o
// backend cria um lembrete vermelho "Documentos organizados para
// ajuizamento" no PAJ (ver organizar_ajuizamento_service._persistir) — como
// os lembretes são lidos do disco a cada carregamento da página (ver
// lembretesApp), o reload automático abaixo já mostra esse aviso persistente,
// mesmo que o Defensor tenha saído da página antes de terminar.
function organizarAjuizamentoApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        status: 'idle',
        error: '',
        _pollTimer: null,

        async init() {
            await this.fetchStatus();
            if (this.status === 'running') {
                this.startPolling();
            }
        },

        async fetchStatus() {
            try {
                const resp = await fetch('/api/organizar-ajuizamento/status/' + this.pajNorm);
                const data = await resp.json();
                this.status = data.status || 'idle';
                this.error = data.error || '';
            } catch (e) {
                this.error = 'Erro ao consultar status: ' + e.message;
                this.status = 'error';
            }
        },

        async organizar() {
            let resp, data;
            try {
                resp = await fetch('/api/organizar-ajuizamento/' + this.pajNorm, { method: 'POST' });
                data = await resp.json().catch(() => ({}));
            } catch (e) {
                showToast('Falha de rede: ' + e, 'error');
                return;
            }
            if (!data.ok && data.precisa_confirmar) {
                const ok = confirm('A pasta "Ajuizamento" já existe para este PAJ. Reorganizar e sobrescrever (apaga a versão atual por completo)?');
                if (!ok) return;
                try {
                    resp = await fetch('/api/organizar-ajuizamento/' + this.pajNorm, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ forcar: true }),
                    });
                    data = await resp.json().catch(() => ({}));
                } catch (e) {
                    showToast('Falha de rede: ' + e, 'error');
                    return;
                }
            }
            if (!data.ok) {
                showToast('Erro: ' + (data.erro || 'falha ao organizar'), 'error');
                return;
            }
            this.status = 'running';
            this.error = '';
            showToast('Organizando documentos — pode levar alguns minutos. NÃO edite/renomeie nada em "Ajuizamento" enquanto isso roda (a organização reescreve a pasta).', 'info');
            this.startPolling();
        },

        startPolling() {
            if (this._pollTimer) return;
            this._pollTimer = setInterval(async () => {
                await this.fetchStatus();
                if (this.status === 'done' || this.status === 'error') {
                    clearInterval(this._pollTimer);
                    this._pollTimer = null;
                    if (this.status === 'done') {
                        showToast('Documentos organizados em "Ajuizamento" — recarregando…', 'success');
                        setTimeout(() => window.location.reload(), 1200);
                    } else {
                        showToast('Erro ao organizar: ' + (this.error || 'ver log no servidor'), 'error');
                    }
                }
            }, 3000);
        },
    };
}

/** Dispara o polling do componente elaborarApp presente na pagina (se houver). */
function _triggerElaborarPolling() {
    const el = document.querySelector('[x-data^="elaborarApp"]');
    if (!el) return;
    // Alpine v3 expoe os dados via _x_dataStack
    const data = el._x_dataStack && el._x_dataStack[0];
    if (data && typeof data.startPolling === 'function') {
        data.status = 'running';
        data.startPolling();
    }
}

async function enviarCorrecao(pajNorm) {
    const textarea = document.getElementById('correcao-text');
    const text = (textarea?.value || '').trim();
    if (!text) {
        showToast('Digite a correção primeiro', 'warning');
        return;
    }
    try {
        const resp = await fetch('/api/elaborar/correcao/' + pajNorm, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({text: text}),
        });
        if (!resp.ok) {
            const err = await resp.json();
            showToast('Erro: ' + (err.erro || resp.status), 'error');
            return;
        }
        const data = await resp.json();
        const msg = data.last_action && data.last_action !== 'iniciando...'
            ? 'Correção enviada — ' + data.last_action
            : 'Correção enviada — Claude refazendo a peça';
        showToast(msg, 'info');
        textarea.value = '';
        document.getElementById('resumo-modal')?.close();
        // Inicia o polling para mostrar progresso e recarregar ao concluir
        _triggerElaborarPolling();
    } catch (e) {
        showToast('Erro: ' + e.message, 'error');
    }
}

/* =========================================================
   Limpeza de anexos (apaga binarios apos OCR)
   ========================================================= */
let _limpezaPajAtual = null;
let _limpezaPodeLimpar = false;

function _fmtBytes(n) {
    if (!n && n !== 0) return '—';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / (1024 * 1024)).toFixed(2) + ' MB';
}

function _limpezaResetUI() {
    document.getElementById('limpeza-loading').style.display = 'block';
    document.getElementById('limpeza-conteudo').style.display = 'none';
    document.getElementById('limpeza-bloqueios').style.display = 'none';
    document.getElementById('limpeza-resultado').style.display = 'none';
    document.getElementById('limpeza-erro').style.display = 'none';
    document.getElementById('limpeza-btn-executar').disabled = true;
    const chk = document.getElementById('limpeza-confirmar');
    if (chk) chk.checked = false;
    const force = document.getElementById('limpeza-forcar');
    if (force) force.checked = false;
}

async function abrirLimpezaModal(pajNorm) {
    _limpezaPajAtual = pajNorm;
    let modal = document.getElementById('limpeza-modal');
    if (!modal) return;
    _limpezaResetUI();
    modal.showModal();

    try {
        let resp = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/limpar-anexos/preview');
        let data = await resp.json();
        if (!resp.ok || !data.ok) {
            document.getElementById('limpeza-loading').style.display = 'none';
            document.getElementById('limpeza-conteudo').style.display = 'block';
            document.getElementById('limpeza-erro').style.display = 'flex';
            document.getElementById('limpeza-erro-msg').textContent =
                data.erro || ('HTTP ' + resp.status);
            return;
        }
        _limpezaRenderPreview(data);
    } catch (e) {
        document.getElementById('limpeza-loading').style.display = 'none';
        document.getElementById('limpeza-conteudo').style.display = 'block';
        document.getElementById('limpeza-erro').style.display = 'flex';
        document.getElementById('limpeza-erro-msg').textContent = 'Erro: ' + e.message;
    }
}

function _limpezaRenderPreview(data) {
    document.getElementById('limpeza-loading').style.display = 'none';
    document.getElementById('limpeza-conteudo').style.display = 'block';

    _limpezaPodeLimpar = !!data.pode_limpar;

    const aRemover = data.arquivos_a_remover || [];
    const preservados = data.arquivos_preservados || [];
    document.getElementById('limpeza-n-remover').textContent = aRemover.length;
    document.getElementById('limpeza-bytes-liberar').textContent =
        _fmtBytes(data.bytes_total_disponivel || 0) + ' liberaveis';
    document.getElementById('limpeza-n-preservar').textContent = preservados.length;

    // Lista a remover
    const tbR = document.getElementById('limpeza-lista-remover');
    tbR.innerHTML = '';
    aRemover.forEach(function(a) {
        let tr = document.createElement('tr');
        tr.innerHTML =
            '<td class="font-mono text-xs break-all">' + _htmlEscape(a.nome) + '</td>' +
            '<td class="text-xs">' + ((a.tamanho || 0) / 1024).toFixed(1) + '</td>' +
            '<td>' + (a.tem_ocr ? '<span class="badge badge-success badge-xs">sim</span>' :
                                  '<span class="badge badge-error badge-xs">NÃO</span>') + '</td>';
        tbR.appendChild(tr);
    });

    const tbP = document.getElementById('limpeza-lista-preservar');
    tbP.innerHTML = '';
    preservados.forEach(function(a) {
        let tr = document.createElement('tr');
        tr.innerHTML =
            '<td class="font-mono text-xs break-all">' + _htmlEscape(a.nome) + '</td>' +
            '<td class="text-xs">' + ((a.tamanho || 0) / 1024).toFixed(1) + '</td>';
        tbP.appendChild(tr);
    });

    // Bloqueios
    const motivos = data.motivos_bloqueio || [];
    if (motivos.length > 0) {
        document.getElementById('limpeza-bloqueios').style.display = 'flex';
        const ul = document.getElementById('limpeza-motivos');
        ul.innerHTML = '';
        motivos.forEach(function(m) {
            const li = document.createElement('li');
            li.textContent = m;
            ul.appendChild(li);
        });
    }

    // Se nao tem nada a remover, desabilita o botao
    if (aRemover.length === 0) {
        document.getElementById('limpeza-btn-executar').disabled = true;
        document.getElementById('limpeza-resultado').style.display = 'flex';
        document.getElementById('limpeza-resultado-msg').textContent =
            'Nenhum arquivo binário a remover — já está limpo.';
        return;
    }

    // Habilita botao quando usuario marcar confirmar (e forcar, se houver bloqueio)
    const confirmar = document.getElementById('limpeza-confirmar');
    let forcar = document.getElementById('limpeza-forcar');
    let btn = document.getElementById('limpeza-btn-executar');

    function avaliar() {
        let ok = confirmar.checked;
        if (!_limpezaPodeLimpar) {
            ok = ok && forcar.checked;
        }
        btn.disabled = !ok;
    }
    confirmar.onchange = avaliar;
    if (forcar) forcar.onchange = avaliar;
}

function _htmlEscape(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function executarLimpeza() {
    if (!_limpezaPajAtual) return;
    let forcar = !_limpezaPodeLimpar && document.getElementById('limpeza-forcar').checked;
    let btn = document.getElementById('limpeza-btn-executar');
    btn.disabled = true;
    btn.textContent = 'Apagando...';

    try {
        let url = '/api/paj/' + encodeURIComponent(_limpezaPajAtual) + '/limpar-anexos';
        if (forcar) url += '?forcar=true';
        let resp = await fetch(url, { method: 'POST' });
        let data = await resp.json();
        if (!resp.ok || !data.ok) {
            document.getElementById('limpeza-erro').style.display = 'flex';
            document.getElementById('limpeza-erro-msg').textContent =
                data.erro || ('HTTP ' + resp.status);
            btn.textContent = 'Apagar arquivos';
            return;
        }
        document.getElementById('limpeza-resultado').style.display = 'flex';
        document.getElementById('limpeza-resultado-msg').textContent =
            (data.removidos || 0) + ' arquivo(s) apagado(s) — ' +
            _fmtBytes(data.bytes_liberados || 0) + ' liberados.';
        btn.style.display = 'none';
        showToast('Anexos apagados — recarregando', 'success');
        setTimeout(function() { window.location.reload(); }, 1200);
    } catch (e) {
        document.getElementById('limpeza-erro').style.display = 'flex';
        document.getElementById('limpeza-erro-msg').textContent = 'Erro: ' + e.message;
        btn.textContent = 'Apagar arquivos';
        btn.disabled = false;
    }
}

function fecharLimpezaModal() {
    const m = document.getElementById('limpeza-modal');
    if (m) m.close();
}

/* Exclui UM arquivo de peça/despacho da pasta do PAJ (ícone de lixeira das
 * abas Outros > Peças/Despachos). Diferente de "Limpar anexos" (que apaga em
 * massa e tem safeguards de PAJ ativo): aqui é exclusão pontual de um
 * arquivo especifico, sem safeguard algum além de confirmar com o usuário —
 * remove a linha da tabela e decrementa o badge de contagem da aba na hora,
 * sem recarregar a página. `badgeId` é o id do <span> de contagem da aba
 * (ver templates/paj_detail.html). */
async function excluirArquivoPaj(pajNorm, caminho, nome, btnEl, badgeId) {
    if (!confirm('Excluir "' + nome + '"? Esta ação não pode ser desfeita.')) return;
    try {
        const r = await fetch(
            '/api/paj/' + encodeURIComponent(pajNorm) + '/arquivo?caminho=' + encodeURIComponent(caminho),
            { method: 'DELETE' }
        );
        const data = await r.json();
        if (!r.ok || !data.ok) {
            showToast(data.erro || 'Falha ao excluir arquivo', 'error');
            return;
        }
        const row = btnEl && btnEl.closest('tr');
        if (row) row.remove();
        const badge = badgeId && document.getElementById(badgeId);
        if (badge) badge.textContent = String(Math.max(0, (parseInt(badge.textContent, 10) || 0) - 1));
        showToast('Arquivo excluído', 'success');
    } catch (e) {
        showToast('Erro de rede ao excluir arquivo', 'error');
    }
}

/* =========================================================
   Fallback OCR-LLM (Sonnet) — transcreve anexos de OCR fraco
   ========================================================= */
async function melhorarOcr(pajNorm, btn) {
    const original = btn ? btn.innerHTML : '';
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="loading loading-spinner loading-xs"></span> Transcrevendo…';
    }
    showToast('Melhorando OCR: tenta OCR local (grátis) primeiro e usa Sonnet só no que faltar — pode demorar em autos grandes.', 'info');
    try {
        const r = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/melhorar-ocr', { method: 'POST' });
        const d = await r.json();
        const nt = (d.transcritos || []).length;
        const nl = (d.ocr_local || []).length;
        const nf = (d.falhas || []).length;
        const ng = (d.grandes || []).length;
        const extra = (nf ? ' · ' + nf + ' falha(s)' : '') + (ng ? ' · ' + ng + ' grande(s) pulado(s) — Opus lê direto' : '');
        if (d.cota) {
            showToast('Cota esgotada durante a transcrição — tente novamente após a renovação.', 'warning');
        } else if ((nt + nl) > 0) {
            const partes = [];
            if (nl) partes.push(nl + ' via OCR local (grátis)');
            if (nt) partes.push(nt + ' via Sonnet');
            showToast(partes.join(' + ') + ' melhorado(s)' + extra + ' — recarregando.', 'success');
            setTimeout(function () { window.location.reload(); }, 1200);
        } else if (nf > 0 || ng > 0) {
            showToast('Nada melhorado' + extra + '.', nf ? 'warning' : 'info');
        } else {
            showToast('Nada a melhorar: o OCR já está bom em todos os anexos.', 'info');
        }
    } catch (e) {
        showToast('Erro ao transcrever: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = original; }
    }
}

/* Abre a pasta do PAJ no Explorer — so' funciona na maquina onde o painel roda. */
async function abrirPastaPaj(pajNorm, btn, sub) {
    const original = btn ? btn.innerHTML : '';
    if (btn) { btn.disabled = true; }
    try {
        const r = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/abrir-pasta'
                              + (sub ? '?sub=' + encodeURIComponent(sub) : ''), { method: 'POST' });
        const d = await r.json();
        if (!r.ok || !d.ok) {
            showToast(d.erro || 'Erro ao abrir a pasta.', 'error');
            return;
        }
        showToast('Pasta aberta no Explorer.', 'success');
    } catch (e) {
        showToast('Erro ao abrir a pasta: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = original; }
    }
}

/* Pretensões cujo nome cadastrado no SISDPU vem como "Nome antigo (Nome atual)"
   (ex.: "AUXÍLIO-DOENÇA (BENEFÍCIO POR INCAPACIDADE TEMPORÁRIA)") ocupam espaço
   demais em colunas/badges. Exibe só o parêntese final (o nome vigente pós-Lei
   13.846/2019) — a comparação/filtro em qualquer tela continua usando o valor
   CRU (paj.pretensaopura original); só o RENDER passa por esta função. Mesma
   regra replicada em Python: app.py `_formatar_pretensao` (paj_detail.html usa
   Jinja, não Alpine, pra exibir). */
function formatarPretensao(txt) {
    if (!txt) return txt;
    // {4,} evita mostrar só "A" quando a pretensão termina em marcador de
    // gênero — "...PELO(A) ASSISTIDO(A)" — em vez do nome atual da
    // pretensão (mesmo raciocínio do mirror em Python, app.py _formatar_pretensao).
    const m = txt.match(/\(([^()]{4,})\)\s*$/);
    return m ? m[1].trim() : txt;
}

/* Estrela de watchlist — toggle rápido de incluir/remover, sempre ao lado do
   ícone de copiar o número do PAJ (dashboard, Retornos, página do PAJ). Um
   clique só: sem motivo, sem modal (pra motivo, use a página Watchlist).
   Devolve o novo estado `watched` (ou o antigo, sem mudar, se a chamada
   falhar) — quem chama atualiza sua própria variável reativa com o retorno. */
async function toggleWatchlist(pajNorm, watched) {
    try {
        if (watched) {
            const r = await fetch('/api/watchlist/' + encodeURIComponent(pajNorm), { method: 'DELETE' });
            if (!r.ok && r.status !== 404) {
                showToast('Erro ao remover da watchlist', 'error');
                return watched;
            }
            showToast('Removido da watchlist', 'info');
            return false;
        }
        const r = await fetch('/api/watchlist', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paj_norm: pajNorm, motivo: '' }),
        });
        if (!r.ok) {
            showToast('Erro ao adicionar à watchlist', 'error');
            return watched;
        }
        showToast('Adicionado à watchlist', 'success');
        return true;
    } catch (e) {
        showToast('Falha de rede', 'error');
        return watched;
    }
}

/* Minimenu "Consultar processo" (número do processo clicável) — mesmo padrão da
   tabela de intimações (Prazos e-proc): "Consultar processo (MCP)" abre a página
   de íntegra estruturada (sem navegador); "Abrir no navegador" abre a Consulta
   Processual na janela persistente do e-proc. */
function consultarProcessoMcp(numero) {
    if (!numero) return;
    const qs = new URLSearchParams({ numero: numero, instancia: 1 });
    window.open('/eproc/consultar?' + qs.toString(), '_blank');
}

// Caixa "e-Sisrec" do cabeçalho: abre a página de consulta do painel
// (/esisrec/consultar) numa aba nova — a leitura do recurso administrativo é
// feita sem janela (headless), ver ingestao/esisrec_client.py.
function consultarEsisrec(numero) {
    const digitos = (numero || '').replace(/\D/g, '');
    if (!digitos) { showToast('Informe o protocolo, CPF ou NB do recurso.', 'warning'); return; }
    window.open('/esisrec/consultar?numero=' + encodeURIComponent(digitos), '_blank');
}

async function abrirProcessoNavegador(numero, link) {
    if (!numero) return;
    if (link) { link.style.opacity = '0.5'; link.style.pointerEvents = 'none'; }
    showToast('Abrindo processo na janela do e-proc…', 'info');
    try {
        const r = await fetch('/api/prazos/abrir-processo-numero?numero=' + encodeURIComponent(numero));
        const d = await r.json();
        if (!r.ok || !d.ok) {
            showToast(d.erro || 'Erro ao abrir o processo no e-proc.', 'error');
            return;
        }
        showToast('Processo aberto na janela do e-proc.', 'success');
    } catch (e) {
        showToast('Erro ao abrir o processo: ' + e.message, 'error');
    } finally {
        if (link) { link.style.opacity = ''; link.style.pointerEvents = ''; }
    }
}

/* Minimenu "Abrir PAJ" (número do PAJ clicável) — mesmo padrão do minimenu de
   processo: "Abrir no Painel" navega pra página do PAJ neste painel (mesma
   aba); "Abrir no navegador" abre o detalhamento do PAJ no SISDPU (aba nova,
   via /api/prazos/abrir-paj — resolve o link stateless e redireciona).

   O número do PAJ aqui costuma vir de texto livre (ex.: lembrete/post-it do
   e-proc) — não tem nenhuma garantia de que esse PAJ já foi sincronizado no
   painel (PAJs antigos/concluídos, ou que não voltaram pra caixa de entrada
   recentemente, nunca chegam a ter pasta local). Por isso confere a
   existência ANTES de abrir: se não achar localmente (404), dispara uma
   sincronização avulsa LEVE (só movimentações/metadata — mesma rota usada
   pela sincronização em lote dos "prazos em aberto") via
   /api/sync/paj/{norm}/movimentacoes, entrando pela pesquisa global do
   SISDPU — NÃO depende do PAJ estar na caixa de entrada. Mostra o MESMO
   modal de log das outras sincronizações (não só um toast) — sem isso o
   usuário não tem como saber que algo está rodando durante os ~1-3 minutos
   que a sincronização leva, e pode clicar de novo por engano: um 2º clique
   enquanto o 1º ainda está rodando colide no lock global do backend, falha
   na hora, e cai direto no fallback de SISDPU (parecendo um bug, mas é só
   a falta de feedback visual). Só cai pro fallback de "Abrir no navegador"
   (SISDPU ao vivo) se a sincronização falhar de verdade (ex.: número errado
   no lembrete, ou o PAJ realmente não existe no SISDPU). */
async function abrirPajPainel(pajNorm) {
    if (!pajNorm) return;
    try {
        // GET, não HEAD: essa rota só responde a GET (HEAD dá 405, não 404 —
        // com HEAD a checagem abaixo nunca disparava e sempre caía na tela
        // morta de "PAJ não encontrado", já que 405 !== 404).
        const r = await fetch('/api/paj/' + encodeURIComponent(pajNorm));
        if (r.status === 404) {
            const ok = await _sincronizarPajAvulsoComModal(pajNorm);
            if (!ok) {
                showToast('Não consegui sincronizar esse PAJ — abrindo direto no SISDPU...', 'error');
                abrirPajNavegador(formatarPajNorm(pajNorm));
                return;
            }
        }
    } catch (e) { /* falha na checagem: cai no comportamento antigo abaixo */ }
    window.open('/paj/' + pajNorm, '_blank');
}

/* Sincronização avulsa e LEVE de um único PAJ, usada como passo intermediário
   de abrirPajPainel() acima quando o PAJ ainda não existe localmente. Reusa
   o widget flutuante compartilhado (sempre presente, ver base.html) —
   minimizado, já que o usuário está esperando ativamente (o toast/resolve
   já avisa quando terminar). Resolve `true`/`false`. */
function _sincronizarPajAvulsoComModal(pajNorm) {
    const url = '/api/sync/paj/' + encodeURIComponent(pajNorm) + '/movimentacoes';
    return new Promise((resolve) => {
        let resolvido = false;
        let pollTimer = null;
        const finalizar = (ok) => {
            if (resolvido) return;
            resolvido = true;
            if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
            if (!ok) showToast('Não foi possível sincronizar este PAJ.', 'error');
            resolve(ok);
        };
        SyncWidget.iniciar(url, {
            titulo: 'Sincronizar PAJ ' + formatarPajNorm(pajNorm) + ' (avulso)',
            minimizado: true,
            onDone: async function () {
                let ok = false;
                try {
                    const r = await fetch('/api/paj/' + encodeURIComponent(pajNorm));
                    ok = r.status !== 404;
                } catch (e) { /* ok fica false */ }
                finalizar(ok);
            },
        }).then(function (iniciou) {
            if (!iniciou) { finalizar(false); return; }
            // Mesma salvaguarda de pajCarregandoApp() — se a conexão SSE do
            // widget cair no meio, `onDone` nunca dispara e este Promise ficava
            // pendurado pra sempre (o "Abrir PAJ" parecia não fazer nada).
            // 3s × 20 = 1 minuto — tempo mais que suficiente pra sincronização
            // LEVÍSSIMA usada aqui (pedido do usuário 2026-09-18).
            let tentativas = 0;
            pollTimer = setInterval(async () => {
                tentativas++;
                if (tentativas > 20) { finalizar(false); return; }
                try {
                    const r = await fetch('/api/paj/' + encodeURIComponent(pajNorm));
                    if (r.status !== 404) finalizar(true);
                } catch (e) { /* tenta de novo no proximo tick */ }
            }, 3000);
        });
    });
}
function abrirPajNavegador(pajDisplay) {
    if (!pajDisplay) return;
    window.open('/api/prazos/abrir-paj?paj=' + encodeURIComponent(pajDisplay), '_blank');
}

/* 3ª opção do MESMO minimenu "Abrir PAJ" (ver acima) — "Adicionar movimentação":
   leva pra "Movimentações Pendentes" já com o número do PAJ pré-preenchido
   (?novo_paj=...), abrindo direto o modal de criação (ver movimentacoesPendentesApp()
   em templates/movimentacoes_pendentes.html). Mesma aba (não é _blank) — o usuário
   volta com o botão do navegador quando terminar, como qualquer outra navegação
   normal do painel. NÃO precisa que o PAJ já esteja sincronizado — é exatamente
   o caso de uso (registrar a movimentação agora, executar quando o SISDPU voltar). */
function adicionarMovimentacaoPendente(pajDisplay) {
    if (!pajDisplay) return;
    window.location.href = '/movimentacoes-pendentes?novo_paj=' + encodeURIComponent(pajDisplay);
}

/* Item "Movimentar no eproc" do minimenu do NÚMERO DO PROCESSO (não do PAJ —
   ver partials/_macro_minimenu_processo.html) — mesmo espírito de
   adicionarMovimentacaoPendente() acima, mas pra "Petições pendentes": leva
   pra lá já com o número do processo pré-preenchido (?novo_processo=...),
   abrindo direto o modal "Adicionar petição" (ver peticoesPendentesApp() em
   templates/peticoes_pendentes.html). NÃO precisa de PAJ nenhum — peticionar/
   lançar evento no e-proc só exige o número do processo (pedido do usuário
   2026-09-11, mesma lógica de "Adicionar movimentação" acima).

   `pajNorm`/`pajLabel` (opcionais) — quando a página de origem JÁ SABE o PAJ
   correlacionado a este processo (dashboard/intimações/retornos/prazos/
   eproc_consultar/paj_detail sempre sabem, cada uma à sua maneira), repassa
   pra pré-preencher — sem isso, a petição nascia "sem PAJ" mesmo quando havia
   um vínculo óbvio na tela de origem, e ficava sem trazer essa informação pra
   uma eventual movimentação no SISDPU (bug real 2026-09-11, PAJ 2026/029-00897,
   processo 5014914-73.2026.4.04.7000). */
function movimentarNoEproc(numeroProcesso, pajNorm, pajLabel) {
    if (!numeroProcesso) return;
    const qs = new URLSearchParams({ novo_processo: numeroProcesso });
    if (pajNorm) qs.set('paj_norm', pajNorm);
    if (pajLabel) qs.set('paj_label', pajLabel);
    window.location.href = '/peticoes-pendentes?' + qs.toString();
}

/* Item "Movimentar Eproc + Sisdpu" — presente nos DOIS minimenus (do PAJ e do
   número do processo — ver _macro_minimenu_paj.html/_macro_minimenu_processo.html).
   Leva pra "Movimentar Eproc + SISDPU" (peticionar/lançar evento no e-proc E
   registrar a movimentação no SISDPU numa única execução — ver
   templates/movimentar_conjunto.html) já com o(s) identificador(es) que a
   página de origem conhecer. Quando só UM vier informado (caso comum — cada
   minimenu só conhece de fato o seu próprio, o outro é um bônus quando a linha
   de origem já carrega os dois juntos), a página resolve o outro sozinha,
   cruzando intimações/prazos/cadastro do PAJ (ver
   services.paj_processo_link_service) — pedido do usuário 2026-09-11.
   `paj` aceita tanto "AAAA/UUU-NNNNN" quanto "PAJ-AAAA-UUU-NNNNN". */
function movimentarConjunto(numeroProcesso, paj) {
    if (!numeroProcesso && !paj) return;
    const qs = new URLSearchParams();
    if (numeroProcesso) qs.set('processo', numeroProcesso);
    if (paj) qs.set('paj', paj);
    window.location.href = '/movimentar-conjunto?' + qs.toString();
}

/* Caixa de pesquisa rápida de PAJ no rodapé do menu lateral (ver
   templates/base.html, acima de Histórico/Pipeline/Configurações) — mesmo
   espírito da busca nativa do SISDPU. Abre o detalhe numa aba NOVA
   IMEDIATAMENTE (window.open tem que ficar colado no gesto de clique/Enter,
   sem nenhum await antes, senão o navegador bloqueia como pop-up) — se o
   PAJ ainda não estiver sincronizado localmente, a própria aba nova mostra
   a tela de carregamento (ver pajCarregandoApp() abaixo e
   templates/paj_carregando.html) que sincroniza sozinha e recarrega. */
function pesquisarPajMenu(pajDisplay) {
    const norm = pajExibicaoParaNorm(pajDisplay);
    if (!norm) {
        showToast('Número de PAJ incompleto — use o formato AAAA/UUU-NNNNN.', 'warning');
        return;
    }
    window.open('/paj/' + norm, '_blank');
}

/* x-data da tela de carregamento (templates/paj_carregando.html) — servida
   pela própria rota /paj/{paj_norm} quando o PAJ pedido (ex.: pela caixa de
   pesquisa acima, ou por um link/favorito antigo) ainda não foi sincronizado
   localmente. Dispara a MESMA sincronização avulsa LEVÍSSIMA usada pelo
   minimenu "Abrir PAJ" (ver abrirPajPainel()/_sincronizarPajAvulsoComModal()
   — sem extração de documentos por evento, sem download de anexos, ver
   services/sync_service.rodar_sync_movimentacoes) e recarrega esta mesma
   página quando terminar — a rota então já encontra o PAJ e devolve a
   página de detalhe de verdade, sem exigir nenhuma ação do usuário. */
function pajCarregandoApp(pajNorm) {
    return {
        pajNorm: pajNorm,
        erro: '',
        _pollTimer: null,
        _pararPoll() {
            if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
        },
        iniciar() {
            this.erro = '';
            this._pararPoll();
            const url = '/api/sync/paj/' + encodeURIComponent(this.pajNorm) + '/movimentacoes';
            SyncWidget.iniciar(url, {
                titulo: 'Sincronizando PAJ ' + formatarPajNorm(this.pajNorm),
                minimizado: true,
                onDone: (status) => {
                    this._pararPoll();
                    if (status && status.ok === false) {
                        this.erro = 'Não foi possível sincronizar este PAJ — confira o número ou tente abrir direto no SISDPU.';
                        return;
                    }
                    fetch('/api/paj/' + encodeURIComponent(this.pajNorm))
                        .then((r) => {
                            if (r.status === 404) throw new Error('not_found');
                            window.location.reload();
                        })
                        .catch(() => {
                            this.erro = 'PAJ não encontrado no SISDPU — confira o número.';
                        });
                },
            }).then((iniciou) => {
                if (!iniciou) {
                    this.erro = 'Não foi possível iniciar a sincronização (pode já haver outra rodando — aguarde um pouco e tente de novo).';
                    return;
                }
                // Salvaguarda INDEPENDENTE do SSE do widget: se a conexão cair no
                // meio (reinício do servidor, instabilidade de rede — mesma classe
                // de bug já corrigida em sincronizarCaixa()/prazos-abertos e em
                // desarquivarPaj()), o `onDone` acima nunca dispara e esta página
                // ficava presa em "sincronizando" pra sempre, só resolvendo com F5
                // manual (relatado pelo usuário 2026-09-18). Este poll roda em
                // paralelo e recarrega assim que o PAJ aparecer, mesmo sem o
                // widget ter avisado — até 1 minuto (20×3s, tempo mais que
                // suficiente pra sincronização LEVÍSSIMA usada aqui — pedido do
                // usuário 2026-09-18), depois desiste e mostra erro em vez de
                // ficar pendurado pra sempre.
                let tentativas = 0;
                this._pollTimer = setInterval(() => {
                    tentativas++;
                    if (tentativas > 20) {
                        this._pararPoll();
                        this.erro = 'A sincronização está demorando demais — confira o número ou tente abrir direto no SISDPU.';
                        return;
                    }
                    fetch('/api/paj/' + encodeURIComponent(this.pajNorm))
                        .then((r) => {
                            if (r.status !== 404) {
                                this._pararPoll();
                                window.location.reload();
                            }
                        })
                        .catch(() => {});
                }, 3000);
            });
        },
        abrirNoSisdpu() {
            abrirPajNavegador(formatarPajNorm(this.pajNorm));
        },
    };
}

/* Item "Gerenciar processo" do minimenu do número do processo — abre o modal
   GLOBAL de gerenciamento avulso (ver templates/base.html:gerenciarProcessoApp
   — montado uma vez, sempre presente em toda página). Ação isolada de
   propósito (2026-09-11): gerenciamento no e-proc já é um único POST
   (tipo + procurador), sem rascunho/preparar-depois-confirmar como peticionar
   — não precisa da tela cheia de "Ações no Eproc" só pra isso. Motivado por um
   gerenciamento configurado numa "Movimentar Eproc + SISDPU" ter falhado (bug
   corrigido à parte) e não ter, até então, nenhuma forma de rodar de novo
   isolado pro mesmo processo já peticionado. */
function abrirGerenciarProcesso(numeroProcesso) {
    if (!numeroProcesso) return;
    window.dispatchEvent(new CustomEvent('abrir-gerenciar-processo', { detail: { processo: numeroProcesso } }));
}

function gerenciarProcessoApp() {
    return {
        aberto: false, processo: '', executando: false, erro: '',
        tipo: 'S', procuradorValor: '', procuradorRotulo: '',
        // Resultado da execução, mostrado DENTRO do modal (sem alert() — pedido do
        // usuário 2026-09-16): { tipo: 'sucesso'|'erro'|'indefinido', mensagem,
        // detalhes?: string[] }. Enquanto null, o formulário fica visível; uma vez
        // preenchido, o formulário some e só resta o banner + botão "Fechar".
        resultado: null,
        // Mesma lista estática de sempre (ver acoes_eproc.js:tiposGerenciamento —
        // duplicada aqui de propósito: este modal é intencionalmente
        // independente do módulo grande "Ações no Eproc", sem compartilhar
        // estado com ele).
        tiposGerenciamento: [
            { valor: 'S', rotulo: 'Substituir procurador' },
            { valor: 'C', rotulo: 'Associar procurador' },
            { valor: 'D', rotulo: 'Desassociar procurador' },
            { valor: 'T', rotulo: 'Substituição Transitória' },
        ],
        procuradoresFav: [], procuradoresTodos: [],
        async init() {
            try {
                const j = await (await fetch('/api/acoes-eproc/preferencias')).json();
                this.procuradoresFav = j.procuradores_favoritos || [];
                this.procuradoresTodos = j.procuradores_todos || [];
                if (j.procurador_padrao) {
                    this.procuradorValor = j.procurador_padrao.valor || '';
                    this.procuradorRotulo = j.procurador_padrao.rotulo || '';
                }
            } catch (e) {}
        },
        procuradoresDropdown() { return this.procuradoresFav.length ? this.procuradoresFav : this.procuradoresTodos; },
        onTrocarProcurador() {
            const p = this.procuradoresDropdown().find((p) => p.valor === this.procuradorValor);
            this.procuradorRotulo = p ? p.rotulo : '';
        },
        abrir(processo) {
            if (!processo) return;
            this.processo = processo;
            this.erro = ''; this.executando = false; this.resultado = null;
            this.aberto = true;
        },
        // Sem confirm() nativo aqui de propósito (2026-09-16, pedido do usuário):
        // o próprio modal JÁ é a confirmação — mostra processo/tipo/procurador,
        // o aviso fixo "Ação IRREVERSÍVEL" e um botão "Cancelar" ao lado. Um
        // segundo diálogo de confirmação depois de já clicar "Executar
        // gerenciamento" era redundante.
        async executar() {
            this.executando = true; this.erro = ''; this.resultado = null;
            try {
                const resp = await fetch('/api/acoes-eproc/gerenciar', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        processo: this.processo, tipo: this.tipo,
                        procurador_valor: this.tipo === 'D' ? '' : this.procuradorValor,
                    }),
                });
                const j = await resp.json();
                if (!j.ok) { this.erro = j.erro || 'Falha ao gerenciar o processo.'; return; }
                // Mesma checagem rigorosa de sempre — provavel_sucesso só é True se
                // TODAS as partes marcadas mostrarem a mensagem exata de sucesso
                // (ver eproc_client.gerenciar_processo); erro parcial nunca fica
                // mascarado atrás de um sucesso geral. Resultado fica no próprio
                // modal (sem alert() — pedido do usuário 2026-09-16), com "Fechar".
                const comErro = j.partes_com_erro || [];
                if (j.provavel_sucesso) {
                    this.resultado = {
                        tipo: 'sucesso',
                        mensagem: 'Gerenciamento executado com sucesso no e-proc para ' + (j.partes_marcadas || []).length + ' parte(s).',
                    };
                } else if (comErro.length) {
                    this.resultado = {
                        tipo: 'erro',
                        mensagem: 'ATENÇÃO — nem todas as partes foram gerenciadas com sucesso. Confira e tome providências:',
                        detalhes: comErro.map((b) => b.parte_label + ': ' + b.status),
                    };
                } else {
                    this.resultado = {
                        tipo: 'indefinido',
                        mensagem: 'O gerenciamento foi executado, mas o resultado não pôde ser confirmado automaticamente — confira a aba do e-proc.',
                    };
                }
            } catch (e) {
                this.erro = 'Erro de rede: ' + e;
            } finally {
                this.executando = false;
            }
        },
        // Só recarrega a página quando o gerenciamento foi um SUCESSO (pedido do
        // usuário 2026-09-22, testado via minimenu na página de Consultar Processo
        // — sem isso, os dados na tela ficavam desatualizados até um F5 manual).
        // Com erro/indefinido, NÃO recarrega — o usuário já viu a mensagem e pode
        // querer copiar/analisar o texto do erro antes de perder o estado da tela.
        fechar() {
            const foiSucesso = this.resultado && this.resultado.tipo === 'sucesso';
            this.aberto = false;
            this.resultado = null;
            if (foiSucesso) window.location.reload();
        },
    };
}

/* =========================================================
   Busca global (Ctrl+K / Cmd+K)
   ========================================================= */
let _buscaDebounce = null;
let _buscaResultados = [];
let _buscaIndiceSelecionado = -1;

function _buscaEscape(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _renderBuscaResultados() {
    let cont = document.getElementById('busca-results');
    if (!cont) return;
    if (_buscaResultados.length === 0) {
        cont.innerHTML = '<div class="p-4 text-sm text-base-content/50 text-center">Nenhum resultado.</div>';
        return;
    }
    let html = '';
    for (let i = 0; i < _buscaResultados.length; i++) {
        let r = _buscaResultados[i];
        let badge = '';
        if (r.fonte === 'metadata') badge = '<span class="badge badge-xs badge-primary">meta</span>';
        else if (r.fonte === 'sisdpu') badge = '<span class="badge badge-xs badge-info">sisdpu</span>';
        else if (r.fonte === 'ocr') badge = '<span class="badge badge-xs badge-warning">OCR '+_buscaEscape(r.arquivo)+'</span>';
        const selClass = (i === _buscaIndiceSelecionado) ? 'bg-base-300' : '';
        html += '<div class="p-3 border-b border-base-300 cursor-pointer hover:bg-base-300/50 '+selClass+'" ' +
                'data-idx="'+i+'" onclick="_buscaAbrir('+i+')">' +
                '<div class="flex items-center gap-2 mb-1">' +
                '<span class="font-mono text-xs font-semibold">'+_buscaEscape(r.paj_norm)+'</span>' +
                badge +
                '<span class="text-xs text-base-content/60 truncate">'+_buscaEscape(r.assistido || '')+'</span>' +
                (r.etiqueta ? '<span class="badge badge-xs badge-outline badge-accent ml-auto">'+_buscaEscape(r.etiqueta)+'</span>' : '') +
                '</div>' +
                '<div class="text-xs text-base-content/70 leading-snug">'+ (r.trecho || '') +'</div>' +
                '</div>';
    }
    cont.innerHTML = html;
    // Scroll pra o selecionado
    if (_buscaIndiceSelecionado >= 0) {
        let el = cont.querySelector('[data-idx="'+_buscaIndiceSelecionado+'"]');
        if (el) el.scrollIntoView({block: 'nearest'});
    }
}

function _buscaAbrir(i) {
    if (i < 0 || i >= _buscaResultados.length) return;
    let r = _buscaResultados[i];
    let url = '/paj/' + encodeURIComponent(r.paj_norm);
    window.location.href = url;
}

async function _buscaFetch(q) {
    let status = document.getElementById('busca-status');
    let cont = document.getElementById('busca-count');
    if (q.length < 2) {
        _buscaResultados = [];
        _buscaIndiceSelecionado = -1;
        _renderBuscaResultados();
        if (status) status.textContent = 'digite pelo menos 2 caracteres';
        if (cont) cont.textContent = '';
        return;
    }
    if (status) status.textContent = 'buscando...';
    try {
        let resp = await fetch('/api/busca?q=' + encodeURIComponent(q));
        let data = await resp.json();
        _buscaResultados = data.resultados || [];
        _buscaIndiceSelecionado = _buscaResultados.length > 0 ? 0 : -1;
        _renderBuscaResultados();
        if (status) status.textContent = _buscaResultados.length
            ? 'Enter abre o PAJ selecionado' : 'nenhum resultado';
        if (cont) cont.textContent = _buscaResultados.length + ' achados';
    } catch (e) {
        if (status) status.textContent = 'erro: ' + e.message;
    }
}

function abrirBuscaModal() {
    let modal = document.getElementById('busca-modal');
    let input = document.getElementById('busca-input');
    if (!modal || !input) return;
    input.value = '';
    _buscaResultados = [];
    _buscaIndiceSelecionado = -1;
    _renderBuscaResultados();
    document.getElementById('busca-status').textContent = 'digite pelo menos 2 caracteres';
    document.getElementById('busca-count').textContent = '';
    modal.showModal();
    setTimeout(function(){ input.focus(); }, 50);
}

document.addEventListener('keydown', function(e) {
    // Ctrl+K ou Cmd+K abre busca
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        abrirBuscaModal();
        return;
    }
    // Se o modal de busca esta aberto, trata navegacao
    let modal = document.getElementById('busca-modal');
    if (!modal || !modal.open) return;
    if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (_buscaResultados.length) {
            _buscaIndiceSelecionado = Math.min(_buscaResultados.length - 1, _buscaIndiceSelecionado + 1);
            _renderBuscaResultados();
        }
    } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (_buscaResultados.length) {
            _buscaIndiceSelecionado = Math.max(0, _buscaIndiceSelecionado - 1);
            _renderBuscaResultados();
        }
    } else if (e.key === 'Enter') {
        if (_buscaIndiceSelecionado >= 0) {
            e.preventDefault();
            _buscaAbrir(_buscaIndiceSelecionado);
        }
    }
});

// Debounce no input
document.addEventListener('input', function(e) {
    if (e.target && e.target.id === 'busca-input') {
        if (_buscaDebounce) clearTimeout(_buscaDebounce);
        let q = e.target.value;
        _buscaDebounce = setTimeout(function() { _buscaFetch(q); }, 250);
    }
});
