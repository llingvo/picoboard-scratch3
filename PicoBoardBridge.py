import asyncio
import serial
import time
import json
import threading
import argparse
import os
from datetime import datetime
import websockets
from serial.tools import list_ports

# -----------------------------
class PicoBoardCore:
    DOC_FRAME_SIZE = 9
    DOC_SYNC_BYTE = 0x80
    LEGACY_PACKET_SIZE = 18
    LEGACY_MIN_SCORE = 7

    def __init__(
        self,
        protocol_mode="auto",
        legacy_map=None,
        legacy_button_mask=0x02,
        legacy_button_invert=False,
        button_scan_candidates=None,
        button_scan_mask=None,
    ):
        self.values = {
            "slider": 0,
            "light": 0,
            "sound": 0,
            "button": 0,
            "A": 0,
            "B": 0,
            "C": 0,
            "D": 0
        }
        self.protocol_mode = protocol_mode
        self.legacy_map = dict(legacy_map or {
            "slider": 3,
            "light": 13,
            "sound": 15,
            "button": 9,
            "A": 11,
            "B": 15,
            "C": 17,
            "D": 9,
        })
        self.legacy_button_mask = legacy_button_mask
        self.legacy_button_invert = legacy_button_invert
        self.button_scan_mask = legacy_button_mask if button_scan_mask is None else button_scan_mask
        self.active_protocol = None if protocol_mode == "auto" else protocol_mode
        self._doc_buffer = bytearray()
        self._legacy_buffer = bytearray()
        self.doc_frame_count = 0
        self.legacy_packet_count = 0
        # WebSocket 线程安全的稳定 button 值（覆盖 values["button"] 的原始噪声）
        self._stable_button = None
        self.button_scan_candidates = []
        self._button_scan_state = {}
        self._button_scan_events = []
        self.set_button_scan_candidates(button_scan_candidates)

    def set_button_scan_candidates(self, candidates):
        unique = []
        for idx in candidates or []:
            if 0 <= idx < self.LEGACY_PACKET_SIZE and idx not in unique:
                unique.append(idx)
        self.button_scan_candidates = unique
        self._button_scan_state = {
            idx: {
                "last": None,
                "samples": 0,
                "ones": 0,
                "transitions": 0,
                "rise": 0,
                "fall": 0,
            }
            for idx in self.button_scan_candidates
        }
        self._button_scan_events = []

    def _update_button_scan(self, packet):
        if not self.button_scan_candidates:
            return
        for idx in self.button_scan_candidates:
            state = self._button_scan_state.get(idx)
            if state is None:
                continue
            bit = 1 if (packet[idx] & self.button_scan_mask) else 0
            state["samples"] += 1
            state["ones"] += bit
            if state["last"] is None:
                state["last"] = bit
                continue
            if bit != state["last"]:
                state["transitions"] += 1
                if bit == 1:
                    state["rise"] += 1
                else:
                    state["fall"] += 1
                self._button_scan_events.append({
                    "idx": idx,
                    "bit": bit,
                    "byte": packet[idx],
                })
                state["last"] = bit

    def drain_button_scan_events(self):
        events = self._button_scan_events
        self._button_scan_events = []
        return events

    def get_button_scan_summary(self):
        summary = {}
        for idx, state in self._button_scan_state.items():
            samples = state["samples"]
            ones_ratio = (state["ones"] / samples) if samples else 0.0
            summary[str(idx)] = {
                "samples": samples,
                "ones_ratio": round(ones_ratio, 4),
                "transitions": state["transitions"],
                "rise": state["rise"],
                "fall": state["fall"],
                "last": state["last"],
            }
        return summary

    def _legacy_packet_score(self, packet):
        if len(packet) < self.LEGACY_PACKET_SIZE:
            return -1
        if packet[0] != 0xF8 or packet[1] != 0x04:
            return -1

        score = 0
        if packet[8] in (0x9F, 0x98):
            score += 2
        if (packet[8] == 0x9F and packet[9] == 0x7F) or (packet[8] == 0x98 and packet[9] == 0x00):
            score += 2
        elif packet[9] in (0x7F, 0x00):
            score += 1

        if packet[12] in (0xAE, 0xAF):
            score += 2
        elif 0xA0 <= packet[12] <= 0xAF:
            score += 1

        if 0xB0 <= packet[14] <= 0xB4:
            score += 2
        elif 0xB0 <= packet[14] <= 0xBF:
            score += 1

        if packet[16] == 0xBF and packet[17] == 0x7F:
            score += 2
        elif packet[17] == 0x7F:
            score += 1
        return score

    def _legacy_markers_match(self, packet):
        return self._legacy_packet_score(packet) >= self.LEGACY_MIN_SCORE

    def feed_bytes(self, data):
        if self.active_protocol == "doc":
            self._feed_doc_bytes(data)
            return
        if self.active_protocol == "legacy":
            self._feed_legacy_bytes(data)
            return

        # auto 模式: 同时尝试两种解析，先稳定命中的协议会被锁定
        self._feed_doc_bytes(data)
        self._feed_legacy_bytes(data)
        if self.legacy_packet_count >= 3 and self.doc_frame_count == 0:
            self.active_protocol = "legacy"
        elif self.doc_frame_count >= 3 and self.legacy_packet_count == 0:
            self.active_protocol = "doc"
        elif self.legacy_packet_count >= 3 and self.doc_frame_count >= 3:
            if self.legacy_packet_count >= self.doc_frame_count:
                self.active_protocol = "legacy"
            else:
                self.active_protocol = "doc"

    def _feed_doc_bytes(self, data):
        for byte in data:
            self._doc_buffer.append(byte)
            while len(self._doc_buffer) >= self.DOC_FRAME_SIZE:
                if self._doc_buffer[0] == self.DOC_SYNC_BYTE:
                    frame = self._doc_buffer[:self.DOC_FRAME_SIZE]
                    self._parse_doc_frame(frame)
                    self.doc_frame_count += 1
                    self._doc_buffer = self._doc_buffer[self.DOC_FRAME_SIZE:]
                else:
                    self._doc_buffer = self._doc_buffer[1:]

    def _feed_legacy_bytes(self, data):
        self._legacy_buffer.extend(data)
        while len(self._legacy_buffer) >= self.LEGACY_PACKET_SIZE:
            # 先对齐到 F8 04 头，再用打分规则放宽 marker 检测。
            if not (self._legacy_buffer[0] == 0xF8 and self._legacy_buffer[1] == 0x04):
                self._legacy_buffer = self._legacy_buffer[1:]
                continue

            packet = self._legacy_buffer[:self.LEGACY_PACKET_SIZE]
            if self._legacy_markers_match(packet):
                self._parse_legacy_packet(packet)
                self.legacy_packet_count += 1
                self._legacy_buffer = self._legacy_buffer[self.LEGACY_PACKET_SIZE:]
                continue
            self._legacy_buffer = self._legacy_buffer[1:]

    def _parse_doc_frame(self, frame):
        self.values["slider"] = frame[1] * 100 // 255
        self.values["light"] = frame[2] * 100 // 255
        self.values["sound"] = frame[3] * 100 // 255
        self.values["button"] = 1 if frame[4] & 0x80 else 0
        self.values["A"] = frame[5]
        self.values["B"] = frame[6]
        self.values["C"] = frame[7]
        self.values["D"] = frame[8]

    def _legacy_analog(self, name, id_byte, val_byte):
        """解码 legacy 协议模拟通道。

        legacy 协议每通道 2 字节：
          high_byte = 0x80 | (channel_id << 3) | (value >> 7)
          low_byte  = value & 0x7F
        合并得到 10-bit ADC 值（0-1023），再线性映射到 0-100。
        id_byte 低 3 位 (bits[2:0]) 是 value 的高 3 位，val_byte 是低 7 位。
        """
        value_10bit = ((id_byte & 0x07) << 7) | (val_byte & 0x7F)
        return value_10bit * 100 // 1023

    def _parse_legacy_packet(self, packet):
        # 可配置 legacy 映射，用于快速校准不同固件
        slider_idx = self.legacy_map["slider"]
        light_idx = self.legacy_map["light"]
        sound_idx = self.legacy_map["sound"]
        button_idx = self.legacy_map["button"]
        self._update_button_scan(packet)
        # 模拟通道：id 字节紧邻 val 字节前一位，使用进位补偿解码
        self.values["slider"] = self._legacy_analog("slider", packet[slider_idx - 1], packet[slider_idx])
        self.values["light"] = self._legacy_analog("light", packet[light_idx - 1], packet[light_idx])
        self.values["sound"] = self._legacy_analog("sound", packet[sound_idx - 1], packet[sound_idx])
        raw_bit = 1 if (packet[button_idx] & self.legacy_button_mask) else 0
        self.values["button"] = 1 - raw_bit if self.legacy_button_invert else raw_bit
        self.values["A"] = packet[self.legacy_map["A"]]
        self.values["B"] = packet[self.legacy_map["B"]]
        self.values["C"] = packet[self.legacy_map["C"]]
        self.values["D"] = packet[self.legacy_map["D"]]

    def set_stable_button(self, val):
        """由 serial_thread 防抖逻辑调用，确保 get_values() 对任何线程都返回稳定值。"""
        self._stable_button = val

    def get_values(self):
        v = dict(self.values)
        if self._stable_button is not None:
            v["button"] = self._stable_button
        return v

