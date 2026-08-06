#!/usr/bin/env python3
"""install_app.py hook: publish this app's bundled sounds into Home Assistant.

The announcer plays its chime via a ``media-source://media_source/local/<file>``
URI, so the audio files that ship in this repo's ``sounds/`` directory must live
in Home Assistant's *local media* folder. That folder is owned by Home Assistant
(a different host than the AppDaemon container), so we can't just copy files on
disk - we upload them over HA's REST API instead.

The hook is invoked by install_app.py as::

    python hook.py <conf> <app_dir>

It reads the HA URL + long-lived token straight out of the AppDaemon HASS plugin
config (``<conf>/appdaemon.yaml`` -> ``appdaemon.plugins.HASS``), then uploads
every file under ``<app_dir>/sounds/`` to ``media-source://media_source/local/.``.
Re-uploading an existing name overwrites it in place (HA does not create a
``name_1`` duplicate), so this is safe to run on every install/update.
"""

import mimetypes
import os
import sys
import urllib.request
import uuid

import yaml

UPLOAD_PATH = "/api/media_source/local_source/upload"
TARGET_DIR = "media-source://media_source/local/."


def _hass_plugin(conf):
    with open(os.path.join(conf, "appdaemon.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    plugins = ((cfg.get("appdaemon") or {}).get("plugins") or {})
    # The plugin key is conventionally "HASS" but match case-insensitively on
    # type: hass so a differently-named plugin still works.
    for _, p in plugins.items():
        if isinstance(p, dict) and str(p.get("type", "")).lower() == "hass":
            return p
    raise SystemExit("no HASS plugin found in appdaemon.yaml; cannot upload sounds")


def _multipart(fields, filename, data, content_type):
    boundary = uuid.uuid4().hex
    nl = b"\r\n"
    body = b""
    for name, value in fields.items():
        body += b"--" + boundary.encode() + nl
        body += f'Content-Disposition: form-data; name="{name}"'.encode() + nl + nl
        body += str(value).encode() + nl
    body += b"--" + boundary.encode() + nl
    body += (
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'
    ).encode() + nl
    body += f"Content-Type: {content_type}".encode() + nl + nl
    body += data + nl
    body += b"--" + boundary.encode() + b"--" + nl
    return body, "multipart/form-data; boundary=" + boundary


def _upload(ha_url, token, verify, path):
    filename = os.path.basename(path)
    with open(path, "rb") as f:
        data = f.read()
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body, content_type = _multipart(
        {"media_content_id": TARGET_DIR}, filename, data, ctype
    )
    req = urllib.request.Request(
        ha_url.rstrip("/") + UPLOAD_PATH, data=body, method="POST"
    )
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", content_type)
    ctx = None
    if not verify:
        import ssl

        ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        if resp.status not in (200, 201):
            raise SystemExit(f"upload of {filename} failed: HTTP {resp.status}")
    print(f"uploaded sound '{filename}' -> {TARGET_DIR}")


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: hook.py <conf> <app_dir>")
    conf, app_dir = sys.argv[1], sys.argv[2]

    sounds_dir = os.path.join(app_dir, "sounds")
    if not os.path.isdir(sounds_dir):
        print("no sounds/ dir; nothing to upload")
        return
    files = sorted(
        os.path.join(sounds_dir, n)
        for n in os.listdir(sounds_dir)
        if os.path.isfile(os.path.join(sounds_dir, n)) and not n.startswith(".")
    )
    if not files:
        print("sounds/ is empty; nothing to upload")
        return

    plugin = _hass_plugin(conf)
    ha_url = plugin.get("ha_url")
    token = plugin.get("token")
    verify = bool(plugin.get("cert_verify", True))
    if not ha_url or not token:
        raise SystemExit("HASS plugin is missing ha_url/token; cannot upload sounds")

    for path in files:
        _upload(ha_url, token, verify, path)


if __name__ == "__main__":
    main()
