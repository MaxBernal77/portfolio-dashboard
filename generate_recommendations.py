import json
import os
import requests
from datetime import datetime
import anthropic

import xml.etree.ElementTree as ET

# ── IBKR FLEX ─────────────────────────────────────────────────────────────────
def get_ibkr_positions():
    token    = os.environ.get("IBKR_FLEX_TOKEN")
    query_id = os.environ.get("IBKR_FLEX_QUERY_ID")
    if not token or not query_id:
        print("IBKR Flex no configurado, usando portafolio hardcodeado")
        return None

    try:
        # Paso 1: solicitar el reporte
        url1 = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest"
        r1   = requests.get(url1, params={"t": token, "q": query_id, "v": "3"}, timeout=15)
        root1 = ET.fromstring(r1.text)
        ref   = root1.findtext("ReferenceCode")
        if not ref:
            print("IBKR Flex: no se obtuvo ReferenceCode. Respuesta: " + r1.text[:200])
            return None
        print("IBKR Flex ReferenceCode: " + ref)

        # Paso 2: descargar el reporte (esperar hasta 30s)
        import time
        url2 = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"
        for attempt in range(6):
            time.sleep(5)
            r2   = requests.get(url2, params={"t": token, "q": ref, "v": "3"}, timeout=15)
            root2 = ET.fromstring(r2.text)
            status = root2.findtext("Status")
            if status == "Complete" or root2.tag == "FlexQueryResponse":
                break
            print("IBKR Flex esperando... intento " + str(attempt + 1))

        # Paso 3: parsear posiciones
        positions = []
        for pos in root2.iter("OpenPosition"):
            symbol     = pos.get("symbol", "")
            asset      = pos.get("assetCategory", "STK")
            qty        = float(pos.get("position", 0))
            avg_cost   = float(pos.get("costBasisPrice", 0))
            mark_price = float(pos.get("markPrice", 0))
            description= pos.get("description", symbol)
            strike     = pos.get("strike", "")
            expiry     = pos.get("expiry", "")
            put_call   = pos.get("putCall", "")
            unreal_pnl = float(pos.get("fifoPnlUnrealized", 0))

            if not symbol:
                continue

            positions.append({
                "ticker":      symbol,
                "name":        description,
                "qty":         qty,
                "avg_cost":    avg_cost,
                "mark_price":  mark_price,
                "unrealized":  unreal_pnl,
                "type":        "crypto" if symbol in ["GBTC","ETHE"] else ("etf" if asset == "ETF" else "stock"),
                "asset_class": asset,
                "strike":      strike,
                "expiry":      expiry,
                "put_call":    put_call,
            })

        print("IBKR Flex: " + str(len(positions)) + " posiciones obtenidas")
        return positions if positions else None

    except Exception as e:
        print("IBKR Flex error: " + str(e))
        return None


    {"ticker": "EC",   "name": "Ecopetrol",       "qty": 110, "avg_cost": 15.19,   "type": "stock",  "sector": "Energia"},
    {"ticker": "EIMI", "name": "MSCI EM IMI ETF",  "qty": 25,  "avg_cost": 50.948,  "type": "etf",    "sector": "Emergentes"},
    {"ticker": "ETHE", "name": "Ethereum Trust",   "qty": 5,   "avg_cost": 31.40,   "type": "crypto", "sector": "Crypto"},
    {"ticker": "GBTC", "name": "Bitcoin Trust",    "qty": 9,   "avg_cost": 71.38,   "type": "crypto", "sector": "Crypto"},
    {"ticker": "NFLX", "name": "Netflix",          "qty": 3,   "avg_cost": 126.267, "type": "stock",  "sector": "Comunicaciones"},
    {"ticker": "NTR",  "name": "Nutrien",          "qty": 10,  "avg_cost": 74.609,  "type": "stock",  "sector": "Materiales"},
]

OPTIONS_OPEN = [
    {"desc": "EC Jul17 $17 CALL SHORT",    "pos": -1, "avg": 0.699, "strike": 17,   "exp": "2026-07-17"},
    {"desc": "NTR Aug21 $62.5 PUT SHORT",  "pos": -1, "avg": 2.37,  "strike": 62.5, "exp": "2026-08-21"},
]

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(message):
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram no configurado, saltando...")
        return
    url  = "https://api.telegram.org/bot" + token + "/sendMessage"
    data = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=data, timeout=10)
        if r.status_code == 200:
            print("Telegram enviado OK")
        else:
            print("Telegram error: " + r.text)
    except Exception as e:
        print("Telegram error: " + str(e))

