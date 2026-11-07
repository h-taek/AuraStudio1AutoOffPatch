<div align="center">

# Aura Studio 1 Auto-Off Patch

> Firmware dump, analysis and a 1-byte patch that stops the Harman Kardon Aura Studio 1 from powering itself off.

English | [한국어](README.ko.md)

</div>

## Problem and solution

The Aura Studio 1 turns itself off after a period of silence, and there is no setting to disable it.
This firmware patch blocks only the scheduling of the auto power-off event. Standby entry, wake-up and the manual power button are unchanged.

Target: Aura Studio 1, FCC ID `APIHKAURAST`, USB `0ECB:1EB2`. Not the original Aura (`HK AURA`) and not Aura Wi-Fi models.

## What is included

- Full firmware read over USB HID DFU, twice, byte-identical (`firmware_bin/`)
- CSR-dfu2 container split into regions, 17 internal files and 58 PS records (`firmware_analysis/`)
- XAP assembly listings for the Stack program and the VM application (`firmware_analysis/disassembly/`)
- Auto-off control flow trace with evidence excerpts (`firmware_analysis/power_off/`)
- Patched and restore DFU packages, plus the logs of every real write (`firmware_patched/`)
- Read, patch and write tools (`tools/`) with 24 offline tests (`tests/`)

## How it works

1. **Trace.** In the VM application, idle event `0x60D8` schedules timer `0x60DF`, whose handler sends power-off event `0x6002`.
2. **Patch.** At VM code word `0xE0F` the branch `BEQ 0x1C6E` becomes `BRA 0x1C6E`, so `0x60DF` is never scheduled.
   That is 1 byte (`F4`→`E0`) at DFU offset `0xDFCE6`.
3. **Checksums.** VM header XOR16 (1 byte) and the filesystem header CRC32 (4 bytes) are updated; the trailing file CRC stays the same.
   A dump whose filesystem preamble came back as all `FF` gets the canonical `fsr_dfu1` preamble, which the device requires.
4. **Built from your own unit.** The writer reads the speaker, builds the target image from that readback and keeps the unit's PS (settings) area byte for byte.
5. **Firmware check.** Only the firmware build analysed here is accepted. Its fingerprint ignores the PS area, so any unit on this build passes and other builds are refused.
6. **Write safely.** After writing, the whole image is read back and reset is sent only if it is byte-identical.

Other power-off causes are not touched.

## Status

- Written to a real unit on 2026-11-07: final status 0, full readback identical to the patched image (`firmware_patched/flashing/run_20261107T081225Z/`).
- The speaker has since stayed in standby for hours without powering off, and plays normally.

## Requirements

- macOS (IOKit through `ctypes`), Python 3 standard library only
- Speaker connected directly to a Mac USB-C port
- Update mode: hold **Volume− + Phone** for 5 seconds. Volume− + Bluetooth enters demo mode; do not use it.

## Layout

```
firmware_bin/
  aura_studio1_original.bin          first dump (SHA-256 d287d29d…)
  aura_studio1_repeat.bin            second dump, identical
firmware_analysis/
  regions/  stack/  filesystem/fs00/ split container
  settings.json  settings.md         PS records
  disassembly/stack_program.asm      Stack program listing
  disassembly/vm_program.asm         VM application listing
  power_off/                         auto-off trace
firmware_patched/                    history of the original unit
  device_package/                    image actually written + the image before it
  usb_package_v2/                    first image with the rebuilt FS preamble
  usb_package/, *.dfu (top level)    first attempt, rejected by the device. Never write
  flashing/run_*/                    write logs and readbacks
  recovery/  preflight/              FS preamble rebuild proof and pre-write checks
tools/
  device_read.py                     read-only dump
  device_write.py                    write patch / unpatch to the speaker
  device_usb_config.c                select USB configuration 1
  image_patch.py                     build patch / unpatch image from a dump (offline)
  image_unpack.py                    CSR-dfu2 container parser used by both
tests/                               offline tests (python3 -m unittest)
runs/                                write logs of your runs (git-ignored)
```

## Usage

Run from the repository root. Put the speaker in update mode first.

```sh
# 0. tests (offline)
python3 -m unittest

# 1. read-only dump -> my_dump/firmware.upload
python3 tools/device_read.py --dump-dir my_dump --log my_dump.jsonl

# 2. offline plan: firmware check, changed bytes, packet plan. No device access
python3 tools/device_write.py patch --dump my_dump/firmware.upload

# 3. write. Refuses if the speaker no longer matches the reviewed dump
python3 tools/device_write.py patch --dump my_dump/firmware.upload --apply
```

To undo, run the same steps with `unpatch` instead of `patch`.
Each write leaves `pre_write.upload`, `target.dfu`, `post_write.upload` and a log in `runs/run_<UTC time>/`.
`tools/image_patch.py patch my_dump/firmware.upload --output out.dfu` builds the image file without a device.

If the speaker shows up in update mode with USB configuration 0 (no HID interface), select configuration 1 first.

```sh
clang -framework IOKit -framework CoreFoundation tools/device_usb_config.c -o tools/device_usb_config
tools/device_usb_config --activate
```

`Unsupported firmware build` means the unit runs a firmware build other than the one analysed here; nothing is written.

## Firmware and disclaimer

Unofficial. Not affiliated with or endorsed by Harman International.
The firmware images and listings are Harman's work and are published for interoperability and preservation of a discontinued product.
Writing firmware can brick the speaker. Use at your own risk.

## License

[MIT](LICENSE) for the original tools and documentation.
Firmware images and files derived from them (`firmware_bin/`, `firmware_analysis/`, `firmware_patched/`) are not covered.
