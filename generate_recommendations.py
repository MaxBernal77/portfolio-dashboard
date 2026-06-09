"""
generate_recommendations.py
Llama a Claude con el portafolio actual y genera recommendations.json
para el dashboard. Corre cada lunes vía GitHub Actions.
"""

import json, os, requests
from datetime import datetime
import anthropic

# ── PORTAFOLIO (actualizar cuando cambien las posiciones) ────────────────────
PORTFOLIO = [
    {"ticker": "EC",   "name": "Ecopetrol",      "qty": 110, "avg_cost": 15.19,  "type": "stock",  "sector": "Energia"},
    {"ticker": "EIMI", "name": "MSCI EM IMI ETF", "qty": 25,  "avg_cost": 50.948, "type": "etf",    "sector": "Emergentes"},
    {"ticker": "ETHE", "name": "Ethereum Trust",  "qty": 5,   "avg_cost": 31.40,  "type": "crypto", "sector": "Crypto"},
    {"ticker": "GBTC", "name": "Bitcoin Trust",   "qty": 9,   "avg_cost": 71.38,  "type": "crypto", "sector": "Crypto"},
    {"ticker": "NFLX", "name": "Netflix",          "qty": 3,   "avg_cost": 126.267,"type": "stock",  "sector": "Comunicaciones"},
    {"ticker": "NTR",  "name": "Nutrien",          "qty": 10,  "avg_cost": 74.609, "type": "stock",  "sector": "Materiales"},
]

OPTIONS = [
    {"desc": "EC Jul17'26 $17 CALL",    "pos": -1, "avg": 0.699, "exp": "2026-07-17", "strike": 17, "side": "short"},
    {"desc": "GBTC Jul02'26 $51 CALL",  "pos":  1, "avg": 1.682, "exp": "2026-07-02", "strike": 51, "side": "long"},
]

# ── OBTENER PRECIOS ACTUALES ──────────────────────────────────────────────────
def get_prices():
    tickers = [p["ticker"] for p in PORTFOLIO]
    prices = {}
    try:
        syms = ",".join(tickers)
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={syms}"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        quotes = r.json()["quoteResponse"]["result"]
        for q in quotes:
            sym = q.get("symbol")
            prices[sym] = {
                "price": q.get("regularMarketPrice", 0),
                "chg_pct": q.get("regularMarketChangePercent", 0),
                "pre_price": q.get("preMarketPrice"),
                "post_price": q.get("postMarketPrice"),
            }
    except Exception as e:
        print(f"Warning: no se pudieron obtener precios: {e}")
    return prices

def get_crypto():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true", timeout=8)
        return r.json()
    except:
        return {}

# ── CONSTRUIR CONTEXTO PARA CLAUDE ───────────────────────────────────────────
def build_context(prices, crypto):
    lines = ["PORTAFOLIO ACTUAL:\n"]
    total_value = 0
    total_cost = 0

    for p in PORTFOLIO:
        t = p["ticker"]
        if t in ["GBTC", "ETHE"]:
            # usar precio de cripto si está disponible
            c_price = crypto.get("bitcoin", {}).get("usd") if t == "GBTC" else crypto.get("ethereum", {}).get("usd")
            price_info = prices.get(t, {})
            price = price_info.get("price", p["avg_cost"])
        else:
            price_info = prices.get(t, {})
            price = price_info.get("price", p["avg_cost"])

        mkt_val = price * p["qty"]
        pnl = (price - p["avg_cost"]) * p["qty"]
        pnl_pct = ((price - p["avg_cost"]) / p["avg_cost"]) * 100
        total_value += mkt_val
        total_cost += p["avg_cost"] * p["qty"]

        lines.append(f"- {t} ({p['name']}): {p['qty']} acc @ avg ${p['avg_cost']:.2f} | precio actual ${price:.2f} | P&L ${pnl:+.2f} ({pnl_pct:+.1f}%) | sector: {p['sector']}")

    lines.append(f"\nOPCIONES ACTIVAS:")
    for o in OPTIONS:
        lines.append(f"- {o['desc']}: pos {o['pos']:+d}, avg ${o['avg']:.3f}, lado {o['side']}")

    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost) * 100 if total_cost else 0
    lines.append(f"\nRESUMEN: Valor total ${total_value:.2f} | P&L total ${total_pnl:+.2f} ({total_pnl_pct:+.1f}%) | Net liq ~$3,894")

    btc_price = crypto.get("bitcoin", {}).get("usd", "N/A")
    eth_price = crypto.get("ethereum", {}).get("usd", "N/A")
    lines.append(f"\nCRIPTO: BTC=${btc_price} | ETH=${eth_price}")

    return "\n".join(lines)

