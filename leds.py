"""LEDs del ONU por horario (ZTE D401: Administration → LED Control).

La página `pon_manager_led_control_t.gch` tiene un checkbox "Turn Off Leds"
(`Frm_LedControl`) y el hidden `LedControl` (1 = apagados, 0 = encendidos). Su
pageSubmit() copia el checkbox al hidden, pone IF_ACTION=apply y envía fSubmit.
Usamos ESE submit del firmware (como haría una persona) y después releemos la
página para verificar que el cambio quedó aplicado.

Encendidos de día: durante un corte se ve a ojo si el PON/LOS está mal (ONU) o
si está todo verde (entonces mirar el DNS del router). Apagados de noche.
"""
import asyncio
from datetime import datetime, time as dtime

import config
from reboot_onu import launch_browser, new_page, onu_login, onu_logout, _goto_retry


def _parse_hhmm(s: str) -> dtime:
    h, m = s.strip().split(":")
    return dtime(int(h), int(m))


def in_off_window(now: datetime, start: str | None = None, end: str | None = None) -> bool:
    """¿`now` cae en la franja de LEDs apagados? Soporta cruzar la medianoche."""
    a = _parse_hhmm(start or config.LED_OFF_START)
    b = _parse_hhmm(end or config.LED_OFF_END)
    t = now.time().replace(second=0, microsecond=0)
    if a == b:
        return False
    if a < b:
        return a <= t < b
    return t >= a or t < b


def desired_leds_on(now: datetime) -> bool:
    return not in_off_window(now)


_READ_JS = """() => {
  const h = document.querySelector('input#LedControl, input[name="LedControl"]');
  const c = document.getElementById('Frm_LedControl');
  return { hidden: h ? h.value : null, checked: c ? c.checked : null,
           submit: typeof pageSubmit === 'function' };
}"""


def leds_on_from_page(info: dict) -> bool | None:
    """Interpreta la lectura de la página. None = no es la página esperada."""
    if info.get("hidden") in ("0", "1"):
        return info["hidden"] == "0"
    if info.get("checked") is not None:
        return not info["checked"]
    return None


async def _read(page) -> tuple[bool | None, dict]:
    await _goto_retry(page, f"{config.ONU_URL}/getpage.gch?pid=1002&nextpage={config.LED_PAGE}",
                      "led-page")
    await page.wait_for_timeout(800)
    info = await page.evaluate(_READ_JS)
    return leds_on_from_page(info), info


async def apply_leds(want_on: bool, dry_run: bool | None = None) -> dict:
    """Lleva los LEDs al estado pedido. Devuelve ok, changed, leds_on, detail."""
    from playwright.async_api import async_playwright

    dry_run = config.DRY_RUN if dry_run is None else dry_run
    res = {"ok": False, "changed": False, "leds_on": None, "detail": ""}
    if not config.ONU_PASS:
        res["detail"] = "ONU_PASS no configurada."
        return res

    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        page = None
        try:
            page = await new_page(browser)
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))
            if not await onu_login(page, None, []):
                res["detail"] = "El login no avanzó (credenciales o lockout)."
                return res

            current, info = await _read(page)
            if current is None:
                res["detail"] = f"No reconozco la página de LEDs ({info})."
                return res
            res["leds_on"] = current
            if current == want_on:
                res["ok"] = True
                res["detail"] = "Ya estaban " + ("encendidos." if want_on else "apagados.")
                return res
            if dry_run:
                res["ok"] = True
                res["detail"] = ("DRY_RUN: los " + ("encendería" if want_on else "apagaría")
                                 + "; no se envió nada.")
                return res

            # Checkbox "Turn Off Leds" = marcado para apagar. Submit del propio firmware.
            await page.set_checked("#Frm_LedControl", not want_on)
            if info.get("submit"):
                try:
                    await page.evaluate("() => pageSubmit()")
                except Exception:
                    pass   # el submit navega y puede abortar el evaluate: esperado
            else:
                await page.click("#Btn_Submit")
            try:
                await page.wait_for_load_state("domcontentloaded",
                                               timeout=config.PW_NAV_TIMEOUT_MS)
            except Exception:
                pass
            await page.wait_for_timeout(1500)

            after, info2 = await _read(page)      # verificación: releer de cero
            res["leds_on"] = after
            res["changed"] = after == want_on
            res["ok"] = after == want_on
            res["detail"] = ("LEDs " + ("encendidos." if want_on else "apagados.")
                             if res["ok"] else f"El cambio no quedó aplicado ({info2}).")
            return res
        except Exception as e:
            res["detail"] = f"Excepción controlando LEDs: {e}"[:300]
            return res
        finally:
            if page is not None:
                await onu_logout(page)
            await browser.close()
