#!/usr/bin/env python3
"""
login_probe.py — Valida el login del ONU V-SOL/ZTE y localiza el endpoint de
reboot, SIN reiniciar nada.

Uso (corré esto en un host que alcance el ONU, ej. la Kali):

    ONU_PASS='tu_password' python3 tools/login_probe.py

Opcionales (con defaults):
    ONU_URL   (default http://192.168.77.1)
    ONU_USER  (default user)

- La password se lee SOLO de la env var ONU_PASS. Nunca se imprime ni se guarda.
- NO envía reboot: solo hace login, reporta si funcionó, y busca el formulario
  de reboot para que sepamos el endpoint exacto.
- Al terminar intenta desloguear para no dejar la sesión colgada.
"""
import os
import re
import sys
import getpass
from urllib.parse import urljoin

try:
    import requests
except ImportError:
    sys.exit("Falta 'requests'. Instalá:  pip install requests")

ONU_URL  = os.getenv("ONU_URL", "http://192.168.77.1").rstrip("/") + "/"
ONU_USER = os.getenv("ONU_USER", "user")
ONU_PASS = os.getenv("ONU_PASS")

if not ONU_PASS:
    # Fallback interactivo (no echo). Si preferís, exportá ONU_PASS.
    try:
        ONU_PASS = getpass.getpass("ONU password (no se muestra): ")
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nSin password. Abortado.")
if not ONU_PASS:
    sys.exit("Password vacía. Abortado.")


def looks_like_login(html: str) -> bool:
    """La página de login trae el form fLogin / 'Please login'."""
    h = html.lower()
    return ("frm_password" in h) or ("please login" in h) or ('name="flogin"' in h)


def main() -> int:
    s = requests.Session()
    s.headers["User-Agent"] = "crappy-ISP-probe/1.0"

    # 1) GET del login (cookies iniciales + confirmar que es la página esperada)
    try:
        r = s.get(ONU_URL, timeout=8)
    except requests.RequestException as e:
        print(f"❌ No se pudo alcanzar {ONU_URL}: {e}")
        print("   ¿Este host rutea a la IP del ONU? Probá:  ping -c1 192.168.77.1")
        return 2

    print(f"→ GET {ONU_URL}  [HTTP {r.status_code}]  server='{r.headers.get('Server','?')}'")
    if not looks_like_login(r.text):
        print("⚠️  La página inicial no parece el login esperado. Igual sigo con el POST.")

    # 2) POST de login — replica exactamente los campos del form fLogin.
    #    El IdentCode se valida SOLO en JS del navegador; el server lo ignora,
    #    así que mandamos un valor cualquiera. Frm_Logintoken='1' como el JS.
    payload = {
        "action": "login",
        "Username": ONU_USER,
        "Password": ONU_PASS,
        "Frm_Logintoken": "1",
        "IdentCode": "ABCD",
        "frashnum": "",
    }
    try:
        r2 = s.post(ONU_URL, data=payload, timeout=8, allow_redirects=True)
    except requests.RequestException as e:
        print(f"❌ Falló el POST de login: {e}")
        return 2

    print(f"→ POST login  [HTTP {r2.status_code}]  url_final={r2.url}")
    cookies = "; ".join(f"{c.name}" for c in s.cookies)  # nombres, no valores
    print(f"   cookies de sesión: {cookies or '(ninguna)'}")

    # 3) Verificar éxito: pedir el landing post-login y ver que NO sea el login.
    landing_candidates = ["start.ghtml", "", "getpage.gch?pid=1001"]
    success = False
    landing_html = ""
    landing_url = ""
    for cand in landing_candidates:
        url = urljoin(ONU_URL, cand)
        try:
            rc = s.get(url, timeout=8)
        except requests.RequestException:
            continue
        if rc.status_code == 200 and not looks_like_login(rc.text):
            success = True
            landing_html = rc.text
            landing_url = url
            break

    if not success:
        print("\n❌ LOGIN FALLÓ (seguimos viendo la página de login).")
        print("   Revisá usuario/password. Si el server bloqueó por 3 intentos,")
        print("   esperá ~1 min. El campo IdentCode NO es la causa (se ignora).")
        return 1

    print(f"\n✅ LOGIN OK — landing: {landing_url}")

    # 4) Buscar el endpoint de reboot SIN ejecutarlo.
    #    Escaneamos el landing y (si hace falta) el menú por referencias a reboot.
    print("\n🔎 Buscando el endpoint de reboot (sin dispararlo)...")
    haystack = {landing_url: landing_html}

    # Sumar páginas de menú/admin típicas de ZTE para localizar el form de reboot.
    for cand in [
        "getpage.gch?pid=1002", "manage_reboot.gch", "admin_reboot.gch",
        "manage_diagnose_t.gch", "template.gch?pid=1002",
    ]:
        url = urljoin(ONU_URL, cand)
        try:
            rc = s.get(url, timeout=8)
            if rc.status_code == 200:
                haystack[url] = rc.text
        except requests.RequestException:
            pass

    hits = []
    pat_form   = re.compile(r"<form[^>]*action=[\"']([^\"']+)[\"']", re.I)
    pat_reboot = re.compile(r"(reboot|reset|restart)", re.I)
    pat_gch    = re.compile(r"[\w./?=&-]+\.(?:gch|cmd|ghtml)", re.I)
    for url, html in haystack.items():
        if not pat_reboot.search(html):
            continue
        forms = pat_form.findall(html)
        gchs  = set(pat_gch.findall(html))
        near  = [g for g in gchs if "reboot" in g.lower() or "manage" in g.lower()
                 or "sys" in g.lower()]
        if forms or near:
            hits.append((url, forms, sorted(near)[:8]))

    if hits:
        print("   Candidatos (revisamos juntos cuál es el POST de reboot):")
        for url, forms, near in hits:
            print(f"   · página {url}")
            if forms:
                print(f"       form action(s): {forms}")
            if near:
                print(f"       endpoints cercanos: {near}")
    else:
        print("   No lo encontré por heurística (el menú usa frames/JS).")
        print("   No pasa nada: lo sacamos del DevTools/HTML de la pág. de reboot.")

    # 5) Logout para no dejar la sesión ocupada.
    for lo in ["logout.gch", "getpage.gch?pid=1001&logout=1", "manage_reboot.gch?logout=1"]:
        try:
            s.get(urljoin(ONU_URL, lo), timeout=5)
        except requests.RequestException:
            pass
    print("\n(hecho — sesión cerrada, no se reinició nada)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
