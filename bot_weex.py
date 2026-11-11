
"""
Asistente WEEX + Telegram: monitor de mercado y ranking técnico.
Por seguridad, el análisis/monitorización NO envía órdenes automáticamente.
Las órdenes manuales requieren DRY_RUN=false Y ALLOW_LIVE_TRADING=true.
"""
import os
import logging
from datetime import datetime, timezone

import ccxt
import pandas as pd
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from learning_center import LearningCenter
import asyncio

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("weex-assistant")

API_KEY = os.getenv("WEEX_API_KEY", "")
API_SECRET = os.getenv("WEEX_API_SECRET", "")
API_PASSPHRASE = os.getenv("WEEX_API_PASSPHRASE", "")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
ALLOW_LIVE = os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"
MARKET_TYPE = os.getenv("MARKET_TYPE", "spot")
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
INTERVAL = max(60, int(os.getenv("CHECK_INTERVAL_SEC", "300")))
AMOUNT_USDT = float(os.getenv("TRADE_AMOUNT_USDT", "20"))
EMA_FAST = int(os.getenv("EMA_FAST", "9"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "21"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
UNIVERSE_LIMIT = int(os.getenv("UNIVERSE_LIMIT", "40"))
RANKING_LIMIT = int(os.getenv("RANKING_LIMIT", "10"))
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME_USDT", "1000000"))
WATCHLIST = [x.strip().upper() for x in os.getenv(
    "SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT"
).split(",") if x.strip()]

exchange = None
monitor_enabled = False
last_scan_text = "Todavía no se ha realizado un escaneo."
last_scan_at = None
learning = None


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


def analyze_symbol(symbol: str) -> dict:
    candles = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=150)
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
        reasons.append("EMA rápida sobre EMA lenta")
    else:
        reasons.append("EMA rápida bajo EMA lenta")
    if last["macd"] > last["macd_signal"]:
        score += 20
        reasons.append("MACD alcista")
    else:
        reasons.append("MACD bajista")
    rv = float(last["rsi"])
    if 50 <= rv <= 68:
        score += 20
        reasons.append("RSI en zona de impulso saludable")
    elif 40 <= rv < 50:
        score += 10
        reasons.append("RSI neutral-débil")
    elif rv > 75:
        score -= 10
        reasons.append("RSI alto: posible sobrecompra")
    else:
        reasons.append("RSI débil")
    vol_ratio = float(last["volume"] / last["vol_avg"]) if pd.notna(last["vol_avg"]) and last["vol_avg"] else 0
    if vol_ratio >= 1.5:
        score += 15
        reasons.append("Volumen ≥ 1.5 veces su media")
    elif vol_ratio >= 1:
        score += 8
        reasons.append("Volumen por encima de su media")
    if price > float(last["ema_fast"]):
        score += 10
        reasons.append("Precio sobre EMA rápida")
    if last["ema_fast"] > prev["ema_fast"]:
        score += 10
        reasons.append("EMA rápida ascendiendo")

    signal = "ALCISTA" if score >= 65 else "NEUTRAL" if score >= 40 else "BAJISTA/DÉBIL"
    return {
        "symbol": symbol, "price": price, "score": max(0, min(100, score)),
        "rsi": rv, "volume_ratio": vol_ratio, "signal": signal,
        "reasons": reasons,
    }


def get_usdt_universe():
    tickers = exchange.fetch_tickers()
    candidates = []
    for symbol, t in tickers.items():
        if not symbol.endswith("/USDT") or ":USDT" in symbol:
            continue
        quote_vol = t.get("quoteVolume")
        if quote_vol is None:
            base_vol, last = t.get("baseVolume"), t.get("last")
            quote_vol = (base_vol or 0) * (last or 0)
        try:
            qv = float(quote_vol or 0)
        except (TypeError, ValueError):
            continue
        if qv >= MIN_QUOTE_VOLUME and t.get("last"):
            candidates.append((symbol, qv))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:UNIVERSE_LIMIT]


def build_ranking(limit=None):
    results, errors = [], []
    for symbol, quote_vol in get_usdt_universe():
        try:
            item = analyze_symbol(symbol)
            item["quote_volume"] = quote_vol
            results.append(item)
        except Exception as exc:
            errors.append(f"{symbol}: {exc}")
    results.sort(key=lambda x: (x["score"], x["quote_volume"]), reverse=True)
    return results[:(limit or RANKING_LIMIT)], len(errors)


