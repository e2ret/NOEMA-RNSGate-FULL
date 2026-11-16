#!/usr/bin/env python3
"""Compose multi-font glyph stacks into single directories servable as static files.

MapLibre requests a composed fontstack as one path segment, e.g.
    /fonts/Metropolis Regular,Noto Sans Regular/0-255.pbf
Upstream font packs ship each face separately, and composing them is normally the
job of a glyph server. Since the map is meant to run off plain nginx, we do the
composition once at build time and write out the combined directories.

Glyph precedence follows the stack order: the first font that provides a codepoint
wins, later fonts only fill gaps (Metropolis covers Latin, Noto Sans covers the rest).

Vendored protobuf handling is deliberate -- glyphs.proto is small enough to parse by
hand, and that keeps the build free of a protobuf dependency.

    glyphs   { repeated fontstack stacks = 1; }
    fontstack{ required string name = 1; required string range = 2;
               repeated glyph glyphs = 3; }
    glyph    { required uint32 id = 1; ... }
"""
import os
import sys

def _read_varint(buf, pos):
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7

def _write_varint(value):
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | (0x80 if value else 0))
        if not value:
            return bytes(out)

def _fields(buf, start, end):
    """Yield (field_number, wire_type, payload_bytes, next_pos) over a message body."""
    pos = start
    while pos < end:
        key, pos = _read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, pos = _read_varint(buf, pos)
            yield field, wire, val
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            yield field, wire, buf[pos:pos + length]
            pos += length
        elif wire == 5:
            yield field, wire, buf[pos:pos + 4]
            pos += 4
        elif wire == 1:
            yield field, wire, buf[pos:pos + 8]
            pos += 8
        else:
            raise ValueError(f"unsupported wire type {wire}")

def _tag(field, wire):
    return _write_varint((field << 3) | wire)

def _delimited(field, payload):
    return _tag(field, 2) + _write_varint(len(payload)) + payload

def parse_glyphs(data):
    """Return (range_string, {codepoint: raw_glyph_submessage}) from one .pbf."""
    rng = None
    glyphs = {}
    for field, _wire, payload in _fields(data, 0, len(data)):
        if field != 1:
            continue
        for sfield, _swire, spayload in _fields(payload, 0, len(payload)):
            if sfield == 2:
                rng = spayload.decode("utf-8")
            elif sfield == 3:
                for gfield, _gwire, gpayload in _fields(spayload, 0, len(spayload)):
                    if gfield == 1:
                        glyphs[gpayload] = spayload
                        break
    return rng, glyphs

def build_stack(data, name, rng, glyphs):
    body = _delimited(1, name.encode("utf-8")) + _delimited(2, rng.encode("utf-8"))
    for gid in sorted(glyphs):
        body += _delimited(3, glyphs[gid])
    return _delimited(1, body)

def compose(src_root, out_root, stack):
    """Write one composed stack directory; returns the number of ranges written."""
    fonts = [f.strip() for f in stack.split(",")]
    missing = [f for f in fonts if not os.path.isdir(os.path.join(src_root, f))]
    if missing:
        raise SystemExit(f"missing source font(s): {missing}")

    out_dir = os.path.join(out_root, stack)
    os.makedirs(out_dir, exist_ok=True)

    ranges = sorted(f for f in os.listdir(os.path.join(src_root, fonts[0]))
                    if f.endswith(".pbf"))
    for fname in ranges:
        merged = {}
        rng = None
        for font in fonts:
            path = os.path.join(src_root, font, fname)
            if not os.path.exists(path):
                continue
            with open(path, "rb") as fh:
                r, glyphs = parse_glyphs(fh.read())
            if r is not None:
                rng = r
            for gid, raw in glyphs.items():
                merged.setdefault(gid, raw)   # first font in the stack wins
        if rng is None:
            rng = fname[:-4]
        with open(os.path.join(out_dir, fname), "wb") as fh:
            fh.write(build_stack(None, stack, rng, merged))
    return len(ranges)

STACKS = [
    "Metropolis Regular,Noto Sans Regular",
    "Metropolis Light,Noto Sans Regular",
    "Metropolis Light Italic,Noto Sans Italic",
    "Metropolis Medium Italic,Noto Sans Italic",
]

def main():
    src_root = sys.argv[1] if len(sys.argv) > 1 else "fonts-src"
    out_root = sys.argv[2] if len(sys.argv) > 2 else "fonts"
    for stack in STACKS:
        n = compose(src_root, out_root, stack)
        total = sum(os.path.getsize(os.path.join(out_root, stack, f))
                    for f in os.listdir(os.path.join(out_root, stack)))
        print(f"{stack}: {n} ranges, {total / 1e6:.1f} MB")

if __name__ == "__main__":
    main()
