"""
scan_candidates.py — Button 候选位离线扫描评分工具

用法:
    python scripts/scan_candidates.py logs/serial_20260503_054511.jsonl --expected 8

功能:
    - 对所有候选位（或指定范围）统计 transitions / rise / fall / ones_ratio
    - 按"最接近 expected 次按压"排序，自动推荐最优 idx
    - 打印每个候选位的原始翻转时间线，便于验证空闲期是否干净
"""

import json
import argparse
from pathlib import Path


def load(path):
    rows = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def scan(rows, candidates, mask, expected_presses):
    rx = [r for r in rows if r.get('event') == 'rx']

    stats = {idx: {'samples': 0, 'ones': 0, 'last': None, 'trans': 0, 'rise': 0, 'fall': 0, 'timeline': []}
             for idx in candidates}

    for r in rx:
        toks = r['data']['hex'].split()
        for idx in candidates:
            if idx >= len(toks):
                continue
            raw = 1 if (int(toks[idx], 16) & mask) else 0
            st = stats[idx]
            st['samples'] += 1
            st['ones'] += raw
            if st['last'] is None:
                st['last'] = raw
                continue
            if raw != st['last']:
                st['trans'] += 1
                if raw == 1:
                    st['rise'] += 1
                else:
                    st['fall'] += 1
                st['timeline'].append((r['ts'], raw))
                st['last'] = raw

    # For inverted candidates (ones% > 50%), swap rise/fall meaning
    print(f"\n{'='*70}")
    print(f"CANDIDATE SCAN  mask={hex(mask)}  expected_presses={expected_presses}")
    print(f"{'='*70}")
    print(f"{'idx':>4}  {'trans':>5}  {'rise':>5}  {'fall':>5}  {'ones%':>6}  "
          f"{'inv?':>5}  {'eff_rise':>8}  {'eff_fall':>8}  {'err':>5}  score")
    print('-' * 70)

    scored = []
    for idx in sorted(candidates):
        st = stats[idx]
        n = st['samples']
        ones_pct = round(st['ones'] / n * 100, 1) if n else 0
        inverted = ones_pct > 50  # idle=1 → active-low
        eff_rise = st['fall'] if inverted else st['rise']
        eff_fall = st['rise'] if inverted else st['fall']
        err = abs(eff_rise - expected_presses) + abs(eff_fall - expected_presses)
        scored.append((err, idx, st, ones_pct, inverted, eff_rise, eff_fall))

    scored.sort(key=lambda x: (x[0], x[1]))
    for err, idx, st, ones_pct, inverted, eff_rise, eff_fall in scored:
        marker = " ← BEST" if err == scored[0][0] and idx == scored[0][1] else ""
        inv_str = "yes" if inverted else "no"
        print(f"{idx:>4}  {st['trans']:>5}  {st['rise']:>5}  {st['fall']:>5}  {ones_pct:>5}%  "
              f"{inv_str:>5}  {eff_rise:>8}  {eff_fall:>8}  {err:>5}{marker}")

    # Detailed timeline for top 3 candidates
    print()
    for err, idx, st, ones_pct, inverted, eff_rise, eff_fall in scored[:3]:
        inv_str = "(inverted)" if inverted else "(normal)"
        print(f"--- idx={idx} {inv_str} ---")
        print(f"  timeline ({len(st['timeline'])} transitions):")
        for ts, v in st['timeline']:
            display = (1 - v) if inverted else v
            label = '(press)' if display == 1 else '(release)'
            print(f"    {ts}  raw={v}  effective={display}  {label}")
        print()

    return scored


def main():
    parser = argparse.ArgumentParser(description="Scan button candidate indices from serial JSONL log")
    parser.add_argument("logfile", help="Path to .jsonl log file")
    parser.add_argument("--candidates", default="7,9,11,12,15,17",
                        help="Comma-separated byte indices to scan (default: 7,9,11,12,15,17)")
    parser.add_argument("--mask", default="0x02", help="Bit mask (hex or decimal, default: 0x02)")
    parser.add_argument("--expected", type=int, default=8,
                        help="Expected number of physical presses (default: 8)")
    args = parser.parse_args()

    candidates = [int(x.strip()) for x in args.candidates.split(',') if x.strip()]
    mask = int(args.mask, 0)
    rows = load(args.logfile)
    scan(rows, candidates, mask, args.expected)


if __name__ == '__main__':
    main()
