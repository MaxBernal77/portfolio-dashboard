import json
import os
import re
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import anthropic

# Unica constante manual — spread entre tasa interbancaria y tasa real al convertir en Colombia
COP_SPREAD = 0.96

# Valores dinamicos — se calculan automaticamente cada ejecucion
IBR_FALLBACK     = 8.25   # solo si BanRep no responde
AVG_PURCHASE_COP = None   # se calcula desde depositos IBKR; None = sin datos aun
IBKR_CASH_BALANCE = -605.34  # se actualiza desde NAV de IBKR Flex
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


def get_ibkr_positions():
    token    = os.environ.get("IBKR_FLEX_TOKEN")
    query_id = os.environ.get("IBKR_FLEX_QUERY_ID")
    if not token or not query_id:
        print("IBKR Flex secrets no configurados, usando fallback")
        return None
    try:
        url1  = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest"
        r1    = requests.get(url1, params={"t": token, "q": query_id, "v": "3"}, timeout=15)
        root1 = ET.fromstring(r1.text)
        ref   = root1.findtext("ReferenceCode")
        if not ref:
            print("IBKR Flex: sin ReferenceCode")
            return None
        print("IBKR Flex ref: " + ref)

        url2  = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"
        root2 = None
        for attempt in range(6):
            time.sleep(5)
            r2    = requests.get(url2, params={"t": token, "q": ref, "v": "3"}, timeout=15)
            root2 = ET.fromstring(r2.text)
            if root2.tag == "FlexQueryResponse" or root2.findtext("Status") == "Complete":
                break
            print("Esperando IBKR... intento " + str(attempt + 1))

        if root2 is None:
            return None

        positions = []
        cash_balance = 0.0

        # Leer NAV para obtener cash balance real
        for nav in root2.iter("NAVInBase"):
            cash = nav.get("cash") or nav.get("Cash")
            if cash:
                try:
                    cash_balance = float(cash)
                    print("Cash balance desde NAV: $" + str(round(cash_balance, 2)))
                except ValueError:
                    pass

        # Si no vino en NAVInBase buscar en ChangeInNAVInBase
        if cash_balance == 0.0:
            for nav in root2.iter("ChangeInNAVInBase"):
                cash = nav.get("endingCash") or nav.get("cash")
                if cash:
                    try:
                        cash_balance = float(cash)
                        print("Cash balance desde ChangeInNAV: $" + str(round(cash_balance, 2)))
                    except ValueError:
                        pass

        if cash_balance == 0.0:
            cash_balance = IBKR_CASH_BALANCE
            print("Cash balance usando fallback: $" + str(cash_balance))
        for pos in root2.iter("OpenPosition"):
            symbol   = pos.get("symbol", "")
            asset    = pos.get("assetCategory", "STK")
            qty      = float(pos.get("position",        0) or 0)
            avg_cost = float(pos.get("costBasisPrice",  0) or 0)
            mark     = float(pos.get("markPrice",       0) or 0)
            pnl      = float(pos.get("fifoPnlUnrealized", 0) or 0)
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
                "strike":      pos.get("strike", ""),
                "expiry":      pos.get("expiry", ""),
                "put_call":    pos.get("putCall", ""),
                "type":        "crypto" if symbol in ("GBTC", "ETHE") else ("etf" if asset == "ETF" else "stock"),
            })

        # Depositos para calcular tasa promedio COP
        deposits = []
        for tx in root2.iter("CashTransaction"):
            tx_type = tx.get("type", "")
            if "Deposit" in tx_type or "Wire" in tx_type or "Transfer" in tx_type:
                date_str = tx.get("dateTime", "") or tx.get("date", "")
                amount   = float(tx.get("amount", 0) or 0)
                currency = tx.get("currency", "USD")
                if amount > 0 and currency == "USD":
                    deposits.append({"date": date_str[:8], "amount": amount})

        print("IBKR Flex: " + str(len(positions)) + " posiciones | Cash: $" + str(round(cash_balance, 2)))
        return {"positions": positions, "deposits": deposits, "cash_balance": cash_balance}

    except Exception as e:
        print("IBKR Flex error: " + str(e))
        return None


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
        print("Yahoo precios error: " + str(e))
    return prices


def get_crypto():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true"
        return requests.get(url, timeout=8).json()
    except Exception:
        return {}


