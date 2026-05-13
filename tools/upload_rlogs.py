#!/usr/bin/env python3
"""
External file uploader for comma devices.
Runs on laptop with comma account credentials (via tools/lib/auth.py).

Authenticates using the user OAuth token from ~/.comma/auth.json,
discovers missing files via athena's listDataDirectory RPC, fetches
presigned upload URLs from the API, then dispatches uploads back to
the device via athena's uploadFilesToUrls RPC.

The authenticated user must own the device (shared/viewer access is rejected).

Usage:
  python3 tools/upload_rlogs.py <dongle_id>                              # rlogs only (default)
  python3 tools/upload_rlogs.py <dongle_id> --types all                  # all supported types
  python3 tools/upload_rlogs.py <dongle_id> --types rlog,fcamera         # multiple types
  python3 tools/upload_rlogs.py <dongle_id> --route 00000007--354981eb2f # specific route
  python3 tools/upload_rlogs.py <dongle_id> --dry-run                    # preview without uploading
  python3 tools/upload_rlogs.py <dongle_id> --auto                       # daemon: repeat every 10m
  python3 tools/upload_rlogs.py <dongle_id> --auto --interval 300        # repeat every 5m
  python3 tools/upload_rlogs.py <dongle_id> --allow-cellular             # allow metered connections

Supported --types values: rlog, fcamera, dcamera, ecamera, all

Authentication:
  Run tools/lib/auth.py first to log in with your comma account.
  Token is read from ~/.comma/auth.json automatically.
"""
import argparse
import json
import os
import signal
import sys
import time

import requests

API_HOST = os.getenv('API_HOST', 'https://api.commadotai.com')
ATHENA_HOST = os.getenv('ATHENA_HOST', 'https://athena.comma.ai')
UPLOAD_EXPIRY_DAYS = 7
DEFAULT_INTERVAL = 600  # 10 minutes

# Filenames on device (relative to segment dir) for each supported type
FILE_TYPES: dict[str, tuple[str, ...]] = {
  'rlog':    ('rlog', 'rlog.bz2', 'rlog.zst'),
  'fcamera': ('fcamera.hevc', 'fcamera.ts'),
  'dcamera': ('dcamera.hevc', 'dcamera.ts'),
  'ecamera': ('ecamera.hevc', 'ecamera.ts'),
}

# Corresponding field in /v1/route/{fullname}/files response
API_FIELD: dict[str, str] = {
  'rlog':    'logs',
  'fcamera': 'cameras',
  'dcamera': 'dcameras',
  'ecamera': 'ecameras',
}

