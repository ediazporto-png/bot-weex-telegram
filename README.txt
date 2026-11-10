WEEX TELEGRAM MARKET ASSISTANT

INSTALACIÓN (Windows):
1. Instala Python 3.10 o superior.
2. Extrae el ZIP en una carpeta.
3. Abre CMD en esa carpeta.
4. Ejecuta: py -m venv .venv
5. Ejecuta: .venv\Scripts\activate
6. Ejecuta: pip install -r requirements.txt
7. Copia .env.example a .env y añade credenciales NUEVAS.
8. Ejecuta: py bot_weex.py

COMANDOS TELEGRAM:
/start
/precio BTC/USDT
/analizar BTC/USDT
/ranking
/mercado
/monitor on
/monitor off
/ultimo
/estado
/balance
/comprar BTC/USDT 20
/vender BTC/USDT 20

SEGURIDAD:
- Las claves y el token que estaban en el .env subido quedaron expuestos.
  Revoca la API Key/Secret/Passphrase en WEEX y regenera el token con BotFather.
- No reutilices credenciales compartidas.
- El archivo .env.example no contiene secretos.
- DRY_RUN=true y ALLOW_LIVE_TRADING=false por defecto.
- El monitor y el ranking solo analizan; no colocan operaciones automáticas.
- Las órdenes manuales solo pueden enviarse si DRY_RUN=false y
  ALLOW_LIVE_TRADING=true. Prueba primero en demo.
- Configura permisos de API de WEEX con mínimo privilegio y sin retiros.
- El ranking usa indicadores técnicos y volumen, no garantiza rendimiento.

ALCANCE / LIMITACIONES:
- /ranking escanea hasta UNIVERSE_LIMIT mercados spot USDT con volumen mínimo.
- Indicadores: EMA, RSI, MACD y volumen relativo.
- El análisis no constituye asesoramiento financiero.
- copy_elite.py se incluye como referencia original, pero no se inicia ni se
  considera funcional en este paquete. Importa weex_copy_api.py, que no fue
  proporcionado. Tampoco se incluyó elite_html.py ni un método verificado para
  extraer historial de traders públicos. La función de aprender patrones de
  traders élite requiere primero implementar y probar esa fuente de datos.
- No hay modelo de IA que aprenda por sí solo ni entrenamiento automático
  persistente en esta versión.


CENTRO DE APRENDIZAJE PARA PC DE 4 GB
- El aprendizaje usa el mismo proceso del bot y SQLite; no entrena modelos pesados.
- Horario local predeterminado: 08:00 a 20:00 (12 horas). Cambia LEARNING_START_HOUR y
  LEARNING_END_HOUR en .env si prefieres otro horario.
- Escanea los pares de SYMBOLS, registra indicadores y simula entradas/salidas.
- No coloca órdenes reales ni utiliza saldo real para las simulaciones.
- Los resultados quedan en weex_learning.db y sobreviven al reinicio. Haz copia de ese archivo.
- Telegram: /aprendizaje estado | /aprendizaje informe | /aprendizaje on | /aprendizaje off
- En PC de 4 GB, conserva 3 pares y CHECK_INTERVAL_SEC=300 (5 minutos).
- El bot debe permanecer abierto y la PC conectada a Internet durante el horario.
- Evita suspensión/hibernación de Windows; apaga solo la pantalla.
- La simulación no incluye deslizamiento real y usa una comisión estimada configurable;
  no equivale a backtesting completo ni predice ganancias.
