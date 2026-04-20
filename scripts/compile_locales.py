"""Compile every .po under src/bilibili_vision/locales/ into a sibling .mo file.

Usage:
    python scripts/compile_locales.py

Requires only the standard library — uses the msgfmt routine shipped with
CPython's Tools/i18n if available, else a minimal inline implementation.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOCALES = REPO / "src" / "bilibili_vision" / "locales"


def _parse_po(po_path: Path) -> dict[tuple[str, str], str]:
    """Parse a minimal gettext .po file into {(ctx, msgid): msgstr}."""
    messages: dict[tuple[str, str], str] = {}
    ctx = ""
    msgid = ""
    msgstr = ""
    state = None
    for raw in po_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            if state == "msgstr" and msgid is not None:
                messages[(ctx, msgid)] = msgstr
                ctx = ""
                msgid = ""
                msgstr = ""
                state = None
            continue
        if line.startswith("msgctxt "):
            if state == "msgstr":
                messages[(ctx, msgid)] = msgstr
                ctx = ""
                msgid = ""
                msgstr = ""
            ctx = _unquote(line[len("msgctxt ") :])
            state = "msgctxt"
        elif line.startswith("msgid "):
            if state == "msgstr":
                messages[(ctx, msgid)] = msgstr
                ctx = ""
                msgid = ""
                msgstr = ""
            msgid = _unquote(line[len("msgid ") :])
            state = "msgid"
        elif line.startswith("msgstr "):
            msgstr = _unquote(line[len("msgstr ") :])
            state = "msgstr"
        elif line.startswith('"') and line.endswith('"'):
            piece = _unquote(line)
            if state == "msgid":
                msgid += piece
            elif state == "msgstr":
                msgstr += piece
            elif state == "msgctxt":
                ctx += piece
    if state == "msgstr":
        messages[(ctx, msgid)] = msgstr
    return messages


def _unquote(s: str) -> str:
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    return (
        s.replace("\\n", "\n")
         .replace("\\t", "\t")
         .replace('\\"', '"')
         .replace("\\\\", "\\")
    )


def _write_mo(messages: dict[tuple[str, str], str], mo_path: Path) -> None:
    """Write a minimal MO file per GNU gettext format (little-endian)."""
    keys = sorted(messages.keys())
    offsets: list[tuple[int, int, int, int]] = []
    ids = b""
    strs = b""
    for ctx, msgid in keys:
        key = msgid.encode("utf-8") if not ctx else ctx.encode("utf-8") + b"\x04" + msgid.encode("utf-8")
        val = messages[(ctx, msgid)].encode("utf-8")
        offsets.append((len(ids), len(key), len(strs), len(val)))
        ids += key + b"\x00"
        strs += val + b"\x00"
    keystart = 7 * 4 + 16 * len(keys)
    valuestart = keystart + len(ids)
    koffsets = []
    voffsets = []
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + keystart]
        voffsets += [l2, o2 + valuestart]
    output = struct.pack(
        "Iiiiiii",
        0x950412DE,  # magic
        0,            # version
        len(keys),
        7 * 4,
        7 * 4 + len(keys) * 8,
        0, 0,
    )
    output += struct.pack("i" * len(koffsets), *koffsets)
    output += struct.pack("i" * len(voffsets), *voffsets)
    output += ids
    output += strs
    mo_path.write_bytes(output)


def main() -> int:
    if not LOCALES.is_dir():
        print(f"No locales dir at {LOCALES}", file=sys.stderr)
        return 1
    n = 0
    for po in LOCALES.rglob("*.po"):
        mo = po.with_suffix(".mo")
        messages = _parse_po(po)
        _write_mo(messages, mo)
        print(f"  {po.relative_to(REPO)}  →  {mo.relative_to(REPO)}  ({len(messages)} entries)")
        n += 1
    print(f"Compiled {n} catalog(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