# -----------------------------
WS_HOST = "127.0.0.1"
WS_PORT = 8765
SERIAL_PORT = "COM14"
BAUDRATE = 38400
LOG_SERIAL = False
LOG_FRAME_HEX = True
LOG_VALUES_EVERY_N_FRAMES = 10
LOG_TO_FILE = False
LOG_FILE_PATH = None
PROTOCOL_MODE = "auto"
LEGACY_MAP = {
    "slider": 3,   # ch0, bytes[2:3], confirmed by slider-move test
    "light": 13,   # ch5, bytes[12:13], stable under slider-move = real light sensor
    "sound": 15,   # ch6, bytes[14:15], 0% in quiet room = real sound sensor
    "button": 9,   # ch3, bytes[8:9], active-low with invert=True
    "A": 11,       # ch4, bytes[10:11]
    "B": 15,       # ch6 (same as sound val byte, raw read)
    "C": 17,       # ch7 (official slider pin, unused on clone)
    "D": 9,        # ch3 (same as button, raw read)
}
LEGACY_BUTTON_INVERT = True
LEGACY_BUTTON_MASK = 0x02
BUTTON_DEBOUNCE_MS = 120
BUTTON_DEBOUNCE_FRAMES = 2
BUTTON_PRESS_DEBOUNCE_MS = 0
BUTTON_PRESS_DEBOUNCE_FRAMES = 1
BUTTON_RELEASE_DEBOUNCE_MS = 180
BUTTON_RELEASE_DEBOUNCE_FRAMES = 2
BUTTON_SCAN_CANDIDATES = []
BUTTON_SCAN_MASK = LEGACY_BUTTON_MASK
BUTTON_SCAN_LOG_EVERY_FRAMES = 50
SERIAL_RECONNECT_DELAY_SEC = 2.0

