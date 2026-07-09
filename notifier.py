"""Notificaciones.

- Discord (flush diferido): durante el corte no hay WAN, así que los eventos se
  bufferizan en el estado y se envían al webhook recién cuando vuelve internet.
- ntfy (opcional): push local instantáneo. Si el server ntfy está en la LAN,
  llega al celular incluso durante el corte. Best-effort.
"""
import aiohttp

import config
import state as st

_EMOJI = {
    st.WAN_DOWN:     "🔴",
    st.WAN_UP:       "🟢",
    st.REBOOT_START: "🔄",
    st.REBOOT_OK:    "✅",
    st.REBOOT_FAIL:  "❌",
    st.REBOOT_SKIP:  "⏸️",
    st.INFO:         "ℹ️",
}


async def push_ntfy(title: str, msg: str, priority: str = "default"):
    """Push local best-effort. No rompe nada si el server no está."""
    if not (config.NTFY_URL and config.NTFY_TOPIC):
        return
    url = config.NTFY_URL.rstrip("/") + "/" + config.NTFY_TOPIC
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, data=msg.encode("utf-8"),
                         headers={"Title": title, "Priority": priority},
                         timeout=aiohttp.ClientTimeout(total=5))
    except Exception:
        pass  # durante el corte el ntfy remoto no llega; es esperado


async def flush_discord(store: st.Store):
    """Envía al webhook los eventos pendientes. Se llama cuando el WAN está OK."""
    if not config.DISCORD_WEBHOOK_URL:
        return
    pending = store.pending_notifications()
    if not pending:
        return

    lines = []
    for e in pending:
        emoji = _EMOJI.get(e["type"], "•")
        lines.append(f"{emoji} `{e['iso']}` — {e['msg']}")

    # Discord: máx ~2000 chars por mensaje; troceamos si hace falta.
    chunks, buf = [], ""
    for ln in lines:
        if len(buf) + len(ln) + 1 > 1800:
            chunks.append(buf); buf = ""
        buf += ln + "\n"
    if buf:
        chunks.append(buf)

    try:
        async with aiohttp.ClientSession() as s:
            for i, chunk in enumerate(chunks):
                payload = {
                    "embeds": [{
                        "title": "🛰️ crappy-ISP — reporte de conectividad"
                                 + (f" ({i+1}/{len(chunks)})" if len(chunks) > 1 else ""),
                        "description": chunk,
                        "color": 0x3498db,
                    }]
                }
                async with s.post(config.DISCORD_WEBHOOK_URL, json=payload,
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status >= 300:
                        return  # no marcamos como notificado; reintenta luego
        store.mark_notified(pending)
    except Exception:
        pass  # sin conexión estable; se reintenta en el próximo ciclo con WAN
