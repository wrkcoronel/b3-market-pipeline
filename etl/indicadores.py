"""Calcula a tabela `indicadores`: uma linha por empresa listada, pronta para o filtro."""

from __future__ import annotations

import logging
from datetime import date

from etl.destino import Destino

log = logging.getLogger("etl")


def _div(a, b, so_positivo: bool = True):
    """a / b, ou None se faltar dado ou se b não fizer sentido (zero ou negativo)."""
    if a is None or b is None or b == 0 or (so_positivo and b < 0):
        return None
    return a / b


def _mes_menos(mes: str, meses: int) -> str:
    ano, m = int(mes[:4]), int(mes[5:7])
    total = ano * 12 + (m - 1) - meses
    return f"{total // 12:04d}-{total % 12 + 1:02d}-01"


def calcular(destino: Destino, hoje: date | None = None) -> list[dict]:
    hoje = hoje or date.today()
    empresas = destino.consultar(
        "SELECT cnpj, nome, setor, financeira, ticker_principal AS ticker "
        "FROM empresas WHERE listada = 1 AND ticker_principal IS NOT NULL"
    )
    if not empresas:
        log.warning("Nenhuma empresa listada com ticker: rode o cadastro e os preços antes")
        return []

    fundamentos: dict[str, list[dict]] = {}
    for f in destino.consultar(f"SELECT * FROM fundamentos WHERE ano >= {hoje.year - 11} ORDER BY ano"):
        fundamentos.setdefault(f["cnpj"], []).append(f)

    precos: dict[str, list[dict]] = {}
    inicio = _mes_menos(f"{hoje:%Y-%m}-01", 66)
    for p in destino.consultar(
        "SELECT p.ticker, p.mes, p.fechamento, p.ajustado FROM precos_mensais p "
        "JOIN empresas e ON e.ticker_principal = p.ticker WHERE e.listada = 1 AND p.mes >= ? ORDER BY p.mes",
        [inicio],
    ):
        precos.setdefault(p["ticker"], []).append(p)

    cdi = {m["mes"]: m["valor"] for m in destino.consultar("SELECT mes, valor FROM macro WHERE serie = 'cdi'")}
    mes_minimo_preco = _mes_menos(f"{hoje:%Y-%m}-01", 2)

    saida = []
    for e in empresas:
        historico = fundamentos.get(e["cnpj"], [])
        com_lucro = [f for f in historico if f["lucro"] is not None]
        if not com_lucro or com_lucro[-1]["ano"] < hoje.year - 2:
            continue  # sem balanço recente: não dá para avaliar
        base = com_lucro[-1]
        ultimos10 = com_lucro[-10:]

        serie = precos.get(e["ticker"], [])
        ultimo = serie[-1] if serie and serie[-1]["mes"] >= mes_minimo_preco else None
        preco = ultimo["fechamento"] if ultimo else None
        vm = preco * base["acoes"] if preco and base["acoes"] else None

        retorno_5a = cdi_5a = None
        if ultimo:
            mes_ini = _mes_menos(ultimo["mes"], 60)
            antigo = next((p for p in serie if p["mes"] == mes_ini), None)
            if antigo and antigo["ajustado"] and ultimo["ajustado"]:
                retorno_5a = ultimo["ajustado"] / antigo["ajustado"] - 1
                acumulado = 1.0
                for mes, taxa in cdi.items():
                    if mes_ini < mes <= ultimo["mes"]:
                        acumulado *= 1 + taxa / 100
                cdi_5a = acumulado - 1

        saida.append({
            "cnpj": e["cnpj"],
            "ticker": e["ticker"],
            "nome": e["nome"],
            "setor": e["setor"],
            "financeira": e["financeira"],
            "ano_balanco": base["ano"],
            "preco": preco,
            "valor_mercado": vm,
            "roe": _div(base["lucro"], base["patrimonio"]),
            "margem_liquida": _div(base["lucro"], base["receita"]),
            "divida_liq_ebitda": _div(base["divida_liquida"], base["ebitda"]),
            "p_l": _div(vm, base["lucro"]),           # prejuízo -> sem P/L
            "p_vp": _div(vm, base["patrimonio"]),
            "dividend_yield": _div(base["dividendos_pagos"], vm),
            "anos_com_lucro": sum(1 for f in ultimos10 if f["lucro"] > 0),
            "anos_com_dividendo": sum(1 for f in ultimos10 if (f["dividendos_pagos"] or 0) > 0),
            "anos_de_balanco": len(ultimos10),
            "retorno_5a": retorno_5a,
            "cdi_5a": cdi_5a,
        })

    log.info("Indicadores calculados para %d empresas", len(saida))
    return saida
