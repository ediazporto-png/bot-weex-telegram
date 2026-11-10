import os, ccxt, pandas as pd

def configurar_wallet_y_excel():
    print("Configurando integración de Wallet personal (NC) y exportación a Excel...")
    
    # 1. Actualizar .env con micro-inversión de 1 USDT y modo real/prueba
    env_path = '.env'
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Ajustar TRADE_AMOUNT_USDT a 1
        if 'TRADE_AMOUNT_USDT=' in content:
            lines = content.splitlines()
            new_lines = [l if not l.startswith('TRADE_AMOUNT_USDT=') else 'TRADE_AMOUNT_USDT=1' for l in lines]
            content = '\n'.join(new_lines)
        else:
            content += '\nTRADE_AMOUNT_USDT=1\n'
            
        with open(env_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print("¡Monto de operación fijado en 1 USDT en el archivo .env!")

    # 2. Asegurar que las librerías de Excel (openpyxl) estén instaladas
    import subprocess
    subprocess.run(['python', '-m', 'pip', 'install', 'openpyxl', '--upgrade'], check=True)
    print("¡Librería para generar archivos Excel (openpyxl) instalada y actualizada!")

if __name__ == '__main__':
    configurar_wallet_y_excel()
