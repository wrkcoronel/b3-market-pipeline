"""Preços mensais pelo Yahoo Finance (yfinance).

Não é fonte oficial, mas entrega o preço ajustado por proventos e
desdobramentos, que é o que precisamos para calcular retorno.
"""

from __future__ import annotations

import logging
import math
from datetime import date

log = logging.getLogger("etl")


def mensais(tickers: list[str], inicio: date, lote: int = 80) -> list[dict]:
    """[{'ticker', 'mes', 'fechamento', 'ajustado', 'volume'}] para cada ticker com dados."""
    import yfinance as yf  # import aqui: os testes não precisam do yfinance

    linhas: list[dict] = []
    for i in range(0, len(tickers), lote):
        grupo = tickers[i : i + lote]
        log.info("yfinance: tickers %d a %d de %d", i + 1, i + len(grupo), len(tickers))
        df = yf.download(
            [f"{t}.SA" for t in grupo],
            start=inicio.isoformat(),
            interval="1mo",
            auto_adjust=False,   # queremos o preço de tela E o ajustado
            group_by="ticker",
            threads=True,
            progress=False,
        )
        if df is None or df.empty:
            continue
        multi = df.columns.nlevels > 1
        for t in grupo:
            chave = f"{t}.SA"
            if multi:
                if chave not in df.columns.get_level_values(0):
                    continue
                sub = df[chave]
            elif len(grupo) == 1:  # versões antigas devolvem colunas simples para 1 ticker
                sub = df
            else:
                continue
            for ts, reg in sub.iterrows():
                fech = reg.get("Close")
                if fech is None or (isinstance(fech, float) and math.isnan(fech)):
                    continue
                aj = reg.get("Adj Close")
                vol = reg.get("Volume")
                linhas.append({
                    "ticker": t,
                    "mes": ts.strftime("%Y-%m-01"),
                    "fechamento": float(fech),
                    "ajustado": None if aj is None or math.isnan(aj) else float(aj),
                    "volume": None if vol is None or math.isnan(vol) else float(vol),
                })
    return linhas


def escolher_principal(tickers: list[dict], precos: list[dict], meses_recentes: set[str]) -> dict[str, str]:
    """Um ticker por empresa: o de maior volume financeiro nos meses recentes.

    Ações ON/PN têm preferência sobre units: a unit (ex.: TAEE11) é um pacote de
    várias ações, então preço da unit x total de ações daria um valor de mercado
    errado. A unit só vence se for o único papel com negociação.
    """
    volume: dict[str, float] = {}
    for p in precos:
        if p["mes"] in meses_recentes and p["volume"]:
            volume[p["ticker"]] = volume.get(p["ticker"], 0) + p["volume"] * p["fechamento"]

    por_empresa: dict[str, list[tuple[bool, float, str]]] = {}
    for t in tickers:
        vol = volume.get(t["ticker"], 0)
        if vol <= 0:
            continue
        unit = t["ticker"].endswith("11") or "UNIT" in (t.get("tipo") or "").upper()
        por_empresa.setdefault(t["cnpj"], []).append((unit, -vol, t["ticker"]))

    return {cnpj: sorted(op)[0][2] for cnpj, op in por_empresa.items()}
