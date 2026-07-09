"""Chequeos de conectividad livianos, vía TCP-connect asíncrono.

Un connect TCP con timeout corto a un puerto conocido (DNS/HTTPS) es una prueba
de vida de WAN más confiable que un ICMP ping (que suele filtrarse en NAT/CGNAT)
y no requiere privilegios de raw-socket dentro del contenedor.
"""
import asyncio

import config


async def _tcp_ok(host: str, port: int, timeout: float) -> bool:
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (asyncio.TimeoutError, OSError):
        return False


async def wan_alive() -> bool:
    """True si CUALQUIER target WAN responde (basta uno para 'hay internet')."""
    tasks = []
    for tgt in config.WAN_TARGETS:
        if ":" in tgt:
            host, port = tgt.rsplit(":", 1)
            port = int(port)
        else:
            host, port = tgt, 443
        tasks.append(_tcp_ok(host, port, config.CHECK_TIMEOUT))
    if not tasks:
        return False
    # En cuanto uno responda OK, cortamos (menos carga de red).
    for coro in asyncio.as_completed(tasks):
        if await coro:
            return True
    return False


async def onu_alive() -> bool:
    """True si el ONU responde en su puerto web (distingue cuelgue de corte total)."""
    return await _tcp_ok(config.ONU_HOST, config.ONU_PORT, config.CHECK_TIMEOUT)
