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
# The default for every camera. cameras-meta.yml can widen or narrow it per
# camera, so this is a starting point rather than a ceiling.
NOTIFY_LABELS = {
    label.strip() for label in os.environ.get("NOTIFY_LABELS", "person").split(",")
}
CAMERA_META_PATH = os.environ.get("CAMERA_META_PATH", "/config/cameras-meta.yml")

# ntfy's named priorities. A typo here would make ntfy reject the push, so it's
# worth catching once at load time rather than per notification.
NTFY_PRIORITIES = {"min", "low", "default", "high", "urgent"}

# "visitor" is this bridge's invention, not one of Frigate's object classes:
# a person Frigate tracked inside one of the camera's visitor zones. Frigate
# detects a `person` either way — the zone is what separates someone walking
# up to the door from someone passing on the pavement — so the substitution
# happens here, between the event arriving and the label filter.
VISITOR_LABEL = "visitor"
# Which Frigate label a visitor is promoted from, unless the camera says
# otherwise. Anything in `objects.track` works: `car` plus a driveway zone
# gives "a car that pulled in" rather than one driving past.
VISITOR_BASE_DEFAULT = "person"


def parse_visitor_rule(camera: str, value) -> dict | None:
    """A camera's visitor zones, from any of the three forms allowed:

        visitor: porch                # one zone, promoting `person`
        visitor: [porch, steps]       # several
        visitor:
          zones: [porch]
          from: person                # which Frigate label gets promoted

    Zone names have to match the zone keys in Frigate's own config for that
    camera — Frigate reports zones by name in the event, and a name that
    doesn't exist there simply never matches.

    Returns None for a rule with no usable zones: an empty zone set would
    silently never promote anything, which on a camera narrowed to
    `labels: [visitor]` means silence rather than an obvious failure.
    """
    base = VISITOR_BASE_DEFAULT
    if isinstance(value, dict):
        zones = value.get("zones", [])
        if value.get("from") is not None:
            base = str(value["from"]).strip()
    else:
        zones = value

    # A bare string is the likely hand-edit slip, same as with `labels`.
    if isinstance(zones, str):
        zones = zones.split(",")
    if not isinstance(zones, list):
        print(f"[bridge] ignoring visitor rule for {camera}: expected a zone "
              "name, a list of them, or a mapping with `zones`")
        return None

    zones = {str(zone).strip() for zone in zones if str(zone).strip()}
    if not zones:
        print(f"[bridge] ignoring visitor rule for {camera}: no zone names")
        return None

    return {"zones": zones, "from": base}


def parse_camera_entry(camera: str, entry) -> dict:
    """One camera's overrides, from either form the file allows:

        backyard: Garden        # display name only
        front_door:             # any subset of the four keys
          name: Porch
          labels: [person, car]
          priority: high
          visitor: [porch]

    Anything unrecognised is dropped with a log line rather than raised. The
    file is hand-edited on a live system, and a typo on one camera shouldn't
    take notifications down for the rest.
    """
    if isinstance(entry, str):
        return {"name": entry}
    if not isinstance(entry, dict):
        print(f"[bridge] ignoring {camera}: expected a name or a mapping, "
              f"got {type(entry).__name__}")
        return {}

    meta = {}

    if entry.get("name") is not None:
        meta["name"] = str(entry["name"])

    if "labels" in entry:
        labels = entry["labels"]
        # A bare string is the likely hand-edit slip; read it the way
        # NOTIFY_LABELS is read rather than as one label named "person,car".
        if isinstance(labels, str):
            labels = labels.split(",")
        if isinstance(labels, list):
            # An empty list is meaningful — it mutes the camera — so it has to
            # stay distinct from the key being absent, which inherits instead.
            meta["labels"] = {str(label).strip() for label in labels}
        else:
            print(f"[bridge] ignoring labels for {camera}: expected a list")

    if entry.get("visitor") is not None:
        rule = parse_visitor_rule(camera, entry["visitor"])
        if rule:
            meta["visitor"] = rule

    priority = entry.get("priority")
    if priority is not None:
        if str(priority) in NTFY_PRIORITIES:
            meta["priority"] = str(priority)
        else:
            print(f"[bridge] ignoring priority {priority!r} for {camera}: "
                  f"expected one of {', '.join(sorted(NTFY_PRIORITIES))}")

    return meta


