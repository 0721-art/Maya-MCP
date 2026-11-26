import json

FRAME_LIMIT = 8 * 1024 * 1024


def encode(message):
    data = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    if len(data) > FRAME_LIMIT:
        raise ValueError("Bridge frame exceeds 8 MiB")
    return data


def read_frame(stream):
    line = stream.readline(FRAME_LIMIT + 1)
    if not line or not line.endswith(b"\n") or len(line) > FRAME_LIMIT:
        raise ValueError("Missing, incomplete or oversized bridge frame")
    message = json.loads(line.decode("utf-8"), parse_constant=_invalid_number)
    if not isinstance(message, dict):
        raise ValueError("Bridge frame must contain a JSON object")
    return message


def _invalid_number(value):
    raise ValueError(f"Invalid JSON number: {value}")