serial_log_file = None
serial_log_lock = threading.Lock()


def parse_args():
    parser = argparse.ArgumentParser(description="PicoBoard serial to WebSocket bridge")
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List available serial ports and exit"
    )
    parser.add_argument(
        "--serial-port",
        default=SERIAL_PORT,
        help="Serial port name, e.g. COM3"
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=BAUDRATE,
        help="Serial baudrate, default: %(default)s"
    )
    parser.add_argument(
        "--serial-log",
        action="store_true",
        help="Enable serial TX/RX logs"
    )
    parser.add_argument(
        "--no-serial-log",
        action="store_true",
        help="Disable serial TX/RX logs"
    )
    parser.add_argument(
        "--serial-log-file",
        nargs="?",
        const="auto",
        default=None,
        help="Save serial logs to file (optional path, default: auto timestamped file)"
    )
    parser.add_argument(
        "--protocol",
        choices=["auto", "doc", "legacy"],
        default="auto",
        help="Frame parser protocol mode"
    )
    parser.add_argument(
        "--legacy-map",
        default=None,
        help="Override legacy mapping, e.g. slider=3,light=4,sound=5,button=6,A=7,B=11,C=13,D=15"
    )
    parser.add_argument(
        "--legacy-button-mask",
        default=f"0x{LEGACY_BUTTON_MASK:02x}",
        help="Button bit mask for legacy mode (hex or decimal)"
    )
    parser.add_argument(
        "--button-debounce-ms",
        type=int,
        default=BUTTON_DEBOUNCE_MS,
        help="Debounce window for button edge logging in milliseconds"
    )
    parser.add_argument(
        "--button-debounce-frames",
        type=int,
        default=BUTTON_DEBOUNCE_FRAMES,
        help="Require N consecutive frames before accepting a button state change"
    )
    parser.add_argument(
        "--button-press-debounce-ms",
        type=int,
        default=BUTTON_PRESS_DEBOUNCE_MS,
        help="Debounce ms for press transition (default: %(default)s)"
    )
    parser.add_argument(
        "--button-press-debounce-frames",
        type=int,
        default=BUTTON_PRESS_DEBOUNCE_FRAMES,
        help="Debounce frames for press transition (default: %(default)s)"
    )
    parser.add_argument(
        "--button-release-debounce-ms",
        type=int,
        default=BUTTON_RELEASE_DEBOUNCE_MS,
        help="Debounce ms for release transition (default: %(default)s)"
    )
    parser.add_argument(
        "--button-release-debounce-frames",
        type=int,
        default=BUTTON_RELEASE_DEBOUNCE_FRAMES,
        help="Debounce frames for release transition (default: %(default)s)"
    )
    parser.add_argument(
        "--legacy-button-invert",
        action=argparse.BooleanOptionalAction,
        default=LEGACY_BUTTON_INVERT,
        help="Invert button bit: treat mask=0 as pressed (active-low). Use --no-legacy-button-invert to disable."
    )
    parser.add_argument(
        "--button-scan-candidates",
        default=None,
        help="Comma-separated legacy byte indices to scan as button candidates, e.g. 7,9,11,12"
    )
    parser.add_argument(
        "--button-scan-mask",
        default=None,
        help="Bit mask used by candidate scan (hex or decimal). Default: same as --legacy-button-mask"
    )
    parser.add_argument(
        "--button-scan-log-every-frames",
        type=int,
        default=BUTTON_SCAN_LOG_EVERY_FRAMES,
        help="Emit button scan summary every N frames"
    )
    return parser.parse_args()


