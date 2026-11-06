"""Funções de apoio: download com cache, leitura dos CSVs da CVM e texto."""

from __future__ import annotations

import io
import logging
import os
import re
import time
import unicodedata
import zipfile
from pathlib import Path

import polars as pl
import requests

log = logging.getLogger("etl")

CACHE = Path(os.environ.get("ETL_CACHE", ".cache"))
USER_AGENT = "b3-market-pipeline (projeto de estudo; github.com/wrkcoronel/b3-market-pipeline)"


def sem_acento(texto: str | None) -> str:
    """'Último Exercício' -> 'ULTIMO EXERCICIO'. Usado para comparar textos da CVM."""
    if texto is None:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).upper().strip()


def baixar(url: str, cache: bool = True, tentativas: int = 3) -> bytes | None:
    """Baixa uma URL. Devolve None em 404 (arquivo do ano ainda não publicado)."""
    destino = CACHE / re.sub(r"[^A-Za-z0-9._-]", "_", url.split("://", 1)[-1])
    if cache and destino.exists():
        return destino.read_bytes()

    for tentativa in range(1, tentativas + 1):
        try:
            resp = requests.get(url, timeout=180, headers={"User-Agent": USER_AGENT})
            if resp.status_code == 404:
                log.info("Não encontrado (404): %s", url)
                return None
            resp.raise_for_status()
            if cache:
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(resp.content)
            return resp.content
        except requests.RequestException as err:
            if tentativa == tentativas:
                raise
            espera = 5 * tentativa
            log.warning("Falha em %s (%s). Nova tentativa em %ss", url, err, espera)
            time.sleep(espera)
    return None


def ler_csv_cvm(conteudo: bytes) -> pl.DataFrame:
    """CSVs da CVM: separador ';', encoding Latin-1, tudo lido como texto."""
    texto = conteudo.decode("latin-1")
    return pl.read_csv(
        io.StringIO(texto),
        separator=";",
        infer_schema=False,       # tudo como texto; convertemos o que precisar
        quote_char='"',
        truncate_ragged_lines=True,
    )


def ler_do_zip(conteudo: bytes, trecho_nome: str) -> pl.DataFrame | None:
    """Lê o CSV de dentro do zip cujo nome contém `trecho_nome` (sem diferenciar maiúsculas).

    Os nomes internos mudam um pouco entre anos, então procuramos por trecho
    em vez do nome exato. Ex.: 'DRE_con' acha 'dfp_cia_aberta_DRE_con_2024.csv'.
    """
    with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
        alvo = trecho_nome.lower()
        nomes = [n for n in z.namelist() if alvo in n.lower() and n.lower().endswith(".csv")]
        if not nomes:
            log.warning("Arquivo com '%s' não encontrado no zip. Há: %s", trecho_nome, z.namelist())
            return None
        # Se houver mais de um (ex.: 'DFC_MD' e 'DFC_MI'), o chamador pede o exato
        return ler_csv_cvm(z.read(sorted(nomes, key=len)[0]))


def coluna(df: pl.DataFrame, *candidatos: str) -> str | None:
    """Acha uma coluna por nome, ignorando maiúsculas/acentos. Protege contra mudança de layout."""
    mapa = {sem_acento(c): c for c in df.columns}
    for cand in candidatos:
        if sem_acento(cand) in mapa:
            return mapa[sem_acento(cand)]
    return None


def para_numero(expr: pl.Expr) -> pl.Expr:
    """Converte texto em número aceitando '1234.56' (CVM) e '1.234,56'."""
    s = expr.cast(pl.Utf8).str.strip_chars()
    s = pl.when(s.str.contains(",")).then(s.str.replace_all(r"\.", "").str.replace(",", ".")).otherwise(s)
    return s.cast(pl.Float64, strict=False)
