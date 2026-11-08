// Juntada ao SISDPU dos dossiês do menu Consultas da página do PAJ — parte
// ÚNICA do front (05/11/2026, pedido do usuário: "uniformize essas rotinas de
// download de dossiês e de juntada ao PAJ"). Usada por Dossiê Seguro-Desemprego
// Web (dossie_sd.js), Dossiê CadÚnico (dossie_cadunico.js), Extrato de processo
// CRPS e Íntegra do processo e-Sisrec (extrato_crps.js). O bloco visual é o
// macro `bloco_juntada` de templates/partials/dossie_juntada.html; no servidor,
// services/juntada_dossie_service.py.
//
// Uso: `return { ...juntadaDossie('/api/dossie-sd', 'Dossiê'), pajNorm, ... }`.
// O componente precisa ter `pajNorm` e `fetchStatus()` (que preenche
// `arquivos`, `juntada` e `erroJuntada` a partir do status do serviço).
function juntadaDossie(api, rotulo) {
    return {
        arquivos: [],
        juntada: null,        // null | 'running' | 'ok' | 'erro' | 'dispensada'
        erroJuntada: '',
        _timerJuntada: null,

        urlArquivoDossie(nome) {
            return api + '/arquivo/' + this.pajNorm + '?nome=' + encodeURIComponent(nome);
        },

        async juntar() {
            if (this.juntada === 'running' || this.juntada === 'ok') return;
            let d;
            try {
                const resp = await fetch(api + '/juntar/' + this.pajNorm, { method: 'POST' });
                d = await resp.json().catch(() => ({}));
            } catch (e) {
                showToast('Falha de rede: ' + e, 'error');
                return;
            }
            if (!d.ok) { showToast('Erro: ' + (d.erro || 'falha ao iniciar a juntada'), 'error'); return; }
            this.juntada = 'running';
            this.erroJuntada = '';
            this._acompanharJuntada();
        },

        async dispensarJuntada() {
            this.juntada = 'dispensada';
            if ('perguntaJuntada' in this) this.perguntaJuntada = false;
            try { await fetch(api + '/dispensar-juntada/' + this.pajNorm, { method: 'POST' }); } catch (e) { /* só evita repetir a pergunta */ }
        },

        _acompanharJuntada() {
            if (this._timerJuntada) return;
            this._timerJuntada = setInterval(async () => {
                await this.fetchStatus();
                if (this.juntada === 'running') return;
                clearInterval(this._timerJuntada);
                this._timerJuntada = null;
                if (this.juntada === 'ok') showToast(rotulo + ' juntado ao SISDPU com sucesso', 'success');
                else showToast('Erro ao juntar ao SISDPU: ' + (this.erroJuntada || 'ver log do servidor'), 'error');
            }, 3000);
        },
    };
}
