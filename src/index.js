// B3 Market Pipeline — Worker
// - scheduled: coleta as cotações diárias no Yahoo Finance e grava no D1
// - fetch: expõe os dados em JSON para consumo (Power BI, front, etc.)

const TICKERS = {
  Ibovespa: "^BVSP",
  VALE3: "VALE3.SA",
  PETR4: "PETR4.SA",
  ITUB4: "ITUB4.SA",
  BBDC4: "BBDC4.SA",
  BBAS3: "BBAS3.SA",
};

const SCHEMA = `CREATE TABLE IF NOT EXISTS cotacoes (
  ticker     TEXT NOT NULL,
  data       TEXT NOT NULL,
  abertura   REAL,
  maxima     REAL,
  minima     REAL,
  fechamento REAL,
  volume     INTEGER,
  coletado_em TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (ticker, data)
)`;

const UPSERT = `INSERT INTO cotacoes (ticker, data, abertura, maxima, minima, fechamento, volume)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (ticker, data) DO UPDATE SET
  abertura = excluded.abertura,
  maxima = excluded.maxima,
  minima = excluded.minima,
  fechamento = excluded.fechamento,
  volume = excluded.volume,
  coletado_em = datetime('now')`;

async function garantirTabela(db) {
  await db.prepare(SCHEMA).run();
}

// Busca o histórico diário de um símbolo no endpoint de gráfico do Yahoo Finance
async function buscarCotacoes(simbolo, periodo) {
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(simbolo)}?range=${periodo}&interval=1d`;
  const resp = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0 (b3-market-pipeline)" } });
  if (!resp.ok) throw new Error(`${simbolo}: HTTP ${resp.status}`);

  const json = await resp.json();
  const r = json?.chart?.result?.[0];
  if (!r?.timestamp) throw new Error(`${simbolo}: resposta sem dados`);

  const q = r.indicators.quote[0];
  const offset = r.meta.gmtoffset ?? -10800; // horário de Brasília

  return r.timestamp
    .map((ts, i) => ({
      data: new Date((ts + offset) * 1000).toISOString().slice(0, 10),
      abertura: q.open[i],
      maxima: q.high[i],
      minima: q.low[i],
      fechamento: q.close[i],
      volume: q.volume[i],
    }))
    .filter((l) => l.fechamento != null);
}

async function coletar(env) {
  await garantirTabela(env.DB);

  // Banco vazio: carga histórica de 5 anos. Depois disso, só os últimos dias.
  const { n } = await env.DB.prepare("SELECT COUNT(*) AS n FROM cotacoes").first();
  const periodo = n === 0 ? "5y" : "5d";

  const resumo = {};
  for (const [nome, simbolo] of Object.entries(TICKERS)) {
    try {
      const linhas = await buscarCotacoes(simbolo, periodo);
      const stmt = env.DB.prepare(UPSERT);
      const binds = linhas.map((l) =>
        stmt.bind(nome, l.data, l.abertura, l.maxima, l.minima, l.fechamento, l.volume)
      );
      // D1 aceita batches grandes, mas dividir mantém cada chamada leve
      for (let i = 0; i < binds.length; i += 500) {
        await env.DB.batch(binds.slice(i, i + 500));
      }
      resumo[nome] = linhas.length;
    } catch (err) {
      console.error(`Falha ao coletar ${nome}:`, err.message);
      resumo[nome] = `erro: ${err.message}`;
    }
  }
  console.log(`Coleta (${periodo}) concluída`, JSON.stringify(resumo));
  return { periodo, resumo };
}

function autorizado(request, url, env) {
  if (!env.API_KEY) return false;
  const enviada =
    url.searchParams.get("key") ??
    request.headers.get("Authorization")?.replace(/^Bearer\s+/i, "") ??
    "";
  const a = new TextEncoder().encode(enviada);
  const b = new TextEncoder().encode(env.API_KEY);
  // Comparação em tempo constante para não vazar a chave por timing
  return a.byteLength === b.byteLength && crypto.subtle.timingSafeEqual(a, b);
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Access-Control-Allow-Origin": "*" },
  });
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(coletar(env));
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    // Toda rota exige a chave (secret API_KEY), via ?key=... ou header Authorization: Bearer ...
    // Sem o secret configurado, a API fica fechada.
    if (!autorizado(request, url, env)) return json({ erro: "não autorizado" }, 401);

    await garantirTabela(env.DB);

    // POST /coletar — dispara a coleta na hora
    if (url.pathname === "/coletar" && request.method === "POST") {
      return json(await coletar(env));
    }

    // GET /cotacoes?ticker=PETR4&inicio=2025-01-01
    if (url.pathname === "/cotacoes") {
      const ticker = url.searchParams.get("ticker");
      const inicio = url.searchParams.get("inicio") ?? "1900-01-01";
      const sql = ticker
        ? "SELECT * FROM cotacoes WHERE ticker = ? AND data >= ? ORDER BY data"
        : "SELECT * FROM cotacoes WHERE data >= ? ORDER BY ticker, data";
      const params = ticker ? [ticker, inicio] : [inicio];
      const { results } = await env.DB.prepare(sql).bind(...params).all();
      return json(results);
    }

    // GET /ultimas — último fechamento de cada ticker
    if (url.pathname === "/ultimas") {
      const { results } = await env.DB.prepare(
        `SELECT c.* FROM cotacoes c
         JOIN (SELECT ticker, MAX(data) AS data FROM cotacoes GROUP BY ticker) u
           ON u.ticker = c.ticker AND u.data = c.data
         ORDER BY c.ticker`
      ).all();
      return json(results);
    }

    if (url.pathname === "/") {
      const contar = () =>
        env.DB.prepare("SELECT COUNT(*) AS n, MAX(data) AS ultima FROM cotacoes").first();
      let { n, ultima } = await contar();

      // Banco vazio: faz a carga inicial na hora, sem esperar o cron.
      // Só acontece enquanto não houver nenhum registro, e o upsert não duplica.
      let carga_inicial;
      if (n === 0) {
        carga_inicial = await coletar(env);
        ({ n, ultima } = await contar());
      }

      return json({
        projeto: "b3-market-pipeline",
        registros: n,
        ultima_data: ultima,
        ...(carga_inicial && { carga_inicial }),
        endpoints: ["/cotacoes?ticker=PETR4&inicio=2025-01-01", "/ultimas"],
      });
    }

    return json({ erro: "não encontrado" }, 404);
  },
};
