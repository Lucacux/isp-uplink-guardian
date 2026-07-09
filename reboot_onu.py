"""Acción de reboot del ONU V-SOL/ZTE vía navegador headless (Playwright).

Replica el flujo humano exacto que sabemos que funciona (por eso es robusto
ante las rarezas de token/sesión del firmware):

  1. Abre el login.
  2. Completa usuario/contraseña.
  3. Lee el IdentCode del DOM (es client-side) y lo tipea — así pasa el
     chequeo JS del propio panel, igual que una persona.
  4. Login.
  5. Va a Administration → System Management.
  6. Click en Reboot y acepta el confirm.

Toma screenshots de cada paso (traza visual) y captura el POST de reboot en la
red (para poder migrar a una versión HTTP liviana más adelante si se quiere).
Con DRY_RUN=1 hace todo menos el click final.
"""
import os
import time
import asyncio

import config


async def _shot(page, shot_dir: str, name: str) -> str | None:
    try:
        os.makedirs(shot_dir, exist_ok=True)
        path = os.path.join(shot_dir, f"{int(time.time())}_{name}.png")
        await page.screenshot(path=path, full_page=True)
        return path
    except Exception:
        return None


async def _goto_retry(page, url: str, label: str):
    """page.goto con reintentos y timeout holgado.

    Durante un corte el panel del ONU suele responder lento o colgado; reintentar
    da chance de que el reboot igual se ejecute (que es justo cuando más importa).
    """
    last = None
    for i in range(config.PW_NAV_RETRIES):
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=config.PW_NAV_TIMEOUT_MS)
            return
        except Exception as e:
            last = e
            print(f"[reboot] goto {label} intento {i+1}/{config.PW_NAV_RETRIES} "
                  f"falló: {e}", flush=True)
            if i < config.PW_NAV_RETRIES - 1:
                await page.wait_for_timeout(config.PW_RETRY_GAP * 1000)
    raise RuntimeError(f"goto {label} falló tras {config.PW_NAV_RETRIES} intentos: {last}")


async def _find_reboot_control(scope):
    """Busca el control de Reboot en un page o frame. Devuelve un locator o None."""
    selectors = [
        'input[value="Reboot"]',
        'input#Btn_restart',
        'input[name="Btn_restart"]',
        'input[onclick*="reboot" i]',
        'input[onclick*="restart" i]',
        'button:has-text("Reboot")',
    ]
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            if await loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


async def reboot_onu(shot_dir: str) -> dict:
    """Ejecuta el reboot. Devuelve dict: ok, detail, shots[], reboot_request."""
    from playwright.async_api import async_playwright  # import perezoso

    result = {"ok": False, "detail": "", "shots": [], "reboot_request": None,
              "dry_run": config.DRY_RUN}
    captured = []

    if not config.ONU_PASS:
        result["detail"] = "ONU_PASS no configurada."
        return result

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            ctx = await browser.new_context(ignore_https_errors=True)
            page = await ctx.new_page()
            # Timeouts holgados para fills/clicks/navegación (ONU lento en corte).
            page.set_default_timeout(config.PW_NAV_TIMEOUT_MS)
            page.set_default_navigation_timeout(config.PW_NAV_TIMEOUT_MS)

            # Auto-aceptar cualquier confirm() del panel ("¿reiniciar?").
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))
            # Capturar el POST de reboot para referencia futura.
            def _on_request(req):
                if req.method == "POST" and config.ONU_HOST in req.url:
                    try:
                        captured.append({"url": req.url, "post": req.post_data})
                    except Exception:
                        captured.append({"url": req.url, "post": None})
            page.on("request", _on_request)

            # 1) Login page (con reintentos)
            await _goto_retry(page, config.ONU_URL + "/", "login")
            result["shots"].append(await _shot(page, shot_dir, "1_login"))

            # 2-3) Credenciales + IdentCode (leído del DOM)
            await page.fill("#Frm_Username", config.ONU_USER)
            await page.fill("#Frm_Password", config.ONU_PASS)
            try:
                code = await page.evaluate(
                    "() => { var e=document.getElementById('checkCode'); return e ? e.value : ''; }"
                )
                if code:
                    await page.fill("#Frm_IdentCode", code)
            except Exception:
                pass

            # 4) Login — esperar a que el form de login desaparezca (navegó al panel).
            await page.click("#LoginId")
            try:
                await page.wait_for_selector("#Frm_Password", state="detached",
                                             timeout=config.PW_NAV_TIMEOUT_MS)
            except Exception:
                await page.wait_for_timeout(2500)
            result["shots"].append(await _shot(page, shot_dir, "2_after_login"))

            # ¿Seguimos en el login? => credenciales/lockout
            if await page.locator("#Frm_Password").count() > 0:
                result["detail"] = "El login no avanzó (credenciales o lockout de 3 intentos)."
                return result

            # 5) Ir a la página de reboot (System Management), con reintentos.
            await _goto_retry(
                page,
                config.ONU_URL + "/getpage.gch?pid=1002&nextpage=manager_dev_conf_t.gch",
                "reboot-page",
            )
            await page.wait_for_timeout(1200)
            result["shots"].append(await _shot(page, shot_dir, "3_reboot_page"))

            # 6) Localizar el botón Reboot (en el page o en algún frame).
            ctrl = await _find_reboot_control(page)
            if ctrl is None:
                for fr in page.frames:
                    ctrl = await _find_reboot_control(fr)
                    if ctrl is not None:
                        break

            if ctrl is None:
                result["detail"] = ("No encontré el botón Reboot en la página. "
                                    "Revisar screenshot 3_reboot_page para ajustar el selector.")
                return result

            if config.DRY_RUN:
                try:
                    await ctrl.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                result["shots"].append(await _shot(page, shot_dir, "4_dryrun_found_button"))
                result["ok"] = True
                result["detail"] = "DRY_RUN: botón Reboot localizado, NO se hizo click."
                return result

            # Click real → reboot.
            await ctrl.click()
            await page.wait_for_timeout(3000)
            result["shots"].append(await _shot(page, shot_dir, "4_after_reboot_click"))

            result["reboot_request"] = captured[-1] if captured else None
            result["ok"] = True
            result["detail"] = "Reboot enviado al ONU."
            return result

        except Exception as e:
            try:
                result["shots"].append(await _shot(page, shot_dir, "error"))
            except Exception:
                pass
            result["detail"] = f"Excepción durante el reboot: {e}"
            return result
        finally:
            result["shots"] = [s for s in result["shots"] if s]
            await browser.close()
