"""
generate_recommendations.py
Corre cada dia habil a las 8:30am Bogota via GitHub Actions.
Genera: prices.json, market_config.json, recommendations.json (dias estrategicos)
Envia alerta diaria por Telegram.
"""
import json, os, re, time, requests, xml.etree.ElementTree as ET
import yfinance as yf
from datetime import datetime
import anthropic

# Nombres en español para fechas (GitHub Actions corre en inglés)
MESES = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
DIAS  = ["lunes","martes","miercoles","jueves","viernes","sabado","domingo"]

def fecha_es(dt):
    return DIAS[dt.weekday()] + " " + str(dt.day) + " de " + MESES[dt.month-1] + " de " + str(dt.year)


COP_SPREAD = 0.96   # spread entre tasa interbancaria y tasa real en Colombia

# ── FALLBACKS (usados solo si las fuentes automaticas fallan) ─────────────────
IBR_FALLBACK          = 8.25
CASH_BALANCE_FALLBACK = 138.38

# ── PORTAFOLIO FALLBACK ───────────────────────────────────────────────────────
PORTFOLIO_FALLBACK = [
    {"ticker":"EC",  "name":"Ecopetrol",      "qty":110,"avg_cost":15.19,  "type":"stock", "sector":"Energia"},
    {"ticker":"EIMI","name":"MSCI EM IMI ETF", "qty":25, "avg_cost":50.948,"type":"etf",   "sector":"Emergentes"},
    {"ticker":"NFLX","name":"Netflix",         "qty":3,  "avg_cost":126.267,"type":"stock","sector":"Comunicaciones"},
    {"ticker":"NTR", "name":"Nutrien",         "qty":10, "avg_cost":74.609,"type":"stock", "sector":"Materiales"},
]
OPTIONS_FALLBACK = [
    {"desc":"NTR Aug21 $62.5 PUT SHORT", "pos":-1,"avg":2.53, "strike":62.5,"exp":"2026-08-21"},
]

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(message):
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram no configurado"); return
    try:
        r = requests.post(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            data={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10
        )
        print("Telegram OK" if r.status_code == 200 else "Telegram error: " + r.text)
    except Exception as e:
        print("Telegram error: " + str(e))

# ── IBKR FLEX ─────────────────────────────────────────────────────────────────
def get_ibkr_data():
    token    = os.environ.get("IBKR_FLEX_TOKEN")
    query_id = os.environ.get("IBKR_FLEX_QUERY_ID")
    if not token or not query_id:
        print("IBKR Flex no configurado, usando fallback"); return None
    try:
        r1    = requests.get(
            "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest",
            params={"t": token, "q": query_id, "v": "3"}, timeout=15
        )
        ref = ET.fromstring(r1.text).findtext("ReferenceCode")
        if not ref:
            print("IBKR Flex: sin ReferenceCode"); return None
        print("IBKR Flex ref: " + ref)

        root2 = None
        for attempt in range(6):
            time.sleep(5)
            r2    = requests.get(
                "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement",
                params={"t": token, "q": ref, "v": "3"}, timeout=15
            )
            root2 = ET.fromstring(r2.text)
            if root2.tag == "FlexQueryResponse" or root2.findtext("Status") == "Complete":
                break
            print("Esperando IBKR... intento " + str(attempt + 1))
        if root2 is None: return None

        # Posiciones
        positions = []
        # Debug: verificar que el XML tiene OpenPosition
        open_pos_count = len(list(root2.iter("OpenPosition")))
        print("OpenPosition tags encontrados: " + str(open_pos_count))
        if open_pos_count == 0:
            all_tags = set(elem.tag for elem in root2.iter())
            print("Tags disponibles en XML: " + ", ".join(sorted(all_tags)))
        for pos in root2.iter("OpenPosition"):
            symbol = pos.get("symbol", "")
            if not symbol: continue
            qty        = float(pos.get("position",       0) or 0)
            avg_cost   = float(pos.get("costBasisPrice", 0) or 0)
            mark_price = float(pos.get("markPrice",      0) or 0)
            pos_value  = float(pos.get("positionValue",  0) or pos.get("mktValue", 0) or 0)
            # Calcular precio desde valor si mark_price es 0
            if mark_price <= 0 and pos_value != 0 and qty != 0:
                mark_price = abs(pos_value / qty)
            positions.append({
                "ticker":      symbol,
                "name":        pos.get("description", symbol),
                "qty":         qty,
                "avg_cost":    avg_cost,
                "mark_price":  mark_price,
                "pos_value":   pos_value,
                "asset_class": pos.get("assetCategory", "STK"),
                "strike":      pos.get("strike", ""),
                "expiry":      pos.get("expiry", ""),
                "put_call":    pos.get("putCall", ""),
                "type":        "crypto" if symbol in ("GBTC","ETHE") else "stock",
            })

        # Cash balance — buscar en multiples tags y atributos que genera IBKR Flex
        cash_balance = CASH_BALANCE_FALLBACK
        cash_found   = False

        # 1. CashReport (el mas confiable para USD cash)
        for cr in root2.iter("CashReportCurrency"):
            if cr.get("currency", "") == "USD" and not cash_found:
                for attr in ("endingCash","ending","endCash","cashBalance","cash","Cash"):
                    val = cr.get(attr)
                    if val is not None:
                        try:
                            cash_balance = float(val)
                            print("Cash desde CashReportCurrency." + attr + ": $" + str(round(cash_balance,2)))
                            cash_found = True; break
                        except ValueError: pass

        # 2. EquitySummaryByReportDateInBase
        if not cash_found:
            for eq in root2.iter("EquitySummaryByReportDateInBase"):
                for attr in ("cash","Cash","totalCash","cashAndCashEquivalents"):
                    val = eq.get(attr)
                    if val is not None:
                        try:
                            cash_balance = float(val)
                            print("Cash desde EquitySummary." + attr + ": $" + str(round(cash_balance,2)))
                            cash_found = True; break
                        except ValueError: pass
                if cash_found: break

        # 3. NAVInBase / ChangeInNAVInBase
        if not cash_found:
            for nav in list(root2.iter("NAVInBase")) + list(root2.iter("ChangeInNAVInBase")):
                for attr in ("cash","Cash","endingCash","cashAndCashEquivalents","starting","ending"):
                    val = nav.get(attr)
                    if val is not None:
                        try:
                            cash_balance = float(val)
                            print("Cash desde NAV." + attr + ": $" + str(round(cash_balance,2)))
                            cash_found = True; break
                        except ValueError: pass
                if cash_found: break

        # 4. Si aun no encontramos, buscar cualquier tag con "cash" en el nombre
        if not cash_found:
            for elem in root2.iter():
                if "cash" in elem.tag.lower():
                    for attr, val in elem.attrib.items():
                        if "cash" in attr.lower() or attr in ("ending","endingCash","balance"):
                            try:
                                cash_balance = float(val)
                                if -50000 < cash_balance < 0 or 0 <= cash_balance < 100000:
                                    print("Cash desde " + elem.tag + "." + attr + ": $" + str(round(cash_balance,2)))
                                    cash_found = True; break
                            except (ValueError, TypeError): pass
                    if cash_found: break

        if not cash_found:
            # Debug: imprimir todos los tags disponibles en el XML para diagnostico
            tags = set(elem.tag for elem in root2.iter())
            print("⚠️  Cash no encontrado. Tags en XML: " + ", ".join(sorted(tags)[:30]))
            print("   Usando fallback: $" + str(CASH_BALANCE_FALLBACK))

        # Depositos para tasa COP promedio
        deposits = []
        for tx in root2.iter("CashTransaction"):
            tx_type = tx.get("type", "")
            if any(k in tx_type for k in ("Deposit","Wire","Transfer")):
                date_str = (tx.get("dateTime") or tx.get("date",""))[:8]
                amount   = float(tx.get("amount", 0) or 0)
                if amount > 0 and tx.get("currency","") == "USD":
                    deposits.append({"date": date_str, "amount": amount})

        print("IBKR Flex: " + str(len(positions)) + " posiciones | cash: $" + str(round(cash_balance,2)) + " | depositos: " + str(len(deposits)))
        return {"positions": positions, "cash_balance": cash_balance, "deposits": deposits}

    except Exception as e:
        print("IBKR Flex error: " + str(e)); return None

# ── PRECIOS (Yahoo Finance directo desde Python, sin proxy) ───────────────────
def get_yahoo_prices(tickers):
    """yfinance es mas confiable que requests directos a Yahoo Finance."""
    prices = {}
    try:
        # Descargar todos los tickers de una vez
        raw = yf.download(
            tickers, period="2d", interval="1d",
            auto_adjust=True, progress=False, threads=True
        )
        # Obtener ultimo precio de cierre para cada ticker
        close = raw["Close"] if "Close" in raw.columns else raw
        for t in (tickers if isinstance(tickers, list) else [tickers]):
            try:
                ticker_obj = yf.Ticker(t)
                info       = ticker_obj.fast_info
                price      = float(info.last_price) if info.last_price else 0
                prev       = float(info.previous_close) if info.previous_close else price
                chg        = round(((price - prev) / prev * 100) if prev else 0, 2)
                if price > 0:
                    prices[t] = {"price": round(price, 4), "chg": chg, "label": "Cierre"}
            except Exception as e:
                print("yfinance error " + t + ": " + str(e))
        print("yfinance: " + str(len(prices)) + "/" + str(len(tickers)) + " precios obtenidos")
    except Exception as e:
        print("yfinance error general: " + str(e))
    return prices

def get_crypto():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true",
            timeout=8
        )
        return r.json()
    except Exception: return {}

