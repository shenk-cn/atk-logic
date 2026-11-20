"""Command line interface: `atk-logic <command> ...` / `python -m atk_logic ...`.

Every command prints one JSON document on stdout (use --pretty for indentation).
Errors are printed as {"error": ..., "type": ..., "hint": ...} with exit code != 0.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Dict, List, Optional

from . import __version__
from . import protocol as P

EXIT_USAGE, EXIT_NOTFOUND, EXIT_ACCESS, EXIT_STATE, EXIT_CAPTURE, EXIT_OTHER = 2, 3, 4, 5, 6, 1

_SI = {"": 1, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "g": 1e9, "G": 1e9}


def parse_freq(s: str) -> float:
    """'10M', '2.5MHz', '1k', '250000' -> Hz (m/M both mean mega here)."""
    m = re.fullmatch(r"\s*([0-9.]+(?:e[-+]?\d+)?)\s*([kKmMgG]?)(?:hz|Hz|HZ)?\s*", str(s))
    if not m:
        raise ValueError("bad frequency %r (examples: 10M, 2.5MHz, 1k)" % s)
    return float(m.group(1)) * _SI[m.group(2)]


def parse_time(s: str) -> float:
    """'10ms', '2s', '100us', '500ns', '1.5' (seconds) -> seconds."""
    m = re.fullmatch(r"\s*([0-9.]+(?:e[-+]?\d+)?)\s*(s|ms|us|µs|ns|min|m)?\s*", str(s))
    if not m:
        raise ValueError("bad time %r (examples: 10ms, 2s, 100us)" % s)
    mul = {None: 1, "s": 1, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9, "min": 60, "m": 60}[m.group(2)]
    return float(m.group(1)) * mul


def parse_count(s: str) -> int:
    m = re.fullmatch(r"\s*([0-9.]+)\s*([kKmMgG]?)\s*(?:sa|Sa|samples)?\s*", str(s))
    if not m:
        raise ValueError("bad sample count %r (examples: 100000, 10M)" % s)
    return int(float(m.group(1)) * _SI[m.group(2)])


def parse_channels(s: str) -> List[int]:
    out = []
    for part in str(s).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def parse_kv(items: Optional[List[str]], what: str) -> Dict[str, str]:
    d = {}
    for it in items or []:
        for part in it.split(","):
            if not part.strip():
                continue
            if "=" not in part and ":" not in part:
                raise ValueError("%s must look like key=value, got %r" % (what, part))
            k, v = re.split(r"[=:]", part, maxsplit=1)
            d[k.strip()] = v.strip()
    return d


def parse_triggers(items: Optional[List[str]]) -> Dict[int, str]:
    return {int(k): v for k, v in parse_kv(items, "--trigger").items()}


def _emit(obj, args) -> None:
    txt = json.dumps(obj, indent=2 if getattr(args, "pretty", False) else None,
                     ensure_ascii=False, default=_json_default)
    sys.stdout.write(txt + "\n")


def _json_default(o):
    try:
        import numpy as np
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:  # pragma: no cover
        pass
    return str(o)


# ------------------------------------------------------------------ helpers
def _load_file(path: str):
    from . import load
    return load(path)


def _window(cap, args):
    start = int(parse_time(args.start) * cap.samplerate) if getattr(args, "start", None) else 0
    end = int(parse_time(args.end) * cap.samplerate) if getattr(args, "end", None) else None
    if start or end is not None:
        cap = cap.slice(start, end)
    chs = getattr(args, "channels", None)
    if chs:
        keep = parse_channels(chs)
        cap.channels = {k: v for k, v in cap.channels.items() if k in keep}
    return cap


def _exports(cap, outs: Optional[List[str]]) -> List[str]:
    files = []
    try:
        for output in outs or []:
            target = Path(output).absolute()
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix=target.suffix,
                                             prefix=".atk-", delete=False) as tmp:
                staged = Path(tmp.name)
            files.append((staged, target))
            cap.save(str(staged))
        for staged, target in files:
            staged.replace(target)
        return [str(target) for _, target in files]
    finally:
        for staged, _ in files:
            staged.unlink(missing_ok=True)


def _stack_from_args(args, cap) -> List[dict]:
    """Build a decoder stack from --decoder/--map/--opt, or take it from the .atkdl file."""
    from .decode import load_decoder_class
    decs = []
    for d in args.decoder or []:
        decs.extend(x.strip() for x in d.split(",") if x.strip())
    if not decs:
        saved = (cap.meta or {}).get("decoders") or []
        saved = [s for s in saved if "stack" in s]
        if not saved:
            raise ValueError("no --decoder given and the input has no saved decoder configuration")
        idx = getattr(args, "saved", 0) or 0
        if idx >= len(saved):
            raise ValueError("--saved %d out of range (%d saved decoder stacks)" % (idx, len(saved)))
        return saved[idx]["stack"]
    mapping = parse_kv(args.map, "--map")
    opts = parse_kv(args.opt, "--opt")
    stack = [{"id": d, "channels": {}, "options": {}} for d in decs]
    classes = [load_decoder_class(d, args.decoders_dir) for d in decs]
    for k, v in mapping.items():
        stack[0]["channels"][k] = int(v)
    for k, v in opts.items():
        if "." in k:
            dec, opt = k.split(".", 1)
            hits = [i for i, c in enumerate(classes) if c.id == dec]
            if not hits:
                raise ValueError("--opt %s: decoder %s is not in the stack" % (k, dec))
            stack[hits[0]]["options"][opt] = v
            continue
        hits = [i for i, c in enumerate(classes) if any(o["id"] == k for o in getattr(c, "options", ()))]
        if not hits:
            raise ValueError("--opt %s: no decoder in the stack has this option" % k)
        if len(hits) > 1:
            raise ValueError("--opt %s is ambiguous; write it as <decoder>.%s" % (k, k))
        stack[hits[0]]["options"][k] = v
    return stack


def _decode_output(res: dict, args) -> dict:
    max_texts = args.max_texts
    out = {k: v for k, v in res.items() if k != "annotations"}
    rows = {}
    for k, v in res["rows"].items():
        if args.rows and not any(r in k for r in args.rows.split(",")):
            continue
        texts = v["texts"]
        rows[k] = {"count": v["count"], "texts": texts[:max_texts]}
        if len(texts) > max_texts:
            rows[k]["truncated"] = True
    if args.ann:
        rows = {}
        keys = [k.strip() for k in args.ann.split(",") if k.strip()]
        for a in res["annotations"]:
            if any(k == a["ann"] or (k.endswith("*") and a["ann"].startswith(k[:-1])) for k in keys):
                r = rows.setdefault("%s.%s" % (a["decoder"], a["ann"]), {"count": 0, "texts": []})
                r["count"] += 1
                if len(r["texts"]) < max_texts:
                    r["texts"].append(a["text"])
                else:
                    r["truncated"] = True
    out["rows"] = rows
    if args.annotations:
        anns = res["annotations"]
        if args.rows:
            keys = args.rows.split(",")
            anns = [a for a in anns if any(r in "%s.%s" % (a["decoder"], a["row"] or a["ann"]) for r in keys)]
        out["annotations"] = [
            {"t0": round(a["t0"], 9), "t1": round(a["t1"], 9), "decoder": a["decoder"],
             "row": a["row"], "ann": a["ann"], "text": a["text"]} for a in anns[:args.max_annotations]]
        if len(anns) > args.max_annotations:
            out["annotations_truncated"] = True
    return out


# ------------------------------------------------------------------ commands
def cmd_list(args):
    from .device import list_devices
    devs = list_devices()
    _emit({"devices": devs, "count": len(devs)}, args)


def _open(args):
    from .device import AtkLogic
    log = (lambda m: sys.stderr.write(m + "\n")) if args.verbose else None
    return AtkLogic.open(index=args.index, log=log)


def cmd_info(args):
    with _open(args) as la:
        _emit({"device": la.info.as_dict(), "api_version": __version__}, args)


_CAPTURE_DEFAULTS = {
    "channels": None, "rate": None, "duration": None, "samples": None,
    "threshold": None, "mode": "buffer", "rle": False, "trigger": None,
    "trigger_pos": 1.0, "timeout": None, "acquisition": None,
    "repeat_count": None, "interval": None, "stop_mode": "abort",
}


def _apply_capture_preset(args):
    preset = {}
    if args.preset:
        preset = json.loads(Path(args.preset).read_text(encoding="utf-8-sig"))
        if not isinstance(preset, dict):
            raise ValueError("the preset must be a JSON object")
        unknown = preset.keys() - _CAPTURE_DEFAULTS.keys()
        if unknown:
            raise ValueError("unknown preset fields: " + ", ".join(sorted(unknown)))
    if args.duration is not None:
        preset.pop("samples", None)
    if args.samples is not None:
        preset.pop("duration", None)
    for key, default in _CAPTURE_DEFAULTS.items():
        if getattr(args, key) is None:
            setattr(args, key, preset.get(key, default))


def _capture_kwargs(args):
    if args.channels is None or args.rate is None:
        raise ValueError("capture needs -c/--channels and -r/--rate")
    if (args.duration is None) == (args.samples is None):
        raise ValueError("give exactly one of --duration and --samples")
    channels = parse_channels(args.channels)
    if not channels or any(not 0 <= ch < P.CHANNEL_COUNT for ch in channels):
        raise ValueError("select at least one channel from 0..15")
    rate = parse_freq(args.rate)
    P.rate_index(rate)
    if args.mode not in ("buffer", "stream") or not isinstance(args.rle, bool):
        raise ValueError("mode must be buffer or stream, and rle must be true or false")
    if rate not in P.allowed_rates(args.mode, len(set(channels)), plus=True):
        raise ValueError("requested sample rate is unavailable with %d channels in %s mode" %
                         (len(set(channels)), args.mode))
    if args.mode == "stream" and args.rle:
        raise ValueError("DL16 hardware RLE requires Buffer mode")
    if args.acquisition == "rolling" or args.stop_mode == "upload":
        raise ValueError("native rolling and Buffer stop-and-upload are DL32 features; DL16/Plus do not support them")
    if args.acquisition not in (None, "normal", "immediate", "repeat"):
        raise ValueError("acquisition must be normal, immediate or repeat")
    if args.stop_mode not in ("abort", "save-received"):
        raise ValueError("stop_mode must be abort or save-received")
    if args.stop_mode == "save-received" and args.mode != "stream":
        raise ValueError("--stop-mode save-received requires Stream mode")
    if args.acquisition != "repeat" and (args.interval is not None or args.repeat_count is not None):
        raise ValueError("--interval and --repeat-count require repeat acquisition")
    if args.repeat_count is not None and (type(args.repeat_count) is not int or args.repeat_count < 1):
        raise ValueError("repeat_count must be a positive integer")
    if args.trigger is not None and (not isinstance(args.trigger, list) or
                                     any(not isinstance(trigger, str) for trigger in args.trigger)):
        raise ValueError('trigger must be a list of strings, e.g. ["1=rising"]')
    triggers = {} if args.acquisition == "immediate" else parse_triggers(args.trigger)
    for ch, trigger in triggers.items():
        if ch not in channels:
            raise ValueError("trigger channel %d is not an enabled channel" % ch)
        P.normalize_trigger(trigger)
    duration = parse_time(args.duration) if args.duration is not None else None
    samples = parse_count(args.samples) if args.samples is not None else None
    timeout = parse_time(args.timeout) if args.timeout is not None else None
    trigger_position = float(args.trigger_pos)
    if not 1 <= trigger_position <= 90:
        raise ValueError("trigger_pos must be 1..90 percent")
    if args.mode == "stream" and trigger_position != 1:
        raise ValueError("--trigger-pos is adjustable in Buffer mode only")
    if (samples is not None and samples < 1000) or (duration is not None and duration * rate < 1000):
        raise ValueError("capture at least 1000 samples per channel")
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be positive")
    for path in args.out or []:
        target = Path(path)
        if target.suffix.lower() not in (".atkdl", ".csv", ".vcd", ".atkjson", ".json"):
            raise ValueError("unsupported output extension: %s" % target)
        if not target.parent.is_dir():
            raise ValueError("output directory does not exist: %s" % target.parent)
    from .device import parse_threshold
    P.encode_threshold(parse_threshold(args.threshold))
    return dict(
        channels=channels, samplerate=rate, samples=samples, duration=duration,
        threshold=args.threshold,
        mode=args.mode, rle=args.rle, trigger=triggers, trigger_position=trigger_position,
        timeout=timeout,
        immediate={"immediate": True, "normal": False, "repeat": False}.get(args.acquisition),
    )


class _StopFile:
    def __init__(self, path):
        self.path = Path(path) if path else None
        if self.is_set():
            raise ValueError("stop file already exists: %s" % self.path)

    def is_set(self):
        return self.path is not None and self.path.exists()

    def wait(self, seconds):
        until = time.monotonic() + seconds
        while not self.is_set():
            left = until - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 0.1))


def _capture_pwm(args, la):
    pwm_res = []
    for spec in args.pwm or []:
        parts = spec.split(":")
        if len(parts) not in (2, 3):
            raise ValueError("--pwm must look like INDEX:FREQ[:DUTY], e.g. 0:1M:50")
        pwm_res.append(la.pwm(int(parts[0]), parse_freq(parts[1]),
                              float(parts[2]) if len(parts) == 3 else 50))
    return pwm_res


def _capture_once(args, la, kw, stop_file, cycle):
    def progress(stage, percent):
        if args.events and stage == "armed":
            print(json.dumps({"event": "armed", "cycle": cycle}), file=sys.stderr, flush=True)
        elif args.verbose and not args.events:
            print("%s %d%%" % (stage, percent), file=sys.stderr, flush=True)

    return la.capture(progress=progress, stop_event=stop_file, **kw)


def _check_capture(cap, args):
    from .device import CaptureError
    if cap.meta["complete"]:
        return
    if cap.meta["end_reason"] == "requested_stop":
        if args.stop_mode == "abort":
            raise KeyboardInterrupt
        if cap.meta["partial_exportable"]:
            return
    error = CaptureError("capture ended before valid data could be published: %s" % cap.meta["end_reason"])
    error.capture = cap.summary(include_stats=False)
    raise error


def _capture_result(cap, args, outputs, pwm_res):
    _check_capture(cap, args)
    out = {
        "capture": cap.summary(include_stats=args.stats),
        "collected_at": cap.start_time.isoformat(timespec="milliseconds"),
        "saved": _exports(cap, outputs),
    }
    if pwm_res:
        out["pwm"] = pwm_res
    if args.edges:
        out["edges_text"] = cap.edges_text(max_edges=args.max_edges)
    if args.decoder:
        from .decode import decode
        res = decode(cap, _stack_from_args(args, cap), args.decoders_dir, max_annotations=args.limit)
        out["decode"] = _decode_output(res, args)
    return out


def cmd_capture(args):
    _apply_capture_preset(args)
    kw = _capture_kwargs(args)
    stop_file = _StopFile(args.stop_file)
    if args.acquisition != "repeat":
        with _open(args) as la:
            pwm_res = _capture_pwm(args, la)
            cap = _capture_once(args, la, kw, stop_file, cycle=1)
        _emit(_capture_result(cap, args, args.out, pwm_res), args)
        return

    interval = parse_time(args.interval) if args.interval is not None else 0
    if args.pretty:
        raise ValueError("repeat acquisition emits one compact JSON line per cycle")
    with _open(args) as la:
        pwm_res = _capture_pwm(args, la)
        cycle = 1
        while not stop_file.is_set():
            outputs = [str(p.with_name("%s.%04d%s" % (p.stem, cycle, p.suffix)))
                       for p in map(Path, args.out or [])]
            for output in outputs:
                if Path(output).exists():
                    raise FileExistsError("repeat output already exists: %s" % output)
            cap = _capture_once(args, la, kw, stop_file, cycle)
            next_start = time.monotonic() + interval
            out = _capture_result(cap, args, outputs, pwm_res)
            out["cycle"] = cycle
            _emit(out, args)
            sys.stdout.flush()
            if cycle == args.repeat_count or cap.meta["end_reason"] == "requested_stop":
                break
            cycle += 1
            del cap, out
            stop_file.wait(next_start - time.monotonic())


def cmd_open(args):
    cap = _window(_load_file(args.file), args)
    out = {"capture": cap.summary(), "saved": _exports(cap, args.out)}
    if args.edges:
        out["edges_text"] = cap.edges_text(max_edges=args.max_edges)
    _emit(out, args)


def cmd_edges(args):
    cap = _load_file(args.file)
    start = int(parse_time(args.start) * cap.samplerate) if args.start else 0
    end = int(parse_time(args.end) * cap.samplerate) if args.end else None
    chans = parse_channels(args.channels) if args.channels else None
    sys.stdout.write(cap.edges_text(channels=chans, max_edges=args.max_edges, start=start, end=end) + "\n")


def cmd_decode(args):
    from .decode import decode
    if args.file:
        cap = _window(_load_file(args.file), args)
    else:
        if not args.capture:
            raise ValueError("give an input FILE or --capture with capture options")
        _apply_capture_preset(args)
        kw = _capture_kwargs(args)
        if args.acquisition == "repeat":
            raise ValueError("use capture to run repeat acquisition")
        stop_file = _StopFile(args.stop_file)
        with _open(args) as la:
            _capture_pwm(args, la)
            cap = _capture_once(args, la, kw, stop_file, cycle=1)
        _check_capture(cap, args)
        if args.out:
            _exports(cap, args.out)
    res = decode(cap, _stack_from_args(args, cap), args.decoders_dir, max_annotations=args.limit)
    _emit(_decode_output(res, args), args)


def cmd_decoders(args):
    from .decode import describe, list_decoders, load_decoder_class
    if args.id:
        _emit(describe(load_decoder_class(args.id, args.decoders_dir)), args)
    else:
        lst = list_decoders(args.decoders_dir)
        if not args.full:
            lst = [{"id": d["id"], "name": d.get("name"), "inputs": d.get("inputs"),
                    "outputs": d.get("outputs"), "error": d.get("error")} if "error" in d else
                   {"id": d["id"], "name": d["name"], "inputs": d["inputs"], "outputs": d["outputs"]}
                   for d in lst]
        _emit({"decoders": lst, "count": len(lst)}, args)


def cmd_limits(args):
    plus = args.plus
    n = len(parse_channels(args.channels))
    _emit({
        "channels": n, "plus": plus,
        "buffer_rates_hz": P.allowed_rates("buffer", n, plus),
        "stream_rates_hz": P.allowed_rates("stream", n, plus),
        "buffer_max_samples_per_channel": (P.BUFFER_MAX_BITS_PLUS if plus else P.BUFFER_MAX_BITS_STD) // max(1, n),
        "buffer_max_samples_per_channel_rle": (P.BUFFER_MAX_BITS_PLUS if plus else P.BUFFER_MAX_BITS_STD)
        * P.RLE_DEPTH_FACTOR // max(1, n),
        "threshold_range_v": [P.THRESHOLD_MIN_V, P.THRESHOLD_MAX_V],
        "threshold_presets_v": {"5.0V CMOS": 2.5, "3.3V CMOS": 1.6, "3.0V CMOS": 1.5, "2.5V CMOS": 1.2,
                                "1.8V CMOS": 0.9, "1.5V CMOS": 0.7, "1.2V CMOS": 0.6},
        "pwm_range_hz": [P.PWM_MIN_HZ, P.PWM_MAX_HZ],
        "trigger_types": list(P.TRIGGER_TYPES),
    }, args)


# ------------------------------------------------------------------ parser
def _add_capture_opts(p):
    p.add_argument("--preset", help="JSON capture preset; explicit flags override it")
    p.add_argument("-c", "--channels", help="channels, e.g. 0,1,2 or 0-3")
    p.add_argument("-r", "--rate", help="sample rate, e.g. 10M, 100M, 1G")
    length = p.add_mutually_exclusive_group()
    length.add_argument("-d", "--duration", help="capture length as time, e.g. 10ms, 1s")
    length.add_argument("-n", "--samples", help="capture length in samples per channel, e.g. 1M")
    p.add_argument("--threshold", default=None,
                   help="switching threshold in V (-5..5) or preset 3.3V/1.8V/5V... (default 1.6 V = 3.3V CMOS)")
    p.add_argument("--mode", choices=["buffer", "stream"], help="hardware capture mode (default: buffer)")
    rle = p.add_mutually_exclusive_group()
    rle.add_argument("--rle", action="store_true", default=None, help="hardware run-length compression")
    rle.add_argument("--no-rle", dest="rle", action="store_false", default=None, help="turn off RLE")
    trigger = p.add_mutually_exclusive_group()
    trigger.add_argument("-t", "--trigger", action="append",
                         help="per-channel trigger, e.g. 0=falling,3=high (types: rising falling high low edge)")
    trigger.add_argument("--no-trigger", dest="trigger", action="store_const", const=[],
                         help="clear preset trigger conditions")
    p.add_argument("--trigger-pos", type=float, help="pre-trigger buffer in %% (1..90; default: 1)")
    p.add_argument("--timeout", help="give up after this time, e.g. 10s (default: wait for trigger forever)")
    p.add_argument("--acquisition", choices=["normal", "immediate", "repeat", "rolling"],
                   help="capture policy (native rolling is DL32-only)")
    p.add_argument("--repeat-count", type=int, help="repeat this many times (otherwise until stopped)")
    p.add_argument("--interval", help="wait after a repeat cycle finishes, e.g. 500ms")
    p.add_argument("--events", action="store_true", help="write a JSON armed event to stderr")
    p.add_argument("--stop-file", help="stop when this file appears; it must be absent at startup")
    p.add_argument("--stop-mode", choices=["abort", "save-received", "upload"],
                   help="discard on stop or keep received Stream samples (upload is DL32-only)")
    p.add_argument("--pwm", action="append", help="also drive PWM during capture: INDEX:FREQ[:DUTY]")


def _add_decode_opts(p):
    p.add_argument("-D", "--decoder", action="append",
                   help="decoder id; repeat or comma-separate to stack (lowest first), e.g. i2c,eeprom24xx")
    p.add_argument("-m", "--map", action="append", help="channel mapping, e.g. rx=0 or scl=0,sda=1")
    p.add_argument("-O", "--opt", action="append",
                   help="decoder option, e.g. baudrate=115200 or uart.parity=even")
    p.add_argument("--saved", type=int, default=0,
                   help="when no --decoder: index of the decoder stack saved in the .atkdl")
    p.add_argument("--rows", help="only rows containing these names (comma separated), e.g. tx-data,rx-data")
    p.add_argument("--ann", help="only these annotation classes (exact, or prefix with *), e.g. tx-data or "
                                 "address,data-read (see `decoders ID`); rows are then keyed by class")
    p.add_argument("--max-texts", type=int, default=200, help="texts per row in the output")
    p.add_argument("--limit", type=int, default=100000,
                   help="stop decoding after this many annotations (default 100000)")
    p.add_argument("--annotations", action="store_true", help="include timed annotation list")
    p.add_argument("--max-annotations", type=int, default=2000)


def _add_window_opts(p):
    p.add_argument("--start", help="window start time, e.g. 1ms")
    p.add_argument("--end", help="window end time")
    p.add_argument("--channels", help="only these channels, e.g. 0,1")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="atk-logic-cli", description="ATK-Logic (ALIENTEK DL16) logic analyzer CLI")
    ap.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pretty", action="store_true", help="indent JSON output")
    common.add_argument("-i", "--device", dest="index", type=int, default=0, help="device index (see `list`)")
    common.add_argument("-v", "--verbose", action="store_true", help="progress/log on stderr")
    common.add_argument("--decoders-dir", default=None, help="sigrok decoders directory (default: runtime/decoders)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", parents=[common], help="list connected analyzers")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("info", parents=[common], help="connect and show device info")
    sp.set_defaults(fn=cmd_info)

    sp = sub.add_parser("limits", parents=[common], help="allowed rates/depths for a channel set (offline)")
    sp.add_argument("-c", "--channels", default="0-15")
    sp.add_argument("--plus", action="store_true", help="Plus model")
    sp.set_defaults(fn=cmd_limits)

    sp = sub.add_parser("capture", parents=[common], help="acquire and print a JSON summary")
    _add_capture_opts(sp)
    sp.add_argument("-o", "--out", action="append", help="save to .atkdl/.vcd/.csv/.atkjson/.json (repeatable)")
    sp.add_argument("--stats", action="store_true", help="also compute edge and timing statistics")
    sp.add_argument("--edges", action="store_true", help="include compact edge/run-length text")
    sp.add_argument("--max-edges", type=int, default=200)
    _add_decode_opts(sp)
    sp.set_defaults(fn=cmd_capture)

    sp = sub.add_parser("pwm", parents=[common], help="start a PWM output")
    sp.add_argument("index_pwm", metavar="OUTPUT", type=int, choices=[0, 1], help="PWM output 0 or 1")
    sp.add_argument("-f", "--freq", required=True, help="frequency, 1 Hz..20 MHz, e.g. 1k, 2.5M")
    sp.add_argument("--duty", type=float, default=50, help="duty cycle %% (1..99)")
    sp.add_argument("--hold", help="keep the program running this long then stop PWM (e.g. 10s); "
                                   "default: leave PWM running and exit")
    sp.set_defaults(fn=_run_pwm)

    sp = sub.add_parser("pwm-stop", parents=[common], help="stop PWM output(s)")
    sp.add_argument("index_pwm", metavar="OUTPUT", nargs="?", default="all", choices=["0", "1", "all"],
                    help="0, 1 or all (default)")
    sp.set_defaults(fn=_run_pwm)

    sp = sub.add_parser("open", parents=[common], help="load a .atkdl/.atkjson file, summarize/export")
    sp.add_argument("file")
    _add_window_opts(sp)
    sp.add_argument("-o", "--out", action="append", help="export to .atkdl/.vcd/.csv/.atkjson/.json")
    sp.add_argument("--edges", action="store_true")
    sp.add_argument("--max-edges", type=int, default=200)
    sp.set_defaults(fn=cmd_open)

    sp = sub.add_parser("edges", parents=[common], help="print compact run-length text of a file")
    sp.add_argument("file")
    _add_window_opts(sp)
    sp.add_argument("--max-edges", type=int, default=200)
    sp.set_defaults(fn=cmd_edges)

    sp = sub.add_parser("decode", parents=[common], help="run sigrok protocol decoders")
    sp.add_argument("file", nargs="?", help=".atkdl/.atkjson input (omit with --capture)")
    sp.add_argument("--capture", action="store_true", help="capture from the device first")
    _add_capture_opts(sp)
    sp.add_argument("-o", "--out", action="append", help="with --capture: also save the capture")
    sp.add_argument("--start", help="window start time")
    sp.add_argument("--end", help="window end time")
    _add_decode_opts(sp)
    sp.set_defaults(fn=cmd_decode)

    sp = sub.add_parser("decoders", parents=[common], help="list available protocol decoders")
    sp.add_argument("id", nargs="?", help="show details (channels/options) of one decoder")
    sp.add_argument("--full", action="store_true")
    sp.set_defaults(fn=cmd_decoders)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    from .device import AtkError, CaptureError, DeviceAccessError, DeviceNotFound, DeviceStateError
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        rc = args.fn(args)
        return rc or 0
    except DeviceNotFound as e:
        return _fail(e, EXIT_NOTFOUND, args)
    except DeviceAccessError as e:
        return _fail(e, EXIT_ACCESS, args)
    except DeviceStateError as e:
        return _fail(e, EXIT_STATE, args)
    except CaptureError as e:
        return _fail(e, EXIT_CAPTURE, args)
    except AtkError as e:
        return _fail(e, EXIT_OTHER, args)
    except (ValueError, KeyError, FileNotFoundError, ImportError) as e:
        return _fail(e, EXIT_USAGE, args)
    except OSError as e:
        return _fail(e, EXIT_OTHER, args)
    except KeyboardInterrupt:
        return _fail(RuntimeError("interrupted"), 130, args)


def _run_pwm(args) -> int:
    from .device import AtkLogic
    log = (lambda m: sys.stderr.write(m + "\n")) if args.verbose else None
    la = AtkLogic.open(index=args.index, log=log)
    if args.cmd == "pwm":
        try:
            res = la.pwm(int(args.index_pwm), parse_freq(args.freq), args.duty)
        except Exception:
            la.close()
            raise
        if args.hold:
            try:
                time.sleep(parse_time(args.hold))
            finally:
                la.close()
            res["state"] = "stopped after --hold"
        else:
            la.close(keep_pwm=True)
            res["state"] = "running (USB released, output left on; stop with `pwm-stop`)"
        _emit({"pwm": res}, args)
    else:
        try:
            if str(args.index_pwm) == "all":
                la.close_all_pwm()
                la.set_reset_state(0)
            else:
                la.pwm_stop(int(args.index_pwm))
        finally:
            la.close()
        _emit({"pwm_stopped": str(args.index_pwm)}, args)
    return 0


def _fail(e: Exception, code: int, args) -> int:
    hint = getattr(e, "hint", "")
    if not hint and isinstance(e, ImportError):
        hint = "pip install libusb1 numpy"
    obj = {"error": str(e), "type": type(e).__name__}
    if hint:
        obj["hint"] = hint
    if getattr(e, "capture", None) is not None:
        obj["capture"] = e.capture
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2 if getattr(args, "pretty", False) else None) + "\n")
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