# ── LLAMAR A CLAUDE ───────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Eres un asesor financiero senior con 20+ años de experiencia.
Tu objetivo es generar recomendaciones semanales para un portafolio de inversión cuyo 
objetivo es superar el rendimiento del S&P 500 por al menos 1 punto porcentual (alfa > S&P+1pt).

Responde SOLO con un JSON válido, sin texto adicional, sin comillas de código.
El JSON debe tener exactamente esta estructura:
{
  "recs": [
    {
      "type": "action|rotate|add|hold",
      "icon": "emoji",
      "title": "titulo corto",
      "badge": "br|by|bb|bg",
      "badgeText": "texto del badge",
      "body": "explicacion de 2-3 oraciones",
      "tags": ["TAG1", "TAG2"]
    }
  ],
  "hyps": [
    {
      "id": "id_unico",
      "cls": "semi|agri|hedge|stream|btc|ec",
      "icon": "emoji",
      "name": "Nombre de la hipotesis",
      "riskLbl": "Bajo|Medio|Alto|Muy alto",
      "risk": 0-100,
      "riskColor": "#hex",
      "horizon": "X semanas/meses",
      "tickers": ["TICK1","TICK2"],
      "thesis": "tesis de inversion en 2-3 oraciones",
      "directo": "entrada directa con precio, stop y target",
      "opciones": "estrategia de opciones con strikes y primas estimadas",
      "sizing": "cuanto capital usar",
      "catalizadores": "eventos clave separados por -"
    }
  ]
}

Genera exactamente 4 recs y 6 hyps. Basa el analisis en el estado actual del portafolio, 
el contexto de mercado reciente, y eventos proximos. Se especifico con tickers, precios de entrada, 
estrategias de opciones (con strikes y primas estimadas), y sizing del buying power disponible (~$10,692)."""

def generate_with_claude(context):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_message = f"""Hoy es {datetime.now().strftime('%A %d de %B de %Y')}.

{context}

Contexto de mercado esta semana:
- S&P 500 YTD: +8.2%
- FOMC próximo: reunión 17-18 jun 2026 (sin recorte esperado)
- CPI Mayo 2026: publicación esta semana (11-12 jun)
- Bitcoin: consolidando en zona $60-63K
- Sector IA/semis: fuerte, datacenter spending sigue acelerando
- Geopolítica: tensiones Iran afectan cadena de fertilizantes (positivo para NTR)

Genera las recomendaciones semanales en el formato JSON solicitado."""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}]
    )

    raw = message.content[0].text.strip()
    # limpiar posibles bloques de codigo
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Obteniendo precios de mercado...")
    prices = get_prices()
    crypto = get_crypto()

    print("Construyendo contexto del portafolio...")
    context = build_context(prices, crypto)
    print(context)

    print("\nLlamando a Claude para generar recomendaciones...")
    data = generate_with_claude(context)

    data["generated"] = datetime.now().isoformat()
    data["week"] = datetime.now().strftime("%Y-%m-%d")

    with open("recommendations.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\nrecommendations.json generado con {len(data['recs'])} recs y {len(data['hyps'])} hipotesis.")
