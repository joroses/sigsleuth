"""Anonymise a recorded sample in place before committing it.

Usage: uv run --locked python scripts/anonymise-sample.py [tests/sample/deliveries.jsonl.gz]

Node names become "Node 3f2a" (from the node number), texts and node statuses
become filler of about the same length, and positions finer than 15 bits
(about 730 m) are cut to 15 bits the way the firmware reduces precision.
Pure emoji texts (reactions) stay. Node numbers, signal and routes stay.
Each change depends only on what it replaces, so every gateway's copy of a
packet still comes out the same and groups as before. Deliveries it does not
change keep their bytes.
"""

import base64
import gzip
import hashlib
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from google.protobuf.message import DecodeError
from meshtastic.protobuf import mesh_pb2, mqtt_pb2, portnums_pb2  # type: ignore[import-untyped]

from sigsleuth.decode import DEFAULT_CHANNEL_KEY, invalid_position_reason, parse_delivery

PRECISION_BITS = 15
# Node status texts also arrive on this port, which this protobuf version does
# not name (loranet.pl, 2026-10).
STATUS_PORTS = (portnums_pb2.NODE_STATUS_APP, 278)
WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()


def name(node_id: int) -> tuple[str, str]:
    short = f"{node_id:08x}"[-4:]
    return f"Node {short}", short


def filler(text: str) -> str:
    """Words picked by the text's hash, about as long as the text. Filler stays
    as it is, so a second run changes nothing."""
    if not any(character.isalnum() for character in text):
        return text
    *whole, last = text.split(" ")
    if set(whole) <= set(WORDS) and any(word.startswith(last) for word in WORDS):
        return text
    digest = hashlib.sha256(text.encode()).digest()
    words: list[str] = []
    while len(" ".join(words)) < len(text):
        words.append(WORDS[digest[len(words) % len(digest)] % len(WORDS)])
    return " ".join(words)[: max(len(text), 1)].rstrip()


def coarse(latitude_i: int, longitude_i: int, precision: int) -> tuple[int, int, int]:
    if 0 < precision <= PRECISION_BITS:
        return latitude_i, longitude_i, precision
    invalid = invalid_position_reason(latitude_i / 1e7, longitude_i / 1e7)
    if invalid is not None:
        return latitude_i, longitude_i, precision
    return _cut(latitude_i), _cut(longitude_i), PRECISION_BITS


def _cut(value: int) -> int:
    """Keep the top PRECISION_BITS bits and move to the middle of the cell, as firmware does."""
    unsigned = value & 0xFFFFFFFF & (0xFFFFFFFF << (32 - PRECISION_BITS))
    unsigned += 1 << (31 - PRECISION_BITS)
    return unsigned - (1 << 32) if unsigned >= 1 << 31 else unsigned


def anonymise_payload(sender: int, data: mesh_pb2.Data) -> bool:
    """Anonymise one packet's application data in place; whether it changed."""
    before = data.payload
    if data.portnum == portnums_pb2.TEXT_MESSAGE_APP or data.portnum in STATUS_PORTS:
        text = (
            data.payload.decode("utf-8", "replace")
            if data.portnum == portnums_pb2.TEXT_MESSAGE_APP
            else _status_text(data.payload)
        )
        replaced = filler(text).encode()
        if data.portnum in STATUS_PORTS:
            replaced = b"\n" + _varint(len(replaced)) + replaced
        data.payload = replaced
    elif data.portnum == portnums_pb2.NODEINFO_APP:
        user = mesh_pb2.User()
        user.ParseFromString(data.payload)
        long_name, short_name = name(sender)
        if user.long_name:
            user.long_name = long_name
        if user.short_name:
            user.short_name = short_name
        data.payload = user.SerializeToString()
    elif data.portnum == portnums_pb2.POSITION_APP:
        position = mesh_pb2.Position()
        position.ParseFromString(data.payload)
        if position.HasField("latitude_i") and position.HasField("longitude_i"):
            position.latitude_i, position.longitude_i, position.precision_bits = coarse(
                position.latitude_i, position.longitude_i, position.precision_bits
            )
            data.payload = position.SerializeToString()
    elif data.portnum == portnums_pb2.MAP_REPORT_APP:
        report = mqtt_pb2.MapReport()
        report.ParseFromString(data.payload)
        long_name, short_name = name(sender)
        if report.long_name:
            report.long_name = long_name
        if report.short_name:
            report.short_name = short_name
        if report.latitude_i or report.longitude_i:
            report.latitude_i, report.longitude_i, report.position_precision = coarse(
                report.latitude_i, report.longitude_i, report.position_precision
            )
        data.payload = report.SerializeToString()
    return bool(data.payload != before)


def _status_text(payload: bytes) -> str:
    """A node status payload's text: field 1, which this protobuf version does not know."""
    if not payload.startswith(b"\n"):
        return payload.decode("utf-8", "replace")
    length, offset = 0, 1
    for shift in range(0, 35, 7):
        byte = payload[offset]
        offset += 1
        length |= (byte & 0x7F) << shift
        if byte < 0x80:
            break
    return payload[offset : offset + length].decode("utf-8", "replace")


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 0x80:
        encoded.append(value & 0x7F | 0x80)
        value >>= 7
    return bytes([*encoded, value])


def _default_key(packet: mesh_pb2.MeshPacket, payload: bytes) -> bytes:
    """AES-CTR with the default key; the same call encrypts and decrypts."""
    nonce = packet.id.to_bytes(8, "little") + getattr(packet, "from").to_bytes(4, "little")
    cipher = Cipher(algorithms.AES(DEFAULT_CHANNEL_KEY), modes.CTR(nonce + bytes(4)))
    crypt = cipher.encryptor()
    return crypt.update(payload) + crypt.finalize()


def anonymise_delivery(topic: str, payload: bytes) -> bytes | None:
    """The delivery's anonymised payload, or None when nothing in it changes."""
    parsed = parse_delivery(topic, payload)
    if parsed.map_report is not None:
        sender = parsed.map_report.node_id
    # Not only packets capture reads: the default key also opens ports it
    # rejects, such as 278's status texts.
    elif parsed.evidence is not None:
        sender = parsed.evidence.sender_node_id
    else:
        return None
    envelope = mqtt_pb2.ServiceEnvelope()
    envelope.ParseFromString(payload)
    packet = envelope.packet
    if packet.WhichOneof("payload_variant") == "decoded":
        changed = anonymise_payload(sender, packet.decoded)
    else:
        data = mesh_pb2.Data()
        try:
            data.ParseFromString(_default_key(packet, packet.encrypted))
        except DecodeError:
            return None
        changed = anonymise_payload(sender, data)
        packet.encrypted = _default_key(packet, data.SerializeToString())
    return envelope.SerializeToString() if changed else None


def main(path: Path) -> None:
    changed = 0
    with gzip.open(path, "rt", encoding="utf-8") as lines:
        output: list[str] = []
        for line in lines:
            delivery = json.loads(line)
            payload = anonymise_delivery(delivery["topic"], base64.b64decode(delivery["payload"]))
            if payload is not None:
                delivery["payload"] = base64.b64encode(payload).decode("ascii")
                line = json.dumps(delivery, separators=(",", ":")) + "\n"
                changed += 1
            output.append(line)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as out:
        out.writelines(output)
    print(f"{changed:,} of {len(output):,} deliveries changed")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) == 2 else "tests/sample/deliveries.jsonl.gz"))