def fmt_item(x):
    return (f"{x['symbol']} | {x['score']}/100 | {x['signal']}\n"
            f"Precio: {x['price']:.8g} | RSI: {x['rsi']:.1f} | "
            f"Volumen/media: {x['volume_ratio']:.2f}x")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text(
        "🤖 WEEX Market Assistant\n\n"
        "/precio BTC/USDT - precio actual\n"
        "/analizar BTC/USDT - análisis técnico\n"
        "/ranking - mejores puntuaciones técnicas del universo USDT\n"
        "/mercado - resumen de monedas vigiladas\n"
        "/monitor on|off - alertas periódicas de mercado\n"
        "/ultimo - último informe generado\n"
        "/aprendizaje estado|informe|on|off - centro de aprendizaje\n"
        "/estado - configuración y modo\n"
        "/balance - saldo (requiere API de lectura)\n"
        "/comprar BTC/USDT 20 y /vender BTC/USDT 20 - órdenes manuales protegidas\n\n"
        "El ranking es un filtro técnico, no una promesa de rentabilidad."
    )


async def cmd_precio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    sym = (context.args[0] if context.args else "BTC/USDT").upper()
    try:
        t = exchange.fetch_ticker(sym)
        await update.effective_message.reply_text(
            f"💰 {sym}: {t.get('last')}\n24h: {t.get('percentage', '?')}% | "
            f"Máx: {t.get('high')} | Mín: {t.get('low')}"
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"No pude obtener {sym}: {exc}")


