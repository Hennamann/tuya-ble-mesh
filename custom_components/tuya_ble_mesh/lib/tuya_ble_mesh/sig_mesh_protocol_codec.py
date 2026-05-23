"""SIG Mesh protocol codec — packet encoding/decoding.

Pure encoding and decoding of SIG Mesh packets, config model messages,
access layer opcodes, Tuya vendor frames, composition data, and proxy PDUs.

No cryptographic operations — those live in ``sig_mesh_protocol.py``.

SECURITY: Key material is NEVER logged, printed, or included in
exception messages. Only lengths and opcodes are safe to log.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

from tuya_ble_mesh.exceptions import MalformedPacketError, ProtocolError

_LOGGER = logging.getLogger(__name__)

# --- Proxy PDU constants ---
PROXY_SAR_COMPLETE = 0x00
PROXY_TYPE_NETWORK = 0x00

# --- Transport constants ---
MAX_UNSEG_ACCESS_PAYLOAD = 11  # 15 byte upper transport - 4 byte TransMIC
SEG_DATA_SIZE = 12  # max bytes per segment chunk

# --- Network PDU field masks and lengths ---
MESH_NID_MASK = 0x7F
MESH_IVI_SHIFT = 7
MESH_TTL_MASK = 0x7F
MESH_CTL_SHIFT = 7
MIC_LEN_ACCESS = 4
MIC_LEN_CONTROL = 8

# --- Lower transport header masks ---
MESH_SEG_BIT = 0x80
MESH_AKF_SHIFT = 6
MESH_AID_MASK = 0x3F

# --- Segmented transport header bit positions (24-bit info field) ---
MESH_SZMIC_SHIFT = 23
MESH_SEQ_ZERO_SHIFT = 10
MESH_SEQ_ZERO_MASK = 0x1FFF
MESH_SEG_O_SHIFT = 5
MESH_SEG_MASK = 0x1F

# --- Proxy PDU header masks ---
PROXY_SAR_MASK = 0x03
PROXY_TYPE_MASK = 0x3F

# --- Access layer opcode format bits (Mesh Profile 3.7.3) ---
MESH_OPCODE_1BYTE_MASK = 0x80
MESH_OPCODE_2BYTE_MASK = 0xC0
MESH_OPCODE_2BYTE_VALUE = 0x80

# --- Config model opcodes (SIG Mesh Profile 4.3) ---
OP_CONFIG_COMPOSITION_GET = 0x8008
OP_CONFIG_COMPOSITION_STATUS = 0x02
OP_CONFIG_APPKEY_ADD = 0x0000
OP_CONFIG_APPKEY_STATUS = 0x8003
OP_CONFIG_MODEL_APP_BIND = 0x803D
OP_CONFIG_MODEL_APP_STATUS = 0x803E

# --- Generic OnOff model opcodes (Mesh Model 3.2) ---
OP_GENERIC_ONOFF_GET = 0x8201
OP_GENERIC_ONOFF_SET = 0x8202
OP_GENERIC_ONOFF_STATUS = 0x8204

# --- Light Lightness model opcodes (Mesh Model 6.3) ---
OP_LIGHT_LIGHTNESS_GET = 0x824B
OP_LIGHT_LIGHTNESS_SET = 0x824C
OP_LIGHT_LIGHTNESS_SET_UNACK = 0x824D
OP_LIGHT_LIGHTNESS_STATUS = 0x824E

# --- Light CTL model opcodes (Mesh Model 6.4) ---
OP_LIGHT_CTL_GET = 0x825D
OP_LIGHT_CTL_SET = 0x825E
OP_LIGHT_CTL_SET_UNACK = 0x825F
OP_LIGHT_CTL_STATUS = 0x8260

# --- Light HSL model opcodes (Mesh Model 6.5) ---
OP_LIGHT_HSL_GET = 0x826D
OP_LIGHT_HSL_HUE_SET = 0x826F
OP_LIGHT_HSL_HUE_SET_UNACK = 0x8270
OP_LIGHT_HSL_HUE_STATUS = 0x8271
OP_LIGHT_HSL_SATURATION_SET = 0x8273
OP_LIGHT_HSL_SATURATION_SET_UNACK = 0x8274
OP_LIGHT_HSL_SATURATION_STATUS = 0x8275
OP_LIGHT_HSL_SET = 0x8276
OP_LIGHT_HSL_SET_UNACK = 0x8277
OP_LIGHT_HSL_STATUS = 0x8278

# --- SIG Mesh Light model IDs ---
MODEL_GENERIC_ONOFF_SERVER = 0x1000
MODEL_LIGHT_LIGHTNESS_SERVER = 0x1300
MODEL_LIGHT_LIGHTNESS_SETUP_SERVER = 0x1301
MODEL_LIGHT_CTL_SERVER = 0x1303
MODEL_LIGHT_HSL_SERVER = 0x1307

# --- Tuya Vendor Model (CID 0x07D0) ---
TUYA_CID = 0x07D0
TUYA_VENDOR_OPCODE = 0xCDD007
TUYA_VENDOR_WRITE_ACK = 0xC9D007
TUYA_VENDOR_WRITE_UNACK = 0xCAD007
TUYA_CMD_DP_DATA = 0x01
TUYA_CMD_TIMESTAMP_SYNC = 0x02
DP_ID_SWITCH = 1
DP_ID_ENERGY_KWH = 17
DP_ID_POWER_W = 18
DP_ID_CURRENT_MA = 19
DP_ID_VOLTAGE_V = 20

# Tuya category-dj (lighting) DP IDs — v2 function set
DP_ID_LIGHT_SWITCH = 20       # switch_led — bool
DP_ID_LIGHT_MODE = 21         # work_mode — enum
DP_ID_LIGHT_BRIGHTNESS = 28   # bright_value_v2 — value (10..1000)
DP_ID_LIGHT_COLOUR = 30       # colour_data_v2 — string "HHHHSSSSVVVV"

# Tuya DP type byte values (per Tuya standard)
DP_TYPE_RAW = 0x00
DP_TYPE_BOOL = 0x01
DP_TYPE_VALUE = 0x02   # 4-byte big-endian signed int
DP_TYPE_STRING = 0x03
DP_TYPE_ENUM = 0x04    # 1-byte enum index
DP_TYPE_BITMAP = 0x05

# Internal alias used by segments module
_OPCODE_COMPOSITION_STATUS = OP_CONFIG_COMPOSITION_STATUS


# ============================================================
# Segment Header Parsing (Mesh Profile 3.5.2.2)
# ============================================================


@dataclass(frozen=True)
class SegmentHeader:
    """Parsed segmented access message header fields."""

    akf: int
    aid: int
    szmic: int
    seq_zero: int
    seg_o: int
    seg_n: int
    segment_data: bytes


def parse_segment_header(transport_pdu: bytes) -> SegmentHeader:
    """Parse a segmented access message lower transport PDU header."""
    if len(transport_pdu) < 4:
        msg = f"Segmented transport PDU too short: {len(transport_pdu)} bytes"
        raise MalformedPacketError(msg)

    hdr = transport_pdu[0]
    if not (hdr & MESH_SEG_BIT):
        msg = "Not a segmented PDU (SEG bit not set)"
        raise MalformedPacketError(msg)

    akf = (hdr >> MESH_AKF_SHIFT) & 1
    aid = hdr & MESH_AID_MASK
    info = (transport_pdu[1] << 16) | (transport_pdu[2] << 8) | transport_pdu[3]

    return SegmentHeader(
        akf=akf,
        aid=aid,
        szmic=(info >> MESH_SZMIC_SHIFT) & 1,
        seq_zero=(info >> MESH_SEQ_ZERO_SHIFT) & MESH_SEQ_ZERO_MASK,
        seg_o=(info >> MESH_SEG_O_SHIFT) & MESH_SEG_MASK,
        seg_n=info & MESH_SEG_MASK,
        segment_data=transport_pdu[4:],
    )


# ============================================================
# Proxy PDU (Mesh Profile 6.3)
# ============================================================


def make_proxy_pdu(network_pdu: bytes) -> bytes:
    """Wrap a network PDU in a Mesh Proxy PDU (SAR=complete, type=network)."""
    return bytes([(PROXY_SAR_COMPLETE << 6) | PROXY_TYPE_NETWORK]) + network_pdu


@dataclass(frozen=True)
class ProxyPDU:
    """Parsed Mesh Proxy PDU."""

    sar: int
    pdu_type: int
    payload: bytes


def parse_proxy_pdu(data: bytes) -> ProxyPDU:
    """Parse a Mesh Proxy PDU from GATT characteristic bytes."""
    if not data:
        msg = "Empty proxy PDU"
        raise MalformedPacketError(msg)
    return ProxyPDU(
        sar=(data[0] >> 6) & PROXY_SAR_MASK,
        pdu_type=data[0] & PROXY_TYPE_MASK,
        payload=data[1:],
    )


# ============================================================
# Config Model Messages (Mesh Profile 4.3)
# ============================================================


def config_composition_get(page: int = 0) -> bytes:
    """Config Composition Data Get (opcode 0x8008)."""
    if not 0 <= page <= 0xFF:
        msg = f"Page must be 0..255, got {page}"
        raise ProtocolError(msg)
    return struct.pack(">H", OP_CONFIG_COMPOSITION_GET) + bytes([page])


def config_appkey_add(net_idx: int, app_idx: int, app_key: bytes) -> bytes:
    """Config AppKey Add (opcode 0x00). 20-byte payload — requires segmented transport."""
    if not 0 <= net_idx <= 0xFFF:
        msg = f"net_idx must be 0..0xFFF, got {net_idx}"
        raise ProtocolError(msg)
    if not 0 <= app_idx <= 0xFFF:
        msg = f"app_idx must be 0..0xFFF, got {app_idx}"
        raise ProtocolError(msg)
    if len(app_key) != 16:
        msg = f"app_key must be 16 bytes, got {len(app_key)}"
        raise ProtocolError(msg)
    idx = (net_idx & 0xFFF) | ((app_idx & 0xFFF) << 12)
    return bytes([OP_CONFIG_APPKEY_ADD]) + struct.pack("<I", idx)[:3] + app_key


def config_model_app_bind(element_addr: int, app_idx: int, model_id: int) -> bytes:
    """Config Model App Bind (opcode 0x803D). SIG Model IDs only (16-bit)."""
    if not 0 <= element_addr <= 0xFFFF:
        msg = f"element_addr must be 0..0xFFFF, got {element_addr}"
        raise ProtocolError(msg)
    if not 0 <= app_idx <= 0xFFF:
        msg = f"app_idx must be 0..0xFFF, got {app_idx}"
        raise ProtocolError(msg)
    if not 0 <= model_id <= 0xFFFF:
        msg = f"model_id must be 0..0xFFFF, got {model_id}"
        raise ProtocolError(msg)
    return struct.pack(">H", OP_CONFIG_MODEL_APP_BIND) + struct.pack(
        "<HHH", element_addr, app_idx, model_id
    )


def config_model_app_bind_vendor(
    element_addr: int, app_idx: int, cid: int, model_id: int
) -> bytes:
    """Config Model App Bind for a vendor model (opcode 0x803D).

    Vendor models use a 4-byte model identifier: ``[cid 2B][model_id 2B]``
    appended after the element and app indexes. Total access payload is
    8 bytes, which requires segmented transport.

    Args:
        element_addr: Element unicast address.
        app_idx: Application key index (0..0xFFF).
        cid: Company identifier (e.g. ``0x07D0`` for Tuya).
        model_id: Vendor-defined model identifier within the CID.
    """
    if not 0 <= element_addr <= 0xFFFF:
        msg = f"element_addr must be 0..0xFFFF, got {element_addr}"
        raise ProtocolError(msg)
    if not 0 <= app_idx <= 0xFFF:
        msg = f"app_idx must be 0..0xFFF, got {app_idx}"
        raise ProtocolError(msg)
    if not 0 <= cid <= 0xFFFF:
        msg = f"cid must be 0..0xFFFF, got {cid}"
        raise ProtocolError(msg)
    if not 0 <= model_id <= 0xFFFF:
        msg = f"model_id must be 0..0xFFFF, got {model_id}"
        raise ProtocolError(msg)
    return struct.pack(">H", OP_CONFIG_MODEL_APP_BIND) + struct.pack(
        "<HHHH", element_addr, app_idx, cid, model_id
    )


# ============================================================
# Generic OnOff Model Messages (Mesh Model 3.2)
# ============================================================


def generic_onoff_set(on: bool, tid: int = 0) -> bytes:
    """Generic OnOff Set (opcode 0x8202)."""
    return struct.pack(">H", OP_GENERIC_ONOFF_SET) + bytes([0x01 if on else 0x00, tid & 0xFF])


def generic_onoff_get() -> bytes:
    """Generic OnOff Get (opcode 0x8201)."""
    return struct.pack(">H", OP_GENERIC_ONOFF_GET)


# ============================================================
# Light Lightness Model Messages (Mesh Model 6.3)
# ============================================================


def light_lightness_set_unack(lightness: int, tid: int = 0) -> bytes:
    """Light Lightness Set Unacknowledged (opcode 0x824D).

    Args:
        lightness: 16-bit unsigned lightness value (0..65535).
        tid: Transaction identifier.
    """
    if not 0 <= lightness <= 0xFFFF:
        msg = f"lightness must be 0..65535, got {lightness}"
        raise ProtocolError(msg)
    return struct.pack(">H", OP_LIGHT_LIGHTNESS_SET_UNACK) + struct.pack(
        "<HB", lightness, tid & 0xFF
    )


def light_lightness_set(lightness: int, tid: int = 0) -> bytes:
    """Light Lightness Set (opcode 0x824C, acknowledged)."""
    if not 0 <= lightness <= 0xFFFF:
        msg = f"lightness must be 0..65535, got {lightness}"
        raise ProtocolError(msg)
    return struct.pack(">H", OP_LIGHT_LIGHTNESS_SET) + struct.pack(
        "<HB", lightness, tid & 0xFF
    )


def light_lightness_get() -> bytes:
    """Light Lightness Get (opcode 0x824B)."""
    return struct.pack(">H", OP_LIGHT_LIGHTNESS_GET)


def parse_light_lightness_status(params: bytes) -> int:
    """Parse a Light Lightness Status message body.

    The status format is::

        Present Lightness   2 bytes uint16 little-endian
        [Target Lightness   2 bytes uint16 little-endian, optional]
        [Remaining Time     1 byte, optional, present iff Target present]

    Returns the Present Lightness (0..65535). Optional fields are ignored.

    Raises:
        MalformedPacketError: If the buffer is shorter than 2 bytes.
    """
    if len(params) < 2:
        msg = f"Light Lightness Status too short: {len(params)} bytes"
        raise MalformedPacketError(msg)
    return struct.unpack_from("<H", params, 0)[0]


def parse_light_hsl_status(params: bytes) -> tuple[int, int, int]:
    """Parse a Light HSL Status message body.

    The status format is::

        HSL Lightness   2 bytes uint16 little-endian
        HSL Hue         2 bytes uint16 little-endian
        HSL Saturation  2 bytes uint16 little-endian
        [Remaining Time 1 byte, optional]

    Returns ``(lightness, hue, saturation)`` each 0..65535.

    Raises:
        MalformedPacketError: If the buffer is shorter than 6 bytes.
    """
    if len(params) < 6:
        msg = f"Light HSL Status too short: {len(params)} bytes"
        raise MalformedPacketError(msg)
    return (
        struct.unpack_from("<H", params, 0)[0],
        struct.unpack_from("<H", params, 2)[0],
        struct.unpack_from("<H", params, 4)[0],
    )


# ============================================================
# Light HSL Model Messages (Mesh Model 6.5)
# ============================================================


def light_hsl_set_unack(lightness: int, hue: int, saturation: int, tid: int = 0) -> bytes:
    """Light HSL Set Unacknowledged (opcode 0x8277).

    Args:
        lightness: 16-bit HSL Lightness (0..65535).
        hue: 16-bit HSL Hue (0..65535 → 0..360°).
        saturation: 16-bit HSL Saturation (0..65535 → 0..100%).
        tid: Transaction identifier.
    """
    for name, value in (("lightness", lightness), ("hue", hue), ("saturation", saturation)):
        if not 0 <= value <= 0xFFFF:
            msg = f"{name} must be 0..65535, got {value}"
            raise ProtocolError(msg)
    return struct.pack(">H", OP_LIGHT_HSL_SET_UNACK) + struct.pack(
        "<HHHB", lightness, hue, saturation, tid & 0xFF
    )


def light_hsl_set(lightness: int, hue: int, saturation: int, tid: int = 0) -> bytes:
    """Light HSL Set (opcode 0x8276, acknowledged)."""
    for name, value in (("lightness", lightness), ("hue", hue), ("saturation", saturation)):
        if not 0 <= value <= 0xFFFF:
            msg = f"{name} must be 0..65535, got {value}"
            raise ProtocolError(msg)
    return struct.pack(">H", OP_LIGHT_HSL_SET) + struct.pack(
        "<HHHB", lightness, hue, saturation, tid & 0xFF
    )


def light_hsl_get() -> bytes:
    """Light HSL Get (opcode 0x826D)."""
    return struct.pack(">H", OP_LIGHT_HSL_GET)


def light_hsl_hue_set_unack(hue: int, tid: int = 0) -> bytes:
    """Light HSL Hue Set Unacknowledged (opcode 0x8270).

    Args:
        hue: 16-bit HSL Hue (0..65535 → 0..360°).
        tid: Transaction identifier.
    """
    if not 0 <= hue <= 0xFFFF:
        msg = f"hue must be 0..65535, got {hue}"
        raise ProtocolError(msg)
    return struct.pack(">H", OP_LIGHT_HSL_HUE_SET_UNACK) + struct.pack("<HB", hue, tid & 0xFF)


def light_hsl_saturation_set_unack(saturation: int, tid: int = 0) -> bytes:
    """Light HSL Saturation Set Unacknowledged (opcode 0x8274).

    Args:
        saturation: 16-bit HSL Saturation (0..65535 → 0..100%).
        tid: Transaction identifier.
    """
    if not 0 <= saturation <= 0xFFFF:
        msg = f"saturation must be 0..65535, got {saturation}"
        raise ProtocolError(msg)
    return struct.pack(">H", OP_LIGHT_HSL_SATURATION_SET_UNACK) + struct.pack(
        "<HB", saturation, tid & 0xFF
    )


# ============================================================
# Light CTL Model Messages (Mesh Model 6.4)
# ============================================================


def light_ctl_set_unack(
    lightness: int, temperature: int, delta_uv: int = 0, tid: int = 0
) -> bytes:
    """Light CTL Set Unacknowledged (opcode 0x825F).

    Args:
        lightness: 16-bit CTL Lightness (0..65535).
        temperature: 16-bit CTL Temperature (0x0320..0x4E20 = 800K..20000K).
        delta_uv: 16-bit signed Delta UV (default 0).
        tid: Transaction identifier.
    """
    if not 0 <= lightness <= 0xFFFF:
        msg = f"lightness must be 0..65535, got {lightness}"
        raise ProtocolError(msg)
    if not 0 <= temperature <= 0xFFFF:
        msg = f"temperature must be 0..65535, got {temperature}"
        raise ProtocolError(msg)
    if not -0x8000 <= delta_uv <= 0x7FFF:
        msg = f"delta_uv must be int16, got {delta_uv}"
        raise ProtocolError(msg)
    return struct.pack(">H", OP_LIGHT_CTL_SET_UNACK) + struct.pack(
        "<HHhB", lightness, temperature, delta_uv, tid & 0xFF
    )


# ============================================================
# Access Layer Opcode Parsing (Mesh Profile 3.7.3)
# ============================================================


def parse_access_opcode(data: bytes) -> tuple[int, bytes]:
    """Parse a SIG Mesh access layer opcode (1, 2, or 3 bytes)."""
    if not data:
        msg = "Empty access payload"
        raise MalformedPacketError(msg)

    if data[0] & MESH_OPCODE_1BYTE_MASK == 0:
        return data[0], data[1:]
    elif data[0] & MESH_OPCODE_2BYTE_MASK == MESH_OPCODE_2BYTE_VALUE:
        if len(data) < 2:
            msg = "2-byte opcode truncated"
            raise MalformedPacketError(msg)
        return (data[0] << 8) | data[1], data[2:]
    else:
        if len(data) < 3:
            msg = "3-byte vendor opcode truncated"
            raise MalformedPacketError(msg)
        return (data[0] << 16) | (data[1] << 8) | data[2], data[3:]


# ============================================================
# Tuya Vendor Model (CID 0x07D0)
# ============================================================


@dataclass(frozen=True)
class TuyaVendorDP:
    """A single Tuya vendor Data Point from a vendor message."""

    dp_id: int
    dp_type: int
    value: bytes


@dataclass(frozen=True)
class TuyaVendorFrame:
    """Parsed Tuya vendor message frame.

    Attributes:
        command: Frame command byte (0x01=DP data, 0x02=timestamp sync, 0=unknown/raw).
        data: Raw data bytes after the frame header.
        dps: Parsed data points (populated only when command is TUYA_CMD_DP_DATA).
    """

    command: int
    data: bytes
    dps: list[TuyaVendorDP]


def parse_tuya_vendor_frame(params: bytes) -> TuyaVendorFrame:
    """Parse a Tuya vendor message with frame header ``[command 1B][data_length 1B][data NB]``."""
    if len(params) < 2:
        _LOGGER.debug("Vendor frame too short (%d bytes)", len(params))
        return TuyaVendorFrame(command=0, data=params, dps=[])

    command = params[0]
    data = params[2:]

    if command == TUYA_CMD_TIMESTAMP_SYNC:
        _LOGGER.debug("Tuya timestamp sync request (%d data bytes)", len(data))
        return TuyaVendorFrame(command=command, data=data, dps=[])

    if command == TUYA_CMD_DP_DATA:
        return TuyaVendorFrame(command=command, data=data, dps=_parse_dp_bytes(data))

    _LOGGER.debug("Unknown vendor command 0x%02X, trying raw DP parse on full params", command)
    return TuyaVendorFrame(command=command, data=params, dps=_parse_dp_bytes(params))


def tuya_vendor_timestamp_response() -> bytes:
    """Build a Tuya vendor WRITE_UNACK payload with current UTC timestamp."""
    import time

    now = int(time.time())
    opcode_bytes = TUYA_VENDOR_WRITE_UNACK.to_bytes(3, "big")
    ts_bytes = now.to_bytes(4, "big")
    tz_offset = time.timezone // -3600 if not time.daylight else time.altzone // -3600
    tz_byte = tz_offset.to_bytes(1, "big", signed=True) if -12 <= tz_offset <= 14 else b"\x00"
    data = ts_bytes + tz_byte + b"\x00\x00\x00"
    frame = bytes([TUYA_CMD_TIMESTAMP_SYNC, len(data)]) + data
    return opcode_bytes + frame


def parse_tuya_vendor_dps(params: bytes) -> list[TuyaVendorDP]:
    """Parse Tuya vendor DP values (raw TLV, no frame header)."""
    return _parse_dp_bytes(params)


def encode_tuya_vendor_dp(dp_id: int, dp_type: int, value: bytes) -> bytes:
    """Encode a single Tuya DP as ``[dp_id 1B][dp_type 1B][dp_len 1B][value]``.

    Args:
        dp_id: Tuya data point id (0..255).
        dp_type: Tuya DP type byte (``DP_TYPE_BOOL``/``DP_TYPE_VALUE``/...).
        value: Raw value bytes.

    Returns:
        Encoded TLV bytes.

    Raises:
        MalformedPacketError: If any field exceeds single-byte range.
    """
    if not 0 <= dp_id <= 0xFF:
        msg = f"dp_id out of range: {dp_id}"
        raise MalformedPacketError(msg)
    if not 0 <= dp_type <= 0xFF:
        msg = f"dp_type out of range: {dp_type}"
        raise MalformedPacketError(msg)
    if len(value) > 0xFF:
        msg = f"DP value too long ({len(value)} bytes)"
        raise MalformedPacketError(msg)
    return bytes([dp_id, dp_type, len(value)]) + value


def make_tuya_vendor_dp_payload(
    opcode: int,
    dps: list[TuyaVendorDP],
) -> bytes:
    """Build a full Tuya vendor access payload carrying one or more DPs.

    Wire format::

        [opcode 3B big-endian]
        [TUYA_CMD_DP_DATA = 0x01][total_dp_bytes_len 1B]
        [dp_id 1B][dp_type 1B][dp_len 1B][value]...

    Args:
        opcode: Vendor opcode (``TUYA_VENDOR_WRITE_UNACK`` or
            ``TUYA_VENDOR_WRITE_ACK``).
        dps: One or more DPs to include.

    Returns:
        Bytes suitable to pass to ``SIGMeshDevice.send_vendor_command``.

    Raises:
        MalformedPacketError: If the encoded DP block exceeds 0xFF bytes
            (unsegmented payload limit) or no DPs are supplied.
    """
    if not dps:
        msg = "make_tuya_vendor_dp_payload requires at least one DP"
        raise MalformedPacketError(msg)

    dp_bytes = b"".join(encode_tuya_vendor_dp(d.dp_id, d.dp_type, d.value) for d in dps)
    if len(dp_bytes) > 0xFF:
        msg = f"Total DP block exceeds 255 bytes ({len(dp_bytes)})"
        raise MalformedPacketError(msg)

    opcode_bytes = opcode.to_bytes(3, "big")
    frame = bytes([TUYA_CMD_DP_DATA, len(dp_bytes)]) + dp_bytes
    return opcode_bytes + frame


def _parse_dp_bytes(data: bytes) -> list[TuyaVendorDP]:
    """Parse raw DP bytes: ``[dp_id 1B][dp_type 1B][dp_len 1B][value NB]...``"""
    dps: list[TuyaVendorDP] = []
    offset = 0
    while offset < len(data):
        if offset + 3 > len(data):
            _LOGGER.debug("Truncated DP header at offset %d", offset)
            break
        dp_id = data[offset]
        dp_type = data[offset + 1]
        dp_len = data[offset + 2]
        offset += 3
        if offset + dp_len > len(data):
            _LOGGER.debug(
                "Truncated DP value: dp_id=%d, need %d bytes, have %d",
                dp_id,
                dp_len,
                len(data) - offset,
            )
            break
        dps.append(TuyaVendorDP(dp_id=dp_id, dp_type=dp_type, value=data[offset : offset + dp_len]))
        offset += dp_len
    return dps


# ============================================================
# Composition Data (Mesh Profile 4.2.1)
# ============================================================


@dataclass(frozen=True)
class CompositionData:
    """Parsed Composition Data Page 0 header."""

    cid: int  # Company ID
    pid: int  # Product ID
    vid: int  # Version ID
    crpl: int  # Replay protection list size
    features: int  # Features bitmask
    raw_elements: bytes  # Unparsed element data


def parse_composition_data(params: bytes) -> CompositionData:
    """Parse Composition Data Status page 0 parameters."""
    if len(params) < 11:
        msg = f"Composition Data too short: {len(params)} bytes (need >= 11)"
        raise MalformedPacketError(msg)

    data = params[1:]  # Skip page byte
    return CompositionData(
        cid=struct.unpack_from("<H", data, 0)[0],
        pid=struct.unpack_from("<H", data, 2)[0],
        vid=struct.unpack_from("<H", data, 4)[0],
        crpl=struct.unpack_from("<H", data, 6)[0],
        features=struct.unpack_from("<H", data, 8)[0],
        raw_elements=data[10:],
    )


@dataclass(frozen=True)
class ElementInfo:
    """One element parsed from Composition Data Page 0.

    Per Bluetooth Mesh Profile 4.2.1.4: each element advertises its location
    descriptor plus the SIG and vendor model identifiers it implements.
    """

    loc: int
    sig_model_ids: tuple[int, ...]
    vendor_models: tuple[tuple[int, int], ...]  # list of (cid, model_id)


def parse_composition_elements(raw_elements: bytes) -> list[ElementInfo]:
    """Parse the element list inside Composition Data Page 0 raw_elements.

    Each element header is 4 bytes (loc 2B, NumS 1B, NumV 1B), followed by
    NumS * 2 bytes of SIG model IDs and NumV * 4 bytes of vendor model IDs
    (each as cid 2B + model 2B, little-endian).

    Returns an empty list rather than raising if the buffer is malformed —
    the caller can still fall back to defaults.
    """
    elements: list[ElementInfo] = []
    offset = 0
    while offset + 4 <= len(raw_elements):
        loc = struct.unpack_from("<H", raw_elements, offset)[0]
        num_s = raw_elements[offset + 2]
        num_v = raw_elements[offset + 3]
        offset += 4
        if offset + num_s * 2 + num_v * 4 > len(raw_elements):
            break
        sig_ids: list[int] = []
        for _ in range(num_s):
            sig_ids.append(struct.unpack_from("<H", raw_elements, offset)[0])
            offset += 2
        vendor_models: list[tuple[int, int]] = []
        for _ in range(num_v):
            cid = struct.unpack_from("<H", raw_elements, offset)[0]
            model_id = struct.unpack_from("<H", raw_elements, offset + 2)[0]
            vendor_models.append((cid, model_id))
            offset += 4
        elements.append(
            ElementInfo(loc=loc, sig_model_ids=tuple(sig_ids), vendor_models=tuple(vendor_models))
        )
    return elements


# ============================================================
# Status Response Formatting
# ============================================================

_CONFIG_STATUS_NAMES: dict[int, str] = {
    0x00: "Success",
    0x01: "InvalidAddress",
    0x02: "InvalidModel",
    0x03: "InvalidAppKeyIndex",
    0x04: "InvalidNetKeyIndex",
    0x05: "InsufficientResources",
    0x06: "KeyIndexAlreadyStored",
}


def format_status_response(opcode: int, params: bytes) -> str:
    """Format a mesh status response for human-readable display."""
    if opcode == OP_CONFIG_APPKEY_STATUS:
        status = params[0] if params else 0xFF
        return f"AppKey Status: {_CONFIG_STATUS_NAMES.get(status, f'Unknown(0x{status:02X})')}"

    if opcode == OP_CONFIG_MODEL_APP_STATUS:
        status = params[0] if params else 0xFF
        bind_status: dict[int, str] = {
            0x00: "Success",
            0x02: "InvalidModel",
            0x03: "InvalidAppKeyIndex",
            0x04: "InvalidNetKeyIndex",
            0x06: "ModelAppAlreadyBound",
        }
        return f"Model App Status: {bind_status.get(status, f'Unknown(0x{status:02X})')}"

    if opcode == OP_CONFIG_COMPOSITION_STATUS:
        page = params[0] if params else 0xFF
        return f"Composition Data: page={page} ({len(params) - 1} bytes)"

    if opcode == OP_GENERIC_ONOFF_STATUS:
        state = params[0] if params else 0xFF
        msg = f"OnOff Status: {'ON' if state else 'OFF'}"
        if len(params) >= 3:
            msg += f" (target={'ON' if params[1] else 'OFF'}, remaining={params[2]})"
        return msg

    return f"Opcode 0x{opcode:04X}: {len(params)} bytes"
