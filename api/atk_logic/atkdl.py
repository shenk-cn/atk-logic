"""Read and write ATK-Logic project files (*.atkdl).

Format (ThreadWork::SaveSourceFileThreadPara / LoadSourceFileThreadPara):
  zip archive containing
    channel.ini           SessionName=, SamplingFrequency=<kHz>, SamplingDepth=,
                          TriggerSamplingDepth=, Decodes=<id,id..>, GlitchRemoval=
    set.ini               GUI session settings (QSettings ini, JSON values)
    vernier.ini / measure.ini
    <decodeId>            JSON describing a decoder stack configured in the GUI
    <ch>/channel.ini      name, start offset (bits), max sample including offset,
                          two optional range fields, then "tog,value" per node
    <ch>/<node>-<blk>.bin 1 MiB raw block (LSB-first packed samples)
  A block whose bit is set in `tog` is constant (= corresponding bit of `value`);
  a block without .bin file and without tog bit reads as 0.
"""

from __future__ import annotations

import configparser
from datetime import datetime
import json
import re
import zipfile
from typing import Dict, List, Optional

import numpy as np

from .capture import Capture, Channel, shift_packed
from .protocol import CHANNEL_COUNT, SAMPLE_RATES

BLOCK_BYTES = 1024 * 1024
NODE_BLOCKS = 64
_GUI_DURATIONS_MS = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000)


def _lines(raw: bytes) -> List[str]:
    return raw.decode("utf-8-sig", "replace").replace("\r", "").split("\n")


