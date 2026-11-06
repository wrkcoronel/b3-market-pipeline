"""Dados abertos da CVM: cadastro, tickers (FCA), balanços (DFP) e fatos relevantes (IPE).

Portal: https://dados.cvm.gov.br/dados/CIA_ABERTA/
"""

from __future__ import annotations

import logging
import re
from datetime import date

import polars as pl

from etl.util import baixar, coluna, ler_csv_cvm, ler_do_zip, para_numero, sem_acento

log = logging.getLogger("etl")

BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA"

# Setores com balanço de instituição financeira (sem EBITDA nem dívida "normal")
SETORES_FINANCEIROS = ("BANCO", "INTERMEDIACAO FINANCEIRA", "SEGURADORA", "SEGUROS", "ARRENDAMENTO MERCANTIL")

TICKER_ACAO = re.compile(r"^[A-Z]{4}(3|4|5|6|7|8|11)$")


def so_digitos(expr: pl.Expr) -> pl.Expr:
    """CNPJ '33.000.167/0001-01' -> '33000167000101' (mesmo formato em todas as tabelas)."""
    return expr.cast(pl.Utf8).str.replace_all(r"\D", "")


def _norm(expr: pl.Expr) -> pl.Expr:
    """Versão polars do sem_acento: maiúsculas e sem acentos comuns da CVM."""
    e = expr.cast(pl.Utf8).str.to_uppercase().str.strip_chars()
    for de, para in {"ÁÀÂÃ": "A", "ÉÊ": "E", "Í": "I", "ÓÔÕ": "O", "ÚÜ": "U", "Ç": "C"}.items():
        e = e.str.replace_all(f"[{de}]", para)
    return e


# --------------------------------------------------------------------------- cadastro

def cadastro() -> pl.DataFrame:
    """Companhias categoria A (ativas e canceladas), uma linha por CNPJ."""
    df = ler_csv_cvm(baixar(f"{BASE}/CAD/DADOS/cad_cia_aberta.csv"))

    df = df.select(
        so_digitos(pl.col("CNPJ_CIA")).alias("cnpj"),
        pl.col("CD_CVM").cast(pl.Int64, strict=False).alias("cd_cvm"),
        pl.col("DENOM_SOCIAL").alias("nome"),
        pl.col("DENOM_COMERC").alias("nome_comercial"),
        pl.col("SETOR_ATIV").alias("setor"),
        _norm(pl.col("SIT")).alias("situacao"),
        pl.col("DT_CANCEL").alias("dt_cancel"),
        _norm(pl.col("CATEG_REG")).alias("categoria"),
        pl.col("DT_INI_SIT").alias("_dt_sit"),
    )

    # Categoria A = pode ter ação na bolsa. Categoria B emite só dívida.
    df = df.filter(pl.col("categoria").str.contains(r"(^|\s)A$"))

    # Um CNPJ pode aparecer mais de uma vez (histórico). Fica a linha ativa mais recente.
    df = (
        df.sort(["cnpj", pl.col("situacao") == "ATIVO", "_dt_sit"], descending=[False, True, True], nulls_last=True)
        .unique("cnpj", keep="first")
        .with_columns(
            _norm(pl.col("setor")).str.contains("|".join(SETORES_FINANCEIROS)).fill_null(False).alias("financeira")
        )
        .drop("_dt_sit", "categoria")
    )
    log.info("Cadastro: %d companhias categoria A (%d ativas)", df.height, df.filter(pl.col("situacao") == "ATIVO").height)
    return df


# --------------------------------------------------------------------------- tickers

