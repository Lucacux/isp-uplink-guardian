"""ISP Uplink Guardian — watchdog que reinicia el ONU cuando se cae internet.

Loop principal (asyncio):
  · Cada CHECK_INTERVAL: prueba WAN (TCP-connect liviano) con debounce.
  · WAN cae de forma confirmada → si se cumplen las salvaguardas, dispara el
    reboot del ONU vía Playwright y espera la recuperación.
  · Cuando el WAN vuelve, hace el flush diferido de eventos a Discord.
El dashboard LAN corre en el mismo event loop.
"""
import asyncio
import time
from datetime import datetime

import checks
import config
import state as st
from checks import wan_alive, onu_alive, router_alive, dns_ok
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
            router = await router_alive()
            store.router_up = router
            _skip(store, "WAN caído pero el ONU no responde — no reinicio a ciegas "
                         + ("(puede ser corte de energía o del ISP)." if router else
                            "(el router OpenWRT tampoco responde: el problema está de este lado)."))
            return

        # Salvaguarda 2: tope de reboots exitosos por ventana.
        if not manual and store.reboots_in_window() >= config.MAX_REBOOTS_WINDOW:
            store.last_reboot_ok = True  # frena reintentos por el cooldown largo
            _skip(store, f"Alcanzado el tope de {config.MAX_REBOOTS_WINDOW} reboots por "
                         "ventana. Probablemente sea un corte del ISP; solo monitoreo.")
            return

        # Una sola sesión de navegador en el ONU: si justo se están ajustando los
        # LEDs (segundos), esperar a que terminen en vez de saltear el reboot.
        try:
            await asyncio.wait_for(store.onu_lock.acquire(), timeout=240)
        except asyncio.TimeoutError:
            store.last_reboot_ok = False
            store.add_event(st.REBOOT_FAIL, "La sesión del panel del ONU sigue ocupada; reintento luego.")
            return
        try:
            await _run_reboot(store, reboot_onu, manual)
        finally:
            store.onu_lock.release()


def _skip(store: st.Store, msg: str):
    """Registra un skip, pero una sola vez por corte si el motivo no cambió
    (sin esto, un ONU apagado llenaba el log con un evento cada 2 min)."""
    if msg == store.last_skip_msg:
        return
    store.last_skip_msg = msg
    store.add_event(st.REBOOT_SKIP, msg)


async def _run_reboot(store: st.Store, reboot_onu, manual: bool):
    store.add_event(st.REBOOT_START,
                    "Reiniciando el ONU" + (" [DRY_RUN]" if config.DRY_RUN else "") + "…")
    await notifier.push_ntfy("ISP Uplink Guardian", "Reiniciando el ONU por caída de internet", "default")

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
    if not config.DRY_RUN:
        store.led_dirty = True   # el reboot puede devolver los LEDs a su default

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
                        store.last_skip_msg = None
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


_DIAG_NTFY = {   # diagnóstico → (prioridad ntfy) ; los demás solo quedan en el log
    checks.DIAG_ROUTER_DNS: "default",
    checks.DIAG_DNS_UPSTREAM: "default",
    checks.DIAG_ROUTER_DOWN: "default",
}


async def diag_tick(store: st.Store):
    """Una pasada de diagnóstico: router, DNS del router vs público, y el estado.

    El DNS necesita DNS_FAIL_THRESHOLD fallos seguidos para darse por roto (un
    SERVFAIL suelto no es un incidente). Solo registra/avisa en los cambios.
    """
    if await router_alive():
        store.router_fail_count = 0
        store.router_up = True
    else:
        store.router_fail_count += 1
        if store.router_fail_count >= 2:
            store.router_up = False
    if config.DNS_CHECK_ENABLED:
        r_ok, p_ok = await asyncio.gather(dns_ok(config.ROUTER_HOST), dns_ok(config.DNS_PUBLIC))
        store.dns_public_up = p_ok
        if r_ok:
            store.dns_fail_count = 0
            store.dns_router_up = True
        else:
            store.dns_fail_count += 1
            if store.dns_fail_count >= config.DNS_FAIL_THRESHOLD:
                store.dns_router_up = False
    new = checks.diagnose(store.wan_up, store.onu_up, store.router_up,
                          store.dns_router_up, store.dns_public_up)
    old = store.diagnosis
    if new == old:
        return
    store.diagnosis = new
    if old is None and new == checks.DIAG_OK:
        return   # arranque normal: no hace falta un evento
    text = checks.DIAG_TEXT[new]
    store.add_event(st.DIAG, f"Diagnóstico: {text}" + (f" (antes: {old})" if old else ""))
    if new in _DIAG_NTFY:
        await notifier.push_ntfy("ISP Uplink Guardian · diagnóstico", text, _DIAG_NTFY[new])
    elif old in _DIAG_NTFY and new == checks.DIAG_OK:
        await notifier.push_ntfy("ISP Uplink Guardian · diagnóstico", "Resuelto: " + text, "low")


