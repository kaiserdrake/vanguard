# RTSP surveillance stack: Frigate + MQTT + ntfy

## What's here
- `docker-compose.yml` — the services: mosquitto (MQTT broker), frigate (RTSP
  ingestion, motion/object detection, recording), ntfy-bridge, and scheduler.
  The ntfy **server** is not part of this stack — it's deployed separately at
  `NTFY_URL`, and ntfy-bridge is purely a client that publishes to it.
- `frigate/config.yml` — camera list, detector, and recording settings.
- `ntfy-bridge/` — small Python service, subscribes to `frigate/events` over
  MQTT and posts to ntfy with the detection snapshot attached.
- `scheduler/` — publishes camera control changes over MQTT on a time schedule.
- `tests/` — end-to-end test for the two Python services (see Testing below).
- `unraid/` — Docker tab icon for Unraid deployments (see Unraid below).
- `.env.example` — copy to `.env` and fill in before starting.

The two Python services run from images that GitHub Actions builds and
publishes to GHCR; mosquitto and Frigate come from upstream images.

## Setup

1. **Intel iGPU access** (host, one-time): confirm `/dev/dri` exists —
   `ls /dev/dri` should list `renderD128`. That's all the OpenVINO detector
   and VAAPI decode need; the compose file passes the device through and the
   container runs as root, so no render-group setup is required.

   On an NVIDIA host instead, install the
   [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
   and swap the commented `deploy:` block and `:stable-tensorrt` image back in.

2. **Copy and edit the env file**
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

3. **Edit `frigate/config.yml`**
   - Replace the `front_door` / `backyard` camera blocks with your actual
     RTSP URLs and credentials.
   - Point each camera's **detect** role at its low-res substream and the
     **record** role at the main stream, and set `detect.width`/`height` to the
     substream's real resolution. This matters most on low-power hosts — a
     Celeron N4505 cannot software-decode two 1080p streams just to run
     detection on them.
   - The `detectors:` block defaults to OpenVINO on the Intel iGPU; see the
     comments in the file for the NVIDIA/tensorrt and CPU alternatives, and
     update the `image:` tag on the `frigate` service to match if you switch.
   - The `objects.track` list controls what Frigate looks for; `NOTIFY_LABELS`
     in `.env` controls the subset that actually triggers a push.

4. **Start it**
   ```bash
   docker compose up -d
   ```

5. **Verify**
   - Frigate UI: `https://<host>:8971` — log in, then check the feeds are live and
     tune motion masks per-camera here (reduces false triggers from trees,
     passing traffic on a street-facing camera, etc.).
   - Trigger a test event (walk in front of a camera) and confirm a push
     shows up in the ntfy iOS app subscribed to your `NTFY_TOPIC`.
   - `docker compose logs -f ntfy-bridge` to debug notification delivery.

## Camera labels

`cameras-meta.yml` maps each camera's Frigate config key (e.g. `front_door`)
to a friendly display name. Frigate itself doesn't read this file — it's
used by `ntfy-bridge` for notification titles, and is there for any custom
UI you build later. Edit it and restart `ntfy-bridge`
(`docker compose restart ntfy-bridge`) to pick up changes.

## Time-based camera control

`scheduler/schedule.yml` defines windows during which a camera control
(`enabled`, `detect`, `recordings`, or `snapshots`) is turned on, publishing
`ON`/`OFF` to Frigate over MQTT outside the config file — no restart needed
to take effect, and no restart needed when you edit the schedule either
(the scheduler re-reads it every check interval). Set `TZ` in `.env` so the
time windows line up with your local time.

Note this only affects runtime state, not `frigate/config.yml` — if Frigate
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
docker compose up -d --build --no-deps mosquitto ntfy-bridge
```

`--no-deps` matters: `ntfy-bridge` declares `depends_on: frigate`, so without
it compose starts Frigate too and that needs the GPU. Subscribe to your topic
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
- add `front_door: Porch` to `cameras-meta.yml`, restart the bridge → the
  title follows the friendly name

**2. The scheduler.** Watch the control topics it publishes:

```bash
docker exec mosquitto mosquitto_sub -t 'frigate/#' -v
```

In another terminal, edit `scheduler/schedule.yml` so a window starts a minute
or two out, then start it. Drop `CHECK_INTERVAL_SECONDS` to `5` in
`docker-compose.yml` first so you're not waiting a full minute per check:

```bash
docker compose up -d --build --no-deps scheduler
```

You should see `frigate/<camera>/<control>/set ON` cross the window boundary,
and nothing repeated in between — it only publishes on transitions.

**3. Against real cameras.** Once RTSP URLs are in `frigate/config.yml`, bring
up the full stack and leave `mosquitto_sub -t 'frigate/#' -v` running — that
shows exactly what Frigate publishes, which is the fastest way to debug a
detection that didn't turn into a notification.

Tear down with `docker compose down` when you're finished.

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

Icons and the WebUI link are set through Unraid's own Docker UI rather than
compose labels, so the compose file stays portable and Unraid remains the
single source of truth for how containers are presented.

`unraid/vanguard.png` is a ready-made icon for the stack. Copy it somewhere
the webgui can read and point each container's **Icon URL** at that path:

```bash
mkdir -p /mnt/user/appdata/vanguard
cp unraid/vanguard.png /mnt/user/appdata/vanguard/
```

For Frigate's **WebUI** field use `https://[IP]:[PORT:8971]` — `https://`
because 8971 serves TLS with a self-signed cert, and `[IP]`/`[PORT:x]` are
Unraid placeholders it substitutes when rendering the link.

Use PNG: Unraid shows a question mark for SVG icons and renders nothing at all
for WebP. `unraid/vanguard.svg` is the source; re-render with
`unraid/render-icon.sh` after editing it.

## Network exposure

Only ntfy is reachable from the internet, and it isn't part of this stack —
it's a separate deployment behind Nginx Proxy Manager. **Frigate is LAN-only
and nothing here should be proxied by NPM.**

The compose file publishes exactly one Frigate port, `8971` — the
authenticated UI/API. Port `5000` is intentionally not published: Frigate
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

8971 serves TLS using a certificate Frigate generates itself, so expect a
one-time certificate warning per device. That's why `FRIGATE_UI_URL` uses
`https://`. Leave `tls:` alone in `frigate/config.yml` — disabling it is only
appropriate when a reverse proxy terminates TLS instead, which isn't the case
here.

This means **notification links only work on the local network**. The
snapshot embedded in each push works anywhere, because the bridge uploads the
image bytes to ntfy rather than linking back to Frigate — the phone never
contacts Frigate to render it. Tapping through to the recording needs LAN or
VPN access.

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
- Retention (`record.retain.days`, `record.events.retain.default` in
  `frigate/config.yml`) determines disk usage — tune based on available
  storage in `MEDIA_PATH`.
