"""Radio and geography helpers shared by traceroutes, links and zones."""

import math
import string
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Protocol

EARTH_RADIUS_KM = 6371.0

# A wide LoRa region (2.4 GHz) runs each preset at this many times its
# bandwidth (firmware's modemPresetToParams), and the survey app reports the
# widened bandwidth.
WIDE_LORA_BANDWIDTH = 3.25


@dataclass(frozen=True)
class Radio:
    """A radio's LoRa spreading factor, bandwidth in kHz and frequency in MHz
    (None when unknown), and the radio channel it is on: None for settings
    alone, such as a map report's preset or a survey's. The spreading factor
    sets how weak a signal the radio can still decode. Presets on one
    spreading factor but different bandwidths (ShortFast and NarrowFast) do
    not hear each other. exact: the bandwidth is the one the radio runs at (a
    survey's report, a 2.4 GHz channel); otherwise it is a preset's, which a
    wide region runs wider."""

    spreading_factor: int
    bandwidth_khz: float | None = None
    exact: bool = False
    frequency_mhz: float | None = None
    channel: str | None = None

    def bandwidths(self) -> tuple[float, ...]:
        """The bandwidths the radio may run at; empty when unknown."""
        if self.bandwidth_khz is None:
            return ()
        if self.exact:
            return (self.bandwidth_khz,)
        return (self.bandwidth_khz, self.bandwidth_khz * WIDE_LORA_BANDWIDTH)

    def matches(self, other: "Radio") -> bool:
        """Whether the two can be on one mesh. Two radio channels only when
        they are the same one, as each primary channel name gets its own
        frequency slot. Otherwise the settings must match and fit the channel."""
        if self.channel is not None and other.channel is not None:
            return self.channel == other.channel
        if not same_settings(self, other):
            return False
        if self.channel is not None:
            return self.channel in other.channels()
        if other.channel is not None:
            return other.channel in self.channels()
        return True

    def channels(self) -> set[str]:
        """The radio channels with these settings (same_settings), only
        those declaring this frequency when any does."""
        fitting = {name for name, radio in PRESET_RADIOS.items() if same_settings(self, radio)}
        declared = {name for name in fitting if PRESET_RADIOS[name].frequency_mhz is not None}
        return declared if self.frequency_mhz is not None and declared else fitting


def same_settings(a: Radio, b: Radio) -> bool:
    """The same spreading factor, a bandwidth both may run at, and the same
    frequency slot, each unless either is unknown. Slots sit a bandwidth
    apart, so frequencies within half a bandwidth share one: the survey app
    sends 869.618 for SFNarrow's 869.6187."""
    if a.spreading_factor != b.spreading_factor:
        return False
    mine, theirs = a.bandwidths(), b.bandwidths()
    if mine and theirs and set(mine).isdisjoint(theirs):
        return False
    if a.frequency_mhz is None or b.frequency_mhz is None:
        return True
    widths = [radio.bandwidth_khz for radio in (a, b) if radio.bandwidth_khz is not None]
    return not widths or abs(a.frequency_mhz - b.frequency_mhz) < min(widths) / 2000


# Meshtastic's modem presets, by the name their primary channel gets when
# left unnamed (the firmware's DisplayFormatters, so LongModerate is "LongMod").
MODEM_PRESETS = {
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
}
_MODEM_PRESET_KEYS = {
    **{name.lower(): radio for name, radio in MODEM_PRESETS.items()},
    "longmoderate": MODEM_PRESETS["LongMod"],
}

# Radio channels: channel names that tell a node's radio settings. The modem
# presets', regional primary channels with known settings, and a network's
# own (declare_radio_channels). Regional: SFNarrow and NarrowPlus (62.5 kHz),
# Medium24 (2.4 GHz MediumSlow, SF10 at 812.5 kHz) and Gaulix's Fr_Balise
# (France, LongModerate). A channel on a preset's settings names its frequency
# when known, so surveys there count for it alone. Add a community's primary
# channel here once its preset is known; not its chat channels, or nodes
# would flip to them, nor channels bridged over MQTT downlink, such as
# Canarias: SFNarrow's settings, but its senders are mostly LongFast nodes.
PRESET_RADIOS = {
    name: replace(radio, channel=name)
    for name, radio in {
        **MODEM_PRESETS,
        "SFNarrow": Radio(7, 62.5, frequency_mhz=869.6187),
        "NarrowPlus": Radio(6, 62.5),
        "Medium24": Radio(10, 812.5, exact=True),
        "Fr_Balise": Radio(11, 125, frequency_mhz=869.4625),
    }.items()
}
BUILT_IN_CHANNELS = frozenset(PRESET_RADIOS)
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def channel_key(name: str) -> str:
    """A channel name lowercased as SQLite's lower() does it, A to Z only,
    so capture and rederive agree on names such as Česko."""
    return name.translate(_ASCII_LOWER)


_PRESET_NAMES = {channel_key(name): name for name in PRESET_RADIOS}


def declare_radio_channels(declared: Mapping[str, Radio]) -> None:
    """Make these a network's own radio channels (SIGSLEUTH_RADIO_CHANNELS),
    in place of any declared before."""
    for name in set(PRESET_RADIOS) - BUILT_IN_CHANNELS:
        del PRESET_RADIOS[name], _PRESET_NAMES[channel_key(name)]
    for name, radio in declared.items():
        PRESET_RADIOS[name] = replace(radio, channel=name)
        _PRESET_NAMES[channel_key(name)] = name


def declared_radio_channels() -> str:
    """The radio channels declare_radio_channels added, as text that changes
    with them."""
    return ",".join(
        f"{name}={radio.spreading_factor}/{radio.bandwidth_khz}@{radio.frequency_mhz}"
        for name, radio in sorted(PRESET_RADIOS.items())
        if name not in BUILT_IN_CHANNELS
    )


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
    """A radio channel's name in its usual spelling, whatever its case; None
    for other names."""
    return _PRESET_NAMES.get(channel_key(name))


def modem_preset_radio(modem_preset: str) -> Radio | None:
    """The radio settings of a modem preset, named as in map reports
    (LONG_FAST) or as its channel (LongFast), whatever the case; None if
    unknown."""
    return _MODEM_PRESET_KEYS.get(modem_preset.replace("_", "").lower())


def relay_byte_of(node_id: int) -> int:
    """The relay byte a node puts on the air: its number's last byte, except
    that a number ending in 00 sends FF (firmware's getLastByteOfNodeNum), as
    0 means no relay. relay_byte_sql is the same rule in SQL."""
    return node_id & 0xFF or 0xFF


def relay_byte_sql(node_id: str) -> str:
    """relay_byte_of in SQL, for the node number expression node_id."""
    return f"coalesce(nullif({node_id} % 256, 0), 255)"
