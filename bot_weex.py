"""
WEEX MASTER AUTONOMOUS BOT: 3 ESTRATEGIAS + MULTI-TIMEFRAME + PnL NETO CON COMISIONES
- 3 Estrategias seleccionables: Agresiva, Moderada y Conservadora.
- Multi-Temporalidad: Confirma tendencia en 15m y entrada precisa en 1m/5m.
- PnL Neto Real: Descuenta automáticamente las comisiones del exchange en cada cálculo.
- Cooldown tras Stop Loss: Evita recomprar un activo en caída por 5 minutos.
- Control total desde Telegram con comandos interactivos (/estrategia, /pnl, /posiciones).
- Servidor keep_alive para Render 24/7 sin suspensión.
"""

import os
import sqlite3
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import ccxt
import pandas as pd
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from keep_alive import start_keep_alive

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("weex-master")

# ==================== CONFIGURACIÓN GENERAL ====================
API_KEY = os.getenv("WEEX_API_KEY", "")
API_SECRET = os.getenv("WEEX_API_SECRET", "")
API_PASSPHRASE = os.getenv("WEEX_API_PASSPHRASE", os.getenv("WEEX_API_PASSWORD", ""))
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Modos de ejecución
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
ALLOW_LIVE = os.getenv("ALLOW_LIVE_TRADING", "true").lower() == "true"
MARKET_TYPE = os.getenv("MARKET_TYPE", "spot")

# Temporalidades
TIMEFRAME_MACRO = os.getenv("TIMEFRAME", "15m")       # Tendencia general
TIMEFRAME_MICRO = os.getenv("ENTRY_TIMEFRAME", "1m")   # Gatillo de entrada precisa
INTERVAL = max(20, int(os.getenv("CHECK_INTERVAL_SEC", "35"))) # Escaneo cada 35s
AMOUNT_USDT = float(os.getenv("TRADE_AMOUNT_USDT", "10"))

# Comisión del exchange estimada por operación (0.2% por lado)
FEE_PCT = float(os.getenv("FEE_PCT", "0.002")) 

# Cooldown tras Stop Loss (en segundos)
COOLDOWN_SL_SEC = int(os.getenv("COOLDOWN_AFTER_SL_SEC", "300"))
cooldown_tracker = {} # symbol -> timestamp

# ==================== 3 ESTRATEGIAS DE TRADING ====================
STRATEGIES = {
    "conservadora": {
        "name": "🛡️ Conservadora",
        "tp_pct": 3.0,     # +3.0% para superar holgadamente las comisiones
        "sl_pct": 1.2,     # -1.2% corte rápido de pérdidas
        "min_score": 78,   # Muy exigente
        "desc": "Alta selectividad. Pocas operaciones pero de alta probabilidad.",
    },
    "moderada": {
        "name": "⚖️ Moderada",
        "tp_pct": 2.4,     # +2.4% ganancia sólida neta
        "sl_pct": 1.5,     # -1.5%
        "min_score": 70,   # Equilibrada
        "desc": "Equilibrio ideal entre frecuencia y protección de capital.",
    },
    "agresiva": {
        "name": "⚡ Agresiva",
        "tp_pct": 2.0,     # +2.0% scalping rápido
        "sl_pct": 1.4,     # -1.4%
        "min_score": 62,   # Mayor frecuencia
        "desc": "Scalping activo con mayor cantidad de entradas.",
    }
}

ACTIVE_STRATEGY = os.getenv("ESTRATEGIA", "moderada").lower()
if ACTIVE_STRATEGY not in STRATEGIES:
    ACTIVE_STRATEGY = "moderada"

AUTOTRADE_ENABLED = os.getenv("AUTOTRADE_ENABLED", "true").lower() == "true"
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))

WATCHLIST = [x.strip().upper() for x in os.getenv(
    "SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT,DOGE/USDT,XRP/USDT").split(",") if x.strip()]

DB_PATH = Path("weex_master_pnl.db")
exchange = None

