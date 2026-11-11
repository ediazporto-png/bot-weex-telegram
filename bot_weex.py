"""
WEEX TELEGRAM AUTONOMOUS TRADING BOT CON APRENDIZAJE ADAPTATIVO & TRACKING DE PnL
- Opera de forma continua y autónoma buscando oportunidades de trading.
- Calcula y acumula el PnL neto en USDT en tiempo real.
- Motor de Aprendizaje: calibra los scores de entrada y Take Profit/Stop Loss
  en base al rendimiento de sus operaciones anteriores.
- Servidor keep_alive integrado para Render.
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
log = logging.getLogger("weex-bot")

# ==================== CONFIGURACIÓN GENERAL ====================
API_KEY = os.getenv("WEEX_API_KEY", "")
API_SECRET = os.getenv("WEEX_API_SECRET", "")
API_PASSPHRASE = os.getenv("WEEX_API_PASSPHRASE", "")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Por defecto configurado para operar continuamente
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
ALLOW_LIVE = os.getenv("ALLOW_LIVE_TRADING", "true").lower() == "true"
MARKET_TYPE = os.getenv("MARKET_TYPE", "spot")
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
INTERVAL = max(30, int(os.getenv("CHECK_INTERVAL_SEC", "45")))  # Cada 45 segundos para alta actividad
AMOUNT_USDT = float(os.getenv("TRADE_AMOUNT_USDT", "10"))

# Parámetros Base del Auto-Trading
AUTOTRADE_ENABLED = os.getenv("AUTOTRADE_ENABLED", "true").lower() == "true"
BASE_BUY_SCORE = int(os.getenv("AUTO_BUY_SCORE", "70"))
BASE_TP_PCT = float(os.getenv("AUTO_TP_PCT", "1.8"))           # Take profit rápido (+1.8%)
BASE_SL_PCT = float(os.getenv("AUTO_SL_PCT", "1.4"))           # Stop loss ajustado (-1.4%)
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))

# Indicadores técnicos
EMA_FAST = int(os.getenv("EMA_FAST", "9"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "21"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))

WATCHLIST = [x.strip().upper() for x in os.getenv(
    "SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT,DOGE/USDT,XRP/USDT").split(",") if x.strip()]

DB_PATH = Path("weex_trading_brain.db")
exchange = None

# ==================== BASE DE DATOS & MEMORIA DE APRENDIZAJE ====================
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
                entry_score INTEGER NOT NULL,
                entry_rsi REAL NOT NULL,
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
                pnl_usdt REAL NOT NULL,
                pnl_pct REAL NOT NULL,
                entry_score INTEGER,
                entry_rsi REAL,
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

def save_new_position(symbol, side, entry_price, amount_coin, amount_usdt, tp_price, sl_price, score, rsi_val):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO live_positions 
            (symbol, side, entry_price, amount_coin, amount_usdt, tp_price, sl_price, entry_score, entry_rsi, opened_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
        """, (symbol, side, entry_price, amount_coin, amount_usdt, tp_price, sl_price, score, rsi_val, datetime.now(timezone.utc).isoformat()))
        conn.commit()

def record_closed_trade(pos, exit_price, pnl_usdt, pnl_pct, reason):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trade_history 
            (symbol, side, entry_price, exit_price, amount_coin, amount_usdt, pnl_usdt, pnl_pct, entry_score, entry_rsi, close_reason, opened_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pos["symbol"], pos["side"], pos["entry_price"], exit_price, pos["amount_coin"], 
            pos["amount_usdt"], pnl_usdt, pnl_pct, pos.get("entry_score", 0), pos.get("entry_rsi", 0.0),
            reason, pos["opened_at"], datetime.now(timezone.utc).isoformat()
        ))
        cur.execute("DELETE FROM live_positions WHERE symbol = ?", (pos["symbol"],))
        conn.commit()

