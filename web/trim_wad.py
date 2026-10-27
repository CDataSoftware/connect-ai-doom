"""Copy freedoom2.wad without MAP02-MAP32, for the browser to download: the game plays only MAP01,
from state/level.wad, and the level has no exit to the next map. That's a third of the file.

    python3 web/trim_wad.py wads/freedoom2.wad web/dist/freedoom2.wad
"""
import re
import struct
import sys

MAP_LUMPS = {"THINGS", "LINEDEFS", "SIDEDEFS", "VERTEXES", "SEGS", "SSECTORS", "NODES", "SECTORS", "REJECT",
             "BLOCKMAP", "BEHAVIOR"}


def trim(src, dst):
    data = open(src, "rb").read()
    kind, count, table = struct.unpack("<4sii", data[:12])
    keep, dropping = [], False
    for i in range(count):
        offset, size, raw = struct.unpack("<ii8s", data[table + 16 * i:table + 16 * i + 16])
        name = raw.rstrip(b"\0").decode("ascii")
        if re.fullmatch(r"MAP\d\d", name):
            dropping = name != "MAP01"      # MAP01's marker and lumps stay: the game expects a Doom II IWAD
        elif name not in MAP_LUMPS:
            dropping = False
        if not dropping:
            keep.append((raw, data[offset:offset + size]))
    out, entries = bytearray(), bytearray()
    pos = 12
    for raw, lump in keep:
        entries += struct.pack("<ii8s", pos, len(lump), raw)
        out += lump
        pos += len(lump)
    with open(dst, "wb") as f:
        f.write(struct.pack("<4sii", kind, len(keep), pos) + out + entries)
    return count, len(keep)


if __name__ == "__main__":
    before, after = trim(sys.argv[1], sys.argv[2])
    print(f"{sys.argv[2]}: {after} of {before} lumps")