# ==================== BASE DE DATOS & REGISTRO ====================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT UNIQUE NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                amount_coin REAL NOT NULL,
                amount_usdt REAL NOT NULL,
                tp_price REAL NOT NULL,
                sl_price REAL NOT NULL,
                strategy_name TEXT NOT NULL,
                entry_score INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                amount_coin REAL NOT NULL,
                amount_usdt REAL NOT NULL,
                pnl_bruto_usdt REAL NOT NULL,
                comision_usdt REAL NOT NULL,
                pnl_neto_usdt REAL NOT NULL,
                pnl_neto_pct REAL NOT NULL,
                strategy_name TEXT NOT NULL,
                close_reason TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT NOT NULL
            )
        """)
        conn.commit()

def get_open_positions():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM live_positions WHERE status = 'OPEN'")
        return [dict(row) for row in cur.fetchall()]

def save_new_position(symbol, side, entry_price, amount_coin, amount_usdt, tp_price, sl_price, strategy, score):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO live_positions 
            (symbol, side, entry_price, amount_coin, amount_usdt, tp_price, sl_price, strategy_name, entry_score, opened_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
        """, (symbol, side, entry_price, amount_coin, amount_usdt, tp_price, sl_price, strategy, score, datetime.now(timezone.utc).isoformat()))
        conn.commit()

def record_closed_trade(pos, exit_price, pnl_bruto, comision, pnl_neto, pnl_neto_pct, reason):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trade_history 
            (symbol, side, entry_price, exit_price, amount_coin, amount_usdt, pnl_bruto_usdt, comision_usdt, pnl_neto_usdt, pnl_neto_pct, strategy_name, close_reason, opened_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pos["symbol"], pos["side"], pos["entry_price"], exit_price, pos["amount_coin"], 
            pos["amount_usdt"], pnl_bruto, comision, pnl_neto, pnl_neto_pct, pos.get("strategy_name", ACTIVE_STRATEGY),
            reason, pos["opened_at"], datetime.now(timezone.utc).isoformat()
        ))
        cur.execute("DELETE FROM live_positions WHERE symbol = ?", (pos["symbol"],))
        conn.commit()

