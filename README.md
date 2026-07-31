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

## ⚙️ Deploy on Dokploy

An **Application** (Dockerfile build) pointing at this repo — same pattern as the
other homelab bots. Configuration:

- **Build type:** Dockerfile (`./Dockerfile`).
- **Port:** publish `8099` (container) as `8099` on the host in **host** mode, so
  the dashboard is reachable on the LAN (`http://<host>:8099`) even while the WAN
  is down — no reverse proxy in the path.
- **Environment** (at a minimum):

  ```
  ONU_PASS=***            # the ONU password (secret — never in the repo)
  DISCORD_WEBHOOK_URL=***  # optional
  ```

  Everything else has sensible defaults (see `.env.example`).

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
