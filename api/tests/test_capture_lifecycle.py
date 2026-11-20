import threading

import numpy as np
import pytest
import usb1

from atk_logic import device
from atk_logic import protocol as P
from fake_device import FakeDevice, packet, to_wire
from test_device_fake import make


def reply(status):
    return packet(P.ORDER_REPLY, bytes([0, 0, P.CMD_SIMPLE_TRIGGER, status]))


def samples(channel, data):
    return packet(P.ORDER_SAMPLES, bytes([channel, 0]) + data)


END = packet(P.ORDER_END, b"\0\0\0")


class ScriptedDevice(FakeDevice):
    def __init__(self, *batches, stop_event=None, request_stop=False):
        super().__init__({})
        self.batches = batches
        self.stop_event = stop_event
        self.request_stop = request_stop

    def _generate(self):
        for batch in self.batches:
            self.chunks.extend(to_wire(batch) if batch else [None])

    def stream_get(self, timeout_s):
        if self.chunks:
            return super().stream_get(timeout_s)
        if self.stop_event is not None:
            self.stop_event.set()
        if self.request_stop:
            raise KeyboardInterrupt
        return None


def record(analyzer, progress=None, mode="stream", **kwargs):
    return analyzer.capture(channels=[0, 1], samplerate=10e6, samples=1000,
                            mode=mode, progress=progress, **kwargs)


def test_positive_ack_arms_before_delayed_data_once():
    fake = ScriptedDevice(reply(3), None,
                          samples(0, b"\xaa" * 125) + samples(1, b"\x55" * 125) + END)
    stages = []

    def progress(stage, _value):
        stages.append((stage, len(fake.chunks)))

    cap = record(make(fake), progress)

    assert stages[0] == ("armed", 2)
    assert [stage for stage, _ in stages].count("armed") == 1
    assert cap.meta["complete"] and cap.meta["armed"]
    assert cap.meta["end_reason"] == "device_end"
    assert cap.meta["channel_samples"] == {0: 1000, 1: 1000}


def test_busy_usb_claim_releases_attempt_without_starting_device(monkeypatch):
    events = []

    class BusyDevice:
        def getVendorID(self):
            return P.VENDOR_ID

        def getProductID(self):
            return P.PRODUCT_ID

        def getBusNumber(self):
            return 1

        def getDeviceAddress(self):
            return 2

        def getPortNumber(self):
            return 3

        def open(self):
            return self

        def claimInterface(self, interface):
            events.append(("claim", interface))
            raise usb1.USBErrorBusy()

        def close(self):
            events.append("handle closed")

    class BusyContext:
        def open(self):
            pass

        def getDeviceIterator(self, **kwargs):
            return iter([BusyDevice()])

        def close(self):
            events.append("context closed")

    monkeypatch.setattr(usb1, "USBContext", BusyContext)
    with pytest.raises(device.DeviceAccessError, match="in use"):
        device.AtkLogic.open()
    assert events == [("claim", P.INTERFACE), "handle closed", "context closed"]


def test_stream_normal_waits_for_trigger_and_uses_reported_position():
    waiting = packet(P.ORDER_PROGRESS, bytes(7))
    triggered = packet(P.ORDER_PROGRESS, bytes(2) + (1).to_bytes(5, "little"))
    position = packet(P.ORDER_OFFSET, bytes(2) + (42).to_bytes(5, "little"))
    data = samples(0, b"\xaa" * 125) + samples(1, b"\x55" * 125)
    fake = ScriptedDevice(reply(3) + waiting, triggered + position + data + END)
    stages = []
    cap = record(make(fake), lambda stage, value: stages.append(stage),
                 trigger={1: "rising"}, immediate=False)

    assert stages[:3] == ["armed", "waiting-trigger", "stream"]
    assert fake.trigger_bytes == bytes([0xF9]) + bytes(8)
    assert fake.settings["trig_depth"] == 10
    assert cap.meta["complete"] and cap.trigger_sample == 42


def test_rejected_ack_never_arms():
    seen = []
    with pytest.raises(device.CaptureError, match="rejected"):
        record(make(ScriptedDevice(reply(1))), lambda *args: seen.append(args))
    assert seen == []


def test_data_without_ack_arms_and_short_capture_is_incomplete():
    cap = record(make(ScriptedDevice(samples(0, b"\xaa" * 125) +
                                     samples(1, b"\x55" * 35) + END)))
    assert cap.n_samples == 280
    assert cap.meta["armed"] and not cap.meta["complete"]
    assert cap.meta["end_reason"] == "short_capture"
    assert cap.meta["channel_samples"] == {0: 1000, 1: 280}
    assert not cap.meta["partial_exportable"]


