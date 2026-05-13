#!/usr/bin/env python3
"""
Standalone rlog uploader for comma devices.

Drop into /data/upload_rlogs/ on device and run:
  python3 /data/upload_rlogs/upload_rlogs.py --auto &          # daemon mode
  python3 /data/upload_rlogs/upload_rlogs.py <route_id>        # one-shot
  python3 /data/upload_rlogs/upload_rlogs.py --latest           # latest route
  python3 /data/upload_rlogs/upload_rlogs.py --auto --dry-run   # preview

Zero sunnypilot code changes required. Uses device's existing auth and APIs.
Replicates the "Upload Logs" button in comma Connect via POST /v1/{dongleId}/upload_urls/.
"""
import argparse
import json
import os
import random
import requests
import signal
import sys
import threading
import time

from cereal import log
import cereal.messaging as messaging
from openpilot.common.api import Api

API_HOST = os.getenv('API_HOST', 'https://api.commadotai.com')
from openpilot.common.params import Params
from openpilot.common.utils import get_upload_stream
from openpilot.system.hardware.hw import Paths
from openpilot.system.loggerd.uploader import listdir_by_creation
from openpilot.system.loggerd.xattr_cache import getxattr, setxattr

NetworkType = log.DeviceState.NetworkType

UPLOAD_ATTR_NAME = 'user.upload'
UPLOAD_ATTR_VALUE = b'1'
PRESERVE_ATTR_NAME = 'user.preserve'
PRESERVE_ATTR_VALUE = b'1'

RLOG_NAMES = ('rlog.zst', 'rlog.bz2', 'rlog')
MAX_RETRY_COUNT = 30
RETRY_BACKOFF_MAX = 120
UPLOAD_EXPIRY_DAYS = 7


def log_msg(msg: str) -> None:
  ts = time.strftime("%Y-%m-%d %H:%M:%S")
  print(f"[{ts}] {msg}", flush=True)


def is_uploaded(fn: str) -> bool:
  try:
    return getxattr(fn, UPLOAD_ATTR_NAME) == UPLOAD_ATTR_VALUE
  except OSError:
    return False


def mark_uploaded(fn: str) -> None:
  try:
    setxattr(fn, UPLOAD_ATTR_NAME, UPLOAD_ATTR_VALUE)
  except OSError:
    log_msg(f"  WARNING: failed to set xattr on {fn}")


def set_preserve(segment_dir: str) -> None:
  try:
    setxattr(segment_dir, PRESERVE_ATTR_NAME, PRESERVE_ATTR_VALUE)
  except OSError:
    pass


def find_rlog(segment_path: str) -> tuple[str, str] | None:
  """Find the rlog file in a segment directory. Returns (filename, full_path) or None."""
  for name in RLOG_NAMES:
    fn = os.path.join(segment_path, name)
    if os.path.isfile(fn):
      return name, fn
  return None


def get_upload_key(logdir: str, rlog_name: str) -> str:
  """Build the upload key, appending .zst if the file needs compression."""
  key = os.path.join(logdir, rlog_name)
  # uncompressed rlogs get uploaded as .zst (compressed on the fly)
  if key.endswith('rlog') and not key.endswith('.zst') and not key.endswith('.bz2'):
    key += ".zst"
  return key


def scan_route_segments(root: str, route_id: str) -> list[str]:
  """Return sorted list of segment dirs matching a route_id prefix."""
  try:
    dirs = [d for d in os.listdir(root) if d.startswith(route_id + "--") and os.path.isdir(os.path.join(root, d))]
  except OSError:
    return []
  return sorted(dirs, key=lambda d: int(d.rsplit("--", 1)[-1]) if d.rsplit("--", 1)[-1].isdigit() else 0)


