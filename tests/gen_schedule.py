"""Generates a schedule.yml whose expected ON/OFF outcome is known for
whatever time the test happens to run at (scheduler container runs TZ=UTC)."""
import datetime

now = datetime.datetime.now(datetime.UTC)
n = now.hour * 60 + now.minute
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def hm(minutes):
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# a window 2-3h in the future: never covers now
off_start, off_end = hm(n + 120), hm(n + 180)
# start just after end -> wraps midnight, covers everything but one minute 12h away
wrap_start, wrap_end = hm(n + 720), hm(n + 719)
tomorrow = DAYS[(now.weekday() + 1) % 7]

print(
    f"""schedules:
  - camera: test_always_on
    control: detect
    start: "00:00"
    end: "23:59"

  - camera: test_always_off
    control: detect
    start: "{off_start}"
    end: "{off_end}"

  - camera: test_wrap
    control: recordings
    start: "{wrap_start}"
    end: "{wrap_end}"

  - camera: test_wrong_day
    control: snapshots
    start: "00:00"
    end: "23:59"
    days: [{tomorrow}]
"""
)
