"""
analyze_log.py — PicoBoard 串口日志综合分析工具

用法:
    python scripts/analyze_log.py logs/serial_20260503_055551.jsonl

输出:
    - CONFIG 摘要（button 参数、防抖设置）
    - 稳定边沿统计（press/release 计数、hold 时长）
    - RAW bit 翻转时间线（可指定任意 idx/mask）
    - 对齐率（legacy 协议包评分）
    - 空闲误触发统计
"""

import json
import sys
import argparse
from datetime import datetime
from pathlib import Path


FMT = '%Y-%m-%dT%H:%M:%S.%f'


def dt(s):
    return datetime.strptime(s, FMT)


def load(path):
    rows = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def legacy_score(packet):
    if len(packet) < 18 or packet[0] != 0xF8 or packet[1] != 0x04:
        return -1
    s = 0
    if packet[8] in (0x9F, 0x98):
        s += 2
    if (packet[8] == 0x9F and packet[9] == 0x7F) or (packet[8] == 0x98 and packet[9] == 0x00):
        s += 2
    elif packet[9] in (0x7F, 0x00):
        s += 1
    if packet[12] in (0xAE, 0xAF):
        s += 2
    elif 0xA0 <= packet[12] <= 0xAF:
        s += 1
    if 0xB0 <= packet[14] <= 0xB4:
        s += 2
    elif 0xB0 <= packet[14] <= 0xBF:
        s += 1
    if packet[16] == 0xBF and packet[17] == 0x7F:
        s += 2
    elif packet[17] == 0x7F:
        s += 1
    return s


def alignment_ratio(rows):
    stream = []
    for r in rows:
        if r.get('event') == 'rx':
            try:
                stream.extend(int(t, 16) for t in r['data']['hex'].split())
            except Exception:
                pass
    cand = 0
    i = 0
    while i + 18 <= len(stream):
        if not (stream[i] == 0xF8 and stream[i + 1] == 0x04):
            i += 1
            continue
        if legacy_score(stream[i:i + 18]) >= 7:
            cand += 1
            i += 18
        else:
            i += 1
    return round(cand * 18 / len(stream), 4) if stream else 0.0


def raw_transitions(rows, idx, mask, invert=False):
    series = []
    for r in rows:
        if r.get('event') != 'rx':
            continue
        toks = r['data']['hex'].split()
        if idx >= len(toks):
            continue
        raw = 1 if (int(toks[idx], 16) & mask) else 0
        bit = (1 - raw) if invert else raw
        series.append((r['ts'], bit))
    trans = []
    if series:
        prev = series[0][1]
        for ts, v in series[1:]:
            if v != prev:
                trans.append((ts, v))
                prev = v
    return trans


