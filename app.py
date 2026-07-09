"""crappy-ISP — watchdog que reinicia el ONU cuando se cae internet.

Loop principal (asyncio):
  · Cada CHECK_INTERVAL: prueba WAN (TCP-connect liviano) con debounce.
  · WAN cae de forma confirmada → si se cumplen las salvaguardas, dispara el
    reboot del ONU vía Playwright y espera la recuperación.
  · Cuando el WAN vuelve, hace el flush diferido de eventos a Discord.
El dashboard LAN corre en el mismo event loop.
"""
import asyncio
import time

import config
import state as st
from checks import wan_alive, onu_alive
from dashboard import start_dashboard
import notifier


async def _confirm(check, target: bool, needed: int, interval: float = 1.5) -> bool:
    """Debounce: confirma `needed` lecturas seguidas == target."""
    for _ in range(needed):
        if await check() != target:
            return False
        await asyncio.sleep(interval)
    return True


async def try_reboot(store: st.Store):
    """Llamado repetidamente mientras hay corte. Espacia los intentos:
    cooldown largo tras un reboot EXITOSO (deja re-sincronizar el ONU) y backoff
    corto tras un intento FALLIDO (para reintentar pronto si el ONU estaba lento).
    """
    if store.last_attempt_ts is not None:
        interval = config.REBOOT_COOLDOWN if store.last_reboot_ok else config.FAILED_REBOOT_BACKOFF
        if time.time() - store.last_attempt_ts < interval:
            return
    await do_reboot(store)


async def do_reboot(store: st.Store, manual: bool = False):
    """Reinicia el ONU si se cumplen las salvaguardas.

    manual=True (dashboard) saltea el tope de ventana porque es una acción humana
    explícita, pero respeta REQUIRE_ONU_UP y DRY_RUN. El espaciado entre intentos
    automáticos lo maneja try_reboot; los intentos FALLIDOS no cuentan contra el
    cooldown/tope (solo los reboots reales exitosos).
    """
    from reboot_onu import reboot_onu  # import perezoso (Playwright)

    if store.reboot_lock.locked():
        store.add_event(st.INFO, "Ya hay un reboot en curso; ignoro el disparo.")
        return
    async with store.reboot_lock:
        if not manual:
            store.last_attempt_ts = time.time()

        # Salvaguarda 1: el ONU tiene que estar accesible (si no, reboot no ayuda).
        onu = await onu_alive()
        store.onu_up = onu
        if config.REQUIRE_ONU_UP and not onu:
            store.last_reboot_ok = False
            store.add_event(st.REBOOT_SKIP,
                            "WAN caído pero el ONU no responde — no reinicio a ciegas "
                            "(puede ser corte de energía o del ISP).")
            return

        # Salvaguarda 2: tope de reboots exitosos por ventana.
        if not manual and store.reboots_in_window() >= config.MAX_REBOOTS_WINDOW:
            store.last_reboot_ok = True  # frena reintentos por el cooldown largo
            store.add_event(st.REBOOT_SKIP,
                            f"Alcanzado el tope de {config.MAX_REBOOTS_WINDOW} reboots por "
                            "ventana. Probablemente sea un corte del ISP; solo monitoreo.")
            return

        await _run_reboot(store, reboot_onu, manual)


async def _run_reboot(store: st.Store, reboot_onu, manual: bool):
    store.add_event(st.REBOOT_START,
                    "Reiniciando el ONU" + (" [DRY_RUN]" if config.DRY_RUN else "") + "…")
    await notifier.push_ntfy("crappy-ISP", "Reiniciando el ONU por caída de internet", "high")

    try:
        res = await reboot_onu(config.SHOT_DIR)
    except Exception as e:
        store.last_reboot_ok = False
        store.add_event(st.REBOOT_FAIL, f"Error lanzando el reboot: {e}")
        return

    if res.get("reboot_request"):
        store.add_event(st.INFO, f"POST de reboot capturado: {res['reboot_request'].get('url')}",
                        extra={"post": res["reboot_request"].get("post")})

    if not res.get("ok"):
        store.last_reboot_ok = False
        store.add_event(st.REBOOT_FAIL, res.get("detail", "Falló el reboot."),
                        extra={"shots": res.get("shots", [])})
        return

    # Éxito. Solo cuenta contra el tope/cooldown un reboot REAL (no dry-run).
    store.last_reboot_ok = True
    if not config.DRY_RUN:
        store.record_reboot()
    store.add_event(st.REBOOT_OK, res.get("detail", "Reboot enviado."),
                    extra={"shots": res.get("shots", [])})

    if config.DRY_RUN:
        return

    # Esperar recuperación (el ONU tarda en bootear + re-sync PON/PPPoE).
    deadline = time.time() + config.POST_REBOOT_GRACE
    while time.time() < deadline:
        await asyncio.sleep(10)
        if await wan_alive():
            store.add_event(st.INFO, "WAN recuperado tras el reboot.")
            return
    store.add_event(st.INFO,
                    f"Sin WAN {config.POST_REBOOT_GRACE}s después del reboot; "
                    "sigo vigilando (respetando cooldown/tope).")


async def watchdog(store: st.Store):
    print("[watchdog] iniciado.", flush=True)
    # Estado inicial
    store.wan_up = await wan_alive()
    store.onu_up = await onu_alive()
    store.last_change_ts = time.time()
    store.add_event(st.INFO,
                    f"Arranque. WAN={'OK' if store.wan_up else 'CAÍDO'}, "
                    f"ONU={'OK' if store.onu_up else 'CAÍDO'}.")

    while True:
        try:
            alive = await wan_alive()
            store.onu_up = await onu_alive()

            if not alive:
                if store.wan_up:
                    # Transición a caído → confirmar con debounce.
                    if await _confirm(wan_alive, False, config.FAIL_THRESHOLD):
                        store.wan_up = False
                        store.last_change_ts = time.time()
                        store.current_outage_start = time.time()
                        store.last_attempt_ts = None   # arrancar intentos de cero
                        store.add_event(st.WAN_DOWN, "Internet caído (confirmado).")
                        await try_reboot(store)
                else:
                    # Sigue caído → reintentar reboot (try_reboot espacia los intentos).
                    await try_reboot(store)

            else:
                if not store.wan_up:
                    # Posible recuperación → confirmar.
                    if await _confirm(wan_alive, True, config.OK_THRESHOLD):
                        down_for = (time.time() - store.current_outage_start) \
                            if store.current_outage_start else 0
                        store.wan_up = True
                        store.last_change_ts = time.time()
                        store.current_outage_start = None
                        store.last_attempt_ts = None
                        store.last_reboot_ok = False
                        store.add_event(st.WAN_UP,
                                        f"Internet restablecido tras {int(down_for)}s de corte.")
                        store.save()
                        await notifier.flush_discord(store)
                else:
                    # Todo OK: aprovechar para vaciar notificaciones pendientes.
                    if store.pending_notifications():
                        await notifier.flush_discord(store)

        except Exception as e:
            store.add_event(st.INFO, f"Excepción en el watchdog: {e}")

        await asyncio.sleep(config.CHECK_INTERVAL)


async def main():
    store = st.Store()
    store.reboot_lock = asyncio.Lock()

    async def manual_trigger():
        """Disparo manual del reboot desde el dashboard (respeta DRY_RUN)."""
        store.add_event(st.INFO, "Disparo manual de reboot desde el dashboard.")
        await do_reboot(store, manual=True)

    await start_dashboard(store, manual_trigger)
    await watchdog(store)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