def load_camera_meta() -> dict:
    """Per-camera display names and notification rules, entirely optional —
    without the file every camera uses NOTIFY_LABELS and a prettified key.

    /config is a directory bind mount that is empty on a first deploy, so the
    file is normally just absent. Docker also creates a missing *file* mount
    source as a directory, which surfaces as IsADirectoryError rather than
    FileNotFoundError; neither is worth crash-looping the bridge over.
    """
    try:
        with open(CAMERA_META_PATH) as f:
            raw = yaml.safe_load(f) or {}
    except (FileNotFoundError, IsADirectoryError):
        print(f"[bridge] no {CAMERA_META_PATH}, every camera uses the defaults")
        return {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"[bridge] ignoring unreadable {CAMERA_META_PATH}: {exc}")
        return {}

    if not isinstance(raw, dict):
        print(f"[bridge] ignoring {CAMERA_META_PATH}: expected a mapping of "
              "camera -> name or settings")
        return {}

    return {camera: parse_camera_entry(camera, entry) for camera, entry in raw.items()}


CAMERA_META = load_camera_meta()


def display_name(camera: str) -> str:
    return CAMERA_META.get(camera, {}).get("name") or camera.replace("_", " ").title()


def labels_for(camera: str) -> set:
    """Which labels this camera pushes for. NOTIFY_LABELS is the default, not a
    ceiling: a camera can widen it as well as narrow it, and `labels: []` mutes
    the camera without stopping Frigate detecting or recording on it."""
    labels = CAMERA_META.get(camera, {}).get("labels")
    return NOTIFY_LABELS if labels is None else labels


def effective_label(camera: str, after: dict) -> str:
    """Frigate's label for the object, or "visitor" where the camera has a
    visitor rule and the object has been in one of its zones.

    Frigate reports zones two ways: `current_zones` is where the object is
    right now, `entered_zones` every zone it has been in during this event.
    Either counts — someone who rings the bell and steps back off the porch
    is still a visitor — so the two are read as one set.
    """
    label = after.get("label")
    rule = CAMERA_META.get(camera, {}).get("visitor")
    if not rule or label != rule["from"]:
        return label

    zones = set(after.get("entered_zones") or []) | set(after.get("current_zones") or [])
    return VISITOR_LABEL if zones & rule["zones"] else label


def priority_for(camera: str, label: str) -> str:
    """A camera-wide priority wins where one is set; otherwise people are the
    thing worth interrupting someone for — a visitor being a person who got
    close enough to matter."""
    return CAMERA_META.get(camera, {}).get("priority") or (
        "high" if label in ("person", VISITOR_LABEL) else "default"
    )

# Track what we've already notified for, so we only push once per event
# instead of once per MQTT "update" message. Keyed by event id *and* label,
# not id alone: one tracked person can push twice on a camera that wants both
# labels — once when they appear, once when they step into the visitor zone —
# and that is two different notifications about the same Frigate event.
# Bounded so a long-running container doesn't grow this forever; Frigate ids
# are time-ordered, and duplicates only ever arrive within seconds of the
# original.
MAX_TRACKED_EVENTS = 1000
_notified_keys = []
_notified_set = set()


def mark_notified(key: str) -> None:
    _notified_keys.append(key)
    _notified_set.add(key)
    while len(_notified_keys) > MAX_TRACKED_EVENTS:
        _notified_set.discard(_notified_keys.pop(0))


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


def send_ntfy(event: dict, snapshot: bytes | None, label: str) -> None:
    # `label` is what the notification says, which is not always
    # `event["label"]` — see effective_label().
    camera = event["camera"]
    score = round(event.get("top_score", 0) * 100)
    event_id = event["id"]

    camera_label = display_name(camera)
    headers = {
        "Title": encode_header(f"{label.capitalize()} detected - {camera_label}"),
        "Priority": priority_for(camera, label),
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
    after = payload.get("after", {})
    event_id = after.get("id")
    camera = after.get("camera")

    if not event_id or not camera:
        return

    label = effective_label(camera, after)

    # `new` arrives once, when Frigate first tracks the object, and is the
    # only message most notifications need. A person who walks into the
    # visitor zone *after* that only shows up in the updates which follow, so
    # those are read too — but solely for the promotion, or every camera
    # would push again on every update. `end` is the backstop for a short
    # event whose zone entry and exit both fall between two updates.
    kind = payload.get("type")
    if kind not in ("new", "update", "end"):
        return
    if kind != "new" and label != VISITOR_LABEL:
        return

    key = f"{event_id}:{label}"
    if key in _notified_set:
        return
    if label not in labels_for(camera):
        return

    mark_notified(key)
    # give Frigate a moment to persist the best snapshot for this event
    time.sleep(2)
    snapshot = fetch_snapshot(event_id)
    send_ntfy(after, snapshot, label)


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
