<div align="center">

# B3 Market Pipeline

**Um filtro das empresas da B3 com 10 anos de balanços, preços e juros, para escolher onde investir e acompanhar as notícias de cada uma.**

![Python](https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white)
![Polars](https://img.shields.io/badge/Polars-CD792C?style=flat-square&logo=polars&logoColor=white)
![yfinance](https://img.shields.io/badge/Cotações-yfinance-6001D2?style=flat-square)
![Cloudflare Workers](https://img.shields.io/badge/Cloudflare_Workers-F38020?style=flat-square&logo=cloudflare&logoColor=white)
![D1](https://img.shields.io/badge/D1-SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white)

**Projeto pessoal · Em desenvolvimento**

</div>

---

## O que é o projeto

A pergunta que o projeto responde: **quais empresas da bolsa são saudáveis, estão a um preço razoável e teriam valido mais a pena do que deixar o dinheiro no CDI?**

O pipeline junta, para cada empresa listada na B3:

- **10 anos de balanços** (lucro, dívida, dividendos), direto dos dados abertos da CVM;
- **preço mensal** da ação, com e sem ajuste por proventos;
- **CDI, Selic e IPCA** do Banco Central, como régua de comparação;
- **fatos relevantes e comunicados** publicados na CVM.

Com isso, o painel filtra as empresas por regras simples (ROE, dívida, preço sobre lucro, dividendos, consistência) e mostra a ficha de cada uma.

## Por que esses dados

| Dado | Fonte | Pergunta que responde |
| --- | --- | --- |
| Lucro, receita, patrimônio | DFP da CVM | A empresa dá lucro todo ano? É eficiente (ROE)? |
| Dívida e caixa | DFP da CVM | A dívida é perigosa (dívida líquida / EBITDA)? |
| Dividendos pagos | DFP da CVM (fluxo de caixa) | Paga dividendo com consistência? Quanto rende por ano? |
| Preço e quantidade de ações | Yahoo Finance + DFP | Está cara ou barata (P/L, P/VP)? |
| CDI | Banco Central | Investir na ação valeu mais do que deixar o dinheiro parado sem risco? |
| Fatos relevantes | IPE da CVM | O que aconteceu com a empresa? (oficial, sai antes da imprensa) |

Balanços vêm da CVM e não de sites de cotação porque é a fonte oficial: toda empresa listada é obrigada a entregar no mesmo formato.

## Arquitetura

```
GitHub Actions (Python)                         Cloudflare
───────────────────────                         ──────────
CVM: cadastro, FCA, DFP, IPE ─┐
Banco Central: CDI, Selic ────┼─► etl/ ──► API REST ──► D1 ◄── Worker (API JSON) ◄── Power BI
Yahoo Finance: preços ────────┘                          ▲
                                     Cron do Worker ─────┘ (cotações diárias dos 6 ativos)
```

O ETL pesado roda no GitHub Actions porque os arquivos da CVM são zips de dezenas de MB, grandes demais para um Worker no plano grátis. O Worker só lê.

| Pasta / arquivo | O que é |
| --- | --- |
| `src/index.js` | Worker: coleta diária das cotações e API de leitura |
| `src/filtro.js` | Rotas do filtro de empresas |
| `etl/` | ETL em Python: fontes, cálculo dos indicadores e gravação no D1 |
| `etl/schema.sql` | Tabelas do D1 |
| `tests/` | Testes do ETL com arquivos no formato da CVM (sem internet) |
| `.github/workflows/etl.yml` | Agendamento do ETL e testes a cada push |
| `notebooks/app.ipynb` | Exploração e protótipo em Python |

### Agendamento

| Quando | Modo | O que faz |
| --- | --- | --- |
| Dias úteis, 19:30 | `diario` | Preço do mês, CDI/Selic/IPCA, notícias dos últimos 10 dias, recalcula indicadores |
| Domingo | `semanal` | O diário + cadastro, tickers e balanços dos últimos 2 anos |
| Uma vez, à mão | `carga-inicial` | 10 anos de tudo |

### Endpoints

Todas as rotas exigem a chave do secret `API_KEY`, enviada como `?key=<chave>` ou no header `Authorization: Bearer <chave>`. Sem o secret configurado, a API responde 401 para tudo.

| Rota | Retorno |
| --- | --- |
| `GET /filtro?roe_min=15&dy_min=6&divida_max=3&anos_lucro_min=8&bate_cdi=1` | Empresas que passam no filtro |
| `GET /empresas` | Empresas listadas (`?todas=1` inclui as que saíram da bolsa) |
| `GET /empresas/PETR4` | Ficha: tickers, indicadores, 10 anos de balanço e últimas notícias (aceita ticker ou CNPJ) |
| `GET /precos?ticker=PETR4` | Preço mensal, 10 anos |
| `GET /macro?serie=cdi&inicio=2020-01-01` | Séries do BC: `cdi`, `selic`, `selic_meta`, `ipca` |
| `GET /noticias?dias=30` | Fatos relevantes e comunicados das empresas listadas |
| `GET /` | Status: total de registros e última data |
| `GET /ultimas` | Último fechamento de cada ativo acompanhado |
| `GET /cotacoes?ticker=PETR4&inicio=2025-01-01` | Série diária dos ativos acompanhados |
| `POST /coletar` | Dispara a coleta diária na hora |

**Parâmetros do `/filtro`** (todos opcionais). Os percentuais vão em %: `roe_min=15` quer dizer ROE de pelo menos 15%.

| Parâmetro | Regra |
| --- | --- |
| `roe_min`, `margem_min`, `dy_min` | ROE, margem líquida e dividend yield mínimos (%) |
| `pl_max`, `pvp_max` | P/L e P/VP máximos |
| `divida_max` | Dívida líquida / EBITDA máxima (não se aplica a bancos) |
| `anos_lucro_min`, `anos_dividendo_min` | Mínimo de anos com lucro ou dividendo, nos últimos 10 |
| `valor_mercado_min` | Valor de mercado mínimo, em R$ |
| `bate_cdi=1` | Só ações que renderam mais que o CDI em 5 anos (com proventos) |
| `financeiras=0` | Exclui bancos e seguradoras |
| `setor` | Parte do nome do setor da CVM |
| `ordem` | `dividend_yield` (padrão), `roe`, `p_l`, `p_vp`, `margem`, `divida`, `valor_mercado`, `retorno` |

### Como os indicadores são calculados

Tudo a partir do último balanço anual (DFP consolidado) e do último preço do ticker principal:

- **ROE** = lucro / patrimônio líquido
- **Margem líquida** = lucro / receita
- **Dívida líquida / EBITDA** = (empréstimos − caixa e aplicações) / (EBIT + depreciação)
- **P/L** = valor de mercado / lucro (vazio se houve prejuízo)
- **P/VP** = valor de mercado / patrimônio líquido
- **Dividend yield** = dividendos e JCP pagos no ano / valor de mercado
- **Valor de mercado** = preço do ticker principal × total de ações (fora tesouraria)
- **Retorno 5 anos** = variação do preço ajustado por proventos, comparado com o CDI acumulado no mesmo período

**Limitações conhecidas:**

- Empresas com ações ON e PN de preços muito diferentes têm valor de mercado aproximado (usa o preço do papel mais negociado). Units entram só se forem o único papel com negociação.
- Dividend yield usa o que foi **pago** no ano (fluxo de caixa), não o que foi anunciado.
- Bancos e seguradoras têm balanço diferente: entram ROE, P/L, P/VP e dividendos, mas não EBITDA nem dívida.
- O histórico de tickers da CVM (FCA) começa em 2018.

## Como rodar

**Worker (local):**

```bash
npm install
npm run dev
# em outro terminal, simula o cron:
curl "http://localhost:8787/__scheduled?cron=0+22+*+*+1-5"
```

**Deploy:** cada push na `main` é publicado pelo Workers Builds da Cloudflare. No primeiro deploy o banco D1 `b3-market` é criado automaticamente.

**ETL (local, num SQLite):**

```bash
pip install -r requirements.txt pytest
python -m pytest tests                          # testes, sem internet
python -m etl carga-inicial --sqlite dev.db     # baixa tudo e grava em dev.db
```

**ETL (no D1, pelo GitHub Actions):**

1. Crie um token em Cloudflare → My Profile → API Tokens, com a permissão **Account · D1 · Edit**.
2. No GitHub, em Settings → Secrets and variables → Actions, crie `CLOUDFLARE_API_TOKEN` e `CLOUDFLARE_ACCOUNT_ID`.
3. Em Actions → *ETL do filtro de empresas* → *Run workflow*, escolha `carga-inicial`.

Depois disso, o diário e o semanal rodam sozinhos.

**Limite do plano grátis do D1:** 100 mil linhas escritas por dia, contando também as escritas em índices. Por isso as tabelas usam `WITHOUT ROWID`, só o ticker principal de cada empresa tem 10 anos de preço, e o ETL para sozinho antes de passar de 90 mil linhas (`--limite`). A carga inicial cabe em um dia; se não couber, ela avisa qual etapa parou e você roda o restante no dia seguinte com `--etapas`.

> O GitHub desativa agendamentos de repositórios públicos sem nenhum commit há 60 dias. Se o ETL parar, é só reativar na aba Actions.

**Notebook:**

```bash
pip install yfinance polars matplotlib jupyter
jupyter notebook notebooks/app.ipynb
```

---

<div align="center">

Projeto de estudo. Não é recomendação de investimento.

</div>
