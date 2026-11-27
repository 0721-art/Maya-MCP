import json
import math

FRAME_LIMIT = 8 * 1024 * 1024


def encode(message):
    data = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    if len(data) > FRAME_LIMIT:
        raise ValueError("Bridge frame exceeds 8 MiB")
    return data


def loads_strict(text):
    message = json.loads(text, parse_constant=_invalid_number, parse_float=_parse_float)
    if not isinstance(message, dict):
        raise ValueError("Bridge frame must contain a JSON object")
    return message


def read_frame(stream):
    line = stream.readline(FRAME_LIMIT + 1)
    if not line or not line.endswith(b"\n") or len(line) > FRAME_LIMIT:
        raise ValueError("Missing, incomplete or oversized bridge frame")
    return loads_strict(line.decode("utf-8"))


def _invalid_number(value):
    raise ValueError(f"Invalid JSON number: {value}")


def _parse_float(value):
    number = float(value)
    if not math.isfinite(number):
        _invalid_number(value)
    return number
