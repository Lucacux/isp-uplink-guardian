# 🛰️ crappy-ISP

Watchdog casero que **reinicia automáticamente el ONU/EPON** cuando se cae
internet, con trazabilidad que funciona **aunque no haya WAN**.

Pensado para un ONU V-SOL/ZTE (probado en un ZTE **D401**) cuyo panel se cuelga
cada tanto y hay que reiniciarlo a mano. Este bot lo hace solo.

## 🧠 Cómo funciona

1. **Detección liviana**: cada ~30 s hace un *TCP-connect* a `8.8.8.8:53`,
   `1.1.1.1:443`, etc. (más confiable que un ICMP ping a través de NAT y sin
   privilegios raw-socket en Docker). Con **debounce**: exige varios fallos
   seguidos antes de declarar la caída, para no reaccionar a un microcorte.
2. **Reboot robusto vía navegador headless** (Playwright): replica el flujo
   humano en el panel — completa usuario/clave, **lee el IdentCode del DOM** (es
   un "anti-bot" puramente client-side) y lo tipea, entra y aprieta *Reboot*.
   Toma **screenshots de cada paso** (traza visual).
3. **Salvaguardas** para no empeorar las cosas:
   - **Cooldown** entre reboots (default 20 min: el ONU tarda en re-sincronizar).
   - **Tope de reboots por ventana** (default 3 en 6 h): si reiniciar no
     arregla, probablemente sea un corte real del ISP → solo monitorea.
   - **`REQUIRE_ONU_UP`**: si el ONU ni siquiera responde (apagón), no reinicia
     a ciegas.
4. **Trazabilidad sin WAN** (3 capas):
   - **Dashboard web LAN** (`http://<host>:8090`): estado en vivo + historial,
     accesible desde el celu en el WiFi de casa **durante** el corte.
   - **Flush diferido a Discord**: bufferiza los eventos y los manda al webhook
     **cuando vuelve** internet (post-mortem: cayó / reboot / volvió).
   - **ntfy opcional**: push local instantáneo si tenés un server ntfy en la LAN.

## ⚙️ Deploy en Dokploy

App tipo **Compose** apuntando a este repo. El `docker-compose.yml` usa
`network_mode: host` (necesario para alcanzar el ONU en otra subred y para
exponer el dashboard directo en la LAN).

Cargá en **Environment** (Dokploy), como mínimo:

```
ONU_PASS=***            # la clave del ONU (secreto — nunca al repo)
DISCORD_WEBHOOK_URL=***  # opcional
```

El resto tiene defaults sensatos (ver `.env.example`).

> **Requisito de red:** el host debe rutear a `192.168.77.1`. Verificá con
> `ping -c1 192.168.77.1` desde el host antes de desplegar.

## 🧪 Validar sin cortar internet

- **Localizar el login/reboot sin reiniciar**: `DRY_RUN=1` → el bot hace todo el
  flujo (login incluido) y localiza el botón Reboot, pero **no lo aprieta**.
- **Probar el mecanismo de login**: `ONU_PASS=... python3 tools/login_probe.py`
  (no reinicia nada; sólo valida auth).

## 🔒 Seguridad

- La password del ONU va por env (Dokploy/`.env` gitignored). Nunca al repo.
- El panel del ONU es HTTP plano en la LAN; este bot no lo expone al WAN.
- El dashboard es de solo lectura y sin credenciales — mantenelo en LAN.

## 🧰 Stack

Python · asyncio · Playwright (Chromium headless) · aiohttp (dashboard)
