#!/usr/bin/env python3
"""Recolour a GL style onto the rns.moscow dark-green palette.

dark-matter is a good base for this because it is entirely neutral: 47 layers carry
only 22 distinct colours, every one of them a grey. So the recolour can be mechanical
-- take each colour's relative luminance and look it up on a green ramp anchored to the
site's own design tokens -- instead of hand-tuning 47 layers and hoping they stay
consistent with each other.

Luminance is preserved, which is what keeps the map readable: dark-matter's designers
already worked out that roads must sit ~0.40 against a ~0.05 background, and that
relationship survives the mapping. Alpha is carried through untouched.

Label colours are the exception and are set from the tokens directly rather than by
ramp. Text has to stay legible against terrain, so it takes --text with a --bg halo,
the same pairing the website uses.

Ramp anchors (site tokens marked):
    0.00  #060806
    0.05  #0a0e0a   --bg
    0.10  #0c130c   --bg-card
    0.16  #0e1a0e   --bg-elevated
    0.24  #14261a
    0.32  #1b3b25
    0.40  #235c33            roads: the brightest structural element
    0.60  #00993a
    0.85  #b8d4b8   --text
    1.00  #d4edd4   --text-bright
"""
import colorsys
import json
import re
import sys

TEXT       = (0xb8, 0xd4, 0xb8)   # --text
TEXT_HALO  = (0x0a, 0x0e, 0x0a)   # --bg

# Water needs its own anchor. dark-matter separates it from the background by only
# ~0.06 luminance and a slight blue cast, and a luminance-only mapping keeps the
# luminance while discarding the cast -- leaving water indistinguishable from land.
# This restores the separation with a cool tint that still sits in the palette's
# darkness range. The site defines no water token, so this is chosen, not derived.
WATER      = (0x09, 0x19, 0x1d)
WATER_LAYERS = {'water', 'waterway'}

RAMP = [
    (0.00, (0x06, 0x08, 0x06)),
    (0.05, (0x0a, 0x0e, 0x0a)),
    (0.10, (0x0c, 0x13, 0x0c)),
    (0.16, (0x0e, 0x1a, 0x0e)),
    (0.24, (0x14, 0x26, 0x1a)),
    (0.32, (0x1b, 0x3b, 0x25)),
    (0.40, (0x23, 0x5c, 0x33)),
    (0.60, (0x00, 0x99, 0x3a)),
    (0.85, TEXT),
    (1.00, (0xd4, 0xed, 0xd4)),
]

_HEX = re.compile(r'^#([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')
_FUNC = re.compile(r'^(rgba?|hsla?)\(([^)]*)\)$')


def parse_colour(c):
    """Return (r, g, b, a) with rgb in 0-255, or None if this is not a colour."""
    if not isinstance(c, str):
        return None
    c = c.strip()
    m = _HEX.match(c)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = ''.join(x * 2 for x in h)
        v = [int(h[i:i + 2], 16) for i in range(0, len(h), 2)]
        return (v[0], v[1], v[2], v[3] / 255 if len(v) == 4 else 1.0)
    m = _FUNC.match(c)
    if not m:
        return None
    kind, body = m.group(1), m.group(2)
    parts = [p.strip().rstrip('%') for p in body.replace(',', ' ').split()]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    a = nums[3] if len(nums) > 3 else 1.0
    if kind.startswith('rgb'):
        return (nums[0], nums[1], nums[2], a)
    r, g, b = colorsys.hls_to_rgb(nums[0] / 360, nums[2] / 100, nums[1] / 100)
    return (r * 255, g * 255, b * 255, a)


def luminance(c):
    return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255


def ramp_lookup(L):
    if L <= RAMP[0][0]:
        return RAMP[0][1]
    if L >= RAMP[-1][0]:
        return RAMP[-1][1]
    for (l0, c0), (l1, c1) in zip(RAMP, RAMP[1:]):
        if l0 <= L <= l1:
            t = (L - l0) / (l1 - l0) if l1 > l0 else 0
            return tuple(round(a + (b - a) * t) for a, b in zip(c0, c1))
    return RAMP[-1][1]


def fmt(rgb, a):
    if a >= 0.999:
        return '#%02x%02x%02x' % rgb
    return 'rgba(%d, %d, %d, %g)' % (rgb[0], rgb[1], rgb[2], round(a, 3))


