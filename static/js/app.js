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

/* Sincronizacao da caixa SISDPU — streaming SSE */
let _syncSource = null;

function _syncBtnCancel(show) {
    let btn = document.getElementById('sync-cancel-btn');
    if (btn) btn.style.display = show ? '' : 'none';
}

function cancelarSync() {
    let statusEl = document.getElementById('sync-status');
    let btn = document.getElementById('sync-cancel-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'cancelando...'; }
    if (statusEl) {
        statusEl.textContent = 'cancelando...';
        statusEl.className = 'badge badge-sm badge-warning';
    }
    fetch('/api/sync/cancel', {method: 'POST'})
        .then(function(r) { return r.json(); })
        .then(function(j) {
            if (!j.ok) {
                showToast('Sem sync rodando pra cancelar', 'info');
                if (btn) { btn.disabled = false; btn.textContent = 'Cancelar'; }
            } else {
                showToast('Cancelamento solicitado — aguardando o PAJ atual terminar', 'warning');
            }
        })
        .catch(function() {
            showToast('Falha ao pedir cancelamento', 'error');
            if (btn) { btn.disabled = false; btn.textContent = 'Cancelar'; }
        });
}

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

function sincronizarCaixa(baixarAnexos) {
    if (typeof baixarAnexos === 'undefined') baixarAnexos = true;
    let modal = document.getElementById('sync-modal');
    let logEl = document.getElementById('sync-log');
    let statusEl = document.getElementById('sync-status');
    let closeBtn = document.getElementById('sync-close-btn');
    if (!modal || !logEl) return;

    logEl.textContent = '';
    statusEl.textContent = baixarAnexos ? 'sincronizando...' : 'sincronizando (rápido)...';
    statusEl.className = 'badge badge-sm badge-warning';
    closeBtn.disabled = true;
    const cancelBtn = document.getElementById('sync-cancel-btn');
    if (cancelBtn) { cancelBtn.disabled = false; cancelBtn.textContent = 'Cancelar'; }
    _syncBtnCancel(true);
    modal.showModal();

    if (_syncSource) {
        _syncSource.close();
        _syncSource = null;
    }

    let url = baixarAnexos ? '/api/sync' : '/api/sync?anexos=0';
    url = _syncUrlComOcr(url, !baixarAnexos);
    _syncSource = new EventSource(url);

    _syncSource.addEventListener('log', function(e) {
        logEl.textContent += e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
    });

    _syncSource.addEventListener('done', function(e) {
        logEl.textContent += '\n' + e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
        statusEl.textContent = 'concluído';
        statusEl.className = 'badge badge-sm badge-success';
        closeBtn.disabled = false;
        _syncBtnCancel(false);
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
        showToast('Caixa sincronizada — recarregando dashboard', 'success');
        setTimeout(function() {
            if (!modal.open) {
                window.location.reload();
            } else {
                modal.addEventListener('close', function once() {
                    modal.removeEventListener('close', once);
                    window.location.reload();
                });
            }
        }, 500);
    });

    _syncSource.onerror = function() {
        statusEl.textContent = 'erro/desconectado';
        statusEl.className = 'badge badge-sm badge-error';
        closeBtn.disabled = false;
        _syncBtnCancel(false);
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
    };
}

/* Atualizar caixa — reconciliacao rapida (1 pagina, sem abrir cada PAJ):
 * remove do dashboard os PAJs ja sincronizados que saíram da caixa de
 * entrada. NAO adiciona PAJs novos — isso continua sendo papel do
 * "Sincronizar caixa" completo. Sem modal/SSE — endpoint responde direto. */
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
            if (d.removidos > 0) {
                showToast(d.removidos + ' PAJ(s) saíram da caixa e foram removidos do dashboard', 'success');
                window.location.reload();
            } else {
                showToast('Nenhum PAJ saiu da caixa — tudo em dia', 'info');
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
    let modal = document.getElementById('sync-modal');
    let logEl = document.getElementById('sync-log');
    let statusEl = document.getElementById('sync-status');
    let closeBtn = document.getElementById('sync-close-btn');
    let titleEl = document.getElementById('sync-title');
    if (!modal || !logEl) {
        showToast('Modal de sync não encontrado na página', 'error');
        return;
    }

    if (titleEl) titleEl.textContent = 'Sincronizar watchlist';
    logEl.textContent = '';
    statusEl.textContent = baixarAnexos ? 'sincronizando watchlist...' : 'sincronizando watchlist (rápido)...';
    statusEl.className = 'badge badge-sm badge-warning';
    closeBtn.disabled = true;
    const cancelBtn = document.getElementById('sync-cancel-btn');
    if (cancelBtn) { cancelBtn.disabled = false; cancelBtn.textContent = 'Cancelar'; }
    _syncBtnCancel(true);
    modal.showModal();

    if (_syncSource) {
        _syncSource.close();
        _syncSource = null;
    }

    let url = '/api/sync/watchlist';
    if (!baixarAnexos) url += '?anexos=0';
    url = _syncUrlComOcr(url, !baixarAnexos);
    _syncSource = new EventSource(url);

    _syncSource.addEventListener('log', function(e) {
        logEl.textContent += e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
    });

    _syncSource.addEventListener('done', function(e) {
        logEl.textContent += '\n' + e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
        statusEl.textContent = 'concluído';
        statusEl.className = 'badge badge-sm badge-success';
        closeBtn.disabled = false;
        _syncBtnCancel(false);
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
        showToast('Watchlist sincronizada — recarregando', 'success');
        setTimeout(function() {
            if (!modal.open) {
                window.location.reload();
            } else {
                modal.addEventListener('close', function once() {
                    modal.removeEventListener('close', once);
                    window.location.reload();
                });
            }
        }, 500);
    });

    _syncSource.onerror = function() {
        statusEl.textContent = 'erro/desconectado';
        statusEl.className = 'badge badge-sm badge-error';
        closeBtn.disabled = false;
        _syncBtnCancel(false);
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
    };
}

/* Sincronizacao de UM unico PAJ — mesmo modal, endpoint /api/sync/paj/{paj} */
function sincronizarPaj(pajNorm, baixarAnexos) {
    if (typeof baixarAnexos === 'undefined') baixarAnexos = true;
    let modal = document.getElementById('sync-modal');
    let logEl = document.getElementById('sync-log');
    let statusEl = document.getElementById('sync-status');
    let closeBtn = document.getElementById('sync-close-btn');
    if (!modal || !logEl) {
        showToast('Modal de sync não encontrado na página', 'error');
        return;
    }

    logEl.textContent = '';
    statusEl.textContent = (baixarAnexos ? 'sincronizando ' : 'sincronizando (rápido) ') + pajNorm + '...';
    statusEl.className = 'badge badge-sm badge-warning';
    closeBtn.disabled = true;
    const cancelBtn2 = document.getElementById('sync-cancel-btn');
    if (cancelBtn2) { cancelBtn2.disabled = false; cancelBtn2.textContent = 'Cancelar'; }
    _syncBtnCancel(true);
    modal.showModal();

    if (_syncSource) {
        _syncSource.close();
        _syncSource = null;
    }

    let url = '/api/sync/paj/' + encodeURIComponent(pajNorm);
    if (!baixarAnexos) url += '?anexos=0';
    url = _syncUrlComOcr(url, !baixarAnexos);
    _syncSource = new EventSource(url);

    _syncSource.addEventListener('log', function(e) {
        logEl.textContent += e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
    });

    _syncSource.addEventListener('done', function(e) {
        logEl.textContent += '\n' + e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
        statusEl.textContent = 'concluído';
        statusEl.className = 'badge badge-sm badge-success';
        closeBtn.disabled = false;
        _syncBtnCancel(false);
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
        showToast('PAJ sincronizado — recarregando página', 'success');
        setTimeout(function() {
            if (!modal.open) {
                window.location.reload();
            } else {
                modal.addEventListener('close', function once() {
                    modal.removeEventListener('close', once);
                    window.location.reload();
                });
            }
        }, 500);
    });

    _syncSource.onerror = function() {
        statusEl.textContent = 'erro/desconectado';
        statusEl.className = 'badge badge-sm badge-error';
        closeBtn.disabled = false;
        _syncBtnCancel(false);
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
    };
}

/* Download seletivo de documentos (sincronizacao parcial por seq) — usado
 * pelos botoes "Baixar todos"/"Baixar selecionados" da coluna Documentos
 * (static/js/movimentacoes.js). Diferente das demais syncs: NAO recarrega a
 * pagina ao concluir — busca só os anexos atualizados e devolve pro
 * componente Alpine atualizar os badges no lugar. Retorna a lista
 * `anexos_sisdpu` atualizada, ou null se deu erro/cancelou. */
function baixarDocumentosPaj(pajNorm, ids) {
    return new Promise(function(resolve) {
        let modal = document.getElementById('sync-modal');
        let logEl = document.getElementById('sync-log');
        let statusEl = document.getElementById('sync-status');
        let closeBtn = document.getElementById('sync-close-btn');
        let titleEl = document.getElementById('sync-title');
        if (!modal || !logEl) {
            showToast('Modal de sync não encontrado na página', 'error');
            resolve(null);
            return;
        }

        if (titleEl) titleEl.textContent = 'Baixar documento(s) selecionado(s)';
        logEl.textContent = '';
        statusEl.textContent = 'baixando ' + ids.length + ' documento(s)...';
        statusEl.className = 'badge badge-sm badge-warning';
        closeBtn.disabled = true;
        const cancelBtn = document.getElementById('sync-cancel-btn');
        if (cancelBtn) { cancelBtn.disabled = false; cancelBtn.textContent = 'Cancelar'; }
        _syncBtnCancel(true);
        modal.showModal();

        if (_syncSource) {
            _syncSource.close();
            _syncSource = null;
        }

        let url = '/api/sync/paj/' + encodeURIComponent(pajNorm)
                + '/baixar-documentos?ids=' + encodeURIComponent(ids.join(','));
        _syncSource = new EventSource(url);

        function atualizarAnexosEResolver() {
            fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/movimentacoes')
                .then(function(r) { return r.json(); })
                .then(function(data) { resolve(data.anexos_sisdpu || []); })
                .catch(function() { resolve(null); });
        }

        _syncSource.addEventListener('log', function(e) {
            logEl.textContent += e.data + '\n';
            logEl.scrollTop = logEl.scrollHeight;
        });

        _syncSource.addEventListener('done', function(e) {
            logEl.textContent += '\n' + e.data + '\n';
            logEl.scrollTop = logEl.scrollHeight;
            statusEl.textContent = 'concluído';
            statusEl.className = 'badge badge-sm badge-success';
            closeBtn.disabled = false;
            _syncBtnCancel(false);
            if (_syncSource) { _syncSource.close(); _syncSource = null; }
            showToast('Documento(s) baixado(s)', 'success');
            atualizarAnexosEResolver();
        });

        _syncSource.onerror = function() {
            statusEl.textContent = 'erro/desconectado';
            statusEl.className = 'badge badge-sm badge-error';
            closeBtn.disabled = false;
            _syncBtnCancel(false);
            if (_syncSource) { _syncSource.close(); _syncSource = null; }
            resolve(null);
        };
    });
}

/* Sincronizacao EXCLUSIVA e INCREMENTAL das movimentacoes (icone de atualizar
 * da tabela compartilhada, static/js/movimentacoes.js) — relê a lista de
 * movimentacoes no SISDPU, mas so' abre popup de anexo pras movimentacoes
 * REALMENTE novas (as ja conhecidas mantem os documentos ja indexados); nao
 * baixa PDF, nao recarrega a pagina. Mesmo padrao de baixarDocumentosPaj:
 * reusa o #sync-modal e devolve os dados frescos pro componente Alpine
 * trocar so' o conteudo da tabela. Resolve {movimentacoes, anexos_sisdpu,
 * max_seq} ou null se deu erro/cancelou. */
function atualizarMovimentacoesPaj(pajNorm) {
    return new Promise(function(resolve) {
        let modal = document.getElementById('sync-modal');
        let logEl = document.getElementById('sync-log');
        let statusEl = document.getElementById('sync-status');
        let closeBtn = document.getElementById('sync-close-btn');
        let titleEl = document.getElementById('sync-title');
        if (!modal || !logEl) {
            showToast('Modal de sync não encontrado na página', 'error');
            resolve(null);
            return;
        }

        if (titleEl) titleEl.textContent = 'Atualizar movimentações';
        logEl.textContent = '';
        statusEl.textContent = 'atualizando movimentações de ' + pajNorm + '...';
        statusEl.className = 'badge badge-sm badge-warning';
        closeBtn.disabled = true;
        const cancelBtn = document.getElementById('sync-cancel-btn');
        if (cancelBtn) { cancelBtn.disabled = false; cancelBtn.textContent = 'Cancelar'; }
        _syncBtnCancel(true);
        modal.showModal();

        if (_syncSource) {
            _syncSource.close();
            _syncSource = null;
        }

        let url = '/api/sync/paj/' + encodeURIComponent(pajNorm) + '/movimentacoes';
        _syncSource = new EventSource(url);

        function buscarMovsEResolver() {
            fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/movimentacoes')
                .then(function(r) { return r.json(); })
                .then(function(data) { resolve(data.ok === false ? null : data); })
                .catch(function() { resolve(null); });
        }

        _syncSource.addEventListener('log', function(e) {
            logEl.textContent += e.data + '\n';
            logEl.scrollTop = logEl.scrollHeight;
        });

        _syncSource.addEventListener('done', function(e) {
            logEl.textContent += '\n' + e.data + '\n';
            logEl.scrollTop = logEl.scrollHeight;
            statusEl.textContent = 'concluído';
            statusEl.className = 'badge badge-sm badge-success';
            closeBtn.disabled = false;
            _syncBtnCancel(false);
            if (_syncSource) { _syncSource.close(); _syncSource = null; }
            showToast('Movimentações atualizadas', 'success');
            buscarMovsEResolver();
        });

        _syncSource.onerror = function() {
            statusEl.textContent = 'erro/desconectado';
            statusEl.className = 'badge badge-sm badge-error';
            closeBtn.disabled = false;
            _syncBtnCancel(false);
            if (_syncSource) { _syncSource.close(); _syncSource = null; }
            showToast('Falha ao atualizar movimentações', 'error');
            resolve(null);
        };
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
            sincronizarPaj(pajNorm, true);
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
   Reusa o modal de sync (sync-modal) para o log SSE. */
function baixarAnexosDesde(pajNorm) {
    let input = document.getElementById('anexos-desde-data');
    let data = (input && input.value || '').trim();
    if (!data) {
        showToast('Escolha uma data de corte', 'warning');
        return;
    }

    let modal = document.getElementById('sync-modal');
    let logEl = document.getElementById('sync-log');
    let statusEl = document.getElementById('sync-status');
    let titleEl = document.getElementById('sync-title');
    let closeBtn = document.getElementById('sync-close-btn');
    if (!modal || !logEl) {
        showToast('Modal de sync não encontrado na página', 'error');
        return;
    }

    logEl.textContent = '';
    if (titleEl) titleEl.textContent = 'Baixar anexos a partir de ' + data;
    statusEl.textContent = 'baixando...';
    statusEl.className = 'badge badge-sm badge-warning';
    closeBtn.disabled = true;
    _syncBtnCancel(false);  // este endpoint nao suporta cancel — esconde o botao
    modal.showModal();

    if (_syncSource) { _syncSource.close(); _syncSource = null; }

    let url = '/api/sync/paj/' + encodeURIComponent(pajNorm)
            + '/anexos-desde?data=' + encodeURIComponent(data);
    url = _syncUrlComOcr(url, true);
    _syncSource = new EventSource(url);

    _syncSource.addEventListener('log', function(e) {
        logEl.textContent += e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
    });

    _syncSource.addEventListener('done', function(e) {
        logEl.textContent += '\n' + e.data + '\n';
        logEl.scrollTop = logEl.scrollHeight;
        statusEl.textContent = 'concluído';
        statusEl.className = 'badge badge-sm badge-success';
        closeBtn.disabled = false;
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
        showToast('Anexos baixados — recarregando página', 'success');
        setTimeout(function() {
            if (!modal.open) {
                window.location.reload();
            } else {
                modal.addEventListener('close', function once() {
                    modal.removeEventListener('close', once);
                    window.location.reload();
                });
            }
        }, 500);
    });

    _syncSource.onerror = function() {
        statusEl.textContent = 'erro/desconectado';
        statusEl.className = 'badge badge-sm badge-error';
        closeBtn.disabled = false;
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
    };
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
 * quando uma MOVIMENTAÇÃO de fato foi registrada com sucesso (`resultado.fase`
 * truthy e a etapa de movimentação não falhou: `status` só vem "falha" quando
 * é exatamente essa etapa que não deu certo — tramitação/conclusão que
 * falharem DEPOIS ainda deixam a movimentação valendo, por isso "parcial"/
 * "ja_ausente" também disparam a atualização).
 *
 * IMPORTANTE: isto precisa ser ESPERADO (não só disparado) por quem chama —
 * static/js/acoes_sisdpu.js recarrega a página inteira logo depois de chamar
 * onExecutado(); se a sincronização real no SISDPU (que leva alguns segundos)
 * não tivesse terminado de escrever em metadata.json ANTES do reload, a
 * página recarregava mostrando as movimentações ainda desatualizadas — foi
 * exatamente o bug relatado em 2026-08-13 com o disparo por evento antigo. */
async function posExecucaoPaj(pajNorm, resultado) {
    if (resultado && resultado.fase && resultado.status !== 'falha') {
        try { await atualizarMovimentacoesPaj(pajNorm); } catch (e) {}
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
   sem movimentar/tramitar) e o modal de sync (sync-modal) pro log SSE, mesmo
   padrão de baixarAnexosDesde() acima. */
/** Alpine do botão "Concluir PAJ da Caixa de Entrada" do cabeçalho — decide
 * entre o caminho simples (0-1 trâmite, usa concluirPajCaixa() abaixo, com
 * log SSE) e o modal de escolha múltipla (>1 trâmite pendente, ver
 * window._paj_tramitacoes_caixa), que conclui os selecionados de uma vez via
 * /api/paj/<paj>/concluir-tramites (services/paj_service.concluir_tramites_caixa). */
function concluirPajModalApp(pajNorm, idProcesso, paj, idTramite) {
    return {
        pajNorm: pajNorm, idProcesso: idProcesso, paj: paj, idTramite: idTramite,
        tramitacoes: window._paj_tramitacoes_caixa || [],
        modalAberto: false,
        selecionados: [],
        concluindo: false,

        abrir() {
            if (this.tramitacoes.length <= 1) {
                concluirPajCaixa(this.pajNorm, this.idProcesso, this.paj, this.idTramite);
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
    if (!confirm('Concluir o trâmite de ' + (paj || pajNorm) + ' na caixa de entrada do SISDPU?\n\nIsso retira o PAJ da caixa de entrada.')) return;

    if (typeof window.garantirCofre === 'function' && !(await window.garantirCofre())) return;

    let modal = document.getElementById('sync-modal');
    let logEl = document.getElementById('sync-log');
    let statusEl = document.getElementById('sync-status');
    let titleEl = document.getElementById('sync-title');
    let closeBtn = document.getElementById('sync-close-btn');
    if (!modal || !logEl) {
        showToast('Modal de sync não encontrado na página', 'error');
        return;
    }

    logEl.textContent = '';
    if (titleEl) titleEl.textContent = 'Concluir PAJ da Caixa de Entrada — ' + (paj || pajNorm);
    statusEl.textContent = 'executando...';
    statusEl.className = 'badge badge-sm badge-warning';
    closeBtn.disabled = true;
    _syncBtnCancel(false);  // este endpoint nao suporta cancel — esconde o botao
    modal.showModal();

    if (_syncSource) { _syncSource.close(); _syncSource = null; }

    const qs = new URLSearchParams({
        id_processo: idProcesso || '', paj: paj || '', paj_norm: pajNorm || '',
        fase: '', descricao_mov: '', descricao_mov_html: '0',
        grupo: '', descricao_tram: '', prazo: '0', prazo_data: '',
        id_tramite: idTramite, concluir: '1', anexos: '[]',
    });
    _syncSource = new EventSource('/api/acoes-sisdpu/executar?' + qs.toString());

    _syncSource.addEventListener('log', function (e) {
        try {
            const d = JSON.parse(e.data);
            logEl.textContent += (d.msg || e.data) + '\n';
        } catch (err) {
            logEl.textContent += e.data + '\n';
        }
        logEl.scrollTop = logEl.scrollHeight;
    });

    _syncSource.addEventListener('done', async function (e) {
        let res = {};
        try { res = JSON.parse(e.data); } catch (err) {}
        const ok = res.ok !== false;
        logEl.textContent += '\nFinalizado.\n';
        logEl.scrollTop = logEl.scrollHeight;
        statusEl.textContent = res.status === 'ja_ausente' ? 'já estava concluído' : (ok ? 'concluído' : 'com aviso');
        statusEl.className = 'badge badge-sm ' + (ok ? 'badge-success' : 'badge-error');
        closeBtn.disabled = false;
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
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
        setTimeout(function () {
            if (!modal.open) {
                window.location.reload();
            } else {
                modal.addEventListener('close', function once() {
                    modal.removeEventListener('close', once);
                    window.location.reload();
                });
            }
        }, 500);
    });

    _syncSource.onerror = function () {
        statusEl.textContent = 'erro/desconectado';
        statusEl.className = 'badge badge-sm badge-error';
        closeBtn.disabled = false;
        if (_syncSource) { _syncSource.close(); _syncSource = null; }
    };
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
            await this.carregar();
        },
        async carregar() {
            this.carregando = true;
            this.falhouVerificacao = false;
            this.erroVerificacao = '';
            try {
                const j = await (await fetch('/api/paj/' + encodeURIComponent(this.pajNorm) + '/tramitacoes?limite=10')).json();
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

let _correcaoDiretaPajAtual = null;

function abrirCorrecaoDireta(pajNorm, nomeArquivo) {
    _correcaoDiretaPajAtual = pajNorm;
    const modal = document.getElementById('correcao-direta-modal');
    if (!modal) {
        showToast('Modal de correção não encontrado', 'error');
        return;
    }
    const label = document.getElementById('correcao-direta-paj-label');
    if (label) label.textContent = pajNorm;
    const ctx = document.getElementById('correcao-direta-contexto');
    if (ctx) {
        ctx.textContent = nomeArquivo
            ? ('Arquivo de referência: ' + nomeArquivo)
            : '';
    }
    const ta = document.getElementById('correcao-direta-text');
    if (ta) ta.value = '';
    modal.showModal();
    setTimeout(() => ta && ta.focus(), 50);
}

async function enviarCorrecaoDireta() {
    const pajNorm = _correcaoDiretaPajAtual;
    if (!pajNorm) {
        showToast('PAJ não identificado', 'warning');
        return;
    }
    const textarea = document.getElementById('correcao-direta-text');
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
        document.getElementById('correcao-direta-modal')?.close();
        _triggerElaborarPolling();
    } catch (e) {
        showToast('Erro: ' + e.message, 'error');
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
   Notas pessoais por PAJ (NOTAS.md) — autosave 2s
   ========================================================= */
let _notasTimer = null;
const _notasCarregado = {};  // evita recarregar ao re-entrar na aba

async function carregarNotas(pajNorm) {
    if (_notasCarregado[pajNorm]) return;
    let ta = document.getElementById('notas-textarea');
    let status = document.getElementById('notas-status');
    if (!ta) return;
    if (status) status.textContent = 'carregando...';
    try {
        let resp = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/notas');
        let data = await resp.json();
        ta.value = data.texto || '';
        _notasCarregado[pajNorm] = true;
        if (status) status.textContent = ta.value ? 'carregado' : 'em branco';
    } catch (e) {
        if (status) status.textContent = 'erro ao carregar: ' + e.message;
    }
}

function agendarSalvarNotas(pajNorm) {
    let status = document.getElementById('notas-status');
    if (status) status.textContent = 'digitando...';
    if (_notasTimer) clearTimeout(_notasTimer);
    _notasTimer = setTimeout(function() { salvarNotas(pajNorm); }, 2000);
}

async function salvarNotas(pajNorm) {
    let ta = document.getElementById('notas-textarea');
    let status = document.getElementById('notas-status');
    if (!ta) return;
    if (status) status.textContent = 'salvando...';
    try {
        let resp = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/notas', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ texto: ta.value }),
        });
        let data = await resp.json();
        if (!resp.ok || !data.ok) {
            if (status) status.textContent = 'erro: ' + (data.erro || resp.status);
            showToast('Erro ao salvar notas', 'error');
            return;
        }
        if (status) status.textContent = 'salvo';
    } catch (e) {
        if (status) status.textContent = 'erro: ' + e.message;
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
async function abrirPastaPaj(pajNorm, btn) {
    const original = btn ? btn.innerHTML : '';
    if (btn) { btn.disabled = true; }
    try {
        const r = await fetch('/api/paj/' + encodeURIComponent(pajNorm) + '/abrir-pasta', { method: 'POST' });
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
    const m = txt.match(/\(([^()]+)\)\s*$/);
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
   via /api/prazos/abrir-paj — resolve o link stateless e redireciona). */
function abrirPajPainel(pajNorm) {
    if (!pajNorm) return;
    window.open('/paj/' + pajNorm, '_blank');
}
function abrirPajNavegador(pajDisplay) {
    if (!pajDisplay) return;
    window.open('/api/prazos/abrir-paj?paj=' + encodeURIComponent(pajDisplay), '_blank');
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
