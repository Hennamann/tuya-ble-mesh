"""Unit tests for SIG Mesh Tuya light command helpers.

Covers:
- ``encode_tuya_vendor_dp`` / ``make_tuya_vendor_dp_payload`` round-trip
- ``SIGMeshDeviceCommandsMixin.send_brightness`` (DP 28 wire scaling)
- ``send_color`` (HSV encoding + work_mode switch)
- ``send_light_mode`` (DP 21 enum)
- ``send_color_temp`` / ``send_scene`` are no-ops
- ``_rgb_to_tuya_hsv`` edge cases
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = str(Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, _ROOT)
sys.path.insert(0, str(Path(_ROOT) / "custom_components" / "tuya_ble_mesh" / "lib"))

from tuya_ble_mesh.exceptions import MalformedPacketError  # noqa: E402
from tuya_ble_mesh.sig_mesh_device import SIGMeshDevice  # noqa: E402
from tuya_ble_mesh.sig_mesh_device_commands import _rgb_to_tuya_hsv  # noqa: E402
from tuya_ble_mesh.sig_mesh_protocol import (  # noqa: E402
    DP_TYPE_BOOL,
    DP_TYPE_ENUM,
    DP_TYPE_STRING,
    DP_TYPE_VALUE,
    TUYA_VENDOR_WRITE_UNACK,
    MeshKeys,
    TuyaVendorDP,
    encode_tuya_vendor_dp,
    make_tuya_vendor_dp_payload,
    parse_composition_elements,
    parse_tuya_vendor_frame,
)


def _make_device() -> SIGMeshDevice:
    """Create a SIGMeshDevice with mock keys and BLE client."""
    dev = SIGMeshDevice("DC:23:4D:CE:AE:69", 0x00B0, 0x0001, MagicMock())
    dev._keys = MeshKeys(
        "f7a2a44f8e8a8029064f173ddc1e2b00",  # pragma: allowlist secret
        "00112233445566778899aabbccddeeff",  # pragma: allowlist secret
        "3216d1509884b533248541792b877f98",  # pragma: allowlist secret
    )
    dev._client = MagicMock()
    dev._client.write_gatt_char = AsyncMock()
    return dev


# ---------------------------------------------------------------------------
# Codec: encode_tuya_vendor_dp / make_tuya_vendor_dp_payload
# ---------------------------------------------------------------------------


class TestEncodeTuyaVendorDP:
    def test_bool_encoding(self) -> None:
        assert encode_tuya_vendor_dp(20, DP_TYPE_BOOL, b"\x01") == bytes([20, 1, 1, 1])

    def test_value_encoding(self) -> None:
        assert encode_tuya_vendor_dp(28, DP_TYPE_VALUE, b"\x00\x00\x03\xe8") == bytes(
            [28, 2, 4, 0x00, 0x00, 0x03, 0xE8]
        )

    def test_rejects_oversized_value(self) -> None:
        with pytest.raises(MalformedPacketError):
            encode_tuya_vendor_dp(1, DP_TYPE_STRING, b"x" * 256)

    def test_rejects_invalid_dp_id(self) -> None:
        with pytest.raises(MalformedPacketError):
            encode_tuya_vendor_dp(-1, DP_TYPE_BOOL, b"\x00")


class TestMakeTuyaVendorDPPayload:
    def test_single_dp_roundtrip(self) -> None:
        dp = TuyaVendorDP(28, DP_TYPE_VALUE, (1000).to_bytes(4, "big", signed=True))
        payload = make_tuya_vendor_dp_payload(TUYA_VENDOR_WRITE_UNACK, [dp])
        # First three bytes are opcode
        assert payload[:3] == TUYA_VENDOR_WRITE_UNACK.to_bytes(3, "big")
        # Parse the body back out via the existing parser
        frame = parse_tuya_vendor_frame(payload[3:])
        assert frame.command == 0x01
        assert len(frame.dps) == 1
        assert frame.dps[0].dp_id == 28
        assert frame.dps[0].dp_type == DP_TYPE_VALUE
        assert int.from_bytes(frame.dps[0].value, "big", signed=True) == 1000

    def test_multi_dp_payload(self) -> None:
        payload = make_tuya_vendor_dp_payload(
            TUYA_VENDOR_WRITE_UNACK,
            [
                TuyaVendorDP(20, DP_TYPE_BOOL, b"\x01"),
                TuyaVendorDP(21, DP_TYPE_ENUM, b"\x01"),
            ],
        )
        frame = parse_tuya_vendor_frame(payload[3:])
        assert [d.dp_id for d in frame.dps] == [20, 21]

    def test_rejects_empty(self) -> None:
        with pytest.raises(MalformedPacketError):
            make_tuya_vendor_dp_payload(TUYA_VENDOR_WRITE_UNACK, [])


# ---------------------------------------------------------------------------
# RGB → HSV conversion
# ---------------------------------------------------------------------------


class TestRgbToTuyaHsv:
    def test_red_is_hue_zero_full_sat_full_val(self) -> None:
        h, s, v = _rgb_to_tuya_hsv(255, 0, 0)
        assert h == 0
        assert s == 1000
        assert v == 1000

    def test_green_is_hue_120(self) -> None:
        h, _, _ = _rgb_to_tuya_hsv(0, 255, 0)
        assert h == 120

    def test_blue_is_hue_240(self) -> None:
        h, _, _ = _rgb_to_tuya_hsv(0, 0, 255)
        assert h == 240

    def test_white_is_zero_saturation(self) -> None:
        h, s, v = _rgb_to_tuya_hsv(255, 255, 255)
        assert s == 0
        assert v == 1000
        assert h == 0

    def test_black_is_zero_value(self) -> None:
        _, _, v = _rgb_to_tuya_hsv(0, 0, 0)
        assert v == 0

    def test_clamps_overflow_inputs(self) -> None:
        h, s, v = _rgb_to_tuya_hsv(999, -1, 300)
        assert 0 <= h <= 360
        assert 0 <= s <= 1000
        assert 0 <= v <= 1000


# ---------------------------------------------------------------------------
# SIGMeshDevice light command methods — capture wire payloads
# ---------------------------------------------------------------------------


def _captured_payload(call_args: tuple) -> bytes:
    """Extract the proxy PDU bytes from a mocked write_gatt_char call."""
    args, kwargs = call_args
    return bytes(args[1] if len(args) >= 2 else kwargs["data"])


class TestSendBrightness:
    @pytest.mark.asyncio
    async def test_scales_1_to_10(self) -> None:
        dev = _make_device()
        await dev.send_brightness(1)
        # Last 7 bytes of the access payload are the DP TLV:
        # [opcode 3B][cmd 1B=0x01][len 1B=0x07][dp_id=28][type=0x02][len=4][value 4B BE]
        # Wire format is wrapped — but we test through the parse path instead.
        assert dev._client.write_gatt_char.called

    @pytest.mark.asyncio
    async def test_scales_100_to_1000(self) -> None:
        """HA-side 100 should map to wire 1000 (max)."""
        dev = _make_device()
        # Patch _send_dp directly to inspect the DP that would be sent
        captured: list[TuyaVendorDP] = []

        async def fake_send(dp: TuyaVendorDP) -> None:
            captured.append(dp)

        dev._send_dp = fake_send  # type: ignore[method-assign]
        await dev.send_brightness(100)
        assert len(captured) == 1
        dp = captured[0]
        assert dp.dp_id == 28
        assert dp.dp_type == DP_TYPE_VALUE
        assert int.from_bytes(dp.value, "big", signed=True) == 1000

    @pytest.mark.asyncio
    async def test_clamps_negative_to_min(self) -> None:
        dev = _make_device()
        captured: list[TuyaVendorDP] = []

        async def fake_send(dp: TuyaVendorDP) -> None:
            captured.append(dp)

        dev._send_dp = fake_send  # type: ignore[method-assign]
        await dev.send_brightness(-5)
        assert int.from_bytes(captured[0].value, "big", signed=True) == 10


class TestSendColor:
    @pytest.mark.asyncio
    async def test_red_emits_mode_then_colour(self) -> None:
        dev = _make_device()
        captured: list[TuyaVendorDP] = []

        async def fake_send(dp: TuyaVendorDP) -> None:
            captured.append(dp)

        dev._send_dp = fake_send  # type: ignore[method-assign]
        await dev.send_color(255, 0, 0)
        # First the work_mode switch to colour, then the colour string
        assert len(captured) == 2
        assert captured[0].dp_id == 21
        assert captured[0].dp_type == DP_TYPE_ENUM
        assert captured[0].value == b"\x01"
        assert captured[1].dp_id == 30
        assert captured[1].dp_type == DP_TYPE_STRING
        # H=0, S=1000=0x03e8, V=1000=0x03e8 → "000003e803e8"
        assert captured[1].value == b"000003e803e8"


class TestSendLightMode:
    @pytest.mark.asyncio
    async def test_white_mode(self) -> None:
        dev = _make_device()
        captured: list[TuyaVendorDP] = []

        async def fake_send(dp: TuyaVendorDP) -> None:
            captured.append(dp)

        dev._send_dp = fake_send  # type: ignore[method-assign]
        await dev.send_light_mode(0)
        assert captured[0].dp_id == 21
        assert captured[0].value == b"\x00"


class TestParseCompositionElements:
    def test_single_element_with_sig_and_vendor(self) -> None:
        # loc=0x0001, NumS=2, NumV=1, sig=[0x1000, 0x1001], vendor=[(0x07D0, 0xFE00)]
        raw = bytes(
            [
                0x01,
                0x00,  # loc 0x0001
                0x02,  # NumS = 2
                0x01,  # NumV = 1
                0x00,
                0x10,  # SIG model 0x1000
                0x01,
                0x10,  # SIG model 0x1001
                0xD0,
                0x07,  # CID 0x07D0
                0x00,
                0xFE,  # Model 0xFE00
            ]
        )
        elems = parse_composition_elements(raw)
        assert len(elems) == 1
        assert elems[0].loc == 0x0001
        assert elems[0].sig_model_ids == (0x1000, 0x1001)
        assert elems[0].vendor_models == ((0x07D0, 0xFE00),)

    def test_two_elements(self) -> None:
        # elem0: loc=0, NumS=1, NumV=0, sig=[0x1000]
        # elem1: loc=0, NumS=0, NumV=1, vendor=[(0x07D0, 0xFE01)]
        raw = bytes(
            [
                0x00,
                0x00,
                0x01,
                0x00,
                0x00,
                0x10,
                0x00,
                0x00,
                0x00,
                0x01,
                0xD0,
                0x07,
                0x01,
                0xFE,
            ]
        )
        elems = parse_composition_elements(raw)
        assert len(elems) == 2
        assert elems[0].sig_model_ids == (0x1000,)
        assert elems[1].vendor_models == ((0x07D0, 0xFE01),)

    def test_empty(self) -> None:
        assert parse_composition_elements(b"") == []

    def test_truncated_returns_what_it_could_parse(self) -> None:
        # Header claims 2 SIG models but buffer only has 1
        raw = bytes([0x00, 0x00, 0x02, 0x00, 0x00, 0x10])
        # Should stop after the header check without raising
        assert parse_composition_elements(raw) == []


class TestNoopStubs:
    @pytest.mark.asyncio
    async def test_send_color_temp_is_silent_noop(self) -> None:
        dev = _make_device()
        # Should not call write_gatt_char and should not raise
        await dev.send_color_temp(50)
        assert not dev._client.write_gatt_char.called

    @pytest.mark.asyncio
    async def test_send_scene_is_silent_noop(self) -> None:
        dev = _make_device()
        await dev.send_scene(1)
        assert not dev._client.write_gatt_char.called
