#!/usr/bin/env python3
"""
Hyundai TechInfo SCC ECU Firmware Research Tool
Goal: Find and catalog SCC radar ECU firmware updates for Hyundai/Kia/Genesis 2020+
"""

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

RESEARCH_DIR = Path(__file__).parent
STATE_FILE = RESEARCH_DIR / "crawl_state.json"
DOCS_DIR = RESEARCH_DIR / "docs"
DOCS_DIR.mkdir(exist_ok=True)

# SCC-related search keywords
SEARCH_TERMS = [
  "SCC",           # Smart Cruise Control
  "SCC ECU",       # SCC ECU specifically
  "SCC software",
  "radar ECU",     # Radar ECU
  "FCA ECU",       # Forward Collision Avoidance
  "RSCC",          # Radar SCC
  "ADAS",          # Advanced Driver Assistance
  "cruise control software update",
  "SCC update",
  "radar software update",
]

# Known SCC/radar ECU part number prefixes (Hyundai/Kia)
# 95900-xxx = radar sensor ECU family
# 96400-xxx = camera/vision systems
# 95210-xxx = radar
PART_PREFIXES = ["95900", "95910", "95920", "95930", "95940", "95950",
                  "96420", "96400", "96410"]

def load_state():
  if STATE_FILE.exists():
    with open(STATE_FILE) as f:
      return json.load(f)
  return {"found_bulletins": [], "downloaded_docs": [], "status": "in_progress"}

def save_state(state):
  with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=2)
  print(f"[{datetime.now().isoformat()}] State saved: {len(state['found_bulletins'])} bulletins found")

def add_bulletin(state, bulletin_id, title, url, doc_type, models=None, notes=None):
  entry = {
    "id": bulletin_id,
    "title": title,
    "url": url,
    "type": doc_type,
    "models": models or [],
    "notes": notes or "",
    "found_at": datetime.now().isoformat(),
  }
  # Deduplicate by ID
  if not any(b["id"] == bulletin_id for b in state["found_bulletins"]):
    state["found_bulletins"].append(entry)
    print(f"  [+] {doc_type}: {bulletin_id} - {title[:80]}")
    save_state(state)
  return entry

def add_doc(state, filename, source_url, bulletin_id=None):
  entry = {
    "filename": filename,
    "source_url": source_url,
    "bulletin_id": bulletin_id,
    "downloaded_at": datetime.now().isoformat(),
  }
  if not any(d["filename"] == filename for d in state["downloaded_docs"]):
    state["downloaded_docs"].append(entry)
    save_state(state)

def summarize(state):
  print("\n" + "="*70)
  print("SCC ECU FIRMWARE RESEARCH SUMMARY")
  print("="*70)
  print(f"Total bulletins found: {len(state['found_bulletins'])}")
  print(f"Documents downloaded: {len(state['downloaded_docs'])}")
  print()

  by_type = {}
  for b in state["found_bulletins"]:
    by_type.setdefault(b["type"], []).append(b)

  for doc_type, bulletins in by_type.items():
    print(f"\n{doc_type} ({len(bulletins)}):")
    for b in bulletins:
      print(f"  [{b['id']}] {b['title'][:70]}")
      if b.get("models"):
        print(f"    Models: {', '.join(b['models'])}")
      if b.get("notes"):
        print(f"    Notes: {b['notes']}")

  print("\nDownloaded docs:")
  for d in state["downloaded_docs"]:
    print(f"  {d['filename']} (from {d['bulletin_id'] or 'unknown'})")

if __name__ == "__main__":
  state = load_state()
  summarize(state)
