<div align="center">

# B3 Market Pipeline

**Cotações da B3 e indicadores econômicos em um pipeline de dados.**

![Python](https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white)
![Polars](https://img.shields.io/badge/Polars-CD792C?style=flat-square&logo=polars&logoColor=white)
![yfinance](https://img.shields.io/badge/Cotações-yfinance-6001D2?style=flat-square)
![Cloudflare Workers](https://img.shields.io/badge/Cloudflare_Workers-F38020?style=flat-square&logo=cloudflare&logoColor=white)
![D1](https://img.shields.io/badge/D1-SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white)

**Projeto pessoal · Em desenvolvimento**

</div>

---

## O que é o projeto

Coleta das cotações das principais ações da B3 para comparar o desempenho de cada setor com o Ibovespa e, nas próximas etapas, com indicadores do Banco Central, como Selic, IPCA e dólar.

| Ativo | Setor |
| --- | --- |
| Ibovespa | Índice |
| PETR4 | Petróleo |
| VALE3 | Mineração |
| ITUB4, BBDC4, BBAS3 | Bancos |

## Arquitetura

```
Cron Trigger (dias úteis, 19h) ──► Worker ──► Yahoo Finance
                                     │
                                     ▼
                                 D1 (cotacoes) ◄── API JSON ◄── Power BI / painel
```

| Pasta / arquivo | O que é |
| --- | --- |
| `src/index.js` | Worker: coleta agendada e API de leitura |
| `wrangler.jsonc` | Configuração do Worker, do Cron e do banco D1 |
| `notebooks/app.ipynb` | Exploração e protótipo em Python |

Na primeira execução com o banco vazio, o Worker faz a carga histórica de 5 anos. Depois, cada execução busca os últimos 5 dias e atualiza o que já existe, então rodar mais de uma vez não duplica dados.

### Endpoints

| Rota | Retorno |
| --- | --- |
| `GET /` | Status: total de registros e última data |
| `GET /ultimas` | Último fechamento de cada ativo |
| `GET /cotacoes?ticker=PETR4&inicio=2025-01-01` | Série histórica (filtros opcionais) |
| `POST /coletar` | Dispara a coleta na hora (header `Authorization: Bearer <COLETA_TOKEN>`) |

## Como rodar

**Worker (local):**

```bash
npm install
npm run dev
# em outro terminal, simula o cron:
curl "http://localhost:8787/__scheduled?cron=0+22+*+*+1-5"
```

**Deploy:** cada push na `main` é publicado pelo Workers Builds da Cloudflare. No primeiro deploy o banco D1 `b3-market` é criado automaticamente.

**Notebook:**

```bash
pip install yfinance polars matplotlib jupyter
jupyter notebook notebooks/app.ipynb
```

---

<div align="center">

Projeto de estudo. Não é recomendação de investimento.

</div>