ALL_TYPES = set(FILE_TYPES.keys())
ALL_FILENAMES: set[str] = {fn for names in FILE_TYPES.values() for fn in names}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_msg(msg: str) -> None:
  ts = time.strftime("%Y-%m-%d %H:%M:%S")
  print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_token() -> str:
  """Read user OAuth token from ~/.comma/auth.json (written by tools/lib/auth.py)."""
  auth_path = os.path.expanduser("~/.comma/auth.json")
  try:
    with open(auth_path) as f:
      token = json.load(f).get('access_token')
    if not token:
      raise ValueError("access_token missing in auth.json")
    return token
  except FileNotFoundError:
    print(f"ERROR: {auth_path} not found. Run tools/lib/auth.py first.", file=sys.stderr)
    sys.exit(1)
  except Exception as e:
    print(f"ERROR reading {auth_path}: {e}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Athena RPC
# ---------------------------------------------------------------------------

def athena_call(dongle_id: str, token: str, method: str, params: list | dict, timeout: int = 60) -> dict:
  """Issue a JSON-RPC 2.0 call to the device via athena. Returns the full response dict."""
  url = f"{ATHENA_HOST}/{dongle_id}"
  payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
  try:
    resp = requests.post(
      url,
      json=payload,
      headers={"Authorization": f"JWT {token}", "Content-Type": "application/json"},
      timeout=timeout,
    )
  except requests.exceptions.Timeout:
    return {"error": {"code": -32000, "message": f"athena request timed out after {timeout}s"}}
  except requests.exceptions.ConnectionError as e:
    return {"error": {"code": -32000, "message": f"athena connection error: {e}"}}

  if resp.status_code == 404:
    return {"error": {"code": -32000, "message": "device not found or offline"}}
  if resp.status_code == 403:
    return {"error": {"code": -32000, "message": "access denied — wrong dongle_id or bad token"}}
  if resp.status_code != 200:
    return {"error": {"code": -32000, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}}

  try:
    return resp.json()
  except Exception as e:
    return {"error": {"code": -32000, "message": f"bad JSON response: {e}"}}


def list_device_files(dongle_id: str, token: str) -> list[str] | None:
  """Returns all file paths under log_root on the device, relative to log_root. None on error."""
  result = athena_call(dongle_id, token, "listDataDirectory", [""], timeout=120)
  if "error" in result:
    log_msg(f"ERROR: listDataDirectory failed: {result['error'].get('message', result['error'])}")
    return None
  files = result.get("result")
  if not isinstance(files, list):
    log_msg(f"ERROR: unexpected listDataDirectory response: {type(files)}")
    return None
  return files


def dispatch_uploads(dongle_id: str, token: str, files_data: list[dict]) -> bool:
  """
  Call uploadFilesToUrls on the device via athena.
  files_data: list of {fn, url, headers, allow_cellular} dicts.
  Returns True if the RPC was accepted.
  """
  # JSON-RPC positional params: wrap list so it maps to the single `files_data` argument
  result = athena_call(dongle_id, token, "uploadFilesToUrls", [files_data], timeout=60)
  if "error" in result:
    log_msg(f"ERROR: uploadFilesToUrls failed: {result['error'].get('message', result['error'])}")
    return False
  return True


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def get_routes(dongle_id: str, token: str) -> list[dict]:
  """Fetch recent routes from /v1/devices/{dongle_id}/routes. Returns [] on error."""
  url = f"{API_HOST}/v1/devices/{dongle_id}/routes"
  try:
    resp = requests.get(url, headers={"Authorization": f"JWT {token}"}, timeout=30)
  except Exception as e:
    log_msg(f"ERROR: routes request failed: {e}")
    return []

  if resp.status_code != 200:
    log_msg(f"ERROR: GET /routes returned {resp.status_code}: {resp.text[:200]}")
    return []
  try:
    return resp.json()
  except Exception:
    return []


def route_type_uploaded(fullname: str, token: str, file_type: str) -> bool:
  """
  Check if files of the given type for a route are already on the server.
  Returns True if the corresponding API field is non-empty.
  """
  url = f"{API_HOST}/v1/route/{fullname}/files"
  try:
    resp = requests.get(url, headers={"Authorization": f"JWT {token}"}, timeout=30)
  except Exception:
    return False

  if resp.status_code != 200:
    return False
  try:
    data = resp.json()
    field = API_FIELD[file_type]
    return bool(data.get(field))
  except Exception:
    return False


def fetch_upload_urls(dongle_id: str, token: str, paths: list[str]) -> dict[str, dict]:
  """
  Batch-fetch presigned upload URLs via POST /v1/{dongle_id}/upload_urls/.
  Returns dict mapping path -> {url, headers}.
  """
  try:
    resp = requests.post(
      f"{API_HOST}/v1/{dongle_id}/upload_urls/",
      json={"expiry_days": UPLOAD_EXPIRY_DAYS, "paths": paths},
      headers={"Authorization": f"JWT {token}", "Content-Type": "application/json"},
      timeout=30,
    )
  except Exception as e:
    log_msg(f"ERROR: upload_urls request failed: {e}")
    return {}

  if resp.status_code in (401, 403):
    log_msg(f"ERROR: auth failed ({resp.status_code}) — is your token valid?")
    return {}
  if resp.status_code != 200:
    log_msg(f"ERROR: upload_urls returned {resp.status_code}: {resp.text[:200]}")
    return {}

  try:
    data = resp.json()
  except Exception as e:
    log_msg(f"ERROR: bad upload_urls response: {e}")
    return {}

  if isinstance(data, list):
    return {paths[i]: item for i, item in enumerate(data) if i < len(paths)}
  elif isinstance(data, dict):
    return data
  else:
    log_msg(f"ERROR: unexpected upload_urls format: {type(data)}")
    return {}


# ---------------------------------------------------------------------------
# File discovery and key normalization
# ---------------------------------------------------------------------------

def upload_key(rel_path: str) -> str:
  """
  Normalize a device-relative path to its upload key.
  Uncompressed 'rlog' -> 'rlog.zst' (device compresses on the fly).
  All other files are uploaded as-is.
  """
  if os.path.basename(rel_path) == 'rlog':
    return rel_path + '.zst'
  return rel_path


def route_hash_from_segment(segment_dir: str) -> str | None:
  """
  '00000007--354981eb2f--0' -> '00000007--354981eb2f', or None if not a segment dir.
  """
  parts = segment_dir.rsplit('--', 1)
  if len(parts) == 2 and parts[1].isdigit():
    return parts[0]
  return None


def find_missing_files(
  device_files: list[str],
  dongle_id: str,
  token: str,
  file_types: set[str],
  route_filter: str | None,
) -> list[tuple[str, str]]:
  """
  Discover files of the requested types on the device that haven't been uploaded.

  Returns list of (rel_path, upload_key) tuples:
    rel_path   — path relative to log_root (fn for uploadFilesToUrls)
    upload_key — path used to request the presigned URL
  """
  # Build the set of filenames to match for the requested types
  wanted_filenames: set[str] = {fn for ft in file_types for fn in FILE_TYPES[ft]}

  # Group matching files by (route_hash, file_type)
  routes_on_device: dict[str, dict[str, list[tuple[str, str]]]] = {}
  # routes_on_device[route_hash][file_type] = [(rel_path, key), ...]

  for rel_path in device_files:
    parts = rel_path.split('/', 1)
    if len(parts) != 2:
      continue
    segment_dir, filename = parts
    if filename not in wanted_filenames:
      continue

    route_hash = route_hash_from_segment(segment_dir)
    if route_hash is None:
      continue

    if route_filter and route_hash != route_filter:
      continue

    # Determine which file_type this filename belongs to
    file_type = next((ft for ft, names in FILE_TYPES.items() if filename in names), None)
    if file_type is None:
      continue

    key = upload_key(rel_path)
    routes_on_device.setdefault(route_hash, {}).setdefault(file_type, []).append((rel_path, key))

  if not routes_on_device:
    return []

  # Build fullname map from API
  api_routes = get_routes(dongle_id, token)
  fullname_by_hash: dict[str, str] = {}
  for route in api_routes:
    fullname = route.get('fullname', '')
    if '|' in fullname:
      _, route_hash = fullname.split('|', 1)
      fullname_by_hash[route_hash] = fullname

  # For each route + type, skip if already uploaded
  missing: list[tuple[str, str]] = []
  for route_hash in sorted(routes_on_device):
    fullname = fullname_by_hash.get(route_hash)
    for file_type, files in routes_on_device[route_hash].items():
      if fullname and route_type_uploaded(fullname, token, file_type):
        log_msg(f"Route {route_hash} [{file_type}]: already uploaded, skipping")
        continue
      missing.extend(files)

  return missing


# ---------------------------------------------------------------------------
# Core upload orchestration
# ---------------------------------------------------------------------------

def run_once(
  dongle_id: str,
  token: str,
  file_types: set[str],
  route_filter: str | None,
  dry_run: bool,
  allow_cellular: bool,
) -> tuple[int, int]:
  """
  Perform one upload pass. Returns (enqueued_count, failed_count).
  """
  types_label = ', '.join(sorted(file_types))
  log_msg(f"Scanning device {dongle_id} for missing files [{types_label}]...")
  device_files = list_device_files(dongle_id, token)
  if device_files is None:
    return 0, 1

  log_msg(f"Device has {len(device_files)} total files under log_root")

  missing = find_missing_files(device_files, dongle_id, token, file_types, route_filter)
  if not missing:
    log_msg("All files already uploaded")
    return 0, 0

  log_msg(f"Found {len(missing)} file(s) to upload")

  if dry_run:
    for rel_path, key in missing:
      log_msg(f"  DRY-RUN: would upload {key}")
    return len(missing), 0

  # Batch-fetch presigned upload URLs
  keys = [key for _, key in missing]
  url_map = fetch_upload_urls(dongle_id, token, keys)
  if not url_map:
    return 0, len(missing)

  # Build uploadFilesToUrls payload
  files_data = []
  failed = 0
  for rel_path, key in missing:
    entry = url_map.get(key)
    if entry is None:
      log_msg(f"  ERROR: no upload URL returned for {key}")
      failed += 1
      continue
    files_data.append({
      "fn": rel_path,
      "url": entry['url'],
      "headers": entry['headers'],
      "allow_cellular": allow_cellular,
    })

  if not files_data:
    return 0, failed

  if dispatch_uploads(dongle_id, token, files_data):
    log_msg(f"  Enqueued {len(files_data)} upload(s) on device")
    return len(files_data), failed
  else:
    return 0, len(files_data) + failed


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def parse_types(value: str) -> set[str]:
  if value == 'all':
    return ALL_TYPES
  types = {t.strip() for t in value.split(',')}
  invalid = types - ALL_TYPES
  if invalid:
    raise argparse.ArgumentTypeError(
      f"Unknown type(s): {', '.join(sorted(invalid))}. Valid: {', '.join(sorted(ALL_TYPES))}, all"
    )
  return types


def main() -> int:
  parser = argparse.ArgumentParser(
    description="Upload files from a comma device to comma servers (laptop orchestration)",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__,
  )
  parser.add_argument("dongle_id", help="Device dongle ID (e.g. 79f534ddd71804c0)")
  parser.add_argument("--route", metavar="ROUTE_HASH",
                      help="Limit to a specific route (e.g. 00000007--354981eb2f)")
  parser.add_argument("--types", default="rlog", metavar="TYPE[,TYPE...]",
                      help="File types to upload: rlog, fcamera, dcamera, ecamera, all (default: rlog)")
  parser.add_argument("--dry-run", action="store_true",
                      help="Show what would be uploaded without uploading")
  parser.add_argument("--auto", action="store_true",
                      help="Daemon mode: repeat upload scan on --interval")
  parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, metavar="SECONDS",
                      help=f"Seconds between scans in --auto mode (default: {DEFAULT_INTERVAL})")
  parser.add_argument("--allow-cellular", action="store_true",
                      help="Allow uploads over metered connections")

  args = parser.parse_args()

  try:
    file_types = parse_types(args.types)
  except argparse.ArgumentTypeError as e:
    parser.error(str(e))

  token = get_token()

  if args.auto:
    shutdown = False

    def handle_signal(sig, frame):
      nonlocal shutdown
      log_msg("Shutting down...")
      shutdown = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log_msg(f"Auto-upload daemon started (interval: {args.interval}s, types: {args.types})")
    if args.dry_run:
      log_msg("  (dry-run mode)")

    while not shutdown:
      try:
        enqueued, failed = run_once(
          args.dongle_id, token, file_types, args.route, args.dry_run, args.allow_cellular
        )
        log_msg(f"Pass complete: {enqueued} enqueued, {failed} failed")
      except Exception as e:
        log_msg(f"ERROR: unexpected error in upload pass: {e}")

      if not shutdown:
        log_msg(f"Sleeping {args.interval}s...")
        for _ in range(args.interval):
          if shutdown:
            break
          time.sleep(1)

    log_msg("Auto-upload daemon stopped")
    return 0

  else:
    enqueued, failed = run_once(
      args.dongle_id, token, file_types, args.route, args.dry_run, args.allow_cellular
    )
    log_msg(f"Done: {enqueued} enqueued, {failed} failed")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
  sys.exit(main())
