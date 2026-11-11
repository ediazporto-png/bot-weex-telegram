"""
Asistente WEEX + Telegram con Trading Autónomo Inteligente.
- Analiza indicadores técnicos (EMA, RSI, MACD, Volumen).
- Abre posiciones automáticamente cuando detecta score alcista >= AUTO_BUY_SCORE.
- Monitorea y gestiona Take Profit y Stop Loss automáticamente.
- Permite control manual y monitoreo desde Telegram (/autotrade on/off, /posiciones, /cerrar).
- Mantiene activo el servicio en Render con keep_alive (/health).
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

# Modos de ejecución
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
ALLOW_LIVE = os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"
MARKET_TYPE = os.getenv("MARKET_TYPE", "spot")
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
INTERVAL = max(30, int(os.getenv("CHECK_INTERVAL_SEC", "60")))  # Revisión cada 60s
AMOUNT_USDT = float(os.getenv("TRADE_AMOUNT_USDT", "10"))

# Parámetros del Auto-Trading Autónomo
AUTOTRADE_ENABLED = os.getenv("AUTOTRADE_ENABLED", "true").lower() == "true"
AUTO_BUY_SCORE = int(os.getenv("AUTO_BUY_SCORE", "70"))        # Puntuación mínima para comprar (0 a 100)
AUTO_TP_PCT = float(os.getenv("AUTO_TP_PCT", "2.0"))           # Take Profit: +2.0%
AUTO_SL_PCT = float(os.getenv("AUTO_SL_PCT", "1.5"))           # Stop Loss: -1.5%
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))  # Máximo de trades simultáneos

# Indicadores técnicos
EMA_FAST = int(os.getenv("EMA_FAST", "9"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "21"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))

WATCHLIST = [x.strip().upper() for x in os.getenv(
    "SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT,DOGE/USDT").split(",") if x.strip()]

DB_PATH = Path("weex_positions.db")

exchange = None

# ==================== BASE DE DATOS DE POSICIONES ====================
def init_positions_db():
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
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closed_at TEXT,
                exit_price REAL,
                pnl_usdt REAL,
                close_reason TEXT
            )
        """)
        conn.commit()

def get_open_positions():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM live_positions WHERE status = 'OPEN'")
        return [dict(row) for row in cur.fetchall()]

def save_new_position(symbol, side, entry_price, amount_coin, amount_usdt, tp_price, sl_price):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO live_positions 
            (symbol, side, entry_price, amount_coin, amount_usdt, tp_price, sl_price, opened_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
        """, (symbol, side, entry_price, amount_coin, amount_usdt, tp_price, sl_price, datetime.now(timezone.utc).isoformat()))
        conn.commit()

def close_position_in_db(symbol, exit_price, pnl_usdt, reason):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE live_positions 
            SET status = 'CLOSED', closed_at = ?, exit_price = ?, pnl_usdt = ?, close_reason = ?
            WHERE symbol = ? AND status = 'OPEN'
        """, (datetime.now(timezone.utc).isoformat(), exit_price, pnl_usdt, reason, symbol))
        conn.commit()

# ==================== SEGURIDAD & CONEXIÓN WEEX ====================
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
    candles = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=120)
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

    # 1. Medias Móviles EMA (25 pts)
    if last["ema_fast"] > last["ema_slow"]:
        score += 25
        reasons.append("EMA rápida > EMA lenta")
    else:
        reasons.append("EMA rápida < EMA lenta")

    # 2. MACD (20 pts)
    if last["macd"] > last["macd_signal"]:
        score += 20
        reasons.append("MACD alcista")
    else:
        reasons.append("MACD bajista")

    # 3. RSI (20 pts)
    rv = float(last["rsi"])
    if 45 <= rv <= 65:
        score += 20
        reasons.append(f"RSI óptimo ({rv:.1f})")
    elif 30 <= rv < 45:
        score += 15
        reasons.append(f"RSI en recuperación ({rv:.1f})")
    elif rv > 70:
        score -= 10
        reasons.append(f"RSI sobrecomprado ({rv:.1f})")
    else:
        reasons.append(f"RSI débil ({rv:.1f})")

    # 4. Volumen (15 pts)
    vol_ratio = float(last["volume"] / last["vol_avg"]) if pd.notna(last["vol_avg"]) and last["vol_avg"] else 0
    if vol_ratio >= 1.5:
        score += 15
        reasons.append(f"Volumen alto ({vol_ratio:.1f}x)")
    elif vol_ratio >= 1.0:
        score += 8
        reasons.append("Volumen sobre media")

    # 5. Acción del precio (20 pts)
    if price > float(last["ema_fast"]):
        score += 10
        reasons.append("Precio > EMA rápida")
    if last["ema_fast"] > prev["ema_fast"]:
        score += 10
        reasons.append("Pendiente EMA alcista")

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

