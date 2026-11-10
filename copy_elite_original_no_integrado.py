# copy_elite.py v2 — usa el MISMO cliente HTTP que elite_scan.py
import os, time, threading, logging
import ccxt
from dotenv import load_dotenv
from weex_copy_api import WeexCopyAPI, _num
load_dotenv()

log = logging.getLogger("copy-elite")

def _norm_sym(s, futures=False):
    if not s:
        return None
    s = str(s).upper().replace("_", "/")
    if "/" not in s and s.endswith("USDT"):
        s = s[:-4] + "/USDT"
    if futures and ":" not in s and s.endswith("/USDT"):
        s += ":USDT"
    return s

class CopyElite:
    def __init__(self, notificar):
        self.notificar = notificar
        self.ratio = float(os.getenv("COPY_RATIO", "0.05"))
        self.max_usdt = float(os.getenv("COPY_MAX_USDT", "10"))
        self.min_leader = float(os.getenv("COPY_MIN_LEADER_USDT", "50"))
        self.leader = os.getenv("COPY_LEADER_ID", "").strip()
        self.enable = os.getenv("COPY_ENABLE", "false").lower() == "true"
        self.futures = os.getenv("COPY_ONLY", "spot").lower() in ("futures", "swap", "contract")
        self.dry = os.getenv("DRY_RUN", "true").lower() == "true"
        self.visto = set()
        self.api = WeexCopyAPI()
        self.ex = ccxt.weex({                      # ccxt SOLO para precio / orden
            "apiKey": os.getenv("WEEX_API_KEY"),
            "secret": os.getenv("WEEX_API_SECRET"),
            "password": os.getenv("WEEX_API_PASSPHRASE"),
            "enableRateLimit": True,
            "options": {"defaultType": "swap" if self.futures else "spot"},
        })

    def _posiciones_leader(self):
        return self.api.positions(self.leader) if self.leader else []

    def _replicar(self, p):
        raw_side = str(p.get("side") or p.get("direction") or "buy").lower()
        if raw_side in ("close", "closed", "reduce", "close_long", "close_short"):
            return  # luego lo manejamos con COPY_MIRROR_CLOSE

        oid = (p.get("id") or p.get("orderId") or p.get("positionId")
               or f'{p.get("symbol")}-{p.get("openTime")}-{raw_side}')
        if oid in self.visto:
            return
        self.visto.add(oid)

        sym = _norm_sym(p.get("symbol") or p.get("symbolName") or p.get("contract"), self.futures)
        side = "buy" if raw_side in ("buy", "long", "open_long") else "sell"
        size = _num(p.get("sizeUsdt") or p.get("amount") or p.get("margin") or p.get("value"))
        price = _num(p.get("entryPrice") or p.get("openPrice") or p.get("price"))

        if not sym or size < self.min_leader:
            return
        if price <= 0:
            try:
                price = _num(self.ex.fetch_ticker(sym)["last"])
            except Exception:
                return

        monto = min(size * self.ratio, self.max_usdt)
        if monto <= 0:
            return

        msg = (f"🧠 *Copy Élite* `{self.leader}`\n"
               f"Par: `{sym}` | {side.upper()}\n"
               f"Líder: `{size:.2f}` USDT @ `{price}`\n"
               f"Tú: `{monto:.2f}` USDT (ratio {self.ratio})")

        if self.dry:
            msg += "\n⚠️ `DRY_RUN` → no se envió orden"
        else:
            try:
                self.ex.create_order(sym, "market", side, monto / price)
                msg += "\n✅ Orden replicada"
            except Exception as e:
                msg += f"\n❌ Error orden: {e}"
        self.notificar(msg)

    def loop(self):
        if not self.enable or not self.leader:
            log.info("Copy desactivado o sin COPY_LEADER_ID")
            return
        _, origen = self.api.probe()
        self.notificar(f"👀 Aprendiendo del élite `{self.leader}`…\n🔌 {origen}")
        while True:
            for p in self._posiciones_leader():
                self._replicar(p)
            time.sleep(12)

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()