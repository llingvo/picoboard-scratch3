"""
simulate_debounce.py — 离线仿真防抖逻辑，验证参数效果

用法:
    python scripts/simulate_debounce.py logs/serial_20260503_055212.jsonl

功能:
    - 在离线日志上重放防抖状态机，无需重新硬件采集
    - 对比多组 press_ms / press_frames 参数输出边沿数
    - 帮助快速确定最优阈值

典型用途:
    帧间隔 ~156ms，短按 ~154ms，可通过仿真验证 press_ms=0 vs 80 的差异。
"""

import json
import argparse
from datetime import datetime
from pathlib import Path


FMT = '%Y-%m-%dT%H:%M:%S.%f'


def dt(s):
    return datetime.strptime(s, FMT)


def load_frames(path, idx, mask, invert):
    rows = [json.loads(x) for x in Path(path).read_text(encoding='utf-8').splitlines() if x.strip()]
    frames = []
    for r in rows:
        if r.get('event') != 'rx':
            continue
        toks = r['data']['hex'].split()
        if idx >= len(toks):
            continue
        raw = 1 if (int(toks[idx], 16) & mask) else 0
        bit = (1 - raw) if invert else raw
        frames.append((r['ts'], bit))
    return frames


def simulate(frames, press_ms, press_frames, release_ms, release_frames, label=""):
    stable = None
    cand = None
    cand_since = None
    cand_f = 0
    edges = []
    start = dt(frames[0][0])

    for ts, bit in frames:
        now = (dt(ts) - start).total_seconds()
        if stable is None:
            stable = bit
            cand = bit
            cand_since = now
            cand_f = 1
            continue
        if bit != cand:
            cand = bit
            cand_since = now
            cand_f = 1
        else:
            cand_f += 1
        tgt_ms = press_ms if cand == 1 else release_ms
        tgt_f = press_frames if cand == 1 else release_frames
        elapsed_ms = (now - cand_since) * 1000
        if cand != stable and elapsed_ms >= tgt_ms and cand_f >= tgt_f:
            kind = 'press' if cand == 1 else 'release'
            edges.append({
                'ts': ts,
                'kind': kind,
                'lag_ms': round(elapsed_ms, 1),
                'frames': cand_f,
                'offset_s': round(now, 3),
            })
            stable = cand
            cand_f = 0

    n_press = sum(1 for e in edges if e['kind'] == 'press')
    n_release = sum(1 for e in edges if e['kind'] == 'release')
    tag = label or f"press={press_ms}ms/{press_frames}f  release={release_ms}ms/{release_frames}f"
    print(f"\n[{tag}]  press={n_press}  release={n_release}")
    for e in edges:
        print(f"  t+{e['offset_s']:>7}s  {e['kind']:<8}  lag={e['lag_ms']}ms  frames={e['frames']}")
    return edges


def main():
    parser = argparse.ArgumentParser(description="Offline debounce simulation for PicoBoard serial logs")
    parser.add_argument("logfile", help="Path to .jsonl log file")
    parser.add_argument("--idx", type=int, default=None, help="Button byte index (default: from config)")
    parser.add_argument("--mask", default=None, help="Bit mask (default: from config)")
    parser.add_argument("--invert", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--press-ms", type=int, default=None,
                        help="Override press debounce ms (default: test several)")
    parser.add_argument("--press-frames", type=int, default=1)
    parser.add_argument("--release-ms", type=int, default=180)
    parser.add_argument("--release-frames", type=int, default=2)
    args = parser.parse_args()

    # Read config defaults from log
    rows = [json.loads(x) for x in Path(args.logfile).read_text(encoding='utf-8').splitlines() if x.strip()]
    cfg = next((r['data'] for r in rows if r.get('event') == 'config'), {})
    idx = args.idx if args.idx is not None else cfg.get('legacy_map', {}).get('button', 9)
    mask = int(args.mask, 0) if args.mask else cfg.get('legacy_button_mask', 0x02)
    invert = args.invert if args.invert is not None else cfg.get('legacy_button_invert', True)

    print(f"Source: {Path(args.logfile).name}")
    print(f"Reading: idx={idx}  mask={hex(mask)}  invert={invert}")

    frames = load_frames(args.logfile, idx, mask, invert)

    # Frame interval stats
    if len(frames) > 1:
        intervals = [(dt(frames[i+1][0]) - dt(frames[i][0])).total_seconds() * 1000
                     for i in range(len(frames) - 1)]
        print(f"\nFrame interval (ms):  min={round(min(intervals),1)}  "
              f"avg={round(sum(intervals)/len(intervals),1)}  max={round(max(intervals),1)}")

    # Raw press hold durations
    raw_holds = []
    prev = frames[0][1]
    t0 = frames[0][0]
    for ts, bit in frames[1:]:
        if bit != prev:
            if prev == 1:
                raw_holds.append(round((dt(ts) - dt(t0)).total_seconds() * 1000, 1))
            t0 = ts
            prev = bit
    if raw_holds:
        print(f"Raw press hold (ms):  {raw_holds}")
        print(f"  min={min(raw_holds)}  avg={round(sum(raw_holds)/len(raw_holds),1)}  max={max(raw_holds)}")

    print(f"\n{'='*60}")
    print("DEBOUNCE SIMULATION")
    print('='*60)

    if args.press_ms is not None:
        # Single run with specified params
        simulate(frames, args.press_ms, args.press_frames, args.release_ms, args.release_frames)
    else:
        # Compare several press thresholds
        for p_ms in [0, 50, 80, 120, 160]:
            simulate(frames, p_ms, 1, args.release_ms, args.release_frames)


if __name__ == '__main__':
    main()
