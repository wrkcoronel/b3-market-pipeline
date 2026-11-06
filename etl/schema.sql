-- Tabelas do filtro de empresas (D1 / SQLite).
--
-- WITHOUT ROWID: a chave primária é a própria tabela, sem índice extra.
-- No plano grátis do D1 cada escrita em índice conta como mais uma linha
-- escrita (limite de 100 mil por dia), então isso corta as escritas pela metade.

-- Cadastro da CVM (categoria A: as que podem ter ação na bolsa)
CREATE TABLE IF NOT EXISTS empresas (
  cnpj           TEXT PRIMARY KEY,
  cd_cvm         INTEGER,
  nome           TEXT NOT NULL,
  nome_comercial TEXT,
  setor          TEXT,
  situacao       TEXT,     -- ATIVO, CANCELADA...
  dt_cancel      TEXT,
  financeira     INTEGER NOT NULL DEFAULT 0,  -- banco/seguradora: balanço diferente
  listada        INTEGER NOT NULL DEFAULT 0,  -- ativa + ticker negociando hoje
  ticker_principal TEXT,                      -- o de maior volume
  atualizado_em  TEXT NOT NULL DEFAULT (datetime('now'))
) WITHOUT ROWID;

-- Tickers de ações (FCA da CVM)
CREATE TABLE IF NOT EXISTS tickers (
  ticker TEXT PRIMARY KEY,
  cnpj   TEXT NOT NULL,
  tipo   TEXT,   -- Ações Ordinárias, Preferenciais, Units
  inicio TEXT,
  fim    TEXT    -- vazio = ainda negocia
) WITHOUT ROWID;

-- Balanço anual resumido (DFP consolidado). Valores em R$.
CREATE TABLE IF NOT EXISTS fundamentos (
  cnpj             TEXT NOT NULL,
  ano              INTEGER NOT NULL,
  receita          REAL,
  ebit             REAL,
  depreciacao      REAL,
  ebitda           REAL,
  lucro            REAL,
  patrimonio       REAL,
  ativo            REAL,
  caixa            REAL,
  divida_bruta     REAL,
  divida_liquida   REAL,
  caixa_operacional REAL,
  dividendos_pagos REAL,   -- dividendos + JCP pagos no ano (DFC)
  acoes            REAL,   -- total de ações, fora tesouraria
  PRIMARY KEY (cnpj, ano)
) WITHOUT ROWID;

-- Preço mensal do ticker principal de cada empresa (último pregão do mês).
-- O mês corrente é atualizado todo dia, então ele vale como "preço atual".
CREATE TABLE IF NOT EXISTS precos_mensais (
  ticker      TEXT NOT NULL,
  mes         TEXT NOT NULL,   -- AAAA-MM-01
  fechamento  REAL,            -- preço de tela (para P/L, P/VP)
  ajustado    REAL,            -- ajustado por proventos e desdobramentos (para retorno)
  volume      REAL,
  PRIMARY KEY (ticker, mes)
) WITHOUT ROWID;

-- Séries do Banco Central, mensais
CREATE TABLE IF NOT EXISTS macro (
  serie TEXT NOT NULL,   -- cdi, selic, selic_meta, ipca
  mes   TEXT NOT NULL,   -- AAAA-MM-01
  valor REAL,
  PRIMARY KEY (serie, mes)
) WITHOUT ROWID;

-- Fatos relevantes e comunicados ao mercado (IPE da CVM)
CREATE TABLE IF NOT EXISTS noticias (
  protocolo  TEXT PRIMARY KEY,
  cnpj       TEXT NOT NULL,
  data       TEXT NOT NULL,   -- data de entrega
  categoria  TEXT,
  assunto    TEXT,
  link       TEXT
) WITHOUT ROWID;

-- Uma linha por empresa listada: o que o filtro do painel consulta.
-- Recalculado pelo ETL a cada execução.
CREATE TABLE IF NOT EXISTS indicadores (
  cnpj            TEXT PRIMARY KEY,
  ticker          TEXT,
  nome            TEXT,
  setor           TEXT,
  financeira      INTEGER,
  ano_balanco     INTEGER,
  preco           REAL,
  valor_mercado   REAL,
  roe             REAL,
  margem_liquida  REAL,
  divida_liq_ebitda REAL,
  p_l             REAL,
  p_vp            REAL,
  dividend_yield  REAL,
  anos_com_lucro  INTEGER,   -- nos últimos 10 balanços
  anos_com_dividendo INTEGER,
  anos_de_balanco INTEGER,
  retorno_5a      REAL,      -- ação, com proventos
  cdi_5a          REAL,      -- CDI no mesmo período
  atualizado_em   TEXT NOT NULL DEFAULT (datetime('now'))
) WITHOUT ROWID;