# ==================== MOTOR DE APRENDIZAJE ADAPTATIVO ====================
def get_adaptive_settings():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT pnl_pct, entry_score FROM trade_history ORDER BY id DESC LIMIT 15")
        rows = cur.fetchall()

    if not rows or len(rows) < 3:
        return {
            "score_threshold": BASE_BUY_SCORE,
            "tp_pct": BASE_TP_PCT,
            "sl_pct": BASE_SL_PCT,
            "confidence": "Iniciando aprendizaje (modo base)",
            "winrate": 0.0
        }

    total = len(rows)
    wins = sum(1 for r in rows if r[0] > 0)
    winrate = (wins / total) * 100

    if winrate < 50:
        adaptive_score = min(85, BASE_BUY_SCORE + 5)
        adaptive_tp = max(1.2, BASE_TP_PCT - 0.3)
        confidence = f"Defensivo (Winrate {winrate:.0f}% -> Subiendo exigencia a Score {adaptive_score})"
    elif winrate >= 70:
        adaptive_score = max(65, BASE_BUY_SCORE - 3)
        adaptive_tp = BASE_TP_PCT + 0.4
        confidence = f"Óptimo (Winrate {winrate:.0f}% -> Alta precisión)"
    else:
        adaptive_score = BASE_BUY_SCORE
        adaptive_tp = BASE_TP_PCT
        confidence = f"Equilibrado (Winrate {winrate:.0f}%)"

    return {
        "score_threshold": adaptive_score,
        "tp_pct": adaptive_tp,
        "sl_pct": BASE_SL_PCT,
        "confidence": confidence,
        "winrate": winrate
    }

