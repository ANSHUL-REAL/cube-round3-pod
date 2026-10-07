"""Which ids may be used as file or folder names.

Workflow ids, record ids, organisation ids and unit ids all end up in paths. They must be plain names: letters, digits,
'.', '_' and '-', starting with a letter or digit, with no '..', no separators and no drive letters.
"""
from __future__ import annotations

import re

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_safe_id(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 200 and bool(SAFE_ID.match(value)) and ".." not in value