def scan_all_routes(root: str) -> list[str]:
  """Return all unique route prefixes from the log root, sorted by creation."""
  routes = set()
  for d in listdir_by_creation(root):
    # segment dirs are like 2026-01-02--03-04-05--0
    parts = d.rsplit("--", 1)
    if len(parts) == 2 and parts[1].isdigit():
      routes.add(parts[0])
  return sorted(routes)


def find_missing_rlogs(root: str, route_id: str) -> list[tuple[str, str, str]]:
  """Find rlogs that haven't been uploaded for a route.
  Returns list of (logdir, upload_key, full_path)."""
  missing = []
  segments = scan_route_segments(root, route_id)
  for logdir in segments:
    segment_path = os.path.join(root, logdir)

    # skip locked segments (still being written)
    try:
      if any(name.endswith(".lock") for name in os.listdir(segment_path)):
        continue
    except OSError:
      continue

    result = find_rlog(segment_path)
    if result is None:
      continue

    rlog_name, fn = result
    if is_uploaded(fn):
      continue

    key = get_upload_key(logdir, rlog_name)
    missing.append((logdir, key, fn))

  return missing


def get_user_token() -> str | None:
  """Read the user OAuth token stored by comma Connect pairing, if available."""
  try:
    config_root = Paths.config_root()
    auth_path = os.path.join(config_root, 'auth.json')
    with open(auth_path) as f:
      return json.load(f).get('access_token')
  except Exception:
    return None


def fetch_upload_urls(api: Api, dongle_id: str, keys: list[str]) -> dict[str, dict]:
  """Batch-fetch presigned upload URLs via POST /v1/{dongleId}/upload_urls/.
  Replicates the comma Connect 'Upload Logs' button.
  Uses user OAuth token (from Connect pairing) if available, falls back to device JWT.
  Returns dict mapping key -> {'url': str, 'headers': dict}."""
  token = get_user_token() or api.get_token()
  try:
    resp = requests.post(
      f"{API_HOST}/v1/{dongle_id}/upload_urls/",
      json={"expiry_days": UPLOAD_EXPIRY_DAYS, "paths": keys},
      headers={
        "Authorization": f"JWT {token}",
        "Content-Type": "application/json",
      },
      timeout=30,
    )
  except Exception as e:
    log_msg(f"  ERROR: upload_urls request failed: {e}")
    return {}

  if resp.status_code in (401, 403):
    log_msg(f"  ERROR: auth failed ({resp.status_code})")
    return {}

  if resp.status_code != 200:
    log_msg(f"  ERROR: upload_urls returned {resp.status_code}: {resp.text[:200]}")
    return {}

  try:
    data = resp.json()
  except Exception as e:
    log_msg(f"  ERROR: bad upload_urls response: {e}")
    return {}

  # response may be a list aligned with keys, or a dict keyed by path
  if isinstance(data, list):
    return {keys[i]: item for i, item in enumerate(data) if i < len(keys)}
  elif isinstance(data, dict):
    return data
  else:
    log_msg(f"  ERROR: unexpected upload_urls response format: {type(data)}")
    return {}


def do_upload(key: str, fn: str, url: str, headers: dict) -> bool:
  """Upload a single rlog file to a presigned URL. Returns True on success."""
  try:
    sz = os.path.getsize(fn)
  except OSError:
    log_msg(f"  ERROR: cannot stat {fn}")
    return False

  if sz == 0:
    log_msg(f"  SKIP: {fn} is empty")
    mark_uploaded(fn)
    return True

  compress = key.endswith('.zst') and not fn.endswith('.zst')
  stream = None
  try:
    stream, content_length = get_upload_stream(fn, compress)
    log_msg(f"  UPLOADING: {key} ({content_length / 1e6:.1f} MB{'  compressed' if compress else ''})...")
    put_resp = requests.put(url, data=stream,
                            headers={**headers, 'Content-Length': str(content_length)},
                            timeout=60)
  except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
    log_msg(f"  ERROR: upload failed: {e}")
    return False
  finally:
    if stream:
      stream.close()

  if put_resp.status_code in (200, 201):
    log_msg(f"  OK: {key} uploaded")
    mark_uploaded(fn)
    return True
  elif put_resp.status_code == 412:
    log_msg(f"  SKIP: {key} already on server (412)")
    mark_uploaded(fn)
    return True
  else:
    log_msg(f"  ERROR: PUT returned {put_resp.status_code}")
    return False


