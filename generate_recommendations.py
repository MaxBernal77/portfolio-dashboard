"""
generate_recommendations.py
Corre cada dia habil a las 8:30am Bogota via GitHub Actions.
- Lunes: genera recommendations.json completo + envia alerta Telegram
- Martes-Viernes: envia solo la alerta Telegram diaria
"""

import json, os, requests
from datetime import datetime
import anthropic

# ── PORTAFOLIO ────────────────────────────────────────────────────────────────
PORTFOLIO = [
    {"ticker": "EC",   "name": "Ecopetrol",       "qty": 110, "avg_cost": 15.19,   "type": "stock",  "sector": "Energia"},
    {"ticker": "EIMI", "name": "MSCI EM IMI ETF",  "qty": 25,  "avg_cost": 50.948,  "type": "etf",    "sector": "Emergentes"},
    {"ticker": "ETHE", "name": "Ethereum Trust",   "qty": 5,   "avg_cost": 31.40,   "type": "crypto", "sector": "Crypto"},
    {"ticker": "GBTC", "name": "Bitcoin Trust",    "qty": 9,   "avg_cost": 71.38,   "type": "crypto", "sector": "Crypto"},
    {"ticker": "NFLX", "name": "Netflix",          "qty": 3,   "avg_cost": 126.267, "type": "stock",  "sector": "Comunicaciones"},
    {"ticker": "NTR",  "name": "Nutrien",          "qty": 10,  "avg_cost": 74.609,  "type": "stock",  "sector": "Materiales"},
]
OPTIONS_OPEN = [
    {"desc": "EC Jul17'26 $17 CALL SHORT",   "pos": -1, "avg": 0.699, "strike": 17, "exp": "2026-07-17"},
    {"desc": "NTR Aug21'26 $62.5 PUT SHORT", "pos": -1, "avg": 2.37,  "strike": 62.5,"exp": "2026-08-21"},
]

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(message):
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram no configurado, saltando...")
        return
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=data, timeout=10)
        if r.status_code == 200:
            print("✅ Telegram enviado")
        else:
            print(f"❌ Telegram error: {r.text}")
    except Exception as e:
        print(f"❌ Telegram error: {e}")

# ── PRECIOS ───────────────────────────────────────────────────────────────────
def get_prices():
    prices = {}
    try:
        tickers = [p["ticker"] for p in PORTFOLIO] + ["^GSPC", "^IXIC", "^VIX", "GLD"]
        url     = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={','.join(tickers)}"
        headers = {"User-Agent": "Mozilla/5.0"}
        r       = requests.get(url, headers=headers, timeout=10)
        for q in r.json()["quoteResponse"]["result"]:
            prices[q["symbol"]] = {
                "price":    q.get("regularMarketPrice", 0),
                "chg_pct":  q.get("regularMarketChangePercent", 0),
                "pre":      q.get("preMarketPrice"),
                "pre_chg":  q.get("preMarketChangePercent"),
            }
    except Exception as e:
        print(f"Warning precios: {e}")
    return prices

def get_crypto():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true",
            timeout=8
        )
        return r.json()
    except:
        return {}

# ── CONTEXTO ──────────────────────────────────────────────────────────────────
def build_context(prices, crypto):
    lines = ["PORTAFOLIO:\n"]
    total_value = total_cost = 0

    for p in PORTFOLIO:
        t     = p["ticker"]
        price = prices.get(t, {}).get("price") or p["avg_cost"]
        chg   = prices.get(t, {}).get("chg_pct", 0)
        mval  = price * p["qty"]
        pnl   = (price - p["avg_cost"]) * p["qty"]
        pnlp  = ((price - p["avg_cost"]) / p["avg_cost"]) * 100
        total_value += mval
        total_cost  += p["avg_cost"] * p["qty"]
        lines.append(f"- {t}: {p['qty']} acc @ ${p['avg_cost']:.2f} | ahora ${price:.2f} ({chg:+.1f}%) | P&L ${pnl:+.0f} ({pnlp:+.1f}%)")

    total_pnl  = total_value - total_cost
    total_pnlp = (total_pnl / total_cost) * 100 if total_cost else 0
    lines.append(f"\nTOTAL: valor ${total_value:.0f} | P&L ${total_pnl:+.0f} ({total_pnlp:+.1f}%)")

    spx = prices.get("^GSPC", {}); ndx = prices.get("^IXIC", {})
    btc = crypto.get("bitcoin",  {}).get("usd", "N/A")
    eth = crypto.get("ethereum", {}).get("usd", "N/A")
    btc_chg = crypto.get("bitcoin",  {}).get("usd_24h_change", 0)
    lines.append(f"\nMERCADO: S&P {spx.get('price','?')} ({spx.get('chg_pct',0):+.1f}%) | Nasdaq {ndx.get('price','?')} ({ndx.get('chg_pct',0):+.1f}%) | BTC ${btc} ({btc_chg:+.1f}%) | ETH ${eth}")

    lines.append("\nOPCIONES ABIERTAS:")
    for o in OPTIONS_OPEN:
        days = (datetime.strptime(o["exp"], "%Y-%m-%d") - datetime.now()).days
        lines.append(f"- {o['desc']}: {days} dias para vencer")

    return "\n".join(lines), total_pnl, total_pnlp, spx, btc, btc_chg

