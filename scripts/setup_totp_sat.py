"""Setup interativo do 2FA automático do SAT Central (GERID/CAS).

Salva a seed TOTP base32 do GERID no Windows Credential Manager (chave
"sat-central", item "totp_seed") e valida gerando um código pra você
comparar com o app autenticador. NÃO precisa senha/CPF do GERID aqui —
login continua manual na janela; só o passo do código 2FA passa a ser
automático (`ingestao.sat_client._preencher_2fa`).

Pode ser rodado remotamente via Claude Code — é totalmente CLI, sem GUI.
A seed nunca aparece em texto (input mascarado via getpass) nem é
registrada em nenhum log/arquivo — só vai pro Credential Manager.

Uso:
    .venv\\Scripts\\python.exe scripts\\setup_totp_sat.py

Pré-requisito: já ter a "chave manual" (seed base32) do 2FA do GERID
cadastrado no app autenticador (ex.: extensão Chrome "Authenticator",
linha "GERID") — normalmente mostrada junto do QR code ao (re)cadastrar
o dispositivo em Configurações -> Autenticação em 2 fatores no GERID.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ingestao import sat_client  # noqa: E402


def _input_secreto(prompt: str) -> str:
    while True:
        val = getpass.getpass(prompt).strip()
        if val:
            return val
        print("  Valor vazio, tente novamente.")


def main() -> int:
    print("=" * 70)
    print("Setup do 2FA automático — SAT Central (GERID/CAS)")
    print("=" * 70)

    print("\n[1/3] Seed TOTP (chave manual base32) do GERID")
    print("  Como obter (se ainda não tiver a chave à mão):")
    print("  1. Acesse o GERID e vá em Configurações -> Autenticação em 2 fatores.")
    print("  2. Recadastre o dispositivo. No QR code mostrado, procure o link")
    print("     'chave manual' (ou similar) e copie a string.")
    print("  3. (Re)cadastre a MESMA chave no app autenticador que você já usa")
    print("     (ex.: extensão Chrome 'Authenticator', linha 'GERID').")
    print("  Formato esperado: A-Z e 2-7 (espaços/hífens opcionais, removidos).")

    seed_raw = _input_secreto("  Seed TOTP do GERID: ")
    try:
        seed_norm = sat_client.validar_seed_totp(seed_raw)
    except ValueError as e:
        print(f"  ERRO: {e}")
        return 2

    print("\n[2/3] Salvando no Credential Manager")
    sat_client.salvar_seed_totp(seed_norm)
    print("  OK - seed salva (chave 'sat-central', item 'totp_seed').")

    print("\n[3/3] Validação cruzada")
    codigo = sat_client._gerar_codigo_totp()
    print(f"  Código TOTP atual gerado: {codigo}")
    print("  Compare com o código mostrado no seu app autenticador (linha GERID).")
    confirmacao = input("  Os códigos batem? [s/N]: ").strip().lower()
    if confirmacao != "s":
        print("  A seed foi salva mesmo assim, mas parece ERRADA — refaça o setup")
        print("  com a chave manual correta, ou apague com:")
        print('    python -c "import keyring; keyring.delete_password(\'sat-central\', \'totp_seed\')"')
        print("  (Seed errada ou relógio do Windows dessincronizado — `w32tm /resync` pode ajudar)")
        return 3

    print("\n" + "=" * 70)
    print("Setup concluído. O login no SAT preencherá o código 2FA sozinho")
    print("quando a tela aparecer — login (CPF/senha) e CAPTCHA continuam manuais.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
