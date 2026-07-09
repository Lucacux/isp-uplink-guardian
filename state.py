"""Estado + log de eventos, con persistencia local en JSON.

Diseñado para trazabilidad sin WAN: todo queda en disco y se expone por el
dashboard LAN. Los eventos además llevan una marca `notified` para el flush
diferido a Discord (se envían cuando vuelve internet).
"""
import os
import json
import time
from datetime import datetime
from collections import deque

import config

# Tipos de evento
WAN_DOWN      = "wan_down"
WAN_UP        = "wan_up"
REBOOT_START  = "reboot_start"
REBOOT_OK     = "reboot_ok"
REBOOT_FAIL   = "reboot_fail"
REBOOT_SKIP   = "reboot_skip"     # bloqueado por cooldown / tope / ONU caído
INFO          = "info"


class Store:
    def __init__(self):
        self.events: deque = deque(maxlen=config.MAX_EVENTS)
        self.wan_up: bool | None = None
        self.onu_up: bool | None = None
        self.last_change_ts: float | None = None      # último cambio de estado WAN
        self.current_outage_start: float | None = None
        self.reboot_history: list[float] = []          # timestamps de reboots
        self.last_reboot_ts: float | None = None
        self.started_ts: float = time.time()
        self.reboot_lock = None       # asyncio.Lock, seteado en app.main()
        self.last_attempt_ts: float | None = None   # último INTENTO de reboot (ok o no)
        self.last_reboot_ok: bool = False           # ¿el último intento fue exitoso?
        self._load()

    # ── persistencia ──────────────────────────────────────────────
    def _load(self):
        try:
            with open(config.STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            for e in d.get("events", []):
                self.events.append(e)
            self.reboot_history = d.get("reboot_history", [])
            self.last_reboot_ts = d.get("last_reboot_ts")
            self.current_outage_start = d.get("current_outage_start")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def save(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        tmp = config.STATE_FILE + ".tmp"
        data = {
            "events": list(self.events),
            "reboot_history": self.reboot_history,
            "last_reboot_ts": self.last_reboot_ts,
            "current_outage_start": self.current_outage_start,
            "saved_at": time.time(),
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, config.STATE_FILE)

    # ── eventos ───────────────────────────────────────────────────
    def add_event(self, etype: str, msg: str, extra: dict | None = None) -> dict:
        ev = {
            "ts": time.time(),
            "iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": etype,
            "msg": msg,
            "extra": extra or {},
            "notified": False,      # para el flush diferido a Discord
        }
        self.events.append(ev)
        print(f"[{ev['iso']}] {etype.upper()}: {msg}", flush=True)
        self.save()
        return ev

    def pending_notifications(self) -> list[dict]:
        return [e for e in self.events if not e.get("notified")]

    def mark_notified(self, evs: list[dict]):
        for e in evs:
            e["notified"] = True
        self.save()

    # ── reboots: ventana deslizante ───────────────────────────────
    def reboots_in_window(self) -> int:
        cutoff = time.time() - config.REBOOT_WINDOW
        self.reboot_history = [t for t in self.reboot_history if t >= cutoff]
        return len(self.reboot_history)

    def record_reboot(self):
        now = time.time()
        self.reboot_history.append(now)
        self.last_reboot_ts = now
        self.save()

    def cooldown_remaining(self) -> float:
        if self.last_reboot_ts is None:
            return 0.0
        return max(0.0, config.REBOOT_COOLDOWN - (time.time() - self.last_reboot_ts))

    # ── snapshot para el dashboard/API ────────────────────────────
    def snapshot(self) -> dict:
        return {
            "wan_up": self.wan_up,
            "onu_up": self.onu_up,
            "since": self.last_change_ts,
            "outage_start": self.current_outage_start,
            "outage_secs": (time.time() - self.current_outage_start)
                           if (self.wan_up is False and self.current_outage_start) else 0,
            "reboots_in_window": self.reboots_in_window(),
            "max_reboots": config.MAX_REBOOTS_WINDOW,
            "cooldown_remaining": self.cooldown_remaining(),
            "last_reboot_ts": self.last_reboot_ts,
            "uptime_secs": time.time() - self.started_ts,
            "now": time.time(),
            "events": list(self.events)[-60:][::-1],   # últimos 60, más nuevos primero
            "config": config.masked_summary(),
        }