@pytest.mark.parametrize("by_keyboard", [False, True])
def test_stream_requested_stop_keeps_only_common_samples(by_keyboard):
    event = threading.Event() if not by_keyboard else None
    fake = ScriptedDevice(samples(0, b"\xff" * 120) + samples(1, b"\x00" * 30),
                          stop_event=event, request_stop=by_keyboard)
    cap = record(make(fake), stop_event=event)

    assert cap.n_samples == 240
    np.testing.assert_array_equal(cap.channel(0).bits(), np.ones(240))
    np.testing.assert_array_equal(cap.channel(1).bits(), np.zeros(240))
    assert cap.meta["state"] == "stopped"
    assert cap.meta["end_reason"] == "requested_stop"
    assert cap.meta["partial_exportable"] and not cap.meta["complete"]
    assert not fake.streaming and ("fpga", P.CMD_STOP, b"") in fake.log


def test_buffer_requested_stop_discards_received_samples():
    event = threading.Event()
    fake = ScriptedDevice(samples(0, b"\xff" * 120) + samples(1, b"\x00" * 30),
                          stop_event=event)
    cap = record(make(fake), mode="buffer", stop_event=event)

    assert cap.meta["end_reason"] == "requested_stop"
    assert not cap.meta["partial_exportable"] and not cap.meta["complete"]
    assert cap.n_samples == 0
    assert not fake.streaming and ("fpga", P.CMD_STOP, b"") in fake.log


def test_stop_before_start_does_not_send_capture_commands():
    fake = FakeDevice({})
    analyzer = make(fake)
    before = list(fake.log)
    event = threading.Event()
    event.set()

    cap = record(analyzer, stop_event=event)

    assert cap.meta["end_reason"] == "requested_stop" and not cap.meta["armed"]
    assert fake.log == before


def test_deadline_applies_with_continuous_packets(monkeypatch):
    fake = ScriptedDevice(samples(0, b"\xaa" * 30) + samples(1, b"\x55" * 30),
                          *([packet(P.ORDER_PROGRESS, b"\0" * 7)] * 10), END)
    analyzer = make(fake)
    ticks = iter(range(100))
    monkeypatch.setattr(device.time, "monotonic", lambda: next(ticks) / 100)

    cap = record(analyzer, timeout=0.1)

    assert cap.n_samples == 240
    assert cap.meta["state"] == "timeout" and cap.meta["end_reason"] == "deadline"
    assert not cap.meta["complete"] and not cap.meta["partial_exportable"]


def test_overflow_invalidates_full_length_stream():
    fake = FakeDevice({}, stream_overflow=True)
    cap = record(make(fake))

    assert cap.n_samples == 1000
    assert cap.meta["overflow"] and cap.meta["end_reason"] == "capacity_overflow"
    assert not cap.meta["complete"] and not cap.meta["partial_exportable"]


def test_host_receive_overflow_invalidates_partial_stream():
    class OverflowDevice(ScriptedDevice):
        def stream_get(self, timeout_s):
            batch = super().stream_get(timeout_s)
            if batch is None:
                self.stream_error = "host USB receive queue overflow"
            return batch

    cap = record(make(OverflowDevice(samples(0, b"\xff" * 30) + samples(1, b"\x00" * 30))))

    assert cap.n_samples == 240
    assert cap.meta["overflow"] and cap.meta["end_reason"] == "transport_overflow"
    assert not cap.meta["complete"] and not cap.meta["partial_exportable"]


def test_buffer_needs_alignment_even_when_all_samples_arrive():
    fake = ScriptedDevice(samples(0, b"\xaa" * 125) + samples(1, b"\x55" * 125) + END)
    cap = record(make(fake), mode="buffer")

    assert cap.n_samples == 1000
    assert cap.meta["end_reason"] == "missing_buffer_offset"
    assert not cap.meta["complete"]


def test_normal_and_immediate_flags_are_explicit():
    fake = FakeDevice({})
    analyzer = make(fake)
    record(analyzer, mode="buffer", immediate=False)
    assert fake.trigger_bytes[-1] == 0
    record(analyzer, mode="buffer", immediate=True, trigger={0: "rising"})
    assert fake.trigger_bytes[-1] == 1
    record(analyzer, mode="buffer")
    assert fake.trigger_bytes[-1] == 1


def test_consecutive_captures_wake_and_sleep_the_fpga_when_pwm_is_off():
    fake = FakeDevice({})
    analyzer = make(fake)
    fake.log.clear()
    for _ in range(2):
        assert record(analyzer, mode="buffer", immediate=False).meta["complete"]
    assert [payload[0] for side, code, payload in fake.log
            if (side, code) == ("mcu", P.MCU_SET_RESET_STATE)] == [1, 0, 1, 0]


def test_pwm_and_capture_in_same_session_do_not_cycle_fpga_power():
    fake = FakeDevice({})
    analyzer = make(fake)
    analyzer.pwm(0, 1000)
    before = len(fake.log)

    analyzer.pwm(1, 1000)
    record(analyzer)
    analyzer.pwm_stop(0)

    operations = fake.log[before:]
    assert not any(item[:2] == ("mcu", P.MCU_SET_RESET_STATE) for item in operations)
    assert [item[2][0] for item in operations if item[:2] == ("fpga", P.CMD_PWM)] == [0x21, 0x10]

    analyzer.pwm_stop(1)
    assert fake.log[-2:] == [("fpga", P.CMD_PWM, b"\x20"),
                            ("mcu", P.MCU_SET_RESET_STATE, b"\x00\x00")]
