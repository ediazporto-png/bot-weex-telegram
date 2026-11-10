import os, ccxt
from dotenv import load_dotenv

load_dotenv()

def verificar_nc_wallet():
    api_key = os.getenv("WEEX_API_KEY")
    secret = os.getenv("WEEX_API_SECRET")
    passphrase = os.getenv("WEEX_API_PASSPHRASE")
    
    print("Conectando con la API de WEEX para validar la NC Wallet...")
    exchange = ccxt.weex({
        'apiKey': api_key,
        'secret': secret,
        'password': passphrase,
        'enableRateLimit': True
    })
    try:
        balance = exchange.fetch_balance()
        usdt_free = balance.get('USDT', {}).get('free', 0.0)
        print(f"¡WALLET CONECTADA CON ÉXITO!")
        print(f"• Cuenta vinculada: NC Wallet / WEEX API")
        print(f"• Saldo disponible en USDT: {usdt_free} USDT")
    except Exception as e:
        print(f"Error al conectar con la Wallet: {e}")

if __name__ == '__main__':
    verificar_nc_wallet()