def fmt_item(x: dict) -> str:
    return (
        f"*{x['symbol']}*: `${x['price']:g}` | Score: *{x['score']}/100* ({x['signal']})\n"
        f"RSI: {x['rsi']:.1f} | Vol: {x['volume_ratio']:.2f}x\n"
        f"Detalles: {', '.join(x['reasons'][:3])}"
    )

# ==================== EJECUCIÓN DE ÓRDENES ====================
def execute_order(symbol: str, side: str, amount_coin: float, reason_text: str = "") -> dict:
    """Envía la orden a WEEX si LIVE está habilitado, o simula si DRY_RUN=true."""
    is_live = (not DRY_RUN) and ALLOW_LIVE
    mode_tag = "🔴 [REAL]" if is_live else "🧪 [SIMULACIÓN]"
    log.info("%s Ejecutando orden %s %s por %s unidades. Motivo: %s", mode_tag, side.upper(), symbol, amount_coin, reason_text)

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
        log.error("Fallo al ejecutar orden en WEEX: %s", exc)
        return {"success": False, "error": str(exc), "is_live": True}

# ==================== BUCLE DE TRADING AUTÓNOMO ====================
async def autonomous_trading_job(context: ContextTypes.DEFAULT_TYPE):
    """Bucle periódico: revisa TP/SL de posiciones abiertas y busca nuevas entradas."""
    global AUTOTRADE_ENABLED
    if not exchange:
        return

    # --- 1. REVISAR POSICIONES ABIERTAS (SALIDAS: TP y SL) ---
    open_positions = get_open_positions()
    for pos in open_positions:
        sym = pos["symbol"]
        entry_price = pos["entry_price"]
        amount_coin = pos["amount_coin"]
        tp_price = pos["tp_price"]
        sl_price = pos["sl_price"]

        try:
            ticker = exchange.fetch_ticker(sym)
            current_price = float(ticker["last"])
        except Exception as e:
            log.warning("No se pudo obtener precio para %s: %s", sym, e)
            continue

        pnl_pct = ((current_price - entry_price) / entry_price) * 100
        pnl_usdt = (current_price - entry_price) * amount_coin

        # A) Take Profit Alcanzado
        if current_price >= tp_price:
            order_res = execute_order(sym, "sell", amount_coin, f"TP alcanzado (+{pnl_pct:.2f}%)")
            close_position_in_db(sym, current_price, pnl_usdt, "TAKE_PROFIT")
            
            msg = (
                f"🎯 *¡TAKE PROFIT ALCANZADO!*\n"
                f"Par: `{sym}`\n"
                f"Entrada: `${entry_price:g}` | Salida: `${current_price:g}`\n"
                f"Resultado: *+{pnl_pct:.2f}%* (+${pnl_usdt:.2f} USDT)\n"
                f"Modo: {'🔴 Dinero Real' if order_res.get('is_live') else '🧪 Simulación'}"
            )
            if CHAT_ID:
                try:
                    await context.bot.send_message(chat_id=int(CHAT_ID), text=msg, parse_mode="Markdown")
                except Exception:
                    pass

        # B) Stop Loss Alcanzado
        elif current_price <= sl_price:
            order_res = execute_order(sym, "sell", amount_coin, f"SL activado ({pnl_pct:.2f}%)")
            close_position_in_db(sym, current_price, pnl_usdt, "STOP_LOSS")
            
            msg = (
                f"🛑 *STOP LOSS ACTIVADO*\n"
                f"Par: `{sym}`\n"
                f"Entrada: `${entry_price:g}` | Salida: `${current_price:g}`\n"
                f"Resultado: *{pnl_pct:.2f}%* (${pnl_usdt:.2f} USDT)\n"
                f"Modo: {'🔴 Dinero Real' if order_res.get('is_live') else '🧪 Simulación'}"
            )
            if CHAT_ID:
                try:
                    await context.bot.send_message(chat_id=int(CHAT_ID), text=msg, parse_mode="Markdown")
                except Exception:
                    pass

    # --- 2. BUSCAR NUEVAS OPORTUNIDADES (ENTRADAS AUTÓNOMAS) ---
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

            # Condición de compra autónoma: Score >= AUTO_BUY_SCORE y señal ALCISTA
            if score >= AUTO_BUY_SCORE and tech["signal"] == "ALCISTA":
                amount_coin = float(exchange.amount_to_precision(sym, AMOUNT_USDT / price))
                if amount_coin <= 0:
                    continue

                tp_price = price * (1 + (AUTO_TP_PCT / 100))
                sl_price = price * (1 - (AUTO_SL_PCT / 100))

                order_res = execute_order(sym, "buy", amount_coin, f"Señal autónoma Score={score}")
                if order_res.get("success"):
                    save_new_position(sym, "buy", price, amount_coin, AMOUNT_USDT, tp_price, sl_price)
                    active_symbols.add(sym)

                    msg = (
                        f"🚀 *NUEVA COMPRA AUTÓNOMA*\n"
                        f"Par: `{sym}`\n"
                        f"Precio: `${price:g}` | Inversión: `{AMOUNT_USDT} USDT`\n"
                        f"🎯 Take Profit (+{AUTO_TP_PCT}%): `${tp_price:.4f}`\n"
                        f"🛑 Stop Loss (-{AUTO_SL_PCT}%): `${sl_price:.4f}`\n"
                        f"Score Técnico: *{score}/100*\n"
                        f"Modo: {'🔴 Dinero Real' if order_res.get('is_live') else '🧪 Simulación'}"
                    )
                    if CHAT_ID:
                        try:
                            await context.bot.send_message(chat_id=int(CHAT_ID), text=msg, parse_mode="Markdown")
                        except Exception:
                            pass
        except Exception as e:
            log.warning("Error analizando %s para trading autónomo: %s", sym, e)

