"""
ibkr_bridge.py
Servidor local que lee posiciones de IBKR TWS en tiempo real
y las expone en http://localhost:5050/positions para el dashboard.

Instalación (una sola vez):
  pip install ib_insync flask flask-cors

Uso:
  1. Abrir TWS y hacer login
  2. En TWS: Edit → Global Configuration → API → Settings
       - Activar "Enable ActiveX and Socket Clients"
       - Socket port: 7497 (paper) o 7496 (real)
       - Desactivar "Read-Only API"
  3. Correr: python ibkr_bridge.py
  4. Dejar corriendo mientras usas el dashboard
"""

from flask import Flask, jsonify
from flask_cors import CORS
from ib_insync import IB, util
import threading, time, logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # permite que el dashboard en GitHub Pages consuma la API

# ── CONFIG ────────────────────────────────────────────────────────────────
TWS_HOST   = '127.0.0.1'
TWS_PORT   = 7496   # 7496 = cuenta real | 7497 = paper trading
CLIENT_ID  = 10     # cualquier número, solo que no lo use otra app
REFRESH_SEC = 15    # actualizar posiciones cada N segundos

# ── ESTADO COMPARTIDO ─────────────────────────────────────────────────────
state = {
    'positions': [],
    'summary': {},
    'last_update': None,
    'connected': False
}

# ── CONEXIÓN IBKR ─────────────────────────────────────────────────────────
def connect_ibkr():
    import asyncio
    # ib_insync necesita un event loop en el hilo donde corre
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    ib = IB()
    while True:
        try:
            log.info(f"Conectando a TWS en {TWS_HOST}:{TWS_PORT}...")
            ib.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID, readonly=False)
            state['connected'] = True
            log.info("✅ Conectado a IBKR TWS")

            while ib.isConnected():
                fetch_data(ib)
                ib.sleep(REFRESH_SEC)   # usar ib.sleep en vez de time.sleep para mantener el loop activo

        except Exception as e:
            log.warning(f"❌ Error IBKR: {e}. Reintentando en 30s...")
            state['connected'] = False
            try: ib.disconnect()
            except: pass
            time.sleep(30)

def fetch_data(ib):
    try:
        # Posiciones
        raw_positions = ib.positions()
        positions = []
        for p in raw_positions:
            contract = p.contract
            # Solicitar datos de mercado para obtener precio actual
            ticker = ib.reqMktData(contract, '', True, False)
            ib.sleep(1)
            mkt_price = ticker.marketPrice()
            mkt_price = round(mkt_price, 4) if mkt_price and mkt_price == mkt_price else None  # NaN check
            mkt_value = round(mkt_price * abs(p.position), 2) if mkt_price else None
            ib.cancelMktData(contract)
            positions.append({
                'ticker':        contract.symbol,
                'description':   contract.localSymbol or contract.symbol,
                'asset_class':   contract.secType,
                'exchange':      contract.exchange,
                'qty':           p.position,
                'avg_cost':      round(p.avgCost, 4),
                'mkt_price':     mkt_price,
                'mkt_value':     mkt_value,
                'unrealized_pnl':round((mkt_price - p.avgCost) * p.position, 2) if mkt_price else None,
                'realized_pnl':  None,
            })

        # Resumen de cuenta
        account_vals = ib.accountValues()
        summary = {}
        keys_wanted = {
            'NetLiquidation':       'net_liquidation',
            'TotalCashValue':       'cash',
            'GrossPositionValue':   'gross_position',
            'BuyingPower':          'buying_power',
            'UnrealizedPnL':        'unrealized_pnl',
            'RealizedPnL':          'realized_pnl',
            'InitMarginReq':        'initial_margin',
            'MaintMarginReq':       'maint_margin',
        }
        for av in account_vals:
            if av.tag in keys_wanted and av.currency == 'USD':
                try: summary[keys_wanted[av.tag]] = float(av.value)
                except: pass

        from datetime import datetime
        state['positions']   = positions
        state['summary']     = summary
        state['last_update'] = datetime.now().isoformat()
        log.info(f"Actualizado: {len(positions)} posiciones | Net Liq: ${summary.get('net_liquidation', 0):,.0f}")

    except Exception as e:
        log.error(f"Error obteniendo datos: {e}")

# ── ENDPOINTS HTTP ────────────────────────────────────────────────────────
@app.route('/positions')
def get_positions():
    return jsonify({
        'connected':   state['connected'],
        'last_update': state['last_update'],
        'summary':     state['summary'],
        'positions':   state['positions']
    })

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'connected': state['connected']})

# ── INICIO ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # IBKR corre en hilo separado para no bloquear Flask
    t = threading.Thread(target=connect_ibkr, daemon=True)
    t.start()

    print("\n" + "="*55)
    print("  IBKR Bridge corriendo en http://localhost:5050")
    print("  Dashboard leerá posiciones en tiempo real")
    print("  Ctrl+C para detener")
    print("="*55 + "\n")

    app.run(host='0.0.0.0', port=5050, debug=False)
