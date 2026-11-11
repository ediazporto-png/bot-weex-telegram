import sqlite3
import pandas as pd
import requests
import ta
import numpy as np

class ModuloAprendizaje:
    def __init__(self, capital_inicial=1000.0, db_name="weex_learning.db"):
        self.capital_inicial = capital_inicial
        self.db_name = db_name
        self.inicializar_bd()

    def inicializar_bd(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS operaciones_simuladas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                senal TEXT,
                precio REAL,
                rsi REAL,
                pnl REAL DEFAULT 0.0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS historial_balance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                balance REAL
            )
        """)
        conn.commit()
        conn.close()

    def obtener_datos_mercado(self):
        try:
            url = "https://api.weex.com/api/v1/market/klines?symbol=BTCUSDT&interval=1m&limit=100"
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json().get('data', [])
                if data:
                    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
                    return df
        except Exception as e:
            print(f"[AVISO] Usando motor de respaldo local: {e}")
        
        np.random.seed(42)
        precios = 60000 + np.cumsum(np.random.randn(100) * 10)
        return pd.DataFrame({
            'timestamp': range(100),
            'open': precios - 5,
            'high': precios + 10,
            'low': precios - 10,
            'close': precios,
            'volume': np.random.randint(100, 1000, 100)
        })

    def calcular_indicadores(self, df):
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        df['ema_fast'] = ta.trend.EMAIndicator(df['close'], window=9).ema_indicator()
        df['ema_slow'] = ta.trend.EMAIndicator(df['close'], window=21).ema_indicator()
        df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
        return df.dropna()

    def evaluar_senal(self, df):
        if df.empty:
            return "ESPERAR", 0.0
        ult = df.iloc[-1]
        rsi = ult['rsi']
        precio = ult['close']
        if rsi < 30:
            return "COMPRA", precio
        elif rsi > 70:
            return "VENTA", precio
        else:
            return "ESPERAR", precio

    def registrar_operacion_simulada(self, senal, precio):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO operaciones_simuladas (senal, precio, rsi)
            VALUES (?, ?, ?)
        """, (senal, precio, 50.0))
        conn.commit()
        conn.close()