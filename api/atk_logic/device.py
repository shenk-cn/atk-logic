"""USB access and device control (Windows + Linux, via libusb / python-libusb1).

The command sequences replicate the official GUI:
  connect  -> ConnectDevice::CheckDeviceCreanInfo   (pv/thread/connect.cpp)
  capture  -> Session::OrderStart + ThreadWork::DeviceRecvThread
  pwm      -> SessionController::pwmStart / pwmStop
  close    -> FramelessWindow shutdown (SetResetState(1), CloseAllPWM, Exit, SetResetState(0))
Firmware update / bootloader commands are intentionally NOT implemented.
"""

from __future__ import annotations

import datetime
import math
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Union

import numpy as np

from . import protocol as P
from .capture import Capture, Channel, shift_packed

THRESHOLD_PRESETS = {  # SetContent.qml thresholdLevelComboBox
    "5.0v": 2.5, "5v": 2.5, "3.3v": 1.6, "3v3": 1.6, "3.0v": 1.5, "3v": 1.5,
    "2.5v": 1.2, "1.8v": 0.9, "1.5v": 0.7, "1.2v": 0.6,
}
DEFAULT_THRESHOLD_V = 1.6  # GUI default: "3.3V CMOS"


class AtkError(Exception):
    """Base error. `hint` is a human-readable suggestion."""

    def __init__(self, msg: str, hint: str = ""):
        super().__init__(msg)
        self.hint = hint


class DeviceNotFound(AtkError):
    pass


class DeviceAccessError(AtkError):
    pass


class DeviceStateError(AtkError):
    pass


class CaptureError(AtkError):
    pass


# =================================================================== transport
class Transport:
    """Minimal interface the device logic needs (real USB or a test fake)."""

    stream_error: Optional[str] = None

    def write(self, data: bytes, timeout_ms: int = 1000) -> None:
        raise NotImplementedError

    def read(self, length: int, timeout_ms: int) -> bytes:
        """Bulk IN; return what arrived (possibly b'' on timeout)."""
        raise NotImplementedError

    def stream_start(self, n_transfers: int, size: int, timeout_ms: int) -> None:
        raise NotImplementedError

    def stream_get(self, timeout_s: float) -> Optional[bytes]:
        """Next completed IN transfer (raw bytes) or None."""
        raise NotImplementedError

    def stream_stop(self) -> None:
        raise NotImplementedError

    @property
    def no_device(self) -> bool:
        return False

    def close(self) -> None:
        pass


def _usb1():
    try:
        import usb1  # python-libusb1; Windows wheels bundle libusb-1.0.dll
    except ImportError as e:  # pragma: no cover
        raise AtkError("python package 'libusb1' is not installed",
                       "pip install libusb1  (Linux also needs the system libusb-1.0 library)") from e
    return usb1


def _access_hint() -> str:
    import sys
    if sys.platform.startswith("linux"):
        return ("Linux: install the udev rule (sudo cp linux/99-atk-logic.rules /etc/udev/rules.d/ && "
                "sudo udevadm control --reload && sudo udevadm trigger), then re-plug the analyzer; "
                "also make sure the ATK-Logic GUI is closed.")
    if sys.platform == "win32":
        return ("Windows: the device needs a WinUSB/libusb driver (installed by the official ATK-Logic "
                "installer, or bind WinUSB with Zadig to VID 1A86 PID FFCC); close the ATK-Logic GUI.")
    return "Check USB permissions and close the ATK-Logic GUI."


def list_devices() -> List[dict]:
    """Enumerate connected analyzers without opening them."""
    usb1 = _usb1()
    out = []
    with usb1.USBContext() as ctx:
        for dev in ctx.getDeviceIterator(skip_on_error=True):
            try:
                if dev.getVendorID() != P.VENDOR_ID or dev.getProductID() != P.PRODUCT_ID:
                    continue
                ports = []
                try:
                    ports = list(dev.getPortNumberList() or [])
                except Exception:  # noqa: BLE001
                    pass
                out.append({
                    "index": len(out),
                    "bus": dev.getBusNumber(),
                    "address": dev.getDeviceAddress(),
                    "port": dev.getPortNumber(),
                    "port_path": ".".join(str(p) for p in ports),
                    "speed": {1: "low", 2: "full", 3: "high(USB2.0)", 4: "super(USB3.0)",
                              5: "super+(USB3.1)"}.get(dev.getDeviceSpeed(), "unknown"),
                    "vid": "%04x" % P.VENDOR_ID, "pid": "%04x" % P.PRODUCT_ID,
                })
            finally:
                dev.close()
    return out


