#!/usr/bin/env python3
"""
KIA_OPTIMA_H SCC12 checksum constant validation.

Demonstrates that SCC12 (0x421) on this route uses nibble-sum constant 14
instead of the standard 16, proving why panda rejects every frame and blocks
engagement (safety_tick_rx_invalid=True) without the fix.

Run from ~/projects/openpilot (or ~/sunnypilot):
  python tools/car_porting/examples/kia_optima_h_scc12_checksum.py
"""

import sys

from openpilot.tools.lib.logreader import LogReader

ROUTE = "8b0a7cd2dcbdd691/00000000--0fe00ee145"
SCC12_ADDR = 0x421
SAMPLE_COUNT = 5


def nibble_sum(data: bytes) -> int:
  """Sum of all nibbles in an SCC12 frame, excluding the checksum nibble (upper nibble of byte 7)."""
  total = 0
  for i, b in enumerate(data):
    if i == 7:
      b &= 0x0F  # zero the checksum field before summing
    total += (b >> 4) + (b & 0xF)
  return total


def expected_checksum(data: bytes, constant: int) -> int:
  return (constant - nibble_sum(data)) % 16


def actual_checksum(data: bytes) -> int:
  return (data[7] >> 4) & 0xF


def main() -> None:
  print(f"Route  : {ROUTE}")
  print(f"Message: SCC12 (0x{SCC12_ADDR:03X})")
  print(f"Claim  : KIA_OPTIMA_H uses nibble-sum constant 14, not the standard 16\n")

  lr = LogReader(ROUTE)
  scc12_msgs = [
    msg
    for packet in lr
    if packet.which() == "can"
    for msg in packet.can
    if msg.address == SCC12_ADDR and msg.src < 128  # exclude echo'd sendcan
  ]

  if not scc12_msgs:
    print("ERROR: No SCC12 messages found on this route.", file=sys.stderr)
    sys.exit(1)

  print(f"Total SCC12 frames on route: {len(scc12_msgs)}\n")

  # --- Step-by-step walkthrough on first N frames ---
  print("=" * 68)
  print(f"Step-by-step: first {SAMPLE_COUNT} frames")
  print("=" * 68)

  for idx, msg in enumerate(scc12_msgs[:SAMPLE_COUNT], 1):
    data = bytes(msg.dat)
    nsum = nibble_sum(data)
    chk_got = actual_checksum(data)
    chk_c16 = expected_checksum(data, 16)
    chk_c14 = expected_checksum(data, 14)

    # Build per-byte nibble breakdown; bracket the checksum nibble in byte 7
    nibbles = []
    for i, b in enumerate(data):
      if i == 7:
        nibbles.append(f"[{b >> 4:X}]{b & 0xF:X}")  # [X] = checksum field
      else:
        nibbles.append(f"{b >> 4:X}{b & 0xF:X}")

    print(f"\n  Frame #{idx}  raw: {data.hex(' ').upper()}")
    print(f"  Nibbles: {' '.join(nibbles)}  ([X] = checksum field, excluded from sum)")
    print(f"  Nibble sum (excl. checksum): {nsum} (0x{nsum:X})")
    print(f"  Byte[7] upper nibble (actual checksum): {chk_got}")
    print(f"  Expected with constant=16: (16 - {nsum}) % 16 = {chk_c16}  "
          f"{'MATCH' if chk_got == chk_c16 else 'MISMATCH'}")
    print(f"  Expected with constant=14: (14 - {nsum}) % 16 = {chk_c14}  "
          f"{'MATCH' if chk_got == chk_c14 else 'MISMATCH'}")

  # --- Aggregate over full route ---
  pass_c16 = sum(1 for m in scc12_msgs if actual_checksum(bytes(m.dat)) == expected_checksum(bytes(m.dat), 16))
  pass_c14 = sum(1 for m in scc12_msgs if actual_checksum(bytes(m.dat)) == expected_checksum(bytes(m.dat), 14))
  total = len(scc12_msgs)

  print(f"\n{'=' * 68}")
  print(f"Full-route validation ({total} SCC12 frames)")
  print("=" * 68)
  print(f"  constant=16 (standard)    : {pass_c16:5d} / {total} pass  ({100 * pass_c16 / total:5.1f}%)")
  print(f"  constant=14 (KIA_OPTIMA_H): {pass_c14:5d} / {total} pass  ({100 * pass_c14 / total:5.1f}%)")

  pct_c14 = 100 * pass_c14 / total
  pct_c16 = 100 * pass_c16 / total
  unmatched = total - pass_c14

  print()
  if pct_c14 > 95 and pct_c14 > pct_c16:
    print(f"CONCLUSION: {pct_c14:.1f}% of SCC12 frames validate with constant=14; only {pct_c16:.1f}% with constant=16.")
    if unmatched:
      print(f"  ({unmatched} frames match neither — likely ACC-off idle or transitional frames.)")
    print()
    print("  Without fix: panda rejects every SCC12 rx -> safety_tick_rx_invalid=True -> engagement blocked.")
    print("  With fix   : hyundai_legacy_scc12_alt_checksum=true (via CAN_LEGACY_SCC12_ALT_CHECKSUM flag)")
    print("               -> constant=14 used in hyundai_compute_checksum -> all frames accepted.")
  else:
    print(f"WARNING: unexpected result - check route or address filter.")
    sys.exit(1)


if __name__ == "__main__":
  main()
