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
| Brightness | SIG Light Lightness Set Unacknowledged (`0x824D`) | ✅ |
| Initial state sync | Composition Data on connect populates `firmware_version`. GenericOnOff Status pushed by the bulb updates `is_on` | After HA restart, the bulb's initial on/off state isn't queried — pressing the device's identify button or toggling power once syncs state |

## What's partial

| Function | Behaviour | Cause |
|---|---|---|
| Colour | Picking a colour sends Light HSL Hue Set Unack (`0x8270`), Saturation Set Unack (`0x8274`), then combined HSL Set Unack (`0x8277`). The bulb enters colour mode and applies the *lightness* component, but appears to ignore the hue and saturation — produces a whitish-blue regardless of requested RGB | The bulb's HSL Server is advertised in Composition Data but does not act on hue/saturation in practice. The same bulb is controllable by Smart Life over BLE, so the firmware *can* drive colour — just through a path we have not identified |
| Brightness in colour mode | A brightness change while in colour mode flips the bulb back to white mode | Light Lightness Set is the SIG signal for "white mode at brightness N". Tuya DP 3 (`bright_value`) was tested as a mode-preserving alternative but produced no response on this product |
| Colour temperature | `send_color_temp` emits Light CTL Set Unack (`0x825F`). Untested against this product (Smart Life UI does not expose CT for this RGBW bulb) | The bulb has CTL models in Composition Data but Smart Life hides the slider — may or may not work |

## What was tried and ruled out

- **SIG Light HSL** in three variants (Hue Set / Saturation Set / combined HSL Set). Wire bytes verified byte-perfect against the spec. Bulb enters colour mode but does not apply hue or saturation.
- **Tuya vendor DP frames** to model `0x07D0:0x0004` (bound), in every combination of DP id / type / value format I could think of:
  - DP ids: 5, 24, 30 (the standard Tuya `dj` colour_data DP ids across v1 and v2 schemas)
  - DP types: `BOOL`, `ENUM`, `STRING`, `RAW`, `VALUE`
  - Wire encodings: 12-char ASCII hex `HHHHSSSSVVVV`, 6-char ASCII hex `HHSSVV`, 6-byte raw big-endian, 6-byte raw little-endian
  - Prefixed by `switch_led=true` (DP 1), `work_mode=colour` as ENUM and as STRING (DP 2)
  - None changed the bulb's behaviour. The vendor model is bound but evidently does not handle the standard Tuya BLE Mesh DP frame format on this product.

## How to confirm the right wire format

We need either:

1. **Tuya's NetKey for this bulb.** Smart Life on iOS holds it in the app sandbox; not extractable without a jailbreak. iPhone HCI snoop of Smart Life→bulb traffic is fully visible at the network layer (NID, packet size, GATT handle) but encrypted+obfuscated at the access layer — useful for confirming packet sizes match SIG HSL Set (they do) but not for revealing the right opcode/payload.
2. **A firmware dump of the bulb** (Telink chip, JTAG/UART) — would expose the message handler registrations and reveal which models actually act on which opcodes.
3. **A working reference implementation** for the same product family. If you have one and find this integration, please open an issue with the wire bytes from a known-good colour change — fixing `send_color` would be a one-commit change.

## File map

- `lib/tuya_ble_mesh/sig_mesh_device_commands.py` — `SIGMeshDeviceCommandsMixin.send_color` / `send_brightness` / `send_color_brightness` / `send_color_temp`
- `lib/tuya_ble_mesh/sig_mesh_protocol_codec.py` — SIG Light Lightness/CTL/HSL message builders, vendor model bind helper, composition data element parser
- `config_flow_sig.py` — provisioning sequence: AppKey Add → GenericOnOff bind → SIG Light model binds driven from Composition Data → vendor model binds driven from Composition Data
- `coordinator.py` — `_on_vendor_update` parses inbound Tuya vendor DPs (power_w, energy_kwh, and the legacy light DP ids — present for future products even though this bulb doesn't emit them); `set_light_state` exposes optimistic state updates for the light entity
- `light.py` — `TuyaBLEMeshLight` with `sig_mesh=True` flavour: RGB-only `supported_color_modes`, optimistic state on colour/brightness writes
