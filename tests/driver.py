"""Integration test driver: publishes fake Frigate events, asserts on the
notifications the bridge sends and the control topics the scheduler publishes.
"""
import base64
import json
import sys
import time

import paho.mqtt.client as mqtt
import requests

MOCK = "http://mock:9000"
scheduler_msgs = []
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""), flush=True)


def on_message(client, userdata, msg):
    scheduler_msgs.append((msg.topic, msg.payload.decode()))
    print(f"[driver] saw {msg.topic} = {msg.payload.decode()}", flush=True)


client = mqtt.Client()
client.on_message = on_message
for attempt in range(30):
    try:
        client.connect("mosquitto", 1883, 60)
        break
    except Exception:
        time.sleep(1)
else:
    print("driver could not reach broker")
    sys.exit(1)

client.subscribe("frigate/+/+/set")
client.loop_start()

# let the bridge and scheduler settle
time.sleep(12)

EVENTS = [
    # (description, payload, should_notify)
    (
        "person event -> notifies",
        {"type": "new", "after": {"id": "evt-person-1", "camera": "front_door",
                                  "label": "person", "top_score": 0.91}},
        True,
    ),
    (
        "duplicate id -> deduped",
        {"type": "new", "after": {"id": "evt-person-1", "camera": "front_door",
                                  "label": "person", "top_score": 0.91}},
        False,
    ),
    (
        "label not in NOTIFY_LABELS -> ignored",
        {"type": "new", "after": {"id": "evt-dog-1", "camera": "backyard",
                                  "label": "dog", "top_score": 0.80}},
        False,
    ),
    (
        "type=update -> ignored",
        {"type": "update", "after": {"id": "evt-person-2", "camera": "backyard",
                                     "label": "person", "top_score": 0.95}},
        False,
    ),
    (
        "car on backyard -> notifies",
        {"type": "new", "after": {"id": "evt-car-1", "camera": "backyard",
                                  "label": "car", "top_score": 0.77}},
        True,
    ),
    (
        "malformed json -> survives",
        "NOT-JSON",
        False,
    ),
    (
        "event missing expected fields -> survives",
        {"type": "new", "after": {"id": "evt-broken-1", "label": "person"}},
        False,
    ),
    (
        "non-ascii camera label -> notifies",
        {"type": "new", "after": {"id": "evt-jp-1", "camera": "side_gate",
                                  "label": "person", "top_score": 0.88}},
        True,
    ),
    (
        "per-camera labels widen NOTIFY_LABELS -> notifies",
        {"type": "new", "after": {"id": "evt-dog-2", "camera": "garage",
                                  "label": "dog", "top_score": 0.72}},
        True,
    ),
    (
        "per-camera labels narrow NOTIFY_LABELS -> ignored",
        {"type": "new", "after": {"id": "evt-person-garage", "camera": "garage",
                                  "label": "person", "top_score": 0.94}},
        False,
    ),
    (
        "labels: [] mutes the camera -> ignored",
        {"type": "new", "after": {"id": "evt-person-shed", "camera": "shed",
                                  "label": "person", "top_score": 0.96}},
        False,
    ),
    (
        "unparseable priority falls back, camera still notifies",
        {"type": "new", "after": {"id": "evt-person-side", "camera": "side_yard",
                                  "label": "person", "top_score": 0.89}},
        True,
    ),
]

expected_notifications = 0
for desc, payload, should_notify in EVENTS:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    client.publish("frigate/events", body)
    if should_notify:
        expected_notifications += 1
    time.sleep(4)  # bridge sleeps 2s before fetching the snapshot

time.sleep(5)

got = requests.get(f"{MOCK}/_received", timeout=10).json()

check("bridge sent exactly the expected pushes",
      len(got) == expected_notifications,
      f"expected {expected_notifications}, got {len(got)}")

