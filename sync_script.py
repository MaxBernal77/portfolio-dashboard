"""
sync_ibkr.py
Sincroniza posiciones de IBKR y actualiza portfolio_data.json
Ejecutar manualmente o vía GitHub Actions (requiere IBKR Gateway activo)

Instalación: pip install ibind requests
"""

import json, os, requests
from datetime import datetime
from ibind import IbkrClient  # pip install ibind

# ── CONFIG ────────────────────────────────────────────────────────────────
IBKR_HOST   = "localhost"   # o IP del gateway si está en otro equipo
IBKR_PORT   = 5000          # puerto del Client Portal Gateway
OUTPUT_FILE = "portfolio_data.json"  # archivo que lee el dashboard

# ── CONECTAR A IBKR ───────────────────────────────────────────────────────
client = IbkrClient(url=f"https://{IBKR_HOST}:{IBKR_PORT}", account_id=None)

def get_positions():
    resp = client.portfolio_accounts()
    account_id = resp.data[0]["id"]
    positions  = client.portfolio_positions(account_id).data
    return [
        {
            "ticker":    p["ticker"],
            "qty":       p["position"],
            "avg_cost":  p["avgPrice"],
            "mkt_price": p["mktPrice"],
            "mkt_value": p["mktValue"],
            "unrealized_pnl": p["unrealizedPnl"],
            "daily_pnl": p["dailyPnl"],
            "asset_class": p["assetClass"],
        }
        for p in positions
    ]

def get_summary():
    resp = client.portfolio_accounts()
    acct = resp.data[0]["id"]
    s    = client.portfolio_summary(acct).data
    return {
        "net_liquidation": s.get("netliquidation", {}).get("amount", 0),
        "buying_power":    s.get("buyingpower",    {}).get("amount", 0),
        "cash":            s.get("totalcashvalue", {}).get("amount", 0),
        "gross_position":  s.get("grosspositionvalue", {}).get("amount", 0),
    }

# ── MAIN ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Conectando a IBKR Gateway...")
    try:
        positions = get_positions()
        summary   = get_summary()

        data = {
            "last_sync":  datetime.now().isoformat(),
            "summary":    summary,
            "positions":  positions,
        }

        with open(OUTPUT_FILE, "w") as f:
            json.dump(data, f, indent=2)

        print(f"✅ Sincronizado: {len(positions)} posiciones guardadas en {OUTPUT_FILE}")
        print(f"   Net Liq: ${summary['net_liquidation']:,.2f}")

    except Exception as e:
        print(f"❌ Error: {e}")
        print("   Asegúrate de que IBKR Client Portal Gateway esté corriendo.")
        print("   Descarga: https://www.interactivebrokers.com/en/trading/ibkr-apis.php")
