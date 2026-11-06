"""Testa o ETL de ponta a ponta com arquivos falsos no formato da CVM.

Nada aqui acessa a internet: os downloads são substituídos por zips montados
em memória, com o mesmo layout (separador ';', Latin-1) dos dados abertos.
"""

import io
import zipfile
from datetime import date

import pytest

from etl import __main__ as cli
from etl.destino import DestinoSQLite, OrcamentoEstourado, literal
from etl.fontes import bcb, cvm, precos

HOJE = date(2026, 11, 6)
PETRO = "33.000.167/0001-01"
BANCO = "60.872.504/0001-23"
QUEBRADA = "11.111.111/0001-11"


def csv(cabecalho, linhas):
    texto = ";".join(cabecalho) + "\n" + "\n".join(";".join(map(str, l)) for l in linhas) + "\n"
    return texto.encode("latin-1")


def zip_com(arquivos: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for nome, conteudo in arquivos.items():
            z.writestr(nome, conteudo)
    return buf.getvalue()


CAD = csv(
    ["CNPJ_CIA", "DENOM_SOCIAL", "DENOM_COMERC", "DT_REG", "DT_CANCEL", "SIT", "DT_INI_SIT", "CD_CVM", "SETOR_ATIV", "CATEG_REG"],
    [
        [PETRO, "PETRÓLEO BRASILEIRO S.A.", "PETROBRAS", "1977-07-20", "", "ATIVO", "1977-07-20", "9512", "Petróleo e Gás", "Categoria A"],
        [BANCO, "ITAÚ UNIBANCO HOLDING S.A.", "ITAU", "1977-01-01", "", "ATIVO", "1977-01-01", "19348", "Bancos", "Categoria A"],
        [QUEBRADA, "QUEBRADA S.A.", "", "2000-01-01", "2019-05-01", "CANCELADA", "2019-05-01", "99999", "Comércio", "Categoria A"],
        ["22.222.222/0001-22", "SÓ DEBÊNTURE S.A.", "", "2010-01-01", "", "ATIVO", "2010-01-01", "88888", "Energia", "Categoria B"],
    ],
)

FCA_COLS = ["CNPJ_Companhia", "Data_Referencia", "Versao", "Valor_Mobiliario", "Codigo_Negociacao", "Mercado",
            "Data_Inicio_Negociacao", "Data_Fim_Negociacao"]


def fca(ano):
    return zip_com({f"fca_cia_aberta_valor_mobiliario_{ano}.csv": csv(FCA_COLS, [
        [PETRO, f"{ano}-01-01", 1, "Ações Ordinárias", "PETR3", "Bolsa", "1977-01-01", ""],
        [PETRO, f"{ano}-01-01", 1, "Ações Preferenciais", "PETR4", "Bolsa", "1977-01-01", ""],
        [PETRO, f"{ano}-01-01", 1, "Debêntures", "PETR16", "Balcão Organizado", "", ""],
        [BANCO, f"{ano}-01-01", 1, "Ações Preferenciais", "ITUB4", "Bolsa", "2009-01-01", ""],
        [QUEBRADA, "2018-01-01", 1, "Ações Ordinárias", "QBRD3", "Bolsa", "2001-01-01", "2019-05-01"],
    ])})


DFP_COLS = ["CNPJ_CIA", "DT_REFER", "VERSAO", "DENOM_CIA", "CD_CVM", "GRUPO_DFP", "MOEDA", "ESCALA_MOEDA",
            "ORDEM_EXERC", "DT_INI_EXERC", "DT_FIM_EXERC", "CD_CONTA", "DS_CONTA", "VL_CONTA", "ST_CONTA_FIXA"]


def linha(cnpj, ano, cod, desc, valor, ordem="ÚLTIMO", versao=1):
    return [cnpj, f"{ano}-12-31", versao, "X", "1", "G", "REAL", "MIL", ordem, f"{ano}-01-01", f"{ano}-12-31",
            cod, desc, valor, "S"]


def dfp(ano):
    lucro = 100_000 if ano != 2020 else -5_000  # um ano de prejuízo
    dre = [
        linha(PETRO, ano, "3.01", "Receita de Venda de Bens e/ou Serviços", 500_000),
        linha(PETRO, ano, "3.05", "Resultado Antes do Resultado Financeiro e dos Tributos", 150_000),
        linha(PETRO, ano, "3.11", "Lucro/Prejuízo Consolidado do Período", lucro),
        linha(PETRO, ano, "3.11", "Lucro/Prejuízo Consolidado do Período", 1, ordem="PENÚLTIMO"),
        # versão 1 substituída pela 2: deve ser ignorada
        linha(BANCO, ano, "3.11", "Lucro ou Prejuízo Líquido Consolidado do Período", 1, versao=1),
        linha(BANCO, ano, "3.01", "Receitas da Intermediação Financeira", 300_000, versao=2),
        linha(BANCO, ano, "3.11", "Lucro ou Prejuízo Líquido Consolidado do Período", 40_000, versao=2),
    ]
    bpa = [
        linha(PETRO, ano, "1", "Ativo Total", 1_000_000),
        linha(PETRO, ano, "1.01.01", "Caixa e Equivalentes de Caixa", 50_000),
        linha(PETRO, ano, "1.01.02", "Aplicações Financeiras", 10_000),
    ]
    bpp = [
        linha(PETRO, ano, "2.01.04", "Empréstimos e Financiamentos", 100_000),
        linha(PETRO, ano, "2.02.01", "Empréstimos e Financiamentos", 200_000),
        linha(PETRO, ano, "2.03", "Patrimônio Líquido Consolidado", 400_000),
        linha(BANCO, ano, "2.08", "Patrimônio Líquido Consolidado", 200_000, versao=2),
    ]
    dfc = [
        linha(PETRO, ano, "6.01", "Caixa Líquido Atividades Operacionais", 180_000),
        linha(PETRO, ano, "6.03.05", "Dividendos e JCP pagos", -30_000),
        linha(PETRO, ano, "6.03.05.01", "Dividendos pagos", -20_000),
        linha(PETRO, ano, "6.03.05.02", "Juros sobre o capital próprio pagos", -10_000),
    ]
    dva = [linha(PETRO, ano, "7.04.01", "Depreciação, Amortização e Exaustão", 50_000)]
    capital = csv(
        ["CNPJ_CIA", "DT_REFER", "VERSAO", "QT_ACAO_ORDIN_CAP_INTEGR", "QT_ACAO_TOTAL_CAP_INTEGR", "QT_ACAO_TOTAL_TESOURO"],
        [[PETRO, f"{ano}-12-31", 1, "0", "13000", "1000"], [BANCO, f"{ano}-12-31", 2, "0", "1000", "0"]],
    )
    return zip_com({
        f"dfp_cia_aberta_{ano}.csv": b"CNPJ_CIA\n",
        f"dfp_cia_aberta_DRE_con_{ano}.csv": csv(DFP_COLS, dre),
        f"dfp_cia_aberta_BPA_con_{ano}.csv": csv(DFP_COLS, bpa),
        f"dfp_cia_aberta_BPP_con_{ano}.csv": csv(DFP_COLS, bpp),
        f"dfp_cia_aberta_DFC_MI_con_{ano}.csv": csv(DFP_COLS, dfc),
        f"dfp_cia_aberta_DFC_MD_con_{ano}.csv": csv(DFP_COLS, []),
        f"dfp_cia_aberta_DVA_con_{ano}.csv": csv(DFP_COLS, dva),
        f"dfp_cia_aberta_composicao_capital_{ano}.csv": capital,
    })


IPE = zip_com({"ipe_cia_aberta_2026.csv": csv(
    ["CNPJ_Companhia", "Nome_Companhia", "Codigo_CVM", "Data_Referencia", "Categoria", "Tipo", "Especie", "Assunto",
     "Data_Entrega", "Tipo_Apresentacao", "Protocolo_Entrega", "Versao", "Link_Download"],
    [
        [PETRO, "PETROBRAS", "9512", "2026-11-01", "Fato Relevante", "", "", "Aquisição de ativos", "2026-11-01", "AP", "P1", 1, "https://x/1"],
        [PETRO, "PETROBRAS", "9512", "2026-11-02", "Comunicado ao Mercado", "", "", "Produção de outubro", "2026-11-02", "AP", "P2", 1, "https://x/2"],
        [PETRO, "PETROBRAS", "9512", "2026-11-03", "Assembleia", "", "", "Edital", "2026-11-03", "AP", "P3", 1, "https://x/3"],
        [QUEBRADA, "QUEBRADA", "99999", "2026-11-03", "Fato Relevante", "", "", "Não listada", "2026-11-03", "AP", "P4", 1, "https://x/4"],
    ],
)})


def falso_baixar(url, cache=True, tentativas=3):
    if url.endswith("cad_cia_aberta.csv"):
        return CAD
    for ano in range(2010, 2030):
        if url.endswith(f"fca_cia_aberta_{ano}.zip"):
            return fca(ano) if ano >= 2018 else None
        if url.endswith(f"dfp_cia_aberta_{ano}.zip"):
            return dfp(ano) if 2016 <= ano <= 2025 else None  # 2026 ainda não publicado
        if url.endswith(f"ipe_cia_aberta_{ano}.zip"):
            return IPE if ano == 2026 else None
    raise AssertionError(f"URL inesperada: {url}")


def meses(inicio_ano, fim):
    ano, m = inicio_ano, 1
    while (ano, m) <= fim:
        yield f"{ano:04d}-{m:02d}-01"
        ano, m = (ano + 1, 1) if m == 12 else (ano, m + 1)


def falsos_precos(tickers, inicio, lote=80):
    saida = []
    for t in tickers:
        if t == "QBRD3":
            continue
        for i, mes in enumerate(meses(inicio.year, (2026, 11))):
            if mes < f"{inicio:%Y-%m}-01":
                continue
            preco = {"PETR4": 30.0, "PETR3": 32.0, "ITUB4": 35.0}[t]
            saida.append({"ticker": t, "mes": mes, "fechamento": preco,
                          "ajustado": 10 + i * 0.1, "volume": {"PETR4": 9e8, "PETR3": 2e8, "ITUB4": 5e8}[t]})
    return saida


def falsa_serie(nome, inicio, fim):
    valor = {"cdi": 1.0, "selic": 1.0, "selic_meta": 15.0, "ipca": 0.4}[nome]
    return [{"serie": nome, "mes": m, "valor": valor} for m in meses(inicio.year, (fim.year, fim.month))
            if m >= f"{inicio:%Y-%m}-01"]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(cvm, "baixar", falso_baixar)
    monkeypatch.setattr(precos, "mensais", falsos_precos)
    monkeypatch.setattr(bcb, "serie", falsa_serie)
    return DestinoSQLite(str(tmp_path / "teste.db"))


def test_carga_inicial_de_ponta_a_ponta(db):
    cli.rodar(db, "carga-inicial", cli.PLANOS["carga-inicial"], HOJE, anos=10)

    empresas = {e["cnpj"]: e for e in db.consultar("SELECT * FROM empresas")}
    assert set(empresas) == {"33000167000101", "60872504000123", "11111111000111"}  # categoria B fora
    assert empresas["33000167000101"]["ticker_principal"] == "PETR4"  # maior volume
    assert empresas["33000167000101"]["listada"] == 1
    assert empresas["60872504000123"]["financeira"] == 1
    assert empresas["11111111000111"]["listada"] == 0  # cancelada fica, mas fora do painel

    tickers = {t["ticker"] for t in db.consultar("SELECT ticker FROM tickers")}
    assert tickers == {"PETR3", "PETR4", "ITUB4", "QBRD3"}  # debênture fora

    f = db.consultar("SELECT * FROM fundamentos WHERE cnpj = '33000167000101' AND ano = 2025")[0]
    assert f["receita"] == 500_000_000  # escala MIL
    assert f["lucro"] == 100_000_000  # PENÚLTIMO ignorado
    assert f["ebitda"] == 200_000_000  # EBIT + depreciação da DVA
    assert f["divida_liquida"] == 240_000_000  # 300 de dívida - 60 de caixa
    assert f["dividendos_pagos"] == 30_000_000  # só as folhas, sem somar o pai
    assert f["acoes"] == 12_000  # total - tesouraria

    b = db.consultar("SELECT * FROM fundamentos WHERE cnpj = '60872504000123' AND ano = 2025")[0]
    assert b["lucro"] == 40_000_000  # versão 2 vence
    assert b["patrimonio"] == 200_000_000  # achado pela descrição (código 2.08)
    assert b["ebitda"] is None  # banco não tem EBITDA

    # Só o ticker principal é gravado (economia de escrita no D1)
    assert {r["ticker"] for r in db.consultar("SELECT DISTINCT ticker FROM precos_mensais")} == {"PETR4", "ITUB4"}

    ind = {i["ticker"]: i for i in db.consultar("SELECT * FROM indicadores")}
    assert set(ind) == {"PETR4", "ITUB4"}
    p = ind["PETR4"]
    assert p["valor_mercado"] == 30.0 * 12_000
    assert p["roe"] == pytest.approx(0.25)
    assert p["margem_liquida"] == pytest.approx(0.2)
    assert p["divida_liq_ebitda"] == pytest.approx(1.2)
    assert p["anos_com_lucro"] == 9  # 2020 teve prejuízo
    assert p["anos_de_balanco"] == 10
    assert p["cdi_5a"] == pytest.approx(1.01 ** 60 - 1)
    assert p["retorno_5a"] == pytest.approx(23.0 / 17.0 - 1)  # ajustado nov/26 vs nov/21
    assert ind["ITUB4"]["divida_liq_ebitda"] is None

    noticias = {n["protocolo"] for n in db.consultar("SELECT protocolo FROM noticias")}
    assert noticias == {"P1", "P2"}  # assembleia e empresa não listada ficam de fora


def test_diario_nao_duplica_e_atualiza(db):
    cli.rodar(db, "carga-inicial", cli.PLANOS["carga-inicial"], HOJE, anos=10)
    antes = db.consultar("SELECT COUNT(*) AS n FROM precos_mensais")[0]["n"]
    db.escritas = 0
    cli.rodar(db, "diario", cli.PLANOS["diario"], HOJE, anos=10)
    assert db.consultar("SELECT COUNT(*) AS n FROM precos_mensais")[0]["n"] == antes
    assert db.escritas < 200  # diário é barato


def test_orcamento_estourado_para_antes_de_gravar(db):
    db.limite = 5
    db.criar_tabelas()
    with pytest.raises(OrcamentoEstourado):
        db.gravar("macro", [{"serie": "x", "mes": f"2020-{m:02d}-01", "valor": 1.0} for m in range(1, 13)], ["serie", "mes"])
    assert db.consultar("SELECT COUNT(*) AS n FROM macro")[0]["n"] == 0


def test_literal_escapa_aspas_e_nan():
    assert literal("O'Brien") == "'O''Brien'"
    assert literal(float("nan")) == "NULL"
    assert literal(None) == "NULL"


def test_janelas_do_bcb_tem_no_maximo_3_anos():
    janelas = list(bcb._janelas(date(2016, 1, 1), date(2026, 11, 6)))
    assert janelas[0] == (date(2016, 1, 1), date(2018, 12, 31))
    assert janelas[-1][1] == date(2026, 11, 6)
    assert all((f - i).days <= 3 * 366 for i, f in janelas)


def test_unit_so_vence_se_for_a_unica_com_volume():
    tks = [{"ticker": "TAEE11", "cnpj": "1", "tipo": "Units"}, {"ticker": "TAEE4", "cnpj": "1", "tipo": "Ações Preferenciais"}]
    p = [{"ticker": "TAEE11", "mes": "2026-11-01", "volume": 1e9, "fechamento": 35.0},
         {"ticker": "TAEE4", "mes": "2026-11-01", "volume": 1e6, "fechamento": 12.0}]
    assert precos.escolher_principal(tks, p, {"2026-11-01"}) == {"1": "TAEE4"}
    assert precos.escolher_principal(tks, p[:1], {"2026-11-01"}) == {"1": "TAEE11"}


def test_d1_monta_as_chamadas_da_api(monkeypatch):
    from etl import destino

    chamadas = []

    class Resp:
        def __init__(self, corpo, status=200):
            self._c, self.status_code = corpo, status

        def json(self):
            return self._c

        def raise_for_status(self):
            pass

    class Sessao:
        headers = {}

        def get(self, url, params=None, timeout=None):
            chamadas.append(("GET", url, params))
            return Resp({"result": [{"name": "outro", "uuid": "x"}, {"name": "b3-market", "uuid": "abc"}]})

        def post(self, url, json=None, timeout=None):
            chamadas.append(("POST", url, json))
            return Resp({"success": True, "result": [{"results": [{"n": 1}]}]})

    monkeypatch.setattr(destino.requests, "Session", Sessao)
    d = destino.DestinoD1("conta1", "tok", "b3-market")
    assert d.id == "abc"
    assert d.consultar("SELECT ?", [1]) == [{"n": 1}]
    assert chamadas[-1] == ("POST", "https://api.cloudflare.com/client/v4/accounts/conta1/d1/database/abc/query",
                            {"sql": "SELECT ?", "params": [1]})