# ── IBR BANCO DE LA REPUBLICA ─────────────────────────────────────────────────
def get_ibr():
    try:
        url = ("https://totoro.banrep.gov.co/analytics/saw.dll"
               "?Go&NQUser=publico&NQPassword=publico&Action=Navigate"
               "&Path=/shared/IBR/IBR_overnight&Options=rdf")
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        for line in reversed(r.text.strip().splitlines()):
            parts = line.replace('"','').split(',')
            if len(parts) >= 2:
                try:
                    val = float(parts[-1].strip().replace('%',''))
                    if 0 < val < 30:
                        print("IBR BanRep: " + str(val) + "%"); return val
                except ValueError: pass
    except Exception as e:
        print("IBR BanRep fallido: " + str(e))
    try:
        r2 = requests.get("https://www.banrep.gov.co/es/estadisticas/tasas-interes-del-mercado-monetario",
                          timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        matches = re.findall(r'IBR[^0-9]*(\d{1,2}[.,]\d{1,4})\s*%', r2.text)
        if matches:
            val = float(matches[0].replace(',','.'))
            if 0 < val < 30:
                print("IBR scrape: " + str(val) + "%"); return val
    except Exception as e:
        print("IBR scrape fallido: " + str(e))
    print("IBR fallback: " + str(IBR_FALLBACK) + "%")
    return IBR_FALLBACK

# ── TASA PROMEDIO COP ─────────────────────────────────────────────────────────
def get_avg_purchase_cop(deposits, usdcop_raw):
    if not deposits:
        print("Sin depositos IBKR, usando tasa actual como avg")
        return round(usdcop_raw, 0)
    total_usd = total_cop = 0.0
    headers   = {"User-Agent": "Mozilla/5.0"}
    for dep in deposits:
        try:
            dt  = datetime.strptime(dep["date"], "%Y%m%d")
            ts1 = int(dt.timestamp())
            url = "https://query1.finance.yahoo.com/v8/finance/chart/COP%3DX?interval=1d&period1=" + str(ts1) + "&period2=" + str(ts1+86400)
            r   = requests.get(url, headers=headers, timeout=8)
            close = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"][0]
            if close and close > 0:
                total_usd += dep["amount"]; total_cop += dep["amount"] * close
                print("Deposito " + dep["date"] + ": $" + str(round(dep["amount"],0)) + " @ " + str(round(close,0)) + " COP")
        except Exception as e:
            print("Sin tasa para deposito " + dep["date"] + ": " + str(e))
    if total_usd > 0:
        avg = round(total_cop / total_usd, 2)
        print("Tasa promedio ponderada: " + str(avg) + " COP/USD"); return avg
    return round(usdcop_raw, 0)

# ── GUARDAR ARCHIVOS JSON ─────────────────────────────────────────────────────
def save_prices_json(ibkr_data, yahoo, crypto, usdcop_raw, ibr_annual, avg_purchase):
    data = {
        "updated":          datetime.now().isoformat(),
        "ibr_annual":       ibr_annual,
        "avg_purchase_cop": avg_purchase,
        "usdcop_effective": round(usdcop_raw * COP_SPREAD, 2),
        "prices":           {}
    }
    # Posiciones desde IBKR (fuente primaria)
    if ibkr_data and ibkr_data.get("positions"):
        for p in ibkr_data["positions"]:
            t = p["ticker"]
            if p["mark_price"] > 0:
                data["prices"][t] = {"price": round(p["mark_price"],4), "chg": 0, "label": "IBKR"}

    # Indices desde Yahoo Finance
    idx_map = {"^GSPC":"IDX_SPX","^IXIC":"IDX_NDX","^VIX":"IDX_VIX","GLD":"IDX_GLD","BNO":"IDX_OIL"}
    for sym, key in idx_map.items():
        if sym in yahoo:
            data["prices"][key] = yahoo[sym]

    # COP rate
    cop_raw = yahoo.get("COP=X",{}).get("price") or usdcop_raw
    data["prices"]["IDX_COP"] = {"price": round(cop_raw * COP_SPREAD, 2), "chg": yahoo.get("COP=X",{}).get("chg",0), "label": "Cierre"}

    # Posiciones sin precio desde IBKR: usar Yahoo como fallback
    for p in PORTFOLIO_FALLBACK:
        t = p["ticker"]
        if t not in data["prices"] and t in yahoo:
            data["prices"][t] = yahoo[t]

    # Cripto desde CoinGecko
    if crypto.get("bitcoin"):
        btc = crypto["bitcoin"]
        data["prices"]["BTC"]  = {"price": btc["usd"], "chg": btc.get("usd_24h_change",0), "label": ""}
        if "GBTC" not in data["prices"]:
            data["prices"]["GBTC"] = {"price": round(btc["usd"] * 0.00077, 2), "chg": btc.get("usd_24h_change",0), "label": "BTC-ratio"}
    if crypto.get("ethereum"):
        eth = crypto["ethereum"]
        data["prices"]["ETH"]  = {"price": eth["usd"], "chg": eth.get("usd_24h_change",0), "label": ""}
        if "ETHE" not in data["prices"]:
            data["prices"]["ETHE"] = {"price": round(eth["usd"] * 0.0085, 2), "chg": eth.get("usd_24h_change",0), "label": "ETH-ratio"}

    with open("prices.json", "w") as f:
        json.dump(data, f, indent=2)
    print("prices.json: " + str(len(data["prices"])) + " precios guardados")

def save_market_config(usdcop_raw, ibr_annual, avg_purchase, cash_balance):
    config = {
        "usdcop_effective": round(usdcop_raw * COP_SPREAD, 2),
        "ibr_annual":       ibr_annual,
        "avg_purchase_cop": avg_purchase,
        "cash_balance":     cash_balance,
        "updated":          datetime.now().isoformat()
    }
    with open("market_config.json", "w") as f:
        json.dump(config, f, indent=2)
    print("market_config.json: USD/COP=" + str(round(usdcop_raw*COP_SPREAD,0)) + " IBR=" + str(ibr_annual) + "%")

def save_portfolio_history(entry):
    """Upsert de la fila diaria en portfolio_history.json (clave: date)."""
    path = "portfolio_history.json"
    hist = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                hist = json.load(f)
            if not isinstance(hist, list): hist = []
        except Exception:
            hist = []
    hist = [h for h in hist if h.get("date") != entry["date"]]
    hist.append(entry)
    hist.sort(key=lambda h: h.get("date", ""))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hist, f, indent=2)
    print("portfolio_history.json: " + str(len(hist)) + " registros (hoy " + entry["date"] + ")")

