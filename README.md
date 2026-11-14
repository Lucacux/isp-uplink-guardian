# 🛰️ ISP Uplink Guardian

A homemade watchdog that **automatically reboots the ONU/EPON** when the internet
goes down, with traceability that works **even when there's no WAN**.

Built for a V-SOL/ZTE ONU (tested on a ZTE **D401**) whose web panel hangs every
so often and has to be rebooted by hand. This bot does it for you.

## 🧠 How it works

1. **Lightweight detection**: every ~30 s it does a *TCP-connect* to `8.8.8.8:53`,
   `1.1.1.1:443`, etc. (more reliable than an ICMP ping across NAT, and needs no
   raw-socket privileges in Docker). With **debounce**: it requires several
   consecutive failures before declaring an outage, so it won't react to a blip.
2. **Robust reboot via a headless browser** (Playwright): it replays the human
   flow on the panel — fills in username/password, **reads the IdentCode from the
   DOM** (a purely client-side "anti-bot") and types it, logs in and clicks
   *Reboot*. Takes **screenshots of every step** (visual trace).
3. **Safeguards** so it doesn't make things worse:
   - **Cooldown** between reboots (default 20 min: the ONU takes a while to
     re-sync).
   - **Max reboots per window** (default 3 in 6 h): if rebooting doesn't help,
     it's probably a real ISP outage → it just monitors.
   - **`REQUIRE_ONU_UP`**: if the ONU itself doesn't respond (power cut), it
     won't reboot blindly.
4. **Traceability without WAN** (3 layers):
   - **LAN web dashboard** (`http://<host>:8099`): live status + history,
     reachable from your phone on the home Wi-Fi **during** the outage.
   - **Prometheus metrics** (`/metrics`): WAN/ONU state, outage duration,
     cooldown, uptime, and persistent reboot counters.
   - **Deferred Discord flush**: it buffers events and sends them to the webhook
     **when the internet comes back** (post-mortem: down / reboot / recovered).
   - **Optional ntfy**: instant local push if you run an ntfy server on the LAN.
5. **Diagnosis: ONU/ISP vs the router's DNS.** Every check also probes the
   OpenWRT router (`ROUTER_HOST`, TCP :53) and asks both the router's resolver
   and a public one (`DNS_PUBLIC`) for a random, uncacheable name. The
   dashboard, `/metrics` (`isp_diagnosis{state=...}`) and ntfy show one verdict:

   | state | meaning |
   |---|---|
   | `ok` | all good |
   | `isp_down` | no internet, ONU reachable → ONU/PON or ISP (the Guardian reboots the ONU) |
   | `onu_down` | no internet and the ONU doesn't answer (power cut / ONU fully hung) |
   | `router_down` | the OpenWRT router doesn't answer |
   | `router_dns` | internet OK but the router's DNS doesn't resolve |
   | `dns_upstream` | internet OK but no resolver answers (UDP 53 blocked / upstream outage) |

6. **ONU LEDs on a schedule.** LEDs on during the day (so you can read PON/LOS
   at a glance during an outage), off while you sleep (`LED_OFF_START`–
   `LED_OFF_END`, default `23:00`–`08:00`). It uses the panel's own
   *Administration → LED Control* form and re-reads the page to verify. It never
   touches the panel during an outage, re-applies after every ONU reboot (the
   ONU may revert to its default), re-verifies every 6 h, and backs off 1 h
   after a failure (every attempt is a login; the panel locks out after 3).
   It shares one browser session lock with the reboot: a reboot waits for an
   LED change in progress instead of being skipped.

## 🛜 Router DNS self-repair (`router/dns-watchdog.sh`)

The repair runs **on the router**, not in the Guardian: it keeps working when the
Guardian's host is down, and the servers' VLAN doesn't need SSH into the router.
Cron every minute; it only acts when the WAN answers but DNS doesn't:

1. 2 min broken → restarts `https-dns-proxy` + `dnsmasq`.
2. Still broken 6 min later → **one** router reboot.
3. Broken again, or a watchdog reboot already happened < 12 h ago → **no more
   reboots** (no loop): ntfy at max priority, waits for a human.