def parse_legacy_map_arg(value):
    result = dict(LEGACY_MAP)
    if not value:
        return result
    valid_fields = set(result.keys())
    parts = [part.strip() for part in value.split(",") if part.strip()]
    for part in parts:
        if "=" not in part:
            raise ValueError(f"Invalid map item: {part}")
        key, raw_idx = [p.strip() for p in part.split("=", 1)]
        if key not in valid_fields:
            raise ValueError(f"Unknown field: {key}")
        idx = int(raw_idx)
        if idx < 0 or idx >= PicoBoardCore.LEGACY_PACKET_SIZE:
            raise ValueError(f"Index out of range for {key}: {idx}")
        result[key] = idx
    return result


def parse_index_list_arg(value):
    if value is None:
        return []
    result = []
    parts = [part.strip() for part in value.split(",") if part.strip()]
    for part in parts:
        idx = int(part)
        if idx < 0 or idx >= PicoBoardCore.LEGACY_PACKET_SIZE:
            raise ValueError(f"Index out of range: {idx}")
        if idx not in result:
            result.append(idx)
    return result


def list_available_serial_ports():
    ports = list(list_ports.comports())
    if not ports:
        print("[Serial] No serial ports found")
        return
    print("[Serial] Available ports:")
    for port in ports:
        desc = port.description or ""
        hwid = port.hwid or ""
        print(f"  - {port.device} | {desc} | {hwid}")


def default_serial_log_path():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join("logs", f"serial_{timestamp}.jsonl")


def init_serial_log_file(path):
    global serial_log_file, LOG_FILE_PATH
    if path == "auto":
        path = default_serial_log_path()
    log_dir = os.path.dirname(path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    serial_log_file = open(path, "a", encoding="utf-8", buffering=1)
    LOG_FILE_PATH = path


def write_serial_log(event, data):
    if not LOG_TO_FILE or serial_log_file is None:
        return
    record = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "event": event,
        "data": data,
    }
    with serial_log_lock:
        serial_log_file.write(json.dumps(record) + "\n")


