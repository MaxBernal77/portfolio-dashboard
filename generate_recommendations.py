import json
import os
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import anthropic

# ── PORTAFOLIO FALLBACK (si IBKR Flex no responde) ───────────────────────────
PORTFOLIO_FALLBACK = [
    {"ticker": "EC",   "name": "Ecopetrol",      "qty": 110, "avg_cost": 15.19,   "type": "stock",  "sector": "Energia"},
    {"ticker": "EIMI", "name": "MSCI EM IMI ETF", "qty": 25,  "avg_cost": 50.948,  "type": "etf",    "sector": "Emergentes"},
    {"ticker": "ETHE", "name": "Ethereum Trust",  "qty": 5,   "avg_cost": 31.40,   "type": "crypto", "sector": "Crypto"},
    {"ticker": "GBTC", "name": "Bitcoin Trust",   "qty": 9,   "avg_cost": 71.38,   "type": "crypto", "sector": "Crypto"},
    {"ticker": "NFLX", "name": "Netflix",         "qty": 3,   "avg_cost": 126.267, "type": "stock",  "sector": "Comunicaciones"},
    {"ticker": "NTR",  "name": "Nutrien",         "qty": 10,  "avg_cost": 74.609,  "type": "stock",  "sector": "Materiales"},
]

OPTIONS_FALLBACK = [
    {"desc": "EC Jul17 $17 CALL SHORT",   "pos": -1, "avg": 0.699, "strike": 17,   "exp": "2026-07-17"},
    {"desc": "NTR Aug21 $62.5 PUT SHORT", "pos": -1, "avg": 2.37,  "strike": 62.5, "exp": "2026-08-21"},
]

# ── USD/COP e IBR ─────────────────────────────────────────────────────────────
IBR_ANNUAL       = 7.75   # IBR Colombia % anual — actualizar mensualmente
AVG_PURCHASE_COP = 4000   # Tasa COP/USD promedio al comprar el portafolio

def get_usdcop():
    try:
        url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=COP=X"
        r   = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        q   = r.json()["quoteResponse"]["result"][0]
        return q.get("regularMarketPrice", AVG_PURCHASE_COP)
    except Exception:
        return AVG_PURCHASE_COP

def calc_cop_metrics(total_usd_value, total_usd_cost, pnl_usd, usdcop):
    value_cop   = total_usd_value * usdcop
    cost_cop    = total_usd_cost  * AVG_PURCHASE_COP
    pnl_cop     = value_cop - cost_cop
    pnl_cop_pct = (pnl_cop / cost_cop * 100) if cost_cop else 0
    mkt_effect  = pnl_usd * usdcop
    fx_effect   = total_usd_cost * (usdcop - AVG_PURCHASE_COP)
    vs_ibr      = pnl_cop_pct - IBR_ANNUAL
    return {
        "usdcop":      round(usdcop, 0),
        "value_cop":   round(value_cop, 0),
        "pnl_cop":     round(pnl_cop, 0),
        "pnl_cop_pct": round(pnl_cop_pct, 2),
        "mkt_effect":  round(mkt_effect, 0),
        "fx_effect":   round(fx_effect, 0),
        "vs_ibr":      round(vs_ibr, 2),
    }

def send_telegram(message):
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram no configurado")
        return
    url  = "https://api.telegram.org/bot" + token + "/sendMessage"
    data = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=data, timeout=10)
        print("Telegram OK" if r.status_code == 200 else "Telegram error: " + r.text)
    except Exception as e:
        print("Telegram error: " + str(e))