def analyze(path, raw_idx=None, raw_mask=0x02, raw_invert=None):
    rows = load(path)
    start = dt(rows[0]['ts'])

    def sec(s):
        return round((dt(s) - start).total_seconds(), 3)

    cfg_row = next((r for r in rows if r.get('event') == 'config'), None)
    edges = [r for r in rows if r.get('event') == 'button_edge']
    end = dt(rows[-1]['ts'])
    duration = round((end - start).total_seconds(), 3)

    print(f"{'='*60}")
    print(f"FILE: {Path(path).name}   duration={duration}s")
    print(f"{'='*60}")

    if cfg_row:
        d = cfg_row['data']
        btn_idx = d.get('legacy_map', {}).get('button', '?')
        mask_val = d.get('legacy_button_mask', 0x02)
        inv = d.get('legacy_button_invert', False)
        p_ms = d.get('button_press_debounce_ms', '?')
        p_f = d.get('button_press_debounce_frames', '?')
        r_ms = d.get('button_release_debounce_ms', '?')
        r_f = d.get('button_release_debounce_frames', '?')
        print(f"\n[CONFIG]")
        print(f"  button_idx={btn_idx}  mask={hex(mask_val)}  invert={inv}")
        print(f"  press={p_ms}ms/{p_f}f   release={r_ms}ms/{r_f}f")
        if raw_idx is None:
            raw_idx = btn_idx
        if raw_invert is None:
            raw_invert = inv
        raw_mask = mask_val

    # Stable edges
    presses = [e for e in edges if e['data']['edge'] == 'press']
    releases = [e for e in edges if e['data']['edge'] == 'release']
    ri = 0
    hold_ms = []
    for p0 in presses:
        t0 = dt(p0['ts'])
        while ri < len(releases) and dt(releases[ri]['ts']) <= t0:
            ri += 1
        if ri < len(releases):
            hold_ms.append((dt(releases[ri]['ts']) - t0).total_seconds() * 1000)
            ri += 1

    print(f"\n[STABLE EDGES]")
    print(f"  presses={len(presses)}  releases={len(releases)}")
    if edges:
        for e in edges:
            print(f"    t+{sec(e['ts']):>7}s  {e['data']['edge']}")
    if hold_ms:
        print(f"  hold_ms  min={round(min(hold_ms),1)}  avg={round(sum(hold_ms)/len(hold_ms),1)}  max={round(max(hold_ms),1)}")

    # Raw transitions
    if raw_idx is not None:
        trans = raw_transitions(rows, raw_idx, raw_mask, raw_invert)
        print(f"\n[RAW bit={raw_idx} mask={hex(raw_mask)} invert={raw_invert}]  transitions={len(trans)}")
        for ts, v in trans:
            label = '(press)' if v == 1 else '(release)'
            print(f"    t+{sec(ts):>7}s  -> {v}  {label}")

        # Idle false triggers = edges before first raw press
        press_times = [ts for ts, v in trans if v == 1]
        if press_times:
            first_press_s = sec(press_times[0])
            false = [e for e in edges if sec(e['ts']) < first_press_s]
            print(f"\n  first_raw_press_s={first_press_s}")
            ok = "[OK]" if len(false) == 0 else "[!!]"
            print(f"  false_edges_before_first_press={len(false)} {ok}")

        # Frame intervals
        rx_times = []
        for r in rows:
            if r.get('event') == 'rx':
                toks = r['data']['hex'].split()
                if raw_idx < len(toks):
                    rx_times.append(dt(r['ts']))
        if len(rx_times) > 1:
            intervals = [(rx_times[i+1] - rx_times[i]).total_seconds()*1000 for i in range(len(rx_times)-1)]
            print(f"\n  frame_interval_ms  min={round(min(intervals),1)}  avg={round(sum(intervals)/len(intervals),1)}  max={round(max(intervals),1)}")

    # Alignment ratio
    ratio = alignment_ratio(rows)
    ok = "[OK]" if ratio >= 0.995 else "[WARN]"
    print(f"\n[ALIGNMENT]  alignment_ratio={ratio} {ok}")

    # Summary verdict
    n_press = len(presses)
    n_raw_press = sum(1 for ts, v in trans if v == 1) if raw_idx is not None else '?'
    n_false = len([e for e in edges if sec(e['ts']) < sec(press_times[0])]) if raw_idx is not None and press_times else 0
    print(f"\n[VERDICT]")
    if n_raw_press == '?' or n_raw_press == 0:
        det_rate = 'N/A'
    else:
        det_rate = f"{round(n_press / n_raw_press * 100, 1)}%"
    print(f"  raw_presses={n_raw_press}  stable_presses={n_press}  detection_rate={det_rate}")
    print(f"  false_triggers={n_false}  alignment={ratio}")


def main():
    parser = argparse.ArgumentParser(description="Analyze PicoBoard serial JSONL log")
    parser.add_argument("logfile", help="Path to .jsonl log file")
    parser.add_argument("--idx", type=int, default=None, help="Override raw bit index to inspect")
    parser.add_argument("--mask", default=None, help="Override raw bit mask (hex or decimal)")
    parser.add_argument("--invert", action=argparse.BooleanOptionalAction, default=None, help="Override invert flag")
    args = parser.parse_args()
    mask = int(args.mask, 0) if args.mask else 0x02
    analyze(args.logfile, raw_idx=args.idx, raw_mask=mask, raw_invert=args.invert)


if __name__ == '__main__':
    main()
