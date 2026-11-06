"""Séries do Banco Central (SGS): https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"""

from __future__ import annotations

import logging
from datetime import date

import requests

from etl.util import USER_AGENT

log = logging.getLogger("etl")

# nome -> (código SGS, periodicidade)
SERIES = {
    "cdi": (4391, "mensal"),        # CDI acumulado no mês, % — a régua de "dinheiro parado"
    "selic": (4390, "mensal"),      # Selic acumulada no mês, %
    "selic_meta": (432, "diaria"),  # Meta Selic do Copom, % a.a. (guardamos o último valor do mês)
    "ipca": (433, "mensal"),        # IPCA, variação mensal %
}

URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"


def _janelas(inicio: date, fim: date, anos: int = 3):
    """A API recusa (HTTP 406) janelas de mais de 10 anos em séries diárias
    e janelas sem data. Pedaços de 3 anos também respondem mais rápido."""
    atual = inicio
    while atual <= fim:
        proximo = date(atual.year + anos, atual.month, 1)
        yield atual, min(fim, date.fromordinal(proximo.toordinal() - 1))
        atual = proximo


def serie(nome: str, inicio: date, fim: date) -> list[dict]:
    """Valores mensais da série: [{'serie', 'mes': 'AAAA-MM-01', 'valor'}]."""
    codigo, periodicidade = SERIES[nome]
    por_mes: dict[str, float] = {}
    for ini, f in _janelas(date(inicio.year, inicio.month, 1), fim):
        resp = requests.get(
            URL.format(codigo=codigo),
            params={"formato": "json", "dataInicial": ini.strftime("%d/%m/%Y"), "dataFinal": f.strftime("%d/%m/%Y")},
            headers={"User-Agent": USER_AGENT},
            timeout=60,
        )
        if resp.status_code == 404:  # janela sem dados
            continue
        resp.raise_for_status()
        for ponto in resp.json():
            dia, mes, ano = ponto["data"].split("/")
            try:
                valor = float(ponto["valor"])
            except (TypeError, ValueError):
                continue
            # Para série diária, a ordem cronológica faz o último dia do mês vencer
            por_mes[f"{ano}-{mes}-01"] = valor

    log.info("BCB %s (%s): %d meses", nome, periodicidade, len(por_mes))
    return [{"serie": nome, "mes": m, "valor": v} for m, v in sorted(por_mes.items())]
