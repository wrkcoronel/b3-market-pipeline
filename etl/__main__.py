"""ETL do filtro de empresas da B3.

Uso:
    python -m etl carga-inicial            # 10 anos de tudo (1ª vez)
    python -m etl semanal                  # cadastro + balanços recentes + diário
    python -m etl diario                   # preços do mês, macro, notícias, indicadores
    python -m etl diario --sqlite dev.db   # grava num SQLite local em vez do D1
    python -m etl carga-inicial --etapas fundamentos,indicadores

Para gravar no D1, defina CLOUDFLARE_ACCOUNT_ID e CLOUDFLARE_API_TOKEN
(token com permissão D1:Edit). O banco padrão é 'b3-market' (D1_DATABASE).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta

from etl import indicadores
from etl.destino import LIMITE_PADRAO, Destino, DestinoD1, DestinoSQLite, OrcamentoEstourado, literal
from etl.fontes import bcb, cvm, precos

log = logging.getLogger("etl")


def _mes_inicio(hoje: date, meses_atras: int) -> date:
    total = hoje.year * 12 + hoje.month - 1 - meses_atras
    return date(total // 12, total % 12 + 1, 1)


# ----------------------------------------------------------------------------- etapas

def etapa_cadastro(d: Destino, hoje: date, anos: int) -> None:
    empresas = cvm.cadastro()
    d.gravar("empresas", empresas.with_columns(empresas["financeira"].cast(int)).to_dicts(), ["cnpj"])
    tks = cvm.tickers(hoje.year - anos, hoje.year)
    # Só tickers de empresas categoria A (as outras não estão na tabela empresas)
    tks = tks.filter(tks["cnpj"].is_in(empresas["cnpj"].implode()))
    d.gravar("tickers", tks.to_dicts(), ["ticker"])


def etapa_precos(d: Destino, hoje: date, inicio: date) -> None:
    candidatos = d.consultar(
        "SELECT t.ticker, t.cnpj, t.tipo FROM tickers t JOIN empresas e ON e.cnpj = t.cnpj "
        "WHERE e.situacao = 'ATIVO' AND (t.fim IS NULL OR t.fim = '')"
    )
    if not candidatos:
        log.warning("Sem tickers: rode a etapa 'cadastro' primeiro")
        return

    # Garante os últimos 3 meses no download, para escolher o ticker principal pelo volume
    inicio = min(inicio, _mes_inicio(hoje, 3))
    linhas = precos.mensais(sorted({c["ticker"] for c in candidatos}), inicio)
    recentes = {f"{_mes_inicio(hoje, i):%Y-%m}-01" for i in range(3)}
    principal = precos.escolher_principal(candidatos, linhas, recentes)

    # Atualiza listada/ticker_principal só onde mudou (cada UPDATE conta como escrita)
    atuais = {r["cnpj"]: r for r in d.consultar("SELECT cnpj, listada, ticker_principal FROM empresas")}
    mudancas = []
    for cnpj, atual in atuais.items():
        novo_ticker = principal.get(cnpj)
        novo_listada = 1 if novo_ticker else 0
        if atual["listada"] != novo_listada or atual["ticker_principal"] != novo_ticker:
            mudancas.append((cnpj, novo_listada, novo_ticker))
    for i in range(0, len(mudancas), 200):
        lote = mudancas[i : i + 200]
        listada = " ".join(f"WHEN {literal(c)} THEN {l}" for c, l, _ in lote)
        ticker = " ".join(f"WHEN {literal(c)} THEN {literal(t)}" for c, _, t in lote)
        em = ", ".join(literal(c) for c, _, _ in lote)
        d.executar(
            f"UPDATE empresas SET listada = CASE cnpj {listada} END, "
            f"ticker_principal = CASE cnpj {ticker} END, atualizado_em = datetime('now') "
            f"WHERE cnpj IN ({em})"
        )
    d.escritas += len(mudancas)
    log.info("Empresas listadas: %d (%d mudaram)", len(principal), len(mudancas))

    # Guarda só o ticker principal: 10 anos de todos estouraria o limite diário do D1
    escolhidos = set(principal.values())
    inicio_txt = f"{inicio:%Y-%m}-01"
    d.gravar(
        "precos_mensais",
        [l for l in linhas if l["ticker"] in escolhidos and l["mes"] >= inicio_txt],
        ["ticker", "mes"],
    )


def etapa_fundamentos(d: Destino, anos: list[int]) -> None:
    com_ticker = {r["cnpj"] for r in d.consultar("SELECT DISTINCT cnpj FROM tickers")}
    financeiras = {r["cnpj"] for r in d.consultar("SELECT cnpj FROM empresas WHERE financeira = 1")}
    if not com_ticker:
        log.warning("Sem tickers: rode a etapa 'cadastro' primeiro")
        return
    for ano in anos:
        resultado = cvm.dfp(ano)
        if resultado is None:
            continue
        contas, acoes = resultado
        contas = contas.filter(contas["cnpj"].is_in(list(com_ticker)))
        linhas = [l for l in cvm.resumir_balancos(contas, acoes, financeiras) if l["ano"] == ano]
        d.gravar("fundamentos", linhas, ["cnpj", "ano"])


def etapa_macro(d: Destino, hoje: date, inicio: date) -> None:
    for nome in bcb.SERIES:
        d.gravar("macro", bcb.serie(nome, inicio, hoje), ["serie", "mes"])


def etapa_noticias(d: Destino, hoje: date, desde: date) -> None:
    listadas = [r["cnpj"] for r in d.consultar("SELECT cnpj FROM empresas WHERE listada = 1")]
    for ano in range(desde.year, hoje.year + 1):
        df = cvm.noticias(ano, desde)
        df = df.filter(df["cnpj"].is_in(listadas))
        d.gravar("noticias", df.to_dicts(), ["protocolo"])


def etapa_indicadores(d: Destino, hoje: date) -> None:
    linhas = indicadores.calcular(d, hoje)
    if not linhas:
        return
    # Recria a tabela inteira: empresa que deixou de ser listada sai do filtro
    antigas = d.consultar("SELECT COUNT(*) AS n FROM indicadores")[0]["n"]
    if d.escritas + antigas + len(linhas) > d.limite:
        raise OrcamentoEstourado("indicadores: sem orçamento para recriar a tabela hoje")
    d.executar("DELETE FROM indicadores")
    d.escritas += antigas
    d.gravar("indicadores", linhas, ["cnpj"])


# ----------------------------------------------------------------------------- execução

PLANOS = {
    # Ordem importa: preços dependem dos tickers; indicadores de tudo
    "carga-inicial": ["cadastro", "macro", "precos", "fundamentos", "indicadores", "noticias"],
    "semanal": ["cadastro", "fundamentos", "macro", "precos", "noticias", "indicadores"],
    "diario": ["macro", "precos", "noticias", "indicadores"],
}


def rodar(d: Destino, modo: str, etapas: list[str], hoje: date, anos: int) -> None:
    inicial = modo == "carga-inicial"
    d.criar_tabelas()
    for etapa in etapas:
        log.info("── etapa: %s", etapa)
        if etapa == "cadastro":
            etapa_cadastro(d, hoje, anos)
        elif etapa == "macro":
            etapa_macro(d, hoje, _mes_inicio(hoje, anos * 12 if inicial else 6))
        elif etapa == "precos":
            etapa_precos(d, hoje, _mes_inicio(hoje, anos * 12 if inicial else 2))
        elif etapa == "fundamentos":
            # DFP sai até março do ano seguinte; semanal pega o ano passado e o retrasado
            # (reapresentações). Na carga inicial, os últimos `anos` anos.
            lista = list(range(hoje.year - anos, hoje.year + 1)) if inicial else [hoje.year - 2, hoje.year - 1]
            etapa_fundamentos(d, lista)
        elif etapa == "noticias":
            etapa_noticias(d, hoje, hoje - timedelta(days=365 if inicial else 10))
        elif etapa == "indicadores":
            etapa_indicadores(d, hoje)
        else:
            raise SystemExit(f"Etapa desconhecida: {etapa}. Opções: {sorted({e for p in PLANOS.values() for e in p})}")
    log.info("Concluído. Linhas escritas nesta execução: %d", d.escritas)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m etl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("modo", choices=PLANOS)
    p.add_argument("--etapas", help="Roda só estas etapas (separadas por vírgula)")
    p.add_argument("--anos", type=int, default=10, help="Anos de histórico na carga inicial (padrão 10)")
    p.add_argument("--sqlite", help="Grava num arquivo SQLite local em vez do D1")
    p.add_argument("--limite", type=int, default=LIMITE_PADRAO, help="Máximo de linhas escritas por execução")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if args.sqlite:
        destino: Destino = DestinoSQLite(args.sqlite, limite_linhas=args.limite)
    else:
        conta, token = os.environ.get("CLOUDFLARE_ACCOUNT_ID"), os.environ.get("CLOUDFLARE_API_TOKEN")
        if not conta or not token:
            p.error("defina CLOUDFLARE_ACCOUNT_ID e CLOUDFLARE_API_TOKEN, ou use --sqlite arquivo.db")
        destino = DestinoD1(conta, token, os.environ.get("D1_DATABASE", "b3-market"), limite_linhas=args.limite)

    etapas = args.etapas.split(",") if args.etapas else PLANOS[args.modo]
    try:
        rodar(destino, args.modo, [e.strip() for e in etapas], date.today(), args.anos)
    except OrcamentoEstourado as err:
        log.error("%s", err)
        log.error("O que já foi gravado fica. Amanhã rode só as etapas que faltaram com --etapas.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
