"""Rotas do botão "Ver qualificação" (seção "Assistidos e Representantes
Legais" da página do PAJ) — ver services/qualificacao_service.py."""

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from services import qualificacao_service
from services.paj_service import PajNorm

router = APIRouter()


@router.get("/api/qualificacao/{paj_norm}")
async def obter(paj_norm: PajNorm):
    """qualificacao.json do PAJ; se ainda não existe, monta pelo SIS-DPU 2.0."""
    q = qualificacao_service.obter(paj_norm)
    if q is None:
        try:
            q = await qualificacao_service.atualizar_do_sis2(paj_norm)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "erro": f"{e}"[:400], **_listas()})
    return {"ok": True, "qualificacao": q, **_listas()}


@router.post("/api/qualificacao/{paj_norm}/atualizar")
async def atualizar(paj_norm: PajNorm):
    """Relê o cadastro do SIS-DPU 2.0 (campos editados pelo Defensor ficam)."""
    try:
        q = await qualificacao_service.atualizar_do_sis2(paj_norm)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": f"{e}"[:400]})
    return {"ok": True, "qualificacao": q}


@router.post("/api/qualificacao/{paj_norm}/completar-documentos")
async def completar_documentos(paj_norm: PajNorm):
    """Fase 2: a IA preenche só as lacunas com dados dos documentos do PAJ,
    citando arquivo e trecho de cada valor."""
    try:
        r = await qualificacao_service.completar_pelos_documentos(paj_norm)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": f"{e}"[:400]})
    return {"ok": True, **r}


@router.post("/api/qualificacao/{paj_norm}/previa")
async def previa(paj_norm: PajNorm, q: dict = Body(...)):
    """Representação e texto recalculados, SEM gravar (modal ao editar)."""
    return {"ok": True, "qualificacao": qualificacao_service._completar(q)}


@router.put("/api/qualificacao/{paj_norm}")
async def salvar(paj_norm: PajNorm, q: dict = Body(...)):
    return {"ok": True, "qualificacao": qualificacao_service.salvar(paj_norm, q)}


def _listas() -> dict:
    return {"vinculos": qualificacao_service.VINCULOS,
            "eproc_autor": sorted(set(qualificacao_service.EPROC_AUTOR.values())),
            "eproc_rep": qualificacao_service.EPROC_REP_OPCOES}
