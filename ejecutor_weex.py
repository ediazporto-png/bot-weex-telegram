import time
def ejecutar_orden_simulada(simbolo, accion, precio, cantidad):
    log = f"[SIMULACION] Orden {accion} enviada para {simbolo}: {cantidad} unidades a ${precio}."
    with open("memoria_razonamiento.md", "a", encoding="utf-8") as f:
        f.write(f"\n- {log}")
    return log