# ── PRECIOS ───────────────────────────────────────────────────────────────────
def get_prices():
    prices = {}
    try:
        tickers = [p["ticker"] for p in PORTFOLIO] + ["^GSPC", "^IXIC", "^VIX", "GLD"]
        url     = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=" + ",".join(tickers)
        headers = {"User-Agent": "Mozilla/5.0"}
        r       = requests.get(url, headers=headers, timeout=10)
        for q in r.json()["quoteResponse"]["result"]:
            prices[q["symbol"]] = {
                "price":   q.get("regularMarketPrice", 0),
                "chg_pct": q.get("regularMarketChangePercent", 0),
                "pre":     q.get("preMarketPrice"),
                "pre_chg": q.get("preMarketChangePercent"),
            }
    except Exception as e:
        print("Warning precios: " + str(e))
    return prices

def get_crypto():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true"
        r   = requests.get(url, timeout=8)
        return r.json()
    except Exception:
        return {}

# ── CONTEXTO ──────────────────────────────────────────────────────────────────
def build_context(prices, crypto):
    lines       = ["PORTAFOLIO:\n"]
    total_value = 0
    total_cost  = 0

    for p in PORTFOLIO:
        t     = p["ticker"]
        price = (prices.get(t) or {}).get("price") or p["avg_cost"]
        chg   = (prices.get(t) or {}).get("chg_pct", 0)
        mval  = price * p["qty"]
        pnl   = (price - p["avg_cost"]) * p["qty"]
        pnlp  = ((price - p["avg_cost"]) / p["avg_cost"]) * 100
        total_value += mval
        total_cost  += p["avg_cost"] * p["qty"]
        lines.append(
            "- " + t + ": " + str(p["qty"]) + " acc @ $" + str(round(p["avg_cost"], 2)) +
            " | ahora $" + str(round(price, 2)) + " (" + str(round(chg, 1)) + "%)" +
            " | P&L $" + str(round(pnl, 0)) + " (" + str(round(pnlp, 1)) + "%)"
        )

    total_pnl  = total_value - total_cost
    total_pnlp = (total_pnl / total_cost * 100) if total_cost else 0
    lines.append(
        "\nTOTAL: valor $" + str(round(total_value, 0)) +
        " | P&L $" + str(round(total_pnl, 0)) +
        " (" + str(round(total_pnlp, 1)) + "%)"
    )

    spx     = prices.get("^GSPC") or {}
    ndx     = prices.get("^IXIC") or {}
    btc     = (crypto.get("bitcoin")  or {}).get("usd", "N/A")
    eth     = (crypto.get("ethereum") or {}).get("usd", "N/A")
    btc_chg = (crypto.get("bitcoin")  or {}).get("usd_24h_change", 0)
    lines.append(
        "\nMERCADO: S&P " + str(spx.get("price","?")) + " (" + str(round(spx.get("chg_pct",0),1)) + "%)" +
        " | Nasdaq " + str(ndx.get("price","?")) +
        " | BTC $" + str(btc) + " (" + str(round(btc_chg,1)) + "%) | ETH $" + str(eth)
    )

    lines.append("\nOPCIONES ABIERTAS:")
    for o in OPTIONS_OPEN:
        days_left = (datetime.strptime(o["exp"], "%Y-%m-%d") - datetime.now()).days
        lines.append("- " + o["desc"] + ": " + str(days_left) + " dias para vencer")

    context_str = "\n".join(lines)
    return context_str, total_pnl, total_pnlp, spx, btc, btc_chg

# ── DIAS ESTRATEGICOS ─────────────────────────────────────────────────────────
STRATEGIC_PROMPT = (
    "Eres un analista financiero senior. Dado el contexto del mercado y el calendario "
    "economico, decide cuales son los 3 dias mas estrategicos para regenerar las "
    "recomendaciones del portafolio esta semana. "
    "Responde SOLO con JSON valido sin texto adicional: "
    '{"strategic_days": [1, 3, 5], "reasoning": "razon breve"} '
    "Donde 1=Lunes, 2=Martes, 3=Miercoles, 4=Jueves, 5=Viernes. "
    "Siempre incluye el lunes. Prioriza dias con eventos macro importantes."
)

def get_strategic_days(context_str):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today  = datetime.now().strftime("%A %d de %B de %Y")
    msg    = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 200,
        system     = STRATEGIC_PROMPT,
        messages   = [{"role": "user", "content":
            "Hoy es " + today + ".\n\n" + context_str +
            "\n\nElige los 3 dias estrategicos de esta semana para regenerar recomendaciones."
        }]
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    result = json.loads(raw)
    return result["strategic_days"], result["reasoning"]

STRATEGIC_DAYS_FILE = "strategic_days.json"

def load_or_compute_strategic_days(today, context_str):
    is_monday = today.weekday() == 0
    if is_monday or not os.path.exists(STRATEGIC_DAYS_FILE):
        print("Calculando dias estrategicos de la semana...")
        days, reasoning = get_strategic_days(context_str)
        cache = {
            "week_start":     today.strftime("%Y-%m-%d"),
            "strategic_days": days,
            "reasoning":      reasoning
        }
        with open(STRATEGIC_DAYS_FILE, "w") as f:
            json.dump(cache, f, indent=2)
        print("Dias estrategicos: " + str(days) + " - " + reasoning)
        return days
    else:
        with open(STRATEGIC_DAYS_FILE) as f:
            cache = json.load(f)
        print("Dias estrategicos esta semana: " + str(cache["strategic_days"]) + " - " + cache["reasoning"])
        return cache["strategic_days"]

