"""Organiza os documentos de um PAJ numa subpasta `Ajuizamento/`, via sessão
real do Claude Code CLI executando a skill `SISDPU-OrganizarArquivos`.

HISTÓRICO — por que isto não chama mais um script determinístico via
subprocess: até 2026-08-19 este service rodava `organizar_ajuizamento.py` como
um pipeline automático (`subprocess.run([sys.executable, script, pasta])`),
herança das versões 1.0-4.0 da skill. Mas em 2026-08-15 a skill foi
REDESENHADA (v5.0) — o método automático (OCR + pontuação de palavras-chave)
causava duplicações confirmadas por hash MD5, e foi eliminado por pedido
explícito do usuário. `organizar_ajuizamento.py` hoje é só um MÓDULO DE
FUNÇÕES UTILITÁRIAS (sem bloco `__main__`) que um agente importa e chama
documento por documento, depois de LER e CONFIRMAR cada um — exatamente como
um humano faria. Rodá-lo como subprocesso não fazia mais nada (só definia
funções e saía com código 0), e o botão relatava "pasta Ajuizamento não foi
gerada" para TODO PAJ desde aquela data (achado organizando o PAJ
2026/029-02269 em 2026-08-19).

MÉTODO NOVO (2026-08-19): mesmo padrão já usado por
`services/analise_pretensao_service.py` para as skills de diagnóstico
globais — reusa o motor de sessão do `chat_service` (`ChatSession`), mas com
instrução PRÓPRIA (`_montar_instrucao_organizar`), sem o wrapper genérico
"produza o TEXTO da peça" de `chat_service._montar_instrucao` (não se aplica
aqui). A instrução embute as regras da skill v5.0 E as lições tiradas de 3
organizações manuais reais (PAJ 2026/029-02269, 02299, 02329, mesma data):
ler a petição inicial ANTES de classificar (só assim dá pra saber quais NBs
são o pedido, em vez de assumir pelo nome do arquivo), confirmar por
OCR/visão quando a busca por título falhar (glifo quebrado em headers
estilizados de alguns PDFs), comparar hash MD5 antes de aceitar dois
documentos como "diferentes", e tratar qualquer duplicata acusada por
`verificar_duplicatas()` como erro bloqueante, não como aviso.

IMPORTANTE — layout de pastas: a skill trabalha sobre `Arquivos/` (onde o
painel guarda os documentos baixados do SISDPU), não a raiz do PAJ — a
instrução já informa isso; o agente grava o resultado direto em
`{pasta_do_paj}\\Ajuizamento\\` (pasta-irmã de `Arquivos`), sem passo
intermediário de mover pasta (diferente do fluxo antigo via subprocess).

Persistência: `organizar_ajuizamento.json` na pasta do PAJ (mesmo arquivo de
antes — só o conteúdo mudou: `log` agora é o RESUMO ESTRUTURADO que o agente
produz, não mais stdout/stderr de um script).
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import unicodedata
from pathlib import Path

from config import PAJS_DIR, TIMEOUT_TOTAL
from services import historico
from services.chat_service import ChatSession

# Pasta da skill — vive no perfil GLOBAL do usuário (mesmo padrão das skills
# de diagnóstico, ver services/analise_pretensao_service.py). O Claude Code
# CLI descobre skills globais independente do cwd do subprocess, mas citamos
# os caminhos explicitamente na instrução para não depender só do nome.
SKILL_DIR = Path.home() / ".claude" / "skills" / "SISDPU-OrganizarArquivos"

_STATE_FILE = "organizar_ajuizamento.json"
_sessions: dict[str, ChatSession] = {}


def _sem_acento(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn"
    ).lower()


def tem_peticao_inicial(paj_norm: str) -> bool:
    """True se houver algum arquivo com 'peticao'/'petição' no nome — na
    subpasta `Peças IA/` (destino padrão da análise de pretensão, ver
    services/analise_pretensao_service.py) OU na raiz do PAJ (compatibilidade
    com petições geradas antes dessa subpasta existir). Usado para habilitar o
    botão 'Organizar Documentos para Ajuizamento'."""
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return False
    pastas_a_checar = [pasta, pasta / "Peças IA"]
    for p in pastas_a_checar:
        if not p.exists() or not p.is_dir():
            continue
        for f in p.iterdir():
            if f.is_file() and "peticao" in _sem_acento(f.stem):
                return True
    return False


def pasta_ajuizamento_existe(paj_norm: str) -> bool:
    return (PAJS_DIR / paj_norm / "Ajuizamento").exists()


def _arquivo_estado(paj_norm: str) -> Path:
    return PAJS_DIR / paj_norm / _STATE_FILE


def ler_disco(paj_norm: str) -> dict | None:
    f = _arquivo_estado(paj_norm)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_status(paj_norm: str) -> dict:
    # Confia na sessão em memória sempre que ela existir (qualquer status, não
    # só "running") — mesmo motivo do get_status() de analise_pretensao_service:
    # a thread leitora do subprocess marca sess.status = "done" imediatamente
    # ao processar o evento "result", mas quem grava o disco é a thread
    # controladora (_run -> _aguardar_conclusao), que só percebe a mudança no
    # próximo polling. Sem confiar na sessão em memória o painel via um "idle"
    # fantasma nessa janela e parava de acompanhar o progresso.
    sess = _sessions.get(paj_norm)
    if sess:
        return {"status": sess.status, "log": sess.summary, "error": sess.error}
    disco = ler_disco(paj_norm)
    if disco:
        return disco
    return {"status": "idle", "log": "", "error": ""}


def reconciliar_sessoes_interrompidas() -> int:
    """Mesmo princípio de
    `analise_pretensao_service.reconciliar_sessoes_interrompidas` (ver lá a
    motivação completa — achado ao vivo 2026-09-30) — corrige
    `organizar_ajuizamento.json` que ficou com status "running" gravado por
    uma sessão que o painel matou no meio (restart). Chamada uma vez no
    startup (ver `app.py`); `_sessions` está sempre vazio nesse momento, então
    qualquer "running" aqui é órfão.

    Critério: pasta `Ajuizamento/` já existe e não está vazia (o trabalho
    terminou) => "done"; senão => "error". Também acrescenta um evento NOVO
    em historico.jsonl (append-only) com o status corrigido, mesmo padrão."""
    if not PAJS_DIR.exists():
        return 0

    corrigidos = 0
    for pasta in PAJS_DIR.iterdir():
        if not pasta.is_dir() or not pasta.name.startswith("PAJ-"):
            continue
        paj_norm = pasta.name
        disco = ler_disco(paj_norm)
        if not disco or disco.get("status") != "running":
            continue

        pasta_ajuiz = pasta / "Ajuizamento"
        terminou = pasta_ajuiz.exists() and any(pasta_ajuiz.iterdir())
        if terminou:
            novo_status = "done"
            erro = ""
        else:
            novo_status = "error"
            erro = "Sessão interrompida por reinício do painel antes da conclusão."

        disco["status"] = novo_status
        if erro:
            disco["error"] = erro
        with contextlib.suppress(Exception):
            _arquivo_estado(paj_norm).write_text(
                json.dumps(disco, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        with contextlib.suppress(Exception):
            historico.registrar(
                paj_norm,
                "organizar_ajuizamento",
                status=novo_status,
                resumo=(
                    "(status corrigido no início do painel — sessão interrompida "
                    "por reinício antes de concluir, mas a pasta Ajuizamento já "
                    "tinha sido gerada com sucesso)"
                    if terminou else ""
                ),
                error=erro,
            )
        corrigidos += 1
    return corrigidos


# Reforça idioma — mesma preocupação/redação de analise_pretensao_service:
# sem instrução explícita, narração de passagem entre leitura de anexos tende
# a sair em inglês mesmo com o restante em português.
_INSTRUCAO_IDIOMA = (
    "IMPORTANTE sobre idioma: toda a sua comunicação nesta tarefa deve ser em "
    "português do Brasil — inclusive comentários breves de passagem entre a "
    "leitura de um documento e outro (ex.: \"vou conferir a página 8 deste "
    "PDF antes de extrair\"). Não narre nem pense em voz alta em inglês em "
    "nenhum momento.\n\n"
)


def _montar_instrucao_organizar(paj_norm: str, forcar: bool) -> str:
    pasta = PAJS_DIR / paj_norm
    pasta_arquivos = pasta / "Arquivos"
    pasta_pecas_ia = pasta / "Peças IA"
    pasta_destino = pasta / "Ajuizamento"

    passo_recriar = (
        f"0. A pasta `{pasta_destino}\\` já existe de uma organização "
        "anterior — apague-a por completo (ex.: `shutil.rmtree`) e recrie do "
        "zero antes de montar os arquivos novos. Não misture arquivos de "
        "rodadas diferentes.\n\n"
        if forcar
        else ""
    )

    return (
        f"Organize os documentos do PAJ {paj_norm} para ajuizamento, seguindo "
        f"INTEGRALMENTE a skill `SISDPU-OrganizarArquivos` "
        f"(leia `{SKILL_DIR / 'SKILL.md'}` primeiro) — método v5.0: leitura "
        "manual e verificada de CADA documento antes de classificar, SEM "
        "classificação automática por OCR/pontuação de palavras-chave (essa "
        "abordagem, usada nas versões 1.0-4.0, causou duplicações "
        "confirmadas por hash MD5 e foi eliminada por pedido explícito do "
        "usuário). Importe as funções utilitárias de "
        f"`{SKILL_DIR / 'organizar_ajuizamento.py'}` "
        f"(`sys.path.insert(0, r'{SKILL_DIR}')` antes do import) e escreva "
        "você mesmo um script Python específico para este PAJ, chamando-as "
        "uma a uma — exatamente como a skill descreve. Salve esse script na "
        "pasta TEMPORÁRIA do sistema (%TEMP%), NUNCA na pasta do PAJ, e "
        "apague-o ao terminar.\n\n"
        "## Passos obrigatórios, nesta ordem\n\n"
        + passo_recriar
        + "1. **Leia a petição inicial ANTES de classificar qualquer "
        f"documento**, se existir (`{pasta_pecas_ia}\\peticao_inicial.txt` — "
        "ou `.docx` se o `.txt` não existir). É dela que você descobre QUAIS "
        "NB(s) são realmente pedidos nesta ação (seção 'Dos Pedidos' e onde "
        "o NB aparece destacado no relato dos fatos) — nunca assuma pelo "
        "nome do arquivo, pela data mais recente, ou por 'parecer' o "
        "principal. Indeferimentos/PROCADMs de NB(s) que NÃO constem do "
        "pedido — mesmo que citados de passagem na petição (ex.: para "
        "afastar preliminar de coisa julgada) — vão para a categoria 8 "
        "(VERIFICAR) rotulados com o motivo, NUNCA para a categoria 3. Se "
        "não houver petição ainda, classifique pelo NB do indeferimento mais "
        "recente e sinalize essa suposição no resumo final.\n\n"
        f"2. Rode sobre `{pasta_arquivos}\\` (é lá que estão os documentos de "
        "origem baixados do SISDPU — a skill, por padrão, varre a raiz da "
        "pasta que você indicar). **Monte a pasta Ajuizamento inteira num "
        "diretório TEMPORÁRIO fora da árvore do PAJ** (ex.: `tempfile."
        "mkdtemp()`) — copie/extraia/comprima/verifique tudo lá — e só ao "
        "final MOVA a pasta pronta e completa para o destino definitivo "
        f"(`{pasta_destino}\\`) numa única operação. Isso evita reescrever "
        "arquivos numa pasta que o Defensor pode estar consultando/com "
        "preview aberto ao mesmo tempo (já causou `PermissionError` "
        "persistente e específico de um único arquivo — organizando o PAJ "
        "2026/029-01658, 2026-08-21 — porque o Defensor tinha aquele PDF "
        "aberto para conferência manual enquanto a compressão tentava "
        "sobrescrevê-lo). Se AINDA ASSIM dependido de retry, use janelas "
        "generosas (múltiplos segundos, não milissegundos) antes de desistir "
        "de um arquivo específico. **Se precisar reescrever o script no meio "
        "da tarefa para contornar um erro, releia a classificação já "
        "decidida (ou o que já copiou) antes de reescrever, e CONFIRME que "
        "todo arquivo já classificado (inclusive os VERIFICAR) está presente "
        "no script novo** — no episódio do PAJ 2026/029-01658 acima, ao "
        "reescrever o script para montar em diretório temporário, 3 "
        "documentos VERIFICAR já corretamente identificados na primeira "
        "passada ficaram de fora da reescrita, e o resumo final relatou "
        "27 arquivos entregues quando só 24 existiam de fato — só foi "
        "percebido porque o Defensor pediu para conferir manualmente depois. "
        "O passo 7 abaixo (contagem de arquivos) existe para pegar "
        "exatamente esse tipo de erro antes de reportar sucesso.\n\n"
        "2.5. **Verifique se há uma petição inicial já minutada em PDF** em "
        f"`{pasta_pecas_ia}\\peticao_inicial.pdf`. Se existir, copie o "
        "arquivo inteiro (`copy_file()`, sem desmembrar) para a raiz de "
        f"`{pasta_destino}\\` com o nome fixo **`00 - Peticao Inicial.pdf`** "
        "— antes de tudo, inclusive antes do documento `01` da categoria 1 "
        "(pedido do usuário, 2026-09-09: a petição já pronta deve sair "
        "dentro do próprio pacote de ajuizamento, não só em `Peças IA`). Se "
        "só existir `.docx`/`.txt` da petição (ainda sem PDF gerado), **não "
        "converta** — apenas siga sem o documento `00`; não é bloqueante. "
        "Esse arquivo `00` fica FORA da contagem/numeração sequencial "
        "`01, 02, ...` do passo 6 abaixo.\n\n"
        "3. Extraia os 3 formulários obrigatórios com "
        "`extrair_documentos_obrigatorios()`. **Se ela não achar Declaração "
        "de Insuficiência de Recursos e/ou Termo de Renúncia por busca de "
        "título, não desista e não deixe de fora**: já aconteceu de PDFs "
        "terem esses 2 cabeçalhos com fonte/glifo que extrai quebrado no "
        "texto nativo (ex.: \"DE ÃO DE | ICIÊNCIA S\" em vez de "
        "\"DECLARAÇÃO DE INSUFICIÊNCIA DE RECURSOS\"). Nesse caso, leia o "
        "texto de cada página candidata (ou renderize e olhe a imagem) para "
        "confirmar visualmente qual página é qual, e extraia manualmente com "
        "`extract_pages()`.\n\n"
        "4. Classifique cada documento restante numa das 7 categorias do "
        "Passo 3 da skill (essenciais do autor, essenciais do processo, "
        "interesse processual do(s) NB(s) pedido(s) — passo 1 acima —, "
        "médicos, miserabilidade/BPC, demais fundamentos do benefício, valor "
        "da causa) ou marque `(VERIFICAR)` na categoria 8 — nunca exclua um "
        "documento em silêncio. Documentos claramente só-internos (termo de "
        "consentimento LGPD, pesquisa socioeconômica isolada, declaração de "
        "veracidade, PRINTS DE WHATSAPP/mensagens internas da DPU) ficam de "
        "fora sem precisar virar VERIFICAR — mas cite-os no resumo final.\n\n"
        "5. **Compare hash MD5 de documentos que pareçam semelhantes** (mesmo "
        "tipo, mesmo NB, datas de extração diferentes, nomes parecidos) ANTES "
        "de decidir incluir os dois — quando dois arquivos cobrirem "
        "essencialmente a mesma informação (ex.: dois CNIS de datas "
        "próximas, duas comunicações de decisão do mesmo NB, dois "
        "comprovantes de residência), inclua o mais completo/recente/oficial "
        "na categoria correspondente e o outro em VERIFICAR como versão "
        "anterior/duplicada, explicando por quê — não inclua os dois como se "
        "fossem documentos diferentes.\n\n"
        "6. Numere sequencialmente (`01 -`, `02 -`, ...) na ordem das "
        "categorias, sem lacunas — o `00 - Peticao Inicial.pdf` do passo 2.5 "
        "(quando existir) fica fora dessa sequência, sempre antes do `01`. "
        "Todo nome de arquivo passa por `sanitizar_nome()` antes de gravar "
        "(compatibilidade com o e-proc).\n\n"
        "7. **Rode `comprimir_pasta()` e, em seguida, `verificar_duplicatas()` "
        "ao final, sem exceção.** Se `verificar_duplicatas()` acusar "
        "qualquer grupo, isso é ERRO BLOQUEANTE, não um aviso — investigue a "
        "causa (provavelmente uma extração que reaproveitou um caminho ou "
        "copiou a fonte errada) e corrija antes de finalizar. Nunca entregue "
        "a pasta com duplicatas. **Além disso, antes de escrever o resumo "
        "final, faça uma contagem simples**: liste os arquivos REALMENTE "
        "presentes na pasta final no disco (`os.listdir()`) e confira, um a "
        "um, contra a lista de categorização que você decidiu — todo "
        "documento que você classificou (numerado ou VERIFICAR) precisa "
        "estar fisicamente lá. Só escreva o resumo DEPOIS dessa conferência "
        "— o resumo deve descrever o que está no disco, nunca o que você "
        "planejou ou lembra ter copiado.\n\n"
        "8. Se o valor da causa for conhecido (leia "
        f"`{pasta_pecas_ia}\\valor_causa_result.json` se existir, ou o texto "
        "da petição), rode `checar_valor_causa_jef()` com a competência de "
        "hoje e reporte o alerta no resumo, se aplicável.\n\n"
        + _INSTRUCAO_IDIOMA
        + "Não me pergunte nada — decida autonomamente com base no exposto e "
        "sinalize qualquer incerteza como `(VERIFICAR)` no resultado, nunca "
        "como pergunta.\n\n"
        "## Ao final, apresente um RESUMO ESTRUTURADO\n\n"
        "```\n"
        "## Organização de Documentos — Ajuizamento\n\n"
        "### NB(s) pedido(s) identificado(s) na petição\n"
        "<NB — tipo de pedido (principal/subsidiário)>\n\n"
        "### Arquivos gerados (numerados, por categoria)\n"
        "00 - Peticao Inicial — <presente/ausente, e por quê>\n"
        "01 - <nome> — <categoria>\n"
        "...\n\n"
        "### Marcados (VERIFICAR) e por quê\n"
        "- <nome> — <motivo>\n\n"
        "### Documentos excluídos por serem só-internos\n"
        "- <nome> — <motivo>\n\n"
        "### Checagem de duplicatas (hash MD5)\n"
        "<resultado — deve ser 'nenhuma duplicata encontrada'>\n\n"
        "### Alerta de 60 salários-mínimos\n"
        "<se aplicável, ou 'não se aplica'>\n\n"
        "### Documentos da pasta raiz que não entraram em nenhuma categoria "
        "nem em VERIFICAR nem nos só-internos\n"
        "<deveria estar vazio — se não estiver, é bug: corrija antes de "
        "finalizar>\n"
        "```\n"
    )


def _persistir(paj_norm: str, session: ChatSession) -> None:
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return
    import datetime as _dt

    data = {
        "status": session.status,
        "log": session.summary,
        "error": session.error,
        "session_id": session.session_id,
        "atualizado_em": _dt.datetime.now().isoformat(timespec="minutes"),
    }
    with contextlib.suppress(Exception):
        _arquivo_estado(paj_norm).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    # Script auxiliar que a IA deixou na pasta do PAJ (16/10/2026).
    with contextlib.suppress(Exception):
        from services.limpeza_scripts_ia import limpar_scripts_auxiliares
        limpar_scripts_auxiliares(paj_norm)
    with contextlib.suppress(Exception):
        historico.registrar(
            paj_norm,
            "organizar_ajuizamento",
            status=session.status,
            resumo=historico.primeira_linha_util(session.summary),
        )

    # Lembrete vermelho ao concluir com sucesso — pedido do Felipe (PAJ
    # 2026/029-01658, 2026-08-21): a organização agora roda em background
    # (minutos, não mais instantânea) e sem sinal nenhum na tela dava a
    # impressão de que a pasta "Ajuizamento" mudava sozinha enquanto ele
    # mexia nela manualmente ao mesmo tempo. O lembrete é lido do disco a
    # cada carregamento da página (ver lembretes_service/lembretesApp), então
    # fica visível mesmo que ninguém tenha acompanhado o polling em tempo
    # real — diferente do toast, que some se a página não estiver aberta.
    # Best-effort: nunca deve derrubar a persistência do resultado principal.
    if session.status == "done":
        with contextlib.suppress(Exception):
            from services import lembretes_service

            lembretes_service.criar_lembrete(
                paj_norm, "Documentos organizados para ajuizamento", "vermelho"
            )


def _aguardar_conclusao(session: ChatSession, timeout: int = TIMEOUT_TOTAL) -> None:
    inicio = time.time()
    while session.is_alive() and session.status == "running" and time.time() - inicio < timeout:
        time.sleep(0.5)


def iniciar(paj_norm: str, forcar: bool = False) -> dict:
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return {"ok": False, "erro": "Pasta do PAJ não encontrada."}
    if not (SKILL_DIR / "SKILL.md").exists():
        return {"ok": False, "erro": f"Skill não encontrada em {SKILL_DIR}."}

    pasta_arquivos = pasta / "Arquivos"
    if not pasta_arquivos.exists() or not any(pasta_arquivos.iterdir()):
        return {
            "ok": False,
            "erro": "Pasta 'Arquivos' (documentos baixados do SISDPU) vazia ou "
            "inexistente para este PAJ — sincronize com anexos antes de organizar.",
        }

    existing = _sessions.get(paj_norm)
    # Não exigir existing.is_alive() — mesmo motivo de analise_pretensao_service:
    # is_alive() só vira True depois que o subprocess é aberto; um segundo
    # clique durante o pré-processamento (OCR-LLM) passaria direto e
    # spawnaria uma sessão duplicada escrevendo na mesma pasta Ajuizamento.
    if existing and existing.status == "running":
        return {"ok": False, "erro": "Já há uma organização em andamento para este PAJ."}

    pasta_ajuiz_destino = pasta / "Ajuizamento"
    if pasta_ajuiz_destino.exists() and not forcar:
        return {
            "ok": False,
            "erro": "A pasta Ajuizamento já existe para este PAJ.",
            "precisa_confirmar": True,
        }
    if existing:
        existing.stop()

    session = ChatSession(paj_norm)
    session.encerrar_ao_concluir = True
    # Marca "running" já aqui (síncrono, antes da thread) — mesmo motivo de
    # analise_pretensao_service.iniciar(): sem isto, get_status() reportava
    # "idle" durante o pré-processamento (OCR-LLM, que pode levar bem mais
    # que os 3s do intervalo de polling do frontend em PAJs com muitos
    # anexos), e o polling sincronizava com esse falso "idle".
    session.status = "running"
    session.last_action = "preparando o contexto (lendo petição inicial e anexos)…"
    _sessions[paj_norm] = session

    def _run() -> None:
        # Fallback OCR-LLM: mesmo cuidado dos demais fluxos (chat_service,
        # analise_pretensao_service) — melhora ANTES de classificar os
        # anexos cujo OCR local ficou fraco/ausente, pra a leitura
        # documento-por-documento da skill não trabalhar com texto
        # incompleto. Idempotente e best-effort; pula se a cota está
        # esgotada (a organização também falharia).
        # Documentos do PAJ conferidos ANTES da análise (decisão do Defensor,
        # 13/10/2026): baixa o que faltar e extrai o texto (OCR) do que estiver
        # sem — services/preparo_documentos_service. Best-effort.
        with contextlib.suppress(Exception):
            from services.preparo_documentos_service import preparar_documentos_paj

            session.last_action = "conferindo os documentos do PAJ (download e OCR)…"
            preparar_documentos_paj(paj_norm)
        with contextlib.suppress(Exception):
            import services.cota_service as _cota_chk

            if not _cota_chk.esta_pausado():
                from services.ocr_llm_service import melhorar_ocr_paj

                melhorar_ocr_paj(paj_norm)

        instrucao = _montar_instrucao_organizar(paj_norm, forcar)
        if not session.start_elaboracao(instrucao):
            _persistir(paj_norm, session)
            return
        _aguardar_conclusao(session)
        _persistir(paj_norm, session)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}