def get_usdcop():
    try:
        url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=COP%3DX"
        r   = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        raw = r.json()["quoteResponse"]["result"][0].get("regularMarketPrice", 4200)
        return round(raw * COP_SPREAD, 2)
    except Exception:
        return round(4200 * COP_SPREAD, 2)  # fallback con tasa aproximada


def get_ibr():
    # Intento 1: API publica BanRep Totoro
    try:
        url = (
            "https://totoro.banrep.gov.co/analytics/saw.dll"
            "?Go&NQUser=publico&NQPassword=publico"
            "&Action=Navigate"
            "&Path=/shared/IBR/IBR_overnight"
            "&Options=rdf"
        )
        r    = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        text = r.text.strip()
        for line in reversed(text.splitlines()):
            parts = line.replace('"', '').split(',')
            if len(parts) >= 2:
                try:
                    val = float(parts[-1].strip().replace('%', ''))
                    if 0 < val < 30:
                        print("IBR BanRep: " + str(val) + "%")
                        return val
                except ValueError:
                    continue
    except Exception as e:
        print("IBR intento 1 fallido: " + str(e))

    # Intento 2: scraping pagina BanRep
    try:
        url2 = "https://www.banrep.gov.co/es/estadisticas/tasas-interes-del-mercado-monetario"
        r2   = requests.get(url2, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        matches = re.findall(r'IBR[^0-9]*(\d{1,2}[.,]\d{1,4})\s*%', r2.text)
        if matches:
            val = float(matches[0].replace(',', '.'))
            if 0 < val < 30:
                print("IBR scrape: " + str(val) + "%")
                return val
    except Exception as e:
        print("IBR intento 2 fallido: " + str(e))

    print("IBR usando fallback: " + str(IBR_FALLBACK) + "%")
    return IBR_FALLBACK


def calc_avg_purchase_cop(deposits):
    if not deposits:
        return AVG_PURCHASE_COP
    total_usd  = 0.0
    total_cop  = 0.0
    headers    = {"User-Agent": "Mozilla/5.0"}
    for dep in deposits:
        date_str = dep["date"]
        amount   = dep["amount"]
        try:
            dt       = datetime.strptime(date_str, "%Y%m%d")
            # Buscar precio historico USD/COP en esa fecha
            ts_start = int(dt.timestamp())
            ts_end   = ts_start + 86400
            url      = "https://query1.finance.yahoo.com/v8/finance/chart/COP%3DX?interval=1d&period1=" + str(ts_start) + "&period2=" + str(ts_end)
            r        = requests.get(url, headers=headers, timeout=8)
            close    = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"][0]
            if close and close > 0:
                total_usd += amount
                total_cop += amount * close
                print("Deposito " + date_str + ": $" + str(round(amount,0)) + " USD @ " + str(round(close,0)) + " COP")
        except Exception as e:
            print("No se pudo obtener tasa para deposito " + date_str + ": " + str(e))

    if total_usd > 0:
        avg = round(total_cop / total_usd, 2)
        print("Tasa promedio ponderada COP/USD: " + str(avg))
        return avg
    return AVG_PURCHASE_COP


def save_prices(prices, crypto, usdcop):
    data = {"updated": datetime.now().isoformat(), "prices": {}}
    # Indices
    map_keys = {"^GSPC":"IDX_SPX","^IXIC":"IDX_NDX","^VIX":"IDX_VIX","GLD":"IDX_GLD","BNO":"IDX_OIL"}
    for yf_sym, key in map_keys.items():
        if yf_sym in prices:
            data["prices"][key] = {"price": prices[yf_sym]["price"], "chg": prices[yf_sym]["chg_pct"], "label": "Cierre"}
    # Posiciones
    for p in PORTFOLIO_FALLBACK:
        t = p["ticker"]
        if t in prices:
            data["prices"][t] = {"price": prices[t]["price"], "chg": prices[t]["chg_pct"], "label": "Cierre"}
    # Cripto
    if crypto.get("bitcoin"):
        data["prices"]["BTC"] = {"price": crypto["bitcoin"]["usd"], "chg": crypto["bitcoin"].get("usd_24h_change", 0), "label": ""}
    if crypto.get("ethereum"):
        data["prices"]["ETH"] = {"price": crypto["ethereum"]["usd"], "chg": crypto["ethereum"].get("usd_24h_change", 0), "label": ""}
    # COP
    data["prices"]["IDX_COP"] = {"price": round(usdcop / COP_SPREAD, 2), "chg": 0, "label": "Cierre"}

    with open("prices.json", "w") as f:
        json.dump(data, f, indent=2)
    print("prices.json guardado con " + str(len(data["prices"])) + " precios")
    config = {
        "usdcop_effective": usdcop,
        "usdcop_spread":    COP_SPREAD,
        "ibr_annual":       ibr,
        "avg_purchase_cop": avg_purchase,
        "updated":          datetime.now().isoformat()
    }
    with open("market_config.json", "w") as f:
        json.dump(config, f, indent=2)
    print("market_config.json: USD/COP=" + str(usdcop) + " IBR=" + str(ibr) + "% avg_cop=" + str(avg_purchase))


def build_context(prices, crypto, ibkr_data=None):
    lines       = ["PORTAFOLIO:\n"]
    total_value = 0
    total_cost  = 0

    ibkr_positions = ibkr_data.get("positions") if ibkr_data else None
    portfolio = ibkr_positions if ibkr_positions else PORTFOLIO_FALLBACK
    stocks    = [p for p in portfolio if p.get("asset_class") != "OPT" and not p.get("put_call")]
    options   = [p for p in portfolio if p.get("asset_class") == "OPT" or p.get("put_call")]

    for p in stocks:
        t     = p["ticker"]
        price = p.get("mark_price") or 0
        if price <= 0:
            price = (prices.get(t) or {}).get("price") or p["avg_cost"]
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

    # Net liquidation = valor bruto posiciones + cash (negativo si hay margen)
    net_liq    = total_value + IBKR_CASH_BALANCE
    net_pnl    = net_liq - total_cost
    net_pnlp   = (net_pnl / total_cost * 100) if total_cost else 0

    lines.append(
        "\nRESUMEN USD:" +
        "\n  Valor bruto posiciones: $" + str(round(total_value, 0)) +
        "\n  Cash / Margen: $" + str(round(IBKR_CASH_BALANCE, 0)) +
        "\n  NET LIQUIDATION: $" + str(round(net_liq, 0)) +
        " | P&L real: $" + str(round(net_pnl, 0)) + " (" + str(round(net_pnlp, 1)) + "%)"
    )

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
    lines.append(
        "\nMERCADO: S&P " + str(spx.get("price","?")) + " (" + str(round(spx.get("chg_pct",0),1)) + "%)" +
        " | BTC $" + str(btc) + " (" + str(round(btc_chg,1)) + "%) | ETH $" + str(eth)
    )
    lines.append("Fuente: " + ("IBKR Flex" if ibkr_positions else "fallback"))

    return "\n".join(lines), net_pnl, net_pnlp, spx, btc, btc_chg, net_liq, total_cost


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


ALERT_PROMPT = (
    "Eres un asesor financiero senior. Genera una alerta diaria concisa para Telegram en espanol. "
    "Maximo 30 lineas usando solo emojis y saltos de linea. Estructura: "
    "ALERTA PREMERCADO [fecha] | "
    "MERCADO (S&P Nasdaq BTC con variacion) | "
    "PORTAFOLIO HOY (P&L USD y movimientos clave) | "
    "PORTAFOLIO EN COP (valor, P&L, efecto divisa, vs IBR) | "
    "OPCIONES (estado) | "
    "ACCION DEL DIA (una sola, EJECUTAR/ESPERAR/NO EJECUTAR si hay decision pendiente) | "
    "EVENTO CLAVE HOY"
)


def generate_daily_alert(context_str, btc, btc_chg, cop_info):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg    = client.messages.create(
        model      = "claude-sonnet-4-6",
        max_tokens = 700,
        system     = ALERT_PROMPT,
        messages   = [{"role": "user", "content":
            "Hoy es " + datetime.now().strftime("%A %d de %B de %Y") + ".\n\n" + context_str +
            "\nRESUMEN COP:" +
            "\n  USD/COP efectivo: " + str(int(cop_info["usdcop"])) +
            "\n  Net Liq en COP: $" + "{:,.0f}".format(cop_info["value_cop"]) +
            "\n  P&L en COP: $" + "{:,.0f}".format(cop_info["pnl_cop"]) + " (" + str(cop_info["pnl_pct"]) + "%)" +
            "\n  Efecto mercado: $" + "{:,.0f}".format(cop_info["mkt_eff"]) +
            "\n  Efecto divisa: $" + "{:,.0f}".format(cop_info["fx_eff"]) +
            "\n  vs IBR " + str(cop_info["ibr"]) + "%: " + str(cop_info["vs_ibr"]) + "pts" +
            "\nFOMC 17-18 jun. BTC $" + str(btc) + " (" + str(round(btc_chg,1)) + "%)."
        }]
    )
    return msg.content[0].text


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
    client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today   = datetime.now().strftime("%A %d de %B de %Y")
    content = (
        "Hoy es " + today + ".\n\n" + context_str +
        "\n\nContexto: S&P YTD +8.2%, objetivo alfa S&P+1pt, buying power ~$9,116, FOMC 17-18 jun."
    )
    for attempt in range(2):
        msg = client.messages.create(
            model      = "claude-sonnet-4-6",
            max_tokens = 6000,
            system     = WEEKLY_PROMPT,
            messages   = [{"role": "user", "content": content}]
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            print("JSON error intento " + str(attempt+1) + ": " + str(e))
            if attempt == 0:
                # Pedir version mas corta
                content = content + "\n\nIMPORTANTE: Genera JSON mas conciso. Limita body/thesis a 1 oracion."
    raise ValueError("No se pudo generar JSON valido despues de 2 intentos")


if __name__ == "__main__":
    today = datetime.now()

    print("Obteniendo posiciones IBKR Flex...")
    ibkr_data = get_ibkr_positions()

    print("Obteniendo precios de mercado...")
    ibkr_tickers = [p["ticker"] for p in ibkr_data["positions"]] if ibkr_data else []
    prices       = get_prices(ibkr_tickers)
    crypto       = get_crypto()

    print("Construyendo contexto...")
    context_str, total_pnl, total_pnlp, spx, btc, btc_chg, total_usd, cost_usd = build_context(prices, crypto, ibkr_data)
    print(context_str)

    # Tomar cash balance real de IBKR si está disponible
    if ibkr_data and "cash_balance" in ibkr_data:
        IBKR_CASH_BALANCE = ibkr_data["cash_balance"]
        print("Cash balance actualizado desde IBKR Flex: $" + str(round(IBKR_CASH_BALANCE, 2)))
    usdcop     = get_usdcop()
    ibr_annual = get_ibr()

    # Calcular tasa promedio COP real desde depositos IBKR
    # Tasa promedio COP real desde depositos IBKR
    deposits     = ibkr_data.get("deposits", []) if ibkr_data else []
    avg_purchase = calc_avg_purchase_cop(deposits) if deposits else None

    if avg_purchase is None:
        # Sin depositos aun — usar tasa actual como aproximacion
        avg_purchase = round(usdcop / COP_SPREAD, 0)
        print("Sin depositos IBKR — usando tasa actual como avg_purchase: " + str(avg_purchase))
    save_prices(prices, crypto, usdcop)
    save_market_config(usdcop, ibr_annual, avg_purchase)

    # Metricas COP usando NET LIQUIDATION (no valor bruto)
    value_cop  = total_usd * usdcop        # total_usd ya es net_liq
    cost_cop   = cost_usd  * avg_purchase
    pnl_cop    = value_cop - cost_cop
    pnl_pct    = round((pnl_cop / cost_cop * 100) if cost_cop else 0, 2)
    mkt_eff    = (total_usd - cost_usd) * usdcop
    fx_eff     = cost_usd * (usdcop - avg_purchase)
    vs_ibr     = round(pnl_pct - ibr_annual, 2)
    cop_info   = {
        "usdcop":    usdcop,
        "value_cop": value_cop,
        "pnl_cop":   pnl_cop,
        "pnl_pct":   pnl_pct,
        "mkt_eff":   mkt_eff,
        "fx_eff":    fx_eff,
        "ibr":       ibr_annual,
        "vs_ibr":    vs_ibr,
        "net_liq":   total_usd,
        "cash":      IBKR_CASH_BALANCE,
    }

    print("\nGenerando alerta diaria...")
    alert = generate_daily_alert(context_str, btc, btc_chg, cop_info)
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