async def diag_loop(store: st.Store):
    while True:
        try:
            await diag_tick(store)
        except Exception as e:
            store.add_event(st.INFO, f"Excepción en el diagnóstico: {e}")
        await asyncio.sleep(config.CHECK_INTERVAL)


def led_action_needed(store: st.Store, want_on: bool, now: float) -> bool:
    """¿Hay que entrar al panel por los LEDs? Nunca durante un corte (ahí manda el
    reboot) ni antes del backoff tras un fallo (cada intento es un login)."""
    if not config.LED_SCHEDULE_ENABLED:
        return False
    if store.wan_up is not True or store.onu_up is not True:
        return False
    if store.reboot_lock.locked() or now < store.led_next_try_ts:
        return False
    return (store.led_dirty or store.leds_on is None or store.leds_on != want_on
            or store.led_verified_ts is None
            or now - store.led_verified_ts >= config.LED_RECHECK_SECS)


async def led_tick(store: st.Store, apply=None):
    from leds import apply_leds, desired_leds_on
    apply = apply or apply_leds
    want_on = desired_leds_on(datetime.now(st._TZ))
    now = time.time()
    if not led_action_needed(store, want_on, now):
        return
    async with store.onu_lock:
        res = await apply(want_on)
    if res.get("ok"):
        before = store.leds_on
        store.leds_on = res.get("leds_on", want_on)
        store.led_verified_ts = time.time()
        store.led_dirty = False
        if store.leds_on != want_on:   # DRY_RUN o el panel no convergió: no insistir cada minuto
            store.led_next_try_ts = time.time() + config.LED_RETRY_SECS
        if res.get("changed") or (before is not None and before != store.leds_on):
            franja = "horario diurno" if want_on else "horario nocturno"
            store.add_event(st.LED, f"{res.get('detail')} ({franja})")
        elif config.DRY_RUN and "DRY_RUN" in res.get("detail", ""):
            store.add_event(st.LED, res["detail"])
    else:
        store.led_next_try_ts = time.time() + config.LED_RETRY_SECS
        store.add_event(st.LED_FAIL, f"{res.get('detail')} — reintento en "
                                     f"{config.LED_RETRY_SECS // 60} min.")


async def led_loop(store: st.Store):
    await asyncio.sleep(20)   # dejar que el watchdog fije el estado WAN/ONU inicial
    while True:
        try:
            await led_tick(store)
        except Exception as e:
            store.led_next_try_ts = time.time() + config.LED_RETRY_SECS
            store.add_event(st.LED_FAIL, f"Excepción en el control de LEDs: {e}")
        await asyncio.sleep(60)


async def browser_selftest(store: st.Store):
    from reboot_onu import browser_selftest as _selftest
    async with store.onu_lock:
        try:
            ok, detail = await asyncio.wait_for(_selftest(), timeout=120)
        except asyncio.TimeoutError:
            ok, detail = False, "timeout de 120 s lanzando Chromium"
    store.browser_ok, store.browser_detail = ok, detail
    if ok:
        print(f"[selftest] {detail}", flush=True)
    else:
        store.add_event(st.REBOOT_FAIL,
                        f"Autotest: Chromium no arranca ({detail}). El reboot del ONU NO va a funcionar.")
        await notifier.push_ntfy("ISP Uplink Guardian", "Chromium no arranca: revisar la imagen", "high")


async def main():
    store = st.Store()
    store.reboot_lock = asyncio.Lock()
    store.onu_lock = asyncio.Lock()

    async def manual_trigger():
        """Disparo manual del reboot desde el dashboard (respeta DRY_RUN)."""
        store.add_event(st.INFO, "Disparo manual de reboot desde el dashboard.")
        await do_reboot(store, manual=True)

    await start_dashboard(store, manual_trigger)
    if config.BROWSER_SELFTEST:
        await browser_selftest(store)
    await asyncio.gather(watchdog(store), diag_loop(store), led_loop(store))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
