"""Para onde o ETL grava: SQLite local (testes) ou D1 pela API REST da Cloudflare."""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from pathlib import Path

import requests

log = logging.getLogger("etl")

SCHEMA = Path(__file__).with_name("schema.sql")

# Plano grátis do D1: 100 mil linhas escritas por dia. Paramos antes para
# sobrar folga para o Worker (que grava as cotações diárias).
LIMITE_PADRAO = 90_000
TAM_MAX_SQL = 90_000  # D1 aceita até 100 KB por instrução


def literal(v) -> str:
    """Valor Python -> literal SQL. Usado porque o D1 aceita só 100 parâmetros por consulta."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return "NULL" if math.isnan(v) or math.isinf(v) else repr(v)
    texto = str(v).replace("\x00", "").replace("'", "''")
    return f"'{texto}'"


class OrcamentoEstourado(RuntimeError):
    pass


class Destino:
    """Interface comum. `executar` roda SQL; `consultar` devolve linhas como dict."""

    def __init__(self, limite_linhas: int = LIMITE_PADRAO):
        self.limite = limite_linhas
        self.escritas = 0

    def executar(self, sql: str) -> None:
        raise NotImplementedError

    def consultar(self, sql: str, params: list | None = None) -> list[dict]:
        raise NotImplementedError

    def criar_tabelas(self) -> None:
        for instrucao in SCHEMA.read_text(encoding="utf-8").split(";"):
            # remove comentários de linha para não mandar instrução vazia
            limpo = "\n".join(l for l in instrucao.splitlines() if not l.strip().startswith("--")).strip()
            if limpo:
                self.executar(limpo)

    def gravar(self, tabela: str, linhas: list[dict], chave: list[str]) -> int:
        """Upsert em lotes. Para sem gravar nada se o lote estourar o orçamento do dia."""
        if not linhas:
            return 0
        if self.escritas + len(linhas) > self.limite:
            raise OrcamentoEstourado(
                f"{tabela}: gravar {len(linhas)} linhas passaria do limite de {self.limite} "
                f"por execução (já foram {self.escritas}). Rode o restante amanhã ou use --limite."
            )

        colunas = list(linhas[0].keys())
        atualizar = [c for c in colunas if c not in chave]
        cabecalho = f"INSERT INTO {tabela} ({', '.join(colunas)}) VALUES "
        conflito = f" ON CONFLICT ({', '.join(chave)}) DO " + (
            "UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in atualizar) if atualizar else "NOTHING"
        )

        lote: list[str] = []
        tamanho = len(cabecalho) + len(conflito)
        for linha in linhas:
            valores = "(" + ", ".join(literal(linha.get(c)) for c in colunas) + ")"
            if lote and tamanho + len(valores) + 2 > TAM_MAX_SQL:
                self.executar(cabecalho + ", ".join(lote) + conflito)
                lote, tamanho = [], len(cabecalho) + len(conflito)
            lote.append(valores)
            tamanho += len(valores) + 2
        if lote:
            self.executar(cabecalho + ", ".join(lote) + conflito)

        self.escritas += len(linhas)
        log.info("%s: %d linhas gravadas (total na execução: %d)", tabela, len(linhas), self.escritas)
        return len(linhas)


class DestinoSQLite(Destino):
    def __init__(self, caminho: str, **kw):
        super().__init__(**kw)
        self.con = sqlite3.connect(caminho)
        self.con.row_factory = sqlite3.Row

    def executar(self, sql: str) -> None:
        self.con.execute(sql)
        self.con.commit()

    def consultar(self, sql: str, params: list | None = None) -> list[dict]:
        return [dict(r) for r in self.con.execute(sql, params or [])]


class DestinoD1(Destino):
    """D1 via API REST: https://api.cloudflare.com/client/v4/accounts/{conta}/d1/database"""

    def __init__(self, conta: str, token: str, banco: str, **kw):
        super().__init__(**kw)
        self.base = f"https://api.cloudflare.com/client/v4/accounts/{conta}/d1/database"
        self.sessao = requests.Session()
        self.sessao.headers["Authorization"] = f"Bearer {token}"
        self.id = self._achar_banco(banco)

    def _achar_banco(self, nome: str) -> str:
        resp = self.sessao.get(self.base, params={"name": nome}, timeout=60)
        resp.raise_for_status()
        achados = [b for b in resp.json().get("result", []) if b.get("name") == nome]
        if not achados:
            raise SystemExit(
                f"Banco D1 '{nome}' não encontrado. Faça o primeiro deploy do Worker "
                "(ele cria o banco) ou confira o nome em wrangler.jsonc."
            )
        return achados[0]["uuid"]

    def _query(self, sql: str, params: list | None = None) -> list[dict]:
        corpo = {"sql": sql, **({"params": params} if params else {})}
        for tentativa in range(1, 4):
            resp = self.sessao.post(f"{self.base}/{self.id}/query", json=corpo, timeout=120)
            if resp.status_code in (429, 500, 502, 503, 504) and tentativa < 3:
                time.sleep(5 * tentativa)
                continue
            dados = resp.json()
            if not dados.get("success"):
                raise RuntimeError(f"D1 recusou a consulta: {dados.get('errors')} | SQL: {sql[:200]}…")
            return dados["result"][0].get("results", [])
        raise RuntimeError("D1 não respondeu após 3 tentativas")

    def executar(self, sql: str) -> None:
        self._query(sql)

    def consultar(self, sql: str, params: list | None = None) -> list[dict]:
        return self._query(sql, params)
