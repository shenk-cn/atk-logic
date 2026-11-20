from datetime import datetime
from zipfile import ZipFile

import numpy as np
import pytest

from atk_logic import Capture, Channel, read_atkdl
from atk_logic.atkdl import BLOCK_BYTES, NODE_BLOCKS


def test_atkdl_native_headers_blocks_and_roundtrip(tmp_path):
    n = BLOCK_BYTES * 8 + 3
    mixed = np.zeros(BLOCK_BYTES + 1, dtype=np.uint8)
    mixed[-1] = 0xFC
    high = np.full(BLOCK_BYTES + 1, 0xFF, dtype=np.uint8)
    start = datetime(2025, 1, 2, 3, 4, 5, 678000)
    capture = Capture(10_000_000, {
        1: Channel(1, mixed, n, "JR=数字"),
        7: Channel(7, high, n, "DSC"),
    }, n, 1_000_000, meta={"mode": "stream", "threshold_v": 1.6}, start_time=start)
    path = str(tmp_path / "capture.atkdl")

    assert capture.save(path) == path
    assert mixed[-1] == 0xFC
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        assert archive.read("1/channel.ini").decode().splitlines() == [
            "JR=数字", "0", str(n), "-1", "-1", "1,0",
        ]
        assert archive.read("7/channel.ini").decode().splitlines()[5] == "3,3"
        assert archive.read("15/channel.ini").decode().splitlines()[1:5] == ["0", "0", "-1", "-1"]
        assert archive.getinfo("1/0-1.bin").file_size == BLOCK_BYTES
        assert archive.read("1/0-1.bin") == b"\x04" + bytes(BLOCK_BYTES - 1)
        assert archive.read("vernier.ini") == b"Trigger,ff26a3d9,100000000,\n"
        assert b"CollectDate=2025-01-02 03:04:05.678\n" in archive.read("channel.ini")

    actual = read_atkdl(path)
    assert actual.start_time == start
    assert actual.samplerate == capture.samplerate
    assert actual.n_samples == n
    assert actual.trigger_sample == 1_000_000
    assert actual.meta["settings"]["mode"] == "stream"
    assert actual.meta["settings"]["settingData"]["thresholdLevel"] == 1.6
    assert actual.meta["settings"]["channelsSet"][1]["name"] == "JR=数字"
    assert actual.summary(include_stats=False)["channels"] == [
        {"channel": 1, "name": "JR=数字", "samples": n},
        {"channel": 7, "name": "DSC", "samples": n},
    ]
    assert list(actual.channels) == [1, 7]
    for index in actual.channels:
        np.testing.assert_array_equal(actual.channel(index).bits(n - 11),
                                      capture.channel(index).bits(n - 11))


@pytest.mark.parametrize("trigger", [None, 0])
def test_atkdl_distinguishes_missing_trigger_and_trigger_at_start(tmp_path, trigger):
    capture = Capture.from_bits(1_000_000, {0: [0, 1, 0]}, trigger_sample=trigger)
    path = str(tmp_path / "capture.atkdl")
    capture.to_atkdl(path)
    assert read_atkdl(path).trigger_sample == trigger


def test_atkdl_first_block_in_second_node(tmp_path):
    second_node_byte = BLOCK_BYTES * NODE_BLOCKS
    data = np.zeros(second_node_byte + 1, dtype=np.uint8)
    data[second_node_byte] = 1
    n = len(data) * 8
    capture = Capture(10_000_000, {2: Channel(2, data, n)}, n)
    path = str(tmp_path / "second-node.atkdl")
    capture.save(path)
    with ZipFile(path) as archive:
        assert archive.getinfo("2/1-0.bin").file_size == BLOCK_BYTES
        assert archive.read("2/channel.ini").decode().splitlines()[5:] == [
            f"{(1 << NODE_BLOCKS) - 1},0", "0,0",
        ]
    restored = read_atkdl(path).channel(2)
    assert restored.bits(n - 9).tolist() == [0, 1, 0, 0, 0, 0, 0, 0, 0]
