#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Utilitários da skill SAT-DownloadArquivos.

Funções:
- sanitize(nome): remove acentos, () [] e caracteres especiais; preserva pontos
  (NB) e traços (datas); colapsa espaços.
- abreviar(texto): substitui espécie/serviço completo pelas siglas (BPC, BPC
  DEFICIENTE, BPC IDOSO, BI TEMPORARIO, etc.), tolerante a acentos.
- nome_final(base): abreviar + sanitize.
- extrair_campos_procadm(pdf_path): {prot, nb, der, paginas, subtarefas[]}.

CLI:
  python sat_utils.py pat <pasta_temp> <pasta_destino> <mapa_json>
    mapa_json: { "arquivo_temp.pdf": {"servico": "...", "protocolo": "<prot da linha>"} }
    Deduplica por (PROTOCOLO DE REQUERIMENTO interno + nº de páginas), mantendo a
    tarefa PRINCIPAL (linha cujo protocolo == prot interno), aplica nome PROCADM,
    sanitiza/abrevia e MOVE os únicos para a pasta destino. Imprime relatório.

  python sat_utils.py nome "<texto>"      -> testa abreviar+sanitize
  python sat_utils.py campos <pdf>        -> imprime campos PROCADM de um PDF
"""
import sys, os, re, json, shutil, unicodedata

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# ---- Abreviação de espécie / serviço (ordem: mais específico primeiro) ----
_ABREV = [
    (r'BENEFICIO DE PRESTACAO CONTINUADA A PESSOA COM DEFICIENCIA', 'BPC DEFICIENTE'),
    (r'BENEFICIO DE PRESTACAO CONTINUADA (?:AO IDOSO|A PESSOA IDOSA)', 'BPC IDOSO'),
    (r'BENEFICIO DE PRESTACAO CONTINUADA', 'BPC'),
    (r'BENEFICIO ASSISTENCIAL A PESSOA COM DEFICIENCIA', 'BPC DEFICIENTE'),
    (r'BENEFICIO ASSISTENCIAL AO IDOSO', 'BPC IDOSO'),
    (r'BENEFICIO ASSISTENCIAL', 'BPC'),
    (r'AUXILIO POR INCAPACIDADE TEMPORARIA ACIDENTARIO', 'BI TEMPORARIO ACIDENTARIO'),
    (r'AUXILIO POR INCAPACIDADE TEMPORARIA PREVIDENCIARIO', 'BI TEMPORARIO'),
    (r'APOSENTADORIA POR INCAPACIDADE PERMANENTE', 'APOSENTADORIA POR INCAPACIDADE'),
]


def _noacc(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s)
                   if unicodedata.category(c) != 'Mn')


def abreviar(texto):
    """Aplica as abreviações de espécie/serviço (sobre texto sem acento)."""
    out = _noacc(texto)
    for pat, ab in _ABREV:
        out = re.sub(pat, ab, out, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', out).strip()


def sanitize(base):
    """Sem acentos, sem () [], sem caracteres especiais; mantém pontos e traços."""
    n = _noacc(base)
    n = n.replace('(', '').replace(')', '').replace('[', '').replace(']', '')
    n = re.sub(r'[^\w\s\-.]', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def nome_final(base):
    """Nome pronto para arquivo (sem extensão): abreviar + sanitize."""
    return sanitize(abreviar(base))


# ---- Extração de campos do PDF de processo (GET - Gerenciador de Tarefas) ----
def _fmt_nb(raw):
    return f'{raw[0:3]}.{raw[3:6]}.{raw[6:9]}-{raw[9]}'


def _achar_nb(t):
    # rótulo NB -> valor pontuado (10) ou cru (10); (?!\d) evita o CPF (11 díg.)
    m = re.search(r'\bNB\b\s*[:\-]?\s*\n?\s*(\d{3}\.\d{3}\.\d{3}-\d|\d{10})(?!\d)', t, re.I)
    if m:
        v = m.group(1)
        return v if '.' in v else _fmt_nb(v)
    m = re.search(r'(\d{3}\.\d{3}\.\d{3}-\d)(?!\d)', t)  # fallback pontuado, não-CPF
    return m.group(1) if m else ''


def extrair_campos_procadm(pdf_path):
    if fitz is None:
        raise RuntimeError('PyMuPDF (fitz) não instalado.')
    d = fitz.open(pdf_path)
    paginas = d.page_count
    full = '\n'.join(d.load_page(i).get_text() for i in range(paginas))
    d.close()
    pm = re.search(r'PROTOCOLO\s+DE\s+REQUERIMENTO\s*[\n\s]*(\d{6,})', full, re.I | re.M)
    prot = pm.group(1) if pm else ''
    nb = _achar_nb(full)
    dm = re.search(r'(?:Data\s+de\s+entrada|Entrada)[:\s]+(\d{2}/\d{2}/\d{4})', full, re.I)
    der = '-'.join(dm.group(1).split('/')) if dm else ''
    subs = re.findall(r'(\d{6,})\s*-\s*[^\n]*\(Subtarefa\)', full)
    return {'prot': prot, 'nb': nb, 'der': der, 'paginas': paginas,
            'subtarefas': list(dict.fromkeys(subs))}


def _nome_procadm(servico, campos, prefixo=''):
    nb, der, prot = campos['nb'], campos['der'], campos['prot']
    if servico and nb and der and prot:
        base = f'PROCADM {servico} - NB {nb} DER {der} PROT {prot}'
    elif servico and der and prot:
        base = f'PROCADM {servico} - DER {der} PROT {prot}'
    else:
        base = f'PROCADM {servico} - PROT {prot}'
    return nome_final(prefixo + base) + '.pdf'


def processar_pat(pasta_temp, pasta_dest, mapa, prefixo=''):
    """mapa: {arquivo_temp: {'servico':..., 'protocolo': <prot da linha>}}. `prefixo`
    é anteposto ao nome final (ex.: 'INSTITUIDOR - ')."""
    info = {}
    for f in mapa:
        p = os.path.join(pasta_temp, f)
        if os.path.exists(p):
            info[f] = extrair_campos_procadm(p)

    # agrupar por (prot interno, paginas)
    grupos = {}
    for f, c in info.items():
        grupos.setdefault((c['prot'], c['paginas']), []).append(f)

    mantidos, descartados = [], []
    for chave, arquivos in grupos.items():
        prot_int = chave[0]
        # principal = linha cujo protocolo == prot interno; senão o primeiro
        principal = next((a for a in arquivos
                          if str(mapa[a].get('protocolo', '')) == prot_int), arquivos[0])
        servico = mapa[principal].get('servico', '')
        nome = _nome_procadm(servico, info[principal], prefixo)
        shutil.move(os.path.join(pasta_temp, principal), os.path.join(pasta_dest, nome))
        mantidos.append((principal, nome))
        for a in arquivos:
            if a != principal:
                os.remove(os.path.join(pasta_temp, a))
                descartados.append((a, f'protocolo {mapa[a].get("protocolo","?")} '
                                       f'descartado — é subtarefa/duplicata do protocolo '
                                       f'principal {prot_int} ({servico})'))
    return mantidos, descartados


def _cli():
    if len(sys.argv) < 2:
        print(__doc__); return
    cmd = sys.argv[1]
    if cmd == 'nome':
        print(nome_final(sys.argv[2]))
    elif cmd == 'campos':
        print(json.dumps(extrair_campos_procadm(sys.argv[2]), ensure_ascii=False, indent=2))
    elif cmd == 'pat':
        pasta_temp, pasta_dest, mapa_json = sys.argv[2], sys.argv[3], sys.argv[4]
        with open(mapa_json, encoding='utf-8') as fh:
            mapa = json.load(fh)
        mant, desc = processar_pat(pasta_temp, pasta_dest, mapa)
        print('=== MANTIDOS (principais/únicos) ===')
        for _, nome in mant:
            print('  [OK]', nome)
        print('=== DESCARTADOS ===')
        for a, motivo in desc:
            print('  [x]', a, '->', motivo)
    else:
        print(__doc__)


if __name__ == '__main__':
    _cli()
