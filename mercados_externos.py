import requests 
def consultar_precio_externo(symbol="BTCUSDT"): 
    try: 
        # Ejemplo consultando el libro de ¢rdenes p£blico de Binance como referencia global 
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}" 
        r = requests.get(url, timeout=5) 
        return r.json().get("price", "N/A") 
    except Exception as e: 
        return f"Error de conexi¢n: {e}" 