class UsbTransport(Transport):
    def __init__(self, index: int = 0, bus: Optional[int] = None, address: Optional[int] = None):
        usb1 = self._u = _usb1()
        self._ctx = usb1.USBContext()
        self._ctx.open()
        self._handle = None
        self._claimed = False
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=128)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._pending = 0
        self._lock = threading.Lock()
        self._transfers = []
        self._no_device = False
        try:
            devs = [d for d in self._ctx.getDeviceIterator(skip_on_error=True)
                    if d.getVendorID() == P.VENDOR_ID and d.getProductID() == P.PRODUCT_ID]
            if bus is not None and address is not None:
                devs = [d for d in devs if d.getBusNumber() == bus and d.getDeviceAddress() == address]
            if not devs:
                raise DeviceNotFound(
                    "no ATK-Logic analyzer (USB %04x:%04x) found" % (P.VENDOR_ID, P.PRODUCT_ID),
                    "Plug in the analyzer and check the cable/port (Linux: `lsusb | grep 1a86:ffcc`; "
                    "Windows: Device Manager). Close the ATK-Logic GUI if it is running.")
            if index >= len(devs):
                raise DeviceNotFound("device index %d out of range (%d found)" % (index, len(devs)))
            dev = devs[index]
            self.bus, self.address, self.port = dev.getBusNumber(), dev.getDeviceAddress(), dev.getPortNumber()
            try:
                self._handle = dev.open()
            except usb1.USBErrorBusy as e:
                raise DeviceAccessError("the analyzer is in use by another program (%s)" % e)
            except usb1.USBErrorAccess as e:
                raise DeviceAccessError("permission denied opening the analyzer (%s)" % e, _access_hint())
            except (usb1.USBErrorNotSupported, usb1.USBErrorNotFound) as e:
                raise DeviceAccessError("no usable driver for the analyzer (%s)" % e, _access_hint())
            except usb1.USBError as e:
                raise DeviceAccessError("cannot open the analyzer (%s)" % e, _access_hint())
            try:
                self._handle.setAutoDetachKernelDriver(True)
            except Exception:  # noqa: BLE001 - not supported on Windows
                pass
            try:
                self._handle.claimInterface(P.INTERFACE)
                self._claimed = True
            except usb1.USBErrorBusy as e:
                raise DeviceAccessError("the analyzer is in use by another program (%s)" % e,
                                        "Close the ATK-Logic GUI or other scripts using the device.")
            except usb1.USBError as e:
                raise DeviceAccessError("cannot claim the USB interface (%s)" % e, _access_hint())
        except Exception:
            self.close()
            raise

    # -- sync ---------------------------------------------------------
    def write(self, data: bytes, timeout_ms: int = 1000) -> None:
        u = self._u
        try:
            self._handle.bulkWrite(P.EP_OUT, bytes(data), timeout_ms)
        except u.USBErrorNoDevice:
            self._no_device = True
            raise DeviceStateError("device disconnected", "Re-plug the analyzer.")
        except u.USBErrorTimeout:
            raise DeviceStateError("USB write timed out",
                                   "Re-plug the analyzer; if it persists, update firmware with the official GUI.")
        except u.USBError as e:
            raise DeviceStateError("USB write failed: %s" % e) from e

    def read(self, length: int, timeout_ms: int) -> bytes:
        u = self._u
        try:
            return bytes(self._handle.bulkRead(P.EP_IN, length, timeout_ms))
        except u.USBErrorTimeout as e:
            return bytes(getattr(e, "received", b"") or b"")
        except u.USBErrorNoDevice:
            self._no_device = True
            return b""
        except u.USBErrorOverflow:
            return b""
        except u.USBError as e:
            raise DeviceStateError("USB read failed: %s" % e) from e

    # -- async streaming ----------------------------------------------
    def _cb(self, t):
        u = self._u
        st = t.getStatus()
        if st in (u.TRANSFER_COMPLETED, u.TRANSFER_TIMED_OUT):
            n = t.getActualLength()
            if n > 0:
                try:
                    self._q.put_nowait(bytes(t.getBuffer()[:n]))
                except queue.Full:
                    self.stream_error = self.stream_error or "host USB receive queue overflow"
            if self._running and self.stream_error is None:
                try:
                    t.submit()
                    return
                except Exception as e:  # noqa: BLE001
                    self.stream_error = "resubmit failed: %s" % e
        elif st == u.TRANSFER_NO_DEVICE:
            self._no_device = True
        elif st == u.TRANSFER_OVERFLOW:
            self.stream_error = self.stream_error or "USB transfer overflow"
        elif st != u.TRANSFER_CANCELLED:
            self.stream_error = self.stream_error or "transfer status %s" % st
        with self._lock:
            self._pending -= 1

    def _loop(self):
        while True:
            with self._lock:
                pending = self._pending
            if not self._running and pending <= 0:
                break
            try:
                self._ctx.handleEventsTimeout(tv=0.05)
            except self._u.USBErrorInterrupted:
                continue
            except Exception as e:  # noqa: BLE001
                self.stream_error = self.stream_error or str(e)
                break

    def stream_start(self, n_transfers: int, size: int, timeout_ms: int) -> None:
        self._q = queue.Queue(maxsize=self._q.maxsize)
        self._running = True
        self.stream_error = None
        self._transfers = []
        for _ in range(n_transfers):
            t = self._handle.getTransfer()
            t.setBulk(P.EP_IN, size, callback=self._cb, timeout=timeout_ms)
            self._transfers.append(t)
        for t in self._transfers:
            try:
                t.submit()
            except self._u.USBError as e:
                self.stream_error = "USB transfer submission failed: %s" % e
                break
            with self._lock:
                self._pending += 1
        self._thread = threading.Thread(target=self._loop, name="atk-usb-events", daemon=True)
        self._thread.start()

    def stream_get(self, timeout_s: float) -> Optional[bytes]:
        try:
            return self._q.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def stream_stop(self) -> None:
        self._running = False
        for t in self._transfers:
            try:
                if t.isSubmitted():
                    t.cancel()
            except Exception:  # noqa: BLE001
                pass
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        for t in self._transfers:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass
        self._transfers = []

    @property
    def no_device(self) -> bool:
        return self._no_device

    def close(self) -> None:
        if self._running:
            self.stream_stop()
        if self._handle is not None:
            if self._claimed:
                try:
                    self._handle.releaseInterface(P.INTERFACE)
                except Exception:  # noqa: BLE001
                    pass
            try:
                self._handle.close()
            except Exception:  # noqa: BLE001
                pass
            self._handle = None
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:  # noqa: BLE001
                pass
            self._ctx = None


