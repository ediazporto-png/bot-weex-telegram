import ccxt, os

def sincronizar_mercados_weex():
    print("Conectando a WEEX para descargar todos los mercados y futuros disponibles...")
    exchange = ccxt.weex()
    markets = exchange.load_markets()
    symbols = list(markets.keys())
    print(f"¡Total de mercados encontrados en WEEX: {len(symbols)}!")
    
    # Filtrar pares USDT
    usdt_symbols = [s for s in symbols if 'USDT' in s]
    print(f"Pares con USDT detectados: {len(usdt_symbols)}")
    
    # Actualizar .env dinámicamente con los primeros 200 pares de alta liquidez
    env_path = '.env'
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Seleccionar muestra masiva
        sample_symbols = ",".join(usdt_symbols[:150])
        if 'SYMBOLS=' in content:
            lines = content.splitlines()
            new_lines = [l if not l.startswith('SYMBOLS=') else f'SYMBOLS={sample_symbols}' for l in lines]
            content = "\n".join(new_lines)
        else:
            content += f"\nSYMBOLS={sample_symbols}\n"
            
        with open(env_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print("¡Archivo .env actualizado automáticamente con el universo masivo de WEEX!")

if __name__ == '__main__':
    sincronizar_mercados_weex()