def tickers(ano_inicio: int, ano_fim: int) -> pl.DataFrame:
    """Tickers de ações negociadas em bolsa, a partir do FCA de cada ano.

    O FCA em dados abertos começa em 2018; o mais recente vence.
    """
    partes = []
    for ano in range(max(ano_inicio, 2018), ano_fim + 1):
        conteudo = baixar(f"{BASE}/DOC/FCA/DADOS/fca_cia_aberta_{ano}.zip")
        if conteudo is None:
            continue
        df = ler_do_zip(conteudo, "valor_mobiliario")
        if df is None:
            continue

        c = {
            "cnpj": coluna(df, "CNPJ_Companhia", "CNPJ_CIA"),
            "ref": coluna(df, "Data_Referencia"),
            "tipo": coluna(df, "Valor_Mobiliario"),
            "ticker": coluna(df, "Codigo_Negociacao"),
            "mercado": coluna(df, "Mercado"),
            "inicio": coluna(df, "Data_Inicio_Negociacao"),
            "fim": coluna(df, "Data_Fim_Negociacao"),
        }
        faltando = [k for k in ("cnpj", "tipo", "ticker") if c[k] is None]
        if faltando:
            log.warning("FCA %s: colunas não encontradas %s. Colunas: %s", ano, faltando, df.columns)
            continue

        def col(nome):
            return pl.col(c[nome]) if c[nome] else pl.lit(None, dtype=pl.Utf8)

        partes.append(
            df.select(
                so_digitos(col("cnpj")).alias("cnpj"),
                col("ref").alias("_ref"),
                col("tipo").alias("tipo"),
                col("ticker").str.strip_chars().str.to_uppercase().alias("ticker"),
                _norm(col("mercado")).alias("_mercado"),
                col("inicio").alias("inicio"),
                col("fim").alias("fim"),
            )
        )

    if not partes:
        raise RuntimeError("Nenhum arquivo do FCA foi lido")

    df = pl.concat(partes, how="vertical_relaxed")
    df = df.filter(
        pl.col("ticker").str.contains(TICKER_ACAO.pattern)
        & _norm(pl.col("tipo")).str.contains("ACOES|UNITS|CERTIFICADO")
        # Mercado vazio em alguns anos: aceitamos, o ticker já filtra bastante
        & (pl.col("_mercado").is_null() | pl.col("_mercado").str.contains("BOLSA"))
    )
    df = (
        df.sort(["ticker", "_ref"], descending=[False, True], nulls_last=True)
        .unique("ticker", keep="first")
        .with_columns(pl.col("fim").replace("", None))
        .select("ticker", "cnpj", "tipo", "inicio", "fim")
    )
    log.info("Tickers: %d", df.height)
    return df


# --------------------------------------------------------------------------- balanços

DEMONSTRACOES = {
    "BPA": "BPA_con",
    "BPP": "BPP_con",
    "DRE": "DRE_con",
    "DFC_MI": "DFC_MI_con",  # método indireto (a maioria)
    "DFC_MD": "DFC_MD_con",  # método direto
    "DVA": "DVA_con",
}


def _contas(df: pl.DataFrame, demonstracao: str) -> pl.DataFrame:
    """Padroniza uma demonstração: só o exercício atual, última versão, valores em R$."""
    df = df.with_columns(
        so_digitos(pl.col("CNPJ_CIA")).alias("cnpj"),
        pl.col("VERSAO").cast(pl.Int64, strict=False).alias("_versao"),
        _norm(pl.col("ORDEM_EXERC")).alias("_ordem"),
    )
    # ÚLTIMO = exercício do ano do arquivo; PENÚLTIMO = ano anterior (repetido)
    df = df.filter(pl.col("_ordem").str.starts_with("ULTIMO"))
    ultima = df.group_by("cnpj").agg(pl.col("_versao").max().alias("_vmax"))
    df = df.join(ultima, on="cnpj").filter(pl.col("_versao") == pl.col("_vmax"))

    escala = pl.when(_norm(pl.col("ESCALA_MOEDA")) == "MIL").then(1000.0).otherwise(1.0)
    return df.select(
        "cnpj",
        pl.col("DT_FIM_EXERC").str.slice(0, 4).cast(pl.Int64, strict=False).alias("ano"),
        pl.lit(demonstracao).alias("demonstracao"),
        pl.col("CD_CONTA").str.strip_chars().alias("codigo"),
        _norm(pl.col("DS_CONTA")).alias("descricao"),
        (para_numero(pl.col("VL_CONTA")) * escala).alias("valor"),
    )