# ── CONTEXTO PARA CLAUDE ──────────────────────────────────────────────────────
def build_context(ibkr_data, yahoo, crypto, cash_balance):
    lines       = ["PORTAFOLIO:\n"]
    total_value = total_cost = 0.0

    portfolio  = ibkr_data["positions"] if ibkr_data else PORTFOLIO_FALLBACK
    stocks     = [p for p in portfolio if p.get("asset_class","STK") != "OPT" and not p.get("put_call")]
    options_ib = [p for p in portfolio if p.get("asset_class") == "OPT" or p.get("put_call")]

    for p in stocks:
        t     = p["ticker"]
        price = p.get("mark_price") or 0
        if price <= 0: price = yahoo.get(t,{}).get("price") or p["avg_cost"]
        chg   = yahoo.get(t,{}).get("chg", 0)
        avg   = p["avg_cost"]
        qty   = abs(p["qty"])
        pnl   = (price - avg) * qty
        pnlp  = ((price - avg) / avg * 100) if avg else 0
        total_value += price * qty
        total_cost  += avg   * qty
        lines.append("- " + t + ": " + str(round(qty)) + " acc @ $" + str(round(avg,2)) +
                     " | $" + str(round(price,2)) + " (" + str(round(chg,1)) + "%)" +
                     " | P&L $" + str(round(pnl,0)) + " (" + str(round(pnlp,1)) + "%)")

    # Opciones IBKR: valor de mercado (negativo si corto) y prima recibida
    opt_value   = 0.0
    opt_premium = 0.0
    for o in options_ib:
        oqty = o.get("qty", 0) or 0
        ov   = o.get("pos_value")
        if ov in (None, 0):
            ov = (o.get("mark_price", 0) or 0) * oqty * 100
        opt_value   += ov
        opt_premium += (o.get("avg_cost", 0) or 0) * abs(oqty) * 100 * (1 if oqty < 0 else -1)
    total_cost -= opt_premium   # los cortos reducen el costo base (credito recibido)

    net_liq  = total_value + opt_value + cash_balance
    net_pnl  = net_liq - total_cost
    net_pnlp = (net_pnl / total_cost * 100) if total_cost else 0

    lines.append("\nRESUMEN USD:" +
                 "\n  Valor bruto: $" + str(round(total_value,0)) +
                 "\n  Opciones (mkt): $" + str(round(opt_value,0)) +
                 "\n  Cash/Margen: $" + str(round(cash_balance,0)) +
                 "\n  NET LIQUIDATION: $" + str(round(net_liq,0)) +
                 " | P&L: $" + str(round(net_pnl,0)) + " (" + str(round(net_pnlp,1)) + "%)")

    lines.append("\nOPCIONES:")
    if options_ib:
        for o in options_ib:
            days_left = 0
            if o.get("expiry"):
                try: days_left = (datetime.strptime(str(o["expiry"]),"%Y%m%d") - datetime.now()).days
                except: pass
            lines.append("- " + o["ticker"] + " " + str(o.get("strike","")) + " " + str(o.get("put_call","")) + " exp " + str(o.get("expiry","")) + ": " + str(days_left) + " dias")
    else:
        for o in OPTIONS_FALLBACK:
            days_left = (datetime.strptime(o["exp"],"%Y-%m-%d") - datetime.now()).days
            lines.append("- " + o["desc"] + ": " + str(days_left) + " dias")

    spx     = yahoo.get("^GSPC",{})
    btc     = (crypto.get("bitcoin")  or {}).get("usd","N/A")
    eth     = (crypto.get("ethereum") or {}).get("usd","N/A")
    btc_chg = (crypto.get("bitcoin")  or {}).get("usd_24h_change",0)
    lines.append("\nMERCADO: S&P " + str(spx.get("price","?")) + " (" + str(round(spx.get("chg",0),1)) + "%)" +
                 " | BTC $" + str(btc) + " (" + str(round(btc_chg,1)) + "%) | ETH $" + str(eth))
    lines.append("Fuente posiciones: " + ("IBKR Flex" if ibkr_data else "fallback"))

    return "\n".join(lines), net_pnl, net_pnlp, net_liq, total_cost, spx, btc, btc_chg, opt_value

