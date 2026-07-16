"""Configuración centralizada, toda por variables de entorno.

Ningún secreto vive en el repo. En Dokploy los valores van en el env de la
aplicación; en local, en un archivo .env (gitignored).
"""
import os

# Marcador de build para verificar que el auto-deploy (push→webhook→rebuild) llegó.
DEPLOY_MARKER = "2026-07-10-autodeploy-github-provider"


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# ── Chequeo de WAN (liviano, TCP-connect) ─────────────────────────────
# Formato host:puerto. Se considera "hay internet" si al menos uno responde.
# TCP-connect en vez de ICMP: no necesita privilegios raw-socket en Docker y
# atraviesa NAT/firewalls de forma más confiable que un ping.
WAN_TARGETS       = _list("WAN_TARGETS", "8.8.8.8:53,1.1.1.1:443,9.9.9.9:53")
CHECK_INTERVAL    = _int("CHECK_INTERVAL_SECS", 30)   # cadencia del watchdog
CHECK_TIMEOUT     = _int("CHECK_TIMEOUT_SECS", 4)     # timeout por intento
FAIL_THRESHOLD    = _int("FAIL_THRESHOLD", 4)         # fallos seguidos = WAN caído
OK_THRESHOLD      = _int("OK_THRESHOLD", 2)           # éxitos seguidos = WAN OK

# ── ONU / EPON ────────────────────────────────────────────────────────
ONU_URL   = os.getenv("ONU_URL", "http://192.168.77.1").rstrip("/")
ONU_HOST  = os.getenv("ONU_HOST", "192.168.77.1")    # para distinguir cuelgue
ONU_PORT  = _int("ONU_PORT", 80)
ONU_USER  = os.getenv("ONU_USER", "user")
ONU_PASS  = os.getenv("ONU_PASS", "")

# ── Política de reboot (salvaguardas) ─────────────────────────────────
REBOOT_COOLDOWN     = _int("REBOOT_COOLDOWN_SECS", 1200)   # 20 min entre reboots exitosos
MAX_REBOOTS_WINDOW  = _int("MAX_REBOOTS_WINDOW", 3)        # tope por ventana
REBOOT_WINDOW       = _int("REBOOT_WINDOW_SECS", 21600)    # ventana = 6 h
POST_REBOOT_GRACE   = _int("POST_REBOOT_GRACE_SECS", 300)  # espera recuperación
REQUIRE_ONU_UP      = _bool("REQUIRE_ONU_UP", True)        # solo si el ONU responde
DRY_RUN             = _bool("DRY_RUN", False)              # simula, no reinicia
# Reintento tras un intento de reboot FALLIDO (no cuenta contra el cooldown).
# Evita esperar 20 min si el ONU estaba lento; sin relanzar chromium a lo loco.
FAILED_REBOOT_BACKOFF = _int("FAILED_REBOOT_BACKOFF_SECS", 120)

# ── Robustez de Playwright (ONU lento durante un corte) ───────────────
# Durante un corte el panel del ONU puede tardar mucho o colgarse; por eso
# timeouts holgados y reintentos de navegación.
PW_NAV_TIMEOUT_MS = _int("PW_NAV_TIMEOUT_MS", 45000)   # timeout por navegación
PW_NAV_RETRIES    = _int("PW_NAV_RETRIES", 3)          # reintentos de goto
PW_RETRY_GAP      = _int("PW_RETRY_GAP_SECS", 5)       # espera entre reintentos

# ── Dashboard LAN ─────────────────────────────────────────────────────
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = _int("DASHBOARD_PORT", 8099)
# Token compartido para /api/trigger (reinicia el ONU). Vacío = sin exigir
# token (comportamiento anterior, solo para dev local). En Dokploy siempre
# debe estar seteado. Se pasa como ?token=... (mismo patrón que Vigilancia-MBP)
# para poder seguir abriendo el dashboard desde el celu con una URL guardada.
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")

# ── Notificaciones ────────────────────────────────────────────────────
# Discord: flush diferido (se envía cuando vuelve el WAN).
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
# ntfy opcional (push local instantáneo, sirve durante el corte si el server
# ntfy está en la LAN). Dejar vacío para desactivar.
NTFY_URL   = os.getenv("NTFY_URL", "")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")

# ── Persistencia / artefactos ─────────────────────────────────────────
DATA_DIR       = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
STATE_FILE     = os.path.join(DATA_DIR, "state.json")
SHOT_DIR       = os.path.join(DATA_DIR, "shots")
MAX_EVENTS     = _int("MAX_EVENTS", 500)                   # ring buffer del log

# Zona horaria para los timestamps mostrados (el contenedor debería tener TZ).
TZ = os.getenv("TZ", "America/Argentina/Buenos_Aires")


def masked_summary() -> dict:
    """Config visible para el dashboard/logs, sin exponer la password."""
    return {
        "onu_url": ONU_URL,
        "onu_pass_set": bool(ONU_PASS),
        "wan_targets": WAN_TARGETS,
        "check_interval": CHECK_INTERVAL,
        "fail_threshold": FAIL_THRESHOLD,
        "reboot_cooldown": REBOOT_COOLDOWN,
        "max_reboots_window": MAX_REBOOTS_WINDOW,
        "reboot_window": REBOOT_WINDOW,
        "require_onu_up": REQUIRE_ONU_UP,
        "dry_run": DRY_RUN,
        "discord": bool(DISCORD_WEBHOOK_URL),
        "ntfy": bool(NTFY_URL and NTFY_TOPIC),
    }
