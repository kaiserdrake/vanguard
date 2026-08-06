import base64
import json
import os
import time

import paho.mqtt.client as mqtt
import requests
import yaml

MQTT_HOST = os.environ["MQTT_HOST"]
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
FRIGATE_URL = os.environ["FRIGATE_URL"]
FRIGATE_UI_URL = os.environ.get("FRIGATE_UI_URL", FRIGATE_URL)
# Required, never defaulted. On a public ntfy server the topic name is the only
# thing standing between the feed and anyone who knows it, so a baked-in default
# would publish a live topic to every reader of this repo and quietly become the
# shared topic for every deployment that forgot to override it.
NTFY_URL = os.environ["NTFY_URL"].rstrip("/")
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
NTFY_AUTH_TOKEN = os.environ.get("NTFY_AUTH_TOKEN", "")
NOTIFY_LABELS = {
    label.strip() for label in os.environ.get("NOTIFY_LABELS", "person").split(",")
}
CAMERA_META_PATH = os.environ.get("CAMERA_META_PATH", "/config/cameras-meta.yml")


def load_camera_labels() -> dict:
    """Friendly camera names, entirely optional — without them the camera key
    is prettified instead.

    /config is a directory bind mount that is empty on a first deploy, so the
    file is normally just absent. Docker also creates a missing *file* mount
    source as a directory, which surfaces as IsADirectoryError rather than
    FileNotFoundError; neither is worth crash-looping the bridge over.
    """
    try:
        with open(CAMERA_META_PATH) as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, IsADirectoryError):
        print(f"[bridge] no {CAMERA_META_PATH}, using camera names as labels")
        return {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"[bridge] ignoring unreadable {CAMERA_META_PATH}: {exc}")
        return {}


CAMERA_LABELS = load_camera_labels()

# Track event ids we've already notified for, so we only push once per
# event instead of once per MQTT "update" message. Bounded so a long-running
# container doesn't grow this forever; Frigate ids are time-ordered, and
# duplicates only ever arrive within seconds of the original.
MAX_TRACKED_IDS = 1000
_notified_ids = []
_notified_set = set()


def mark_notified(event_id: str) -> None:
    _notified_ids.append(event_id)
    _notified_set.add(event_id)
    while len(_notified_ids) > MAX_TRACKED_IDS:
        _notified_set.discard(_notified_ids.pop(0))


def encode_header(value: str) -> str:
    """HTTP headers are latin-1 only, and `requests` raises on anything else.
    ntfy documents RFC 2047 encoded-words as the workaround, so use that for
    non-ASCII camera labels (and anything else that sneaks in)."""
    if value.isascii():
        return value
    return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def fetch_snapshot(event_id: str) -> bytes | None:
    url = f"{FRIGATE_URL}/api/events/{event_id}/snapshot.jpg"
    try:
        resp = requests.get(url, params={"crop": 1, "quality": 80}, timeout=10)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as exc:
        print(f"[bridge] snapshot fetch failed for {event_id}: {exc}")
        return None


def send_ntfy(event: dict, snapshot: bytes | None) -> None:
    camera = event["camera"]
    label = event["label"]
    score = round(event.get("top_score", 0) * 100)
    event_id = event["id"]

    camera_label = CAMERA_LABELS.get(camera, camera.replace("_", " ").title())
    headers = {
        "Title": encode_header(f"{label.capitalize()} detected - {camera_label}"),
        "Priority": "high" if label == "person" else "default",
        "Tags": "rotating_light",
        # Deep-links to the tracked object and its recording. /explore is the
        # 0.14+ route; the older /events?event_id= form is ignored by current
        # Frigate. Only resolves on the LAN — Frigate is not exposed publicly.
        "Click": f"{FRIGATE_UI_URL}/explore?event_id={event_id}",
    }
    if NTFY_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_AUTH_TOKEN}"

    body = f"{score}% confidence".encode()
    if snapshot:
        # ntfy treats the request body as an attachment when Filename is set
        headers["Filename"] = f"{event_id}.jpg"
        body = snapshot

    try:
        resp = requests.post(
            f"{NTFY_URL}/{NTFY_TOPIC}", headers=headers, data=body, timeout=10
        )
        resp.raise_for_status()
        print(f"[bridge] notified: {label} on {camera} ({event_id})")
    except requests.RequestException as exc:
        print(f"[bridge] ntfy push failed: {exc}")


def on_connect(client, userdata, flags, rc):
    print(f"[bridge] connected to mqtt broker (rc={rc})")
    client.subscribe("frigate/events")


def handle_event(payload: dict) -> None:
    if payload.get("type") != "new":
        return  # only alert once, when the event first starts

    after = payload.get("after", {})
    event_id = after.get("id")
    label = after.get("label")

    if not event_id or event_id in _notified_set:
        return
    if label not in NOTIFY_LABELS:
        return

    mark_notified(event_id)
    # give Frigate a moment to persist the best snapshot for this event
    time.sleep(2)
    snapshot = fetch_snapshot(event_id)
    send_ntfy(after, snapshot)


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    # An exception raised here escapes paho's network loop and tears down the
    # MQTT connection, so one malformed event must not be allowed to kill the
    # bridge — log it and stay subscribed.
    try:
        handle_event(payload)
    except Exception as exc:
        print(f"[bridge] error handling event: {exc!r}")


def main():
    print(f"[bridge] publishing to {NTFY_URL}/{NTFY_TOPIC}")
    if not NTFY_AUTH_TOKEN:
        # Servers configured deny-all reject anonymous publishing outright,
        # and that only shows up once a detection actually fires.
        print("[bridge] warning: NTFY_AUTH_TOKEN is empty — pushes will fail "
              "with 403 if the server requires auth")

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message

    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as exc:
            print(f"[bridge] mqtt connection error: {exc}, retrying in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
