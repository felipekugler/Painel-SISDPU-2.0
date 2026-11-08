// "+ Adicionar Externo" abrindo direto na pasta do PAJ (06/11/2026). O
// <input type="file"> não deixa escolher a pasta inicial; então o painel (que
// roda neste computador) abre a janela nativa "Abrir" do Windows na pasta do
// PAJ — ver services/seletor_arquivos_service.py. Devolve os arquivos
// escolhidos como `File` (lista vazia se cancelar), ou `null` quando não deu
// para usar a janela nativa (sem PAJ, pasta inexistente, falha) — aí o
// chamador abre o seletor do navegador, como antes.
let _seletorNativoIndisponivel = false;

async function escolherArquivosNaPastaDoPaj(pajNorm, titulo) {
    if (!pajNorm || _seletorNativoIndisponivel) return null;
    let d;
    try {
        const resp = await fetch('/api/seletor-arquivos/escolher/' + encodeURIComponent(pajNorm)
            + '?titulo=' + encodeURIComponent(titulo || ''), { method: 'POST' });
        d = await resp.json();
    } catch (e) {
        return null;
    }
    if (!d.ok) {
        if (d.motivo === 'ocupado') { showToast(d.erro, 'warning'); return []; }
        if (d.motivo === 'falha') _seletorNativoIndisponivel = true;
        return null;
    }
    const arquivos = [];
    for (const a of d.arquivos || []) {
        try {
            const r = await fetch('/api/seletor-arquivos/arquivo/' + encodeURIComponent(a.token));
            if (!r.ok) continue;
            const blob = await r.blob();
            arquivos.push(new File([blob], a.nome, { type: blob.type || '' }));
        } catch (e) { /* ignora o arquivo que não deu para ler */ }
    }
    return arquivos;
}
