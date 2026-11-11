"""
keep_alive.py - Mantiene vivo el bot en Render (plan gratuito).

Render apaga ("spin down") los Web Services gratuitos tras 15 minutos sin
recibir trafico entrante, y ademas exige que el servicio escuche en el
puerto de la variable de entorno PORT (si no, el despliegue falla con
"Timed Out"). Este modulo resuelve ambas cosas sin dependencias nuevas:

1. Servidor HTTP minimo en 0.0.0.0:$PORT con endpoint /health (200 OK).
2. Auto-ping cada pocos minutos a la URL publica del servicio, de modo
   que siempre haya trafico entrante y Render no lo apague.

Uso en bot_weex.py (antes de app.run_polling()):

    from keep_alive import start_keep_alive
    start_keep_alive()

Variables de entorno:
    PORT                  Puerto a escuchar (Render lo define solo).
    RENDER_EXTERNAL_URL   URL publica del servicio (Render la define solo),
                          p. ej. https://bot-weex-telegram.onrender.com
    SELF_PING_URL         URL alternativa a la que hacer ping (opcional,
                          tiene prioridad sobre RENDER_EXTERNAL_URL).
    SELF_PING_ENABLED     "true"/"false". Por defecto "true".
    SELF_PING_INTERVAL_SEC  Segundos entre pings. Por defecto 600 (10 min,
                          por debajo del limite de 15 min de Render).
"""
import json
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("keep-alive")

STARTED_AT = datetime.now(timezone.utc)
PORT = int(os.getenv("PORT", "10000"))
PING_ENABLED = os.getenv("SELF_PING_ENABLED", "true").lower() == "true"
PING_INTERVAL = max(60, int(os.getenv("SELF_PING_INTERVAL_SEC", "600")))
PING_URL = (os.getenv("SELF_PING_URL", "").strip()
            or os.getenv("RENDER_EXTERNAL_URL", "").strip()).rstrip("/")


class HealthHandler(BaseHTTPRequestHandler):
    server_version = "KeepAlive/1.0"

    def _send(self, code, body, content_type):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            uptime = (datetime.now(timezone.utc) - STARTED_AT).total_seconds()
            self._send(200, json.dumps({
                "status": "ok",
                "service": "bot-weex-telegram",
                "uptime_sec": int(uptime),
                "time_utc": datetime.now(timezone.utc).isoformat(),
            }), "application/json")
        else:
            self._send(200, "🤖 WEEX Telegram bot en linea. /health para estado.",
                       "text/plain; charset=utf-8")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):  # evita ruido en los logs de Render
        if self.path.startswith("/health"):
            return
        log.info("%s - %s", self.address_string(), fmt % args)


def start_health_server():
    """Levanta el servidor HTTP en un hilo daemon. Devuelve (server, thread)."""
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 5},
                              name="health-server", daemon=True)
    thread.start()
    log.info("Servidor de salud escuchando en 0.0.0.0:%s (/health)", PORT)
    return server, thread


def _self_ping_loop(url, interval):
    time.sleep(60)  # deja que el servicio termine de arrancar
    target = url + "/health"
    log.info("Auto-ping activado: %s cada %ss", target, interval)
    while True:
        try:
            req = urllib.request.Request(target, headers={"User-Agent": "KeepAlive/1.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                log.info("Auto-ping OK (%s)", resp.status)
        except Exception as exc:
            log.warning("Auto-ping fallo: %s", exc)
        time.sleep(interval)


def start_self_ping():
    """Hilo daemon que se hace ping a si mismo para evitar el spin-down."""
    if not PING_ENABLED:
        log.info("Auto-ping desactivado (SELF_PING_ENABLED=false).")
        return None
    if not PING_URL:
        log.warning("Sin RENDER_EXTERNAL_URL/SELF_PING_URL: auto-ping omitido. "
                    "Usa un monitor externo (UptimeRobot, cron-job.org) contra /health.")
        return None
    thread = threading.Thread(target=_self_ping_loop,
                              args=(PING_URL, PING_INTERVAL),
                              name="self-ping", daemon=True)
    thread.start()
    return thread


def start_keep_alive():
    """Arranca servidor de salud + auto-ping. Llamar una sola vez al inicio."""
    start_health_server()
    start_self_ping()