def dfp(ano: int) -> tuple[pl.DataFrame, pl.DataFrame] | None:
    """Contas do DFP consolidado de um ano (formato longo) e a composição do capital."""
    conteudo = baixar(f"{BASE}/DOC/DFP/DADOS/dfp_cia_aberta_{ano}.zip")
    if conteudo is None:
        return None

    partes = []
    for nome, trecho in DEMONSTRACOES.items():
        df = ler_do_zip(conteudo, trecho)
        if df is not None and df.height:
            partes.append(_contas(df, nome))
    contas = pl.concat(partes, how="vertical_relaxed")

    capital = ler_do_zip(conteudo, "composicao_capital")
    acoes = pl.DataFrame(schema={"cnpj": pl.Utf8, "acoes": pl.Float64})
    if capital is not None:
        total = coluna(capital, "QT_ACAO_TOTAL_CAP_INTEGR")
        tesouraria = coluna(capital, "QT_ACAO_TOTAL_TESOURO")
        versao = coluna(capital, "VERSAO")
        if total:
            acoes = capital.with_columns(
                so_digitos(pl.col("CNPJ_CIA")).alias("cnpj"),
                (pl.col(versao).cast(pl.Int64, strict=False) if versao else pl.lit(1)).alias("_v"),
            )
            acoes = (
                acoes.sort("_v", descending=True)
                .unique("cnpj", keep="first")
                .select(
                    "cnpj",
                    (
                        para_numero(pl.col(total))
                        - (para_numero(pl.col(tesouraria)).fill_null(0) if tesouraria else 0)
                    ).alias("acoes"),
                )
            )
        else:
            log.warning("DFP %s: composição do capital sem QT_ACAO_TOTAL_CAP_INTEGR. Colunas: %s", ano, capital.columns)

    log.info("DFP %s: %d contas de %d companhias", ano, contas.height, contas["cnpj"].n_unique())
    return contas, acoes


def _folhas(contas: list[tuple[str, str, float]]) -> float | None:
    """Soma só as contas mais detalhadas, para não contar o pai e o filho juntos."""
    if not contas:
        return None
    codigos = [c for c, _, _ in contas]
    folhas = [v for c, _, v in contas if not any(o != c and o.startswith(c + ".") for o in codigos)]
    return sum(abs(v) for v in folhas if v is not None)


