"""Announcer -- ducked Sonos TTS announcements for AppDaemon.

Replaces a brittle Home Assistant automation that hand-tuned a fixed
``delay`` before restoring audio. This app instead:

* **snapshots** the current Sonos audio (whole group), **ducks** the volume,
  optionally plays a **chime**, **speaks** a TTS message, then **restores** the
  previous audio the moment playback *actually* finishes -- no hand-tuned
  delays, so the spoken text can be any length / fully dynamic;
* **serializes** announcements through a single worker thread, so two triggers
  firing close together never clobber each other's snapshot (the bug in the old
  automation, where a second ``sonos.snapshot`` would capture the *ducked*
  state and "restore" to it);
* supports **quiet hours** (either fully ``silent`` or just ``quieter``);
* is **reusable**: any number of config-driven state ``events`` map a trigger to
  a message, and a generic ``announcer.say`` AppDaemon event lets any other app
  or automation request a properly ducked announcement with dynamic text.
"""

import queue
import threading
import time
from datetime import datetime, time as _dtime

import appdaemon.plugins.hass.hassapi as hass


def _parse_hhmm(value):
    """'HH:MM' -> datetime.time, or None."""
    if not value:
        return None
    try:
        hh, mm = str(value).split(":")
        return _dtime(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def _as_list(value):
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


class Announcer(hass.Hass):
    def initialize(self):
        # --- Targets -------------------------------------------------------
        self.speakers = _as_list(self.args.get("speakers"))
        self.duck_with_group = bool(self.args.get("duck_with_group", True))

        # --- Volume --------------------------------------------------------
        self.announce_volume = float(self.args.get("announce_volume", 0.5))

        # --- Chime ---------------------------------------------------------
        chime = self.args.get("chime", {}) or {}
        self.chime_enabled = bool(chime.get("enabled", False))
        self.chime_url = chime.get("media_url")
        self.chime_type = chime.get("media_content_type", "music")
        self.chime_volume = chime.get("volume")  # None -> use announce volume
        self.chime_timeout = float(chime.get("timeout", 10))

        # --- TTS -----------------------------------------------------------
        tts = self.args.get("tts", {}) or {}
        self.tts_engine = tts.get("engine", "tts.home_assistant_cloud")
        self.tts_cache = bool(tts.get("cache", True))

        # --- Timing safety caps (no fixed announcement delay) --------------
        self.start_timeout = float(self.args.get("start_timeout", 15))
        self.announce_timeout = float(self.args.get("announce_timeout", 45))
        self.settle_seconds = max(0.0, float(self.args.get("settle_seconds", 0.3)))
        # async volume_set / snapshot need a beat to land before the next step,
        # else the duck lands mid-speech (blasting the first word) or the
        # snapshot captures the already-ducked volume (matches the 1s delay the
        # old HA automation needed).
        self.duck_settle = max(0.0, float(self.args.get("duck_settle_seconds", 0.7)))
        self._poll = 0.15

        self.dry_run = bool(self.args.get("dry_run", False))

        # --- Quiet hours ---------------------------------------------------
        qh = self.args.get("quiet_hours", {}) or {}
        self.qh_start = _parse_hhmm(qh.get("start"))
        self.qh_end = _parse_hhmm(qh.get("end"))
        self.qh_mode = str(qh.get("mode", "silent")).lower()  # silent | quieter
        self.qh_volume = float(qh.get("quiet_volume", 0.2))
        self.qh_skip_chime = bool(qh.get("skip_chime", True))

        # --- Serialized worker (fixes the concurrent-snapshot bug) ---------
        self._stopping = False
        self._queue = queue.Queue()
        self._worker = threading.Thread(
            target=self._run_worker, name="announcer-worker", daemon=True
        )
        self._worker.start()

        # --- Config-driven state triggers ----------------------------------
        self.events = self.args.get("events", []) or []
        n_triggers = 0
        for ev in self.events:
            trig = ev.get("trigger", {}) or {}
            for ent in _as_list(trig.get("entity_id")):
                self.listen_state(
                    self._on_state, ent, ev=ev, to_target=trig.get("to")
                )
                n_triggers += 1

        # --- Generic event API ---------------------------------------------
        self.say_event = self.args.get("say_event", "announcer.say")
        self.listen_event(self._on_say_event, self.say_event)

        self.log(
            "Announcer initialised: %d speaker(s), %d trigger(s), chime=%s, "
            "quiet_hours=%s, say_event=%s, dry_run=%s",
            len(self.speakers),
            n_triggers,
            self.chime_enabled,
            ("%s-%s/%s" % (qh.get("start"), qh.get("end"), self.qh_mode)
             if self.qh_start and self.qh_end else "off"),
            self.say_event,
            self.dry_run,
        )

    # -- trigger shims -------------------------------------------------------
    def _on_state(self, entity, attribute, old, new, kwargs):
        ev = kwargs["ev"]
        to_target = kwargs.get("to_target")
        if to_target is not None and new not in _as_list(to_target):
            return
        if old == new:  # ignore attribute-only churn
            return
        self._enqueue(ev, default_name=entity)

    def _on_say_event(self, event_name, data, kwargs):
        if not data.get("message"):
            self.log("%s with no 'message' ignored", event_name, level="WARNING")
            return
        self._enqueue(data, default_name=data.get("name", "say_event"))

    def _enqueue(self, spec, default_name):
        message = spec.get("message")
        if not message:
            self.log("Announcement '%s' has no message; skipping",
                     default_name, level="WARNING")
            return
        self._queue.put({
            "message": message,
            "speakers": spec.get("speakers"),
            "volume": spec.get("volume"),
            "chime": spec.get("chime"),
            "ignore_quiet_hours": bool(spec.get("ignore_quiet_hours", False)),
            "name": spec.get("name", default_name),
        })

    # -- worker --------------------------------------------------------------
    def _run_worker(self):
        while True:
            req = self._queue.get()
            if req is None or self._stopping:  # shutdown sentinel / draining
                self._queue.task_done()
                if req is None:
                    return
                continue
            try:
                self._process(req)
            except Exception as exc:  # noqa: BLE001
                self.log("Announcement '%s' failed: %s",
                         req.get("name"), exc, level="ERROR")
            finally:
                self._queue.task_done()

    def terminate(self):
        """Stop the worker cleanly on reload/shutdown.

        Signals any in-flight announcement to abort (its `finally` still
        restores audio), drains the queue, then joins briefly so the old
        instance's worker doesn't overlap the reloaded one.
        """
        self._stopping = True
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except queue.Empty:
            pass
        self._queue.put(None)
        try:
            self._worker.join(timeout=3.0)
        except Exception:  # noqa: BLE001
            pass

    def _process(self, req):
        speakers = _as_list(req["speakers"]) or self.speakers
        if not speakers:
            self.log("No speakers configured; cannot announce '%s'",
                     req["name"], level="WARNING")
            return

        quiet = self._in_quiet_hours() and not req["ignore_quiet_hours"]
        if quiet and self.qh_mode == "silent":
            self.log("[quiet:silent] suppressed '%s': %s",
                     req["name"], req["message"])
            return

        volume = req["volume"]
        if volume is None:
            volume = self.qh_volume if quiet else self.announce_volume

        want_chime = req["chime"]
        if want_chime is None:
            want_chime = self.chime_enabled
        if quiet and self.qh_skip_chime:
            want_chime = False

        primary = speakers[0]
        self.log("Announcing '%s' on %s (vol=%.2f chime=%s quiet=%s): %s",
                 req["name"], primary, volume, bool(want_chime), quiet,
                 req["message"])

        if self.dry_run:
            self.log("[dry_run] would snapshot -> duck -> "
                     "%schime -> speak -> restore",
                     "" if want_chime else "no ")
            return

        # Capture pre-duck volume/state. If nothing is playing there is nothing
        # to snapshot/restore -- and worse, sonos.restore of an idle snapshot
        # reasserts the *ducked* volume asynchronously, racing (and beating) any
        # volume we set afterwards. So when idle we skip snapshot/restore and
        # simply undo the duck ourselves; when something IS playing we snapshot
        # and let sonos.restore put back audio + volume.
        pre_volume = {s: self._safe_float(
            self.get_state(s, attribute="volume_level")) for s in speakers}
        pre_playing = self.get_state(primary) in ("playing", "paused", "buffering")

        # 1) snapshot the current audio (whole group) -- only if something plays
        if pre_playing:
            self.call_service("sonos/snapshot", entity_id=speakers,
                              with_group=self.duck_with_group)
            if self.duck_settle:  # let snapshot capture the true (un-ducked) volume
                time.sleep(self.duck_settle)
        try:
            # 2) duck to the announcement volume -- poll until HA reports the new
            #    level so the duck has actually landed before any audio plays
            #    (volume_set is fire-and-forget; there is no sync variant).
            self._set_volume(speakers, volume)

            # 3) optional chime (failure is non-fatal -- still speak)
            if want_chime and self.chime_url:
                self._play_chime(speakers, primary, volume)

            # 4) speak, then wait for playback to ACTUALLY finish
            baseline = self._media_signature(primary)
            self.call_service("tts/speak", entity_id=self.tts_engine,
                              media_player_entity_id=speakers,
                              message=req["message"], cache=self.tts_cache)
            self._wait_for_clip(primary, baseline, self.announce_timeout)
        finally:
            # 5) restore what was playing, or just undo the duck if idle
            if self.settle_seconds:
                time.sleep(self.settle_seconds)
            if pre_playing:
                try:
                    self.call_service("sonos/restore", entity_id=speakers,
                                      with_group=self.duck_with_group)
                except Exception as exc:  # noqa: BLE001
                    self.log("sonos.restore failed for %s: %s",
                             primary, exc, level="ERROR")
            else:
                for spk, vol in pre_volume.items():
                    if vol is not None:
                        self._set_volume(spk, vol)

    def _play_chime(self, speakers, primary, base_volume):
        chime_vol = self.chime_volume if self.chime_volume is not None else base_volume
        try:
            if chime_vol != base_volume:
                self._set_volume(speakers, chime_vol)
            baseline = self._media_signature(primary)
            self.call_service("media_player/play_media", entity_id=speakers,
                              media_content_id=self.chime_url,
                              media_content_type=self.chime_type)
            self._wait_for_clip(primary, baseline, self.chime_timeout)
        except Exception as exc:  # noqa: BLE001
            self.log("Chime failed (continuing to speech): %s", exc,
                     level="WARNING")
        finally:
            if chime_vol != base_volume:
                self._set_volume(speakers, base_volume)

    # -- helpers -------------------------------------------------------------
    def _media_signature(self, entity):
        """(content_id, position_updated_at) identifying the CURRENT clip."""
        return (
            self.get_state(entity, attribute="media_content_id"),
            self.get_state(entity, attribute="media_position_updated_at"),
        )

    def _wait_for_clip(self, entity, baseline, timeout):
        """Wait for a freshly-started clip to finish playing.

        We must not trust ``media_duration`` until we've confirmed the *new*
        clip is actually the current media -- otherwise a stale value left over
        from a prior clip (e.g. the chime) could truncate the wait. So:

        (a) wait until the player is ``playing`` a media signature that differs
            from ``baseline`` (the clip we just asked for has started), bounded
            by ``start_timeout``; if we never see a distinct start we bail
            without over-waiting;
        (b) only then read this clip's ``media_duration`` and wait for playback
            to stop, bounded by ``timeout`` (and by duration+buffer when known).

        Aborts early if the app is being torn down.
        """
        start = time.time()

        # (a) confirm the new clip started
        started = False
        begin_cap = start + min(self.start_timeout, timeout)
        while time.time() < begin_cap and not self._stopping:
            if (self.get_state(entity) == "playing"
                    and self._media_signature(entity) != baseline):
                started = True
                break
            time.sleep(self._poll)
        if not started:
            return

        # (b) wait for THIS clip to finish
        deadline = start + timeout
        duration = self._safe_float(
            self.get_state(entity, attribute="media_duration")
        )
        if duration:
            deadline = min(deadline, time.time() + duration + 1.5)
        while time.time() < deadline and not self._stopping:
            if self.get_state(entity) != "playing":
                return
            time.sleep(self._poll)
        if not self._stopping:
            self.log("wait_for_clip: timed out waiting for %s to finish",
                     entity, level="WARNING")

    @staticmethod
    def _safe_float(value):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    def _set_volume(self, entities, volume):
        """Set volume and poll until HA confirms it (fire-and-forget service).

        ``media_player.volume_set`` returns before the device applies the
        change, so a blind sleep either wastes time or races the next step.
        Instead we set it and poll ``get_state`` until every target reports the
        target level (within a small tolerance), bounded by ``duck_settle`` --
        confirming faster than a fixed sleep in the common case, and never
        proceeding with the duck only half-applied.
        """
        targets = _as_list(entities)
        self.call_service("media_player/volume_set", entity_id=targets,
                          volume_level=volume)
        deadline = time.time() + max(self.duck_settle, 0.05)
        while time.time() < deadline and not self._stopping:
            if all(
                (lambda v: v is not None and abs(v - volume) <= 0.02)(
                    self._safe_float(
                        self.get_state(e, attribute="volume_level")))
                for e in targets
            ):
                return
            time.sleep(self._poll)

    def _in_quiet_hours(self):
        if not self.qh_start or not self.qh_end:
            return False
        now = datetime.now().time()
        start, end = self.qh_start, self.qh_end
        if start <= end:
            return start <= now < end
        return now >= start or now < end  # window wraps past midnight