# ====================================================================== device
@dataclass
class DeviceInfo:
    name: str
    usb: str
    mcu_version: int
    fpga_version: int
    hw_version: int
    level: int
    channels: int = P.CHANNEL_COUNT
    bus: Optional[int] = None
    address: Optional[int] = None
    min_app_version: int = 0

    @property
    def plus(self) -> bool:
        return self.level == 1

    def as_dict(self) -> dict:
        d = asdict(self)
        d["plus"] = self.plus
        d["model"] = self.name + (" Plus" if self.plus and "plus" not in self.name.lower() else "")
        d["max_buffer_rate_hz"] = P.BUFFER_MAX_RATE_PLUS if self.plus else P.BUFFER_MAX_RATE_STD
        if self.plus:
            d["max_buffer_rate_hz_8ch"] = 1_000_000_000
        d["stream_bandwidth_hz"] = P.STREAM_MAX_BANDWIDTH
        return d


@dataclass
class CaptureConfig:
    channels: List[int]
    samplerate: int
    depth: int
    threshold_v: float = DEFAULT_THRESHOLD_V
    mode: str = "buffer"
    rle: bool = False
    triggers: Dict[int, str] = field(default_factory=dict)
    trigger_position: float = 1.0
    timeout_s: Optional[float] = None


def parse_threshold(v: Union[str, float, None]) -> float:
    if v is None:
        return DEFAULT_THRESHOLD_V
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().replace(" ", "").replace("cmos", "")
    if s in THRESHOLD_PRESETS:
        return THRESHOLD_PRESETS[s]
    return float(s.rstrip("v"))


