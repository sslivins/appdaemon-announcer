# appdaemon-announcer

An [AppDaemon](https://appdaemon.readthedocs.io/) app that makes **ducked Sonos
TTS announcements** — it snapshots whatever is playing, lowers the volume,
optionally plays a chime, speaks a message, and then restores the previous audio
**the moment playback actually finishes**.

It replaces a Home Assistant automation whose weak spot was a hand-tuned
`delay: 2s` before `sonos.restore`: that fixed gap can't know how long the
spoken text takes, so it breaks the instant you change the message. AppDaemon
waits for the real end of playback instead, so the text can be anything and
fully dynamic.

## Why AppDaemon (vs. the HA automation)

- **No hand-tuned delay.** The app watches the speaker's state (and
  `media_duration`) and restores exactly when it stops — works for any message
  length.
- **Concurrent-safe.** Announcements run through a single worker queue. In the
  old automation, if a second trigger fired mid-announcement its
  `sonos.snapshot` captured the *already-ducked* state and then "restored" to
  it — leaving the volume low and the wrong track. Serializing fixes that.
- **Reusable.** Config-driven `events` map any state trigger to a message, and a
  generic `announcer.say` event lets any other app/automation request a properly
  ducked announcement.
- **Quiet hours.** Suppress announcements overnight, or just make them quieter.

## How an announcement runs

```
sonos.snapshot (with_group)
  -> volume_set (duck)
  -> [chime] play_media, wait until it finishes
  -> tts.speak, wait until playback actually finishes   <- no fixed delay
  -> sonos.restore (with_group)
```

A chime failure (e.g. an unreachable URL) is non-fatal — it logs a warning and
still speaks.

## Triggering announcements

**1. Config `events`** (state → message), in `announcer.yaml`:

```yaml
  events:
    - name: laundry_complete
      trigger:
        entity_id:
          - sensor.zv367575n_laundry_machine_state
          - sensor.zv367668n_laundry_machine_state
        to: "Finished"
      message: "The laundry cycle has completed"
```

**2. The generic `announcer.say` event** — from any other app or a HA
automation:

```yaml
# Home Assistant automation action:
action: event.fire            # (or appdaemon fires it directly)
# event_type: announcer.say
# event_data:
#   message: "Dinner is ready"
#   speakers: [media_player.kitchen]   # optional, overrides default
#   volume: 0.6                        # optional
#   chime: false                       # optional
#   ignore_quiet_hours: true           # optional
```

From another AppDaemon app: `self.fire_event("announcer.say", message="…")`.

## Chime

`chime.media_url` must be resolvable **for the Sonos speaker**. The simplest and
recommended option is to reference a file that ships with this repo:

- **Bundled sound (recommended):** files under [`sounds/`](sounds/) are uploaded
  into Home Assistant's *local media* automatically at install time (see
  [Installation](#installation)), so point at
  `media-source://media_source/local/chime.wav`. A mellow default `chime.wav` is
  included; drop your own files in `sounds/` and re-run the installer to publish
  them.
- **Host on HA `www`:** drop a sound in `/config/www/` and point at
  `http://homeassistant.bdl:8123/local/chime.mp3`.
- **Public URL:** any reachable `.mp3`.

Set `chime.enabled: false` (globally) or `chime: false` (per event) to skip it.

## Ducking modes

- **`overlay` (default, Sonos):** plays the chime and speech as Sonos *audio
  clips* on top of the current music — Sonos automatically ducks the music under
  the clip and restores it, with no snapshot/restore. This is the low-latency,
  "voice on top of quieter music" behaviour.
- **`snapshot`:** the legacy path — snapshot the group, stop/duck, play, then
  restore. Use it for non-Sonos speakers that don't support audio-clip overlay.

## Quiet hours

```yaml
  quiet_hours:
    start: "21:30"
    end: "07:30"     # window may wrap past midnight
    mode: quieter    # 'silent' = skip entirely | 'quieter' = play at quiet_volume
    quiet_volume: 0.2
    skip_chime: true
```

Any event or `announcer.say` call can set `ignore_quiet_hours: true` to always
play (e.g. a critical alert).

## Installation

Follows the
[`appdaemon-base`](https://github.com/sslivins/appdaemon-base) convention — a
self-contained public repo cloned into `conf/apps/announcer/`, with its
`install/logs.yaml` fragment merged into the shared `appdaemon.yaml` and its
`install/hook.py` uploading everything under `sounds/` into Home Assistant's
local media — all by `install_app.py`:

```sh
cd ~/docker/appdaemon-base
python3 install_app.py announcer \
    --repo https://github.com/sslivins/appdaemon-announcer \
    --conf ~/docker/appdaemon/conf \
    --restart
```

Then edit `conf/apps/announcer/announcer.yaml` for your speakers, TTS engine,
chime, and events. No third-party Python dependencies. App `.py`/`.yaml` edits
hot-reload without a restart.

> The bundled chime is uploaded to HA local media by `install/hook.py`, which
> reads the HA URL + token from the AppDaemon `HASS` plugin config and POSTs each
> `sounds/` file to `media-source://media_source/local/.` (overwriting in place).
> Add or replace files in `sounds/` and re-run `install_app.py` to republish.

To update: `git -C ~/docker/appdaemon/conf/apps/announcer pull`.
To remove: re-run `install_app.py announcer --remove`.

## Configuration (`announcer.yaml`)

| Key | What it is |
| --- | --- |
| `speakers` | Default target `media_player.*` speaker(s). |
| `duck_mode` | `overlay` (Sonos audio-clip, default) or `snapshot` (legacy). |
| `duck_with_group` | Snapshot/restore the whole Sonos group (default true). |
| `announce_volume` | Volume while speaking (default 0.5). |
| `overlay_gap_seconds` | Spacing between chime and voice clips in overlay mode. |
| `chime.enabled` / `chime.media_url` | Play a chime before speech; URL must be Sonos-reachable. |
| `chime.volume` / `chime.timeout` | Optional chime volume; max wait for it to finish. |
| `tts.engine` / `tts.cache` | TTS entity (default `tts.home_assistant_cloud`) and cache flag. |
| `tts.voice` / `tts.language` | Optional voice (e.g. `AvaNeural`) and language for the TTS engine. |
| `quiet_hours.*` | `start`/`end` window, `mode` (`silent`/`quieter`), `quiet_volume`, `skip_chime`. |
| `start_timeout` / `announce_timeout` | Safety caps on waiting for playback to start / finish. |
| `say_event` | Event name for the generic API (default `announcer.say`). |
| `events` | List of `{name, trigger:{entity_id,to}, message, …overrides}`. |
| `dry_run` | Log the plan without touching the speakers. |

## Files

| File | Purpose |
| --- | --- |
| `announcer.py` | The app logic. |
| `announcer.yaml` | App configuration (edit for your setup). |
| `sounds/` | Bundled audio (e.g. `chime.wav`), uploaded to HA local media on install. |
| `install/logs.yaml` | `logs:` fragment merged into `appdaemon.yaml`. |
| `install/hook.py` | Uploads `sounds/` files into HA local media at install time. |

## License

MIT
