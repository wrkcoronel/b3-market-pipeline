// Rotas do filtro de empresas. As tabelas são preenchidas pelo ETL em Python
// (pasta etl/, rodado pelo GitHub Actions); o Worker só lê.

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Access-Control-Allow-Origin": "*" },
  });
}

// Tabela ainda não criada = ETL ainda não rodou. Responde 503 com a dica em vez de 500.
async function consultar(env, sql, params = []) {
  try {
    const { results } = await env.DB.prepare(sql).bind(...params).all();
    return results;
  } catch (err) {
    if (/no such table/i.test(err.message)) {
      throw Object.assign(new Error("Dados ainda não carregados: rode o ETL (python -m etl carga-inicial)"), { status: 503 });
    }
    throw err;
  }
}

const numero = (v) => (v === null || v === "" || isNaN(Number(v)) ? null : Number(v));
const pct = (v) => (numero(v) === null ? null : numero(v) / 100);

// Colunas aceitas em ?ordem= (lista fechada: nada do usuário vai direto pro SQL)
const ORDENS = {
  dividend_yield: "dividend_yield DESC",
  roe: "roe DESC",
  p_l: "p_l ASC",
  p_vp: "p_vp ASC",
  margem: "margem_liquida DESC",
  divida: "divida_liq_ebitda ASC",
  valor_mercado: "valor_mercado DESC",
  retorno: "retorno_5a DESC",
};

// GET /filtro?roe_min=15&pl_max=12&dy_min=6&divida_max=3&anos_lucro_min=8&bate_cdi=1
// Percentuais (roe, dy, margem) vão em %, ex.: roe_min=15 = ROE de pelo menos 15%.
async function filtro(url, env) {
  const p = url.searchParams;
  const condicoes = [];
  const params = [];
  const regra = (sql, valor) => {
    if (valor !== null) {
      condicoes.push(sql);
      params.push(valor);
    }
  };

  regra("roe >= ?", pct(p.get("roe_min")));
  regra("margem_liquida >= ?", pct(p.get("margem_min")));
  regra("dividend_yield >= ?", pct(p.get("dy_min")));
  regra("p_l <= ?", numero(p.get("pl_max")));
  regra("p_vp <= ?", numero(p.get("pvp_max")));
  regra("anos_com_lucro >= ?", numero(p.get("anos_lucro_min")));
  regra("anos_com_dividendo >= ?", numero(p.get("anos_dividendo_min")));
  regra("valor_mercado >= ?", numero(p.get("valor_mercado_min")));
  // Bancos não têm dívida/EBITDA: o limite só vale para as não financeiras
  regra("(financeira = 1 OR divida_liq_ebitda <= ?)", numero(p.get("divida_max")));
  if (p.get("setor")) regra("setor LIKE ?", `%${p.get("setor")}%`);
  if (p.get("financeiras") === "0") condicoes.push("financeira = 0");
  if (p.get("bate_cdi") === "1") condicoes.push("retorno_5a > cdi_5a");

  const ordem = ORDENS[p.get("ordem")] ?? ORDENS.dividend_yield;
  const where = condicoes.length ? `WHERE ${condicoes.join(" AND ")}` : "";
  const linhas = await consultar(env, `SELECT * FROM indicadores ${where} ORDER BY ${ordem} NULLS LAST`, params);
  return json({ total: linhas.length, empresas: linhas });
}

// GET /empresas/PETR4 ou /empresas/33000167000101 — ficha completa
async function ficha(chave, env) {
  const alvo = chave.toUpperCase();
  const [empresa] = await consultar(
    env,
    `SELECT e.* FROM empresas e
     WHERE e.cnpj = ?1 OR e.cnpj = (SELECT cnpj FROM tickers WHERE ticker = ?1)`,
    [alvo.replace(/\D/g, "").length === 14 ? alvo.replace(/\D/g, "") : alvo]
  );
  if (!empresa) return json({ erro: "empresa não encontrada" }, 404);

  const [tickers, fundamentos, indicadores, noticias] = await Promise.all([
    consultar(env, "SELECT * FROM tickers WHERE cnpj = ? ORDER BY ticker", [empresa.cnpj]),
    consultar(env, "SELECT * FROM fundamentos WHERE cnpj = ? ORDER BY ano", [empresa.cnpj]),
    consultar(env, "SELECT * FROM indicadores WHERE cnpj = ?", [empresa.cnpj]),
    consultar(env, "SELECT * FROM noticias WHERE cnpj = ? ORDER BY data DESC LIMIT 20", [empresa.cnpj]),
  ]);
  return json({ ...empresa, tickers, indicadores: indicadores[0] ?? null, fundamentos, noticias });
}

export async function rotasFiltro(url, env) {
  const caminho = url.pathname.replace(/\/+$/, "");
  const p = url.searchParams;

  try {
    if (caminho === "/filtro") return await filtro(url, env);

    // GET /empresas?setor=Bancos&todas=1
    if (caminho === "/empresas") {
      const condicoes = p.get("todas") === "1" ? [] : ["listada = 1"];
      const params = [];
      if (p.get("setor")) {
        condicoes.push("setor LIKE ?");
        params.push(`%${p.get("setor")}%`);
      }
      const where = condicoes.length ? `WHERE ${condicoes.join(" AND ")}` : "";
      return json(await consultar(env, `SELECT * FROM empresas ${where} ORDER BY nome`, params));
    }

    const m = caminho.match(/^\/empresas\/([^/]+)$/);
    if (m) return await ficha(decodeURIComponent(m[1]), env);

    // GET /macro?serie=cdi&inicio=2020-01-01
    if (caminho === "/macro") {
      const inicio = p.get("inicio") ?? "1900-01-01";
      const serie = p.get("serie");
      return json(
        await consultar(
          env,
          serie
            ? "SELECT * FROM macro WHERE serie = ? AND mes >= ? ORDER BY mes"
            : "SELECT * FROM macro WHERE mes >= ? ORDER BY serie, mes",
          serie ? [serie, inicio] : [inicio]
        )
      );
    }

    // GET /precos?ticker=PETR4 — histórico mensal (10 anos) do ticker principal
    if (caminho === "/precos") {
      const ticker = (p.get("ticker") ?? "").toUpperCase();
      if (!ticker) return json({ erro: "informe ?ticker=" }, 400);
      return json(await consultar(env, "SELECT * FROM precos_mensais WHERE ticker = ? ORDER BY mes", [ticker]));
    }

    // GET /noticias?dias=30 — fatos relevantes e comunicados das empresas listadas
    if (caminho === "/noticias") {
      const dias = Math.min(Math.max(numero(p.get("dias")) ?? 30, 1), 365);
      return json(
        await consultar(
          env,
          `SELECT n.*, e.nome, e.ticker_principal AS ticker FROM noticias n
           JOIN empresas e ON e.cnpj = n.cnpj
           WHERE n.data >= date('now', ?) ORDER BY n.data DESC LIMIT 500`,
          [`-${dias} days`]
        )
      );
    }
  } catch (err) {
    if (err.status) return json({ erro: err.message }, err.status);
    throw err;
  }
  return null; // não é uma rota do filtro
}

export const ROTAS_FILTRO = [
  "/filtro?roe_min=15&dy_min=6&divida_max=3&anos_lucro_min=8&bate_cdi=1",
  "/empresas",
  "/empresas/PETR4",
  "/precos?ticker=PETR4",
  "/macro?serie=cdi",
  "/noticias?dias=30",
];
