/* Widget FLUTUANTE de sincronizacao SISDPU (2026-08-28) — substitui o antigo
 * <dialog class="modal"> que travava a pagina inteira com showModal().
 *
 * MODELO: a sincronizacao roda no SERVIDOR, desacoplada de qualquer conexao
 * HTTP (ver services/sync_service.py) — este widget so' OBSERVA via
 * /api/sync/stream, e pode ser minimizado, fechado (a aba, ou so' o widget)
 * e reaberto de qualquer pagina do painel sem afetar o trabalho em si.
 *
 * Uso pelos demais scripts (static/js/app.js etc.):
 *   SyncWidget.iniciar('/api/sync?anexos=0', {
 *       titulo: 'Sincronizar caixa SISDPU',
 *       onDone: function(status) { ...reload/toast específico... }
 *   });
 * `iniciar()` faz o POST que dispara a sincronizacao; se aceito, conecta ao
 * stream compartilhado e mostra o widget. Se ja houver outra rodando, o
 * POST volta 409 e nada e' iniciado — so' um toast de erro.
 *
 * `checarEmAndamento()` roda sozinha em TODA pagina (DOMContentLoaded) —
 * reconecta ao widget se algo estiver rodando (ou tiver terminado e ainda
 * nao foi "visto"/dispensado nesta aba), independente de qual pagina
 * iniciou. */
