"""Radio and geography helpers shared by traceroutes, links and zones."""

import math
from typing import Protocol

EARTH_RADIUS_KM = 6371.0

# Radio channels: channel names that tell a node's radio settings, with the
# LoRa spreading factor of those settings. Each Meshtastic modem preset is
# listed by the name its primary channel gets when left unnamed (the
# firmware's DisplayFormatters, so LongModerate is "LongMod"), plus regional
# primary channels with known settings: SFNarrow, NarrowPlus and
# Medium24 (2.4 GHz, SF9 at 250 kHz) and Gaulix's Fr_Balise (France,
# LongModerate). Add a community's primary channel here once its preset is
# known; not its chat channels, or nodes would flip to them, nor channels
# bridged over MQTT downlink, such as Canarias: SFNarrow's settings,
# but its senders are mostly LongFast nodes. The spreading factor sets how
# weak a signal the radio can still decode.
PRESET_SPREADING_FACTORS = {
    "ShortTurbo": 7,
    "ShortFast": 7,
    "ShortSlow": 8,
    "MediumTurbo": 9,
    "MediumFast": 9,
    "MediumSlow": 10,
    "LongTurbo": 11,
    "LongFast": 11,
    "LongMod": 11,
    "LongSlow": 12,
    "LiteFast": 9,
    "LiteSlow": 10,
    "NarrowFast": 7,
    "NarrowSlow": 8,
    "TinyFast": 7,
    "TinySlow": 8,
    "SFNarrow": 7,
    "NarrowPlus": 6,
    "Medium24": 9,
    "Fr_Balise": 11,
}
_PRESET_NAMES = {name.lower(): name for name in PRESET_SPREADING_FACTORS}


def great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance between two positions over the Earth's surface (haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi, d_lambda = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


class Placed(Protocol):
    """Anything with a position, such as a node or a zone."""

    @property
    def latitude(self) -> float | None: ...

    @property
    def longitude(self) -> float | None: ...


def distance_km(a: Placed | None, b: Placed | None) -> float | None:
    """The great-circle distance between two positions, to 0.1 km; None when
    either is missing or has no position."""
    if a is None or b is None:
        return None
    if a.latitude is None or a.longitude is None or b.latitude is None or b.longitude is None:
        return None
    return round(great_circle_km(a.latitude, a.longitude, b.latitude, b.longitude), 1)


def spreading_factor(channel: str, default_preset: str) -> int:
    """The spreading factor a channel name suggests, else the default preset's.

    Channel names are only hints: a channel named after a place says nothing
    about the radio settings, so it gets the configured default.
    """
    return PRESET_SPREADING_FACTORS[preset_name(channel) or default_preset]


def fade_margin(average_snr: float, spreading_factor: int) -> float:
    """How far an average SNR sits above the weakest the radio can still
    decode at this spreading factor, in dB (the map's link colours use the same)."""
    return average_snr - (-7.5 - 2.5 * (spreading_factor - 7))


def preset_name(name: str) -> str | None:
    """A known preset name in its usual spelling, whatever its case; None if unknown."""
    return _PRESET_NAMES.get(name.lower())


def modem_preset_spreading_factor(modem_preset: str) -> int | None:
    """The spreading factor of a map report's modem preset (LONG_FAST and the
    like); None if unknown."""
    name = (
        "LongMod" if modem_preset == "LONG_MODERATE" else preset_name(modem_preset.replace("_", ""))
    )
    return None if name is None else PRESET_SPREADING_FACTORS[name]


def relay_byte_of(node_id: int) -> int:
    """The relay byte a node puts on the air: its number's last byte, except
    that a number ending in 00 sends FF (firmware's getLastByteOfNodeNum), as
    0 means no relay. relay_byte_sql is the same rule in SQL."""
    return node_id & 0xFF or 0xFF


def relay_byte_sql(node_id: str) -> str:
    """relay_byte_of in SQL, for the node number expression node_id."""
    return f"coalesce(nullif({node_id} % 256, 0), 255)"