# ── DIAS ESTRATEGICOS ─────────────────────────────────────────────────────────
STRATEGIC_PROMPT = (
    "Eres un analista financiero. Decide los 3 dias mas estrategicos para actualizar "
    "recomendaciones del portafolio esta semana segun el calendario macro. "
    "Responde SOLO con JSON: "
    '{"strategic_days":[1,3,5],"reasoning":"razon breve"} '
    "1=Lunes 2=Martes 3=Miercoles 4=Jueves 5=Viernes. Siempre incluye lunes."
)
STRATEGIC_FILE = "strategic_days.json"

def get_strategic_days(today, context_str):
    is_monday = today.weekday() == 0
    if is_monday or not os.path.exists(STRATEGIC_FILE):
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg    = client.messages.create(model="claude-sonnet-4-6", max_tokens=200,
                     system=STRATEGIC_PROMPT,
                     messages=[{"role":"user","content":"Hoy es " + today.strftime("%A %d %B %Y") + ".\n\n" + context_str}])
        raw = msg.content[0].text.strip()
        if raw.startswith("```"): raw = raw.split("```")[1]; raw = raw[4:] if raw.startswith("json") else raw
        result = json.loads(raw)
        days   = result["strategic_days"]; reason = result["reasoning"]
        with open(STRATEGIC_FILE,"w") as f:
            json.dump({"week_start":today.strftime("%Y-%m-%d"),"strategic_days":days,"reasoning":reason},f)
        print("Dias estrategicos: " + str(days) + " - " + reason)
        return days
    with open(STRATEGIC_FILE) as f: cache = json.load(f)
    print("Dias esta semana: " + str(cache["strategic_days"]))
    return cache["strategic_days"]

