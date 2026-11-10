"""Centro de aprendizaje WEEX: registro y simulación, nunca envía órdenes."""
import os
import sqlite3
from datetime import datetime, time as dtime
from pathlib import Path

import pandas as pd

DB_PATH = Path(os.getenv("LEARNING_DB", "weex_learning.db"))
PAPER_USDT = float(os.getenv("PAPER_TRADE_USDT", "100"))
STOP_LOSS = float(os.getenv("PAPER_STOP_LOSS_PCT", "2.0"))
TAKE_PROFIT = float(os.getenv("PAPER_TAKE_PROFIT_PCT", "4.0"))
FEE_PCT = float(os.getenv("PAPER_FEE_PCT", "0.10"))
START_HOUR = int(os.getenv("LEARNING_START_HOUR", "8"))
END_HOUR = int(os.getenv("LEARNING_END_HOUR", "20"))

class LearningCenter:
    def __init__(self, exchange, symbols, timeframe="15m"):
        self.exchange = exchange
        self.symbols = list(symbols)[:8]  # límite conservador para PC de 4 GB
        self.timeframe = timeframe
        self.enabled = os.getenv("LEARNING_ENABLED", "true").lower() == "true"
        self.db = str(DB_PATH)
        self._init_db()

    def _connect(self):
        con = sqlite3.connect(self.db, timeout=20)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _init_db(self):
        with self._connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, candle_ts INTEGER NOT NULL,
                recorded_at TEXT NOT NULL, price REAL NOT NULL, score INTEGER NOT NULL,
                signal TEXT NOT NULL, rsi REAL, ema_fast REAL, ema_slow REAL,
                macd REAL, macd_signal REAL, volume_ratio REAL,
                UNIQUE(symbol,candle_ts))""")
            con.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, entry_ts TEXT NOT NULL,
                entry_price REAL NOT NULL, amount_usdt REAL NOT NULL,
                exit_ts TEXT, exit_price REAL, gross_pct REAL, net_pct REAL,
                status TEXT NOT NULL DEFAULT 'OPEN', exit_reason TEXT)""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status,symbol)")

    def active_now(self):
        now = datetime.now().time()
        start, end = dtime(START_HOUR, 0), dtime(END_HOUR, 0)
        if start == end:
            return True
        if start < end:
            return start <= now < end
        return now >= start or now < end

    @staticmethod
    def _rsi(close, period=14):
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
        loss = -delta.clip(upper=0).ewm(alpha=1/period, adjust=False).mean()
        rs = gain / loss.replace(0, 1e-12)
        return 100 - 100/(1+rs)

    def _indicators(self, symbol):
        candles = self.exchange.fetch_ohlcv(symbol, timeframe=self.timeframe, limit=100)
        if not candles or len(candles) < 35:
            raise ValueError("Historial insuficiente")
        df = pd.DataFrame(candles, columns=["ts","open","high","low","close","volume"])
        c = df.close
        df["ef"] = c.ewm(span=9, adjust=False).mean()
        df["es"] = c.ewm(span=21, adjust=False).mean()
        df["rsi"] = self._rsi(c)
        df["macd"] = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        df["ms"] = df.macd.ewm(span=9, adjust=False).mean()
        df["vavg"] = df.volume.rolling(20).mean()
        x = df.iloc[-1]
        score = 0
        score += 25 if x.ef > x.es else 0
        score += 20 if x.macd > x.ms else 0
        score += 20 if 50 <= x.rsi <= 68 else (10 if 40 <= x.rsi < 50 else (-10 if x.rsi > 75 else 0))
        vr = float(x.volume / x.vavg) if pd.notna(x.vavg) and x.vavg else 0.0
        score += 15 if vr >= 1.5 else (8 if vr >= 1 else 0)
        score += 10 if x.close > x.ef else 0
        score += 10 if x.ef > df.iloc[-2].ef else 0
        score = max(0, min(100, int(score)))
        signal = "ALCISTA" if score >= 65 else ("BAJISTA" if score < 30 else "NEUTRAL")
        return {"ts": int(x.ts), "price": float(x.close), "score": score,
                "signal": signal, "rsi": float(x.rsi), "ef": float(x.ef),
                "es": float(x.es), "macd": float(x.macd), "ms": float(x.ms),
                "vr": vr}

    def _save_observation(self, symbol, x):
        with self._connect() as con:
            con.execute("""INSERT OR IGNORE INTO observations
                (symbol,candle_ts,recorded_at,price,score,signal,rsi,ema_fast,ema_slow,macd,macd_signal,volume_ratio)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (symbol,x["ts"],datetime.now().isoformat(timespec="seconds"),x["price"],x["score"],x["signal"],
                 x["rsi"],x["ef"],x["es"],x["macd"],x["ms"],x["vr"]))

    def _paper_trade(self, symbol, x):
        with self._connect() as con:
            row = con.execute("SELECT id,entry_price FROM paper_trades WHERE symbol=? AND status='OPEN' ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
            price = x["price"]
            if row:
                trade_id, entry = row
                gross = (price-entry)/entry*100
                reason = None
                if gross <= -STOP_LOSS: reason = "STOP_LOSS_SIMULADO"
                elif gross >= TAKE_PROFIT: reason = "TAKE_PROFIT_SIMULADO"
                elif x["signal"] == "BAJISTA": reason = "CAMBIO_TENDENCIA"
                if reason:
                    net = gross - (2*FEE_PCT)
                    con.execute("UPDATE paper_trades SET exit_ts=?,exit_price=?,gross_pct=?,net_pct=?,status='CLOSED',exit_reason=? WHERE id=?",
                        (datetime.now().isoformat(timespec="seconds"),price,gross,net,reason,trade_id))
            elif x["signal"] == "ALCISTA":
                con.execute("INSERT INTO paper_trades(symbol,entry_ts,entry_price,amount_usdt,status) VALUES(?,?,?,?, 'OPEN')",
                    (symbol,datetime.now().isoformat(timespec="seconds"),price,PAPER_USDT))

    def scan(self):
        if not self.enabled or not self.active_now():
            return {"status":"fuera_de_horario", "scanned":0, "errors":0}
        scanned = errors = 0
        for symbol in self.symbols:
            try:
                x = self._indicators(symbol)
                self._save_observation(symbol,x)
                self._paper_trade(symbol,x)
                scanned += 1
            except Exception:
                errors += 1
        return {"status":"ok", "scanned":scanned, "errors":errors}

    def report(self):
        with self._connect() as con:
            obs = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            total, wins, losses, avg, net = con.execute("""SELECT COUNT(*),
                SUM(CASE WHEN net_pct>0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN net_pct<=0 THEN 1 ELSE 0 END), AVG(net_pct), SUM(net_pct)
                FROM paper_trades WHERE status='CLOSED'""").fetchone()
            opened = con.execute("SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'").fetchone()[0]
        wins, losses = wins or 0, losses or 0
        winrate = (wins/total*100) if total else 0
        return (f"🧠 CENTRO DE APRENDIZAJE WEEX\n"
                f"Observaciones guardadas: {obs}\nOperaciones simuladas cerradas: {total}\n"
                f"Ganadoras: {wins} | Perdedoras: {losses}\nAcierto: {winrate:.1f}%\n"
                f"Promedio neto simulado: {avg or 0:.2f}%\nSuma neta por operación: {net or 0:.2f}%\n"
                f"Simulaciones abiertas: {opened}\nBase de datos: {self.db}\n"
                f"Nota: estadísticas históricas; no garantizan resultados futuros.")