def close_serial_log_file():
    global serial_log_file
    if serial_log_file is not None:
        serial_log_file.close()
        serial_log_file = None

pico = PicoBoardCore()
clients = set()

# -----------------------------
async def ws_handler(ws):
    clients.add(ws)
    try:
        while True:
            await asyncio.sleep(0.05)
            try:
                await ws.send(json.dumps(pico.get_values()))
            except websockets.exceptions.ConnectionClosedOK:
                break
            except websockets.exceptions.ConnectionClosedError:
                break
    finally:
        clients.remove(ws)

# -----------------------------
def serial_thread():
    frame_count = 0
    reported_protocol = None
    stable_button_state = None
    candidate_button_state = None
    candidate_since = None
    candidate_frames = 0
    stable_edge_count = 0

    while True:
        ser = None
        try:
            ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=0.1)
            print(f"[Serial] Connected to {SERIAL_PORT} at {BAUDRATE}bps")
            write_serial_log("serial_connected", {"port": SERIAL_PORT, "baudrate": BAUDRATE})

            while True:
                # 激活采样
                ser.write(b'\x01')
                ser.flush()
                write_serial_log("tx", {"hex": "01"})
                if LOG_SERIAL:
                    print("[Serial][TX] 01")
                # 读取串口字节流，交给解析器按 0x80 自动对齐
                chunk = ser.read(64)
                if chunk:
                    write_serial_log("rx", {"hex": chunk.hex(' ')})
                    if LOG_SERIAL and LOG_FRAME_HEX:
                        print(f"[Serial][RX] {chunk.hex(' ')}")
                    pico.feed_bytes(chunk)
                    if pico.active_protocol and pico.active_protocol != reported_protocol:
                        reported_protocol = pico.active_protocol
                        print(f"[Parser] Active protocol: {reported_protocol}")
                        write_serial_log("parser_protocol", {"mode": reported_protocol})

                    for scan_event in pico.drain_button_scan_events():
                        write_serial_log("button_scan_raw_edge", scan_event)

                    # 按键边沿防抖后上报，避免噪声抖动误触发
                    now = time.monotonic()
                    # 直接从原始解析值读取，不经过 stable 覆盖
                    button_state = int(pico.values.get("button", 0))
                    if stable_button_state is None:
                        stable_button_state = button_state
                        candidate_button_state = button_state
                        candidate_since = now
                        candidate_frames = 1
                    if button_state != candidate_button_state:
                        candidate_button_state = button_state
                        candidate_since = now
                        candidate_frames = 1
                    else:
                        candidate_frames += 1

                    # 非对称防抖：按下和释放使用不同阈值
                    target_ms = BUTTON_PRESS_DEBOUNCE_MS if candidate_button_state == 1 else BUTTON_RELEASE_DEBOUNCE_MS
                    target_frames = (
                        BUTTON_PRESS_DEBOUNCE_FRAMES if candidate_button_state == 1 else BUTTON_RELEASE_DEBOUNCE_FRAMES
                    )

                    if (
                        candidate_button_state != stable_button_state
                        and candidate_since is not None
                        and (now - candidate_since) * 1000 >= target_ms
                        and candidate_frames >= target_frames
                    ):
                        stable_button_state = candidate_button_state
                        edge = "press" if stable_button_state == 1 else "release"
                        pico.set_stable_button(stable_button_state)
                        write_serial_log(
                            "button_edge",
                            {
                                "edge": edge,
                                "button": stable_button_state,
                                "values": pico.get_values(),
                            },
                        )
                        stable_edge_count += 1
                        if LOG_SERIAL:
                            print(f"[Serial][Button] {edge}: {pico.get_values()}")
                        candidate_frames = 0

                    # 初始化时同步一次 stable button
                    if stable_button_state is not None:
                        pico.set_stable_button(stable_button_state)

                    frame_count += 1
                    if LOG_SERIAL and frame_count % LOG_VALUES_EVERY_N_FRAMES == 0:
                        print(f"[Serial][Values] {pico.get_values()}")
                    if frame_count % LOG_VALUES_EVERY_N_FRAMES == 0:
                        write_serial_log("values", pico.get_values())
                    if (
                        BUTTON_SCAN_CANDIDATES
                        and frame_count % BUTTON_SCAN_LOG_EVERY_FRAMES == 0
                    ):
                        write_serial_log("button_scan_summary", {
                            "stable_edges": stable_edge_count,
                            "scan_mask": BUTTON_SCAN_MASK,
                            "candidates": BUTTON_SCAN_CANDIDATES,
                            "stats": pico.get_button_scan_summary(),
                        })
                time.sleep(0.03)

        except Exception as e:
            print(f"[Serial] Error: {e}")
            write_serial_log("serial_error", {"message": str(e)})
            print(f"[Serial] Reconnect in {SERIAL_RECONNECT_DELAY_SEC:.1f}s...")
            write_serial_log("serial_reconnect_wait", {"seconds": SERIAL_RECONNECT_DELAY_SEC})
            time.sleep(SERIAL_RECONNECT_DELAY_SEC)
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
# -----------------------------
async def main_async():
    print(f"[WS] Starting WS server at ws://{WS_HOST}:{WS_PORT}")
    async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
        await asyncio.Future()  # run forever