const SyncWidget = (function () {
    let es = null;
    let logBuffer = '';
    let currentId = 0;
    let running = false;

    function el(id) { return document.getElementById(id); }

    function setStatus(texto, classe) {
        const s = el('sync-widget-status');
        if (s) { s.textContent = texto; s.className = 'badge badge-sm ' + classe; }
    }

    function appendLog(linha) {
        if (!linha) return;
        logBuffer += linha + '\n';
        const logEl = el('sync-widget-log');
        if (logEl) {
            logEl.textContent = logBuffer;
            logEl.scrollTop = logEl.scrollHeight;
        }
    }

    function definirTitulo(titulo) {
        const t1 = el('sync-widget-titulo');
        const t2 = el('sync-widget-pill-titulo');
        const texto = titulo || 'Sincronização';
        if (t1) t1.textContent = texto;
        if (t2) t2.textContent = texto;
    }

    function definirSpinnerPill(rodando) {
        const sp = el('sync-widget-pill-spinner');
        const ic = el('sync-widget-pill-icone');
        if (sp) sp.style.display = rodando ? '' : 'none';
        if (ic) ic.style.display = rodando ? 'none' : '';
    }

    function raiz() { return el('sync-widget'); }

    function expandir() {
        const r = raiz(); if (r) r.className = '';
        const card = el('sync-widget-card'); if (card) card.style.display = '';
        const pill = el('sync-widget-pill'); if (pill) pill.style.display = 'none';
        try { localStorage.setItem('syncWidgetExpandido', '1'); } catch (e) { /* ignora */ }
    }

    function minimizar() {
        const r = raiz(); if (r) r.className = '';
        const card = el('sync-widget-card'); if (card) card.style.display = 'none';
        const pill = el('sync-widget-pill'); if (pill) pill.style.display = '';
        try { localStorage.setItem('syncWidgetExpandido', '0'); } catch (e) { /* ignora */ }
    }

    function ocultar() {
        const r = raiz(); if (r) r.className = 'sync-widget-oculto';
        const card = el('sync-widget-card'); if (card) card.style.display = 'none';
        const pill = el('sync-widget-pill'); if (pill) pill.style.display = 'none';
    }

    /* "X" do card — so' fecha de vez (esconde tudo) quando NAO esta rodando;
     * enquanto roda, so' minimiza (o trabalho continua, so' nao mostra o log). */
    function dispensar() {
        if (running) { minimizar(); return; }
        ocultar();
        try { if (currentId) localStorage.setItem('syncWidgetDismissedId', String(currentId)); } catch (e) { /* ignora */ }
    }

    function cancelar() {
        const btn = el('sync-widget-cancel-btn');
        if (btn) { btn.disabled = true; btn.textContent = 'cancelando...'; }
        setStatus('cancelando...', 'badge-warning');
        fetch('/api/sync/cancel', { method: 'POST' })
            .then(function (r) { return r.json(); })
            .then(function (j) {
                if (!j.ok) {
                    if (window.showToast) showToast('Sem sync rodando pra cancelar', 'info');
                    if (btn) { btn.disabled = false; btn.textContent = 'Cancelar'; }
                } else if (window.showToast) {
                    showToast('Cancelamento solicitado — aguardando o PAJ atual terminar', 'warning');
                }
            })
            .catch(function () {
                if (btn) { btn.disabled = false; btn.textContent = 'Cancelar'; }
            });
    }

    function _finalizarUI(status) {
        running = false;
        currentId = (status && status.id) || currentId;
        const ok = status ? status.ok : null;
        setStatus(ok === false ? 'erro' : 'concluído', ok === false ? 'badge-error' : 'badge-success');
        definirSpinnerPill(false);
        const cancelBtn = el('sync-widget-cancel-btn');
        if (cancelBtn) cancelBtn.style.display = 'none';
    }

    /* Conecta ao stream compartilhado e passa a exibir o log em tempo real.
     * `opts.onDone(status)` roda so' se AINDA estivermos na mesma pagina/
     * contexto JS que chamou `iniciar()` (se o usuario navegou pra outra
     * pagina no meio, esse contexto simplesmente nao existe mais — a proxima
     * pagina reconecta sozinha via `checarEmAndamento()`, sem callback
     * especifico, so' toast genérico).
     *
     * `opts.es`: passe um EventSource JA ABERTO (ex.: "Ações no SISDPU"/
     * "Ações no Eproc", que usam um backend PRÓPRIO — não passam pelo
     * sync_service/`/api/sync/stream`) pra exibi-lo neste MESMO widget, só
     * pelo reaproveitamento visual — sem `opts.es`, conecta a
     * `/api/sync/stream` (o caminho normal das sincronizações SISDPU).
     * IMPORTANTE: com `opts.es`, a sincronizacao NÃO sobrevive a uma
     * navegação de página (não é rastreada por `sync_service` — não há
     * status pra `checarEmAndamento()` reconectar). `status` passado a
     * `onDone` nesse caso é sempre `null` (não existe `/api/sync/status`
     * pra esse backend). */
    function conectar(opts) {
        opts = opts || {};
        if (es) { es.close(); es = null; }
        logBuffer = '';
        running = true;
        definirTitulo(opts.titulo);
        setStatus('sincronizando...', 'badge-warning');
        const cancelBtn = el('sync-widget-cancel-btn');
        const suportaCancel = opts.semCancelar !== true;
        if (cancelBtn) {
            cancelBtn.style.display = suportaCancel ? '' : 'none';
            cancelBtn.disabled = false;
            cancelBtn.textContent = 'Cancelar';
        }
        definirSpinnerPill(true);
        if (opts.minimizado) minimizar(); else expandir();

        const transformar = opts.transformarLinha || function (dadoCru) { return dadoCru; };

        es = opts.es || new EventSource('/api/sync/stream');
        es.addEventListener('log', function (e) { appendLog(transformar(e.data)); });
        es.addEventListener('done', function (e) {
            appendLog(transformar(e.data));
            if (es) { es.close(); es = null; }
            if (opts.es) {
                // Backend proprio (sem /api/sync/status) — nao ha status pra buscar.
                _finalizarUI(null);
                if (opts.onDone) opts.onDone(null);
                return;
            }
            fetch('/api/sync/status').then(function (r) { return r.json(); }).then(function (status) {
                _finalizarUI(status);
                if (opts.onDone) opts.onDone(status);
            }).catch(function () {
                _finalizarUI(null);
                if (opts.onDone) opts.onDone(null);
            });
        });
        es.onerror = function () {
            if (es) { es.close(); es = null; }
            running = false;
            definirSpinnerPill(false);
            setStatus('desconectado', 'badge-error');
        };
    }

    /* Dispara `url` (POST) — inicia a sincronizacao em segundo plano no
     * servidor. Se aceito (200), conecta ao stream e mostra o widget. Se
     * houver outra sincronizacao em andamento (409), so' avisa em toast —
     * nada foi iniciado, e o widget que já estava acompanhando a outra
     * sincronizacao continua do jeito que estava. */
    function iniciar(url, opts) {
        opts = opts || {};
        return fetch(url, { method: 'POST' })
            .then(function (r) {
                return r.json().catch(function () { return {}; }).then(function (j) {
                    return { status: r.status, body: j };
                });
            })
            .then(function (res) {
                if (res.status !== 200 || res.body.ok === false) {
                    if (window.showToast) {
                        showToast(res.body.mensagem || 'Não foi possível iniciar a sincronização', 'error');
                    }
                    return false;
                }
                conectar(opts);
                return true;
            })
            .catch(function (e) {
                if (window.showToast) showToast('Falha de rede: ' + e.message, 'error');
                return false;
            });
    }

    /* Roda em TODA pagina ao carregar — reconecta se algo estiver rodando,
     * ou mostra o resultado se terminou e ainda nao foi "visto" nesta aba
     * (rastreado por id incremental em localStorage, ja que a sessao de
     * sincronizacao sobrevive no servidor indefinidamente ate a proxima
     * comecar). */
    function checarEmAndamento() {
        fetch('/api/sync/status').then(function (r) { return r.json(); }).then(function (status) {
            if (!status || !status.id) return;
            currentId = status.id;

            let dismissedId = 0;
            try { dismissedId = parseInt(localStorage.getItem('syncWidgetDismissedId') || '0', 10); } catch (e) { /* ignora */ }
            if (status.done && status.id <= dismissedId) return; // ja visto/dispensado

            logBuffer = (status.log || []).join('\n') + ((status.log && status.log.length) ? '\n' : '');
            const logEl = el('sync-widget-log');
            if (logEl) { logEl.textContent = logBuffer; logEl.scrollTop = logEl.scrollHeight; }
            definirTitulo(status.titulo);
            running = !!status.rodando;

            const cancelBtn = el('sync-widget-cancel-btn');
            if (cancelBtn) cancelBtn.style.display = status.rodando ? '' : 'none';

            if (status.rodando) {
                setStatus('sincronizando...', 'badge-warning');
                definirSpinnerPill(true);
            } else {
                setStatus(status.ok === false ? 'erro' : 'concluído', status.ok === false ? 'badge-error' : 'badge-success');
                definirSpinnerPill(false);
            }

            let expandidoAntes = '0';
            try { expandidoAntes = localStorage.getItem('syncWidgetExpandido') || '0'; } catch (e) { /* ignora */ }
            if (expandidoAntes === '1') expandir(); else minimizar();

            if (status.rodando) {
                if (es) { es.close(); }
                es = new EventSource('/api/sync/stream');
                es.addEventListener('log', function (e) { appendLog(e.data); });
                es.addEventListener('done', function (e) {
                    appendLog(e.data);
                    if (es) { es.close(); es = null; }
                    fetch('/api/sync/status').then(function (r) { return r.json(); }).then(function (st) {
                        _finalizarUI(st);
                        if (window.showToast) showToast((st && st.titulo) + ' — concluída', st && st.ok === false ? 'error' : 'success');
                    }).catch(function () { _finalizarUI(null); });
                });
                es.onerror = function () {
                    if (es) { es.close(); es = null; }
                    running = false;
                    definirSpinnerPill(false);
                };
            }
        }).catch(function () { /* silencioso — sem widget pra mostrar mesmo */ });
    }

    document.addEventListener('DOMContentLoaded', checarEmAndamento);

    return {
        iniciar: iniciar,
        conectar: conectar,
        expandir: expandir,
        minimizar: minimizar,
        dispensar: dispensar,
        cancelar: cancelar,
        estaRodando: function () { return running; },
    };
})();
