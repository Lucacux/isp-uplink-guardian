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


BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


async def launch_browser(pw):
    return await pw.chromium.launch(headless=True, args=BROWSER_ARGS)


async def new_page(browser):
    ctx = await browser.new_context(ignore_https_errors=True)
    page = await ctx.new_page()
    # Timeouts holgados para fills/clicks/navegación (ONU lento en corte).
    page.set_default_timeout(config.PW_NAV_TIMEOUT_MS)
    page.set_default_navigation_timeout(config.PW_NAV_TIMEOUT_MS)
    return page


async def onu_login(page, shot_dir: str | None, shots: list) -> bool:
    """Login humano al panel (usuario, password, IdentCode leído del DOM).

    Devuelve False si seguimos en el login (credenciales o lockout de 3 intentos).
    Un solo intento por llamada: reintentar a ciegas acerca el lockout.
    shot_dir=None no toma screenshots (operaciones de rutina como los LEDs).
    """
    await _goto_retry(page, config.ONU_URL + "/", "login")
    if shot_dir:
        shots.append(await _shot(page, shot_dir, "1_login"))
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
    # Esperar a que el form de login desaparezca (navegó al panel).
    await page.click("#LoginId")
    try:
        await page.wait_for_selector("#Frm_Password", state="detached",
                                     timeout=config.PW_NAV_TIMEOUT_MS)
    except Exception:
        await page.wait_for_timeout(2500)
    if shot_dir:
        shots.append(await _shot(page, shot_dir, "2_after_login"))
    return await page.locator("#Frm_Password").count() == 0


async def onu_logout(page):
    """Best-effort: el panel admite una sola sesión; no dejarla ocupada."""
    for path in ("/getpage.gch?pid=1001&logout=1", "/logout.gch"):
        try:
            await page.goto(config.ONU_URL + path, wait_until="domcontentloaded",
                            timeout=8000)
        except Exception:
            pass


async def browser_selftest() -> tuple[bool, str]:
    """Lanza Chromium y renderiza una página en blanco. Detecta upgrades rotos."""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await launch_browser(pw)
            try:
                page = await browser.new_page()
                await page.set_content("<p>ok</p>")
                ok = (await page.inner_text("p")) == "ok"
                return ok, f"Chromium {browser.version} OK" if ok else "render inesperado"
            finally:
                await browser.close()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:300]


async def _reboot_mechanism(scope) -> bool:
    """True si el scope (page/frame) tiene el mecanismo de reboot del firmware.

    En este ZTE el botón Reboot (type=button) sólo abre un modal custom cuyo
    confirmCallback hace: setValue('IF_ACTION','devrestart'); setValue('flag','1');
    getObj('fSubmit').submit();  — por eso reproducimos ESO directamente, sin el
    modal (clickear el botón no reinicia; sólo abre el diálogo).
    """
    try:
        return bool(await scope.evaluate(
            "() => (typeof setValue==='function' && typeof getObj==='function' && "
            "!!(document.getElementById('fSubmit')||document.forms['fSubmit']) && "
            "!!document.getElementById('IF_ACTION'))"
        ))
    except Exception:
        return False


async def _submit_reboot(scope):
    """Ejecuta el reboot igual que el confirmCallback del firmware (bypass del modal)."""
    await scope.evaluate(
        "() => { setValue('IF_ACTION','devrestart'); setValue('flag','1'); "
        "getObj('fSubmit').submit(); }"
    )


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
        browser = await launch_browser(pw)
        try:
            page = await new_page(browser)

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

            # 1-4) Login (con reintentos y screenshots)
            if not await onu_login(page, shot_dir, result["shots"]):
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

            # 6) Ubicar el scope (page/frame) con el mecanismo de reboot del firmware.
            scope = page if await _reboot_mechanism(page) else None
            if scope is None:
                for fr in page.frames:
                    if await _reboot_mechanism(fr):
                        scope = fr
                        break
            # El botón sólo se usa como evidencia/confirmación de que estamos bien.
            ctrl = await _find_reboot_control(page)
            if ctrl is None:
                for fr in page.frames:
                    ctrl = await _find_reboot_control(fr)
                    if ctrl is not None:
                        break

            if scope is None and ctrl is None:
                result["detail"] = ("No encontré ni el botón ni el mecanismo de reboot. "
                                    "Revisar screenshot 3_reboot_page.")
                return result

            if config.DRY_RUN:
                result["shots"].append(await _shot(page, shot_dir, "4_dryrun_found_button"))
                result["ok"] = True
                result["detail"] = (
                    "DRY_RUN: página de reboot OK — "
                    + ("mecanismo devrestart presente (fSubmit/IF_ACTION), "
                       if scope else "botón presente, ")
                    + "NO se ejecutó."
                )
                return result

            # Reboot real: reproducir el confirmCallback del firmware (sin modal).
            if scope is not None:
                try:
                    await _submit_reboot(scope)
                except Exception:
                    pass   # la navegación/reboot puede abortar el evaluate: esperado
            elif ctrl is not None:
                # Fallback (no debería hacer falta): click al botón.
                try:
                    await ctrl.click()
                except Exception:
                    pass
            await page.wait_for_timeout(3000)
            try:
                result["shots"].append(await _shot(page, shot_dir, "4_after_reboot"))
            except Exception:
                pass

            # ¿Se disparó el POST de reboot correcto (con IF_ACTION=devrestart)?
            reboot_posts = [c for c in captured if "devrestart" in (c.get("post") or "")]
            result["reboot_request"] = (reboot_posts[-1] if reboot_posts
                                        else (captured[-1] if captured else None))
            result["ok"] = True
            result["detail"] = ("Reboot enviado (IF_ACTION=devrestart confirmado en el POST)."
                                if reboot_posts else
                                "Submit de reboot ejecutado (no se pudo confirmar el POST).")
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
