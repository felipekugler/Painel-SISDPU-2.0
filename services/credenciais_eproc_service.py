"""Credenciais do e-proc protegidas por SENHA-MESTRA (cofre cifrado).

Espelha o modelo do PAINEL COMUNICAÇÕES (`credenciais_service`), mas ESCOPADO à
aba "Prazos e-proc" — NÃO tranca o resto do painel SISDPU.

Modelo:
- usuário+senha do e-proc ficam CIFRADOS em disco (dados/_eproc_credenciais.enc).
- A chave é derivada de uma SENHA-MESTRA digitada pelo usuário — a senha-mestra
  NÃO é gravada em lugar nenhum.
- Após o desbloqueio, as credenciais ficam só na MEMÓRIA do processo; ao reiniciar
  o painel, é preciso desbloquear de novo.

Cripto: Fernet (AES-128-CBC + HMAC-SHA256) com chave derivada por PBKDF2-HMAC-SHA256
(salt aleatório por arquivo). Biblioteca `cryptography`.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

BASE_DIR = Path(__file__).resolve().parents[1]  # .../PAINEL SISDPU/Projeto
DADOS_DIR = BASE_DIR.parent / "dados"
CRED_FILE = DADOS_DIR / "_eproc_credenciais.enc"

_ITERACOES = 240_000  # custo do PBKDF2 (dificulta força bruta da senha-mestra)

# Estado em MEMÓRIA da sessão atual (nunca persistido).
_estado: dict = {"destrancado": False, "usuario": "", "senha": ""}


def _derivar_chave(senha_mestra: str, salt: bytes) -> bytes:
    """Deriva a chave Fernet (32 bytes url-safe base64) a partir da senha-mestra."""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_ITERACOES)
    return base64.urlsafe_b64encode(kdf.derive(senha_mestra.encode("utf-8")))


def configurado() -> bool:
    """True se já existe um cofre de credenciais do e-proc cifrado em disco."""
    return CRED_FILE.exists()


def destrancado() -> bool:
    """True se as credenciais já foram decifradas nesta sessão (estão em memória)."""
    return bool(_estado["destrancado"])


def salvar(usuario: str, senha: str, senha_mestra: str) -> None:
    """Cifra usuario+senha do e-proc com a senha-mestra e grava em disco. Também
    deixa a sessão já DESTRANCADA (as credenciais entram na memória)."""
    usuario = (usuario or "").strip()
    senha = senha or ""
    if not usuario or not senha or not (senha_mestra or ""):
        raise ValueError("Usuário, senha do e-proc e senha-mestra são obrigatórios.")
    DADOS_DIR.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(16)
    token = Fernet(_derivar_chave(senha_mestra, salt)).encrypt(
        json.dumps({"usuario": usuario, "senha": senha}).encode("utf-8"))
    CRED_FILE.write_text(
        json.dumps({"v": 1, "salt": base64.b64encode(salt).decode(), "token": token.decode()}),
        encoding="utf-8")
    _estado.update(destrancado=True, usuario=usuario, senha=senha)


def destrancar(senha_mestra: str) -> bool:
    """Tenta decifrar o cofre com a senha-mestra. Carrega em memória e retorna True
    se acertou; False se a senha-mestra estiver errada (ou o arquivo inválido)."""
    if not CRED_FILE.exists():
        return False
    try:
        d = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        salt = base64.b64decode(d["salt"])
        bruto = Fernet(_derivar_chave(senha_mestra or "", salt)).decrypt(d["token"].encode("utf-8"))
        dados = json.loads(bruto.decode("utf-8"))
    except (InvalidToken, KeyError, ValueError, json.JSONDecodeError):
        return False
    except Exception:  # noqa: BLE001 — qualquer falha = senha errada / arquivo corrompido
        return False
    _estado.update(destrancado=True, usuario=dados.get("usuario", ""), senha=dados.get("senha", ""))
    return True


def get_credenciais() -> tuple[str, str] | None:
    """(usuario, senha) do e-proc se destrancado; None caso contrário."""
    if _estado["destrancado"] and _estado["usuario"] and _estado["senha"]:
        return _estado["usuario"], _estado["senha"]
    return None


def get_usuario() -> str:
    """Usuário do e-proc carregado (só quando destrancado) — para exibição."""
    return _estado["usuario"] if _estado["destrancado"] else ""


def bloquear() -> None:
    """Esquece as credenciais da memória (tranca de novo). O cofre em disco fica."""
    _estado.update(destrancado=False, usuario="", senha="")


def remover() -> None:
    """Bloqueia e APAGA o cofre em disco (volta a exigir configuração)."""
    bloquear()
    try:
        CRED_FILE.unlink(missing_ok=True)
    except OSError:
        pass
