"""Chequeos de conectividad livianos, vía TCP-connect asíncrono.

Un connect TCP con timeout corto a un puerto conocido (DNS/HTTPS) es una prueba
de vida de WAN más confiable que un ICMP ping (que suele filtrarse en NAT/CGNAT)
y no requiere privilegios de raw-socket dentro del contenedor.
"""
import asyncio
import secrets
import struct

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


# ── Router y DNS (diagnóstico ONU/ISP vs DNS del OpenWRT) ─────────────
async def router_alive() -> bool:
    """True si el router contesta en :53, aunque sea con un RST.

    Un "connection refused" también prueba que el equipo está vivo (dnsmasq
    caído, pero el router responde); solo el timeout significa router caído.
    """
    try:
        fut = asyncio.open_connection(config.ROUTER_HOST, 53)
        _r, writer = await asyncio.wait_for(fut, timeout=config.CHECK_TIMEOUT)
        writer.close()
        return True
    except ConnectionRefusedError:
        return True
    except (asyncio.TimeoutError, OSError):
        return False


def build_dns_query(name: str, qid: int) -> bytes:
    """Consulta DNS tipo A mínima (RFC 1035), recursion desired."""
    header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.split(".")) + b"\x00"
    return header + qname + struct.pack("!HH", 1, 1)


def dns_answered(resp: bytes, qid: int) -> bool:
    """El resolver respondió de verdad: mismo id, es respuesta y rcode NOERROR o
    NXDOMAIN. SERVFAIL/REFUSED (rcode 2/5) cuentan como DNS roto."""
    if len(resp) < 12:
        return False
    rid, flags = struct.unpack("!HH", resp[:4])
    return rid == qid and bool(flags & 0x8000) and (flags & 0x000F) in (0, 3)


class _DnsProto(asyncio.DatagramProtocol):
    def __init__(self, fut):
        self.fut = fut

    def datagram_received(self, data, addr):
        if not self.fut.done():
            self.fut.set_result(data)

    def error_received(self, exc):
        if not self.fut.done():
            self.fut.set_exception(exc)


async def dns_ok(server: str, port: int = 53) -> bool:
    """¿El resolver contesta una consulta que no puede estar en caché?

    Label aleatorio bajo DNS_PROBE_DOMAIN: obliga a dnsmasq a ir upstream (DoH),
    así que prueba la cadena completa y no solo que dnsmasq siga vivo.
    """
    qid = secrets.randbits(16)
    name = f"gd{secrets.token_hex(4)}.{config.DNS_PROBE_DOMAIN}"
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    transport = None
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _DnsProto(fut), remote_addr=(server, port))
        transport.sendto(build_dns_query(name, qid))
        resp = await asyncio.wait_for(fut, timeout=config.DNS_TIMEOUT)
        return dns_answered(resp, qid)
    except (asyncio.TimeoutError, OSError):
        return False
    finally:
        if transport is not None:
            transport.close()


# Diagnóstico: un único estado legible a partir de las sondas.
DIAG_OK           = "ok"
DIAG_ROUTER_DOWN  = "router_down"
DIAG_ONU_DOWN     = "onu_down"
DIAG_ISP_DOWN     = "isp_down"
DIAG_ROUTER_DNS   = "router_dns"
DIAG_DNS_UPSTREAM = "dns_upstream"

DIAG_TEXT = {
    DIAG_OK:           "Todo OK",
    DIAG_ROUTER_DOWN:  "El router OpenWRT no responde",
    DIAG_ONU_DOWN:     "Sin internet y el ONU no responde (¿corte de luz o el ONU colgado del todo?)",
    DIAG_ISP_DOWN:     "Sin internet con el ONU accesible: problema del ONU/PON o del ISP",
    DIAG_ROUTER_DNS:   "Internet OK pero el DNS del OpenWRT no resuelve (lo repara el dns-watchdog del router)",
    DIAG_DNS_UPSTREAM: "Internet OK pero ningún resolver contesta (¿UDP 53 bloqueado o caída de DNS upstream?)",
}


def diagnose(wan_up, onu_up, router_up, dns_router_up, dns_public_up) -> str:
    """Clasifica el estado. `None` = todavía sin dato (no acusa a nadie)."""
    if router_up is False:
        return DIAG_ROUTER_DOWN
    if wan_up is False:
        return DIAG_ONU_DOWN if onu_up is False else DIAG_ISP_DOWN
    if wan_up and dns_router_up is False:
        return DIAG_DNS_UPSTREAM if dns_public_up is False else DIAG_ROUTER_DNS
    return DIAG_OK
