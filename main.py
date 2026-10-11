"""Listen to AISStream, detect zone entries for watched vessels, send alerts.

    python main.py                 # run live
    python main.py --dry-run       # print alerts instead of sending

vessels.csv, contacts.yaml and zones.yaml are re-read automatically when they
change (for example after an edit in the web UI or the Telegram bot), no
restart needed.
"""
import argparse
import asyncio
import json
import os
import time

import websockets

from health import Health, ping
from notifier import Notifier
from settings import expand_env, load_yaml
from telegram_bot import Bot
from tracker import Tracker, Watchlist, apply_zones, read_zones

URL = "wss://stream.aisstream.io/v0/stream"


def build_sub(cfg, watch):
    sub = {
        "APIKey": cfg["aisstream_api_key"],
        "BoundingBoxes": [cfg["bounding_box"]],
        "FilterMessageTypes": ["PositionReport"],
    }
    # Default: take every ship in the area box and filter here. The feed stays busy,
    # so silence really means a problem. Optionally let AISStream filter instead
    # (max 50 MMSIs, and every vessel needs one).
    mm = watch.mmsi_list()
    if cfg.get("use_mmsi_filter") is True and mm and len(mm) <= 50 and watch.all_have_mmsi():
        sub["FiltersShipMMSI"] = sorted(mm)
    return sub


async def file_watcher(cfg, watch, notifier, tracker, state):
    """Pick up edits to the vessel list, contacts and zones while running."""
    cpath = cfg.get("contacts_file", "contacts.yaml")
    zpath = cfg.get("zones_file")
    interval = float(cfg.get("reload_interval_s") or 5)
    cmtime = os.stat(cpath).st_mtime_ns
    zmtime = os.stat(zpath).st_mtime_ns if zpath else None
    while True:
        await asyncio.sleep(interval)
        try:
            changed = False
            if watch.maybe_reload():
                print(f"[info] vessel list changed, reloaded ({watch.count()} vessels)")
                changed = True
            if zpath:
                m = os.stat(zpath).st_mtime_ns
                if m != zmtime:
                    zmtime = m
                    zones, bbox = read_zones(zpath)
                    tracker.set_zones(zones)
                    cfg["zones"], cfg["bounding_box"] = zones, bbox
                    print("[info] zones reloaded")
                    changed = True
            if changed and state["ws"] is not None and build_sub(cfg, watch) != state["sub"]:
                state["intentional"] = True
                await state["ws"].close()  # the feed loop reconnects with the new subscription
            m = os.stat(cpath).st_mtime_ns
            if m != cmtime:
                cmtime = m
                notifier.contacts = load_yaml(cpath) or {}
                print("[info] contacts reloaded")
        except Exception as e:
            print(f"[warn] reload failed: {e}")


async def monitor(cfg, health, notifier, watch):
    """Raise and clear health problems, write the status file, ping the external watchdog."""
    interval = float(cfg.get("health_interval_s") or 15)
    path = cfg.get("health_file") or "data/health.json"
    probe_every = float(cfg.get("probe_minutes") or 5) * 60
    hb = cfg.get("heartbeat") or {}
    hb_url = (hb.get("url") or "").strip()
    hb_every = float(hb.get("interval_minutes") or 5) * 60
    last_probe = last_hb = 0.0
    raised = {}
    health.write_status(path, health.snapshot(vessels=watch.count()))
    await asyncio.to_thread(
        notifier.send_admin, "Tracker started", f"The vessel tracker started and is watching {watch.count()} vessel(s)."
    )
    while True:
        await asyncio.sleep(interval)
        try:
            if not cfg.get("dry_run") and time.time() - last_probe >= probe_every:
                last_probe = time.time()
                await asyncio.to_thread(notifier.probe_all)
            probs = health.problems()
            for key, (title, text) in probs.items():
                if key not in raised:
                    raised[key] = title
                    exclude = (key.split(":", 1)[1],) if key.startswith("send:") else ()
                    await asyncio.to_thread(notifier.send_admin, f"PROBLEM: {title}", text, exclude)
            for key in [k for k in raised if k not in probs]:
                title = raised.pop(key)
                await asyncio.to_thread(notifier.send_admin, f"Recovered: {title}", f"{title} has cleared.")
            health.write_status(path, health.snapshot(probs, watch.count()))
            if hb_url and not probs and not cfg.get("dry_run") and time.time() - last_hb >= hb_every:
                last_hb = time.time()
                await asyncio.to_thread(ping, hb_url)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[warn] health check failed: {type(e).__name__}: {e}")


async def feed(cfg, tracker, notifier, watch, health, state):
    backoff = 2
    while True:
        sub = build_sub(cfg, watch)
        state["sub"] = sub
        got_data = False
        reason = "closed by server"
        try:
            async with websockets.connect(URL, ping_interval=20) as ws:
                state["ws"] = ws
                await ws.send(json.dumps(sub))
                health.on_connect()
                print("[info] connected to AISStream")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if "error" in msg:
                        print("[error]", msg["error"])
                        health.on_feed_error(msg["error"])
                        continue
                    if msg.get("MessageType") != "PositionReport":
                        continue
                    health.on_message()
                    if not got_data:
                        got_data = True
                        backoff = 2
                    try:
                        md = msg["MetaData"]
                        pr = msg["Message"]["PositionReport"]
                        events = tracker.process(
                            md["MMSI"],
                            md.get("ShipName", ""),
                            md["latitude"],
                            md["longitude"],
                            sog=pr.get("Sog"),
                            cog=pr.get("Cog"),
                            heading=pr.get("TrueHeading"),
                        )
                    except Exception as e:
                        print(f"[warn] skipped a bad position report: {type(e).__name__}: {e}")
                        continue
                    for ev in events:
                        health.on_alert(ev)
                        await asyncio.to_thread(notifier.send, ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
        state["ws"] = None
        health.on_disconnect()
        if state.pop("intentional", False):
            await asyncio.sleep(1)
        else:
            print(f"[warn] feed dropped ({reason}); retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)  # reset once real data arrives


async def run(cfg, tracker, notifier, watch, health):
    state = {"ws": None, "sub": {}}
    tasks = [
        asyncio.create_task(file_watcher(cfg, watch, notifier, tracker, state)),
        asyncio.create_task(monitor(cfg, health, notifier, watch)),
    ]
    if (cfg.get("telegram") or {}).get("bot_token") and not cfg.get("dry_run"):
        tasks.append(asyncio.create_task(Bot(cfg, health=health).run()))
    try:
        await feed(cfg, tracker, notifier, watch, health, state)
    finally:
        for t in tasks:
            t.cancel()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cfg = expand_env(load_yaml(a.config))
    if a.dry_run:
        cfg["dry_run"] = True
    apply_zones(cfg)
    contacts = load_yaml(cfg.get("contacts_file", "contacts.yaml")) or {}
    watch = Watchlist(cfg.get("watchlist_file", "vessels.csv"))
    tracker = Tracker(cfg, watch, cfg.get("state_db", "state.db"))
    notifier = Notifier(cfg, contacts)
    health = Health(cfg)
    notifier.on_result = health.on_send_result
    print(f"[info] watching {watch.count()} vessels")
    asyncio.run(run(cfg, tracker, notifier, watch, health))


if __name__ == "__main__":
    main()