def _ini(raw: bytes) -> Dict[str, str]:
    d = {}
    for ln in _lines(raw):
        if "=" in ln:
            k, v = ln.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def read_atkdl(path: str, channels: Optional[List[int]] = None) -> Capture:
    """Load an .atkdl project into a Capture (only channels that hold data)."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        top = _ini(z.read("channel.ini")) if "channel.ini" in names else {}
        freq_khz = int(top.get("SamplingFrequency", "0") or 0)
        if freq_khz <= 0:
            raise ValueError("%s: missing SamplingFrequency" % path)
        depth = int(top.get("SamplingDepth", "0") or 0)
        trig = int(top.get("TriggerSamplingDepth", "-1") or -1)

        settings = _read_settings(z, names)
        decoders = []
        for did in (top.get("Decodes") or "").split(","):
            did = did.strip()
            if did and did in names:
                try:
                    decoders.append(_decoder_stack(json.loads(z.read(did).decode("utf-8"))))
                except Exception as e:  # keep loading the samples
                    decoders.append({"error": str(e), "id": did})

        bins_by_ch: Dict[int, Dict[int, str]] = {}
        for n in names:
            m = re.match(r"^(\d+)/(\d+)-(\d+)\.bin$", n)
            if m:
                ch, node, blk = (int(x) for x in m.groups())
                bins_by_ch.setdefault(ch, {})[node * NODE_BLOCKS + blk] = n

        chans: Dict[int, Channel] = {}
        lengths = []
        ch_ids = sorted(int(n.split("/")[0]) for n in names
                        if re.match(r"^\d+/channel\.ini$", n))
        for ch in ch_ids:
            if channels is not None and ch not in channels:
                continue
            ls = _lines(z.read("%d/channel.ini" % ch))
            name = ls[0] if ls else "Channel %d" % ch
            offset = int(ls[1] or 0) if len(ls) > 1 else 0
            max_sample = int(ls[2] or 0) if len(ls) > 2 else 0
            nodes = []
            for ln in ls[3:]:
                if "," in ln:
                    t, v = ln.split(",", 1)
                    nodes.append((int(t), int(v)))
            has_tog = any(t for t, _ in nodes)
            if max_sample <= offset or not (bins_by_ch.get(ch) or has_tog):
                continue
            n_bits = max_sample - offset
            if depth:
                n_bits = min(n_bits, depth)
            need_bytes = (offset + n_bits + 7) // 8
            packed = np.zeros(need_bytes, np.uint8)
            n_blocks = (need_bytes + BLOCK_BYTES - 1) // BLOCK_BYTES
            bins = bins_by_ch.get(ch, {})
            for b in range(n_blocks):
                node, blk = divmod(b, NODE_BLOCKS)
                lo = b * BLOCK_BYTES
                hi = min(need_bytes, lo + BLOCK_BYTES)
                t, v = nodes[node] if node < len(nodes) else (0, 0)
                if (t >> blk) & 1:
                    if (v >> blk) & 1:
                        packed[lo:hi] = 0xFF
                elif b in bins:
                    raw = np.frombuffer(z.read(bins[b]), np.uint8)
                    packed[lo:hi] = raw[:hi - lo]
            packed = shift_packed(packed, offset)
            chans[ch] = Channel(ch, packed, n_bits, name)
            lengths.append(n_bits)

    n = min(lengths) if lengths else 0
    rate = freq_khz * 1000.0
    meta = {
        "source": "atkdl",
        "file": path,
        "session_name": top.get("SessionName"),
        "sampling_depth": depth,
        "settings": settings,
        "decoders": decoders,
    }
    if top.get("TriggerDate"):
        meta["trigger_date"] = top["TriggerDate"]
    start_time = None
    if top.get("CollectDate"):
        try:
            start_time = datetime.fromisoformat(top["CollectDate"])
        except ValueError:
            pass
    return Capture(rate, chans, n, trig if 0 <= trig < n else None, meta, start_time)


def write_atkdl(capture: Capture, path: str) -> None:
    """Write a waveform archive. Channel offsets are already applied by Capture."""
    if capture.n_samples <= 0 or not capture.channels:
        raise ValueError("ATKDL export needs a capture with samples")
    if any(not 0 <= index < CHANNEL_COUNT or channel.n != capture.n_samples
           or "\n" in channel.name or "\r" in channel.name
           for index, channel in capture.channels.items()):
        raise ValueError("ATKDL export needs valid DL16 channels, single-line names and equal sample counts")
    rate_khz = capture.samplerate / 1000
    if not rate_khz.is_integer() or rate_khz <= 0:
        raise ValueError("ATKDL export needs a positive sample rate in whole kHz")

    device = capture.meta.get("device") or {}
    name = capture.meta.get("session_name") or device.get("model")
    name = name if name in ("DL16", "DL16 Plus") else "DL16"
    captured_at = capture.start_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    trigger_at = capture.meta.get("trigger_date")
    trigger = capture.trigger_sample
    if trigger is not None and not 0 <= trigger < capture.n_samples:
        raise ValueError("ATKDL export trigger must fall inside the capture")
    top = [f"SessionName={name}", f"SamplingFrequency={int(rate_khz)}",
           f"SamplingDepth={capture.n_samples}", f"CollectDate={captured_at}", "MaxUnitRecord=-1"]
    if isinstance(trigger_at, str) and not any(char in trigger_at for char in "\r\n="):
        top.append(f"TriggerDate={trigger_at}")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        if trigger is not None:
            top.append(f"TriggerSamplingDepth={trigger}")
            trigger_ns = round(trigger * 1_000_000_000 / capture.samplerate)
            archive.writestr("vernier.ini", f"Trigger,ff26a3d9,{trigger_ns},\n")
        archive.writestr("channel.ini", "\n".join(top) + "\n")
        archive.writestr("set.ini", _export_settings(capture, name))
        for index in range(CHANNEL_COUNT):
            channel = capture.channels.get(index)
            if channel is None:
                archive.writestr(f"{index}/channel.ini", f"Channel {index}\n0\n0\n-1\n-1\n0,0\n")
            else:
                _write_channel(archive, index, channel)


def _export_settings(capture: Capture, name: str) -> str:
    saved = capture.meta.get("settings") or {}
    previous = saved.get("settingData") or {}
    mode = capture.meta.get("mode", saved.get("mode", "buffer"))
    duration_ms = capture.duration_s * 1000
    time_index = min(range(len(_GUI_DURATIONS_MS)),
                     key=lambda index: abs(_GUI_DURATIONS_MS[index] - duration_ms))
    trigger_codes = {"rising": 1, "high": 2, "falling": 3, "low": 4, "edge": 5}
    chosen_triggers = {}
    if "triggers" in capture.meta:
        for index, trigger in capture.meta["triggers"].items():
            chosen_triggers[int(index)] = trigger_codes.get(trigger, 0)
    elif isinstance(saved.get("channelsSet"), list):
        for item in saved["channelsSet"]:
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                chosen_triggers[item["id"]] = item.get("triggerType", 0)
    channel_settings = []
    for index in range(CHANNEL_COUNT):
        channel = capture.channels.get(index)
        channel_settings.append({
            "id": index, "enable": channel is not None, "channelHeight": 62,
            "name": channel.name if channel is not None else f"Channel {index}",
            "triggerType": chosen_triggers.get(index, 0),
        })
    settings = {
        "RLE": capture.meta.get("rle", previous.get("RLE", False)),
        "intervalTime": 0, "isBuffer": mode == "buffer", "model": 0,
        "selectHzIndex": SAMPLE_RATES.index(capture.samplerate) if capture.samplerate in SAMPLE_RATES else 0,
        "selectTimeIndex": previous.get("selectTimeIndex", time_index),
        "selectThresholdLevelIndex": previous.get("selectThresholdLevelIndex", 0),
        "setHz": int(capture.samplerate), "setTime": duration_ms,
        "thresholdLevel": capture.meta.get("threshold_v", previous.get("thresholdLevel", 1.6)),
        "triggerPosition": (capture.meta.get("trigger_position_percent")
                            or previous.get("triggerPosition", 1)),
    }
    immediate = capture.meta.get("immediate", saved.get("isInstantly"))
    if immediate is None:
        immediate = not any(channel["triggerType"] for channel in channel_settings)
    channels_json = json.dumps(channel_settings, ensure_ascii=False, separators=(",", ":"))
    channels_json = channels_json.replace("=", r"\u003d")  # Qt splits each setting line on "=".
    return "\n".join([
        f"[{name}]", "isFirst=false", "isOne=true", "isSimpleTrigger=true", "isStop=true",
        "collectType=2" if immediate else "collectType=0",
        "isInstantly=true" if immediate else "isInstantly=false",
        "channelsSet=" + channels_json,
        "settingData=" + json.dumps(settings, separators=(",", ":")),
        "channelsHeight=" + json.dumps([62] * CHANNEL_COUNT, separators=(",", ":")),
        "",
    ])


def _write_channel(archive: zipfile.ZipFile, index: int, channel: Channel) -> None:
    nbytes = (channel.n + 7) // 8
    blocks = (nbytes + BLOCK_BYTES - 1) // BLOCK_BYTES
    header = [channel.name, "0", str(channel.n), "-1", "-1"]
    for node, first in enumerate(range(0, blocks, NODE_BLOCKS)):
        toggles = values = 0
        for block in range(min(NODE_BLOCKS, blocks - first)):
            lo = (first + block) * BLOCK_BYTES
            packed = channel.packed[lo:min(lo + BLOCK_BYTES, nbytes)]
            if not packed.any():
                toggles |= 1 << block
            elif (packed == 0xFF).all():
                toggles |= 1 << block
                values |= 1 << block
            else:
                raw = bytearray(BLOCK_BYTES)
                raw[:len(packed)] = packed.tobytes()
                if lo + len(packed) == nbytes and channel.n % 8:
                    raw[len(packed) - 1] &= (1 << (channel.n % 8)) - 1
                archive.writestr(f"{index}/{node}-{block}.bin", raw)
        header.append(f"{toggles},{values}")
    archive.writestr(f"{index}/channel.ini", "\n".join(header) + "\n")


def _read_settings(z: zipfile.ZipFile, names) -> dict:
    if "set.ini" not in names:
        return {}
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        cp.read_string(z.read("set.ini").decode("utf-8", "replace"))
    except configparser.Error:
        return {}
    out = {}
    for sec in cp.sections():
        for k, v in cp.items(sec):
            try:
                out[k] = json.loads(v)
            except ValueError:
                out[k] = v
        out["device_section"] = sec
    s = out.get("settingData") or {}
    if isinstance(s, dict):
        out["mode"] = "buffer" if s.get("isBuffer") else "stream"
    return out


def _decoder_stack(d: dict) -> dict:
    """Convert the GUI's decoder JSON into the decode.py stack format:
    {"stack":[{"id","channels":{id:ch},"options":{id:value}}...]} lowest first."""
    layers = []
    for key, v in d.items():
        if key == "main" or not isinstance(v, dict) or "main" not in v:
            continue
        chans = {}
        for c in list(v.get("channels") or []) + list(v.get("opt_channels") or []):
            val = str(c.get("value", "-"))
            if val not in ("-", "", "None") and val.lstrip("-").isdigit() and int(val) >= 0:
                chans[c["id"]] = int(val)
        opts = {o["id"]: o.get("value") for o in (v.get("options") or [])}
        layers.append({"id": v["main"]["id"], "name": v["main"].get("name", key),
                       "channels": chans, "options": opts})
    # the decoder with logic channels is the bottom of the stack
    layers.sort(key=lambda L: 0 if L["channels"] else 1)
    return {"stack": layers}
