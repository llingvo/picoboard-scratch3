import argparse
import json
import math
from collections import Counter
from pathlib import Path


def load_rx_packets(path: Path):
    packets = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") != "rx":
                continue
            hex_str = rec.get("data", {}).get("hex", "")
            if not hex_str:
                continue
            try:
                vals = [int(part, 16) for part in hex_str.split()]
            except ValueError:
                continue
            if vals:
                packets.append(vals)
    return packets


def filter_common_length_packets(packets):
    lengths = Counter(len(p) for p in packets)
    if not lengths:
        return [], None, lengths
    common_len, _ = lengths.most_common(1)[0]
    filtered = [p for p in packets if len(p) == common_len]
    return filtered, common_len, lengths


def mean(nums):
    return sum(nums) / len(nums) if nums else 0.0


def stddev(nums):
    if not nums:
        return 0.0
    m = mean(nums)
    return math.sqrt(sum((x - m) ** 2 for x in nums) / len(nums))


def analyze_columns(packets):
    width = len(packets[0])
    cols = []
    for i in range(width):
        series = [row[i] for row in packets]
        uniq = sorted(set(series))
        deltas = [abs(series[j] - series[j - 1]) for j in range(1, len(series))]
        large_jumps = sum(1 for d in deltas if d >= 20)
        changes = sum(1 for d in deltas if d != 0)
        cols.append(
            {
                "idx": i,
                "min": min(series),
                "max": max(series),
                "range": max(series) - min(series),
                "unique": len(uniq),
                "changes": changes,
                "std": stddev(series),
                "avg_delta": mean(deltas) if deltas else 0.0,
                "large_jump_ratio": (large_jumps / len(deltas)) if deltas else 0.0,
            }
        )
    return cols


def score_slider_candidate(col, max_range, max_unique, max_changes):
    if max_range == 0 or max_unique == 0 or max_changes == 0:
        return 0.0
    range_n = col["range"] / max_range
    unique_n = col["unique"] / max_unique
    changes_n = col["changes"] / max_changes
    smooth_n = 1.0 - min(1.0, col["large_jump_ratio"] * 2.0)
    return 0.45 * range_n + 0.2 * unique_n + 0.2 * changes_n + 0.15 * smooth_n


def main():
    parser = argparse.ArgumentParser(description="Analyze serial RX logs and suggest likely sensor byte mappings")
    parser.add_argument("log_file", help="Path to serial JSONL log file")
    parser.add_argument("--top", type=int, default=8, help="How many candidate bytes to print")
    args = parser.parse_args()

    path = Path(args.log_file)
    if not path.exists():
        raise SystemExit(f"Log file not found: {path}")

    raw_packets = load_rx_packets(path)
    print(f"RX packet count: {len(raw_packets)}")
    if not raw_packets:
        raise SystemExit("No RX packets found")

    packets, common_len, lengths = filter_common_length_packets(raw_packets)
    print("Packet length distribution:")
    for k in sorted(lengths):
        print(f"  len={k}: {lengths[k]}")

    if not packets:
        raise SystemExit("No packets left after filtering by common length")

    print(f"Using common length={common_len}, sample_count={len(packets)}")
    cols = analyze_columns(packets)

    max_range = max(c["range"] for c in cols)
    max_unique = max(c["unique"] for c in cols)
    max_changes = max(c["changes"] for c in cols)

    for c in cols:
        c["score"] = score_slider_candidate(c, max_range, max_unique, max_changes)

    print("\nLikely structural/marker bytes (very stable):")
    stable = [c for c in cols if c["unique"] <= 2]
    stable = sorted(stable, key=lambda x: x["idx"])
    for c in stable:
        print(f"  byte[{c['idx']}]: min={c['min']} max={c['max']} unique={c['unique']}")

    print("\nTop slider candidates (higher score is better):")
    ranked = sorted(cols, key=lambda x: x["score"], reverse=True)
    for c in ranked[: args.top]:
        print(
            "  byte[{idx}] score={score:.3f} range={range} unique={unique} changes={changes} "
            "avg_delta={avg_delta:.2f} jump_ratio={large_jump_ratio:.2f} min={min} max={max}".format(**c)
        )

    print("\nAll byte stats:")
    for c in cols:
        print(
            "  byte[{idx}] min={min} max={max} range={range} unique={unique} "
            "changes={changes} std={std:.2f}".format(**c)
        )


if __name__ == "__main__":
    main()
