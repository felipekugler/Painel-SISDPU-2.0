/* Sessão do MCP do SIS-DPU 2.0 — o servidor não renova a sessão sozinho (só
 * `authorization_code`, sem refresh_token; ~8 h), então o painel:
 *
 *  - avisa antes de vencer (toast com "Reconectar" aos 30 e aos 10 minutos, e
 *    quando venceu) — consulta só o token guardado (/api/sisdpu2/status-selo),
 *    nada que conte no limite diário;
 *  - reconecta num clique (abre o login da DPU numa janelinha, igual a
 *    Configurações), sem sair da tela;
 *  - `garantirSessaoMcp()`: chamada ANTES de fluxos que começam no e-proc
 *    (movimentação conjunta, mera ciência) — com pouca sessão, pede a
 *    reconexão antes, para o e-proc nunca ser protocolado com risco de o
 *    SISDPU não gravar depois.
 */
(function () {
    'use strict';

    const MINIMO_PARA_GRAVAR = 10;      // minutos
    const AVISOS = [30, 10];            // minutos
    const INTERVALO_MS = 5 * 60 * 1000;

    function ativo() { return !!window.PAINEL_FONTE_SISDPU_MCP; }

    async function status() {
        try { return await (await fetch('/api/sisdpu2/status-selo')).json(); } catch (e) { return null; }
    }

    // Abre o login da DPU e espera o retorno (a página de retorno avisa por
    // postMessage "sisdpu2-login-ok" / "sisdpu2-login-erro"). true = reconectado.
    function reconectar() {
        return new Promise(async function (resolve) {
            let r;
            try { r = await (await fetch('/api/sisdpu2/login')).json(); } catch (e) { r = { ok: false, erro: String(e) }; }
            if (!r.ok) {
                if (typeof showToast === 'function') showToast('Não foi possível iniciar o login no SIS-DPU 2.0: ' + (r.erro || 'erro'), 'error');
                resolve(false); return;
            }
            const jan = window.open(r.url, 'sisdpu2-login', 'width=560,height=760');
            let fim = false;
            const terminar = function (ok) {
                if (fim) return;
                fim = true;
                window.removeEventListener('message', ouvir);
                clearInterval(vigia);
                if (ok) {
                    fecharAviso();
                    if (typeof showToast === 'function') showToast('SIS-DPU 2.0 reconectado — sessão renovada.', 'success');
                }
                resolve(ok);
            };
            const ouvir = function (e) {
                if (typeof e.data !== 'string' || !e.data.startsWith('sisdpu2-login-')) return;
                terminar(e.data === 'sisdpu2-login-ok');
            };
            window.addEventListener('message', ouvir);
            // Janela fechada sem concluir = desistiu (confere o token antes de dizer não).
            const vigia = setInterval(async function () {
                if (jan && !jan.closed) return;
                const s = await status();
                terminar(!!(s && s.ok && s.minutos != null && s.minutos >= MINIMO_PARA_GRAVAR));
            }, 1500);
        });
    }

    // --- Aviso no padrão de toast do painel (canto inferior direito) ---------
    function fecharAviso() {
        const el = document.getElementById('sessao-mcp-aviso');
        if (el) el.remove();
    }

    function avisar(texto, tom, semReconectar) {
        const container = document.getElementById('toast-container');
        if (!container) return;
        let el = document.getElementById('sessao-mcp-aviso');
        if (!el) {
            el = document.createElement('div');
            el.id = 'sessao-mcp-aviso';
            container.appendChild(el);
        }
        el.className = 'alert ' + (tom === 'erro' ? 'alert-error' : 'alert-warning') + ' text-sm py-2 px-4 shadow-lg';
        el.innerHTML = '<span></span><div class="flex gap-1">'
            + '<button type="button" class="btn btn-xs">Reconectar</button>'
            + '<button type="button" class="btn btn-xs btn-ghost" aria-label="Fechar">✕</button></div>';
        el.firstChild.textContent = texto;
        const [btnRec, btnX] = el.querySelectorAll('button');
        btnRec.onclick = function () { reconectar(); };
        if (semReconectar) btnRec.remove();
        btnX.onclick = fecharAviso;
    }

    let ultimoAviso = null;  // menor faixa já avisada nesta página (30, 10 ou 0)
    async function conferir() {
        if (!ativo()) return;
        const s = await status();
        if (!s) return;
        if (s.teto) {  // limite diário de leitura: a sessão continua válida
            if (ultimoAviso !== 'teto') {
                ultimoAviso = 'teto';
                avisar('Limite diário de leitura do SIS-DPU 2.0 atingido — o painel volta a ler amanhã (a sessão continua ativa).', 'aviso', true);
            }
            return;
        }
        if (!s.ok || s.minutos === 0) {
            if (ultimoAviso !== 0) {
                ultimoAviso = 0;
                avisar('A sessão do SIS-DPU 2.0 terminou — reconecte para ler e gravar pelo MCP.', 'erro');
            }
            return;
        }
        if (s.minutos == null) return;
        const faixa = AVISOS.filter(function (m) { return s.minutos <= m; }).pop();
        if (faixa == null) { ultimoAviso = null; return; }
        if (ultimoAviso === null || faixa < ultimoAviso) {
            ultimoAviso = faixa;
            avisar('A sessão do SIS-DPU 2.0 termina em ' + s.minutos + ' min.');
        }
    }

    // --- Antes de fluxos que começam no e-proc --------------------------------
    function perguntar(texto) {
        return new Promise(function (resolve) {
            let dlg = document.getElementById('sessao-mcp-dialog');
            if (!dlg) {
                dlg = document.createElement('dialog');
                dlg.id = 'sessao-mcp-dialog';
                dlg.className = 'modal';
                dlg.innerHTML = '<div class="modal-box max-w-md">'
                    + '<h3 class="font-bold text-base mb-2">Sessão do SIS-DPU 2.0</h3>'
                    + '<p class="text-sm" id="sessao-mcp-texto"></p>'
                    + '<div class="modal-action">'
                    + '<button type="button" class="btn btn-sm btn-ghost" id="sessao-mcp-cancelar">Cancelar</button>'
                    + '<button type="button" class="btn btn-sm btn-success" id="sessao-mcp-reconectar">Reconectar agora</button>'
                    + '</div></div>';
                document.body.appendChild(dlg);
            }
            document.getElementById('sessao-mcp-texto').textContent = texto;
            document.getElementById('sessao-mcp-cancelar').onclick = function () { dlg.close(); resolve(false); };
            document.getElementById('sessao-mcp-reconectar').onclick = async function () {
                this.disabled = true;
                const ok = await reconectar();
                this.disabled = false;
                dlg.close();
                resolve(ok);
            };
            dlg.showModal();
        });
    }

    async function garantirSessaoMcp(minimo) {
        if (!ativo()) return true;
        minimo = minimo || MINIMO_PARA_GRAVAR;
        const s = await status();
        if (s && s.ok && (s.minutos == null || s.minutos >= minimo)) return true;
        // Limite diário de leitura não é sessão vencida: as gravações seguiram
        // aceitas depois do teto (15/10/2026) — não pede reconexão.
        if (s && s.teto && (s.minutos == null || s.minutos >= minimo)) return true;
        const texto = !s || !s.ok
            ? 'A sessão do SIS-DPU 2.0 terminou. Reconecte antes de executar: o e-proc é protocolado '
              + 'primeiro (irreversível) e o SISDPU precisa gravar em seguida.'
            : 'A sessão do SIS-DPU 2.0 termina em ' + s.minutos + ' min. Reconecte antes de executar, para o SISDPU '
              + 'não ficar sem gravar depois do protocolo no e-proc (irreversível).';
        return perguntar(texto);
    }

    window.reconectarSisdpu2 = reconectar;
    window.garantirSessaoMcp = garantirSessaoMcp;

    function iniciar() {
        if (!ativo()) return;
        conferir();
        setInterval(conferir, INTERVALO_MS);
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar);
    else iniciar();
})();