class AtkLogic:
    """One opened analyzer.  Use as a context manager::

        with AtkLogic.open() as la:
            print(la.info)
            cap = la.capture(channels=[0, 1], samplerate=10e6, duration=0.01)
    """

    def __init__(self, transport: Transport, connect: bool = True, log=None):
        self.t = transport
        self.info: Optional[DeviceInfo] = None
        self._log = log or (lambda msg: None)
        self._pwm_on = [False, False]
        self._fpga_awake = False
        self._start_payload_tail = b""
        self._sleep = time.sleep
        self.silence_timeout_s = 2.5  # GUI: ~20 x 100 ms without packets -> "应答超时"
        if connect:
            try:
                self.connect()
            except Exception:
                if self._fpga_awake and not self.t.no_device:
                    try:
                        self.set_reset_state(0)
                    except AtkError:
                        pass
                self.t.close()
                raise

    @classmethod
    def open(cls, index: int = 0, bus: Optional[int] = None, address: Optional[int] = None,
             log=None) -> "AtkLogic":
        return cls(UsbTransport(index, bus, address), log=log)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ----------------------------------------------------------- low level
    def _fpga(self, code: int, payload: bytes = b"") -> None:
        self.t.write(P.fpga_command(code, payload))

    def _mcu(self, cmd: int, payload: bytes = b"") -> None:
        self.t.write(P.mcu_command(cmd, payload))

    def _read_sync(self, length: int = 512, timeout_ms: int = 50):
        """USBControl::ReadSynchronous -> (ok, normalized data)."""
        raw = self.t.read(length, timeout_ms)
        return len(raw) > 0, P.normalize_in(raw)

    def _drain(self, limit: int = 1000) -> int:
        n = 0
        while n < limit:
            ok, _ = self._read_sync()
            if not ok:
                break
            n += 1
        return n

    def set_reset_state(self, state: int) -> None:
        """MCU 0x87: 1 = wake FPGA, 0 = put FPGA to sleep."""
        self._mcu(P.MCU_SET_RESET_STATE, bytes([state & 0xFF]))
        self._fpga_awake = bool(state)

    def _fpga_active(self) -> None:
        if not self._fpga_awake:
            self.set_reset_state(1)
            self._read_sync()

    # ------------------------------------------------------------ connect
    def connect(self, max_attempts: int = 5) -> DeviceInfo:
        """Handshake (ConnectDevice::CheckDeviceCreanInfo)."""
        self._sleep(0.3)
        name, usb, mcu, fpga, hw, level = "", "NULL", 0, 0, 0, 0
        min_app_version = 0
        self._start_payload_tail = b""
        count = 0
        while True:
            # flush stale data, detect a device stuck in bootloader mode
            self._mcu(P.MCU_GET_VERSION)
            garbage = other = 0
            while True:
                ok, d = self._read_sync()
                if not ok:
                    break
                if len(d) >= 512 and d[0] > 200 and d[511] > 200:
                    garbage += 1
                    self._sleep(0.01)
                    if garbage > 120:
                        raise DeviceStateError(
                            "the analyzer looks like it is in bootloader / firmware-update mode",
                            "Open the official ATK-Logic GUI and let it update the firmware; "
                            "this API never flashes firmware.")
                else:
                    other += 1
                    if other == 100:
                        self._fpga(P.CMD_GET_DEVICE_DATA)
                    if other > 900:
                        self._sleep(0.01)
                    if other > 1100:
                        break
            self._log("connect attempt %d" % (count + 1))
            active = False
            if mcu == 0:
                self._mcu(P.MCU_GET_VERSION)
                ok, d = self._read_sync()
                v = P.parse_mcu_version(d) if ok else None
                if v is not None and v.bootloader:
                    raise DeviceStateError(
                        "the analyzer reports bootloader mode (firmware update pending)",
                        "Open the official ATK-Logic GUI to finish the firmware update.")
                if v is not None and v.ok:
                    level, mcu, hw = v.level, v.mcu_version, v.hw_version
                    self.set_reset_state(0)
                    self.set_reset_state(1)
                    self._drain(2000)
                    active = True
            else:
                active = True
            if active:
                self._fpga(P.CMD_GET_DEVICE_DATA)
                raw = self.t.read(2048, 50)
                if raw:
                    for pkt in P.PacketParser().feed(P.normalize_in(raw)):
                        if pkt.order != P.ORDER_DEVICE_INFO:
                            continue
                        fi = P.parse_device_info(pkt.payload)
                        if not fi.status_ok:
                            raise DeviceStateError("FPGA status error reported by the analyzer",
                                                   "Re-plug the analyzer; if it persists use the official GUI.")
                        usb, fpga = fi.usb, fi.fpga_version
                        if P.FPGA_MIN_VERSION_NUM > fpga:
                            raise DeviceStateError(
                                "FPGA firmware %d is older than required %d" % (fpga, P.FPGA_MIN_VERSION_NUM),
                                "Update the firmware with the official ATK-Logic GUI.")
                        min_app_version = fi.min_app_version
                        # This exact DL16 handshake has been verified on USB with both capture modes.
                        verified_dl16 = (fi.name, usb, hw, level, mcu, fpga, min_app_version) == (
                            "DL16", "2.0", 2, 0, 19, 222, 1108)
                        if P.APP_VERSION_NUM < min_app_version and not verified_dl16:
                            raise DeviceStateError(
                                "unverified %s firmware: hardware %d, MCU %d, FPGA %d, minimum host %d"
                                % (fi.name, hw, mcu, fpga, min_app_version),
                                "Use an updated API or the official ATK-Logic application.")
                        if verified_dl16:
                            # The 1.1.x format has an additional trailing zero.
                            # FPGA 222 rejects the legacy form on the 100 MSa/ch Buffer request.
                            self._start_payload_tail = b"\x00"
                        name = fi.name
                        break
            self._sleep(0.3)
            count += 1
            if count > max_attempts or self.t.no_device:
                break
            if name and usb != "NULL" and mcu and fpga and hw:
                break
        if self.t.no_device:
            raise DeviceStateError("device disconnected during connect", "Re-plug the analyzer.")
        if not (name and usb != "NULL" and mcu and fpga and hw):
            raise DeviceStateError(
                "device handshake failed (mcu=%d fpga=%d hw=%d name=%r usb=%s)" % (mcu, fpga, hw, name, usb),
                "Re-plug the analyzer and close other programs using it; update firmware with the official GUI.")
        self.close_all_pwm()
        self._sleep(0.03)
        self.set_reset_state(0)
        self._read_sync()
        self.info = DeviceInfo(name, usb, mcu, fpga, hw, level,
                               bus=getattr(self.t, "bus", None), address=getattr(self.t, "address", None),
                               min_app_version=min_app_version)
        return self.info

    # ---------------------------------------------------------------- PWM
    def pwm(self, index: int, freq_hz: float, duty_percent: float = 50) -> dict:
        """Start PWM output `index` (0/1): 1 Hz..20 MHz, duty 1..99 %."""
        payload = P.encode_pwm(index, freq_hz, duty_percent)
        self._fpga_active()
        self._fpga(P.CMD_PWM, payload)
        self._pwm_on[index] = True
        period = int.from_bytes(payload[1:5], "little")
        high = int.from_bytes(payload[5:9], "little")
        return {"index": index, "requested_hz": freq_hz, "actual_hz": P.PWM_BASE_HZ / period,
                "duty_percent": 100.0 * high / period, "period_ticks": period, "high_ticks": high}

    def pwm_stop(self, index: int) -> None:
        self._fpga(P.CMD_PWM, P.encode_pwm_stop(index))
        self._pwm_on[index] = False
        if not any(self._pwm_on):
            self.set_reset_state(0)

    def close_all_pwm(self) -> None:
        self._fpga(P.CMD_PWM, P.encode_pwm_stop(1))
        self._fpga(P.CMD_PWM, P.encode_pwm_stop(0))
        self._pwm_on = [False, False]

    # ------------------------------------------------------------ capture
    def build_config(self, channels: Iterable[int], samplerate: float,
                     samples: Optional[int] = None, duration: Optional[float] = None,
                     threshold: Union[str, float, None] = None, mode: str = "buffer",
                     rle: bool = False, trigger: Optional[Dict[int, str]] = None,
                     trigger_position: float = 1.0, timeout: Optional[float] = None) -> CaptureConfig:
        """Validate parameters like the GUI does; raises ValueError with a clear message."""
        chans = sorted(set(int(c) for c in channels))
        if not chans:
            raise ValueError("enable at least one channel")
        bad = [c for c in chans if not 0 <= c < P.CHANNEL_COUNT]
        if bad:
            raise ValueError("channel numbers must be 0..15, got %s" % bad)
        mode = mode.lower()
        if mode not in ("buffer", "stream"):
            raise ValueError("mode must be 'buffer' or 'stream'")
        plus = bool(self.info and self.info.plus)
        rate = int(round(float(samplerate)))
        P.rate_index(rate)
        allowed = P.allowed_rates(mode, len(chans), plus)
        if rate not in allowed:
            why = ""
            if mode == "stream":
                why = (" (stream mode: max 100 MHz and rate x channels <= 320 MHz;"
                       " use fewer channels or buffer mode)")
            elif rate == 1_000_000_000:
                why = " (1 GHz needs a Plus model and at most 8 channels)"
            else:
                why = " (500 MHz needs a Plus model)" if rate == 500_000_000 else ""
            raise ValueError("sample rate %s not allowed with %d channel(s) in %s mode%s; allowed: %s"
                             % (P.fmt_hz(rate), len(chans), mode, why,
                                ", ".join(P.fmt_hz(r) for r in allowed)))
        if samples is None and duration is None:
            raise ValueError("give samples or duration")
        depth = int(samples) if samples is not None else int(math.ceil(float(duration) * rate))
        if depth < 1000:
            raise ValueError("capture at least 1000 samples (got %d)" % depth)
        if mode == "buffer":
            cap = P.BUFFER_MAX_BITS_PLUS if plus else P.BUFFER_MAX_BITS_STD
            if rle:
                cap *= P.RLE_DEPTH_FACTOR
            if depth * len(chans) > cap:
                raise ValueError(
                    "buffer too small: %d samples x %d channels > %d (%s); reduce duration/rate/channels%s"
                    % (depth, len(chans), cap, "Plus" if plus else "standard",
                       "" if rle else " or enable RLE (compressible signals only)"))
        if depth >= 1 << 40:
            raise ValueError("too many samples")
        trig = {}
        for k, v in (trigger or {}).items():
            k = int(k)
            if k not in chans:
                raise ValueError("trigger channel %d is not an enabled channel" % k)
            t = P.normalize_trigger(v)
            if t != "none":
                trig[k] = t
        if not 1 <= float(trigger_position) <= 90:
            raise ValueError("trigger_position must be 1..90 %")
        # The GUI disables pre-trigger position in Stream. Send the default,
        # and use any trigger position reported by the device in the result.
        trigger_position = 1.0 if mode == "stream" else float(trigger_position)
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be a finite positive number of seconds")
        thr = parse_threshold(threshold)
        P.encode_threshold(thr)
        return CaptureConfig(chans, rate, depth, thr, mode, bool(rle), trig,
                             float(trigger_position), timeout)

    def capture(self, channels: Iterable[int], samplerate: float, samples: Optional[int] = None,
                duration: Optional[float] = None, threshold: Union[str, float, None] = None,
                mode: str = "buffer", rle: bool = False, trigger: Optional[Dict[int, str]] = None,
                trigger_position: float = 1.0, timeout: Optional[float] = None,
                progress=None, *, stop_event=None, immediate: Optional[bool] = None) -> Capture:
        """Acquire samples.

        channels:   iterable of channel numbers 0..15
        samplerate: Hz, one of protocol.SAMPLE_RATES
        samples / duration: capture length (per channel)
        threshold:  switching threshold in volts (-5..5) or preset "3.3V", "1.8V"...
        mode:       'buffer' (device memory, triggers, highest rates) or
                    'stream' (continuous USB streaming, <=100 MHz, rate*channels<=320 MHz)
        rle:        hardware run-length compression
        trigger:    {channel: 'rising'|'falling'|'high'|'low'|'edge'}; empty = start immediately
        trigger_position: percent of the buffer before the trigger (1..90, Buffer only)
        timeout:    capture deadline in seconds, separate from the USB inactivity limit
        stop_event: optional event checked during acquisition, equivalent to Ctrl+C
        immediate:  True bypasses triggers, False uses ordinary start, None infers from trigger settings
        """
        cfg = self.build_config(channels, samplerate, samples, duration, threshold, mode, rle,
                                trigger, trigger_position, timeout)
        return self.capture_config(cfg, progress, stop_event=stop_event, immediate=immediate)

    def capture_config(self, cfg: CaptureConfig, progress=None, *, stop_event=None,
                       immediate: Optional[bool] = None) -> Capture:
        is_buffer = cfg.mode == "buffer"
        depth = cfg.depth
        trig_depth = P.trigger_depth(depth, cfg.trigger_position if is_buffer else 1.0)
        settings = P.encode_settings(is_buffer, cfg.rle, cfg.threshold_v, cfg.samplerate, depth, trig_depth)
        instant = not cfg.triggers if immediate is None else immediate
        trig_bytes = P.encode_trigger(cfg.channels, cfg.triggers, instant) + self._start_payload_tail
        seconds = depth / cfg.samplerate
        n_xfer = P.read_transfer_count(depth, len(cfg.channels), seconds)
        warnings: List[str] = []

        # --- ThreadWork::DeviceRecvThread
        parser = P.PacketParser()
        n_ch = P.CHANNEL_COUNT
        data = {c: bytearray() for c in cfg.channels}
        got = {c: 0 for c in range(n_ch)}  # bytes appended per channel
        offsets = [0] * n_ch  # start offset (bits) still to drop
        trigger_pos = 0
        offset_seen = False
        exceed_capacity = exceed_bandwidth = host_overflow = False
        armed = False
        state, reason = "running", None
        error = None
        start_time = datetime.datetime.now()
        t0 = last_packet = time.monotonic()
        triggered = instant or not cfg.triggers
        last_prog = -1
        start_sent = stream_started = False

        def mark_armed():
            nonlocal armed
            if not armed:
                armed = True
                if progress:
                    progress("armed", 0)

        try:
            # --- Session::OrderStart
            if stop_event is not None and stop_event.is_set():
                raise KeyboardInterrupt
            self._fpga_active()
            self._drain(1000)
            self._fpga(P.CMD_PARAMETER_SETTING, settings)
            self._sleep(0.03)  # FPGA needs time to settle the threshold voltage
            if stop_event is not None and stop_event.is_set():
                raise KeyboardInterrupt
            start_time = datetime.datetime.now()
            t0 = last_packet = time.monotonic()
            start_sent = True
            self._fpga(P.CMD_SIMPLE_TRIGGER, trig_bytes)
            stream_started = True
            self.t.stream_start(n_xfer, P.READ_TRANSFER_SIZE, 1200)
            while state == "running":
                if self.t.no_device:
                    state, reason = "nodevice", "disconnected"
                    error = "device disconnected during capture"
                    break
                if self.t.stream_error:
                    raise CaptureError(self.t.stream_error)
                if stop_event is not None and stop_event.is_set():
                    raise KeyboardInterrupt
                now = time.monotonic()
                if cfg.timeout_s is not None and now - t0 >= cfg.timeout_s:
                    state, reason = "timeout", "deadline"
                    error = "capture did not finish within %.1f s" % cfg.timeout_s
                    if not triggered:
                        reason = "trigger_timeout"
                        error = "no trigger within %.1f s" % cfg.timeout_s
                    break
                if now - last_packet >= self.silence_timeout_s:
                    state, reason = "timeout", "communication_timeout"
                    error = "device stopped answering (communication timeout)"
                    break
                wait = min(0.1, self.silence_timeout_s - (now - last_packet))
                if cfg.timeout_s is not None:
                    wait = min(wait, cfg.timeout_s - (now - t0))
                chunk = self.t.stream_get(wait)
                if chunk is None:
                    continue
                for pkt in parser.feed(P.normalize_stream(chunk)):
                    last_packet = time.monotonic()
                    o, p = pkt.order, pkt.payload
                    if o == P.ORDER_SAMPLES:
                        if len(p) < 3 or (cfg.rle and len(p) % 2):
                            raise CaptureError("malformed sample packet")
                        ch = p[0]
                        if ch >= n_ch:
                            raise CaptureError("data packet for invalid channel %d" % ch,
                                               "Re-plug the analyzer and try again.")
                        if ch not in data:
                            continue
                        d = P.rle_expand(p[2:]) if cfg.rle else bytes(p[2:])
                        if d:
                            mark_armed()
                            triggered = True
                        off = offsets[ch]
                        if off > 7:  # drop whole bytes now, remaining bits at the end
                            rb = off // 8
                            if len(d) > rb:
                                d = d[rb:]
                                offsets[ch] = off & 7
                            else:
                                offsets[ch] = off - len(d) * 8
                                d = b""
                        if d:
                            # The final byte may hold padding. Stop retaining data beyond
                            # the requested sample count plus the pending bit offset.
                            keep = (depth + offsets[ch] + 7) // 8 - got[ch]
                            if keep > 0:
                                data[ch].extend(d[:keep])
                            got[ch] += len(d)
                        if progress and cfg.channels and ch == cfg.channels[0]:
                            pr = min(99, int(got[ch] * 8 * 100 / depth))
                            if pr != last_prog:
                                last_prog = pr
                                progress("transfer", pr)
                    elif o == P.ORDER_OFFSET:
                        if offset_seen:
                            raise CaptureError("unexpected second offset packet",
                                               "Re-plug the analyzer and try again.")
                        offset_seen = True
                        if len(p) < 7:
                            raise CaptureError("malformed offset packet")
                        trigger_pos = P.u40(p[2:7])
                        rest = p[7:]
                        if is_buffer:
                            if len(rest) < n_ch * 5:
                                raise CaptureError("malformed offset packet (channel counts)")
                            counts = [P.u40(rest[i * 5:i * 5 + 5]) for i in range(n_ch)]
                            for i, cnt in enumerate(counts):
                                if cnt and cnt * 8 > depth:
                                    offsets[i] = max(0, cnt * 8 - trig_depth - trigger_pos)
                                else:
                                    offsets[i] = 0
                            rest = rest[n_ch * 5:]
                            if cfg.rle:
                                if len(rest) < 1:
                                    raise CaptureError("malformed offset packet (flags)")
                                exceed_capacity = bool(rest[0] & 1)
                                exceed_bandwidth = bool(rest[0] & 2)
                                if exceed_capacity:
                                    nz = [counts[i] for i in cfg.channels if counts[i]]
                                    mn = min(nz) if nz else 0
                                    for i in range(n_ch):
                                        offsets[i] = (counts[i] - mn) * 8 if (counts[i] and i in cfg.channels) else 0
                                    state, reason = "overflow", "capacity_overflow"
                                if exceed_bandwidth:
                                    state, reason = "overflow", "bandwidth_overflow"
                        triggered = True
                    elif o == P.ORDER_REPLY:
                        if len(p) >= 3:
                            code = p[2]
                            if code == P.CMD_STOP:
                                state, reason = "error", "unexpected_stop"
                                error = "device reported a stop before capture completion"
                            elif code == P.CMD_SIMPLE_TRIGGER:
                                if len(p) < 4:
                                    raise CaptureError("malformed capture-start response")
                                if p[3] != 3:
                                    raise CaptureError("the analyzer rejected the capture parameters (status 0x%02x)" % p[3],
                                                       "Choose another sample rate / depth / channel set.")
                                mark_armed()
                    elif o == P.ORDER_END:
                        state, reason = "done", "device_end"
                        if not is_buffer and len(p) >= 3 and p[2] == 1:
                            exceed_capacity = True
                            state, reason = "overflow", "capacity_overflow"
                    elif o == P.ORDER_PROGRESS:
                        if len(p) < 7:
                            raise CaptureError("malformed progress packet")
                        val = P.u40(p[2:7])
                        mark_armed()
                        if is_buffer:
                            if val > trig_depth:
                                triggered = True
                            if progress:
                                pr = min(100, int(val * 100 / depth)) if depth else 0
                                progress("triggered" if val > trig_depth else "pre-trigger", pr)
                        else:
                            # In Stream, progress zero reports that the device is waiting for the trigger.
                            triggered = triggered or val != 0
                            if progress:
                                progress("stream" if triggered else "waiting-trigger", 0)
                    if state != "running":
                        break
        except KeyboardInterrupt:
            state, reason = "stopped", "requested_stop"
        except CaptureError as e:
            state, reason, error = "error", "capture_error", str(e)
        except DeviceStateError as e:
            state, reason, error = "error", "transport_error", str(e)
        finally:
            # end of DeviceRecvThread: stop reader, send Stop, flush the MCU
            if stream_started:
                self.t.stream_stop()
            if start_sent and not self.t.no_device:
                try:
                    self._fpga(P.CMD_STOP)
                    # ATK-Logic sleeps the FPGA at the end of each acquisition
                    # unless PWM is running. The next acquisition starts with a clean wake.
                    if not any(self._pwm_on):
                        self.set_reset_state(0)
                    self._drain(1000)
                except AtkError as e:
                    if state in ("done", "stopped"):
                        state, reason, error = "error", "cleanup_error", str(e)

        if self.t.stream_error and stream_started:
            error = self.t.stream_error
            host_overflow = "overflow" in error.lower()
            state, reason = (("overflow", "transport_overflow") if host_overflow
                             else ("error", "transport_error"))

        if exceed_bandwidth:
            warnings.append("data exceeded the hardware bandwidth; data is invalid")
        if exceed_capacity:
            warnings.append("data exceeded the hardware capacity")
        if error:
            warnings.append(error)

        chans: Dict[int, Channel] = {}
        channel_samples = {}
        discard = is_buffer and reason == "requested_stop"
        if discard:
            warnings.append("buffer capture cancelled; DL16 does not upload a stopped buffer")
        for c in cfg.channels:
            raw = b"" if discard else data[c]
            packed = np.frombuffer(memoryview(raw).toreadonly(), np.uint8)
            nbits = min(max(0, len(packed) * 8 - offsets[c]), depth)
            packed = shift_packed(packed, offsets[c])  # remaining start offset (bits)
            channel_samples[c] = nbits
            chans[c] = Channel(c, packed, nbits)
        n = min(channel_samples.values())
        if n == 0 and state in ("error", "timeout", "nodevice") and error:
            raise CaptureError(error, "Check wiring/trigger settings; re-plug the analyzer if it persists.")
        if state == "done" and n < depth:
            state, reason = "short", "short_capture"
            warnings.append("received %d of %d samples" % (n, depth))
        if state == "done" and is_buffer and not offset_seen:
            state, reason = "error", "missing_buffer_offset"
            warnings.append("buffer capture ended without channel alignment information")
        trig_sample = trig_depth if is_buffer else (trigger_pos or None)
        if trig_sample is not None and trig_sample >= n:
            trig_sample = None
        meta = {
            "source": "device",
            "device": self.info.as_dict() if self.info else None,
            "mode": cfg.mode, "rle": cfg.rle, "threshold_v": cfg.threshold_v,
            "requested_samples": depth,
            "triggers": {str(k): v for k, v in cfg.triggers.items()},
            "trigger_position_percent": cfg.trigger_position if is_buffer else None,
            "state": state,
            "end_reason": reason,
            "armed": armed,
            "immediate": instant,
            "complete": state == "done" and armed,
            "partial_exportable": state == "stopped" and not is_buffer and n > 0,
            "overflow": exceed_bandwidth or exceed_capacity or host_overflow,
            "channel_samples": channel_samples,
            "warnings": warnings,
            "elapsed_s": round(time.monotonic() - t0, 3),
        }
        return Capture(cfg.samplerate, chans, n, trig_sample, meta, start_time)

    # -------------------------------------------------------------- close
    def close(self, keep_pwm: bool = False) -> None:
        """Shutdown like the GUI on exit; keep_pwm=True leaves PWM outputs running
        (skips CloseAllPWM / Exit / sleep) and only releases the USB interface."""
        try:
            if not keep_pwm and self.info is not None and not self.t.no_device:
                self.set_reset_state(1)
                self.close_all_pwm()
                self._fpga(P.CMD_EXIT)
                self.set_reset_state(0)
        except AtkError:
            pass
        finally:
            self.t.close()
