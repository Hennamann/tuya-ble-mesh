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
    OP_LIGHT_HSL_SET_UNACK,
    OP_LIGHT_LIGHTNESS_SET_UNACK,
    TUYA_VENDOR_WRITE_UNACK,
    MeshKeys,
    TuyaVendorDP,
    encode_tuya_vendor_dp,
    light_hsl_set_unack,
    light_lightness_set_unack,
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


class TestLightnessCodec:
    def test_set_unack_max_value(self) -> None:
        payload = light_lightness_set_unack(0xFFFF, 0)
        # opcode 2B (big-endian) + lightness 2B (little-endian) + tid 1B
        assert payload[:2] == bytes([0x82, OP_LIGHT_LIGHTNESS_SET_UNACK & 0xFF])
        assert payload[2:4] == bytes([0xFF, 0xFF])
        assert payload[4] == 0x00

    def test_set_unack_rejects_out_of_range(self) -> None:
        from tuya_ble_mesh.exceptions import ProtocolError

        with pytest.raises(ProtocolError):
            light_lightness_set_unack(70000, 0)


class TestHSLCodec:
    def test_set_unack_field_order(self) -> None:
        # lightness=0x1234 hue=0x5678 sat=0x9ABC tid=0x42
        payload = light_hsl_set_unack(0x1234, 0x5678, 0x9ABC, 0x42)
        assert payload[:2] == bytes([0x82, OP_LIGHT_HSL_SET_UNACK & 0xFF])
        # Little-endian: lightness, hue, saturation, then tid
        assert payload[2:4] == bytes([0x34, 0x12])
        assert payload[4:6] == bytes([0x78, 0x56])
        assert payload[6:8] == bytes([0xBC, 0x9A])
        assert payload[8] == 0x42


class TestSendBrightness:
    @pytest.mark.asyncio
    async def test_sends_lightness_set_unack(self) -> None:
        """send_brightness should emit a Light Lightness Set Unacknowledged."""
        dev = _make_device()
        captured: list[bytes] = []

        async def fake_send(payload: bytes) -> None:
            captured.append(payload)

        dev.send_vendor_command = fake_send  # type: ignore[method-assign]
        await dev.send_brightness(100)
        assert len(captured) == 1
        # Opcode 0x824D big-endian at start
        assert captured[0][:2] == bytes([0x82, 0x4D])
        # Lightness little-endian at offset 2..4 should be ~ 0xFFFF (max)
        lightness = int.from_bytes(captured[0][2:4], "little")
        assert lightness == 0xFFFF

    @pytest.mark.asyncio
    async def test_scales_50_percent_to_half(self) -> None:
        dev = _make_device()
        captured: list[bytes] = []

        async def fake_send(payload: bytes) -> None:
            captured.append(payload)

        dev.send_vendor_command = fake_send  # type: ignore[method-assign]
        await dev.send_brightness(50)
        lightness = int.from_bytes(captured[0][2:4], "little")
        # 50/100 * 65535 ≈ 32768
        assert 30000 < lightness < 35000

    @pytest.mark.asyncio
    async def test_clamps_negative_to_min(self) -> None:
        dev = _make_device()
        captured: list[bytes] = []

        async def fake_send(payload: bytes) -> None:
            captured.append(payload)

        dev.send_vendor_command = fake_send  # type: ignore[method-assign]
        await dev.send_brightness(-5)
        lightness = int.from_bytes(captured[0][2:4], "little")
        # Clamps level to 1 → 1 * 65535 / 100 ≈ 655
        assert lightness > 0


class TestSendColor:
    @pytest.mark.asyncio
    async def test_red_emits_workmode_hsl_then_tuya_colour_dps(self) -> None:
        """send_color emits work_mode, Hue, Sat, HSL Set, then RAW and STRING Tuya DPs."""
        dev = _make_device()
        captured: list[bytes] = []

        async def fake_send(payload: bytes) -> None:
            captured.append(payload)

        dev.send_vendor_command = fake_send  # type: ignore[method-assign]
        await dev.send_color(255, 0, 0)
        # 5 Tuya DP writes: switch_led, work_mode×2 (enum+string), colour_data×2
        assert len(captured) == 5
        # All five carry the Tuya vendor WRITE_UNACK opcode 0xCAD007
        for tuya_msg in captured:
            assert tuya_msg[:3] == bytes([0xCA, 0xD0, 0x07])


class TestSendLightMode:
    @pytest.mark.asyncio
    async def test_is_noop(self) -> None:
        """send_light_mode is implicit via HSL/Lightness writes; the method
        should not call write_gatt_char."""
        dev = _make_device()
        await dev.send_light_mode(0)
        assert not dev._client.write_gatt_char.called


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


class TestSendColorTemp:
    @pytest.mark.asyncio
    async def test_sends_ctl_set_unack(self) -> None:
        """send_color_temp should emit a Light CTL Set Unacknowledged."""
        dev = _make_device()
        captured: list[bytes] = []

        async def fake_send(payload: bytes) -> None:
            captured.append(payload)

        dev.send_vendor_command = fake_send  # type: ignore[method-assign]
        await dev.send_color_temp(370)  # warmest mireds
        assert len(captured) == 1
        # OP_LIGHT_CTL_SET_UNACK = 0x825F
        assert captured[0][:2] == bytes([0x82, 0x5F])


class TestSendScene:
    @pytest.mark.asyncio
    async def test_is_noop(self) -> None:
        dev = _make_device()
        await dev.send_scene(1)
        assert not dev._client.write_gatt_char.called