def get_network_state(sm: messaging.SubMaster) -> tuple[int, bool]:
  """Returns (network_type, is_metered). Falls back to HTTP probe if service unavailable."""
  try:
    # wait up to 2s for a fresh deviceState message
    sm.update(2000)
    if sm.updated['deviceState']:
      return sm['deviceState'].networkType.raw, sm['deviceState'].networkMetered
  except Exception:
    pass

  # deviceState not available or stale — probe connectivity directly
  try:
    requests.head("https://api.comma.ai", timeout=5)
    return NetworkType.wifi, False
  except Exception:
    return NetworkType.none, False


def upload_route(api: Api, dongle_id: str, root: str, route_id: str, dry_run: bool = False) -> tuple[int, int]:
  """Upload all missing rlogs for a route. Returns (uploaded_count, failed_count)."""
  missing = find_missing_rlogs(root, route_id)
  if not missing:
    log_msg(f"Route {route_id}: all rlogs already uploaded")
    return 0, 0

  log_msg(f"Route {route_id}: {len(missing)} rlog(s) to upload")

  if dry_run:
    for _, key, fn in missing:
      try:
        sz = os.path.getsize(fn)
      except OSError:
        sz = 0
      log_msg(f"  DRY-RUN: would upload {key} ({sz / 1e6:.1f} MB)")
    return len(missing), 0

  # batch-fetch presigned URLs for all missing rlogs in one request
  keys = [key for _, key, _ in missing]
  url_map = fetch_upload_urls(api, dongle_id, keys)
  if not url_map:
    return 0, len(missing)

  uploaded = 0
  failed = 0
  for logdir, key, fn in missing:
    segment_path = os.path.join(root, logdir)
    set_preserve(segment_path)

    entry = url_map.get(key)
    if entry is None:
      log_msg(f"  ERROR: no upload URL returned for {key}")
      failed += 1
      continue

    if do_upload(key, fn, entry['url'], entry['headers']):
      uploaded += 1
    else:
      failed += 1

  return uploaded, failed


def run_oneshot(args: argparse.Namespace) -> int:
  params = Params()
  dongle_id = params.get("DongleId")
  if dongle_id is None:
    log_msg("ERROR: DongleId not set")
    return 1

  root = Paths.log_root()
  api = Api(dongle_id)

  # resolve route
  if args.latest:
    routes = scan_all_routes(root)
    if not routes:
      log_msg("ERROR: no routes found")
      return 1
    route_id = routes[-1]
    log_msg(f"Latest route: {route_id}")
  else:
    route_id = args.route
    segments = scan_route_segments(root, route_id)
    if not segments:
      log_msg(f"ERROR: no segments found for route {route_id}")
      available = scan_all_routes(root)
      if available:
        log_msg(f"Available routes: {', '.join(available[-10:])}")
      return 1

  # check network (best-effort)
  sm = messaging.SubMaster(['deviceState'])
  net_type, metered = get_network_state(sm)
  if net_type == NetworkType.none:
    log_msg("ERROR: no network connection")
    return 1
  if metered and not args.allow_cellular:
    log_msg("ERROR: metered network detected, use --allow-cellular to proceed")
    return 1

  uploaded, failed = upload_route(api, dongle_id, root, route_id, args.dry_run)
  log_msg(f"Done: {uploaded} uploaded, {failed} failed")
  return 1 if failed > 0 else 0


