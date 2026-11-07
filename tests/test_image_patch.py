"""Builder regressions against real dumps and the image actually written to hardware."""
import struct
import unittest
import zlib
from pathlib import Path
import image_patch as p

ROOT = Path(__file__).resolve().parents[1]
read = lambda name: (ROOT / name).read_bytes()


def mutate(raw, offset, value):
    """Well-formed but different image: FS XOR16 and file CRC are made consistent again."""
    l = p.layout(raw)
    out = bytearray(raw); out[offset] = value
    start, end = l['payload']
    struct.pack_into('<H', out, l['fs_checksum'], 0)
    struct.pack_into('<H', out, l['fs_checksum'], p.xor16(out[start:end]))
    struct.pack_into('<I', out, len(out) - 4, zlib.crc32(out[:-4]) ^ 0xffffffff)
    return bytes(out)


class BuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = read('firmware_bin/aura_studio1_original.bin')
        cls.booted = read('firmware_patched/flashing/run_20261107T080828Z/baseline_after_normal_boot.upload')
        cls.written = read('firmware_patched/flashing/run_20261107T081225Z/post_write.upload')

    def test_reproduces_hardware_written_image(self):
        out, report = p.build(self.booted, 'patch')
        self.assertEqual(out, self.written)
        self.assertEqual(out, read('firmware_patched/device_package/aura_studio1_auto_off_disabled.dfu'))
        self.assertEqual(report['changed_offsets_hex'], ['0x9c06a', '0x9c06b', '0x9c06c', '0x9c06d', '0xde0bc', '0xdfce6'])

    def test_unpatch_restores_exact_image(self):
        self.assertEqual(p.build(self.written, 'unpatch')[0], self.booted)

    def test_all_ff_dump_gets_canonical_preamble(self):
        out, _ = p.build(self.original, 'patch')
        self.assertEqual(out, read('firmware_patched/usb_package_v2/aura_studio1_auto_off_disabled.dfu'))
        canonical, _ = p.build(out, 'unpatch')
        self.assertEqual(canonical, read('firmware_patched/flashing/run_20261107T074942Z/canonical_original_restored.upload'))

    def test_ps_kept_and_ignored_by_identity(self):
        l = p.layout(self.booted)
        start, end = l['ps']
        self.assertNotEqual(self.original[start:end], self.booted[start:end])
        self.assertEqual(p.identity(self.original, l), p.identity(self.booted, l))
        self.assertEqual(p.build(self.booted, 'patch')[0][start:end], self.booted[start:end])

    def test_already_in_target_state_refused(self):
        with self.assertRaisesRegex(ValueError, 'already patched'): p.build(self.written, 'patch')
        with self.assertRaisesRegex(ValueError, 'already original'): p.build(self.booted, 'unpatch')

    def test_other_firmware_refused(self):
        offset = 1257546  # inside tts/0.raw
        raw = mutate(self.booted, offset, self.booted[offset] ^ 1)
        with self.assertRaisesRegex(ValueError, 'Unsupported firmware'): p.build(raw, 'patch')

    def test_unknown_branch_refused(self):
        raw = mutate(self.booted, p.layout(self.booted)['branch'] + 2, 0x00)
        with self.assertRaisesRegex(ValueError, 'Unknown instruction'): p.build(raw, 'patch')


if __name__ == '__main__':
    unittest.main()