# ── IBKR FLEX ─────────────────────────────────────────────────────────────────
def get_ibkr_positions():
    token    = os.environ.get("IBKR_FLEX_TOKEN")
    query_id = os.environ.get("IBKR_FLEX_QUERY_ID")
    if not token or not query_id:
        print("IBKR Flex secrets no configurados, usando fallback")
        return None
    try:
        url1 = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest"
        r1   = requests.get(url1, params={"t": token, "q": query_id, "v": "3"}, timeout=15)
        root1 = ET.fromstring(r1.text)
        ref   = root1.findtext("ReferenceCode")
        if not ref:
            print("IBKR Flex: sin ReferenceCode. Respuesta: " + r1.text[:200])
            return None
        print("IBKR Flex ref: " + ref)

        url2 = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"
        root2 = None
        for attempt in range(6):
            time.sleep(5)
            r2    = requests.get(url2, params={"t": token, "q": ref, "v": "3"}, timeout=15)
            root2 = ET.fromstring(r2.text)
            if root2.tag == "FlexQueryResponse" or root2.findtext("Status") == "Complete":
                break
            print("Esperando IBKR Flex... intento " + str(attempt + 1))

        if root2 is None:
            return None

        positions = []
        for pos in root2.iter("OpenPosition"):
            symbol    = pos.get("symbol", "")
            asset     = pos.get("assetCategory", "STK")
            qty       = float(pos.get("position", 0) or 0)
            avg_cost  = float(pos.get("costBasisPrice", 0) or 0)
            mark      = float(pos.get("markPrice", 0) or 0)
            pnl       = float(pos.get("fifoPnlUnrealized", 0) or 0)
            strike    = pos.get("strike", "")
            expiry    = pos.get("expiry", "")
            put_call  = pos.get("putCall", "")
            if not symbol:
                continue
            positions.append({
                "ticker":      symbol,
                "name":        pos.get("description", symbol),
                "qty":         qty,
                "avg_cost":    avg_cost,
                "mark_price":  mark,
                "unrealized":  pnl,
                "asset_class": asset,
                "strike":      strike,
                "expiry":      expiry,
                "put_call":    put_call,
                "type":        "crypto" if symbol in ("GBTC", "ETHE") else ("etf" if asset == "ETF" else "stock"),
            })

        print("IBKR Flex: " + str(len(positions)) + " posiciones")
        return positions if positions else None

    except Exception as e:
        print("IBKR Flex error: " + str(e))
        return None

# ── PRECIOS YAHOO ─────────────────────────────────────────────────────────────
def get_prices(tickers_extra=None):
    prices  = {}
    base    = [p["ticker"] for p in PORTFOLIO_FALLBACK]
    indices = ["^GSPC", "^IXIC", "^VIX", "GLD"]
    all_t   = list(set(base + (tickers_extra or []) + indices))
    try:
        url     = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=" + ",".join(all_t)
        headers = {"User-Agent": "Mozilla/5.0"}
        r       = requests.get(url, headers=headers, timeout=10)
        for q in r.json()["quoteResponse"]["result"]:
            prices[q["symbol"]] = {
                "price":   q.get("regularMarketPrice", 0),
                "chg_pct": q.get("regularMarketChangePercent", 0),
            }
    except Exception as e:
        print("Yahoo error: " + str(e))
    return prices

def get_crypto():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true"
        return requests.get(url, timeout=8).json()
    except Exception:
        return {}

