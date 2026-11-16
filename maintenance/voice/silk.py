"""Strict packet framing inspection; payload decoding remains pilk's responsibility."""

import struct


def inspect_packets(data):
    if data.startswith(b"\x02"):
        data = data[1:]
    if not data.startswith(b"#!SILK_V3"):
        raise ValueError("Not Silk V3")
    offset = 9
    lengths = []
    while offset < len(data):
        if len(data) - offset < 2:
            raise ValueError("Truncated Silk packet header")
        length = struct.unpack_from("<h", data, offset)[0]
        offset += 2
        if length == -1:
            if offset != len(data):
                raise ValueError("Unexpected bytes after Silk terminator")
            break
        if length < 0 or offset + length > len(data):
            raise ValueError("Invalid or truncated Silk packet")
        lengths.append(length)
        offset += length
    return {
        "packet_count": len(lengths),
        "max_packet_bytes": max(lengths, default=0),
        "duration_if_20ms_packets": len(lengths) * 0.02,
    }