def resumir_balancos(contas: pl.DataFrame, acoes: pl.DataFrame, financeiras: set[str]) -> list[dict]:
    """Formato longo -> uma linha por empresa/ano com as contas que o filtro usa.

    Contas fixas do plano da CVM para empresas não financeiras:
      DRE 3.01 receita, 3.05 EBIT, 3.11 lucro consolidado
      BPA 1 ativo, 1.01.01 caixa, 1.01.02 aplicações
      BPP 2.01.04 e 2.02.01 empréstimos, 2.03 patrimônio líquido
      DFC 6.01 caixa das operações; DVA 7.04.01 depreciação
    Bancos e seguradoras usam outros códigos, por isso lucro e patrimônio
    são achados pela descrição.
    """
    por_empresa: dict[tuple[str, int], dict[str, list]] = {}
    for cnpj, ano, dem, cod, desc, valor in contas.iter_rows():
        if valor is None or ano is None:
            continue
        por_empresa.setdefault((cnpj, ano), {}).setdefault(dem, []).append((cod, desc, valor))

    mapa_acoes = dict(acoes.iter_rows())
    saida = []
    for (cnpj, ano), dem in por_empresa.items():
        def conta(d: str, codigo: str):
            for c, _, v in dem.get(d, []):
                if c == codigo:
                    return v
            return None

        def nivel2(d: str, raiz: str, padrao: str, preferir: str | None = None):
            achados = [
                (c, desc, v) for c, desc, v in dem.get(d, [])
                if c.startswith(raiz + ".") and c.count(".") == 1 and re.search(padrao, desc)
            ]
            if preferir:
                achados.sort(key=lambda x: preferir not in x[1])
            return achados[0][2] if achados else None

        financeira = cnpj in financeiras
        lucro = nivel2("DRE", "3", r"LUCRO.*PERIODO", preferir="CONSOLIDADO")
        if lucro is None:
            lucro = conta("DRE", "3.11")
        patrimonio = nivel2("BPP", "2", r"PATRIMONIO LIQUIDO")

        dfc = "DFC_MI" if "DFC_MI" in dem else "DFC_MD"
        dividendos = _folhas([
            x for x in dem.get(dfc, [])
            if x[0].startswith("6.03.") and re.search(r"DIVIDEND|JUROS SOBRE (O )?CAPITAL|JCP", x[1])
        ])

        linha = {
            "cnpj": cnpj,
            "ano": ano,
            "receita": conta("DRE", "3.01"),
            "lucro": lucro,
            "patrimonio": patrimonio,
            "ativo": conta("BPA", "1"),
            "caixa_operacional": conta(dfc, "6.01"),
            "dividendos_pagos": dividendos,
            "acoes": mapa_acoes.get(cnpj),
            "ebit": None, "depreciacao": None, "ebitda": None,
            "caixa": None, "divida_bruta": None, "divida_liquida": None,
        }

        if not financeira:
            depre = conta("DVA", "7.04.01")
            if depre is None:
                depre = _folhas([x for x in dem.get(dfc, []) if x[0].startswith("6.01.01.") and "DEPRECIA" in x[1]])
            depre = abs(depre) if depre is not None else None
            ebit = conta("DRE", "3.05")
            caixa = sum(v for v in (conta("BPA", "1.01.01"), conta("BPA", "1.01.02")) if v is not None)
            emprestimos = [conta("BPP", "2.01.04"), conta("BPP", "2.02.01")]
            divida = sum(v for v in emprestimos if v is not None) if any(v is not None for v in emprestimos) else None
            linha.update(
                ebit=ebit,
                depreciacao=depre,
                ebitda=(ebit + depre) if ebit is not None and depre is not None else None,
                caixa=caixa,
                divida_bruta=divida,
                divida_liquida=(divida - caixa) if divida is not None else None,
            )
        saida.append(linha)
    return saida


# --------------------------------------------------------------------------- fatos relevantes

CATEGORIAS_NOTICIA = ("FATO RELEVANTE", "COMUNICADO AO MERCADO")


def noticias(ano: int, desde: date) -> pl.DataFrame:
    """Fatos relevantes e comunicados ao mercado entregues a partir de `desde`."""
    vazio = pl.DataFrame(schema={k: pl.Utf8 for k in ("protocolo", "cnpj", "data", "categoria", "assunto", "link")})
    conteudo = baixar(f"{BASE}/DOC/IPE/DADOS/ipe_cia_aberta_{ano}.zip", cache=False)
    if conteudo is None:
        return vazio
    df = ler_do_zip(conteudo, "ipe_cia_aberta")
    if df is None:
        return vazio

    df = df.select(
        pl.col("Protocolo_Entrega").str.strip_chars().alias("protocolo"),
        so_digitos(pl.col("CNPJ_Companhia")).alias("cnpj"),
        pl.col("Data_Entrega").str.slice(0, 10).alias("data"),
        pl.col("Categoria").alias("categoria"),
        pl.col("Assunto").alias("assunto"),
        pl.col("Link_Download").alias("link"),
    )
    return (
        df.filter(
            _norm(pl.col("categoria")).is_in(CATEGORIAS_NOTICIA)
            & (pl.col("data") >= desde.isoformat())
            & pl.col("protocolo").is_not_null()
        )
        .unique("protocolo", keep="last")
    )


__all__ = ["cadastro", "tickers", "dfp", "resumir_balancos", "noticias", "sem_acento"]
