import datetime
import os
import time

import paho.mqtt.client as mqtt
import yaml

MQTT_HOST = os.environ["MQTT_HOST"]
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
SCHEDULE_PATH = os.environ.get("SCHEDULE_PATH", "/config/schedule.yml")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", 60))

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Tracks the last state we published per (camera, control) so we only
# publish on actual transitions, not every check interval.
_last_state = {}


def load_schedules() -> list:
    with open(SCHEDULE_PATH) as f:
        return (yaml.safe_load(f) or {}).get("schedules", [])


def parse_hm(value: str) -> datetime.time:
    hour, minute = value.split(":")
    return datetime.time(int(hour), int(minute))


def in_window(now: datetime.datetime, entry: dict) -> bool:
    days = entry.get("days")
    if days and DAY_NAMES[now.weekday()] not in days:
        return False

    start = parse_hm(entry["start"])
    end = parse_hm(entry["end"])
    now_t = now.time()

    if start <= end:
        return start <= now_t <= end
    return now_t >= start or now_t <= end  # window wraps past midnight


def apply_schedules(client: mqtt.Client, schedules: list) -> None:
    now = datetime.datetime.now()
    for entry in schedules:
        camera = entry["camera"]
        control = entry["control"]
        desired = "ON" if in_window(now, entry) else "OFF"

        key = (camera, control)
        if _last_state.get(key) == desired:
            continue

        topic = f"frigate/{camera}/{control}/set"
        client.publish(topic, desired)
        _last_state[key] = desired
        print(f"[scheduler] {topic} -> {desired}")


def connect_with_retry(client: mqtt.Client) -> None:
    """The broker often isn't accepting connections yet when we start —
    depends_on only orders container startup, it doesn't wait for readiness."""
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            print(f"[scheduler] connected to mqtt broker at {MQTT_HOST}:{MQTT_PORT}")
            return
        except OSError as exc:
            print(f"[scheduler] mqtt connection error: {exc}, retrying in 5s")
            time.sleep(5)


def main():
    client = mqtt.Client()
    connect_with_retry(client)
    # loop_start handles reconnects on its own once the first connect lands
    client.loop_start()

    while True:
        try:
            schedules = load_schedules()
            apply_schedules(client, schedules)
        except Exception as exc:
            print(f"[scheduler] error: {exc}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