# ── ALERTA DIARIA TELEGRAM ────────────────────────────────────────────────────
ALERT_SYSTEM = """Eres un asesor financiero senior. Analiza el portafolio y mercado y genera
una alerta diaria concisa para Telegram en español. Responde SOLO con el texto del mensaje,
sin markdown complejo, usando solo emojis y saltos de linea. Maximo 25 lineas.

Estructura obligatoria:
📊 ALERTA PREMERCADO [fecha]
──────────────────
🌐 MERCADO
[S&P, Nasdaq, BTC con variacion]

💼 PORTAFOLIO HOY
[P&L del dia estimado y posiciones clave que se mueven]

⚠️ OPCIONES
[estado de las opciones abiertas y urgencia si la hay]

🎯 ACCION DEL DIA
[UNA sola accion concreta y especifica, con si/no claro]
[Si hay una decision pendiente como la NFLX Put, dar señal clara: EJECUTAR / ESPERAR / NO EJECUTAR con razon en una linea]

📅 EVENTO CLAVE HOY
[si hay evento macro importante]"""

def generate_daily_alert(context, total_pnl, total_pnlp, spx, btc, btc_chg):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today  = datetime.now().strftime("%A %d de %B de %Y")

    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        system=ALERT_SYSTEM,
        messages=[{"role": "user", "content": f"""Hoy es {today}.

{context}

DECISION PENDIENTE: Vender NFLX $72.5 PUT Ago 2026 (~$200-220 prima).
NFLX precio actual: {spx.get('price','?')} (S&P referencia).
Da señal clara EJECUTAR / ESPERAR / NO EJECUTAR basado en condiciones actuales.

Contexto adicional:
- CPI Mayo 2026 se publica manana (consenso 4.2% YoY - dato caliente esperado)
- FOMC reunion 17-18 jun
- BTC: ${btc} ({btc_chg:+.1f}% 24h)
- Opciones NTR Put $62.5 y EC Call $17 abiertas"""}]
    )
    return msg.content[0].text

# ── RECOMENDACIONES SEMANALES (solo lunes) ────────────────────────────────────
WEEKLY_SYSTEM = """Eres un asesor financiero senior con 20+ anos de experiencia.
Genera recomendaciones semanales en JSON valido, sin texto adicional ni backticks.
{
  "recs": [{"type":"action|rotate|add|hold","icon":"emoji","title":"titulo","badge":"br|by|bb|bg","badgeText":"texto","body":"2-3 oraciones","tags":["TAG"]}],
  "hyps": [{"id":"id","cls":"semi|agri|hedge|stream|btc|ec","icon":"emoji","name":"nombre","riskLbl":"Bajo|Medio|Alto|Muy alto","risk":0-100,"riskColor":"#hex","horizon":"X meses","tickers":["T"],"thesis":"tesis","directo":"entrada directa","opciones":"estrategia opciones con strikes","sizing":"sizing","catalizadores":"eventos"}]
}
Genera exactamente 4 recs y 6 hyps. Se especifico con tickers, precios de entrada y estrategias de opciones."""

def generate_weekly_recs(context):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today  = datetime.now().strftime("%A %d de %B de %Y")

    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=WEEKLY_SYSTEM,
        messages=[{"role": "user", "content": f"Hoy es {today}.\n\n{context}\n\nContexto: S&P YTD +8.2%, objetivo alfa S&P+1pt, buying power ~$9,116, CPI mayo esta semana, FOMC 17-18 jun."}]
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw)

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    today     = datetime.now()
    is_monday = today.weekday() == 0

    print("Obteniendo precios...")
    prices = get_prices()
    crypto = get_crypto()

    print("Construyendo contexto...")
    context, total_pnl, total_pnlp, spx, btc, btc_chg = build_context(prices, crypto)
    print(context)

    print("\nGenerando alerta diaria...")
    alert = generate_daily_alert(context, total_pnl, total_pnlp, spx, btc, btc_chg)
    print("\n--- MENSAJE TELEGRAM ---")
    print(alert)
    send_telegram(alert)

    if is_monday:
        print("\nEs lunes - generando recomendaciones semanales...")
        data = generate_weekly_recs(context)
        data["generated"] = today.isoformat()
        data["week"]      = today.strftime("%Y-%m-%d")
        with open("recommendations.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"✅ recommendations.json generado: {len(data['recs'])} recs, {len(data['hyps'])} hipotesis")
    else:
        print(f"\nHoy es {today.strftime('%A')} - solo alerta diaria, sin recomendaciones semanales")