def should_update_recommendations(today, context_str):
    strategic_days = load_or_compute_strategic_days(today, context_str)
    current_day    = today.weekday() + 1
    return current_day in strategic_days

# ── ALERTA DIARIA ─────────────────────────────────────────────────────────────
ALERT_PROMPT = (
    "Eres un asesor financiero senior. Genera una alerta diaria concisa para Telegram "
    "en espanol usando solo emojis y saltos de linea. Maximo 25 lineas. "
    "Estructura obligatoria:\n"
    "ALERTA PREMERCADO [fecha]\n"
    "MERCADO: S&P, Nasdaq, BTC con variacion\n"
    "PORTAFOLIO HOY: P&L estimado y posiciones clave\n"
    "OPCIONES: estado de opciones abiertas\n"
    "ACCION DEL DIA: UNA accion concreta. Si hay decision pendiente dar EJECUTAR / ESPERAR / NO EJECUTAR con razon\n"
    "EVENTO CLAVE HOY: evento macro importante si lo hay"
)

def generate_daily_alert(context_str, spx, btc, btc_chg):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today  = datetime.now().strftime("%A %d de %B de %Y")
    msg    = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 600,
        system     = ALERT_PROMPT,
        messages   = [{"role": "user", "content":
            "Hoy es " + today + ".\n\n" + context_str +
            "\n\nDecision pendiente: Vender NFLX $72.5 PUT Ago 2026 (~$210 prima)." +
            "\nCIP Mayo 2026 se publica manana (consenso 4.2% - dato caliente esperado)." +
            "\nFOMC reunion 17-18 jun. BTC: $" + str(btc) + " (" + str(round(btc_chg,1)) + "% 24h)."
        }]
    )
    return msg.content[0].text

# ── RECOMENDACIONES SEMANALES ─────────────────────────────────────────────────
WEEKLY_PROMPT = (
    "Eres un asesor financiero senior con 20 anos de experiencia. "
    "Genera recomendaciones en JSON valido sin texto adicional ni backticks. "
    "El JSON debe tener exactamente esta estructura con 4 recs y 6 hyps: "
    '{"recs": [{"type": "action", "icon": "emoji", "title": "titulo", '
    '"badge": "br", "badgeText": "texto", "body": "explicacion", "tags": ["TAG"]}], '
    '"hyps": [{"id": "id", "cls": "semi", "icon": "emoji", "name": "nombre", '
    '"riskLbl": "Alto", "risk": 70, "riskColor": "#f85149", "horizon": "3 meses", '
    '"tickers": ["TICK"], "thesis": "tesis", "directo": "entrada directa con precio y stop", '
    '"opciones": "estrategia con strikes y prima estimada", '
    '"sizing": "cuanto capital usar", "catalizadores": "eventos clave"}]} '
    "Se muy especifico con tickers, precios de entrada, stops y estrategias de opciones."
)

def generate_weekly_recs(context_str):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today  = datetime.now().strftime("%A %d de %B de %Y")
    msg    = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 4000,
        system     = WEEKLY_PROMPT,
        messages   = [{"role": "user", "content":
            "Hoy es " + today + ".\n\n" + context_str +
            "\n\nContexto: S&P YTD +8.2%, objetivo alfa S&P+1pt, buying power ~$9,116, "
            "CPI mayo esta semana, FOMC 17-18 jun."
        }]
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    today = datetime.now()

    print("Obteniendo posiciones desde IBKR Flex...")
    ibkr_positions = get_ibkr_positions()

    print("Obteniendo precios de mercado...")
    prices = get_prices()
    crypto = get_crypto()

    print("Construyendo contexto...")
    context_str, total_pnl, total_pnlp, spx, btc, btc_chg = build_context(prices, crypto, ibkr_positions)
    print(context_str)

    print("\nGenerando alerta diaria...")
    alert = generate_daily_alert(context_str, spx, btc, btc_chg)
    print("\n--- MENSAJE TELEGRAM ---")
    print(alert)
    send_telegram(alert)

    if should_update_recommendations(today, context_str):
        print("\nDia estrategico - regenerando recomendaciones completas...")
        data = generate_weekly_recs(context_str)
        data["generated"] = today.isoformat()
        data["week"]      = today.strftime("%Y-%m-%d")
        with open("recommendations.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("recommendations.json generado: " + str(len(data["recs"])) + " recs, " + str(len(data["hyps"])) + " hipotesis")
    else:
        print("Hoy no es dia estrategico - solo alerta diaria")
