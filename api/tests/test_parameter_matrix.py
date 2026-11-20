import pytest

from atk_logic import protocol as P
from atk_logic.device import AtkLogic
from fake_device import FakeDevice


@pytest.mark.parametrize(
    "rate, rle, rate_code, samples, pretrigger",
    [
        (10_000_000, False, 5, 100_000_000, 1_000_000),
        (100_000_000, True, 10, 1_000_000_000, 10_000_000),
    ],
)
def test_dl16_three_channel_ten_second_buffer(rate, rle, rate_code, samples, pretrigger):
    analyzer = AtkLogic(FakeDevice({}, level=0), connect=False)
    config = analyzer.build_config([0, 1, 2], rate, duration=10, threshold=1.6, rle=rle)
    assert config.depth == samples

    if rle:
        with pytest.raises(ValueError, match="buffer too small"):
            analyzer.build_config([0, 1, 2], rate, duration=10, threshold=1.6)

    settings = P.encode_settings(
        True, config.rle, config.threshold_v, config.samplerate,
        config.depth, P.trigger_depth(config.depth, config.trigger_position),
    )
    assert settings[:3] == bytes([0xC0 if rle else 0x80, 16, rate_code])
    assert P.u40(settings[3:8]) == samples
    assert P.u40(settings[8:13]) == pretrigger


def test_three_channels_normal_and_immediate_packets_differ_without_triggers():
    channel_mask = b"\xff\xf0" + bytes(6)

    assert P.encode_trigger([0, 1, 2], {}, instant=False) == channel_mask + b"\x00"
    assert P.encode_trigger([0, 1, 2], {}, instant=True) == channel_mask + b"\x01"
