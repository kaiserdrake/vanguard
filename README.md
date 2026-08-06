# RTSP surveillance stack: Frigate + MQTT + ntfy

## What's here
- `docker-compose.yml` — the services: mosquitto (MQTT broker), frigate (RTSP
  ingestion, motion/object detection, recording), ntfy-bridge, and scheduler,
  plus a one-shot `init` that stages the default configs (see below).
  The ntfy **server** is not part of this stack — it's deployed separately at
  `NTFY_URL`, and ntfy-bridge is purely a client that publishes to it.
- `defaults/` — starting-point configs, copied into `$APPDATA` on first start
  and not read at runtime: Frigate's `config.yml`, `mosquitto.conf`,
  `cameras-meta.yml`, `schedule.yml`.
- `ntfy-bridge/` — small Python service, subscribes to `frigate/events` over
  MQTT and posts to ntfy with the detection snapshot attached.
- `scheduler/` — publishes camera control changes over MQTT on a time schedule.
- `dev/` — configs for the test-deploy profile, `docker-compose.dev.yml`.
- `tests/` — end-to-end test for the two Python services (see Testing below).
- `unraid/` — Docker tab icon, if you deploy on Unraid (see Unraid below).
- `.env.example` — copy to `.env` and fill in before starting.

The two Python services run from images that GitHub Actions builds and
publishes to GHCR; mosquitto and Frigate come from upstream images.

## How config is staged

Clone, `docker compose up -d`, done — nothing to copy by hand, and nothing in
the checkout to edit.

The `init` service copies `defaults/` into `$APPDATA` **only where a file
doesn't already exist**, then exits; every other service waits on it and reads
its config from `$APPDATA`, never from the repo. So:

- your edits under `$APPDATA` are what runs, and survive every restart;
- `git pull` brings in new defaults without touching a live config — to adopt
  one, delete the live file and restart that service;
- nothing writes to the checkout, so the working tree stays clean and
  fast-forwards cleanly. This matters most for Frigate, which rewrites its own
  `config.yml` on schema migrations and on every save from the UI's Config
  Editor.

`$APPDATA` defaults to `./data` in the checkout (gitignored). Set `APPDATA` in
`.env` to put it elsewhere — `/srv/vanguard`, say. Create that directory
yourself beforehand if you want to edit the seeded files without `sudo`: `init`
gives what it creates the same owner as `$APPDATA`, and a directory docker
creates is owned by root.

## Setup