def recolour(value, override=None):
    """Walk a paint value, recolouring any colour strings inside it.

    Paint values are often expressions (interpolate/step/match), so this recurses
    through lists and dicts rather than assuming a bare string.
    """
    if isinstance(value, str):
        c = parse_colour(value)
        if c is None:
            return value
        rgb = override if override else ramp_lookup(luminance(c))
        return fmt(rgb, c[3])
    if isinstance(value, list):
        return [recolour(v, override) for v in value]
    if isinstance(value, dict):
        return {k: recolour(v, override) for k, v in value.items()}
    return value


def theme(style):
    for layer in style.get('layers', []):
        paint = layer.get('paint')
        if not paint:
            continue
        is_water = layer.get('source-layer') in WATER_LAYERS
        for prop, value in list(paint.items()):
            if 'color' not in prop:
                continue
            if prop == 'text-color':
                paint[prop] = recolour(value, TEXT)
            elif prop == 'text-halo-color':
                paint[prop] = recolour(value, TEXT_HALO)
            elif is_water and prop in ('fill-color', 'line-color', 'fill-outline-color'):
                paint[prop] = recolour(value, WATER)
            else:
                paint[prop] = recolour(value)
    return style


# Zoom-lagged rank ceilings for the dense place classes.
#
# At the default view (display zoom 10, which is MapLibre zoom 9 -- ZOOM_OFFSET is -1)
# the Moscow tile carries 175 villages and 92 towns against 21 cities. dark-matter draws
# every one of them, because it gates place labels only by maxzoom and never by rank.
#
# minzoom cannot fix this: towns and villages first appear in the data at z6, so delaying
# the layer start by one zoom leaves z9 untouched. The lever that works is `rank`, which
# the tileset carries. Each ceiling below is the rank range the data naturally reaches one
# zoom level earlier, so zoom Z now renders what Z-1 used to -- the labels move one zoom
# level in, which is the intent, rather than disappearing outright.
#
# Cities are deliberately left alone. They are 21 of the 288 labels at that zoom and they
# are the ones worth reading; thinning them would cost information, not clutter.
#
# rank is coalesced because a place feature may carry none, and a null on either side of
# <= makes the whole comparison false, which would silently drop those features entirely.
RANK_LAG = {
    'place_village': ['step', ['zoom'], 7, 9, 14, 10, 18, 11, 22],
    'place_town':    ['step', ['zoom'], 7, 8, 14, 10, 18, 11, 19],
    'place_other':   ['step', ['zoom'], 7, 12, 19, 13, 22],
}


def _expr_filter(layer, ceiling):
    """Rebuild a legacy filter as an expression and add the rank ceiling.

    The legacy and expression filter syntaxes cannot be mixed in one filter, so the
    class/geometry tests are translated rather than wrapped.
    """
    out = ['all']
    for clause in layer.get('filter', ['all'])[1:]:
        if clause[:2] == ['==', '$type']:
            out.append(['==', ['geometry-type'], clause[2]])
        elif clause[0] == '==':
            out.append(['==', ['get', clause[1]], clause[2]])
        elif clause[0] == 'in':
            out.append(['in', ['get', clause[1]], ['literal', list(clause[2:])]])
        elif clause[0] in ('<', '<=', '>', '>='):
            out.append([clause[0], ['get', clause[1]], clause[2]])
        else:
            raise ValueError(f"unhandled filter clause in {layer['id']}: {clause}")
    out.append(['<=', ['coalesce', ['get', 'rank'], 0], ceiling])
    return out


def apply_overrides(style):
    for layer in style.get('layers', []):
        ceiling = RANK_LAG.get(layer['id'])
        if ceiling:
            layer['filter'] = _expr_filter(layer, ceiling)
    return style


def main():
    if len(sys.argv) != 3:
        sys.exit('usage: theme_style.py <in.json> <out.json>')
    style = json.load(open(sys.argv[1]))
    style = theme(style)
    style = apply_overrides(style)
    style['name'] = 'RNS Moscow Dark'
    style['id'] = 'rns-dark'
    with open(sys.argv[2], 'w') as f:
        json.dump(style, f, separators=(',', ':'))
    print(f"wrote {sys.argv[2]}")


if __name__ == '__main__':
    main()