# -----------------------------
def main():
    global LOG_SERIAL, LOG_TO_FILE, PROTOCOL_MODE, pico, LEGACY_MAP, LEGACY_BUTTON_MASK, LEGACY_BUTTON_INVERT
    global SERIAL_PORT, BAUDRATE
    global BUTTON_DEBOUNCE_MS, BUTTON_DEBOUNCE_FRAMES
    global BUTTON_PRESS_DEBOUNCE_MS, BUTTON_PRESS_DEBOUNCE_FRAMES
    global BUTTON_RELEASE_DEBOUNCE_MS, BUTTON_RELEASE_DEBOUNCE_FRAMES
    global BUTTON_SCAN_CANDIDATES, BUTTON_SCAN_MASK, BUTTON_SCAN_LOG_EVERY_FRAMES
    args = parse_args()

    if args.list_ports:
        list_available_serial_ports()
        return

    if args.baudrate <= 0:
        raise SystemExit("[Config] --baudrate must be > 0")
    SERIAL_PORT = args.serial_port
    BAUDRATE = args.baudrate

    if args.serial_log:
        LOG_SERIAL = True
    if args.no_serial_log:
        LOG_SERIAL = False

    if args.serial_log_file is not None:
        LOG_TO_FILE = True
        init_serial_log_file(args.serial_log_file)

    try:
        LEGACY_MAP = parse_legacy_map_arg(args.legacy_map)
    except ValueError as e:
        raise SystemExit(f"[Config] Invalid --legacy-map: {e}")

    try:
        LEGACY_BUTTON_MASK = int(args.legacy_button_mask, 0)
    except ValueError:
        raise SystemExit("[Config] Invalid --legacy-button-mask")

    if args.button_debounce_ms < 0:
        raise SystemExit("[Config] --button-debounce-ms must be >= 0")
    if args.button_debounce_frames < 1:
        raise SystemExit("[Config] --button-debounce-frames must be >= 1")
    BUTTON_DEBOUNCE_MS = args.button_debounce_ms
    BUTTON_DEBOUNCE_FRAMES = args.button_debounce_frames

    if args.button_press_debounce_ms < 0:
        raise SystemExit("[Config] --button-press-debounce-ms must be >= 0")
    if args.button_press_debounce_frames < 1:
        raise SystemExit("[Config] --button-press-debounce-frames must be >= 1")
    if args.button_release_debounce_ms < 0:
        raise SystemExit("[Config] --button-release-debounce-ms must be >= 0")
    if args.button_release_debounce_frames < 1:
        raise SystemExit("[Config] --button-release-debounce-frames must be >= 1")
    BUTTON_PRESS_DEBOUNCE_MS = args.button_press_debounce_ms
    BUTTON_PRESS_DEBOUNCE_FRAMES = args.button_press_debounce_frames
    BUTTON_RELEASE_DEBOUNCE_MS = args.button_release_debounce_ms
    BUTTON_RELEASE_DEBOUNCE_FRAMES = args.button_release_debounce_frames

    try:
        BUTTON_SCAN_CANDIDATES = parse_index_list_arg(args.button_scan_candidates)
    except ValueError as e:
        raise SystemExit(f"[Config] Invalid --button-scan-candidates: {e}")

    if args.button_scan_mask is None:
        BUTTON_SCAN_MASK = LEGACY_BUTTON_MASK
    else:
        try:
            BUTTON_SCAN_MASK = int(args.button_scan_mask, 0)
        except ValueError:
            raise SystemExit("[Config] Invalid --button-scan-mask")

    if args.button_scan_log_every_frames < 1:
        raise SystemExit("[Config] --button-scan-log-every-frames must be >= 1")
    BUTTON_SCAN_LOG_EVERY_FRAMES = args.button_scan_log_every_frames
    LEGACY_BUTTON_INVERT = args.legacy_button_invert

    PROTOCOL_MODE = args.protocol
    pico = PicoBoardCore(
        protocol_mode=PROTOCOL_MODE,
        legacy_map=LEGACY_MAP,
        legacy_button_mask=LEGACY_BUTTON_MASK,
        legacy_button_invert=LEGACY_BUTTON_INVERT,
        button_scan_candidates=BUTTON_SCAN_CANDIDATES,
        button_scan_mask=BUTTON_SCAN_MASK,
    )

    print(f"[Config] Serial log: {'ON' if LOG_SERIAL else 'OFF'}")
    print(f"[Config] Protocol mode: {PROTOCOL_MODE}")
    if PROTOCOL_MODE in ("legacy", "auto"):
        print(f"[Config] Legacy map: {LEGACY_MAP}")
        print(f"[Config] Legacy button mask: {hex(LEGACY_BUTTON_MASK)}, invert: {LEGACY_BUTTON_INVERT}")
    print(f"[Config] Button debounce: {BUTTON_DEBOUNCE_MS}ms")
    print(f"[Config] Button debounce frames: {BUTTON_DEBOUNCE_FRAMES}")
    print(
        "[Config] Button asymmetric debounce: "
        f"press={BUTTON_PRESS_DEBOUNCE_MS}ms/{BUTTON_PRESS_DEBOUNCE_FRAMES}f, "
        f"release={BUTTON_RELEASE_DEBOUNCE_MS}ms/{BUTTON_RELEASE_DEBOUNCE_FRAMES}f"
    )
    print(f"[Config] Serial: port={SERIAL_PORT}, baudrate={BAUDRATE}")
    if BUTTON_SCAN_CANDIDATES:
        print(
            "[Config] Button scan: "
            f"candidates={BUTTON_SCAN_CANDIDATES}, mask={hex(BUTTON_SCAN_MASK)}, "
            f"summary_every={BUTTON_SCAN_LOG_EVERY_FRAMES} frames"
        )
    if LOG_TO_FILE:
        print(f"[Config] Serial log file: {LOG_FILE_PATH}")
        write_serial_log("config", {
            "serial_log": LOG_SERIAL,
            "log_frame_hex": LOG_FRAME_HEX,
            "values_every_n_frames": LOG_VALUES_EVERY_N_FRAMES,
            "serial_port": SERIAL_PORT,
            "baudrate": BAUDRATE,
            "protocol_mode": PROTOCOL_MODE,
            "legacy_map": LEGACY_MAP,
            "legacy_button_mask": LEGACY_BUTTON_MASK,
            "legacy_button_invert": LEGACY_BUTTON_INVERT,
            "button_debounce_ms": BUTTON_DEBOUNCE_MS,
            "button_debounce_frames": BUTTON_DEBOUNCE_FRAMES,
            "button_press_debounce_ms": BUTTON_PRESS_DEBOUNCE_MS,
            "button_press_debounce_frames": BUTTON_PRESS_DEBOUNCE_FRAMES,
            "button_release_debounce_ms": BUTTON_RELEASE_DEBOUNCE_MS,
            "button_release_debounce_frames": BUTTON_RELEASE_DEBOUNCE_FRAMES,
            "button_scan_candidates": BUTTON_SCAN_CANDIDATES,
            "button_scan_mask": BUTTON_SCAN_MASK,
            "button_scan_log_every_frames": BUTTON_SCAN_LOG_EVERY_FRAMES,
        })

    t = threading.Thread(target=serial_thread, daemon=True)
    t.start()
    try:
        asyncio.run(main_async())
    finally:
        close_serial_log_file()

# -----------------------------
if __name__ == "__main__":
    main()