# ── CONTEXTO ──────────────────────────────────────────────────────────────────
def build_context(prices, crypto, ibkr_positions=None):
    lines       = ["PORTAFOLIO:\n"]
    total_value = 0
    total_cost  = 0

    portfolio = ibkr_positions if ibkr_positions else PORTFOLIO_FALLBACK
    stocks    = [p for p in portfolio if p.get("asset_class", "STK") not in ("OPT",) and not p.get("put_call")]
    options   = [p for p in portfolio if p.get("asset_class") == "OPT" or p.get("put_call")]

    for p in stocks:
        t     = p["ticker"]
        price = p.get("mark_price") or (prices.get(t) or {}).get("price") or p["avg_cost"]
        chg   = (prices.get(t) or {}).get("chg_pct", 0)
        avg   = p["avg_cost"]
        qty   = abs(p["qty"])
        pnl   = (price - avg) * qty
        pnlp  = ((price - avg) / avg * 100) if avg else 0
        total_value += price * qty
        total_cost  += avg * qty
        lines.append(
            "- " + t + ": " + str(round(qty)) + " acc @ $" + str(round(avg, 2)) +
            " | $" + str(round(price, 2)) + " (" + str(round(chg, 1)) + "%)" +
            " | P&L $" + str(round(pnl, 0)) + " (" + str(round(pnlp, 1)) + "%)"
        )

    total_pnl  = total_value - total_cost
    total_pnlp = (total_pnl / total_cost * 100) if total_cost else 0
    lines.append("\nTOTAL: $" + str(round(total_value, 0)) + " | P&L $" + str(round(total_pnl, 0)) + " (" + str(round(total_pnlp, 1)) + "%)")

    lines.append("\nOPCIONES:")
    if options:
        for o in options:
            days_left = 0
            if o.get("expiry"):
                try:
                    days_left = (datetime.strptime(str(o["expiry"]), "%Y%m%d") - datetime.now()).days
                except Exception:
                    pass
            lines.append("- " + o["ticker"] + " " + str(o.get("strike","")) + " " + str(o.get("put_call","")) + " exp " + str(o.get("expiry","")) + ": " + str(days_left) + " dias")
    else:
        for o in OPTIONS_FALLBACK:
            days_left = (datetime.strptime(o["exp"], "%Y-%m-%d") - datetime.now()).days
            lines.append("- " + o["desc"] + ": " + str(days_left) + " dias")

    spx     = prices.get("^GSPC") or {}
    ndx     = prices.get("^IXIC") or {}
    btc     = (crypto.get("bitcoin")  or {}).get("usd", "N/A")
    eth     = (crypto.get("ethereum") or {}).get("usd", "N/A")
    btc_chg = (crypto.get("bitcoin")  or {}).get("usd_24h_change", 0)
    lines.append("\nMERCADO: S&P " + str(spx.get("price","?")) + " (" + str(round(spx.get("chg_pct",0),1)) + "%) | BTC $" + str(btc) + " (" + str(round(btc_chg,1)) + "%) | ETH $" + str(eth))
    lines.append("Fuente: " + ("IBKR Flex" if ibkr_positions else "fallback hardcodeado"))

    return "\n".join(lines), total_pnl, total_pnlp, spx, btc, btc_chg

# ── DIAS ESTRATEGICOS ─────────────────────────────────────────────────────────
STRATEGIC_PROMPT = (
    "Eres un analista financiero senior. Decide cuales son los 3 dias mas estrategicos "
    "para regenerar recomendaciones del portafolio esta semana segun el calendario macro. "
    "Responde SOLO con JSON sin texto adicional: "
    '{"strategic_days": [1, 3, 5], "reasoning": "razon breve"} '
    "1=Lunes 2=Martes 3=Miercoles 4=Jueves 5=Viernes. Siempre incluye lunes."
)

STRATEGIC_FILE = "strategic_days.json"

def load_or_compute_strategic_days(today, context_str):
    is_monday = today.weekday() == 0
    if is_monday or not os.path.exists(STRATEGIC_FILE):
        print("Calculando dias estrategicos...")
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg    = client.messages.create(
            model      = "claude-sonnet-4-6",
            max_tokens = 200,
            system     = STRATEGIC_PROMPT,
            messages   = [{"role": "user", "content": "Hoy es " + today.strftime("%A %d %B %Y") + ".\n\n" + context_str}]
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        days   = result["strategic_days"]
        reason = result["reasoning"]
        with open(STRATEGIC_FILE, "w") as f:
            json.dump({"week_start": today.strftime("%Y-%m-%d"), "strategic_days": days, "reasoning": reason}, f)
        print("Dias: " + str(days) + " - " + reason)
        return days
    else:
        with open(STRATEGIC_FILE) as f:
            cache = json.load(f)
        print("Dias esta semana: " + str(cache["strategic_days"]) + " - " + cache["reasoning"])
        return cache["strategic_days"]

# ── ALERTA DIARIA ─────────────────────────────────────────────────────────────
ALERT_PROMPT = (
    "Eres un asesor financiero senior. Genera una alerta diaria concisa para Telegram en espanol. "
    "Maximo 30 lineas usando solo emojis y saltos de linea. Estructura: "
    "ALERTA PREMERCADO [fecha] | "
    "MERCADO (indices y BTC) | "
    "PORTAFOLIO HOY (P&L USD y movimientos clave) | "
    "PORTAFOLIO EN COP (valor COP, P&L COP, efecto divisa, comparacion vs IBR) | "
    "OPCIONES (estado) | "
    "ACCION DEL DIA (una sola, con EJECUTAR/ESPERAR/NO EJECUTAR si hay decision pendiente) | "
    "EVENTO CLAVE HOY"
)

def generate_daily_alert(context_str, btc, btc_chg):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg    = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 600,
        system     = ALERT_PROMPT,
        messages   = [{"role": "user", "content":
            "Hoy es " + datetime.now().strftime("%A %d de %B de %Y") + ".\n\n" + context_str +
            "\nDecision pendiente: Vender NFLX $72.5 PUT Ago 2026 (~$210 prima). " +
            "CPI Mayo publicado hoy o ayer. FOMC 17-18 jun. BTC $" + str(btc) + " (" + str(round(btc_chg,1)) + "%)."
        }]
    )
    return msg.content[0].text