# ── ALERTAS Y RECOMENDACIONES ─────────────────────────────────────────────────
ALERT_PROMPT = (
    "Eres un asesor financiero senior. Genera alerta diaria para Telegram en espanol. "
    "Maximo 30 lineas con emojis. Estructura EXACTA: "
    "ALERTA PREMERCADO [fecha] | "
    "MERCADO (S&P Nasdaq BTC variacion) | "
    "PORTAFOLIO HOY USD: usar SIEMPRE el NET LIQUIDATION (ya descuenta margen), NO el valor bruto. Mostrar: Net Liq, Cash/Margen, P&L | "
    "PORTAFOLIO EN COP: Net Liq en pesos, P&L COP, efecto divisa, vs IBR | "
    "OPCIONES: estado | "
    "ACCION DEL DIA: una sola, EJECUTAR/ESPERAR/NO EJECUTAR | "
    "EVENTO CLAVE HOY"
)
WEEKLY_PROMPT = (
    "Eres un asesor financiero senior. Genera recomendaciones en JSON valido sin backticks. "
    "Exactamente 4 recs y 6 hyps. "
    '{"recs":[{"type":"action","icon":"emoji","title":"titulo","badge":"br",'
    '"badgeText":"texto","body":"explicacion","tags":["TAG"]}],'
    '"hyps":[{"id":"id","cls":"semi","icon":"emoji","name":"nombre",'
    '"riskLbl":"Alto","risk":70,"riskColor":"#f85149","horizon":"3 meses",'
    '"tickers":["T"],"thesis":"tesis","directo":"entrada con precio y stop",'
    '"opciones":"estrategia con strikes","sizing":"capital a usar",'
    '"catalizadores":"eventos clave"}]} '
    "Se especifico con tickers, precios y estrategias de opciones."
)

