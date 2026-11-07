#!/usr/bin/env python3
"""Build the auto-off patch, or its reverse, from any Aura Studio 1 dump. No device API.

Only the analysed firmware build is accepted. Its identity is the image with the
per-unit PS area, the FS transport preamble, the patched branch and the derived
checksums normalised, so any unit on this build is supported and keeps its own PS.
The output always carries the canonical fsr_dfu1 preamble; the all-FF preamble seen
in USB uploads was rejected by the device.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib

from image_unpack import Image

FIRMWARE_ID = 'c29fd2bf8d0fc37fe38e8f6aec56c4e92dfeaa8f454d554c5a093d5c94f726e7'
BRANCH_WORD = 0xE0F
STATES = {'original': bytes.fromhex('000ef45e'), 'patched': bytes.fromhex('000ee05e')}
TARGETS = {'patch': 'patched', 'unpatch': 'original'}


def sha(raw): return hashlib.sha256(raw).hexdigest()


def require(ok, why):
    if not ok: raise ValueError(why)


def xor16(raw):
    result = 0
    for (word,) in struct.iter_unpack('<H', raw): result ^= word
    return result


def rewind_crc(crc, byte_count):
    for _ in range(byte_count * 8):
        old_low_bit = crc >> 31
        crc = (((crc ^ (0xedb88320 if old_low_bit else 0)) << 1) | old_low_bit) & 0xffffffff
    return crc


def layout(raw):
    m = Image(raw).parse()
    require(len(m['persistent_stores']) == 1 and len(m['filesystems']) == 1, 'Unexpected container layout')
    ps, fs = m['persistent_stores'][0], m['filesystems'][0]
    vm = next((a for a in m['artifacts'] if a['path'] == 'filesystem/fs00/vm.app'), None)
    require(vm is not None, 'vm.app missing')
    payload, payload_end = fs['payload_offset'], fs['payload_offset'] + fs['payload_length']
    require(payload - fs['offset'] == 140, 'Unexpected FS preamble length')
    header_words = struct.unpack_from('<H', raw, vm['offset'] + 8)[0]
    return dict(ps=(ps['offset'], ps['offset'] + ps['length']), preamble=fs['offset'],
                payload=(payload, payload_end), vm=(vm['offset'], vm['offset'] + vm['length']),
                branch=vm['offset'] + 2 * header_words + 2 * BRANCH_WORD,
                vm_checksum=vm['offset'] + 38, fs_checksum=payload_end - 12)


def state(raw, l):
    found = raw[l['branch']:l['branch'] + 4]
    for name, expected in STATES.items():
        if found == expected: return name
    raise ValueError('Unknown instruction at the auto-off branch')


def identity(raw, l):
    start, end = l['payload']
    normal = bytearray(raw[start:end])
    for offset, value in [(l['branch'], STATES['original']), (l['vm_checksum'], bytes(2)), (l['fs_checksum'], bytes(2))]:
        normal[offset - start:offset - start + len(value)] = value
    return sha(raw[:l['ps'][0]] + bytes(normal))


def build(raw, mode):
    """Pure bytes -> bytes. Keeps every byte outside the branch, checksums and FS preamble."""
    require(mode in TARGETS, 'Unknown mode')
    l = layout(raw)
    current, target = state(raw, l), TARGETS[mode]
    require(identity(raw, l) == FIRMWARE_ID, 'Unsupported firmware build; refusing')
    require(current != target, f'Image is already {target}; nothing to write')
    pre = l['preamble']
    require(raw[pre:pre + 136] in {b'\xff' * 136, b'fsr_dfu1' + bytes(128)}, 'Unexpected FS preamble')
    (vm, vm_end), (fs, fs_end) = l['vm'], l['payload']
    out = bytearray(raw)
    out[l['branch']:l['branch'] + 4] = STATES[target]
    for field, start, end in [(l['vm_checksum'], vm, vm_end), (l['fs_checksum'], fs, fs_end)]:
        struct.pack_into('<H', out, field, 0)
        struct.pack_into('<H', out, field, xor16(out[start:end]))
    crc = zlib.crc32(bytes(4) + out[fs:fs_end]) ^ 0xffffffff
    out[pre:fs] = b'fsr_dfu1' + bytes(128) + struct.pack('<I', rewind_crc(crc, fs_end - fs + 4))
    struct.pack_into('<I', out, len(out) - 4, zlib.crc32(out[:-4]) ^ 0xffffffff)
    out = bytes(out)
    require(layout(out) == l and state(out, l) == target and identity(out, l) == FIRMWARE_ID, 'Rebuilt image check failed')
    require(xor16(out[vm:vm_end]) == 0 and xor16(out[fs:fs_end]) == 0, 'Checksum residue mismatch')
    require(zlib.crc32(out[fs - 4:fs_end]) ^ 0xffffffff == 0, 'FS CRC residue mismatch')
    require(out[:pre] == raw[:pre], 'Header, Stack or PS changed')
    permitted = {l['branch'] + 2, l['vm_checksum'], l['vm_checksum'] + 1, l['fs_checksum'], l['fs_checksum'] + 1,
                 *range(pre, fs), *range(len(out) - 4, len(out))}
    changed = [i for i, (a, b) in enumerate(zip(raw, out)) if a != b]
    require(len(out) == len(raw) and set(changed) <= permitted, 'Difference outside approved fields')
    return out, {'mode': mode, 'from': current, 'to': target, 'firmware_id': FIRMWARE_ID,
                 'input_sha256': sha(raw), 'output_sha256': sha(out), 'bytes': len(out),
                 'ps_preserved': True, 'changed_offsets_hex': [hex(i) for i in changed]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=sorted(TARGETS))
    parser.add_argument('dump', type=Path, help='firmware.upload read from the speaker')
    parser.add_argument('--output', type=Path, required=True, help='new .dfu file; refuses overwrite')
    args = parser.parse_args()
    try:
        out, report = build(args.dump.read_bytes(), args.mode)
        with args.output.open('xb') as f: f.write(out)
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
