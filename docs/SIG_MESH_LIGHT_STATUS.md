# SIG Mesh Light (`DEVICE_TYPE_SIG_LIGHT`) — Status & Known Limitations

This integration adds a `DEVICE_TYPE_SIG_LIGHT` for Bluetooth SIG Mesh lights
that advertise the Tuya vendor model alongside the standard SIG Light Models
(Lightness, CTL, HSL). It was developed and tested against a Tuya RGBW G4 bulb
(product id `keyd9k4q`, composition `CID:07D0 PID:0300 VID:3634`).

## What works

| Function | Implementation | Notes |
|---|---|---|
| Pairing / provisioning | PB‑GATT, key derivation, AppKey Add, Model App Bind for GenericOnOff Server (`0x1000`), Light Lightness/CTL/HSL Servers (`0x1300`/`0x1303`/`0x1307`), HSL Hue/Saturation Servers (`0x130A`/`0x130B`), and every vendor model the bulb reports in Composition Data Page 0 | Composition Data is parsed and the discovered model list drives the bind step, falling back to a hard-coded Tuya vendor model id list if comp-data is unavailable |
| On / off | SIG Generic OnOff Set Unacknowledged (`0x8202`) | ✅ |
| Brightness (white mode) | SIG Light Lightness Set Unacknowledged (`0x824D`) | ✅ |
| Colour | SIG Light HSL Set Unacknowledged (`0x8277`) with proper HSV→HSL conversion (`L = V·(1 - S/2)`). Per Tuya's BT Mesh DP control spec: *"The Bluetooth mesh specification requires the use of the HSL model, while Tuya's DP model uses the HSV model. Model conversion is needed."* | ✅ |
| Brightness in colour mode | Re-emits Light HSL Set with the current pure hue scaled by the new brightness — bulb dims while staying in colour mode | ✅ |
| Exit colour mode | `ColorMode.WHITE` advertised; tapping the HA "White" toggle fires `ATTR_WHITE` → entity sends `Light Lightness Set` which carries both the brightness and the implicit mode switch back to white | ✅ |
| Initial state sync | Composition Data on connect populates `firmware_version`. GenericOnOff Status pushed by the bulb updates `is_on` | After HA restart, the bulb's initial on/off state isn't queried — pressing the device's identify button or toggling power once syncs state |

## What's not supported

| Function | Cause |
|---|---|
| Colour temperature | Bulb advertises Light CTL Server (`0x1303`) in composition data, but field testing confirmed it does not act on `Light CTL Set Unack` — the same way its HSL Server initially appeared inert before we found the HSV→HSL fix. Smart Life also hides the CT slider for this product, suggesting Tuya's product profile doesn't wire CT up on the firmware side. `ColorMode.COLOR_TEMP` is therefore not exposed for SIG bulbs. The `send_color_temp` method remains implemented (emits `Light CTL Set Unack 0x825F`) on the chance another product behaves differently |

## How colour was resolved

For most of this PR's history, picking a colour produced a whitish-blue regardless
of requested RGB. The root cause was an HSV/HSL confusion: Tuya's developer-facing
API expresses colour in HSV (H 0-360, S 0-100%, V 0-100%) — what `_rgb_to_tuya_hsv`
produces — but the standard SIG Mesh `Light HSL Set` message expects HSL on the
wire. Sending HSV's V directly into the HSL `L` field made the bulb interpret a
fully-bright pure colour (V=100%) as white (since HSL L=100% is white regardless
of hue), with a tint from the H field — exactly the whitish-blue symptom.

`_rgb_to_sig_hsl_wire` does the standard HSV→HSL conversion: `L = V·(1 - S/2)`
and `S_HSL = (V - L) / min(L, 1 - L)`. For a fully-saturated colour V=1.0, S=1.0
this gives L=0.5 (0x8000 on the wire) — and at L=50% the SIG HSL Server renders
the pure hue.

Tuya's own [BT Mesh data control spec](https://developer.tuya.com/en/docs/iot-device-dev/bluetooth_software_map_mesh_data_control)
states this explicitly: *"The Bluetooth mesh specification requires the use of the
HSL model, while Tuya's DP model uses the HSV model. Model conversion is needed."*

The bulb's Tuya vendor model (`0x07D0:0x0004`) is bound to AppKey 0 at provisioning
time but is not used for primary lighting control on this product. Earlier
iterations of this PR tried sending colour via Tuya DP 5 in several wire formats
(STRING 12-char ASCII hex, STRING 6-char, RAW 6-byte BE/LE, with switch_led and
work_mode prefixes); none changed the bulb's behaviour. Colour goes through SIG
HSL only on this product family.

## File map

- `lib/tuya_ble_mesh/sig_mesh_device_commands.py` — `SIGMeshDeviceCommandsMixin.send_color` / `send_brightness` / `send_color_brightness` / `send_color_temp`
- `lib/tuya_ble_mesh/sig_mesh_protocol_codec.py` — SIG Light Lightness/CTL/HSL message builders, vendor model bind helper, composition data element parser
- `config_flow_sig.py` — provisioning sequence: AppKey Add → GenericOnOff bind → SIG Light model binds driven from Composition Data → vendor model binds driven from Composition Data
- `coordinator.py` — `_on_vendor_update` parses inbound Tuya vendor DPs (power_w, energy_kwh, and the legacy light DP ids — present for future products even though this bulb doesn't emit them); `set_light_state` exposes optimistic state updates for the light entity
- `light.py` — `TuyaBLEMeshLight` with `sig_mesh=True` flavour: RGB-only `supported_color_modes`, optimistic state on colour/brightness writes