def generate_daily_alert(context_str, spx, btc, btc_chg, cop_info):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=700,
                 system=ALERT_PROMPT,
                 messages=[{"role":"user","content":
                     "Hoy es " + fecha_es(datetime.now()) + ".\n\n" + context_str +
                     "\nCOP: USD/COP=" + str(int(cop_info["usdcop"])) +
                     " | Net Liq COP=" + "${:,.0f}".format(cop_info["net_liq_cop"]) +
                     " | P&L COP=" + "${:,.0f}".format(cop_info["pnl_cop"]) + " (" + str(cop_info["pnl_pct"]) + "%)" +
                     " | Efecto mercado=" + "${:,.0f}".format(cop_info["mkt_eff"]) +
                     " | Efecto divisa=" + "${:,.0f}".format(cop_info["fx_eff"]) +
                     " | vs IBR " + str(cop_info["ibr"]) + "%: " + str(cop_info["vs_ibr"]) + "pts" +
                     "\nRECORDATORIO: el valor del portafolio es NET LIQUIDATION = valor bruto + cash (negativo si hay margen). NO usar valor bruto." +
                     "\nFOMC 17-18 jun. BTC $" + str(btc) + " (" + str(round(btc_chg,1)) + "%)."
                 }])
    return msg.content[0].text

