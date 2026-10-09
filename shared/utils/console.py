"""Command-line output that survives a Windows console.

A Windows console defaults to a legacy code page (cp1252), and model text is not ASCII: Prep's checker crashed printing an
arrow (U+2191) that Gemini wrote about a "this way up" mark. Every command-line entry point calls `utf8_console()` first.
"""
from __future__ import annotations

import sys


def utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)  # absent when output is captured by a test or redirected oddly
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