def get_pnl_summary():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                COUNT(*),
                SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN pnl_usdt <= 0 THEN 1 ELSE 0 END),
                SUM(pnl_usdt),
                AVG(pnl_pct)
            FROM trade_history
        """)
        row = cur.fetchone()
        total, wins, losses, total_pnl, avg_pct = row

    total = total or 0
    wins = wins or 0
    losses = losses or 0
    total_pnl = total_pnl or 0.0
    avg_pct = avg_pct or 0.0
    winrate = (wins / total * 100) if total > 0 else 0.0

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "total_pnl": total_pnl,
        "avg_pct": avg_pct,
        "winrate": winrate
    }

# ==================== SEGURIDAD & CONEXIÓN ====================
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

# ==================== ANÁLISIS TÉCNICO ====================
def analyze_symbol(symbol: str) -> dict:
    candles = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=100)
    if not candles or len(candles) < max(EMA_SLOW, RSI_PERIOD) + 5:
        raise ValueError("Historial insuficiente")
    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
    close = df["close"]
    df["ema_fast"] = close.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = close.ewm(span=EMA_SLOW, adjust=False).mean()
    df["rsi"] = rsi(close, RSI_PERIOD)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["vol_avg"] = df["volume"].rolling(20).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(last["close"])
    reasons = []
    score = 0

    if last["ema_fast"] > last["ema_slow"]:
        score += 25
        reasons.append("EMA 9 > EMA 21")
    if last["macd"] > last["macd_signal"]:
        score += 20
        reasons.append("MACD Cruz Alcista")

    rv = float(last["rsi"])
    if 40 <= rv <= 65:
        score += 20
        reasons.append(f"RSI en zona impulsiva ({rv:.1f})")
    elif 30 <= rv < 40:
        score += 15
        reasons.append(f"RSI saliendo de sobreventa ({rv:.1f})")
    elif rv > 70:
        score -= 10
        reasons.append(f"RSI en sobrecompra ({rv:.1f})")

    vol_ratio = float(last["volume"] / last["vol_avg"]) if pd.notna(last["vol_avg"]) and last["vol_avg"] else 0
    if vol_ratio >= 1.3:
        score += 15
        reasons.append(f"Volumen activo ({vol_ratio:.1f}x)")
    elif vol_ratio >= 1.0:
        score += 8

    if price > float(last["ema_fast"]):
        score += 10
        reasons.append("Precio > EMA rápida")
    if last["ema_fast"] > prev["ema_fast"]:
        score += 10

    signal = "ALCISTA" if score >= 65 else "NEUTRAL" if score >= 40 else "BAJISTA"
    return {
        "symbol": symbol,
        "price": price,
        "score": max(0, min(100, score)),
        "rsi": rv,
        "volume_ratio": vol_ratio,
        "signal": signal,
        "reasons": reasons,
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
        log.error("Error en orden WEEX: %s", exc)
        return {"success": False, "error": str(exc), "is_live": True}

# ==================== BUCLE DE TRADING ACTIVO Y CONTINUO ====================
async def autonomous_engine(context: ContextTypes.DEFAULT_TYPE):
    """Bucle continuo: Gestiona PnL en vivo, cierra en TP/SL y busca nuevas oportunidades."""
    global AUTOTRADE_ENABLED
    if not exchange:
        return

    settings = get_adaptive_settings()
    current_tp_pct = settings["tp_pct"]
    current_sl_pct = settings["sl_pct"]
    required_score = settings["score_threshold"]

    # 1. Vigilar posiciones abiertas para asegurar ganancias o frenar pérdidas
    open_positions = get_open_positions()
    for pos in open_positions:
        sym = pos["symbol"]
        entry_price = pos["entry_price"]
        amount_coin = pos["amount_coin"]
        tp_price = pos["tp_price"]
        sl_price = pos["sl_price"]

        try:
            ticker = exchange.fetch_ticker(sym)
            curr_price = float(ticker["last"])
        except Exception as e:
            log.warning("No se pudo obtener precio de %s: %s", sym, e)
            continue

        pnl_pct = ((curr_price - entry_price) / entry_price) * 100
        pnl_usdt = (curr_price - entry_price) * amount_coin

        # A) Take Profit Alcanzado (GANANCIA)
        if curr_price >= tp_price:
            order_res = execute_order(sym, "sell", amount_coin, f"TP Alcanzado (+{pnl_pct:.2f}%)")
            record_closed_trade(pos, curr_price, pnl_usdt, pnl_pct, "TAKE_PROFIT")

            pnl_stats = get_pnl_summary()
            sign = "🟢 +" if pnl_usdt >= 0 else "🔴 "
            msg = (
                f"🎯 *¡TAKE PROFIT ALCANZADO!*\n"
                f"Par: `{sym}`\n"
                f"Entrada: `${entry_price:g}` ➡️ Salida: `${curr_price:g}`\n"
                f"PnL Operación: *{sign}{pnl_usdt:.4f} USDT ({pnl_pct:+.2f}%)*\n"
                f"💰 *PnL ACUMULADO TOTAL:* `{pnl_stats['total_pnl']:+.4f} USDT`\n"
                f"Acierto: `{pnl_stats['winrate']:.1f}%` ({pnl_stats['wins']} Ganadas / {pnl_stats['losses']} Perdidas)\n"
                f"Modo: {'🔴 Dinero Real' if order_res.get('is_live') else '🧪 Simulación'}"
            )
            if CHAT_ID:
                try:
                    await context.bot.send_message(chat_id=int(CHAT_ID), text=msg, parse_mode="Markdown")
                except Exception:
                    pass

        # B) Stop Loss Alcanzado (PROTECCIÓN)
        elif curr_price <= sl_price:
            order_res = execute_order(sym, "sell", amount_coin, f"SL Ejecutado ({pnl_pct:.2f}%)")
            record_closed_trade(pos, curr_price, pnl_usdt, pnl_pct, "STOP_LOSS")

            pnl_stats = get_pnl_summary()
            msg = (
                f"🛑 *STOP LOSS ACTIVADO*\n"
                f"Par: `{sym}`\n"
                f"Entrada: `${entry_price:g}` ➡️ Salida: `${curr_price:g}`\n"
                f"PnL Operación: `🔴 {pnl_usdt:.4f} USDT ({pnl_pct:.2f}%)`\n"
                f"💰 *PnL ACUMULADO TOTAL:* `{pnl_stats['total_pnl']:+.4f} USDT`\n"
                f"Acierto: `{pnl_stats['winrate']:.1f}%` ({pnl_stats['wins']} Ganadas / {pnl_stats['losses']} Perdidas)\n"
                f"Modo: {'🔴 Dinero Real' if order_res.get('is_live') else '🧪 Simulación'}"
            )
            if CHAT_ID:
                try:
                    await context.bot.send_message(chat_id=int(CHAT_ID), text=msg, parse_mode="Markdown")
                except Exception:
                    pass

    # 2. Buscar y abrir nuevas operaciones de trading continuo
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

        try:
            tech = analyze_symbol(sym)
            score = tech["score"]
            price = tech["price"]
            rsi_val = tech["rsi"]

            if score >= required_score and tech["signal"] == "ALCISTA":
                amount_coin = float(exchange.amount_to_precision(sym, AMOUNT_USDT / price))
                if amount_coin <= 0:
                    continue

                tp_price = price * (1 + (current_tp_pct / 100))
                sl_price = price * (1 - (current_sl_pct / 100))

                order_res = execute_order(sym, "buy", amount_coin, f"Entrada Aprendizaje Score={score}")
                if order_res.get("success"):
                    save_new_position(sym, "buy", price, amount_coin, AMOUNT_USDT, tp_price, sl_price, score, rsi_val)
                    active_symbols.add(sym)

                    msg = (
                        f"🚀 *NUEVA ENTRADA DE TRADING*\n"
                        f"Par: `{sym}`\n"
                        f"Precio: `${price:g}` | Inversión: `{AMOUNT_USDT} USDT`\n"
                        f"🎯 TP (+{current_tp_pct:.1f}%): `${tp_price:.4f}`\n"
                        f"🛑 SL (-{current_sl_pct:.1f}%): `${sl_price:.4f}`\n"
                        f"🧠 Score Técnico: *{score}/100* (Filtro mínimo: {required_score})\n"
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
    msg = (
        "🤖 *WEEX TRADING BOT INTELIGENTE & ACTIVO*\n\n"
        "Comandos en vivo:\n"
        "• `/pnl` - Ganancias netas totales acumuladas en USDT\n"
        "• `/posiciones` - Ver operaciones en curso y su ganancia en vivo\n"
        "• `/aprendizaje` - Estado del motor de optimización\n"
        "• `/autotrade on|off` - Activar o pausar trading automático\n"
        "• `/cerrar BTC/USDT` - Cerrar una posición inmediatamente\n"
        "• `/balance` - Saldo disponible en WEEX\n"
        "• `/estado` - Estado general del sistema\n"
        "• `/ranking` - Mejores activos del momento\n"
        "• `/precio BTC/USDT` - Precio actual\n"
    )
    await update.effective_message.reply_text(msg, parse_mode="Markdown")

async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    stats = get_pnl_summary()
    open_pos = get_open_positions()

    floating_pnl = 0.0
    for p in open_pos:
        try:
            curr = float(exchange.fetch_ticker(p["symbol"])["last"])
            floating_pnl += (curr - p["entry_price"]) * p["amount_coin"]
        except Exception:
            pass

    color = "🟢 +" if stats["total_pnl"] >= 0 else "🔴 "
    msg = (
        "💰 *REPORTE DE PnL & RENDIMIENTO*\n\n"
        f"• PnL Realizado Total: *{color}{stats['total_pnl']:.4f} USDT*\n"
        f"• PnL Flotante en Curso: *{floating_pnl:+.4f} USDT*\n"
        f"• Operaciones Ganadoras: *{stats['wins']}* 🎯\n"
        f"• Operaciones Perdedoras: *{stats['losses']}* 🛑\n"
        f"• Tasa de Acierto (Winrate): *{stats['winrate']:.1f}%*\n"
        f"• Total Operaciones Cerradas: *{stats['total']}*\n"
        f"• Operaciones Abiertas Ahora: *{len(open_pos)}*"
    )
    await update.effective_message.reply_text(msg, parse_mode="Markdown")

async def cmd_aprendizaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    settings = get_adaptive_settings()
    stats = get_pnl_summary()

    msg = (
        "🧠 *ESTADO DEL MOTOR DE APRENDIZAJE*\n\n"
        f"• Estado del Cerebro: *{settings['confidence']}*\n"
        f"• Filtro Score Mínimo: *≥ {settings['score_threshold']}/100*\n"
        f"• Take Profit Dinámico: *+{settings['tp_pct']:.1f}%*\n"
        f"• Stop Loss de Protección: *-{settings['sl_pct']:.1f}%*\n"
        f"• Base de Experiencia: `{stats['total']} operaciones procesadas`\n"
        f"• Tasa de Acierto: `{settings['winrate']:.1f}%`\n\n"
        "_El bot calibra la exigencia de entrada según el PnL y acierto reciente._"
    )
    await update.effective_message.reply_text(msg, parse_mode="Markdown")

async def cmd_posiciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    positions = get_open_positions()
    if not positions:
        await update.effective_message.reply_text("ℹ️ No hay posiciones abiertas actualmente. El bot sigue escaneando el mercado.")
        return

    lines = ["📊 *OPERACIONES EN CURSO (PnL EN VIVO):*\n"]
    for p in positions:
        sym = p["symbol"]
        entry = p["entry_price"]
        amount_coin = p["amount_coin"]
        tp = p["tp_price"]
        sl = p["sl_price"]
        try:
            curr = float(exchange.fetch_ticker(sym)["last"])
            pnl_pct = ((curr - entry) / entry) * 100
            pnl_usdt = (curr - entry) * amount_coin
            badge = "🟢" if pnl_pct >= 0 else "🔴"
            lines.append(
                f"{badge} *{sym}*\n"
                f"• Entrada: `${entry:g}` ➡️ Actual: `${curr:g}`\n"
                f"• PnL Flotante: *{pnl_pct:+.2f}%* ({pnl_usdt:+.4f} USDT)\n"
                f"• TP: `${tp:.4f}` | SL: `${sl:.4f}`\n"
            )
        except Exception:
            lines.append(f"• *{sym}*: Entrada `${entry:g}`")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_autotrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    global AUTOTRADE_ENABLED
    arg = context.args[0].lower() if context.args else ""
    if arg == "on":
        AUTOTRADE_ENABLED = True
        await update.effective_message.reply_text("✅ *Trading Autónomo ACTIVADO*.\nBuscando oportunidades en tiempo real.", parse_mode="Markdown")
    elif arg == "off":
        AUTOTRADE_ENABLED = False
        await update.effective_message.reply_text("⏸️ *Trading Autónomo PAUSADO*.\nNo abrirá nuevas posiciones.", parse_mode="Markdown")
    else:
        status = "ACTIVADO ✅" if AUTOTRADE_ENABLED else "PAUSADO ⏸️"
        await update.effective_message.reply_text(f"Estado del Auto-Trading: *{status}*\nUsa `/autotrade on` o `/autotrade off`", parse_mode="Markdown")

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

    try:
        curr = float(exchange.fetch_ticker(sym)["last"])
        pnl_pct = ((curr - entry_price) / entry_price) * 100
        pnl_usdt = (curr - entry_price) * amount_coin
        order_res = execute_order(sym, "sell", amount_coin, "Cierre manual por usuario")
        record_closed_trade(pos, curr, pnl_usdt, pnl_pct, "CIERRE_MANUAL")

        await update.effective_message.reply_text(
            f"✅ Posición {sym} cerrada a mercado.\n"
            f"Salida: `${curr:g}` | PnL: *{pnl_pct:+.2f}%* ({pnl_usdt:+.4f} USDT)\n"
            f"Modo: {'🔴 Real' if order_res.get('is_live') else '🧪 Simulación'}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.effective_message.reply_text(f"Error al cerrar posición {sym}: {e}")

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
    settings = get_adaptive_settings()
    await update.effective_message.reply_text(
        f"⚙️ *ESTADO DEL BOT WEEX*\n\n"
        f"• Auto-Trading: *{'ACTIVADO ✅' if AUTOTRADE_ENABLED else 'PAUSADO ⏸️'}*\n"
        f"• Modo Órdenes: *{'🔴 DINERO REAL' if is_live else '🧪 SIMULACIÓN (DRY_RUN)'}*\n"
        f"• Posiciones abiertas: *{open_count}/{MAX_OPEN_POSITIONS}*\n"
        f"• Monto por trade: *{AMOUNT_USDT} USDT*\n"
        f"• Filtro adaptado actual: *Score ≥ {settings['score_threshold']}/100*\n"
        f"• Take Profit: *+{settings['tp_pct']:.1f}%* | Stop Loss: *-{settings['sl_pct']:.1f}%*\n"
        f"• Watchlist: `{', '.join(WATCHLIST)}`\n"
        f"• Intervalo de escaneo: `{INTERVAL} segundos`",
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

async def cmd_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text("⏳ Escaneando mercados USDT; analizando indicadores...")
    results = []
    for sym in WATCHLIST:
        try:
            results.append(analyze_symbol(sym))
        except Exception:
            continue
    results.sort(key=lambda x: x["score"], reverse=True)
    if not results:
        await update.effective_message.reply_text("No se pudieron analizar mercados.")
        return
    msg = "🏆 *RANKING TÉCNICO WEEX*\n\n" + "\n\n".join(
        f"{i}. *{x['symbol']}*: ${x['price']:g} | Score: *{x['score']}* ({x['signal']})\nRSI: {x['rsi']:.1f} | Vol: {x['volume_ratio']:.2f}x"
        for i, x in enumerate(results, 1)
    )
    await update.effective_message.reply_text(msg, parse_mode="Markdown")

# ==================== INICIO PRINCIPAL ====================
def main():
    global exchange
    if not TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en las variables de entorno.")

    init_db()
    exchange = get_exchange()
    try:
        exchange.load_markets()
        log.info("WEEX conectado. %s mercados disponibles.", len(exchange.markets))
    except Exception as e:
        log.error("Aviso al cargar mercados: %s", e)

    app = ApplicationBuilder().token(TOKEN).build()

    for name, callback in [
        ("start", cmd_start),
        ("help", cmd_start),
        ("pnl", cmd_pnl),
        ("aprendizaje", cmd_aprendizaje),
        ("posiciones", cmd_posiciones),
        ("autotrade", cmd_autotrade),
        ("cerrar", cmd_cerrar),
        ("estado", cmd_estado),
        ("balance", cmd_balance),
        ("precio", cmd_precio),
        ("ranking", cmd_ranking),
    ]:
        app.add_handler(CommandHandler(name, callback))

    # Tarea de Trading Continuo cada 45 segundos
    app.job_queue.run_repeating(autonomous_engine, interval=INTERVAL, first=10)
    log.info("Motor de trading autónomo y continuo activo cada %s segundos.", INTERVAL)

    start_keep_alive()
    app.run_polling()

if __name__ == "__main__":
    main()
