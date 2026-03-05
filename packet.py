from __future__ import annotations

import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, IntEnum
from zlib import crc32


# Types of data sections in ClusterDuck packets
class Data(ABC):
    @classmethod
    @abstractmethod
    def decode(cls, raw: bytes) -> Data:
        pass

    @abstractmethod
    def encode(self) -> bytes:
        pass


# https://github.com/ClusterDuck-Protocol/ClusterDuck-Protocol/blob/4f0e00d1963783f93bae4ab3d6046a36be4a3e9c/src/CdpPacket.h#L37
@dataclass
class AckData(Data):
    pairs: list[tuple[int, int]]

    @classmethod
    def decode(cls, raw: bytes) -> AckData:
        n = struct.unpack_from("B", raw, 0)[0]
        pairs = []
        pair_iterator = struct.iter_unpack("8s4s", raw[1:])
        for i in range(n):
            pairs.append(next(pair_iterator))
        return cls(pairs)

    def encode(self) -> bytes:
        return struct.pack("!B", len(self.pairs)) + b"".join(
            struct.pack("!8s4s", duid, muid) for (duid, muid) in self.pairs
        )


# https://github.com/ClusterDuck-Protocol/ClusterDuck-Protocol/blob/4f0e00d1963783f93bae4ab3d6046a36be4a3e9c/src/CdpPacket.h#L61
@dataclass
class CommandData(Data):
    n: int
    data: bytes

    @classmethod
    def decode(cls, raw: bytes) -> CommandData:
        n = struct.unpack_from("B", raw, 0)[0]
        return cls(n, raw[1:])

    def encode(self) -> bytes:
        return struct.pack("!B", self.n) + self.data


@dataclass
class UnknownData(Data):
    data: bytes

    @classmethod
    def decode(cls, raw: bytes) -> UnknownData:
        return cls(raw)

    def encode(self) -> bytes:
        return self.data


# https://github.com/ClusterDuck-Protocol/ClusterDuck-Protocol/blob/4f0e00d1963783f93bae4ab3d6046a36be4a3e9c/src/CdpPacket.h#L56
class CommandTypes(IntEnum):
    HEALTH = 0
    WIFI = 1
    CHANNEL = 2


class Topic(IntEnum):
    # Reserved topics
    # https://github.com/ClusterDuck-Protocol/ClusterDuck-Protocol/blob/4f0e00d1963783f93bae4ab3d6046a36be4a3e9c/src/CdpPacket.h#L116
    UNUSED = 0x00
    PING = 0x01
    PONG = 0x02
    GPS = 0x03
    ACK = 0x04
    CMD = 0x05
    MAX_RESERVED = 0x0F
    # Unreserved topics
    # https://github.com/ClusterDuck-Protocol/ClusterDuck-Protocol/blob/4f0e00d1963783f93bae4ab3d6046a36be4a3e9c/src/CdpPacket.h#L84
    # generic message (e.g non emergency messages)
    STATUS = 0x10
    # captive portal message
    CPM = 0x11
    # a gps or geo location (e.g longitude/latitude)
    LOCATION = 0x12
    # sensor captured data
    SENSOR = 0x13
    # an allert message that should be given immediate attention
    ALERT = 0x14
    # Device health status
    HEALTH = 0x15
    # Send duck commands
    DCMD = 0x16
    # MQ7 Gas Sensor
    MQ7 = 0xEF
    # GP2Y Dust Sensor
    GP2Y = 0xFA
    # bmp280
    BMP280 = 0xFB
    # DHT11 sensor
    DHT11 = 0xFC
    # ir sensor
    PIR = 0xFD
    # bmp180
    BMP180 = 0xFE
    # Max supported topics
    MAX = 0xFF
    # Our custom topic
    WILD = 126

    @classmethod
    def _missing_(cls, value):
        if not isinstance(value, int):
            return None
        # https://stackoverflow.com/a/77092528
        unknown_enum_val = int.__new__(cls, value)
        unknown_enum_val._name_ = "OTHER"
        unknown_enum_val._value_ = value
        return unknown_enum_val


# https://github.com/ClusterDuck-Protocol/ClusterDuck-Protocol/blob/4f0e00d1963783f93bae4ab3d6046a36be4a3e9c/src/include/DuckTypes.h#L8
class DuckType(IntEnum):
    # A Duck of unknown type ()
    UNKNOWN = 0x00
    # A PapaDuck
    PAPA = 0x01
    # A MamaDuck
    MAMA = 0x02
    # A DuckLink
    LINK = 0x03
    # A Detector Duck
    DETECTOR = 0x04

    @classmethod
    def _missing_(cls, value):
        if not isinstance(value, int):
            return None
        # https://stackoverflow.com/a/77092528
        unknown_enum_val = int.__new__(cls, value)
        unknown_enum_val._name_ = "OTHER"
        unknown_enum_val._value_ = value
        return unknown_enum_val


class Duids(Enum):
    PAPA = b"\x00\x00\x00\x00\x00\x00\x00\x00"
    BROADCAST = b"\xff\xff\xff\xff\xff\xff\xff\xff"


# https://github.com/ClusterDuck-Protocol/ClusterDuck-Protocol/blob/4f0e00d1963783f93bae4ab3d6046a36be4a3e9c/src/CdpPacket.h#L126
@dataclass
class CdpPacket:
    sduid: bytes
    dduid: bytes
    muid: bytes
    topic: Topic
    duck_type: DuckType
    hop_count: int
    data: Data | None

    @classmethod
    def decode(cls, raw: bytes) -> CdpPacket:
        (sduid, dduid, muid, topic, duck_type, hop_count, data_crc) = (
            struct.unpack_from("!8s8s4sBBBL", raw, 0)
        )
        topic = Topic(topic)
        duck_type = DuckType(duck_type)
        data_raw = raw[27:]
        if len(data_raw) == 0:
            data = None
        elif topic == Topic.CMD:
            data = CommandData.decode(data_raw)
        elif topic == Topic.ACK:
            data = AckData.decode(data_raw)
        else:
            data = UnknownData.decode(data_raw)
        return cls(sduid, dduid, muid, topic, duck_type, hop_count, data)

    def encode(self) -> bytes:
        if self.data is not None:
            data_raw = self.data.encode()
        else:
            data_raw = b""
        return (
            struct.pack(
                "!8s8s4sBBBL",
                self.sduid,
                self.dduid,
                self.muid,
                self.topic,
                self.duck_type,
                self.hop_count,
                crc32(data_raw),
            )
            + data_raw
        )
