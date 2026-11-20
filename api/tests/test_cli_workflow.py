import json
from pathlib import Path

import pytest

from atk_logic import cli, load
from atk_logic.capture import Capture
from atk_logic.device import AtkLogic
from fake_device import FakeDevice


def fake_analyzer(monkeypatch):
    transport = FakeDevice({}, level=0)
    analyzer = AtkLogic(transport, connect=False)
    analyzer._sleep = lambda seconds: None
    analyzer.connect()
    monkeypatch.setattr(cli, "_open", lambda args: analyzer)
    return transport


def test_preset_overrides_and_armed_event(monkeypatch, tmp_path, capsys):
    transport = fake_analyzer(monkeypatch)
    preset = Path(__file__).parents[1] / "presets" / "jr-three-channel.json"
    output = tmp_path / "jr.atkdl"
    assert cli.main(["capture", "--preset", str(preset), "--samples", "1024",
                     "--events", "-o", str(output)]) == 0
    result = capsys.readouterr()
    summary = json.loads(result.out)["capture"]
    assert json.loads(result.err) == {"event": "armed", "cycle": 1}
    assert summary["samples"] == 1024 and summary["meta"]["complete"]
    assert transport.trigger_bytes[-1] == 1 and transport.closed
    capture = load(str(output))
    assert capture.n_samples == 1024 and tuple(capture.channels) == (0, 1, 2)


def test_repeat_exports_independent_normal_captures(monkeypatch, tmp_path, capsys):
    transport = fake_analyzer(monkeypatch)
    output = tmp_path / "series.atkdl"
    assert cli.main(["capture", "-c", "0", "-r", "1M", "-n", "1024", "-o", str(output),
                     "--acquisition", "repeat", "--repeat-count", "2", "--interval", "1ms"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["cycle"] for row in rows] == [1, 2]
    assert [Path(row["saved"][0]).name for row in rows] == ["series.0001.atkdl", "series.0002.atkdl"]
    assert all(Path(row["saved"][0]).exists() for row in rows)
    assert transport.trigger_bytes[-1] == 0 and transport.closed
    assert load(rows[0]["saved"][0]).meta["settings"]["isInstantly"] is False


@pytest.mark.parametrize("options", [
    ["--acquisition", "rolling"], ["--stop-mode", "upload"],
    ["--mode", "stream", "--rle"], ["--mode", "stream", "--trigger-pos", "10"],
    ["--mode", "stream", "-c", "0-3", "-r", "100M"],
])
def test_invalid_requests_do_not_open_usb(monkeypatch, options):
    def unexpected_open(args):
        pytest.fail("invalid request attempted to open the device")

    monkeypatch.setattr(cli, "_open", unexpected_open)
    assert cli.main(["capture", "-c", "0", "-r", "10M", "-n", "1024", *options]) == 2


def test_incomplete_capture_does_not_replace_output(monkeypatch, tmp_path, capsys):
    class ShortCapture:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def capture(self, **kwargs):
            return Capture.from_bits(1e6, {0: [1, 0]}, meta={
                "complete": False, "end_reason": "short_capture", "channel_samples": {0: 2},
            })

    monkeypatch.setattr(cli, "_open", lambda args: ShortCapture())
    output = tmp_path / "evidence.atkdl"
    output.write_bytes(b"previous evidence")
    assert cli.main(["capture", "-c", "0", "-r", "1M", "-n", "1024", "-o", str(output)]) == 6
    result = json.loads(capsys.readouterr().out)
    assert result["capture"]["meta"]["end_reason"] == "short_capture"
    assert output.read_bytes() == b"previous evidence"
    assert list(tmp_path.iterdir()) == [output]
