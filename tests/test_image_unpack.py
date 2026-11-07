"""Boundary/corruption tests using the actual, immutable USB upload fixture."""
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import image_unpack as unpack


class DumpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = (ROOT / 'firmware_bin/aura_studio1_original.bin').read_bytes()
        cls.fs_base, cls.fs_size = 639086, 677820

    def reject(self, value, message=None):
        with self.assertRaisesRegex(unpack.FormatError, message or '.'):
            unpack.Image(bytes(value)).parse()

    def repaired_fs(self, data):
        # Keep transport checks valid so malformed FS metadata reaches structural checks.
        end = self.fs_base + self.fs_size
        checksum_word = end - 10
        data[checksum_word:checksum_word + 2] = b'\0\0'
        value = 0
        for pos in range(self.fs_base, end, 2):
            value ^= int.from_bytes(data[pos:pos + 2], 'little')
        data[checksum_word:checksum_word + 2] = value.to_bytes(2, 'little')
        data[-4:] = (zlib.crc32(data[:-4]) ^ 0xffffffff).to_bytes(4, 'little')
        return data

    def pdp_write(self, data, offset, value):
        data[offset:offset + 4] = (value >> 16).to_bytes(2, 'little') + (value & 65535).to_bytes(2, 'little')

    def test_both_captures_and_source_coverage(self):
        repeat = (ROOT / 'firmware_bin/aura_studio1_repeat.bin').read_bytes()
        self.assertEqual(self.original, repeat)
        image = unpack.Image(self.original)
        result = image.parse()
        self.assertEqual(sum(x['file_count'] for x in result['filesystems']), 17)
        self.assertEqual(sum(x['record_count'] for x in result['persistent_stores']), 58)
        reconstructed = b''.join(image.read(x.offset, x.length) for x in image.regions)
        self.assertEqual(reconstructed, self.original)

    def test_truncated_headers_and_areas(self):
        for size in [0, 8, 100, 1223, 1497, 637485, 638945, len(self.original) - 1]:
            with self.subTest(size=size):
                self.reject(self.original[:size])

    def test_corrupted_stack(self):
        data = bytearray(self.original)
        data[1518] ^= 1
        self.reject(data, 'Stack CRC')

    def test_corrupted_ps(self):
        data = bytearray(self.original)
        data[637496] ^= 1
        self.reject(data, 'PS CRC')

    def test_corrupted_filesystem(self):
        data = bytearray(self.original)
        data[700000] ^= 1
        self.reject(data, 'XOR')

    def test_corrupted_file_crc(self):
        data = bytearray(self.original)
        data[-1] ^= 1
        self.reject(data, 'File CRC')

    def test_unsafe_embedded_filename(self):
        data = bytearray(self.original)
        entry = self.fs_base + 10 + 7 * 12  # vm.app
        encoded = data[entry:entry + 4]
        flags_name = int.from_bytes(encoded[:2], 'little') * 65536 + int.from_bytes(encoded[2:], 'little')
        name_start = self.fs_base + (flags_name & 0xffffff) * 2
        self.assertEqual(int.from_bytes(data[name_start:name_start + 2], 'little'), 6)
        data[name_start + 2:name_start + 14] = '../xxx'.encode('utf-16le')
        self.reject(self.repaired_fs(data), 'filename')

    def test_directory_cycle(self):
        data = bytearray(self.original)
        self.pdp_write(data, self.fs_base + 14, 1)  # root includes itself
        self.reject(self.repaired_fs(data), 'cycle')

    def test_bad_file_offset(self):
        data = bytearray(self.original)
        self.pdp_write(data, self.fs_base + 10 + 7 * 12 + 4, 0xffffffff)
        self.reject(self.repaired_fs(data), 'outside filesystem')

    def test_unknown_filesystem_preamble(self):
        data = bytearray(self.original)
        data[638946] = 0
        self.reject(data, 'preamble')

    def test_existing_destination_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'existing'
            destination.mkdir()
            sentinel = destination / 'keep.txt'
            sentinel.write_text('keep')
            result = subprocess.run([sys.executable, str(ROOT / 'tools/image_unpack.py'),
                                     str(ROOT / 'firmware_bin/aura_studio1_original.bin'),
                                     '--output', str(destination)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sentinel.read_text(), 'keep')
            self.assertEqual(list(destination.iterdir()), [sentinel])


if __name__ == '__main__':
    unittest.main()