if got:
    n = got[0]
    h = {k.lower(): v for k, v in n["headers"].items()}
    check("push goes to the configured topic", n["path"] == "/testtopic", n["path"])
    check("title uses friendly camera name from cameras-meta.yml",
          h.get("title") == "Person detected - Front door", h.get("title"))
    check("person events get high priority", h.get("priority") == "high", h.get("priority"))
    check("snapshot attached as jpeg", n["is_jpeg"] and n["body_len"] > 0,
          f"is_jpeg={n['is_jpeg']} len={n['body_len']}")
    check("Filename header set so ntfy treats it as an attachment",
          h.get("filename") == "evt-person-1.jpg", h.get("filename"))
    check("auth token sent as a Bearer header",
          h.get("authorization") == "Bearer tk_testtoken123", h.get("authorization"))
    check("Click deep-links to the recording via the 0.14+ /explore route",
          h.get("click") == "https://frigate.test:8971/explore?event_id=evt-person-1",
          h.get("click"))

if len(got) > 1:
    h2 = {k.lower(): v for k, v in got[1]["headers"].items()}
    check("non-person events get default priority", h2.get("priority") == "default", h2.get("priority"))
    check("camera without a meta entry still gets a readable title",
          h2.get("title") == "Car detected - Backyard", h2.get("title"))

if len(got) > 2:
    h3 = {k.lower(): v for k, v in got[2]["headers"].items()}
    expected_jp = "=?UTF-8?B?" + base64.b64encode(
        "Person detected - 玄関わき".encode()).decode() + "?="
    check("non-ascii camera label is RFC 2047 encoded, not crashed on",
          h3.get("title") == expected_jp, h3.get("title"))
    check("bridge still delivering after malformed/broken events",
          got[2]["is_jpeg"], "last push arrived with its snapshot")

# --- per-camera settings from cameras-meta.yml ---
# Keyed by event id rather than arrival order, so adding cases above doesn't
# renumber these.
by_event = {}
for n in got:
    h = {k.lower(): v for k, v in n["headers"].items()}
    by_event[h.get("filename", "")] = h

check("per-camera labels widen NOTIFY_LABELS",
      "evt-dog-2.jpg" in by_event, sorted(by_event))
check("per-camera labels narrow NOTIFY_LABELS",
      "evt-person-garage.jpg" not in by_event, sorted(by_event))
check("labels: [] mutes a camera without touching Frigate",
      "evt-person-shed.jpg" not in by_event, sorted(by_event))

if "evt-dog-2.jpg" in by_event:
    hg = by_event["evt-dog-2.jpg"]
    check("per-camera priority overrides the person/other rule",
          hg.get("priority") == "low", hg.get("priority"))
    check("mapping form supplies the display name too",
          hg.get("title") == "Dog detected - Garage", hg.get("title"))

if "evt-person-side.jpg" in by_event:
    hs = by_event["evt-person-side.jpg"]
    check("an unparseable priority is skipped, not fatal",
          hs.get("priority") == "high", hs.get("priority"))
    check("the rest of that camera's entry still applies",
          hs.get("title") == "Person detected - Side yard", hs.get("title"))

# --- scheduler assertions ---
topics = {t: p for t, p in scheduler_msgs}
check("scheduler published the always-on window",
      topics.get("frigate/test_always_on/detect/set") == "ON", str(topics))
check("scheduler published OFF for an out-of-window entry",
      topics.get("frigate/test_always_off/detect/set") == "OFF", str(topics))
check("scheduler handles midnight-wrapping windows",
      topics.get("frigate/test_wrap/recordings/set") == "ON", str(topics))
check("scheduler skips days not listed",
      topics.get("frigate/test_wrong_day/snapshots/set") == "OFF", str(topics))
check("scheduler publishes each transition once",
      len(scheduler_msgs) == len(set(scheduler_msgs)),
      f"{len(scheduler_msgs)} msgs, {len(set(scheduler_msgs))} unique")

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed", flush=True)
sys.exit(1 if failed else 0)
