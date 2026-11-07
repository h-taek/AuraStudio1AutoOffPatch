#!/usr/bin/env python3
"""Read-only CSR-dfu2/v3 unpacker; outputs source slices and JSON/Markdown.

Format references:
https://github.com/ramikg/csr-dfu-parser/blob/main/CSRDFU.bt
https://github.com/ramikg/csr-dfu-parser/blob/main/unpack_dfu_fs.1sc

Supported: 1224-byte v3 descriptor layout, standard filesystem preamble or
explicitly identified 140-byte all-FF USB-upload preamble. No device APIs,
patching, image rebuilding, disassembly or inferred PS key names.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from functools import reduce
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib


class FormatError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise FormatError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def crc32_csr(data):
    return zlib.crc32(data) ^ 0xffffffff


def safe_name(name):
    require(bool(name) and name not in {'.', '..'}, 'Empty or traversal filename')
    require(not any(ch in name for ch in '/\\\x00:'), 'Unsafe filename separator')
    require(not any(ord(ch) < 32 for ch in name), 'Control character in filename')
    return name


@dataclass(frozen=True)
class Slice:
    path: str
    offset: int
    length: int
    role: str


class Image:
    def __init__(self, data):
        self.data = data
        self.slices = []
        self.regions = []
        self.ps = []
        self.filesystems = []
        self.stacks = []
        self.paths = set()

    def read(self, offset, length):
        require(offset >= 0 and length >= 0 and offset + length <= len(self.data),
                f'Out-of-range read at {offset}, length {length}')
        return self.data[offset:offset + length]

    def values(self, fmt, offset):
        return struct.unpack(fmt, self.read(offset, struct.calcsize(fmt)))

    def add(self, path, offset, length, role, region=False):
        self.read(offset, length)
        require(path not in self.paths, f'Duplicate output path: {path}')
        self.paths.add(path)
        item = Slice(path, offset, length, role)
        self.slices.append(item)
        if region:
            self.regions.append(item)
        return item

    def parse_ps(self, offset, length, name):
        require(length >= 8, f'{name}: PS too short')
        raw = self.read(offset, length)
        clear, count = self.values('<HH', offset)
        require(count <= (length - 8) // 6, f'{name}: PS record count too large')
        pos, records = offset + 4, []
        for index in range(count):
            require(pos + 6 <= offset + length - 4, 'PS record header exceeds area')
            action, key, words = self.values('<HHH', pos)
            require(action in (0, 1, 2), f'Unsupported PS action {action}')
            start = pos
            pos += 6
            require(pos + words * 2 <= offset + length - 4, 'PS data exceeds area')
            values = list(self.values('<' + str(words) + 'H', pos))
            records.append(dict(index=index, action=action,
                                action_name=['clear', 'set_if_absent', 'set_always'][action],
                                key=key, key_hex=f'0x{key:04X}', word_count=words,
                                record_offset=start, value_offset=pos,
                                values_u16=values, values_hex=[f'{v:04X}' for v in values],
                                semantic_name=None))
            pos += words * 2
        remainder = offset + length - 4 - pos
        require(remainder in (0, 128), f'{name}: unexplained PS trailer {remainder}')
        stored = self.values('<I', offset + length - 4)[0]
        require(crc32_csr(raw[:-4]) == stored, f'{name}: PS CRC mismatch')
        return dict(area=name, offset=offset, length=length, clear_field=clear,
                    record_count=count, records=records, crc32=f'0x{stored:08X}',
                    crc_verified=True, opaque_trailer_bytes=remainder,
                    signature_validity='not_assessed')

    def parse_fs(self, offset, length, number):
        require(length >= 158, 'Filesystem area too short')
        prefix = self.read(offset, 140)
        if prefix[:8] == b'fsr_dfu1':
            profile = 'fsr_dfu1'
        else:
            require(prefix == b'\xff' * 140, 'Unsupported filesystem preamble')
            profile = 'usb_upload_all_ff_140'
        base = offset + 140
        require(self.read(base, 4) == b'iFel', 'Filesystem magic missing')
        def pdp(pos):
            high, low = self.values('<HH', pos)
            return (high << 16) | low
        size = pdp(base + 4) * 2
        require(size >= 18 and size + 140 == length, 'Filesystem size mismatch')
        end = base + size
        require(self.read(end - 4, 4) == b'iFel', 'Filesystem footer magic missing')
        require(pdp(end - 8) * 2 == size, 'Filesystem footer size mismatch')
        count = self.values('<H', base + 8)[0]
        require(count > 0 and 10 + count * 12 <= size - 8, 'Invalid filesystem node count')
        payload = self.read(base, size)
        require(reduce(int.__xor__, (v[0] for v in struct.iter_unpack('<H', payload)), 0) == 0,
                'Filesystem XOR checksum mismatch')
        entries = []
        for index in range(count):
            row = base + 10 + index * 12
            flags_name, location, node_size = pdp(row), pdp(row + 4), pdp(row + 8)
            flags, name_words = flags_name >> 24, flags_name & 0xffffff
            name = ''
            if name_words:
                name_start = base + name_words * 2
                require(base <= name_start <= end - 10, 'Filesystem name pointer outside payload')
                n = self.values('<H', name_start)[0]
                require(name_start + 2 + n * 2 <= end - 8, 'Filesystem name exceeds payload')
                try:
                    name = self.read(name_start + 2, n * 2).decode('utf-16le')
                except UnicodeDecodeError as exc:
                    raise FormatError('Invalid UTF-16LE filename') from exc
                safe_name(name)
            is_dir = bool(flags & 0x80)
            if index != 0:
                require(bool(name), 'Non-root node has no name')
            if is_dir:
                require(1 <= location <= count + 1 and location - 1 + node_size <= count,
                        'Directory child range outside node table')
            else:
                start = base + location * 2
                require(base + 10 + count * 12 <= start <= end - 8 and start + node_size <= end - 8,
                        'File data outside filesystem data range')
            entries.append(dict(index=index + 1, name=name, flags=f'0x{flags:02X}',
                                directory=is_dir, location=location, size=node_size))
        require(entries[0]['directory'] and entries[0]['name'] == '', 'Invalid filesystem root')
        visited = set()
        file_ranges = []
        def walk(index, parent, depth):
            require(depth <= 64, 'Directory tree too deep')
            require(index not in visited, 'Directory cycle or duplicate parent')
            visited.add(index)
            row = entries[index]
            path = '/'.join(p for p in (parent, row['name']) if p)
            row['path'] = path or '/'
            if row['directory']:
                start = row['location'] - 1
                for child in range(start, start + row['size']):
                    walk(child, path, depth + 1)
            else:
                absolute = base + row['location'] * 2
                out_path = f'filesystem/fs{number:02d}/{path}'
                self.add(out_path, absolute, row['size'], 'filesystem_file')
                row.update(source_offset=absolute, length=row['size'], output_path=out_path,
                           sha256=sha256(self.read(absolute, row['size'])))
                file_ranges.append((absolute, absolute + row['size']))
        walk(0, '', 0)
        require(len(visited) == count, 'Unreachable filesystem nodes')
        # This profile has distinct stored files; avoid silently accepting aliased/corrupt ranges.
        ranges = sorted(file_ranges)
        require(all(a[1] <= b[0] for a, b in zip(ranges, ranges[1:])), 'Overlapping stored files')
        return dict(index=number, offset=offset, length=length, preamble_profile=profile,
                    payload_offset=base, payload_length=size, xor16_verified=True,
                    node_count=count, file_count=len(file_ranges),
                    directory_count=count - len(file_ranges), entries=entries)

    def parse(self):
        require(self.read(0, 8) == b'CSR-dfu2', 'Expected CSR-dfu2')
        version, declared, header_length = self.values('<HIH', 8)
        require(version == 3 and header_length == 1224, 'Unsupported CSR header profile')
        require(declared == len(self.data), 'Declared file length mismatch')
        self.read(0, header_length)
        aggregates = list(self.values('<6I', 80))
        description_length = aggregates[5]
        require(description_length % 2 == 0, 'Odd description length')
        description_raw = self.read(header_length, description_length)
        try:
            description = description_raw.decode('utf-16le').rstrip('\x00')
        except UnicodeDecodeError as exc:
            raise FormatError('Invalid UTF-16LE description') from exc
        tables = {}
        specifications = [('stack', 104, 20, '<QQI'), ('application', 424, 14, '<HqI'),
                          ('persistent', 648, 20, '<QIII'), ('filesystem', 968, 16, '<IHHHHI')]
        for name, start, width, fmt in specifications:
            tables[name] = [list(self.values(fmt, start + i * width)) for i in range(16)]
        self.add('regions/00_header.bin', 0, header_length, 'header', True)
        pos = header_length
        if description_length:
            self.add('regions/01_description.bin', pos, description_length, 'description', True)
            pos += description_length
        def region(name, length):
            nonlocal pos
            start = pos
            self.add(f'regions/{len(self.regions):02d}_{name}.bin', start, length, name, True)
            pos += length
            return start
        for i, row in enumerate(tables['stack']):
            length = row[2]
            if not length:
                continue
            start = region(f'stack_{i:02d}', length)
            require(length >= 20 and self.read(start, 8) == b'CSRbcfw1', 'Invalid stack area')
            crc, program_length, constant_length, stack_version = self.values('<IIHH', start + 8)
            require(20 + program_length + constant_length == length, 'Stack component length mismatch')
            require(crc32_csr(self.read(start + 12, length - 12)) == crc, 'Stack CRC mismatch')
            self.add(f'stack/{i:02d}_program.bin', start + 20, program_length, 'stack_program')
            self.add(f'stack/{i:02d}_constants.bin', start + 20 + program_length,
                     constant_length, 'stack_constants')
            self.stacks.append(dict(index=i, offset=start, length=length, version=stack_version,
                                    program_length=program_length, constant_length=constant_length,
                                    crc32=f'0x{crc:08X}', crc_verified=True))
        for i, row in enumerate(tables['application']):
            if row[2]:
                region(f'application_{i:02d}', row[2])  # opaque application area
        for label, field in [('ps', 1), ('stack_ps', 2), ('application_ps', 3)]:
            for i, row in enumerate(tables['persistent']):
                if row[field]:
                    name = f'{label}_{i:02d}'
                    start = region(name, row[field])
                    self.ps.append(self.parse_ps(start, row[field], name))
        for i, row in enumerate(tables['filesystem']):
            if row[5]:
                start = region(f'filesystem_{i:02d}', row[5])
                self.filesystems.append(self.parse_fs(start, row[5], i))
        require(len(self.data) - pos in (4, 20), 'Unexpected bytes after declared areas')
        stored = self.values('<I', pos)[0]
        require(crc32_csr(self.data[:pos]) == stored, 'File CRC mismatch')
        region('file_crc', 4)
        suffix = None
        if pos < len(self.data):
            raw = self.read(pos, 16)
            require(raw[8:11] == b'UFD' and raw[11] == 16, 'Invalid DFU suffix')
            require(crc32_csr(self.data[:-4]) == self.values('<I', len(self.data) - 4)[0],
                    'DFU suffix CRC mismatch')
            suffix = dict(offset=pos, length=16, crc_verified=True, raw_hex=raw.hex())
            region('dfu_suffix', 16)
        cursor = 0
        for item in self.regions:
            require(item.offset == cursor, 'Region coverage gap or overlap')
            cursor += item.length
        require(cursor == len(self.data), 'Incomplete source coverage')
        artifacts = [dict(path=x.path, offset=x.offset, length=x.length, role=x.role,
                          sha256=sha256(self.read(x.offset, x.length))) for x in self.slices]
        return dict(format='CSR-dfu2', version=version, bytes=len(self.data),
                    sha256=sha256(self.data), header_length=header_length,
                    description_length=description_length, description=description,
                    descriptor_tables=tables, legacy_aggregate_lengths=aggregates[:5],
                    file_crc32=f'0x{stored:08X}', file_crc_verified=True, dfu_suffix=suffix,
                    full_region_coverage=True, regions=[x.path for x in self.regions],
                    stacks=self.stacks, persistent_stores=self.ps, filesystems=self.filesystems,
                    artifacts=artifacts, unknowns=[
                        'PS semantic names are not assigned; key numbers alone do not identify timers.',
                        'Execution code is preserved, not disassembled or decompiled.',
                        'Device model/SoC, runtime behavior, signatures and restoreability are not established by this parser.',
                    ])


def report(manifest):
    lines = ['# CSR DFU 분리 결과', '',
             f'- 원본 SHA-256: `{manifest["sha256"]}`',
             f'- 원본 크기: {manifest["bytes"]:,}바이트',
             '- 파일 CRC와 영역별 검증 통과. 원본은 수정하지 않았다.',
             '- 실행 코드는 원래 바이트 그대로 분리했다. 역어셈블/소스 복원 결과가 아니다.',
             '', '## 원본 영역', '', '| 파일 | 시작 바이트 | 길이 |', '|---|---:|---:|']
    by_path = {a['path']: a for a in manifest['artifacts']}
    for path in manifest['regions']:
        item = by_path[path]
        lines.append(f'| `{path}` | {item["offset"]:,} | {item["length"]:,} |')
    lines += ['', '## 내부 파일', '', '| 경로 | 원본 시작 바이트 | 길이 |', '|---|---:|---:|']
    for fs in manifest['filesystems']:
        for entry in fs['entries']:
            if not entry['directory']:
                lines.append(f'| `{entry["output_path"]}` | {entry["source_offset"]:,} | {entry["length"]:,} |')
    lines += ['', '## 설정값', '',
              '`settings.json`과 `settings.md`에 모든 PS record와 원본 위치를 기록했다.',
              '키 이름과 단위는 아직 미확인이다. 숫자 일치만으로 절전 타이머로 판단하지 않는다.',
              '', '## 검증과 제한', '',
              '`manifest.json`의 artifacts는 원본 바이트 범위와 각 출력의 SHA-256을 기록한다.',
              '`regions/`를 manifest 순서로 이어 붙이면 원본 전체를 재구성할 수 있다.',
              '파일시스템 앞 140바이트의 FF 프로필은 서명 존재/검사 여부를 뜻하지 않는다.',
              '기기 기록용 이미지는 생성하지 않았다.', '']
    return '\n'.join(lines)


def publish(image, manifest, output):
    output.mkdir(parents=True, exist_ok=False)
    for item in image.slices:
        target = output / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(image.read(item.offset, item.length))
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    (output / 'settings.json').write_text(json.dumps(manifest['persistent_stores'], indent=2) + '\n')
    lines = ['# PS 설정 원시 값', '', '키 의미와 단위는 미확인이다. 값은 16비트 unsigned 정수다.', '',
             '| 영역 | 키 | action | 원본 값 위치 | 16진수 값 |', '|---|---|---|---:|---|']
    for area in manifest['persistent_stores']:
        for row in area['records']:
            lines.append(f'| {area["area"]} | {row["key_hex"]} | {row["action_name"]} | '
                         f'{row["value_offset"]} | {" ".join(row["values_hex"])} |')
    (output / 'settings.md').write_text('\n'.join(lines) + '\n')
    (output / 'README.md').write_text(report(manifest))
    for item in manifest['artifacts']:
        require(sha256((output / item['path']).read_bytes()) == item['sha256'],
                f'Published output mismatch: {item["path"]}')
    restored = b''.join((output / path).read_bytes() for path in manifest['regions'])
    require(restored == image.data, 'Published source reconstruction mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        require(args.source.is_file(), 'Source must be a regular file')
        require(args.source.stat().st_size <= 64 * 1024 * 1024, 'Input exceeds 64 MiB limit')
        require(not args.output.exists() and not args.output.is_symlink(), 'Output already exists')
        source = args.source.read_bytes()
        image = Image(source)
        manifest = image.parse()
        manifest['source_path'] = str(args.source)
        publish(image, manifest, args.output)
        require(args.source.read_bytes() == source, 'Source changed during extraction')
        print(json.dumps(dict(output=str(args.output.resolve()), bytes=len(source),
                              sha256=manifest['sha256'],
                              files=sum(fs['file_count'] for fs in manifest['filesystems']),
                              ps_records=sum(ps['record_count'] for ps in manifest['persistent_stores']),
                              checks='CRC, XOR, source slices, full reconstruction, unchanged input'), indent=2))
        return 0
    except (FormatError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
