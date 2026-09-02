"""Encode selected Python values for safe transport through JSON."""

import json


_TYPE_KEY = "__grades_python_type__"
_ITEMS_KEY = "items"


def encode_value(value):
    """Return a JSON-compatible value while preserving tuples and mapping keys."""
    if isinstance(value, tuple):
        return {
            _TYPE_KEY: "tuple",
            _ITEMS_KEY: [encode_value(item) for item in value],
        }
    if isinstance(value, list):
        return [encode_value(item) for item in value]
    if isinstance(value, dict):
        if all(isinstance(key, str) for key in value) and _TYPE_KEY not in value:
            return {key: encode_value(item) for key, item in value.items()}

        items = [
            [encode_value(key), encode_value(item)]
            for key, item in value.items()
        ]
        items.sort(key=lambda pair: json.dumps(
            pair[0], sort_keys=True, separators=(",", ":")
        ))
        return {_TYPE_KEY: "dict", _ITEMS_KEY: items}
    return value


def decode_value(value):
    """Restore values produced by encode_value without executing arbitrary code."""
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    if not isinstance(value, dict):
        return value

    value_type = value.get(_TYPE_KEY)
    if value_type in {"tuple", "dict"} and set(value) == {_TYPE_KEY, _ITEMS_KEY}:
        items = value[_ITEMS_KEY]
        if not isinstance(items, list):
            raise ValueError(f"encoded {value_type} items must be a list")
        if value_type == "tuple":
            return tuple(decode_value(item) for item in items)

        decoded = {}
        for pair in items:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("encoded dict items must be key-value pairs")
            key = decode_value(pair[0])
            try:
                if key in decoded:
                    raise ValueError("encoded dict contains duplicate keys")
                decoded[key] = decode_value(pair[1])
            except TypeError as error:
                raise ValueError("encoded dict key must be hashable") from error
        return decoded

    return {key: decode_value(item) for key, item in value.items()}
