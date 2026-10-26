"""Cofre UNIFICADO de credenciais protegido por uma única SENHA-MESTRA.

Generaliza o modelo do `credenciais_eproc_service` (que era escopado só ao e-proc)
para guardar N sistemas — SISDPU, e-proc e quaisquer sistemas externos futuros —
todos cifrados no MESMO arquivo, sob a MESMA senha-mestra. Desbloqueia uma vez →
todos os sistemas com credencial ficam disponíveis (favorece a reconexão rápida).

Modelo:
- Para cada sistema, usuário+senha ficam CIFRADOS em disco (dados/_cofre.enc).
- A chave é derivada de uma SENHA-MESTRA digitada pelo usuário — a senha-mestra
  NÃO é gravada em lugar nenhum.
- Após o desbloqueio, as credenciais ficam só na MEMÓRIA do processo; ao reiniciar
  o painel, é preciso desbloquear de novo.
- NÃO tranca a navegação do painel: a senha-mestra só é pedida quando o usuário
  aciona algo que realmente usa um sistema externo (modal sob demanda).

Cripto: Fernet (AES-128-CBC + HMAC-SHA256) com chave derivada por PBKDF2-HMAC-SHA256
(salt aleatório por arquivo, 240k iterações). Biblioteca `cryptography`.

Formato do arquivo (JSON de uma linha):
    {"v": 1, "salt": "<b64>", "token": "<fernet>"}
onde o plaintext cifrado é:
    {"sistemas": {"sisdpu": {"usuario": "...", "senha": "..."}, "eproc": {...}}}

Nota: enquanto BLOQUEADO, sabe-se apenas que o cofre EXISTE — não quais sistemas
estão dentro (a lista de sistemas vive dentro do blob cifrado). Após desbloquear,
`sistemas_configurados()` passa a listar.
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
CRED_FILE = DADOS_DIR / "_cofre.enc"

_ITERACOES = 240_000  # custo do PBKDF2 (dificulta força bruta da senha-mestra)

# Sistemas conhecidos (rótulos para exibição). Novos sistemas podem ser
# adicionados aqui — o cofre em si é agnóstico e aceita qualquer chave.
SISTEMAS = {
    "sisdpu": "SISDPU",
    "eproc": "e-proc",
    "sat": "SAT Central (GERID)",
}


class SenhaMestraIncorreta(Exception):
    """A senha-mestra não decifra o cofre existente."""


# Estado em MEMÓRIA da sessão atual (nunca persistido).
# sistemas = { "<sistema>": {"usuario": "...", "senha": "..."} }
_estado: dict = {"destrancado": False, "sistemas": {}}


def _derivar_chave(senha_mestra: str, salt: bytes) -> bytes:
    """Deriva a chave Fernet (32 bytes url-safe base64) a partir da senha-mestra."""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_ITERACOES)
    return base64.urlsafe_b64encode(kdf.derive(senha_mestra.encode("utf-8")))


def _ler_cofre(senha_mestra: str) -> dict:
    """Lê e decifra o cofre em disco com a senha-mestra dada. Retorna o dict de
    sistemas ({} se o arquivo não existe). Levanta SenhaMestraIncorreta se a
    senha-mestra não confere / o arquivo está corrompido."""
    if not CRED_FILE.exists():
        return {}
    try:
        d = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        salt = base64.b64decode(d["salt"])
        bruto = Fernet(_derivar_chave(senha_mestra or "", salt)).decrypt(d["token"].encode("utf-8"))
        dados = json.loads(bruto.decode("utf-8"))
    except (InvalidToken, KeyError, ValueError, json.JSONDecodeError) as e:
        raise SenhaMestraIncorreta() from e
    except Exception as e:  # noqa: BLE001 — qualquer falha = senha errada / corrompido
        raise SenhaMestraIncorreta() from e
    sistemas = dados.get("sistemas", {})
    return sistemas if isinstance(sistemas, dict) else {}


def _gravar_cofre(sistemas: dict, senha_mestra: str) -> None:
    """Cifra o dict de sistemas com a senha-mestra e grava em disco (novo salt)."""
    DADOS_DIR.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(16)
    token = Fernet(_derivar_chave(senha_mestra, salt)).encrypt(
        json.dumps({"sistemas": sistemas}).encode("utf-8"))
    CRED_FILE.write_text(
        json.dumps({"v": 1, "salt": base64.b64encode(salt).decode(), "token": token.decode()}),
        encoding="utf-8")


# --- Consulta de estado -----------------------------------------------------
def configurado() -> bool:
    """True se já existe um cofre cifrado em disco (independe de estar destrancado)."""
    return CRED_FILE.exists()


def destrancado() -> bool:
    """True se o cofre já foi decifrado nesta sessão (credenciais em memória)."""
    return bool(_estado["destrancado"])


def sistemas_configurados() -> list[str]:
    """Lista os sistemas com credencial no cofre. Só conhecido quando destrancado."""
    if not _estado["destrancado"]:
        return []
    return sorted(_estado["sistemas"].keys())


def tem_credencial(sistema: str) -> bool:
    """True se o cofre (destrancado) tem credencial para o sistema dado."""
    return bool(_estado["destrancado"] and _estado["sistemas"].get(sistema))


# --- Desbloqueio / bloqueio -------------------------------------------------
def destrancar(senha_mestra: str) -> bool:
    """Decifra o cofre com a senha-mestra e carrega tudo em memória. Retorna True
    se acertou; False se a senha-mestra estiver errada (ou o arquivo inválido)."""
    if not CRED_FILE.exists():
        return False
    try:
        sistemas = _ler_cofre(senha_mestra)
    except SenhaMestraIncorreta:
        return False
    _estado.update(destrancado=True, sistemas=sistemas)
    return True


def bloquear() -> None:
    """Esquece as credenciais da memória (tranca de novo). O cofre em disco fica."""
    _estado.update(destrancado=False, sistemas={})


# --- Cadastro / remoção de credenciais --------------------------------------
def salvar_sistema(sistema: str, usuario: str, senha: str, senha_mestra: str) -> None:
    """Adiciona/atualiza as credenciais de UM sistema no cofre unificado, mantendo
    os demais sistemas intactos. Deixa a sessão destrancada.

    Se o cofre já existe, a senha-mestra fornecida DEVE conferir com ele (senão
    levanta SenhaMestraIncorreta) — assim garantimos uma única senha-mestra para
    todos os sistemas. Se não existe, este vira o cofre inicial.
    """
    sistema = (sistema or "").strip().lower()
    usuario = (usuario or "").strip()
    senha = senha or ""
    if not sistema:
        raise ValueError("Sistema é obrigatório.")
    if not usuario or not senha or not (senha_mestra or ""):
        raise ValueError("Usuário, senha e senha-mestra são obrigatórios.")

    # Carrega os sistemas existentes (valida a senha-mestra contra o cofre atual).
    if CRED_FILE.exists():
        sistemas = _ler_cofre(senha_mestra)  # levanta SenhaMestraIncorreta se não confere
    elif _estado["destrancado"]:
        sistemas = dict(_estado["sistemas"])
    else:
        sistemas = {}

    sistemas[sistema] = {"usuario": usuario, "senha": senha}
    _gravar_cofre(sistemas, senha_mestra)
    _estado.update(destrancado=True, sistemas=sistemas)
    if sistema == "eproc":
        _sincronizar_gerenciador_eproc(usuario, senha)


def _sincronizar_gerenciador_eproc(usuario: str, senha: str) -> None:
    """Credenciais do e-proc salvas no cofre também vão para o Gerenciador de
    Credenciais do Windows, de onde o MCP eproc-trf4 (e o login do e-proc pelo
    navegador) as lê — chave `credential_key` do config.json do MCP. Sem isso,
    trocar a senha só no cofre deixava o MCP tentando a antiga (26/10/2026).
    A chave do 2FA (TOTP) não muda aqui."""
    try:
        import keyring

        from ingestao.eproc_mcp_client import MCP_DIR
        chave = "mcp-eproc"
        try:
            chave = json.loads((MCP_DIR / "config.json").read_text(encoding="utf-8")).get("credential_key") or chave
        except Exception:  # noqa: BLE001
            pass
        keyring.set_password(chave, "usuario", usuario)
        keyring.set_password(chave, "senha", senha)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("cofre").warning("não atualizou o Gerenciador de Credenciais do e-proc: %s", e)


def remover_sistema(sistema: str, senha_mestra: str) -> bool:
    """Remove as credenciais de UM sistema do cofre (mantém os demais). Retorna
    True se removeu; levanta SenhaMestraIncorreta se a senha-mestra não confere.
    Se após a remoção o cofre ficar vazio, apaga o arquivo."""
    sistema = (sistema or "").strip().lower()
    if not CRED_FILE.exists():
        return False
    sistemas = _ler_cofre(senha_mestra)  # valida senha-mestra
    if sistema not in sistemas:
        return False
    del sistemas[sistema]
    if sistemas:
        _gravar_cofre(sistemas, senha_mestra)
    else:
        # Cofre esvaziou — apaga o arquivo (volta a exigir configuração).
        try:
            CRED_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    if _estado["destrancado"]:
        _estado["sistemas"].pop(sistema, None)
    return True


def trocar_senha_mestra(senha_atual: str, senha_nova: str) -> None:
    """Re-cifra o cofre inteiro com uma nova senha-mestra. Levanta
    SenhaMestraIncorreta se `senha_atual` não confere."""
    if not (senha_nova or ""):
        raise ValueError("A nova senha-mestra é obrigatória.")
    sistemas = _ler_cofre(senha_atual)  # valida a atual
    _gravar_cofre(sistemas, senha_nova)
    _estado.update(destrancado=True, sistemas=sistemas)


def remover_tudo() -> None:
    """Bloqueia e APAGA o cofre inteiro em disco (todos os sistemas)."""
    bloquear()
    try:
        CRED_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# --- Uso pelas rotinas de acesso --------------------------------------------
def get_credenciais(sistema: str) -> tuple[str, str] | None:
    """(usuario, senha) do sistema se o cofre está destrancado e tem a credencial;
    None caso contrário."""
    sistema = (sistema or "").strip().lower()
    if not _estado["destrancado"]:
        return None
    cred = _estado["sistemas"].get(sistema)
    if cred and cred.get("usuario") and cred.get("senha"):
        return cred["usuario"], cred["senha"]
    return None


def get_usuario(sistema: str) -> str:
    """Usuário do sistema carregado (só quando destrancado) — para exibição."""
    cred = get_credenciais(sistema)
    return cred[0] if cred else ""