def generate_weekly_recs(context_str):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    content = ("Hoy es " + datetime.now().strftime("%A %d de %B de %Y") + ".\n\n" + context_str +
               "\nS&P YTD +8.2%, objetivo alfa S&P+1pt, buying power ~$9,116, FOMC 17-18 jun.")
    for attempt in range(2):
        msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=6000,
                  system=WEEKLY_PROMPT, messages=[{"role":"user","content":content}])
        raw = msg.content[0].text.strip()
        if raw.startswith("```"): raw = raw.split("```")[1]; raw = raw[4:] if raw.startswith("json") else raw
        try: return json.loads(raw.strip())
        except json.JSONDecodeError as e:
            print("JSON error intento " + str(attempt+1) + ": " + str(e))
            content += "\nIMPORTANTE: JSON mas conciso, 1 oracion por campo."
    raise ValueError("JSON invalido despues de 2 intentos")

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    today = datetime.now()
    print("=" * 50)

    print("1. Obteniendo posiciones IBKR Flex...")
    ibkr_data = get_ibkr_data()
    cash_balance = ibkr_data["cash_balance"] if ibkr_data else CASH_BALANCE_FALLBACK

    print("2. Obteniendo precios Yahoo Finance (directo Python)...")
    idx_tickers  = ["^GSPC","^IXIC","^VIX","GLD","BNO","COP=X"]
    pos_tickers  = [p["ticker"] for p in PORTFOLIO_FALLBACK]
    yahoo_prices = get_yahoo_prices(idx_tickers + pos_tickers)

    print("3. Obteniendo cripto CoinGecko...")
    crypto = get_crypto()

    print("4. Obteniendo IBR BanRep...")
    ibr_annual = get_ibr()

    print("5. Calculando tasa COP promedio...")
    usdcop_raw = yahoo_prices.get("COP=X",{}).get("price") or 4200
    # Yahoo Finance COP=X a veces devuelve el inverso (USD por COP ~0.00024)
    if usdcop_raw < 10:
        usdcop_raw = round(1.0 / usdcop_raw, 2)
        print("COP=X era inverso, corregido a: " + str(usdcop_raw))
    deposits     = ibkr_data.get("deposits",[]) if ibkr_data else []
    avg_purchase = get_avg_purchase_cop(deposits, usdcop_raw)

    print("6. Guardando prices.json y market_config.json...")
    save_prices_json(ibkr_data, yahoo_prices, crypto, usdcop_raw, ibr_annual, avg_purchase)
    save_market_config(usdcop_raw, ibr_annual, avg_purchase, cash_balance)

    print("7. Construyendo contexto...")
    context_str, net_pnl, net_pnlp, net_liq, cost_usd, spx, btc, btc_chg, opt_value = build_context(
        ibkr_data, yahoo_prices, crypto, cash_balance
    )
    print(context_str)

    print("8. Calculando metricas COP...")
    usdcop     = round(usdcop_raw * COP_SPREAD, 2)
    net_liq_c  = net_liq  * usdcop
    cost_cop   = cost_usd * avg_purchase
    pnl_cop    = net_liq_c - cost_cop
    pnl_pct    = round((pnl_cop / cost_cop * 100) if cost_cop else 0, 2)
    mkt_eff    = net_pnl  * usdcop
    fx_eff     = cost_usd * (usdcop - avg_purchase)
    vs_ibr     = round(pnl_pct - ibr_annual, 2)
    cop_info   = {"usdcop":usdcop,"net_liq_cop":net_liq_c,"pnl_cop":pnl_cop,
                  "pnl_pct":pnl_pct,"mkt_eff":mkt_eff,"fx_eff":fx_eff,
                  "ibr":ibr_annual,"vs_ibr":vs_ibr}

    print("8b. Guardando portfolio_history.json...")
    save_portfolio_history({
        "date":         today.strftime("%Y-%m-%d"),
        "val_usd":      round(net_liq, 2),
        "pos_usd":      round(net_liq - cash_balance - opt_value, 2),
        "opt_mkt_usd":  round(opt_value, 2),
        "cash_balance": round(cash_balance, 2),
        "cost_usd":     round(cost_usd, 2),
        "pnl_usd":      round(net_pnl, 2),
        "pnl_pct_usd":  round(net_pnlp, 3),
        "val_cop":      round(net_liq_c, 0),
        "cost_cop":     round(cost_cop, 0),
        "pnl_cop":      round(pnl_cop, 0),
        "pnl_pct_cop":  round(pnl_pct, 3),
        "mkt_effect":   round(mkt_eff, 0),
        "fx_effect":    round(fx_eff, 0),
        "usdcop":       round(usdcop, 2),
        "avg_purchase": round(avg_purchase, 2),
        "ibr_annual":   ibr_annual,
        "vs_ibr":       round(vs_ibr, 3),
        "spx":          spx.get("price"),
        "btc":          btc,
    })

    print("9. Generando y enviando alerta Telegram...")
    alert = generate_daily_alert(context_str, spx, btc, btc_chg, cop_info)
    print(alert)
    send_telegram(alert)

    print("10. Verificando dias estrategicos...")
    strategic_days = get_strategic_days(today, context_str)
    current_day    = today.weekday() + 1

    if current_day in strategic_days:
        print("11. Dia estrategico - generando recomendaciones...")
        data = generate_weekly_recs(context_str)
        data["generated"] = today.isoformat()
        data["week"]      = today.strftime("%Y-%m-%d")
        with open("recommendations.json","w",encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("recommendations.json: " + str(len(data["recs"])) + " recs, " + str(len(data["hyps"])) + " hyps")
    else:
        print("Hoy no es dia estrategico - solo alerta diaria")

    print("=" * 50)
    print("Completado.")