def get_pnl_summary():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                COUNT(*),
                SUM(CASE WHEN pnl_neto_usdt > 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN pnl_neto_usdt <= 0 THEN 1 ELSE 0 END),
                SUM(pnl_neto_usdt),
                SUM(comision_usdt),
                AVG(pnl_neto_pct)
            FROM trade_history
        """)
        row = cur.fetchone()
        total, wins, losses, total_neto, total_fees, avg_pct = row

    total = total or 0
    wins = wins or 0
    losses = losses or 0
    total_neto = total_neto or 0.0
    total_fees = total_fees or 0.0
    avg_pct = avg_pct or 0.0
    winrate = (wins / total * 100) if total > 0 else 0.0

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "total_neto": total_neto,
        "total_fees": total_fees,
        "avg_pct": avg_pct,
        "winrate": winrate
    }

# ==================== TELEGRAM GUARD & EXCHANGE ====================
def authorized(update: Update) -> bool:
    if not CHAT_ID:
        return True
    return bool(update.effective_chat and str(update.effective_chat.id) == CHAT_ID)

async def guard(update: Update) -> bool:
    if not authorized(update):
        if update.effective_message:
            await update.effective_message.reply_text("No autorizado.")
        return False
    return True

def get_exchange():
    config = {"enableRateLimit": True, "options": {"defaultType": MARKET_TYPE}}
    if API_KEY and API_SECRET and API_PASSPHRASE:
        config.update({"apiKey": API_KEY, "secret": API_SECRET, "password": API_PASSPHRASE})
    return ccxt.weex(config)

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-12)
    return 100 - (100 / (1 + rs))

# ==================== ANÁLISIS MULTI-TEMPORALIDAD ====================
def analyze_multi_timeframe(symbol: str) -> dict:
    # 1. Macro (15m): Tendencia General
    candles_macro = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_MACRO, limit=60)
    if not candles_macro or len(candles_macro) < 25:
        raise ValueError("Historial macro insuficiente")
    df_m = pd.DataFrame(candles_macro, columns=["ts", "open", "high", "low", "close", "vol"])
    c_m = df_m["close"]
    ema9_m = c_m.ewm(span=9, adjust=False).mean().iloc[-1]
    ema21_m = c_m.ewm(span=21, adjust=False).mean().iloc[-1]
    tendencia_alcista = ema9_m > ema21_m

    # 2. Micro (1m): Gatillo de Entrada
    candles_micro = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME_MICRO, limit=60)
    if not candles_micro or len(candles_micro) < 25:
        raise ValueError("Historial micro insuficiente")
    df_u = pd.DataFrame(candles_micro, columns=["ts", "open", "high", "low", "close", "vol"])
    c_u = df_u["close"]
    ema9_u = c_u.ewm(span=9, adjust=False).mean().iloc[-1]
    ema21_u = c_u.ewm(span=21, adjust=False).mean().iloc[-1]
    rsi_u = float(rsi(c_u, 14).iloc[-1])
    vol_avg = df_u["vol"].rolling(15).mean().iloc[-1]
    vol_ratio = float(df_u["vol"].iloc[-1] / vol_avg) if pd.notna(vol_avg) and vol_avg > 0 else 1.0

    last_price = float(c_u.iloc[-1])

    score = 0
    if tendencia_alcista:
        score += 35
    if ema9_u > ema21_u:
        score += 20
    if 40 <= rsi_u <= 65:
        score += 25
    elif 30 <= rsi_u < 40:
        score += 15
    if vol_ratio >= 1.2:
        score += 20

    signal = "ALCISTA" if (tendencia_alcista and score >= 60) else "NEUTRAL"
    return {
        "symbol": symbol,
        "price": last_price,
        "score": score,
        "rsi": rsi_u,
        "macro_bullish": tendencia_alcista,
        "volume_ratio": vol_ratio,
        "signal": signal,
    }

# ==================== EJECUTOR DE ÓRDENES ====================
def execute_order(symbol: str, side: str, amount_coin: float, reason_text: str = "") -> dict:
    is_live = (not DRY_RUN) and ALLOW_LIVE
    mode_tag = "🔴 [REAL]" if is_live else "🧪 [SIMULACIÓN]"
    log.info("%s Orden %s %s por %s unidades. (%s)", mode_tag, side.upper(), symbol, amount_coin, reason_text)

    if not is_live:
        return {
            "success": True,
            "id": f"sim-{int(datetime.now().timestamp())}",
            "is_live": False,
            "amount_coin": amount_coin,
            "side": side,
        }

    try:
        order = exchange.create_market_order(symbol, side, amount_coin)
        return {
            "success": True,
            "id": order.get("id", "desconocido"),
            "is_live": True,
            "amount_coin": amount_coin,
            "side": side,
        }
    except Exception as exc:
        log.error("Fallo al enviar orden a WEEX: %s", exc)
        return {"success": False, "error": str(exc), "is_live": True}

# ==================== BUCLE DE TRADING ACTIVO ====================
async def master_trading_engine(context: ContextTypes.DEFAULT_TYPE):
    global AUTOTRADE_ENABLED
    if not exchange:
        return

    strat = STRATEGIES[ACTIVE_STRATEGY]
    tp_pct = strat["tp_pct"]
    sl_pct = strat["sl_pct"]
    min_score = strat["min_score"]

    now_ts = datetime.now().timestamp()

    # 1. MONITOREO DE POSICIONES ABIERTAS (CIERRES EN TP O SL)
    open_positions = get_open_positions()
    for pos in open_positions:
        sym = pos["symbol"]
        entry_price = pos["entry_price"]
        amount_coin = pos["amount_coin"]
        amount_usdt = pos["amount_usdt"]
        tp_price = pos["tp_price"]
        sl_price = pos["sl_price"]

        try:
            ticker = exchange.fetch_ticker(sym)
            curr_price = float(ticker["last"])
        except Exception as e:
            continue

        pnl_bruto_pct = ((curr_price - entry_price) / entry_price) * 100
        pnl_bruto_usdt = (curr_price - entry_price) * amount_coin

        comision_estimada = (amount_usdt * FEE_PCT) * 2
        pnl_neto_usdt = pnl_bruto_usdt - comision_estimada
        pnl_neto_pct = pnl_bruto_pct - (FEE_PCT * 200)

        # A) Take Profit Alcanzado (GANANCIA NETA)
        if curr_price >= tp_price:
            order_res = execute_order(sym, "sell", amount_coin, f"TP Alcanzado (+{pnl_neto_pct:.2f}% neto)")
            record_closed_trade(pos, curr_price, pnl_bruto_usdt, comision_estimada, pnl_neto_usdt, pnl_neto_pct, "TAKE_PROFIT")

            summary = get_pnl_summary()
            sign = "🟢 +" if pnl_neto_usdt >= 0 else "🔴 "
            msg = (
                f"🎯 *¡TAKE PROFIT ALCANZADO!*\n"
                f"Par: `{sym}` | Estrategia: *{strat['name']}*\n"
                f"Entrada: `${entry_price:g}` ➡️ Salida: `${curr_price:g}`\n"
                f"PnL Neto: *{sign}{pnl_neto_usdt:.4f} USDT ({pnl_neto_pct:+.2f}%)*\n"
                f"_(Comisión descontada: -${comision_estimada:.4f} USDT)_\n\n"
                f"💰 *PnL NETO ACUMULADO:* `{summary['total_neto']:+.4f} USDT`\n"
                f"Acierto: `{summary['winrate']:.1f}%` ({summary['wins']} Ganadas / {summary['losses']} Perdidas)\n"
                f"Modo: {'🔴 Dinero Real' if order_res.get('is_live') else '🧪 Simulación'}"
            )
            if CHAT_ID:
                try:
                    await context.bot.send_message(chat_id=int(CHAT_ID), text=msg, parse_mode="Markdown")
                except Exception:
                    pass

        # B) Stop Loss Alcanzado (PROTECCIÓN)
        elif curr_price <= sl_price:
            order_res = execute_order(sym, "sell", amount_coin, f"SL Ejecutado ({pnl_neto_pct:.2f}% neto)")
            record_closed_trade(pos, curr_price, pnl_bruto_usdt, comision_estimada, pnl_neto_usdt, pnl_neto_pct, "STOP_LOSS")

            cooldown_tracker[sym] = now_ts + COOLDOWN_SL_SEC

            summary = get_pnl_summary()
            msg = (
                f"🛑 *STOP LOSS ACTIVADO*\n"
                f"Par: `{sym}` | Estrategia: *{strat['name']}*\n"
                f"Entrada: `${entry_price:g}` ➡️ Salida: `${curr_price:g}`\n"
                f"PnL Neto: `🔴 {pnl_neto_usdt:.4f} USDT ({pnl_neto_pct:+.2f}%)`\n"
                f"⏳ Cooldown: Pausando {sym} por 5 minutos para evitar caídas.\n"
                f"💰 *PnL NETO ACUMULADO:* `{summary['total_neto']:+.4f} USDT`\n"
                f"Modo: {'🔴 Dinero Real' if order_res.get('is_live') else '🧪 Simulación'}"
            )
            if CHAT_ID:
                try:
                    await context.bot.send_message(chat_id=int(CHAT_ID), text=msg, parse_mode="Markdown")
                except Exception:
                    pass

    # 2. BÚSQUEDA Y APERTURA DE NUEVAS OPERACIONES
    if not AUTOTRADE_ENABLED:
        return

    active_symbols = {p["symbol"] for p in get_open_positions()}
    if len(active_symbols) >= MAX_OPEN_POSITIONS:
        return

    for sym in WATCHLIST:
        if sym in active_symbols:
            continue
        if len(active_symbols) >= MAX_OPEN_POSITIONS:
            break

        if sym in cooldown_tracker and now_ts < cooldown_tracker[sym]:
            continue

        try:
            tech = analyze_multi_timeframe(sym)
            score = tech["score"]
            price = tech["price"]

            if score >= min_score and tech["signal"] == "ALCISTA":
                amount_coin = float(exchange.amount_to_precision(sym, AMOUNT_USDT / price))
                if amount_coin <= 0:
                    continue

                tp_price = price * (1 + (tp_pct / 100))
                sl_price = price * (1 - (sl_pct / 100))

                order_res = execute_order(sym, "buy", amount_coin, f"Entrada {strat['name']} Score={score}")
                if order_res.get("success"):
                    save_new_position(sym, "buy", price, amount_coin, AMOUNT_USDT, tp_price, sl_price, ACTIVE_STRATEGY, score)
                    active_symbols.add(sym)

                    msg = (
                        f"🚀 *NUEVA COMPRA MULTI-TIMEFRAME*\n"
                        f"Par: `{sym}` | Estrategia: *{strat['name']}*\n"
                        f"Precio: `${price:g}` | Inversión: `{AMOUNT_USDT} USDT`\n"
                        f"🎯 Take Profit (+{tp_pct}%): `${tp_price:.4f}`\n"
                        f"🛑 Stop Loss (-{sl_pct}%): `${sl_price:.4f}`\n"
                        f"Score: *{score}/100* (Tendencia 15m + Gatillo 1m)\n"
                        f"Modo: {'🔴 Dinero Real' if order_res.get('is_live') else '🧪 Simulación'}"
                    )
                    if CHAT_ID:
                        try:
                            await context.bot.send_message(chat_id=int(CHAT_ID), text=msg, parse_mode="Markdown")
                        except Exception:
                            pass
        except Exception as e:
            log.warning("Escaneo de %s: %s", sym, e)

# ==================== COMANDOS TELEGRAM ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    strat = STRATEGIES[ACTIVE_STRATEGY]
    msg = (
        "🤖 *WEEX MASTER BOT (MULTI-ESTRATEGIA & PnL NETO)*\n\n"
        f"Estrategia actual: *{strat['name']}*\n\n"
        "Comandos principales:\n"
        "• `/estrategia` - Ver o cambiar estrategia (`agresiva`, `moderada`, `conservadora`)\n"
        "• `/pnl` - Ganancias netas en USDT (con comisiones descontadas)\n"
        "• `/posiciones` - Operaciones abiertas y ganancia flotante en vivo\n"
        "• `/cerrar BTC/USDT` - Cerrar una posición a mercado\n"
        "• `/autotrade on|off` - Activar o pausar el auto-trading\n"
        "• `/balance` - Saldo libre en WEEX\n"
        "• `/estado` - Configuración general y estado del bot\n"
        "• `/precio BTC/USDT` - Precio actual\n"
    )
    await update.effective_message.reply_text(msg, parse_mode="Markdown")

async def cmd_estrategia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    global ACTIVE_STRATEGY
    if context.args:
        eleccion = context.args[0].lower()
        if eleccion in STRATEGIES:
            ACTIVE_STRATEGY = eleccion
            s = STRATEGIES[ACTIVE_STRATEGY]
            await update.effective_message.reply_text(
                f"✅ Estrategia cambiada a: *{s['name']}*\n"
                f"• Take Profit: `+{s['tp_pct']}%`\n"
                f"• Stop Loss: `-{s['sl_pct']}%`\n"
                f"• Score Mínimo: `{s['min_score']}/100`\n"
                f"• {s['desc']}",
                parse_mode="Markdown"
            )
            return

    lines = ["📊 *ESTRATEGIAS DISPONIBLES:*\n"]
    for k, v in STRATEGIES.items():
        actual = "👉 *(ACTUAL)* " if k == ACTIVE_STRATEGY else "• "
        lines.append(f"{actual}*{v['name']}* (`/estrategia {k}`)\n  TP: `+{v['tp_pct']}%` | SL: `-{v['sl_pct']}%` | Score: `≥{v['min_score']}`\n  _{v['desc']}_\n")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    summary = get_pnl_summary()
    open_pos = get_open_positions()

    floating_neto = 0.0
    for p in open_pos:
        try:
            curr = float(exchange.fetch_ticker(p["symbol"])["last"])
            bruto = (curr - p["entry_price"]) * p["amount_coin"]
            fee = (p["amount_usdt"] * FEE_PCT) * 2
            floating_neto += (bruto - fee)
        except Exception:
            pass

    color = "🟢 +" if summary["total_neto"] >= 0 else "🔴 "
    msg = (
        "💰 *REPORTE FINANCIERO (PnL NETO REAL)*\n\n"
        f"• Ganancia Neta Total: *{color}{summary['total_neto']:.4f} USDT*\n"
        f"• PnL Flotante en Curso: *{floating_neto:+.4f} USDT*\n"
        f"• Comisiones Totales Pagadas: `-${summary['total_fees']:.4f} USDT`\n"
        f"• Operaciones Ganadoras: *{summary['wins']}* 🎯\n"
        f"• Operaciones Perdedoras: *{summary['losses']}* 🛑\n"
        f"• Tasa de Acierto (Winrate): *{summary['winrate']:.1f}%*\n"
        f"• Total Operaciones: *{summary['total']}*\n\n"
        "_Todas las ganancias reportadas son 100% netas con comisiones descontadas._"
    )
    await update.effective_message.reply_text(msg, parse_mode="Markdown")

async def cmd_posiciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    positions = get_open_positions()
    if not positions:
        await update.effective_message.reply_text("ℹ️ No hay posiciones abiertas actualmente. Escaneando oportunidades...")
        return

    lines = ["📊 *POSICIONES ABIERTAS (PnL NETO EN VIVO):*\n"]
    for p in positions:
        sym = p["symbol"]
        entry = p["entry_price"]
        amount_coin = p["amount_coin"]
        amount_usdt = p["amount_usdt"]
        tp = p["tp_price"]
        sl = p["sl_price"]
        strat_name = p.get("strategy_name", ACTIVE_STRATEGY)
        try:
            curr = float(exchange.fetch_ticker(sym)["last"])
            bruto_pct = ((curr - entry) / entry) * 100
            fee_usdt = (amount_usdt * FEE_PCT) * 2
            neto_usdt = ((curr - entry) * amount_coin) - fee_usdt
            neto_pct = bruto_pct - (FEE_PCT * 200)

            badge = "🟢" if neto_pct >= 0 else "🔴"
            lines.append(
                f"{badge} *{sym}* ({strat_name.capitalize()})\n"
                f"• Entrada: `${entry:g}` ➡️ Actual: `${curr:g}`\n"
                f"• PnL Neto: *{neto_pct:+.2f}%* ({neto_usdt:+.4f} USDT)\n"
                f"• TP: `${tp:.4f}` | SL: `${sl:.4f}`\n"
            )
        except Exception:
            lines.append(f"• *{sym}*: Entrada `${entry:g}`")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_cerrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.effective_message.reply_text("Uso: `/cerrar BTC/USDT`", parse_mode="Markdown")
        return

    sym = context.args[0].upper()
    positions = [p for p in get_open_positions() if p["symbol"] == sym]
    if not positions:
        await update.effective_message.reply_text(f"No hay posiciones abiertas para {sym}.")
        return

    pos = positions[0]
    amount_coin = pos["amount_coin"]
    entry_price = pos["entry_price"]
    amount_usdt = pos["amount_usdt"]

    try:
        curr = float(exchange.fetch_ticker(sym)["last"])
        pnl_bruto = (curr - entry_price) * amount_coin
        comision = (amount_usdt * FEE_PCT) * 2
        pnl_neto = pnl_bruto - comision
        pnl_neto_pct = ((curr - entry_price) / entry_price * 100) - (FEE_PCT * 200)

        order_res = execute_order(sym, "sell", amount_coin, "Cierre manual")
        record_closed_trade(pos, curr, pnl_bruto, comision, pnl_neto, pnl_neto_pct, "CIERRE_MANUAL")

        await update.effective_message.reply_text(
            f"✅ Posición {sym} cerrada a mercado.\n"
            f"Salida: `${curr:g}` | PnL Neto: *{pnl_neto_pct:+.2f}%* ({pnl_neto:+.4f} USDT)\n"
            f"Modo: {'🔴 Real' if order_res.get('is_live') else '🧪 Simulación'}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.effective_message.reply_text(f"Error al cerrar posición {sym}: {e}")

async def cmd_autotrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    global AUTOTRADE_ENABLED
    arg = context.args[0].lower() if context.args else ""
    if arg == "on":
        AUTOTRADE_ENABLED = True
        await update.effective_message.reply_text("✅ *Auto-Trading ACTIVADO*.", parse_mode="Markdown")
    elif arg == "off":
        AUTOTRADE_ENABLED = False
        await update.effective_message.reply_text("⏸️ *Auto-Trading PAUSADO*.", parse_mode="Markdown")
    else:
        status = "ACTIVADO ✅" if AUTOTRADE_ENABLED else "PAUSADO ⏸️"
        await update.effective_message.reply_text(f"Estado del Auto-Trading: *{status}*\nUsa `/autotrade on` o `/autotrade off`", parse_mode="Markdown")

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    try:
        bal = exchange.fetch_balance()
        rows = [f"• *{c}*: `{v}`" for c, v in bal.get("total", {}).items() if v and v > 0.0001]
        await update.effective_message.reply_text(
            "💼 *Balance en WEEX:*\n" + ("\n".join(rows) if rows else "Sin fondos detectados."),
            parse_mode="Markdown"
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Error consultando balance: {exc}")

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    open_count = len(get_open_positions())
    is_live = (not DRY_RUN) and ALLOW_LIVE
    s = STRATEGIES[ACTIVE_STRATEGY]
    await update.effective_message.reply_text(
        f"⚙️ *ESTADO DEL MASTER BOT*\n\n"
        f"• Estrategia Activa: *{s['name']}*\n"
        f"• Take Profit: `+{s['tp_pct']}%` | Stop Loss: `-{s['sl_pct']}%`\n"
        f"• Auto-Trading: *{'ACTIVADO ✅' if AUTOTRADE_ENABLED else 'PAUSADO ⏸️'}*\n"
        f"• Modo Órdenes: *{'🔴 DINERO REAL' if is_live else '🧪 SIMULACIÓN (DRY_RUN)'}*\n"
        f"• Posiciones abiertas: *{open_count}/{MAX_OPEN_POSITIONS}*\n"
        f"• Monto por trade: *{AMOUNT_USDT} USDT*\n"
        f"• Comisión estimada considerada: `{(FEE_PCT*100):.2f}% por lado`\n"
        f"• Watchlist: `{', '.join(WATCHLIST)}`\n"
        f"• Temporalidades: Macro `{TIMEFRAME_MACRO}` + Micro `{TIMEFRAME_MICRO}`",
        parse_mode="Markdown"
    )

async def cmd_precio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    sym = (context.args[0] if context.args else "BTC/USDT").upper()
    try:
        t = exchange.fetch_ticker(sym)
        await update.effective_message.reply_text(
            f"💰 *{sym}*: `${t.get('last')}`\n"
            f"24h: `{t.get('percentage', '?')}%` | Máx: `${t.get('high')}` | Mín: `${t.get('low')}`",
            parse_mode="Markdown"
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"No pude obtener {sym}: {exc}")

# ==================== INICIO PRINCIPAL ====================
def main():
    global exchange
    if not TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN.")

    init_db()
    exchange = get_exchange()
    try:
        exchange.load_markets()
        log.info("WEEX conectado. %s mercados listos.", len(exchange.markets))
    except Exception as e:
        log.error("Aviso al cargar mercados: %s", e)

    app = ApplicationBuilder().token(TOKEN).build()

    for name, callback in [
        ("start", cmd_start),
        ("help", cmd_start),
        ("estrategia", cmd_estrategia),
        ("pnl", cmd_pnl),
        ("posiciones", cmd_posiciones),
        ("cerrar", cmd_cerrar),
        ("autotrade", cmd_autotrade),
        ("balance", cmd_balance),
        ("estado", cmd_estado),
        ("precio", cmd_precio),
    ]:
        app.add_handler(CommandHandler(name, callback))

    app.job_queue.run_repeating(master_trading_engine, interval=INTERVAL, first=10)
    log.info("Motor Master activo cada %s segundos.", INTERVAL)

    start_keep_alive()
    app.run_polling()

if __name__ == "__main__":
    main()