# ── RECOMENDACIONES COMPLETAS ─────────────────────────────────────────────────
WEEKLY_PROMPT = (
    "Eres un asesor financiero senior con 20 anos de experiencia. "
    "Genera recomendaciones en JSON valido sin texto adicional ni backticks. "
    "Estructura con exactamente 4 recs y 6 hyps: "
    '{"recs": [{"type": "action", "icon": "emoji", "title": "titulo", "badge": "br", '
    '"badgeText": "texto", "body": "explicacion 2-3 oraciones", "tags": ["TAG"]}], '
    '"hyps": [{"id": "id_unico", "cls": "semi", "icon": "emoji", "name": "nombre", '
    '"riskLbl": "Alto", "risk": 70, "riskColor": "#f85149", "horizon": "3 meses", '
    '"tickers": ["TICK"], "thesis": "tesis 2-3 oraciones", '
    '"directo": "entrada directa con precio stop y target", '
    '"opciones": "estrategia con strikes y prima estimada", '
    '"sizing": "cuanto capital usar del buying power", '
    '"catalizadores": "eventos clave separados por guion"}]} '
    "Se muy especifico con tickers reales, precios de entrada y estrategias de opciones viables."
)

def generate_weekly_recs(context_str):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg    = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 4000,
        system     = WEEKLY_PROMPT,
        messages   = [{"role": "user", "content":
            "Hoy es " + datetime.now().strftime("%A %d de %B de %Y") + ".\n\n" + context_str +
            "\nContexto: S&P YTD +8.2%, objetivo alfa S&P+1pt, buying power ~$9,116, FOMC 17-18 jun."
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

    print("Obteniendo posiciones IBKR Flex...")
    ibkr_positions = get_ibkr_positions()

    print("Obteniendo precios de mercado...")
    ibkr_tickers = [p["ticker"] for p in ibkr_positions] if ibkr_positions else []
    prices       = get_prices(ibkr_tickers)
    crypto       = get_crypto()

    print("Construyendo contexto...")
    context_str, total_pnl, total_pnlp, spx, btc, btc_chg = build_context(prices, crypto, ibkr_positions)
    print(context_str)

    print("Obteniendo USD/COP...")
    usdcop = get_usdcop()
    print("USD/COP: " + str(usdcop))

    # Calcular métricas COP
    total_usd = sum((p.get("mark_price") or p["avg_cost"]) * abs(p["qty"]) for p in (ibkr_positions or PORTFOLIO_FALLBACK))
    cost_usd  = sum(p["avg_cost"] * abs(p["qty"]) for p in (ibkr_positions or PORTFOLIO_FALLBACK))
    cop_metrics = calc_cop_metrics(total_usd, cost_usd, total_usd - cost_usd, usdcop)
    print("P&L COP: ${:,.0f} ({:.1f}%) | vs IBR: {:.2f}pts".format(cop_metrics["pnl_cop"], cop_metrics["pnl_cop_pct"], cop_metrics["vs_ibr"]))

    print("\nGenerando alerta diaria...")
    alert = generate_daily_alert(context_str, btc, btc_chg, cop_metrics)
    print(alert)
    send_telegram(alert)

    strategic_days = load_or_compute_strategic_days(today, context_str)
    current_day    = today.weekday() + 1

    if current_day in strategic_days:
        print("\nDia estrategico - generando recomendaciones completas...")
        data = generate_weekly_recs(context_str)
        data["generated"] = today.isoformat()
        data["week"]      = today.strftime("%Y-%m-%d")
        with open("recommendations.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("recommendations.json: " + str(len(data["recs"])) + " recs, " + str(len(data["hyps"])) + " hyps")
    else:
        print("Hoy no es dia estrategico - solo alerta diaria")
