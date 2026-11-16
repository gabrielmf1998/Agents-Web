#!/usr/bin/env python3
"""Wrap a PNG (up to 256x256) in an .ico file, which Windows Vista and later read as is.
Usage: ico.py icon.png icon.ico"""
import struct, sys

png = open(sys.argv[1], "rb").read()
if png[:8] != b"\x89PNG\r\n\x1a\n":
    sys.exit("not a PNG file")
w, h = struct.unpack(">II", png[16:24])
if w > 256 or h > 256:
    sys.exit("an .ico image is at most 256x256")
head = struct.pack("<HHH", 0, 1, 1)
entry = struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32, len(png), 6 + 16)
open(sys.argv[2], "wb").write(head + entry + png)
