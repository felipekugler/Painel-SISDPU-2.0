"""Foto do assistido pelo SIS-DPU 2.0 (22/10/2026).

Com a fonte SIS-DPU 2.0 (MCP), a foto da página do PAJ deixou de vir (ela era
raspada da tela do SISDPU legado). O MCP passou a entregar a foto cadastrada
em `assistido_ficha`, num bloco de imagem à parte. Aqui:

  - `garantir_foto(pasta, det)`: chamado pela sincronização do PAJ — se a pasta
    ainda não tem `foto_assistido.jpg`, busca a ficha do assistido PRINCIPAL e
    grava a foto. UMA consulta por PAJ: o resultado fica em `foto_sis2.json`
    (com ou sem foto) e não se repete a cada sincronização. Cada leitura da
    ficha fica na trilha do SIS 2 e conta no teto diário.
  - Sem foto cadastrada o SIS 2 manda um AVATAR GENÉRICO (desenho sem rosto),
    o mesmo para pessoas diferentes: é descartado (fica a inicial do nome).
    Além dos avatares conhecidos, uma imagem idêntica vista para duas pessoas
    diferentes passa a contar como avatar (`dados/_fotos_sis2_padrao.json`).
  - `ressincronizar_caixa()`: passada única nos PAJs da caixa sem foto.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import datetime
from pathlib import Path

from config import PAJS_DIR
from services.gravacao_protegida import escrever_texto_com_retry_sync

ARQ_FOTO = "foto_assistido.jpg"
ARQ_MARCA = "foto_sis2.json"
_ARQ_PADRAO = Path(__file__).resolve().parents[1].parent / "dados" / "_fotos_sis2_padrao.json"

# Avatar genérico do SIS 2 (masculino), visto em 22/10/2026.
AVATARES_CONHECIDOS = {"ad68e81cf52d60722c37af35bed8895b632c2ba516fbb7403fbde20764168f5f"}


def _ler(path: Path, padrao):
    with contextlib.suppress(Exception):
        return json.loads(path.read_text(encoding="utf-8"))
    return padrao


def _padroes() -> dict:
    """{"avatares": [sha], "vistos": {sha: [idPessoa, ...]}}"""
    d = _ler(_ARQ_PADRAO, {})
    d.setdefault("avatares", [])
    d.setdefault("vistos", {})
    return d


def _eh_avatar(sha: str, id_pessoa: int, padroes: dict) -> bool:
    if sha in AVATARES_CONHECIDOS or sha in padroes["avatares"]:
        return True
    pessoas = set(padroes["vistos"].get(sha, []))
    pessoas.add(int(id_pessoa))
    padroes["vistos"][sha] = sorted(pessoas)
    if len(pessoas) >= 2:  # a mesma imagem para duas pessoas: é desenho padrão
        padroes["avatares"].append(sha)
        return True
    return False


def _id_pessoa_principal(det: dict) -> int | None:
    pf = (det or {}).get("assistidos_pf") or []
    principal = next((a for a in pf if a.get("principal")), pf[0] if pf else None)
    with contextlib.suppress(Exception):
        return int((principal or {}).get("idPessoa"))
    return None


def _desfazer_avatares(padroes: dict) -> None:
    """Um avatar recém-descoberto pode já ter sido gravado como foto em outro
    PAJ (quando foi visto só uma vez) — apaga essas cópias."""
    avatares = set(padroes["avatares"]) | AVATARES_CONHECIDOS
    for marca in PAJS_DIR.glob(f"*/{ARQ_MARCA}"):
        m = _ler(marca, {})
        if m.get("sha256") in avatares and m.get("tem_foto"):
            with contextlib.suppress(Exception):
                (marca.parent / ARQ_FOTO).unlink()
            m.update(tem_foto=False, avatar=True)
            marca.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
            meta_path = marca.parent / "metadata.json"
            meta = _ler(meta_path, None)
            if isinstance(meta, dict) and meta.pop("foto_assistido", None):
                escrever_texto_com_retry_sync(meta_path, json.dumps(meta, ensure_ascii=False, indent=2, default=str))


def garantir_foto(pasta: Path, det: dict, forcar: bool = False) -> dict:
    """Grava a foto do assistido principal se a pasta ainda não tem uma.
    `forcar`: ignora a marca de "já consultado" (não a foto existente)."""
    foto = pasta / ARQ_FOTO
    if foto.exists():
        return {"ok": True, "situacao": "ja_tinha"}
    marca = pasta / ARQ_MARCA
    if marca.exists() and not forcar:
        return {"ok": True, "situacao": "ja_consultado"}
    from ingestao import sisdpu2_fonte, sisdpu2_mcp_client as sis2

    if not (sisdpu2_fonte.fonte_mcp_ativa() and sis2.conectado()):
        return {"ok": False, "situacao": "sem_mcp"}
    id_pessoa = _id_pessoa_principal(det)
    if not id_pessoa:
        return {"ok": False, "situacao": "sem_assistido"}
    _valor, imagens = sis2.chamar_com_imagens("assistido_ficha", {"idPessoa": id_pessoa})
    padroes = _padroes()
    reg = {"id_pessoa": id_pessoa, "verificado_em": datetime.now().isoformat(timespec="seconds"),
           "tem_foto": False, "avatar": False, "sha256": ""}
    situacao = "sem_foto"
    if imagens:
        dados = imagens[0]
        sha = hashlib.sha256(dados).hexdigest()
        reg["sha256"] = sha
        if _eh_avatar(sha, id_pessoa, padroes):
            reg["avatar"] = True
            situacao = "avatar"
        else:
            foto.write_bytes(dados)
            reg["tem_foto"] = True
            situacao = "foto"
    marca.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
    novos_avatares = len(padroes["avatares"])
    _ARQ_PADRAO.parent.mkdir(parents=True, exist_ok=True)
    antes = len(_padroes()["avatares"])
    _ARQ_PADRAO.write_text(json.dumps(padroes, ensure_ascii=False), encoding="utf-8")
    if novos_avatares > antes:
        _desfazer_avatares(padroes)
    return {"ok": True, "situacao": situacao}


def ressincronizar_caixa(log=print) -> dict:
    """Passada única só de fotos nos PAJs da caixa atual (em_caixa_atual) que
    ainda não têm foto — ignora a marca de "já consultado"."""
    from services import paj_service

    contagem: dict[str, int] = {}
    for p in paj_service.listar_pajs():
        pasta = PAJS_DIR / p["paj_norm"]
        meta = _ler(pasta / "metadata.json", {})
        try:
            r = garantir_foto(pasta, meta.get("sisdpu_raw") or {}, forcar=True)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "situacao": f"erro: {type(e).__name__}: {e}"[:120]}
        situacao = r["situacao"] if r.get("ok") or not r["situacao"].startswith("erro") else "erro"
        contagem[situacao] = contagem.get(situacao, 0) + 1
        if situacao == "foto" and not meta.get("foto_assistido"):
            meta["foto_assistido"] = ARQ_FOTO
            escrever_texto_com_retry_sync(pasta / "metadata.json",
                                          json.dumps(meta, ensure_ascii=False, indent=2, default=str))
        log(f"{p['paj']}: {r['situacao']}")
    return contagem
