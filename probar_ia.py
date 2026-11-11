import sys
import os
from modulo_aprendizaje import ModuloAprendizaje

def ejecutar_prueba():
    print(">>> Iniciando prueba del motor de IA / Paper Trading...")
    try:
        ia = ModuloAprendizaje(capital_inicial=1000.0)
        print("[OK] Modulo inicializado.")
        
        df_velas = ia.obtener_datos_mercado()
        if df_velas is None or df_velas.empty:
            print("[ERROR] Sin datos de mercado.")
            return
            
        print(f"[OK] Velas obtenidas: {len(df_velas)}")
        df_ind = ia.calcular_indicadores(df_velas)
        ult = df_ind.iloc[-1]
        
        print(f"  - Cierre: {ult.get('close', 'N/A')}")
        print(f"  - RSI: {ult.get('rsi', 'N/A')}")
        
        senal, precio = ia.evaluar_senal(df_ind)
        print(f"[SENAL] Accion: {senal} | Precio: {precio}")
        
        ia.registrar_operacion_simulada(senal=senal, precio=precio)
        print("\n>>> ¡Prueba exitosa!")
    except Exception as e:
        print(f"\n[ERROR] {e}")

if __name__ == "__main__":
    ejecutar_prueba()