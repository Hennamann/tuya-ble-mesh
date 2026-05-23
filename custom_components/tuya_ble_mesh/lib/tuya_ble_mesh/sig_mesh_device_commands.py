"""SIG Mesh device command methods.

Provides ``SIGMeshDeviceCommandsMixin`` which handles:

- GenericOnOff Set commands with retry and exponential backoff
- Tuya vendor model commands
- Config Composition Data Get
- Config AppKey Add (segmented transport)
- Config Model App Bind

This mixin is not intended for standalone use — it requires attributes
defined in ``SIGMeshDevice.__init__``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from tuya_ble_mesh.const import (
    DEFAULT_SIG_MESH_MAX_RETRIES,
    DEFAULT_SIG_MESH_RESPONSE_TIMEOUT,
    SIG_MESH_ONOFF_RESPONSE_TIMEOUT,
    STATUS_WAIT_POLL_INTERVAL,
)
from tuya_ble_mesh.exceptions import (
    MeshConnectionError,
    SIGMeshError,
    SIGMeshKeyError,
)
from tuya_ble_mesh.logging_context import MeshLogAdapter
from tuya_ble_mesh.sig_mesh_device_segments import (
    _OPCODE_APPKEY_STATUS,
    _OPCODE_MODEL_APP_STATUS,
)
from tuya_ble_mesh.sig_mesh_protocol import (
    DP_TYPE_BOOL,
    DP_TYPE_ENUM,
    DP_TYPE_STRING,
    DP_TYPE_VALUE,
    MAX_UNSEG_ACCESS_PAYLOAD,
    SEG_DATA_SIZE,
    TUYA_VENDOR_WRITE_UNACK,
    TuyaVendorDP,
    config_appkey_add,
    config_composition_get,
    config_model_app_bind,
    config_model_app_bind_vendor,
    encrypt_network_pdu,
    generic_onoff_set,
    light_ctl_set_unack,
    light_hsl_hue_set_unack,
    light_hsl_saturation_set_unack,
    light_hsl_set_unack,
    light_lightness_set_unack,
    make_access_segmented,
    make_access_unsegmented,
    make_proxy_pdu,
    make_tuya_vendor_dp_payload,
)

if TYPE_CHECKING:
    from tuya_ble_mesh.sig_mesh_protocol import MeshKeys

_LOGGER = MeshLogAdapter(logging.getLogger(__name__), {})

# SIG Mesh GATT Proxy Data In UUID (same as in sig_mesh_device.py)
SIG_MESH_PROXY_DATA_IN = "00002add-0000-1000-8000-00805f9b34fb"

# Default TTL for mesh commands
_DEFAULT_TTL = 5

# BLE write retry backoff parameters
_BLE_WRITE_RETRY_INITIAL_BACKOFF = 1.0
_BLE_WRITE_RETRY_BACKOFF_MULTIPLIER = 2.0


def _rgb_to_tuya_hsv(red: int, green: int, blue: int) -> tuple[int, int, int]:
    """Convert 0..255 RGB to Tuya wire HSV (H 0..360, S 0..1000, V 0..1000)."""
    r = max(0, min(int(red), 255)) / 255.0
    g = max(0, min(int(green), 255)) / 255.0
    b = max(0, min(int(blue), 255)) / 255.0
    cmax = max(r, g, b)
    cmin = min(r, g, b)
    delta = cmax - cmin
    if delta == 0:
        hue = 0.0
    elif cmax == r:
        hue = 60.0 * (((g - b) / delta) % 6.0)
    elif cmax == g:
        hue = 60.0 * (((b - r) / delta) + 2.0)
    else:
        hue = 60.0 * (((r - g) / delta) + 4.0)
    sat = 0.0 if cmax == 0 else delta / cmax
    return (
        max(0, min(360, round(hue))),
        max(0, min(1000, round(sat * 1000))),
        max(0, min(1000, round(cmax * 1000))),
    )


class SIGMeshDeviceCommandsMixin:
    """Mixin providing command methods for SIGMeshDevice.

    Requires attributes defined in ``SIGMeshDevice.__init__``:
    ``_client``, ``_keys``, ``_address``, ``_target_addr``,
    ``_our_addr``, ``_tid``, ``_correlation_id``, ``_segment_lock``,
    ``_pending_responses``.

    Also requires ``_next_seq()`` and ``_next_seqs()`` methods.
    """

    # Type stubs for attributes defined in SIGMeshDevice.__init__
    _client: Any
    _keys: MeshKeys | None
    _address: str
    _target_addr: int
    _our_addr: int
    _tid: int
    _correlation_id: int
    _segment_lock: asyncio.Lock
    _pending_responses: dict[tuple[int, int], asyncio.Future[bytes]]

    async def _next_seq(self) -> int:
        raise NotImplementedError

    async def _next_seqs(self, n: int) -> int:
        raise NotImplementedError

    async def send_power(
        self, on: bool, *, max_retries: int = DEFAULT_SIG_MESH_MAX_RETRIES
    ) -> None:
        """Send GenericOnOff Set command with retry.

        Retries on transient BLE write failures with exponential backoff.

        Args:
            on: True to turn on, False to turn off.
            max_retries: Maximum retry attempts (default 3).

        Raises:
            SIGMeshError: If not connected or keys not loaded.
            MeshConnectionError: If BLE write fails after all retries.
        """
        if self._client is None or self._keys is None:
            msg = "Not connected"
            raise SIGMeshError(msg)

        app_key = self._keys.app_key
        if app_key is None:
            msg = "No application key loaded"
            raise SIGMeshKeyError(msg)

        last_error: Exception | None = None
        backoff = _BLE_WRITE_RETRY_INITIAL_BACKOFF

        for attempt in range(1, max_retries + 1):
            try:
                access_payload = generic_onoff_set(on, self._tid)
                self._tid = (self._tid + 1) & 0xFF

                seq = await self._next_seq()

                transport_pdu = make_access_unsegmented(
                    app_key,
                    self._our_addr,
                    self._target_addr,
                    seq,
                    self._keys.iv_index,
                    access_payload,
                    akf=1,
                    aid=self._keys.aid,
                )

                network_pdu = encrypt_network_pdu(
                    self._keys.enc_key,
                    self._keys.priv_key,
                    self._keys.nid,
                    ctl=0,
                    ttl=_DEFAULT_TTL,
                    seq=seq,
                    src=self._our_addr,
                    dst=self._target_addr,
                    transport_pdu=transport_pdu,
                    iv_index=self._keys.iv_index,
                )

                proxy_pdu = make_proxy_pdu(network_pdu)

                await self._client.write_gatt_char(
                    SIG_MESH_PROXY_DATA_IN, proxy_pdu, response=False
                )
                _LOGGER.info(
                    "GenericOnOff %s sent to 0x%04X (seq=%d, attempt=%d)",
                    "ON" if on else "OFF",
                    self._target_addr,
                    seq,
                    attempt,
                )
                return
            except (SIGMeshError, SIGMeshKeyError):
                raise
            except (BleakError, OSError) as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                _LOGGER.warning(
                    "BLE write attempt %d/%d failed for %s: %s, retrying in %.1fs",
                    attempt,
                    max_retries,
                    self._address,
                    type(exc).__name__,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff *= _BLE_WRITE_RETRY_BACKOFF_MULTIPLIER

        msg = f"BLE write failed for {self._address} after {max_retries} attempts"
        raise MeshConnectionError(msg) from last_error

    async def _send_dp(self, dp: TuyaVendorDP) -> None:
        """Build a single-DP Tuya vendor frame and send it (WRITE_UNACK)."""
        payload = make_tuya_vendor_dp_payload(TUYA_VENDOR_WRITE_UNACK, [dp])
        await self.send_vendor_command(payload)

    async def send_dp_bool(self, dp_id: int, value: bool) -> None:
        """Send a Tuya bool DP."""
        await self._send_dp(TuyaVendorDP(dp_id, DP_TYPE_BOOL, b"\x01" if value else b"\x00"))

    async def send_dp_value(self, dp_id: int, value: int) -> None:
        """Send a Tuya value DP (4-byte big-endian signed int)."""
        await self._send_dp(
            TuyaVendorDP(dp_id, DP_TYPE_VALUE, value.to_bytes(4, "big", signed=True))
        )

    async def send_dp_enum(self, dp_id: int, value: int) -> None:
        """Send a Tuya enum DP (1-byte index)."""
        if not 0 <= value <= 0xFF:
            msg = f"enum value {value} out of range for DP {dp_id}"
            raise SIGMeshError(msg)
        await self._send_dp(TuyaVendorDP(dp_id, DP_TYPE_ENUM, bytes([value])))

    async def send_dp_string(self, dp_id: int, value: str) -> None:
        """Send a Tuya string DP."""
        await self._send_dp(TuyaVendorDP(dp_id, DP_TYPE_STRING, value.encode("ascii")))

    async def send_brightness(self, level: int) -> None:
        """Send brightness via Light Lightness Set Unacknowledged.

        Accepts the same 1..100 scale used by ``MeshDevice.send_brightness``
        so the HA light entity stays range-compatible. Internally scaled to
        the standard SIG 0..65535 lightness range.
        """
        clamped = max(1, min(int(level), 100))
        wire = max(1, min(0xFFFF, round(clamped * 0xFFFF / 100)))
        payload = light_lightness_set_unack(wire, self._tid)
        self._tid = (self._tid + 1) & 0xFF
        await self.send_vendor_command(payload)

    async def send_color_brightness(self, level: int) -> None:
        """Send brightness on the colour-side scale (0..255) via Light Lightness."""
        clamped = max(0, min(int(level), 255))
        wire = max(0, min(0xFFFF, round(clamped * 0xFFFF / 255)))
        payload = light_lightness_set_unack(wire, self._tid)
        self._tid = (self._tid + 1) & 0xFF
        await self.send_vendor_command(payload)

    async def send_color(self, red: int, green: int, blue: int) -> None:
        """Send RGB colour as three SIG Mesh messages.

        The combined Light HSL Set Unacknowledged (0x8277) is not honored by
        every BT Mesh light implementation. We send the three per-component
        messages in sequence so the bulb's Hue Server, Saturation Server,
        and Lightness Server each see their own value.

        RGB is converted to HSV, then mapped to the SIG Mesh 16-bit
        encoding (H 0..65535 → 0..360°, S/L 0..65535 → 0..100%).
        """
        h_deg, s_per_1000, v_per_1000 = _rgb_to_tuya_hsv(red, green, blue)
        hue = max(0, min(0xFFFF, round(h_deg * 0xFFFF / 360)))
        saturation = max(0, min(0xFFFF, round(s_per_1000 * 0xFFFF / 1000)))
        lightness = max(0, min(0xFFFF, round(v_per_1000 * 0xFFFF / 1000)))

        _LOGGER.warning(
            "send_color RGB=(%d,%d,%d) -> H=%d (0x%04X / %d°) S=%d (0x%04X) L=%d (0x%04X)",
            red,
            green,
            blue,
            hue,
            hue,
            h_deg,
            saturation,
            saturation,
            lightness,
            lightness,
        )

        # 1. Set hue
        payload = light_hsl_hue_set_unack(hue, self._tid)
        self._tid = (self._tid + 1) & 0xFF
        await self.send_vendor_command(payload)

        # 2. Set saturation
        payload = light_hsl_saturation_set_unack(saturation, self._tid)
        self._tid = (self._tid + 1) & 0xFF
        await self.send_vendor_command(payload)

        # 3. Also send the combined HSL Set as a belt-and-braces — bulbs that
        # only implement the combined message will pick it up; those that
        # only implement the per-component already got the values above.
        payload = light_hsl_set_unack(lightness, hue, saturation, self._tid)
        self._tid = (self._tid + 1) & 0xFF
        await self.send_vendor_command(payload)

    async def send_light_mode(self, mode: int) -> None:
        """Mode switching is implicit in SIG Mesh: writing HSL puts the bulb
        in colour mode; writing Lightness puts it in white mode. No-op here,
        kept so the HA light entity's call site is unconditional."""
        _LOGGER.debug("send_light_mode(%d) ignored — implicit via Light HSL/Lightness", mode)

    async def send_color_temp(self, mireds: int) -> None:
        """Send colour temperature via Light CTL Set Unacknowledged.

        Input is in mireds (153..370). SIG CTL Temperature is in kelvin
        encoded as 16-bit, range 0x0320..0x4E20 (800K..20000K).
        """
        clamped_mireds = max(1, int(mireds))
        kelvin = round(1_000_000 / clamped_mireds)
        ct_value = max(0x0320, min(0x4E20, kelvin))
        # Lightness stays at the current value — we don't change brightness
        # via a CT-only request, so send 0xFFFF as a "keep current" hint.
        payload = light_ctl_set_unack(0xFFFF, ct_value, 0, self._tid)
        self._tid = (self._tid + 1) & 0xFF
        await self.send_vendor_command(payload)

    async def send_scene(self, _scene_id: int) -> None:
        """No-op stub: scene support not yet implemented for SIG lights."""
        _LOGGER.debug("send_scene ignored: not implemented for SIG light")

    async def send_vendor_command(self, access_payload: bytes) -> None:
        """Send a Tuya vendor model command (uses AppKey encryption).

        Args:
            access_payload: Complete access layer payload including opcode bytes.

        Raises:
            SIGMeshError: If not connected or keys not loaded.
            MeshConnectionError: If BLE write fails.
        """
        if self._client is None or self._keys is None:
            msg = "Not connected"
            raise SIGMeshError(msg)

        app_key = self._keys.app_key
        if app_key is None:
            msg = "No application key loaded"
            raise SIGMeshKeyError(msg)

        # Payloads larger than the unsegmented limit require segmented transport.
        # Tuya vendor WRITE_UNACK does not expect a status response, so we send
        # each segment without registering a response future.
        if len(access_payload) > MAX_UNSEG_ACCESS_PAYLOAD:
            upper_len = len(access_payload) + 4  # + 4-byte MIC (szmic=0)
            n_segs = (upper_len + SEG_DATA_SIZE - 1) // SEG_DATA_SIZE
            seq_start = await self._next_seqs(n_segs)

            segments = make_access_segmented(
                app_key,
                self._our_addr,
                self._target_addr,
                seq_start,
                self._keys.iv_index,
                access_payload,
                akf=1,
                aid=self._keys.aid,
            )
            for seg_seq, seg_transport_pdu in segments:
                network_pdu = encrypt_network_pdu(
                    self._keys.enc_key,
                    self._keys.priv_key,
                    self._keys.nid,
                    ctl=0,
                    ttl=_DEFAULT_TTL,
                    seq=seg_seq,
                    src=self._our_addr,
                    dst=self._target_addr,
                    transport_pdu=seg_transport_pdu,
                    iv_index=self._keys.iv_index,
                )
                proxy_pdu = make_proxy_pdu(network_pdu)
                await self._client.write_gatt_char(
                    SIG_MESH_PROXY_DATA_IN, proxy_pdu, response=False
                )
            _LOGGER.info(
                "Vendor command sent to 0x%04X (opcode=%s, seq_start=%d, %d bytes, %d segs)",
                self._target_addr,
                access_payload[:3].hex(),
                seq_start,
                len(access_payload),
                len(segments),
            )
            return

        seq = await self._next_seq()

        transport_pdu = make_access_unsegmented(
            app_key,
            self._our_addr,
            self._target_addr,
            seq,
            self._keys.iv_index,
            access_payload,
            akf=1,
            aid=self._keys.aid,
        )

        network_pdu = encrypt_network_pdu(
            self._keys.enc_key,
            self._keys.priv_key,
            self._keys.nid,
            ctl=0,
            ttl=_DEFAULT_TTL,
            seq=seq,
            src=self._our_addr,
            dst=self._target_addr,
            transport_pdu=transport_pdu,
            iv_index=self._keys.iv_index,
        )

        proxy_pdu = make_proxy_pdu(network_pdu)
        await self._client.write_gatt_char(SIG_MESH_PROXY_DATA_IN, proxy_pdu, response=False)

        _LOGGER.warning(
            "App message sent to 0x%04X (seq=%d, %d bytes, access_payload=%s)",
            self._target_addr,
            seq,
            len(access_payload),
            access_payload.hex(),
        )

    async def request_composition_data(self) -> None:
        """Send Config Composition Data Get to retrieve device info.

        Uses the device key (akf=0) since this is a config message.

        Raises:
            SIGMeshError: If not connected or keys not loaded.
        """
        if self._client is None or self._keys is None:
            msg = "Not connected"
            raise SIGMeshError(msg)

        access_payload = config_composition_get(page=0)
        seq = await self._next_seq()

        transport_pdu = make_access_unsegmented(
            self._keys.dev_key,
            self._our_addr,
            self._target_addr,
            seq,
            self._keys.iv_index,
            access_payload,
            akf=0,
            aid=0,
        )

        network_pdu = encrypt_network_pdu(
            self._keys.enc_key,
            self._keys.priv_key,
            self._keys.nid,
            ctl=0,
            ttl=_DEFAULT_TTL,
            seq=seq,
            src=self._our_addr,
            dst=self._target_addr,
            transport_pdu=transport_pdu,
            iv_index=self._keys.iv_index,
        )

        proxy_pdu = make_proxy_pdu(network_pdu)
        await self._client.write_gatt_char(SIG_MESH_PROXY_DATA_IN, proxy_pdu, response=False)
        _LOGGER.info(
            "Composition Data Get sent to 0x%04X (seq=%d)",
            self._target_addr,
            seq,
        )

    async def send_config_appkey_add(
        self,
        app_key: bytes,
        *,
        net_idx: int = 0,
        app_idx: int = 0,
        response_timeout: float = DEFAULT_SIG_MESH_RESPONSE_TIMEOUT,
    ) -> bool:
        """Send Config AppKey Add and wait for Status response.

        Uses segmented transport (2 segments) and device key (akf=0).

        Args:
            app_key: 16-byte application key to add.
            net_idx: Network key index (0-4095).
            app_idx: Application key index (0-4095).
            response_timeout: Seconds to wait for AppKey Status response.

        Returns:
            True if device responded with Success (0x00), False otherwise.

        Raises:
            SIGMeshError: If not connected or keys not loaded.
        """
        if self._client is None or self._keys is None:
            msg = "Not connected"
            raise SIGMeshError(msg)

        access_payload = config_appkey_add(net_idx, app_idx, app_key)
        upper_len = len(access_payload) + 4  # + 4-byte MIC (szmic=0)
        n_segs = (upper_len + SEG_DATA_SIZE - 1) // SEG_DATA_SIZE
        seq_start = await self._next_seqs(n_segs)

        segments = make_access_segmented(
            self._keys.dev_key,
            self._our_addr,
            self._target_addr,
            seq_start,
            self._keys.iv_index,
            access_payload,
            akf=0,
            aid=0,
        )

        # CF-1: Register response future BEFORE sending (protected by lock)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        async with self._segment_lock:
            corr_id = self._correlation_id
            self._correlation_id += 1
            resp_key = (_OPCODE_APPKEY_STATUS, corr_id)
            self._pending_responses[resp_key] = future

        try:
            for seg_seq, transport_pdu in segments:
                network_pdu = encrypt_network_pdu(
                    self._keys.enc_key,
                    self._keys.priv_key,
                    self._keys.nid,
                    ctl=0,
                    ttl=_DEFAULT_TTL,
                    seq=seg_seq,
                    src=self._our_addr,
                    dst=self._target_addr,
                    transport_pdu=transport_pdu,
                    iv_index=self._keys.iv_index,
                )
                proxy_pdu = make_proxy_pdu(network_pdu)
                await self._client.write_gatt_char(
                    SIG_MESH_PROXY_DATA_IN, proxy_pdu, response=False
                )
                await asyncio.sleep(STATUS_WAIT_POLL_INTERVAL)

            _LOGGER.info(
                "AppKey Add sent to 0x%04X (%d segments, seq_start=%d)",
                self._target_addr,
                len(segments),
                seq_start,
            )

            params = await asyncio.wait_for(asyncio.shield(future), timeout=response_timeout)
        except TimeoutError:
            msg = "Timeout waiting for AppKey Status response"
            raise SIGMeshError(msg) from None
        finally:
            async with self._segment_lock:
                self._pending_responses.pop(resp_key, None)

        status = params[0] if params else 0xFF
        _LOGGER.info(
            "AppKey Status from 0x%04X: 0x%02X (%s)",
            self._target_addr,
            status,
            "Success" if status == 0x00 else "Error",
        )
        return status == 0x00

    async def send_config_model_app_bind(
        self,
        element_addr: int,
        app_idx: int,
        model_id: int,
        *,
        cid: int | None = None,
        response_timeout: float = SIG_MESH_ONOFF_RESPONSE_TIMEOUT,
    ) -> bool:
        """Send Config Model App Bind and wait for Status.

        Uses unsegmented transport and device key (akf=0).

        Args:
            element_addr: Element unicast address.
            app_idx: Application key index to bind.
            model_id: Model ID. With cid=None this is a 16-bit SIG model id
                (e.g. 0x1000 for GenericOnOff Server). With cid set this is
                the 16-bit vendor model id within that company.
            cid: Optional company id for a vendor model bind (e.g. 0x07D0
                for Tuya). When None, a SIG model bind is sent.
            response_timeout: Seconds to wait for Model App Status response.

        Returns:
            True if device responded with Success (0x00), False otherwise.

        Raises:
            SIGMeshError: If not connected or keys not loaded.
        """
        if self._client is None or self._keys is None:
            msg = "Not connected"
            raise SIGMeshError(msg)

        if cid is None:
            access_payload = config_model_app_bind(element_addr, app_idx, model_id)
        else:
            access_payload = config_model_app_bind_vendor(element_addr, app_idx, cid, model_id)
        seq = await self._next_seq()

        transport_pdu = make_access_unsegmented(
            self._keys.dev_key,
            self._our_addr,
            self._target_addr,
            seq,
            self._keys.iv_index,
            access_payload,
            akf=0,
            aid=0,
        )
        network_pdu = encrypt_network_pdu(
            self._keys.enc_key,
            self._keys.priv_key,
            self._keys.nid,
            ctl=0,
            ttl=_DEFAULT_TTL,
            seq=seq,
            src=self._our_addr,
            dst=self._target_addr,
            transport_pdu=transport_pdu,
            iv_index=self._keys.iv_index,
        )
        proxy_pdu = make_proxy_pdu(network_pdu)

        # CF-1: Register response future BEFORE sending (protected by lock)
        loop = asyncio.get_running_loop()
        future_bind: asyncio.Future[bytes] = loop.create_future()
        async with self._segment_lock:
            corr_id = self._correlation_id
            self._correlation_id += 1
            resp_key = (_OPCODE_MODEL_APP_STATUS, corr_id)
            self._pending_responses[resp_key] = future_bind

        try:
            await self._client.write_gatt_char(SIG_MESH_PROXY_DATA_IN, proxy_pdu, response=False)
            _LOGGER.info(
                "Model App Bind sent: element=0x%04X app_idx=%d model=%s (seq=%d)",
                element_addr,
                app_idx,
                f"vendor 0x{cid:04X}:0x{model_id:04X}" if cid is not None else f"0x{model_id:04X}",
                seq,
            )

            params_bind = await asyncio.wait_for(
                asyncio.shield(future_bind), timeout=response_timeout
            )
        except TimeoutError:
            msg = "Timeout waiting for Model App Status response"
            raise SIGMeshError(msg) from None
        finally:
            async with self._segment_lock:
                self._pending_responses.pop(resp_key, None)

        status_bind = params_bind[0] if params_bind else 0xFF
        _LOGGER.info(
            "Model App Status from 0x%04X: 0x%02X (%s)",
            self._target_addr,
            status_bind,
            "Success" if status_bind == 0x00 else "Error",
        )
        return status_bind == 0x00


# Import BleakError at module level for send_power exception handling
try:
    from bleak.exc import BleakError
except ImportError:  # pragma: no cover
    BleakError = OSError