# ==================== COMANDOS TELEGRAM ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    msg = (
        "🤖 *WEEX TRADING BOT AUTÓNOMO*\n\n"
        "Comandos principales:\n"
        "• `/autotrade on` o `off` - Activar o pausar trading autónomo\n"
        "• `/posiciones` - Ver operaciones abiertas y ganancia en vivo\n"
        "• `/cerrar BTC/USDT` - Cerrar una posición inmediatamente\n"
        "• `/balance` - Consultar saldo en cuenta WEEX\n"
        "• `/estado` - Ver configuración actual del bot\n"
        "• `/precio BTC/USDT` - Precio en tiempo real\n"
        "• `/analizar BTC/USDT` - Análisis técnico con indicadores\n"
        "• `/ranking` - Ranking de oportunidades en watchlist\n"
        "• `/comprar BTC/USDT 10` - Orden manual de compra\n"
        "• `/vender BTC/USDT 10` - Orden manual de venta\n"
    )
    await update.effective_message.reply_text(msg, parse_mode="Markdown")

async def cmd_autotrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    global AUTOTRADE_ENABLED
    arg = context.args[0].lower() if context.args else ""
    if arg == "on":
        AUTOTRADE_ENABLED = True
        await update.effective_message.reply_text("✅ *Trading Autónomo ACTIVADO*.\nEl bot buscará oportunidades y gestionará TP/SL automáticamente.", parse_mode="Markdown")
    elif arg == "off":
        AUTOTRADE_ENABLED = False
        await update.effective_message.reply_text("⏸️ *Trading Autónomo PAUSADO*.\nNo abrirá nuevas compras (seguirá vigilando TP/SL de las abiertas).", parse_mode="Markdown")
    else:
        status = "ACTIVADO ✅" if AUTOTRADE_ENABLED else "PAUSADO ⏸️"
        await update.effective_message.reply_text(f"Estado del Auto-Trading: *{status}*\nUsa `/autotrade on` o `/autotrade off`", parse_mode="Markdown")

