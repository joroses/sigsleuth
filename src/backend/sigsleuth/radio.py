"""Radio and geography helpers shared by traceroutes, links and zones."""

import math
from dataclasses import dataclass
from typing import Protocol

EARTH_RADIUS_KM = 6371.0

# A wide LoRa region (2.4 GHz) runs each preset at this many times its
# bandwidth (firmware's modemPresetToParams), and the survey app reports the
# widened bandwidth.
WIDE_LORA_BANDWIDTH = 3.25


@dataclass(frozen=True)
class Radio:
    """A radio channel's LoRa spreading factor and bandwidth in kHz (None
    when unknown). The spreading factor sets how weak a signal the radio can
    still decode. Presets on one spreading factor but different bandwidths
    (ShortFast and NarrowFast) do not hear each other. exact: the bandwidth
    is the one the radio runs at (a survey's report, a 2.4 GHz channel);
    otherwise it is a preset's, which a wide region runs wider."""

    spreading_factor: int
    bandwidth_khz: float | None = None
    exact: bool = False

    def bandwidths(self) -> tuple[float, ...]:
        """The bandwidths the radio may run at; empty when unknown."""
        if self.bandwidth_khz is None:
            return ()
        if self.exact:
            return (self.bandwidth_khz,)
        return (self.bandwidth_khz, self.bandwidth_khz * WIDE_LORA_BANDWIDTH)

    def matches(self, other: "Radio") -> bool:
        """Whether the two can be the same radio settings: the same spreading
        factor, and a bandwidth both may run at unless either is unknown."""
        if self.spreading_factor != other.spreading_factor:
            return False
        mine, theirs = self.bandwidths(), other.bandwidths()
        return not mine or not theirs or not set(mine).isdisjoint(theirs)


# Radio channels: channel names that tell a node's radio settings. Each
# Meshtastic modem preset is listed by the name its primary channel gets when
# left unnamed (the firmware's DisplayFormatters, so LongModerate is
# "LongMod"), plus regional primary channels with known settings: SFNarrow
# and NarrowPlus (62.5 kHz), Medium24 (2.4 GHz MediumSlow, SF10 at 812.5 kHz) and
# Gaulix's Fr_Balise (France, LongModerate). Add a community's primary
# channel here once its preset is known; not its chat channels, or nodes
# would flip to them, nor channels bridged over MQTT downlink, such as
# Canarias: SFNarrow's settings, but its senders are mostly LongFast nodes.
PRESET_RADIOS = {
    "ShortTurbo": Radio(7, 500),
    "ShortFast": Radio(7, 250),
    "ShortSlow": Radio(8, 250),
    "MediumTurbo": Radio(9, 500),
    "MediumFast": Radio(9, 250),
    "MediumSlow": Radio(10, 250),
    "LongTurbo": Radio(11, 500),
    "LongFast": Radio(11, 250),
    "LongMod": Radio(11, 125),
    "LongSlow": Radio(12, 125),
    "LiteFast": Radio(9, 125),
    "LiteSlow": Radio(10, 125),
    "NarrowFast": Radio(7, 62.5),
    "NarrowSlow": Radio(8, 62.5),
    "TinyFast": Radio(7, 15.625),
    "TinySlow": Radio(8, 15.625),
    "SFNarrow": Radio(7, 62.5),
    "NarrowPlus": Radio(6, 62.5),
    "Medium24": Radio(10, 812.5, exact=True),
    "Fr_Balise": Radio(11, 125),
}
PRESET_SPREADING_FACTORS = {name: radio.spreading_factor for name, radio in PRESET_RADIOS.items()}
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


def fade_margin(average_snr: float, spreading_factor: int) -> float:
    """How far an average SNR sits above the weakest the radio can still
    decode at this spreading factor, in dB (the map's link colours use the same)."""
    return average_snr - (-7.5 - 2.5 * (spreading_factor - 7))


def preset_name(name: str) -> str | None:
    """A known preset name in its usual spelling, whatever its case; None if unknown."""
    return _PRESET_NAMES.get(name.lower())


def modem_preset_radio(modem_preset: str) -> Radio | None:
    """The radio settings of a map report's modem preset (LONG_FAST and the
    like); None if unknown."""
    name = (
        "LongMod" if modem_preset == "LONG_MODERATE" else preset_name(modem_preset.replace("_", ""))
    )
    return None if name is None else PRESET_RADIOS[name]


def relay_byte_of(node_id: int) -> int:
    """The relay byte a node puts on the air: its number's last byte, except
    that a number ending in 00 sends FF (firmware's getLastByteOfNodeNum), as
    0 means no relay. relay_byte_sql is the same rule in SQL."""
    return node_id & 0xFF or 0xFF


def relay_byte_sql(node_id: str) -> str:
    """relay_byte_of in SQL, for the node number expression node_id."""
    return f"coalesce(nullif({node_id} % 256, 0), 255)"