1. **Intel iGPU access** (host, one-time): confirm `/dev/dri` exists —
   `ls /dev/dri` should list `renderD128`. That's all the OpenVINO detector
   and VAAPI decode need; the compose file passes the device through and the
   container runs as root, so no render-group setup is required.

   On an NVIDIA host instead, install the
   [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
   and swap the commented `deploy:` block and `:stable-tensorrt` image back in.

2. **Clone the repo on the host** — the compose file reads `defaults/` from
   the checkout, so run everything from there.
   ```bash
   git clone https://github.com/kaiserdrake/vanguard.git /opt/vanguard
   cd /opt/vanguard
   ```

3. **Copy and edit the env file**
   ```bash
   cp .env.example .env
   # edit .env: set NTFY_AUTH_TOKEN (required — the server rejects anonymous
   # publishing), and set FRIGATE_UI_URL to your LAN IP so notification taps
   # open the right place
   ```

   `NTFY_URL` and `NTFY_TOPIC` are **required and have no defaults** — the
   stack refuses to start without them. On a public ntfy server the topic name
   is the only thing gating the feed, so treat it as a secret: generate an
   unguessable one, keep it in `.env` (gitignored), and never commit it.

   ```bash
   head -c 12 /dev/urandom | base64 | tr -dc 'A-Za-z0-9'
   ```

   Lock the server down as well, so a leaked topic name alone isn't enough —
   deny anonymous publish *and* anonymous read, and give the bridge a
   write-only token.

4. **Start it**
   ```bash
   docker compose up -d
   ```

   `init` runs first and stages `defaults/` into `$APPDATA`; watch what it did
   with `docker compose logs init`. Everything else comes up behind it.

5. **Add your cameras.** The stack ships with **no cameras configured** — a
   placeholder feed is one Frigate retries forever and logs an error about
   every few seconds. Add yours from the Frigate UI at `https://<host>:8090`,
   under **Settings → Config Editor** (it saves to
   `$APPDATA/frigate/config.yml`), or edit that file directly and restart
   Frigate. `defaults/frigate/config.yml` carries a commented two-camera
   example to paste in.
   - Point each camera's **detect** role at its low-res substream and the
     **record** role at the main stream, and set `detect.width`/`height` to the
     substream's real resolution. This matters most on low-power hosts — a
     Celeron N4505 cannot software-decode two 1080p streams just to run
     detection on them.
   - The `detectors:` block defaults to OpenVINO on the Intel iGPU; see the
     comments in the file for the NVIDIA/tensorrt and CPU alternatives, and
     update the `image:` tag on the `frigate` service to match if you switch.
   - The `objects.track` list controls what Frigate looks for; `NOTIFY_LABELS`
     in `.env` controls the subset that actually triggers a push. Both can be
     overridden per camera.

6. **Optional, per camera**, both seeded empty and both fine left that way:
   - `$APPDATA/ntfy-bridge/cameras-meta.yml` — display name, label list and
     ntfy priority per camera; without an entry the camera uses
     `NOTIFY_LABELS` and a prettified key (`front_door` → `Front door`). See
     "Per-camera notifications" below.
   - `$APPDATA/scheduler/schedule.yml` — time-of-day camera control; with no
     entries the scheduler idles.

7. **Verify**
   - Frigate UI: `https://<host>:8090` — log in, then check the feeds are live and
     tune motion masks per-camera here (reduces false triggers from trees,
     passing traffic on a street-facing camera, etc.).
   - Trigger a test event (walk in front of a camera) and confirm a push
     shows up in the ntfy iOS app subscribed to your `NTFY_TOPIC`.
   - `docker compose logs -f ntfy-bridge` to debug notification delivery.

## Per-camera notifications

`$APPDATA/ntfy-bridge/cameras-meta.yml` keys off each camera's Frigate config
key (e.g. `front_door`). Frigate never reads it: Frigate decides what to
*detect*, this decides what to *push*. Edit it and restart `ntfy-bridge`
(`docker compose restart ntfy-bridge`) to pick up changes.

A plain string is just a display name. A mapping sets any subset of three keys:

```yaml
front_door:
  name: Porch             # notification title; default is the key prettified
  labels: [person, car]   # what pushes here; default is NOTIFY_LABELS
  priority: high          # min | low | default | high | urgent
driveway:
  labels: [car]           # people on the street don't wake anyone up
shed:
  labels: []              # recorded and detected, never pushed
backyard: Garden          # name only, inherits NOTIFY_LABELS
```

`NOTIFY_LABELS` in `.env` is the default for every camera, not a ceiling — a
camera can widen it as well as narrow it. `labels: []` mutes a camera in the
bridge alone, leaving Frigate detecting and recording as before. Without a
`priority`, person events are `high` and everything else is `default`.

Seeded empty, which is a fine way to leave it. A bad value is logged and
skipped rather than taken down the bridge, so check
`docker compose logs ntfy-bridge` after editing.

For per-camera control of what's detected in the first place — object lists,
motion masks, zones, retention — see Frigate's own config: nearly every
top-level block can be overridden inside a `cameras:` entry, and that's the
better place to stop something being detected at all rather than merely
unreported.

## Time-based camera control

`$APPDATA/scheduler/schedule.yml` defines windows during which a camera control
(`enabled`, `detect`, `recordings`, or `snapshots`) is turned on, publishing
`ON`/`OFF` to Frigate over MQTT outside the config file — no restart needed
to take effect, and no restart needed when you edit the schedule either
(the scheduler re-reads it every check interval). Set `TZ` in `.env` so the
time windows line up with your local time. Seeded with an empty `schedules:`
list; `defaults/scheduler/schedule.yml` documents the format and has commented
examples.

Note this only affects runtime state, not Frigate's `config.yml` — if Frigate
restarts, cameras come back in whatever state the config file specifies
until the scheduler's next check corrects them (default: within 60s).

## Testing

`tests/run_test.sh` starts a real mosquitto broker plus a mock that stands in
for both Frigate's HTTP API and the ntfy server, then drives `ntfy-bridge` and
`scheduler` through it — fake detection events go in over MQTT, and the test
asserts on the notifications that come out and the control topics the
scheduler publishes.

```bash
./tests/run_test.sh
```

Only Docker is needed; Frigate itself is never started (it wants a GPU and
real RTSP cameras, and everything in this repo talks to it over MQTT and
HTTP, which the mock covers). The test builds both images from source, so it
also validates the Dockerfiles.

### Manual testing

Everything below uses `mosquitto_pub`/`mosquitto_sub` from inside the
mosquitto container, so there's nothing to install on the host.

**1. Notifications, without any cameras or a GPU.** This is the useful one —
it exercises the real path to your phone. Frigate stays stopped, so you get a
`93% confidence` text body instead of a snapshot image; everything else
(title, priority, tap-through link) is exactly what a real detection sends.

```bash
cp .env.example .env   # set NTFY_AUTH_TOKEN, or pushes come back 403
docker compose up init                                  # stage the configs
docker compose up -d --build --no-deps mosquitto ntfy-bridge
```

`--no-deps` matters: `ntfy-bridge` declares `depends_on: frigate`, so without
it compose starts Frigate too and that needs the GPU. It also skips `init`,
hence the separate line — without it mosquitto has no `mosquitto.conf` to read.
Subscribe to your topic
in the ntfy app (or
`curl -s -H "Authorization: Bearer $NTFY_AUTH_TOKEN" $NTFY_URL/$NTFY_TOPIC/json`),
then fake a detection:

```bash
docker exec mosquitto mosquitto_pub -t frigate/events -m '{"type":"new","after":{"id":"test-1","camera":"front_door","label":"person","top_score":0.93}}'
```

A push titled `Person detected - Front door` should arrive. Watch
`docker compose logs -f ntfy-bridge` alongside it. Worth poking at:

- re-run the same command → no second push (deduped on event id)
- change `"id"` to `test-2` → a new push
- change `"label"` to something outside `NOTIFY_LABELS` → nothing
- change `"type"` to `update` → nothing (only new events alert)
- add `front_door: Porch` to `$APPDATA/ntfy-bridge/cameras-meta.yml`, restart
  the bridge → the title follows the friendly name
- switch that entry to the mapping form with `labels: [car]`, restart → the
  same person event stops pushing while other cameras keep working

**2. The scheduler.** Watch the control topics it publishes:

```bash
docker exec mosquitto mosquitto_sub -t 'frigate/#' -v
```

In another terminal, edit `$APPDATA/scheduler/schedule.yml` so a window starts
a minute or two out, then start it. Add `CHECK_INTERVAL_SECONDS: "5"` to the
scheduler service in `docker-compose.yml` first (it defaults to 60) so you're
not waiting a full minute per check:

```bash
docker compose up -d --build --no-deps scheduler
```

You should see `frigate/<camera>/<control>/set ON` cross the window boundary,
and nothing repeated in between — it only publishes on transitions.

**3. Against real cameras.** Once RTSP URLs are in
`$APPDATA/frigate/config.yml`, bring up the full stack and leave
`mosquitto_sub -t 'frigate/#' -v` running — that shows exactly what Frigate
publishes, which is the fastest way to debug a detection that didn't turn into
a notification.

Tear down with `docker compose down` when you're finished.

**4. The whole stack, on a box with no cameras and no GPU.**
`docker-compose.dev.yml` is a standalone profile that stands up mosquitto,
Frigate, both Python services (built from source) and a synthetic RTSP camera,
using the configs in `dev/` mounted straight from the checkout — it seeds
nothing and touches no `$APPDATA`.

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

Frigate's UI comes up on 8971 here, not production's 8090, and its state lives
in `dev-config/` and `dev-media/` rather than `$APPDATA` — so the two profiles
can run side by side on one host without colliding.

## Building the images

GitHub Actions builds `ntfy-bridge` and `scheduler` on every push to `main`
that touches them and pushes to GHCR as
`ghcr.io/<owner>/vanguard-ntfy-bridge` and `ghcr.io/<owner>/vanguard-scheduler`
(`:latest` plus a commit-SHA tag). Pull requests build both images and run the
test suite, but don't publish. The push job only runs if the tests pass.

No secrets to configure — the workflow authenticates with the built-in
`GITHUB_TOKEN`. Two one-time repo settings:

- Settings → Actions → General → Workflow permissions: **Read and write**
  (needed for `packages: write`).
- After the first successful run, the packages are created private. To pull
  them on the host without logging in, open each package → Package settings →
  Change visibility → Public. Otherwise run
  `docker login ghcr.io -u <user> -p <PAT with read:packages>` on the host.

To update the running stack once a build lands:

```bash
docker compose pull ntfy-bridge scheduler && docker compose up -d
```

To build those two from source instead of pulling while developing, create
`docker-compose.override.yml` — compose picks it up automatically:

```yaml
services:
  ntfy-bridge:
    build: ./ntfy-bridge
    image: vanguard-ntfy-bridge:local
  scheduler:
    build: ./scheduler
    image: vanguard-scheduler:local
```

It's gitignored on purpose. If it were committed, every clone — production
included — would silently build from source rather than run the CI-tested
images, and you'd only notice when a local-only change reached the real stack.

## Unraid

The stack targets a plain Linux host with a git clone. It still runs on Unraid,
but **pasting `docker-compose.yml` into Compose Manager is no longer enough**:
`init` bind-mounts `./defaults`, and Compose Manager's project directory holds
only a compose file. Docker creates a missing bind source as an empty
*directory*, so `init` would find nothing to seed and mosquitto would exit with
`Unable to open config file`.

Clone the repo into the project directory instead, so `./defaults` resolves:

```bash
git clone https://github.com/kaiserdrake/vanguard.git /boot/config/plugins/compose.manager/projects/vanguard
```

Set `APPDATA=/mnt/user/appdata/vanguard` and `MEDIA_PATH=/mnt/user/frigate` in
the project's `.env`, and start it from Compose Manager as usual.

Icons and the WebUI link are set through Unraid's own Docker UI rather than
compose labels, so the compose file stays portable and Unraid remains the
single source of truth for how containers are presented.

`unraid/vanguard.png` is a ready-made icon for the stack. Copy it somewhere
the webgui can read and point each container's **Icon URL** at that path:

```bash
mkdir -p /mnt/user/appdata/vanguard
cp unraid/vanguard.png /mnt/user/appdata/vanguard/
```

For Frigate's **WebUI** field use `https://[IP]:[PORT:8090]` — `https://`
because Frigate serves that port with a self-signed cert, and `[IP]`/`[PORT:x]`
are Unraid placeholders it substitutes when rendering the link.

Use PNG: Unraid shows a question mark for SVG icons and renders nothing at all
for WebP. `unraid/vanguard.svg` is the source; re-render with
`unraid/render-icon.sh` after editing it.

## Network exposure

By default the only thing reachable from the internet is ntfy, which isn't
part of this stack — it's a separate deployment behind Nginx Proxy Manager.
Frigate stays LAN-only unless you deliberately proxy it; see "Public access"
below for what that costs and how to do it.

The compose file publishes exactly one Frigate port: `8090` on the host, which
is Frigate's authenticated UI/API on `8971` inside the container (that one is
fixed by Frigate's own nginx; only the host side is ours to choose). Port
`5000` is intentionally not published: Frigate
serves it with no authentication whatsoever ("access to this port should be
limited... intended to be used within the docker network"), and nothing needs
it on the host. `ntfy-bridge` reaches it as `http://frigate:5000` over the
internal docker network, which requires no port mapping. Publishing 5000, or
putting a reverse proxy in front of it, hands out full access to your cameras
and recordings.

Mosquitto's `1883` is not published either, and that one matters more than it
looks: the broker runs with `allow_anonymous true`, so exposing it to the LAN
would let anyone publish to `frigate/<camera>/<control>/set` and switch
cameras, detection or recording off. Frigate, ntfy-bridge and scheduler all
reach it over the internal docker network. Publish it only for an off-host
MQTT client such as Home Assistant, and add a password file to
`mosquitto.conf` first if you do.

`8554` (RTSP restream) and `8555` (WebRTC) are commented out for the same
reason — both are unauthenticated. Dropping 8555 only costs you WebRTC live
view; the UI falls back to MSE, which works, with a bit more latency.

That port serves TLS using a certificate Frigate generates itself, so expect a
one-time certificate warning per device on the LAN. That's why
`FRIGATE_UI_URL` uses `https://`, and why `tls.enabled` stays `true` even
behind a proxy — it keeps the proxy-to-host hop encrypted.

Left LAN-only, **notification links only work on the local network**. The
snapshot embedded in each push works anywhere regardless, because the bridge
uploads the image bytes to ntfy rather than linking back to Frigate — the phone
never contacts Frigate to render it. Only tapping through to the recording
needs LAN, VPN, or the public setup below.

## Public access

To make notification taps work off the LAN, put a reverse proxy in front of
Frigate and point `FRIGATE_UI_URL` at it. Frigate's own login stays the gate,
so it's the same account you already use — no second set of credentials.

**Understand what this trades away first.** It puts every camera and every
recording behind one username and password. Frigate has no 2FA and no account
lockout; the rate limit below is the whole brute-force defence, and Frigate's
own docs advise against direct exposure. A VPN (WireGuard, Tailscale) makes the
same links work from anywhere with nothing exposed and no proxy at all — if
that's acceptable, stop here and do that instead.

**1. Frigate.** `defaults/frigate/config.yml` already ships the settings this
needs — `cookie_secure`, a 30-day `session_length` so a phone isn't re-logging
in daily, and `failed_login_rate_limit`. One value is left for you to fill in,
in `$APPDATA/frigate/config.yml`:

```yaml
auth:
  trusted_proxies:
    - 192.168.1.10/32     # the proxy's address, as Frigate sees it
```

Without it every request looks like it came from the proxy, so the rate limit
becomes one shared bucket and a single attacker locks you out of your own
cameras.

**2. The proxy.** In NPM, add a proxy host for your Frigate hostname:

| Field | Value |
| --- | --- |
| Scheme | `https` — Frigate keeps its own TLS on 8090 |
| Forward host / port | the Frigate host's LAN IP, port `8090` |
| Websockets support | **on** — live view and the event stream need it |
| Block common exploits | on |
| SSL | Let's Encrypt cert, Force SSL, HTTP/2, HSTS |

nginx doesn't verify upstream certificates, so Frigate's self-signed one is
fine here — and it keeps the proxy-to-host hop encrypted, which matters when
the proxy runs on a different machine. If it runs on this same host instead,
you can skip the published port entirely and give the `frigate` service the
proxy's docker network, forwarding to `frigate:8971`.

**3. `.env`:**

```bash
FRIGATE_UI_URL=https://frigate.example.com
```

No port. Restart the bridge (`docker compose restart ntfy-bridge`) — the link
is read at startup. New pushes now deep-link to
`https://frigate.example.com/explore?event_id=...`; a tap shows Frigate's login
first, then the event.

Keep the real hostname in `.env` only. It's gitignored, and a committed one
advertises a camera login page to anyone reading the repo.

**Worth adding once it's public:** a distinct viewer-role account for the
phone rather than reusing admin, and — if you want 2FA — Authelia or Authentik
in front, wired up through Frigate's `proxy.header_map`. That does mean the
first factor stops being Frigate's own credentials.

## Notes
- Pushes go to a self-hosted ntfy at `NTFY_URL`, which is configured to deny
  anonymous access, so the camera snapshots aren't world-readable. That
  protection is a property of the ntfy server's config, not of this repo: if
  `auth-default-access` is ever relaxed to `read-only` or `read-write`,
  anyone who guesses the topic can read your snapshots. Give the bridge a
  `write-only` token rather than an admin one.
- Frigate's own web UI already covers live view, event timeline, and clip
  playback — you may not need a custom UI at all beyond configuring masks
  and zones there. Build a custom dashboard on top only if you want it
  unified with your other Docker container monitoring.
- Retention (the `record:` and `snapshots:` blocks in Frigate's `config.yml`)
  determines disk usage — tune based on available storage in `MEDIA_PATH`.
  `defaults/frigate/config.yml` documents what each tier costs per camera
  per day.