async def cmd_posiciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    positions = get_open_positions()
    if not positions:
        await update.effective_message.reply_text("ℹ️ No hay posiciones abiertas actualmente.")
        return

    lines = ["📊 *POSICIONES ABIERTAS:*\n"]
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
                f"• Entrada: `${entry:g}` | Actual: `${curr:g}`\n"
                f"• PnL: *{pnl_pct:+.2f}%* ({pnl_usdt:+.2f} USDT)\n"
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

    try:
        curr = float(exchange.fetch_ticker(sym)["last"])
        pnl_pct = ((curr - entry_price) / entry_price) * 100
        pnl_usdt = (curr - entry_price) * amount_coin
        order_res = execute_order(sym, "sell", amount_coin, "Cierre manual por usuario")
        close_position_in_db(sym, curr, pnl_usdt, "CIERRE_MANUAL")

        await update.effective_message.reply_text(
            f"✅ Posición {sym} cerrada a mercado.\n"
            f"Salida: `${curr:g}` | PnL: *{pnl_pct:+.2f}%* ({pnl_usdt:+.2f} USDT)\n"
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
    await update.effective_message.reply_text(
        f"⚙️ *ESTADO DEL BOT WEEX*\n\n"
        f"• Auto-Trading: *{'ACTIVADO ✅' if AUTOTRADE_ENABLED else 'PAUSADO ⏸️'}*\n"
        f"• Modo Órdenes: *{'🔴 DINERO REAL' if is_live else '🧪 SIMULACIÓN (DRY_RUN)'}*\n"
        f"• Posiciones abiertas: *{open_count}/{MAX_OPEN_POSITIONS}*\n"
        f"• Monto por trade: *{AMOUNT_USDT} USDT*\n"
        f"• Take Profit: *+{AUTO_TP_PCT}%* | Stop Loss: *-{AUTO_SL_PCT}%*\n"
        f"• Score de entrada: *≥ {AUTO_BUY_SCORE}/100*\n"
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

async def cmd_analizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    sym = (context.args[0] if context.args else "BTC/USDT").upper()
    try:
        x = analyze_symbol(sym)
        await update.effective_message.reply_text(
            "🔎 *ANÁLISIS TÉCNICO*\n" + fmt_item(x),
            parse_mode="Markdown"
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Error analizando {sym}: {exc}")

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
        f"{i}. {fmt_item(x)}" for i, x in enumerate(results, 1)
    )
    await update.effective_message.reply_text(msg, parse_mode="Markdown")

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.effective_message.reply_text("Uso: `/comprar BTC/USDT 10`", parse_mode="Markdown")
        return
    try:
        sym = context.args[0].upper()
        amt_usdt = float(context.args[1]) if len(context.args) > 1 else AMOUNT_USDT
        ticker = exchange.fetch_ticker(sym)
        price = float(ticker["last"])
        amount_coin = float(exchange.amount_to_precision(sym, amt_usdt / price))
        tp_price = price * (1 + (AUTO_TP_PCT / 100))
        sl_price = price * (1 - (AUTO_SL_PCT / 100))

        order_res = execute_order(sym, "buy", amount_coin, "Compra manual")
        if order_res.get("success"):
            save_new_position(sym, "buy", price, amount_coin, amt_usdt, tp_price, sl_price)
            await update.effective_message.reply_text(
                f"✅ *Orden de Compra Ejecutada*\n"
                f"Par: `{sym}` | Cantidad: `{amount_coin}`\n"
                f"Precio: `${price:g}` | Inversión: `{amt_usdt} USDT`\n"
                f"TP: `${tp_price:.4f}` | SL: `${sl_price:.4f}`\n"
                f"Modo: {'🔴 Dinero Real' if order_res.get('is_live') else '🧪 Simulación'}",
                parse_mode="Markdown"
            )
        else:
            await update.effective_message.reply_text(f"❌ Error al enviar orden: {order_res.get('error')}")
    except Exception as exc:
        await update.effective_message.reply_text(f"Error de compra: {exc}")

async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.effective_message.reply_text("Uso: `/vender BTC/USDT 10`", parse_mode="Markdown")
        return
    try:
        sym = context.args[0].upper()
        amt_usdt = float(context.args[1]) if len(context.args) > 1 else AMOUNT_USDT
        ticker = exchange.fetch_ticker(sym)
        price = float(ticker["last"])
        amount_coin = float(exchange.amount_to_precision(sym, amt_usdt / price))
        order_res = execute_order(sym, "sell", amount_coin, "Venta manual")
        if order_res.get("success"):
            close_position_in_db(sym, price, 0.0, "VENTA_MANUAL")
            await update.effective_message.reply_text(
                f"✅ *Orden de Venta Ejecutada*\n"
                f"Par: `{sym}` | Cantidad: `{amount_coin}` (${price:g})\n"
                f"Modo: {'🔴 Dinero Real' if order_res.get('is_live') else '🧪 Simulación'}",
                parse_mode="Markdown"
            )
        else:
            await update.effective_message.reply_text(f"❌ Error al enviar orden: {order_res.get('error')}")
    except Exception as exc:
        await update.effective_message.reply_text(f"Error de venta: {exc}")

# ==================== INICIO PRINCIPAL ====================
def main():
    global exchange
    if not TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en las variables de entorno.")

    init_positions_db()
    exchange = get_exchange()
    try:
        exchange.load_markets()
        log.info("WEEX conectado exitosamente. %s mercados cargados.", len(exchange.markets))
    except Exception as e:
        log.error("Aviso al cargar mercados de WEEX: %s", e)

    app = ApplicationBuilder().token(TOKEN).build()

    for name, callback in [
        ("start", cmd_start),
        ("help", cmd_start),
        ("autotrade", cmd_autotrade),
        ("posiciones", cmd_posiciones),
        ("cerrar", cmd_cerrar),
        ("estado", cmd_estado),
        ("balance", cmd_balance),
        ("precio", cmd_precio),
        ("analizar", cmd_analizar),
        ("ranking", cmd_ranking),
        ("comprar", cmd_buy),
        ("vender", cmd_sell),
    ]:
        app.add_handler(CommandHandler(name, callback))

    # Tarea de Trading Autónomo en Segundo Plano
    app.job_queue.run_repeating(autonomous_trading_job, interval=INTERVAL, first=15)
    log.info("Bucle de trading autónomo configurado cada %s segundos.", INTERVAL)

    # Iniciar servidor /health y auto-ping para Render
    start_keep_alive()

    log.info("Iniciando polling del bot de Telegram...")
    app.run_polling()

if __name__ == "__main__":
    main()
