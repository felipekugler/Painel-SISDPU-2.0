"""Estado da conexão do painel com o SISDPU (para o selo de status + Reconectar
na barra superior).

Espelha o mecanismo do PAINEL COMUNICAÇÕES, porém SIMPLIFICADO: o PAINEL SISDPU
faz login AUTOMÁTICO por credenciais do `.env` (SISDPU_USERNAME/PASSWORD), então
não há cofre/senha-mestra. O estado é REATIVO: as rotinas de acesso ao SISDPU
(sisdpu_movtram / sisdpu_client) chamam `marcar_conflito`, `marcar_conectado` ou
`marcar_erro` conforme detectam o diálogo de conflito de sessão, um login bem-
sucedido, ou uma falha. O selo lê este estado via GET /api/seguranca/status.
"""

from __future__ import annotations

import os
from datetime import datetime

# conflito: diálogo de conflito de sessão do SISDPU detectado e não resolvido.
# erro: última falha de login/conexão (ex.: credenciais recusadas, .env ausente).
# conectado: houve um acesso bem-sucedido nesta execução (sinal positivo).
_estado: dict = {
    "conflito": False,
    "detalhe": "",
    "erro": "",
    "conectado": False,
    "atualizado_em": "",
}


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def marcar_conflito(detalhe: str = "") -> None:
    _estado["conflito"] = True
    _estado["detalhe"] = detalhe or "Conflito de sessão do SISDPU (login em outro lugar)."
    _estado["conectado"] = False
    _estado["atualizado_em"] = _agora()


def marcar_conectado() -> None:
    _estado["conflito"] = False
    _estado["detalhe"] = ""
    _estado["erro"] = ""
    _estado["conectado"] = True
    _estado["atualizado_em"] = _agora()


def marcar_erro(detalhe: str = "") -> None:
    _estado["erro"] = detalhe or "Falha ao acessar o SISDPU."
    _estado["conectado"] = False
    _estado["atualizado_em"] = _agora()


def env_tem_credenciais() -> bool:
    return bool(os.environ.get("SISDPU_USERNAME") and os.environ.get("SISDPU_PASSWORD"))


def cofre_bloqueado() -> bool:
    """True se há um cofre configurado mas ainda bloqueado (sem credenciais do
    SISDPU em memória e sem fallback do .env). Nesse caso o selo deve indicar que
    é preciso desbloquear antes de usar."""
    from services import cofre_service
    if env_tem_credenciais():
        return False
    return bool(cofre_service.configurado() and not cofre_service.tem_credencial("sisdpu"))


def credenciais_disponiveis() -> bool:
    """True se há credenciais do SISDPU prontas para uso — do cofre (destrancado)
    ou do .env (legado)."""
    from services import cofre_service
    return bool(cofre_service.get_credenciais("sisdpu")) or env_tem_credenciais()


def status() -> dict:
    """Snapshot para o selo de status. `configurado` = há credenciais prontas para
    uso (cofre destrancado com SISDPU ou .env). `bloqueado` = cofre existe mas
    ainda não foi desbloqueado."""
    return {
        "ok": True,
        "configurado": credenciais_disponiveis(),
        "bloqueado": cofre_bloqueado(),
        "conflito": bool(_estado["conflito"]),
        "conflito_detalhe": _estado["detalhe"],
        "conectado": bool(_estado["conectado"]),
        "erro": _estado["erro"],
        "atualizado_em": _estado["atualizado_em"],
    }
