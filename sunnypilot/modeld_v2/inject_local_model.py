"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Registers locally installed tinygrad .pkl model files as a bundle in ModelManager_ModelsCache
and optionally sets it as the active bundle.

Usage:
  python inject_local_model.py <short_name> [--set-active]

Example:
  python inject_local_model.py opmlocal --set-active
"""

import argparse
import json
import sys

from openpilot.common.params import Params
from openpilot.system.hardware.hw import Paths
from openpilot.sunnypilot.models.helpers import CURRENT_SELECTOR_VERSION

# Maps artifact prefix → model type string (as used in the JSON schema)
MODEL_TYPE_MAP: dict[str, str] = {
  "driving_vision": "vision",
  "driving_off_policy": "offPolicy",
  "driving_on_policy": "onPolicy",
  "driving_policy": "policy",
}

RUNNER_TINYGRAD = 1
TINYGRAD_SUFFIX = "_tinygrad.pkl"
METADATA_SUFFIX = "_metadata.pkl"


def _find_artifacts(short_name: str, model_root: str) -> list[dict]:
  """
  Scans model_root for tinygrad artifacts matching the given short_name.

  Returns a list of model dicts (artifact + metadata + type) suitable for
  inclusion in a bundle JSON.
  """
  import os
  models = []
  for prefix, type_str in MODEL_TYPE_MAP.items():
    tinygrad_filename = f"{prefix}_{short_name}{TINYGRAD_SUFFIX}"
    metadata_filename = f"{prefix}_{short_name}{METADATA_SUFFIX}"
    tinygrad_path = os.path.join(model_root, tinygrad_filename)
    metadata_path = os.path.join(model_root, metadata_filename)

    if not os.path.exists(tinygrad_path):
      continue
    if not os.path.exists(metadata_path):
      print(f"WARNING: metadata file not found for {tinygrad_filename}, skipping", file=sys.stderr)
      continue

    models.append({
      "type": type_str,
      "artifact": {
        "file_name": tinygrad_filename,
        "download_uri": {"url": "", "sha256": ""},
      },
      "metadata": {
        "file_name": metadata_filename,
        "download_uri": {"url": "", "sha256": ""},
      },
    })

  return models


def _build_bundle(short_name: str, models: list[dict], index: int = 9000) -> dict:
  """Constructs a bundle dict that ModelParser._parse_bundle expects."""
  return {
    "index": index,
    "short_name": short_name,
    "display_name": f"Local ({short_name})",
    "models": models,
    "generation": 1,
    "environment": "tinygrad",
    "runner": RUNNER_TINYGRAD,
    "is_20hz": False,
    "minimum_selector_version": CURRENT_SELECTOR_VERSION,
    "overrides": {},
    "ref": short_name,
  }


def inject(short_name: str, set_active: bool) -> None:
  model_root = Paths.model_root()
  params = Params()

  models = _find_artifacts(short_name, model_root)
  if not models:
    print(f"ERROR: No tinygrad artifacts found for short_name='{short_name}' in {model_root}", file=sys.stderr)
    sys.exit(1)

  print(f"Found {len(models)} model(s): {[m['type'] for m in models]}")

  bundle = _build_bundle(short_name, models)

  # Merge into existing cache (or start fresh)
  raw = params.get("ModelManager_ModelsCache")
  if raw:
    if isinstance(raw, (bytes, str)):
      cache = json.loads(raw) if isinstance(raw, str) else raw
    else:
      cache = raw
  else:
    cache = {"bundles": []}

  # Replace any existing bundle with the same short_name
  existing = [b for b in cache.get("bundles", []) if b.get("short_name") != short_name]
  existing.append(bundle)
  cache["bundles"] = existing

  params.put("ModelManager_ModelsCache", cache)
  print(f"Registered bundle '{short_name}' in ModelManager_ModelsCache ({len(existing)} total bundles)")

  if set_active:
    # Store the parsed bundle dict (keyed as capnp expects)
    from openpilot.sunnypilot.models.fetcher import ModelParser
    parsed = ModelParser._parse_bundle(bundle)
    params.put("ModelManager_ActiveBundle", parsed.to_dict())
    print(f"Set '{short_name}' as active bundle (ModelManager_ActiveBundle)")


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description="Inject a local tinygrad model bundle into ModelManager params.")
  parser.add_argument("short_name", help="Short name suffix used in the model filenames (e.g. 'opmlocal')")
  parser.add_argument("--set-active", action="store_true", help="Also set this bundle as the active bundle")
  args = parser.parse_args()

  inject(args.short_name, args.set_active)