def run_auto(args: argparse.Namespace) -> int:
  params = Params()
  dongle_id = params.get("DongleId")
  if dongle_id is None:
    log_msg("ERROR: DongleId not set")
    return 1

  root = Paths.log_root()
  api = Api(dongle_id)
  sm = messaging.SubMaster(['deviceState'])

  exit_event = threading.Event()

  def handle_signal(sig, frame):
    log_msg("Shutting down...")
    exit_event.set()

  signal.signal(signal.SIGTERM, handle_signal)
  signal.signal(signal.SIGINT, handle_signal)

  log_msg("Auto-upload daemon started")
  backoff = 0.1

  while not exit_event.is_set():
    # check network
    net_type, metered = get_network_state(sm)
    offroad = params.get_bool("IsOffroad")

    if net_type == NetworkType.none:
      wait = 60 if offroad else 5
      log_msg(f"No network, retrying in {wait}s")
      exit_event.wait(wait)
      continue

    if metered and not args.allow_cellular:
      wait = 60 if offroad else 10
      exit_event.wait(wait)
      continue

    # scan all routes for missing rlogs
    routes = scan_all_routes(root)
    uploaded_any = False

    for route_id in routes:
      if exit_event.is_set():
        break

      missing = find_missing_rlogs(root, route_id)
      if not missing:
        continue

      if args.dry_run:
        for _, key, fn in missing:
          try:
            sz = os.path.getsize(fn)
          except OSError:
            sz = 0
          log_msg(f"  DRY-RUN: would upload {key} ({sz / 1e6:.1f} MB)")
        uploaded_any = True
        continue

      # batch-fetch presigned URLs for the entire route
      keys = [key for _, key, _ in missing]
      url_map = fetch_upload_urls(api, dongle_id, keys)
      if not url_map:
        backoff = min(backoff * 2, RETRY_BACKOFF_MAX)
        log_msg(f"Failed to get upload URLs, backing off {backoff:.1f}s")
        exit_event.wait(backoff)
        break

      for logdir, key, fn in missing:
        if exit_event.is_set():
          break

        # re-check network before each upload
        net_type, metered = get_network_state(sm)
        if net_type == NetworkType.none:
          break
        if metered and not args.allow_cellular:
          break

        entry = url_map.get(key)
        if entry is None:
          log_msg(f"  ERROR: no upload URL returned for {key}")
          backoff = min(backoff * 2, RETRY_BACKOFF_MAX)
          exit_event.wait(backoff)
          break

        segment_path = os.path.join(root, logdir)
        set_preserve(segment_path)

        if do_upload(key, fn, entry['url'], entry['headers']):
          uploaded_any = True
          backoff = 0.1
          # small pause between uploads to avoid hammering
          exit_event.wait(backoff + random.uniform(0, backoff))
        else:
          backoff = min(backoff * 2, RETRY_BACKOFF_MAX)
          log_msg(f"Upload failed, backing off {backoff:.1f}s")
          exit_event.wait(backoff)
          break  # back to outer loop to re-check network

    if not uploaded_any and not exit_event.is_set():
      # nothing to upload — sleep longer
      wait = 60 if offroad else 5
      exit_event.wait(wait)

  log_msg("Auto-upload daemon stopped")
  return 0


def main() -> int:
  parser = argparse.ArgumentParser(description="Upload rlogs to comma servers")
  parser.add_argument("route", nargs="?", help="Route ID (e.g. 2026-01-02--03-04-05)")
  parser.add_argument("--latest", action="store_true", help="Upload the most recent route")
  parser.add_argument("--auto", action="store_true", help="Daemon mode: continuously upload all routes")
  parser.add_argument("--allow-cellular", action="store_true", help="Allow uploads over metered connections")
  parser.add_argument("--dry-run", action="store_true", help="Show what would be uploaded without uploading")
  args = parser.parse_args()

  if not args.auto and not args.latest and args.route is None:
    parser.error("Provide a route ID, --latest, or --auto")

  if args.auto:
    return run_auto(args)
  else:
    return run_oneshot(args)


if __name__ == "__main__":
  sys.exit(main())
