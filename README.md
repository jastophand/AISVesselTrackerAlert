# Vessel alerts

Watches AIS positions (AISStream.io) and notifies the assigned port captain(s) and boarding agent(s) when a vessel heading inbound reaches the pilot station or enters the port limits.

All settings are environment variables (see `.env.example`). Vessels, people and zones are managed in the web UI and stored in the `vessel_data` volume.

## Run with Portainer

You need an AISStream key (free at aisstream.io) and an SMTP account for email. Pick one way to get the image:

**A. From a Git repository (easiest to update)**
1. Push this folder to a Git repo (a private one is fine).
2. Portainer -> Stacks -> Add stack -> **Repository**. Enter the repo URL, branch, and `docker-compose.yml` as the compose path. Add credentials if the repo is private.
3. Under **Environment variables** add the values from `.env.example` (at minimum `AISSTREAM_API_KEY` and `ADMIN_PASSWORD`, plus the `SMTP_*` settings).
4. Deploy the stack. Portainer builds the image on the host. To update later, use **Pull and redeploy**.

**B. Build the image on the Docker host yourself**
1. Copy this folder to the server and run `docker build -t vessel-alerts:latest .`
2. Portainer -> Stacks -> Add stack -> **Web editor**. Paste `docker-compose.yml` and delete the two `build: .` lines.
3. Add the environment variables as above and deploy.

Either way you end up with two containers, `tracker` (the AIS listener) and `web` (the admin page), sharing one volume. Check progress under Containers -> `tracker` -> Logs; you should see `connected to AISStream`.

Then open `http://<server>:18473` (change it with `WEB_PORT`), log in with `ADMIN_USER` / `ADMIN_PASSWORD`, and:
1. **Zones**: replace the placeholder pilot station, port limits, inbound reference points and AIS area box with real coordinates.
2. **People**: add port captains and boarding agents (email, ntfy, Telegram). Tick "every alert" for anyone who should get everything.
3. **Vessels**: add vessels (MMSI is best) and assign their port captain and boarding agent.

The web login is basic auth over plain HTTP. Keep it on your LAN or VPN, or put it behind an HTTPS reverse proxy (Nginx Proxy Manager, Traefik, Caddy).

## Run with plain Docker
`cp .env.example .env`, fill it in, then `docker compose up -d --build`.

## Without Docker
`pip install -r requirements.txt`, copy `defaults/` to `data/`, export the variables from `.env.example`, then `python main.py` and `uvicorn web:app --port 8080`. Tests (no network or extra packages needed): `python test_offline.py` (geofence and routing), `python test_health_bot.py` (health checks, Telegram bot), `python test_feed.py` (feed loop, takes about 12 s), `python test_web_logic.py` (web page logic).

## Telegram and ntfy

Both work side by side. On the People page tick Telegram, ntfy, email, or any mix for each person; they get each alert on every channel ticked.

**ntfy:** install the ntfy app, subscribe to a topic, and enter that topic on the person's page. On the public ntfy.sh server anyone who knows a topic name can read it and post to it, so use a long random name (the form suggests one) or run your own server with `NTFY_SERVER` and `NTFY_TOKEN`.

**Telegram bot (alerts plus two-way chat):**
1. In Telegram, message @BotFather, send `/newbot`, and follow the prompts. Put the token it gives you in `TELEGRAM_BOT_TOKEN` and redeploy.
2. Each person opens the bot and sends `/start`. It replies with their chat id.
3. On the People page, paste that chat id, tick Telegram, and tick "Let this person add and remove vessels from Telegram" for anyone who should.

Commands (the bot only answers private chats from people on the People page; strangers get a reply to /start, /help and /id only):

| Command | What it does |
|---|---|
| `/mine` | Vessels assigned to you |
| `/list` | Every vessel being watched, with who is assigned (★ = yours) |
| `/status` | Feed health, vessel count, last alert |
| `/add 366123456 PACIFIC STAR` | Watch a vessel (name optional). Adds you to it as port captain or boarding agent, based on your role |
| `/add PACIFIC STAR` | Watch by name only (less reliable than the MMSI) |
| `/remove 366123456` | Take yourself off the vessel. It leaves the list when nobody is assigned |
| `/id` | Show this chat's id |

People with the role "Other" who can manage vessels add vessels with nobody assigned (assign people on the web page) and `/remove` deletes a vessel for everyone. Changes take effect within a few seconds. The bot polls Telegram, so nothing needs to be reachable from the internet. Run only one tracker per bot token.

## Is it alive? (health monitoring)

The tracker checks itself every 15 seconds and messages everyone with "system health alerts" ticked on the People page, on all their channels:

- **AIS feed error**: AISStream rejected the connection (for example a bad API key).
- **AIS feed disconnected** for more than `DISCONNECT_ALERT_MINUTES` (5).
- **AIS feed silent**: connected but no position reports for `FEED_SILENCE_MINUTES` (10). Raise it if your area box is quiet.
- **Delivery failing**: email, ntfy or Telegram fails 3 times in a row. Reachability is tested every 5 minutes (SMTP login, Telegram `getMe`, ntfy server), not just when an alert needs to go out. The message goes out on the other channels.

You also get "Recovered" messages and a "Tracker started" message (a restart loop shows up quickly).

The tracker cannot report its own death, so for that:
- **Heartbeat:** set `HEARTBEAT_URL` to a ping URL from a service such as Healthchecks.io or an Uptime Kuma push monitor. The tracker pings it every `HEARTBEAT_MINUTES` while everything is healthy, and the service alerts you when pings stop (host down, container dead, or the tracker unhealthy).
- **Docker healthcheck:** the tracker container shows as unhealthy in Portainer when it stops reporting or has a problem.
- **Status page:** the **Status** page in the web UI shows the same information and refreshes itself.

## Notes
- Changes made in the web UI (or by editing the files in the volume) are picked up by the tracker within a few seconds, no restart. Changing environment variables does need a redeploy.
- Back up vessels from the Vessels page (Download CSV). The volume also holds `contacts.yaml`, `zones.yaml` and `state.db` (so restarts don't re-send alerts).
- Inbound only: each zone has an `inbound` block (set on the Zones page): a reference point at the harbor entrance plus a tolerance. A vessel only triggers if its course points toward that point. Outbound ships passing the pilot station are ignored, and a vessel too slow to have a reliable course (under `MIN_SPEED_KNOTS`) is re-checked on its next report.
- The tracker takes every ship in the area box from AISStream and filters locally (`use_mmsi_filter: false`). That keeps the feed busy, so silence reliably means a problem, and vessels added by name work too. Use the MMSI where you can, it is the most reliable match.
- A vessel already inside a zone when the tracker starts stays quiet.
- Channels: email, ntfy (push app), Telegram. Signal can be added through signal-cli-rest-api as one more method in `notifier.py`.
