"""Validate bounded ordered traces across trace providers without trusting project output."""
from __future__ import annotations

import math
from .localization import source_path


def validate_timeline(data):
    timeline = data.get('timeline')
    if (not isinstance(timeline, list) or len(timeline) > 512
            or type(data.get('timeline_truncated')) is not bool):
        raise ValueError('invalid ordered trace envelope')
    for event in timeline:
        if not isinstance(event, dict) or set(event) != {'path', 'function', 'line', 'event', 'values'}:
            raise ValueError('invalid ordered trace event')
        source_path(event['path'])
        if (not isinstance(event['function'], str) or not 1 <= len(event['function']) <= 200
                or not event['function'].isprintable() or type(event['line']) is not int or event['line'] < 1
                or not isinstance(event['event'], str) or event['event'] not in {'call', 'line', 'return', 'exception'}):
            raise ValueError('invalid ordered trace location')
        values = event['values']
        if not isinstance(values, dict) or len(values) > 8:
            raise ValueError('trace values must be a bounded numeric map')
        for name, value in values.items():
            if not isinstance(name, str) or not 1 <= len(name) <= 80 or not name.isprintable():
                raise ValueError('invalid trace value name')
            if value is not None and type(value) not in (int, float, bool):
                raise ValueError('only primitive numeric trace values are accepted')
            if type(value) in (int, float) and (abs(value) > 2 ** 53 or not math.isfinite(value)):
                raise ValueError('trace number exceeds the numeric bound')
    return data
