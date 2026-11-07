#!/usr/bin/env python3
"""Read-only macOS IOHID probe for Aura Studio 1 (0ECB:1EB2).

Uses only IOHIDDeviceGetReport; no SetReport, clear, abort, reset or upgrade.
Protocol: https://github.com/tchebb/bose-dfu/blob/master/src/protocol.rs
Cross-check: fwupd 1.8.9 plugins/dfu-csr/fu-dfu-csr-device.c
A GET for report 1 advances the volatile upload stream. Do not restart a
partial upload and concatenate it with an earlier dump.
"""
import argparse
import ctypes as C
import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

VID, PID = 0x0ECB, 0x1EB2
CAPACITY = 1017


def bind(lib, name, result, *args):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = result, list(args)
    return fn


class HID:
    def __init__(self):
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.io = C.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
        p, n, u = C.c_void_p, C.c_long, C.c_uint32
        self.string = bind(self.cf, 'CFStringCreateWithCString', p, p, C.c_char_p, u)
        self.number = bind(self.cf, 'CFNumberCreate', p, p, C.c_int, p)
        self.dictionary = bind(self.cf, 'CFDictionaryCreateMutable', p, p, n, p, p)
        self.put = bind(self.cf, 'CFDictionarySetValue', None, p, p, p)
        self.count = bind(self.cf, 'CFSetGetCount', n, p)
        self.values = bind(self.cf, 'CFSetGetValues', None, p, p)
        self.release = bind(self.cf, 'CFRelease', None, p)
        self.manager_create = bind(self.io, 'IOHIDManagerCreate', p, p, u)
        self.match = bind(self.io, 'IOHIDManagerSetDeviceMatching', None, p, p)
        self.devices = bind(self.io, 'IOHIDManagerCopyDevices', p, p)
        self.open = bind(self.io, 'IOHIDDeviceOpen', C.c_int32, p, u)
        self.close = bind(self.io, 'IOHIDDeviceClose', C.c_int32, p, u)
        self.get = bind(self.io, 'IOHIDDeviceGetReport', C.c_int32, p, C.c_int, n, p, C.POINTER(n))
        self.manager = self.manager_create(None, 0)
        self.refs = [self.manager]
        self.device = None
        matching = self.dictionary(None, 0, None, None)
        self.refs.append(matching)
        for key, value in [('VendorID', VID), ('ProductID', PID)]:
            k = self.string(None, key.encode(), 0x08000100)
            v = C.c_int32(value)
            number = self.number(None, 3, C.byref(v))  # kCFNumberSInt32Type
            self.refs.extend([k, number])
            self.put(matching, k, number)
        self.match(self.manager, matching)
        devices = self.devices(self.manager)
        if not devices:
            raise RuntimeError('No matching HID device (0ECB:1EB2)')
        self.refs.append(devices)
        count = self.count(devices)
        if count != 1:
            raise RuntimeError(f'Expected one matching HID device, found {count}')
        values = (p * count)()
        self.values(devices, values)
        self.device = values[0]
        rc = self.open(self.device, 0)  # shared, no seize
        if rc:
            self.device = None
            raise RuntimeError(f'IOHIDDeviceOpen: 0x{rc & 0xffffffff:08x}')

    def report(self, rid, size):
        data = (C.c_ubyte * size)()
        data[0] = rid
        length = C.c_long(size)
        rc = self.get(self.device, 2, rid, data, C.byref(length))  # Feature=2
        if rc:
            raise RuntimeError(f'GET report {rid}: 0x{rc & 0xffffffff:08x}')
        if not 1 <= length.value <= size:
            raise RuntimeError(f'Invalid returned length {length.value}')
        raw = bytes(data[:length.value])
        if raw[0] != rid:
            raise RuntimeError(f'Unexpected report ID: {raw.hex()}')
        return raw

    def finish(self):
        if self.device:
            self.close(self.device, 0)
            self.device = None
        for ref in reversed(self.refs):
            self.release(ref)
        self.refs.clear()


def status(raw):
    if len(raw) != 7:
        raise ValueError(f'Expected 7-byte status report, got {raw.hex()}')
    return dict(status=raw[1], poll_timeout_ms=int.from_bytes(raw[2:5], 'little'),
                state=raw[5], string_index=raw[6])


def payload(raw):
    if len(raw) < 6:
        raise ValueError('Firmware report shorter than header')
    size = int.from_bytes(raw[1:3], 'little')
    if size > CAPACITY or size > len(raw) - 6:
        raise ValueError(f'Invalid payload length {size}, report length {len(raw)}')
    return raw[6:6 + size]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dump-dir', type=Path, help='Read firmware stream into a NEW directory')
    parser.add_argument('--log', type=Path, required=True, help='New JSONL log; refuses overwrite')
    args = parser.parse_args()
    if args.dump_dir:
        args.dump_dir.mkdir(parents=True, exist_ok=False)
    log = args.log.open('x')
    device = None
    def record(event, **fields):
        item = dict(time=datetime.now(timezone.utc).isoformat(), event=event, **fields)
        log.write(json.dumps(item) + '\n')
        log.flush()
        print(json.dumps(item), flush=True)
    try:
        device = HID()
        record('open', vid=VID, pid=PID)
        raw = device.report(3, 2)
        record('state', raw=raw.hex())
        if len(raw) != 2:
            raise RuntimeError('Unexpected state report length')
        initial_raw = device.report(2, 7)
        initial = status(initial_raw)
        record('status_before', raw=initial_raw.hex(), **initial)
        if not args.dump_dir:
            return 0
        if raw[1] != 2 or initial['status'] != 0 or initial['state'] != 2:
            raise RuntimeError('Not error-free dfuIDLE; no upload attempted, no recovery sent')
        partial = args.dump_dir / 'firmware.partial'
        digest = hashlib.sha256()
        total = 0
        with partial.open('xb') as out:
            for block in range(16500):  # bounded, < 16.1 MiB
                raw = device.report(1, 1023)
                (args.dump_dir / f'report-{block:05d}.bin').write_bytes(raw)
                data = payload(raw)
                status_raw = device.report(2, 7)
                current = status(status_raw)
                record('upload', block=block, report_len=len(raw), payload_len=len(data),
                       header=raw[:6].hex(), preview=data[:32].hex(), status_raw=status_raw.hex(), **current)
                if current['status'] != 0:
                    raise RuntimeError('Device returned error; stopped without recovery commands')
                out.write(data)
                out.flush()
                digest.update(data)
                total += len(data)
                expected = 2 if len(data) < CAPACITY else 9
                if current['state'] != expected:
                    raise RuntimeError(f'Unexpected upload state {current["state"]}, expected {expected}')
                if len(data) < CAPACITY:
                    if total == 0:
                        raise RuntimeError('Empty upload; no firmware obtained')
                    break
            else:
                raise RuntimeError('Read limit reached; kept partial file')
        target = partial.with_name('firmware.upload')
        partial.rename(target)
        record('upload_complete', bytes=total, sha256=digest.hexdigest(), path=str(target),
               note='Transport completed; image contents and coverage require separate verification')
        return 0
    except Exception as exc:
        record('error', message=str(exc))
        return 1
    finally:
        if device:
            device.finish()
        log.close()


if __name__ == '__main__':
    sys.exit(main())