async def cmd_analizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    sym = (context.args[0] if context.args else "BTC/USDT").upper()
    try:
        x = analyze_symbol(sym)
        await update.effective_message.reply_text(
            "🔎 ANÁLISIS TÉCNICO\n" + fmt_item(x) + "\n\n• " +
            "\n• ".join(x["reasons"]) +
            "\n\nEvaluación educativa; confirmar tendencia, liquidez y riesgo antes de operar."
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Error analizando {sym}: {exc}")


async def cmd_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text("⏳ Escaneando mercados USDT; puede tardar un poco…")
    global last_scan_text, last_scan_at
    try:
        limit = int(context.args[0]) if context.args else RANKING_LIMIT
        limit = max(1, min(limit, 20))
        results, errors = build_ranking(limit)
        if not results:
            msg = "No se encontraron mercados con datos suficientes."
        else:
            msg = "🏆 RANKING TÉCNICO WEEX\n\n" + "\n\n".join(
                f"{i}. {fmt_item(x)}" for i, x in enumerate(results, 1)
            )
            msg += "\n\n⚠️ Ranking cuantitativo experimental; no predice ganancias."
        msg += f"\n\nMercados omitidos por errores/datos: {errors}"
        last_scan_text, last_scan_at = msg, datetime.now(timezone.utc)
        await update.effective_message.reply_text(msg)
    except Exception as exc:
        await update.effective_message.reply_text(f"Falló el escaneo: {exc}")


async def cmd_mercado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    lines = ["🌐 RESUMEN DE MERCADO"]
    for sym in WATCHLIST[:12]:
        try:
            t = exchange.fetch_ticker(sym)
            pct = t.get("percentage")
            lines.append(f"{sym}: {t.get('last')} | 24h: {pct if pct is not None else '?'}% | "
                         f"Vol. cotizada: {t.get('quoteVolume')}")
        except Exception as exc:
            lines.append(f"{sym}: error ({exc})")
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    global monitor_enabled
    arg = context.args[0].lower() if context.args else ""
    if arg == "on":
        monitor_enabled = True
        await update.effective_message.reply_text(
            f"✅ Monitor activado. Resumen cada {INTERVAL}s. Solo alertas/análisis; no opera."
        )
    elif arg == "off":
        monitor_enabled = False
        await update.effective_message.reply_text("⏸️ Monitor detenido.")
    else:
        await update.effective_message.reply_text("Uso: /monitor on o /monitor off")


async def market_job(context: ContextTypes.DEFAULT_TYPE):
    if not monitor_enabled or not CHAT_ID:
        return
    try:
        lines = ["📡 WEEX: resumen de vigilancia"]
        for sym in WATCHLIST[:8]:
            try:
                x = analyze_symbol(sym)
                if x["score"] >= 65 or x["score"] < 30:
                    lines.append(fmt_item(x))
            except Exception as exc:
                log.warning("Monitor %s: %s", sym, exc)
        if len(lines) > 1:
            await context.bot.send_message(chat_id=int(CHAT_ID), text="\n\n".join(lines))
    except Exception:
        log.exception("Error en monitor programado")


async def cmd_aprendizaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    global learning
    arg = context.args[0].lower() if context.args else "estado"
    if learning is None:
        await update.effective_message.reply_text("El centro de aprendizaje no está inicializado.")
        return
    if arg == "on":
        learning.enabled = True
        await update.effective_message.reply_text("🧠 Aprendizaje activado. Solo análisis y operaciones simuladas.")
    elif arg == "off":
        learning.enabled = False
        await update.effective_message.reply_text("⏸️ Aprendizaje pausado. El historial se conserva.")
    elif arg == "informe":
        report = await asyncio.to_thread(learning.report)
        await update.effective_message.reply_text(report)
    else:
        status = "ACTIVO" if learning.enabled else "PAUSADO"
        hours = f"{os.getenv('LEARNING_START_HOUR','8')}:00–{os.getenv('LEARNING_END_HOUR','20')}:00"
        await update.effective_message.reply_text(f"🧠 Centro: {status}\nHorario: {hours}\nPares: {', '.join(learning.symbols)}\n{learning.report()}")


async def learning_job(context: ContextTypes.DEFAULT_TYPE):
    if learning is None or not learning.enabled or not learning.active_now():
        return
    try:
        result = await asyncio.to_thread(learning.scan)
        log.info("Centro aprendizaje: %s", result)
    except Exception:
        log.exception("Error en el centro de aprendizaje")


async def cmd_ultimo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    suffix = f"\nGenerado: {last_scan_at.isoformat()}" if last_scan_at else ""
    await update.effective_message.reply_text(last_scan_text + suffix)


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text(
        f"⚙️ Estado\nMercado: {MARKET_TYPE}\nTimeframe: {TIMEFRAME}\n"
        f"Watchlist: {', '.join(WATCHLIST)}\nIntervalo: {INTERVAL}s\n"
        f"Monitor: {'ON' if monitor_enabled else 'OFF'}\n"
        f"DRY_RUN: {DRY_RUN}\nÓrdenes reales autorizadas: {ALLOW_LIVE}\n"
        f"Aprendizaje: {'ACTIVO' if learning and learning.enabled else 'PAUSADO'}"
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    try:
        bal = exchange.fetch_balance()
        rows = [f"{c}: {v}" for c, v in bal.get("total", {}).items() if v and v > 0.00001]
        await update.effective_message.reply_text("💼 Balance:\n" + ("\n".join(rows) if rows else "Sin fondos visibles."))
    except Exception as exc:
        await update.effective_message.reply_text(f"Error consultando balance: {exc}")


def manual_order(symbol: str, side: str, amount_usdt: float) -> str:
    if amount_usdt <= 0:
        return "El monto debe ser mayor que cero."
    if DRY_RUN:
        return f"🧪 DEMO: {side.upper()} {symbol} por {amount_usdt:g} USDT. No se envió orden."
    if not ALLOW_LIVE:
        return "🔒 Orden bloqueada: configura ALLOW_LIVE_TRADING=true tras probar en demo."
    ticker = exchange.fetch_ticker(symbol)
    price = float(ticker["last"])
    amount_coin = float(exchange.amount_to_precision(symbol, amount_usdt / price))
    order = exchange.create_market_order(symbol, side, amount_coin)
    return f"Orden enviada: {side.upper()} {symbol} cantidad {amount_coin} a mercado. ID: {order.get('id')}"


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.effective_message.reply_text("Uso: /comprar BTC/USDT 20"); return
    try:
        sym = context.args[0].upper()
        amt = float(context.args[1]) if len(context.args) > 1 else AMOUNT_USDT
        await update.effective_message.reply_text(manual_order(sym, "buy", amt))
    except Exception as exc:
        await update.effective_message.reply_text(f"Error de compra: {exc}")


async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.effective_message.reply_text("Uso: /vender BTC/USDT 20"); return
    try:
        sym = context.args[0].upper()
        amt = float(context.args[1]) if len(context.args) > 1 else AMOUNT_USDT
        await update.effective_message.reply_text(manual_order(sym, "sell", amt))
    except Exception as exc:
        await update.effective_message.reply_text(f"Error de venta: {exc}")


def main():
    global exchange
    if not TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en el archivo .env")
    exchange = get_exchange()
    exchange.load_markets()
    global learning
    learning = LearningCenter(exchange, WATCHLIST, TIMEFRAME)
    log.info("WEEX conectado; %s mercados.", len(exchange.markets))
    app = ApplicationBuilder().token(TOKEN).build()
    for name, callback in [
        ("start", cmd_start), ("help", cmd_start), ("precio", cmd_precio),
        ("analizar", cmd_analizar), ("ranking", cmd_ranking), ("mercado", cmd_mercado),
        ("monitor", cmd_monitor), ("aprendizaje", cmd_aprendizaje), ("ultimo", cmd_ultimo), ("estado", cmd_estado),
        ("balance", cmd_balance), ("comprar", cmd_buy), ("vender", cmd_sell),
    ]:
        app.add_handler(CommandHandler(name, callback))
    app.job_queue.run_repeating(market_job, interval=INTERVAL, first=15)
    app.job_queue.run_repeating(learning_job, interval=INTERVAL, first=30)
    log.info("Bot de Telegram iniciado.")
    app.run_polling()


if __name__ == "__main__":
    main()