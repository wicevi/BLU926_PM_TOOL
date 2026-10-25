# Vendored from vendor package "blu2-api" (blu_api_python_100k_202508161306.zip),
# file src/blu_all_in_one.py, kept unmodified as the BLU926 driver layer.
# Verified against real hardware (SN 6F6B6BBB000F, 2026-10):
#   - metadata text protocol, REGULATOR_SET / DEVICE_RUNNING_SET / AVERAGE_START all work
#   - 4-byte sample frames decode correctly (auto-range + calibration => accurate uA)
#   - This unit is a BLU926 (20K-class; only the BLU939 does 100K - the "100k" in
#     the vendor zip name refers to that model family, the SDK itself is shared).
#     Sustained upload measures ~16.5k frames/s (bursty USB), so BluClient's
#     100k-based timestamps / RateAdjuster are wrong for time math on this unit.
#     Higher layers (blu926.service) measure the rate at runtime and use
#     sample-count-derived wall-clock timestamps instead of BluClient.

import time
import struct
import logging
import threading
import queue
from collections import deque
from typing import Iterable, List, Dict, Optional, Callable

import serial


# -----------------------------
# Device command definitions
# -----------------------------
class BLU2_Command:
    NO_OP = 0x00
    TRIGGER_SET = 0x01
    AVG_NUM_SET = 0x02
    TRIGGER_WINDOW_SET = 0x03
    TRIGGER_INTERVAL_SET = 0x04
    TRIGGER_SINGLE_SET = 0x05
    AVERAGE_START = 0x06
    AVERAGE_STOP = 0x07
    RANGE_SET = 0x08
    LCD_SET = 0x09
    TRIGGER_STOP = 0x0A
    DEVICE_RUNNING_SET = 0x0C
    REGULATOR_SET = 0x0D
    SWITCH_POINT_DOWN = 0x0E
    SWITCH_POINT_UP = 0x0F
    TRIGGER_EXT_TOGGLE = 0x11
    SET_POWER_MODE = 0x11
    RES_USER_SET = 0x12
    SPIKE_FILTERING_ON = 0x15
    SPIKE_FILTERING_OFF = 0x16
    GET_META_DATA = 0x19
    RESET = 0x20
    SET_USER_GAINS = 0x25


class BLU2_Modes:
    AMPERE_MODE = "AMPERE_MODE"
    SOURCE_MODE = "SOURCE_MODE"