It resets when DNS answers again. Anti-loop in two layers: state in `/tmp`
(cleared by the reboot) and the last reboot time in `/etc/dns-watchdog.last-reboot`.
Install / update:

```sh
cat router/dns-watchdog.sh | ssh openwrt-c59 'cat > /root/dns-watchdog.sh && chmod 755 /root/dns-watchdog.sh'
# crontab:  * * * * * /root/dns-watchdog.sh
# dry-run:  ssh openwrt-c59 'DNS_WD_DRY=1 DNS_WD_FORCE_FAIL=1 DNS_WD_STATE=/tmp/t DNS_WD_MARK=/tmp/m sh /root/dns-watchdog.sh'
```

## ⚙️ Deploy on Dokploy

An **Application** (Dockerfile build) pointing at this repo — same pattern as the
other homelab bots. Configuration:

- **Build type:** Dockerfile (`./Dockerfile`).
- **Port:** publish `8099` (container) as `8099` on the host in **host** mode, so
  the dashboard is reachable on the LAN (`http://<host>:8099`) even while the WAN
  is down — no reverse proxy in the path.
- **Persistent mount:** create a Dokploy **named volume** called
  `isp-uplink-guardian-data` and mount it at `/app/data`. The Dockerfile's
  `VOLUME` declaration alone creates a new anonymous Swarm volume on every
  deploy, which loses the event history, reboot counter, and trace screenshots.
- **Environment** (at a minimum):

  ```
  ONU_PASS=***            # the ONU password (secret — never in the repo)
  DISCORD_WEBHOOK_URL=***  # optional
  ```

  Everything else has sensible defaults (see `.env.example`).

### Playwright: version policy and upgrades

The Playwright Python package and the base image must always be the **same
version** (otherwise the build passes but Chromium fails at runtime, exactly when
the ONU needs a reboot). `tests/test_playwright_pin.py` enforces it in CI.

- **Base:** `-noble` (Ubuntu 24.04 LTS, Python 3.12), pinned by tag **and digest**.
- **Version:** the latest patch of the minor *before* the newest one — upstream
  has no LTS line, so we take one that has been out for a few weeks.
- **At startup** the Guardian launches Chromium once (`BROWSER_SELFTEST`); a broken
  image shows up on the dashboard (`Chromium: ROTO`), in `isp_browser_ok` and
  in ntfy before an outage needs it. The container `HEALTHCHECK` restarts the
  task if the dashboard stops answering.

Upgrade (every few months):

```sh
V=1.65.2   # example
curl -sI -H 'Accept: application/vnd.oci.image.index.v1+json' \
  https://mcr.microsoft.com/v2/playwright/python/manifests/v$V-noble | grep -i docker-content-digest
# update FROM ...:v$V-noble@sha256:<digest> and playwright==$V, then:
python -m unittest discover -s tests
```

> **Network requirement:** the host must route to `192.168.77.1`. The container
> reaches the ONU through the host's routing (egress NAT). Verify with
> `ping -c1 192.168.77.1` from the host before deploying.

## 🧪 Validate without dropping the internet

- **Locate the login/reboot flow without rebooting**: `DRY_RUN=1` → the bot runs
  the whole flow (login included) and locates the Reboot button, but **doesn't
  click it**.
- **Test the login mechanism**: `ONU_PASS=... python3 tools/login_probe.py`
  (reboots nothing; only validates auth).

## 🔒 Security

- The ONU password is passed via env (Dokploy / gitignored `.env`). Never in the
  repo.
- The ONU panel is plain HTTP on the LAN; this bot does not expose it to the WAN.
- The dashboard, status, and metrics endpoints are read-only and unauthenticated;
  keep them on the LAN. Manual reboot requires `DASHBOARD_TOKEN` when configured.

## 🧰 Stack

Python · asyncio · Playwright (headless Chromium) · aiohttp (dashboard)
