#!/bin/sh
# dns-watchdog v2 — OpenWrt (Archer C59). Cron: * * * * * /root/dns-watchdog.sh
#
# Escalera ante "WAN OK pero DNS roto" (proxy DoH zombie / dnsmasq colgado):
#   nivel 0 → 1: tras FAILS_BEFORE_RESTART minutos seguidos, reinicia https-dns-proxy + dnsmasq
#                (lo mismo que hacía la v1, pero con debounce).
#   nivel 1 → 2: si sigue roto FAILS_BEFORE_REBOOT minutos, UN reboot del router.
#   nivel 3:     si tras ese reboot vuelve a romperse, o ya hubo un reboot del watchdog hace
#                menos de REBOOT_MIN_GAP, NO reinicia (evita el loop): avisa y espera a Luca.
# Todo se resetea cuando el DNS vuelve a responder. Si el WAN está caído no es un problema de
# DNS (lo maneja el ISP Guardian con el ONU): no se toca nada.
#
# Anti-loop en dos capas: estado en /tmp (se borra con el reboot) + marca persistente en /etc
# con la hora del último reboot hecho por este script (una escritura en flash por reboot).
#
# Pruebas sin efectos: DNS_WD_DRY=1 (no ejecuta acciones) y DNS_WD_FORCE_FAIL=1 (simula DNS roto).
# STATE/MARK se pueden redirigir con DNS_WD_STATE / DNS_WD_MARK.

FAILS_BEFORE_RESTART=${FAILS_BEFORE_RESTART:-2}
FAILS_BEFORE_REBOOT=${FAILS_BEFORE_REBOOT:-6}
REBOOT_MIN_GAP=${REBOOT_MIN_GAP:-43200}     # 12 h entre reboots del watchdog
MIN_UPTIME_FOR_REBOOT=${MIN_UPTIME_FOR_REBOOT:-600}
STATE=${DNS_WD_STATE:-/tmp/dns-watchdog.state}
MARK=${DNS_WD_MARK:-/etc/dns-watchdog.last-reboot}
NTFY=${DNS_WD_NTFY:-http://192.168.2.40:8095/monitoreo-epon}
DRY=${DNS_WD_DRY:-0}
LOCKDIR=/tmp/dns-watchdog.lock

# Lock: otra corrida en curso (DNS lento). Uno de más de 5 min es de una corrida muerta.
[ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +5 2>/dev/null)" ] && rmdir "$LOCKDIR" 2>/dev/null
mkdir "$LOCKDIR" 2>/dev/null || exit 0
trap 'rmdir "$LOCKDIR"' EXIT

log() { logger -t dns-watchdog "$*"; [ -t 1 ] && echo "$*"; }
notify() { # notify <prioridad 1-5> <mensaje>  (best-effort, ntfy en server-mbp)
	[ -n "$NTFY" ] || return 0
	if [ "$DRY" = 1 ]; then log "[dry] ntfy p$1: $2"; return 0; fi
	uclient-fetch -q -T 5 -O /dev/null --post-data="$2" \
		"$NTFY?title=Router-DNS&priority=$1&tags=globe_with_meridians" 2>/dev/null || true
}
act() { if [ "$DRY" = 1 ]; then log "[dry] $*"; else eval "$@"; fi; }

dns_ok() { # nombre aleatorio = sin caché; NXDOMAIN también cuenta como "responde"
	[ "$DNS_WD_FORCE_FAIL" = 1 ] && return 1
	name="wd$(head -c4 /dev/urandom | hexdump -e '"%08x"').example.com"
	nslookup "$name" 127.0.0.1 >/dev/null 2>&1
}
wan_ok() { ping -c1 -W2 8.8.8.8 >/dev/null 2>&1 || ping -c1 -W2 1.1.1.1 >/dev/null 2>&1; }

fails=0; level=0
[ -f "$STATE" ] && . "$STATE"
save() { printf 'fails=%s\nlevel=%s\n' "$fails" "$level" > "$STATE"; }

if dns_ok; then
	if [ "$level" -gt 0 ]; then
		log "DNS recuperado (nivel $level, $fails min con falla)."
		notify 3 "DNS del router recuperado (nivel $level, ~$fails min con falla)."
	fi
	fails=0; level=0; save; exit 0
fi

if ! wan_ok; then
	# Sin WAN no es un problema de DNS: no reiniciar nada (el Guardian se ocupa del ONU).
	[ "$fails" -gt 0 ] && log "WAN caído: suspendo la escalera de DNS."
	fails=0; [ "$level" -lt 3 ] && level=0; save; exit 0
fi

fails=$((fails + 1))
now=$(date +%s); up=$(cut -d. -f1 /proc/uptime)
last=0; [ -f "$MARK" ] && last=$(cat "$MARK" 2>/dev/null || echo 0)

if [ "$level" -eq 0 ] && [ "$fails" -ge "$FAILS_BEFORE_RESTART" ]; then
	log "WAN OK pero DNS caído hace $fails min: reinicio https-dns-proxy + dnsmasq (nivel 1)."
	act "killall -9 https-dns-proxy 2>/dev/null; sleep 1; /etc/init.d/https-dns-proxy start; /etc/init.d/dnsmasq restart"
	notify 3 "DNS del router caído con WAN OK: reinicié el proxy DoH y dnsmasq."
	level=1; fails=0
elif [ "$level" -eq 1 ] && [ "$fails" -ge "$FAILS_BEFORE_REBOOT" ]; then
	# Gate anti-loop: marca reciente (o reloj atrasado respecto de la marca) = no reiniciar.
	if [ "$last" -gt 0 ] && { [ "$now" -lt "$last" ] || [ $((now - last)) -lt "$REBOOT_MIN_GAP" ]; }; then
		log "DNS sigue caído y ya reinicié el router hace $(( (now - last) / 60 )) min: NO reinicio de nuevo (nivel 3)."
		notify 5 "DNS del router sigue roto tras reiniciar servicios, y ya hubo un reboot automático hace menos de $((REBOOT_MIN_GAP / 3600)) h. No reinicio de nuevo: necesita intervención manual."
		level=3
	elif [ "$up" -lt "$MIN_UPTIME_FOR_REBOOT" ]; then
		log "DNS caído pero el router arrancó hace ${up}s: espero antes de reiniciar."
	else
		log "DNS sigue caído $fails min tras reiniciar servicios: reinicio el router (nivel 2)."
		notify 4 "DNS del router sigue roto tras reiniciar servicios: reinicio el router (único intento automático)."
		level=2; save
		act "echo $now > '$MARK'; sync; sleep 3; reboot"
		exit 0
	fi
elif [ "$level" -eq 2 ]; then
	# Solo se llega en DRY (con reboot real /tmp se borra). Equivale a "ya se intentó".
	log "DNS caído en nivel 2 (sin reboot efectivo): paso a nivel 3."
	level=3
fi
save