# -----------------------------
# Low-level serial API (from blu2_api.py, with improved timeouts & DTR/RTS)
# -----------------------------
class BLU2_API:
    def __init__(self, port: str, **kwargs):
        baud = kwargs.pop("baudrate", 1_000_000)
        timeout = kwargs.pop("timeout", 2)
        write_timeout = kwargs.pop("write_timeout", 2)

        self.ser = None
        try:
            self.ser = serial.Serial(port, baudrate=baud, timeout=timeout, write_timeout=write_timeout, **kwargs)
        except Exception:
            self.ser = serial.Serial(port, baudrate=115200, timeout=timeout, write_timeout=write_timeout, **kwargs)

        try:
            self.ser.dtr = True
            self.ser.rts = True
        except Exception:
            pass
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

        self.modifiers = {
            "Calibrated": None,
            "R": {"0": 1031.64, "1": 101.65, "2": 10.15, "3": 0.94, "4": 0.113, "5": 0.013},
            "GS": {"0": 1, "1": 1, "2": 1, "3": 1, "4": 1, "5": 1},
            "GI": {"0": 1, "1": 1, "2": 1, "3": 1, "4": 1, "5": 1},
            "O": {"0": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
            "S": {"0": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
            "I": {"0": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
            "UG": {"0": 1, "1": 1, "2": 1, "3": 1, "4": 1, "5": 1},
            "HW": None,
            "IA": None
        }

        self.vdd_low = 500
        self.vdd_high = 5000
        self.current_vdd = None
        self.adc_mult = 1.8 / 163840

        self.MEAS_ADC = self._generate_mask(14, 0)
        self.MEAS_RANGE = self._generate_mask(3, 14)
        self.MEAS_LOGIC = self._generate_mask(8, 24)

        self.mode = None
        self.rolling_avg = None
        self.rolling_avg4 = None
        self.prev_range = None
        self.consecutive_range_samples = 0
        self.spike_filter_alpha = 0.18
        self.spike_filter_alpha5 = 0.06
        self.spike_filter_samples = 3
        self.after_spike = 0

        self.remainder = {"sequence": b"", "len": 0}
        self.base_rate_hz = 100000
        self.decimate = 1
        self.applied_rate_hz = self.base_rate_hz
        self._sample_index_counter = 0

    def __del__(self):
        try:
            if self.ser:
                self.ser.close()
        except Exception as e:
            logging.error(f"An error occurred while closing blu2_api: {e}")

    def _pack_struct(self, cmd_tuple):
        return struct.pack("B" * len(cmd_tuple), *cmd_tuple)

    def _write_serial(self, cmd_tuple):
        try:
            cmd_packed = self._pack_struct(cmd_tuple)
            self.ser.write(cmd_packed)
        except Exception as e:
            logging.error(f"An error occurred when writing to serial port: {e}")

    def _twos_comp(self, val):
        if (val & (1 << (32 - 1))) != 0:
            val = val - (1 << 32)
        return val

    def _convert_source_voltage(self, mV):
        if mV < self.vdd_low:
            mV = self.vdd_low
        if mV > self.vdd_high:
            mV = self.vdd_high
        hi = (mV >> 8) & 0xFF
        lo = mV & 0xFF
        return hi, lo

    def _read_metadata(self, timeout_s: float = 3.0):
        end_time = time.time() + timeout_s
        buf = bytearray()
        while time.time() < end_time:
            try:
                chunk = self.ser.read(self.ser.in_waiting or 1)
            except Exception:
                chunk = b""
            if chunk:
                buf += chunk
                try:
                    text = buf.decode("utf-8", errors="ignore")
                except Exception:
                    text = ""
                if "END" in text:
                    return text
            time.sleep(0.05)
        return None

    def _parse_metadata(self, metadata):
        try:
            data_split = [row.replace('=', ':').split(":") for row in metadata.split("\n")]
            for key in self.modifiers.keys():
                for data_pair in data_split:
                    if len(data_pair) < 2:
                        continue
                    k = (data_pair[0] or '').strip()
                    v = (data_pair[1] or '').strip()
                    if key == k:
                        self.modifiers[key] = v
                    for ind in range(0, 6):
                        if key + str(ind) == k:
                            if 'R' in k:
                                try:
                                    fv = float(v)
                                    if fv != 0:
                                        self.modifiers[key][str(ind)] = fv
                                except Exception:
                                    pass
                            else:
                                try:
                                    self.modifiers[key][str(ind)] = float(v)
                                except Exception:
                                    pass
            # parse VDD (saved voltage)
            for data_pair in data_split:
                if len(data_pair) < 2:
                    continue
                k = (data_pair[0] or '').strip().upper()
                v = (data_pair[1] or '').strip()
                if k.startswith('VDD'):
                    digits = ''.join(ch for ch in v if ch.isdigit())
                    if digits:
                        try:
                            self.saved_vdd_mv = int(digits)
                        except Exception:
                            pass
            return True
        except Exception:
            return None

    def _generate_mask(self, bits, pos):
        mask = ((2 ** bits - 1) << pos)
        mask = self._twos_comp(mask)
        return {"mask": mask, "pos": pos}

    def _get_masked_value(self, value, meas):
        masked_value = (value & meas["mask"]) >> meas["pos"]
        return masked_value

    def _handle_raw_data(self, adc_value):
        try:
            current_measurement_range = min(self._get_masked_value(adc_value, self.MEAS_RANGE), 5)
            adc_result = self._get_masked_value(adc_value, self.MEAS_ADC) * 4
            bits = self._get_masked_value(adc_value, self.MEAS_LOGIC)
            analog_value = self.get_adc_result(current_measurement_range, adc_result) * 10 ** 6
            return analog_value, bits
        except Exception:
            print("Measurement outside of range!")
            return None, None

    @staticmethod
    def list_devices():
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        devices = [port.device for port in ports if getattr(port, "pid", None) == 12298] or [port.device for port in ports]
        return devices

    def get_data(self):
        sampling_data = self.ser.read(self.ser.in_waiting)
        return sampling_data

    def set_output_rate_hz(self, target_rate_hz: int):
        try:
            target = int(target_rate_hz)
        except Exception:
            target = 0
        if target <= 0:
            self.decimate = 1
        else:
            d = int(round(self.base_rate_hz / target))
            if d < 1:
                d = 1
            self.decimate = d
        self.applied_rate_hz = self.base_rate_hz // self.decimate
        return self.applied_rate_hz

    def get_output_rate_hz(self):
        return self.applied_rate_hz

    def get_modifiers(self):
        self._write_serial((BLU2_Command.GET_META_DATA,))
        metadata = self._read_metadata()
        ret = self._parse_metadata(metadata) if metadata else None
        return ret

    def start_measuring(self):
        if not self.current_vdd:
            if self.mode == BLU2_Modes.SOURCE_MODE:
                raise Exception("Output voltage not set!")
            if self.mode == BLU2_Modes.AMPERE_MODE:
                raise Exception("Input voltage not set!")
        self._sample_index_counter = 0
        self._write_serial((BLU2_Command.AVERAGE_START,))

    def stop_measuring(self):
        self._write_serial((BLU2_Command.AVERAGE_STOP,))

    def set_source_voltage(self, mV):
        b_1, b_2 = self._convert_source_voltage(mV)
        self._write_serial((BLU2_Command.REGULATOR_SET, b_1, b_2))
        self.current_vdd = mV

    def toggle_DUT_power(self, state):
        on = False
        if isinstance(state, bool):
            on = state
        else:
            on = str(state).strip().upper() == "ON"
        if on:
            self._write_serial((BLU2_Command.DEVICE_RUNNING_SET, BLU2_Command.TRIGGER_SET))
        else:
            self._write_serial((BLU2_Command.DEVICE_RUNNING_SET, BLU2_Command.NO_OP))

    def use_ampere_meter(self):
        self.mode = BLU2_Modes.AMPERE_MODE
        self._write_serial((BLU2_Command.SET_POWER_MODE, BLU2_Command.TRIGGER_SET))

    def use_source_meter(self):
        self.mode = BLU2_Modes.SOURCE_MODE
        self._write_serial((BLU2_Command.SET_POWER_MODE, BLU2_Command.AVG_NUM_SET))

    def get_adc_result(self, current_range, adc_value):
        current_range = str(current_range)
        result_without_gain = (adc_value - self.modifiers["O"][current_range]) * (self.adc_mult / self.modifiers["R"][current_range])
        adc = result_without_gain

        prev_rolling_avg = self.rolling_avg
        prev_rolling_avg4 = self.rolling_avg4

        if self.rolling_avg is None:
            self.rolling_avg = adc
        else:
            self.rolling_avg = self.spike_filter_alpha * adc + (1 - self.spike_filter_alpha) * self.rolling_avg

        if self.rolling_avg4 is None:
            self.rolling_avg4 = adc
        else:
            self.rolling_avg4 = self.spike_filter_alpha5 * adc + (1 - self.spike_filter_alpha5) * self.rolling_avg4

        if self.prev_range is None:
            self.prev_range = current_range

        if self.prev_range != current_range or self.after_spike > 0:
            if self.prev_range != current_range:
                self.consecutive_range_samples = 0
                self.after_spike = self.spike_filter_samples
            else:
                self.consecutive_range_samples += 1

            if current_range == "5":
                if self.consecutive_range_samples < 2:
                    self.rolling_avg = prev_rolling_avg
                    self.rolling_avg4 = prev_rolling_avg4
                adc = self.rolling_avg4
            else:
                adc = self.rolling_avg

            self.after_spike -= 1

        self.prev_range = current_range
        return adc

    def _digital_to_analog(self, adc_value):
        return int.from_bytes(adc_value, byteorder="little", signed=False)

    def digital_channels(self, bits):
        digital_channels = [[], [], [], [], [], [], [], []]
        for sample in bits:
            digital_channels[0].append((sample & 1) >> 0)
            digital_channels[1].append((sample & 2) >> 1)
            digital_channels[2].append((sample & 4) >> 2)
            digital_channels[3].append((sample & 8) >> 3)
            digital_channels[4].append((sample & 16) >> 4)
            digital_channels[5].append((sample & 32) >> 5)
            digital_channels[6].append((sample & 64) >> 6)
            digital_channels[7].append((sample & 128) >> 7)
        return digital_channels

    def get_samples(self, buf):
        sample_size = 4
        offset = self.remainder["len"]
        samples = []
        raw_digital_output = []

        first_reading = (self.remainder["sequence"] + buf[0:sample_size - offset])[:4]
        adc_val = self._digital_to_analog(first_reading)
        measurement, bits = self._handle_raw_data(adc_val)
        if measurement is not None:
            if (self._sample_index_counter % self.decimate) == 0:
                samples.append(measurement)
            self._sample_index_counter += 1
        if bits is not None:
            if (self._sample_index_counter - 1) % self.decimate == 0:
                raw_digital_output.append(bits)

        offset = sample_size - offset

        while offset <= len(buf) - sample_size:
            next_val = buf[offset:offset + sample_size]
            offset += sample_size
            adc_val = self._digital_to_analog(next_val)
            measurement, bits = self._handle_raw_data(adc_val)
            if measurement is not None:
                if (self._sample_index_counter % self.decimate) == 0:
                    samples.append(measurement)
                self._sample_index_counter += 1
            if bits is not None:
                if (self._sample_index_counter - 1) % self.decimate == 0:
                    raw_digital_output.append(bits)

        self.remainder["sequence"] = buf[offset:len(buf)]
        self.remainder["len"] = len(buf) - offset
        return samples, raw_digital_output


class BLU_Fetch(threading.Thread):
    def __init__(self, blu2, quit_evt, buffer_len_s=10, buffer_chunk_s=0.5):
        super().__init__()
        self._blu2 = blu2
        self._quit = quit_evt
        self.print_stats = False
        self._stats = (None, None)
        self._last_timestamp = 0

        self._buffer_max_len = int(buffer_len_s * 100000 * 4)
        self._buffer_chunk = int(buffer_chunk_s * 100000 * 4)
        if self._buffer_max_len % 4 != 0:
            self._buffer_max_len = (self._buffer_max_len // 4) * 4
        if self._buffer_chunk % 4 != 0:
            self._buffer_chunk = (self._buffer_chunk // 4) * 4
        self._buffer_q = queue.Queue()

    def run(self):
        s = 0
        t = time.time()
        local_buffer = b""
        while not self._quit.is_set():
            d = BLU2_API.get_data(self._blu2)
            tm_now = time.time()
            local_buffer += d
            while len(local_buffer) >= self._buffer_chunk:
                self._buffer_q.put(local_buffer[:self._buffer_chunk])
                while self._buffer_q.qsize() > self._buffer_max_len / self._buffer_chunk:
                    self._buffer_q.get()
                local_buffer = local_buffer[self._buffer_chunk:]
                self._last_timestamp = tm_now
            s += len(d)
            dt = tm_now - t
            if dt >= 0.1:
                if self.print_stats:
                    print(f"Samples: {s}, delta time: {dt}")
                self._stats = (s, dt)
                s = 0
                t = tm_now
            time.sleep(0.0001)
        while True:
            try:
                self._buffer_q.get(block=False)
            except queue.Empty:
                break

    def get_data(self):
        ret = b""
        while True:
            try:
                ret += self._buffer_q.get(timeout=0.001)
            except queue.Empty:
                break
        return ret


class BLU2_MP(BLU2_API):
    def __init__(self, port, buffer_max_size_seconds=10, buffer_chunk_seconds=0.1, **kwargs):
        super().__init__(port, **kwargs)
        self._fetcher = None
        self._quit_evt = threading.Event()
        self._buffer_max_size_seconds = buffer_max_size_seconds
        self._buffer_chunk_seconds = buffer_chunk_seconds

    def __del__(self):
        BLU2_API.stop_measuring(self)
        self._quit_evt.clear()
        self._quit_evt = None
        del self._quit_evt
        if self._fetcher is not None:
            self._fetcher.join()
        self._fetcher = None
        del self._fetcher

    def start_measuring(self):
        self.stop_measuring()
        while self.get_data() != b"":
            pass
        BLU2_API.start_measuring(self)
        self._quit_evt.clear()
        if self._fetcher is not None:
            return
        self._fetcher = BLU_Fetch(self, self._quit_evt, self._buffer_max_size_seconds, self._buffer_chunk_seconds)
        self._fetcher.start()

    def stop_measuring(self):
        BLU2_API.stop_measuring(self)
        self.get_data()
        self._quit_evt.set()
        if self._fetcher is not None:
            self._fetcher.join()
            self._fetcher = None

    def get_data(self):
        try:
            return self._fetcher.get_data()
        except (TypeError, AttributeError):
            return b""


# -----------------------------
# Host-side resampler & window averager
# -----------------------------
class RateAdjuster:
    def __init__(self, target_rate_hz: int, base_rate_hz: int = 100_000):
        self.base_rate_hz = int(base_rate_hz)
        self.set_target_rate_hz(target_rate_hz)
        self._acc_sum = 0.0
        self._acc_count = 0
        self._accumulator = 0

    def set_target_rate_hz(self, target_rate_hz: int):
        tr = int(max(1, target_rate_hz))
        self.target_rate_hz = tr
        self._step_num = tr
        self._step_den = self.base_rate_hz

    def push(self, value: float):
        self._acc_sum += value
        self._acc_count += 1
        self._accumulator += self._step_num
        outputs = []
        while self._accumulator >= self._step_den and self._acc_count > 0:
            avg = self._acc_sum / self._acc_count
            outputs.append(avg)
            self._accumulator -= self._step_den
            self._acc_sum = 0.0
            self._acc_count = 0
        return outputs


class WindowAverageResult:
    __slots__ = ("window_ms", "timestamp_us", "average_uA")

    def __init__(self, window_ms: int, timestamp_us: int, average_uA: float):
        self.window_ms = int(window_ms)
        self.timestamp_us = int(timestamp_us)
        self.average_uA = float(average_uA)


class WindowAverager:
    def __init__(self, windows_ms: Iterable[int], emit_only_full_window: bool = True):
        self._windows = [int(w) for w in windows_ms]
        self._emit_only_full = bool(emit_only_full_window)
        self._buffers = {w: deque() for w in self._windows}
        self._sums = {w: 0.0 for w in self._windows}
        self._counts = {w: 0 for w in self._windows}

    def push(self, value_uA: float, timestamp_us: int) -> List[WindowAverageResult]:
        results: List[WindowAverageResult] = []
        for w in self._windows:
            buf = self._buffers[w]
            buf.append((timestamp_us, value_uA))
            self._sums[w] += value_uA
            self._counts[w] += 1
            limit_us = int(w) * 1000
            while buf and (timestamp_us - buf[0][0]) > limit_us:
                _o_ts, o_val = buf.popleft()
                self._sums[w] -= o_val
                self._counts[w] -= 1
            if self._counts[w] > 0:
                span_us = buf[-1][0] - buf[0][0] if buf else 0
                if (not self._emit_only_full) or (span_us + 1 >= limit_us):
                    avg = self._sums[w] / max(1, self._counts[w])
                    results.append(WindowAverageResult(w, timestamp_us, avg))
        return results


# -----------------------------
# Port discovery utilities
# -----------------------------
DEFAULT_VID = 0x15A2
DEFAULT_PID = 0x300A


def find_all(use_setupapi: bool = False) -> List[Dict[str, str]]:
    import serial.tools.list_ports
    ret: List[Dict[str, str]] = []
    for p in serial.tools.list_ports.comports():
        vid = getattr(p, "vid", None)
        pid = getattr(p, "pid", None)
        if vid == DEFAULT_VID and pid == DEFAULT_PID:
            ret.append({
                "port": p.device or "",
                "serial": getattr(p, "serial_number", "") or "",
                "description": getattr(p, "description", "") or "",
            })
    return ret


def find_by_serial(serial_sn: str, use_setupapi: bool = False) -> Dict[str, str]:
    serial_sn = (serial_sn or "").strip().upper()
    for d in find_all(use_setupapi):
        if (d.get("serial") or "").strip().upper() == serial_sn:
            return d
    return {}


def find_by_serials(serial_sns: Iterable[str], use_setupapi: bool = False) -> List[Dict[str, str]]:
    targets = {(s or "").strip().upper() for s in serial_sns}
    ret: List[Dict[str, str]] = []
    for d in find_all(use_setupapi):
        if (d.get("serial") or "").strip().upper() in targets:
            ret.append(d)
    return ret


# -----------------------------
# High-level client with background processing
# -----------------------------
class OutputMode:
    Raw = "raw"
    WindowAverage = "window"


class BluClientOptions:
    def __init__(self,
                 port_name: str,
                 target_sample_rate_hz: int = 1000,
                 enable_windows_ms: Optional[Iterable[int]] = None,
                 output_mode: str = OutputMode.WindowAverage,
                 warmup_samples: int = 0):
        self.port_name = port_name
        self.target_sample_rate_hz = int(target_sample_rate_hz)
        self.enable_windows_ms = list(enable_windows_ms or [])
        self.output_mode = output_mode
        self.warmup_samples = int(max(0, warmup_samples))


class BluClient:
    def __init__(self, options: BluClientOptions):
        self._opt = options
        self._dev = BLU2_MP(options.port_name)
        self._rate = RateAdjuster(options.target_sample_rate_hz)
        self._win = WindowAverager(options.enable_windows_ms, emit_only_full_window=True) if options.enable_windows_ms else None
        self._proc_thread: Optional[threading.Thread] = None
        self._quit = threading.Event()
        self._started = False
        self._warm_left = self._opt.warmup_samples

        self.on_raw: Optional[Callable[[int, float], None]] = None
        self.on_window: Optional[Callable[[WindowAverageResult], None]] = None

    def fetch_and_apply_modifiers(self, timeout_s: float = 3.0) -> bool:
        return bool(self._dev.get_modifiers())

    def set_source_voltage(self, mv: int):
        self._dev.set_source_voltage(int(mv))

    def get_saved_vdd_mv(self) -> int:
        try:
            return int(self._dev.saved_vdd_mv or 0)
        except Exception:
            return 0

    def toggle_dut_power(self, on: bool):
        self._dev.toggle_DUT_power(bool(on))

    def start_and_measure(self):
        if self._started:
            return
        self._quit.clear()
        self._dev.start_measuring()
        self._proc_thread = threading.Thread(target=self._process_loop, daemon=True)
        self._proc_thread.start()
        self._started = True

    def stop(self):
        self._quit.set()
        try:
            self._dev.stop_measuring()
        except Exception:
            pass
        if self._proc_thread:
            self._proc_thread.join(timeout=1.0)
            self._proc_thread = None
        self._started = False

    def dispose(self):
        self.stop()

    def _process_loop(self):
        base_period_us = int(1_000_000 / 100_000)
        timestamp_us = int(time.time() * 1_000_000)
        while not self._quit.is_set():
            buf = self._dev.get_data()
            if not buf:
                time.sleep(0.001)
                continue
            samples, _bits = self._dev.get_samples(buf)
            for v in samples:
                timestamp_us += base_period_us
                for out in self._rate.push(v):
                    if self._warm_left > 0:
                        self._warm_left -= 1
                        continue
                    if self._opt.output_mode == OutputMode.Raw:
                        if self.on_raw:
                            self.on_raw(timestamp_us, out)
                    else:
                        if self._win is not None:
                            for r in self._win.push(out, timestamp_us):
                                if self.on_window:
                                    self.on_window(r)


__all__ = [
    "BLU2_Command",
    "BLU2_Modes",
    "BLU2_API",
    "BLU2_MP",
    "RateAdjuster",
    "WindowAverageResult",
    "WindowAverager",
    "find_all",
    "find_by_serial",
    "find_by_serials",
    "OutputMode",
    "BluClientOptions",
    "BluClient",
] 