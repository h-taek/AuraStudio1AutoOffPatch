#!/usr/bin/env python3
"""Aura Studio 1 auto-off writer. Default is offline; --apply writes hardware.

CSR HID reports: SDK HidDfu.dll 9cf63ef4d0c015a111793af25f615b7d1c4da67e,
framing 0x100069F0; status 0x10006C60; reset 0x10006EA0 sends 03 FF.
With --apply the speaker is read first and the target image is built from that
readback (image_patch.build), so the unit's own PS is kept. Reset is sent
only after a byte-identical full readback. No automatic retry, rollback, abort
or status clear after an error.
"""
import argparse
import ctypes as C
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
import time

from device_read import HID, bind, status, payload, CAPACITY
from image_patch import build, TARGETS
from image_unpack import Image

ROOT=Path(__file__).resolve().parents[1]
DESCRIPTOR=bytes.fromhex('0600ff0901a10115002600017508090296fe038501b102090295068502b102090295018503b102c0')


def sha(raw):return hashlib.sha256(raw).hexdigest()


def dry_run_reports(payload):
    # CSR HID framing, modeled on pinned source and inspected HidDfu.dll.
    # Report 1: ID=1, DNLOAD=1, uint16 block, uint16 length, <=1017 bytes.
    rebuilt = bytearray(); first = last = None; chunks = 0
    for offset in range(0, len(payload), 1017):
        part = payload[offset:offset+1017]
        if chunks >= 65535:
            raise ValueError('16-bit block number overflow')
        packet = struct.pack('<BBHH', 1, 1, chunks, len(part)) + part
        packet = packet.ljust(1023, b'\0')
        if len(packet) != 1023:
            raise ValueError('HID packet length mismatch')
        rid, request, number, size = struct.unpack_from('<BBHH', packet)
        if (rid, request, number) != (1, 1, chunks):
            raise ValueError('HID header mismatch')
        rebuilt.extend(packet[6:6+size])
        first = first or packet[:22].hex();last = packet[:6].hex();chunks += 1
    if bytes(rebuilt) != payload:
        raise ValueError('Dry-run packet reassembly mismatch')
    final_header = struct.pack('<BBHH', 1, 1, chunks, 0)
    return {'mode': 'offline_only_no_hardware_send', 'feature_report_bytes': 1023,
            'payload_per_full_report': 1017, 'data_reports': chunks,
            'final_empty_reports': 1, 'last_data_bytes': (len(payload)-1)%1017+1,
            'first_report_preview': first, 'last_data_header': last,
            'final_empty_header': final_header.hex(), 'reassembled_exactly': True,
            'payload_sha256': hashlib.sha256(payload).hexdigest()}


class WriteHID(HID):
    def __init__(self):
        super().__init__()
        self.set_report=bind(self.io,'IOHIDDeviceSetReport',C.c_int32,C.c_void_p,C.c_int,C.c_long,C.c_void_p,C.c_long)
        get=bind(self.io,'IOHIDDeviceGetProperty',C.c_void_p,C.c_void_p,C.c_void_p)
        length=bind(self.cf,'CFDataGetLength',C.c_long,C.c_void_p)
        pointer=bind(self.cf,'CFDataGetBytePtr',C.c_void_p,C.c_void_p)
        key=self.string(None,b'ReportDescriptor',0x08000100)
        try:
            ref=get(self.device,key)
            raw=C.string_at(pointer(ref),length(ref)) if ref else b''
        finally:self.release(key)
        if raw!=DESCRIPTOR:
            self.finish();raise RuntimeError('Unexpected HID report descriptor; no write')

    def send(self,raw):
        if not ((raw[:2]==b'\x01\x01' and len(raw)==1023) or raw==b'\x03\xff'):
            raise ValueError('Unapproved report shape')
        buf=(C.c_ubyte*len(raw)).from_buffer_copy(raw)
        rc=self.set_report(self.device,2,raw[0],buf,len(raw))
        if rc:raise RuntimeError(f'SET report {raw[0]}: 0x{rc&0xffffffff:08x}; not retried')


def get_status(h,log,**context):
    raw=h.report(2,7);value=status(raw)
    log('status',raw=raw.hex(),**value,**context)
    if value['status']:
        raise RuntimeError(f'DFU error status={value["status"]} state={value["state"]}; stopped')
    return value


def require_idle(h,log):
    raw=h.report(3,2);log('state',raw=raw.hex())
    current=get_status(h,log)
    if raw!=b'\x03\x02' or current['state']!=2:
        raise RuntimeError('Not error-free dfuIDLE; no write attempted')
    return current


def upload_image(h,log,label):
    require_idle(h,log);data=bytearray()
    for block in range(16500):
        raw=h.report(1,1023);part=payload(raw);data.extend(part)
        current=get_status(h,log,phase=label,block=block)
        if current['state']!=(2 if len(part)<CAPACITY else 9):
            raise RuntimeError('Unexpected readback state')
        if len(part)<CAPACITY:
            result=bytes(data);log('readback_complete',phase=label,bytes=len(result),sha256=sha(result))
            return result
    raise RuntimeError('Readback limit exceeded')


def wait_state(h,log,current,expected,phase,sleep):
    for attempt in range(12):
        if current['state']==expected:return current
        allowed={3,4} if expected==5 else {3,4,6,7}
        if current['state'] not in allowed:
            raise RuntimeError(f'Unexpected {phase} state {current["state"]}; expected {expected}')
        delay=max(current['poll_timeout_ms']/1000,0.01)
        if delay>60:raise RuntimeError('Unexpected poll timeout; no further packet sent')
        log('wait',phase=phase,seconds=delay)
        sleep(delay)
        current=get_status(h,log,phase=phase,poll=attempt+1)
    raise RuntimeError(f'{phase} polling limit; no automatic recovery')


def download(h,data,log,sleep=time.sleep):
    require_idle(h,log)
    count=(len(data)+1016)//1017;last=None
    for block in range(count):
        part=data[block*1017:(block+1)*1017]
        raw=(struct.pack('<BBHH',1,1,block,len(part))+part).ljust(1023,b'\0')
        log('write_attempt',block=block,length=len(part),header=raw[:6].hex())
        h.send(raw)
        current=get_status(h,log,phase='download',block=block)
        last=wait_state(h,log,current,5,'download',sleep)
        if block%128==0 or block+1==count:log('progress',block=block+1,blocks=count,bytes=min((block+1)*1017,len(data)))
    final=struct.pack('<BBHH',1,1,count,0).ljust(1023,b'\0')
    log('manifest_attempt',block=count,header=final[:6].hex())
    h.send(final)
    # CSR/Bose implementations retain the previous poll delay before final status.
    delay=max(last['poll_timeout_ms']/1000,0.01)
    if delay>60:raise RuntimeError('Unexpected final delay; no reset attempted')
    log('wait',phase='manifest',seconds=delay);sleep(delay)
    current=get_status(h,log,phase='manifest')
    wait_state(h,log,current,2,'manifest',sleep)
    log('download_complete',bytes=len(data),data_blocks=count)


def verify_readback(actual,expected):
    am=Image(actual).parse();em=Image(expected).parse()
    def artifacts(raw,m):
        return {a['path']:raw[a['offset']:a['offset']+a['length']] for a in m['artifacts']
                if a['role'] in {'filesystem_file','stack_program','stack_constants'}}
    aa,ee=artifacts(actual,am),artifacts(expected,em)
    if aa!=ee:raise RuntimeError('Readback app/stack/files differ; no reset attempted')
    return {'exact_image_match':actual==expected,'all_app_stack_files_match':True,
            'readback_sha256':sha(actual),'expected_sha256':sha(expected)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=sorted(TARGETS))
    parser.add_argument('--dump',type=Path,help='reviewed firmware.upload; with --apply the speaker must still match it')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    reviewed=args.dump.read_bytes() if args.dump else None
    if not args.apply:
        if reviewed is None:parser.error('offline plan needs --dump')
        try:data,report=build(reviewed,args.mode)
        except ValueError as exc:parser.exit(1,str(exc)+'\n')
        print(json.dumps({'device_access':False,**report,'plan':dry_run_reports(data)},indent=2));return 0
    output=args.output or ROOT/'runs'/datetime.now(timezone.utc).strftime('run_%Y%m%dT%H%M%SZ')
    output.mkdir(parents=True,exist_ok=False)
    journal=(output/'events.jsonl').open('x');h=None;write_attempted=False;summary={'result':'in_progress','output':str(output.resolve().relative_to(ROOT) if output.resolve().is_relative_to(ROOT) else output)}
    def log(event,**fields):
        nonlocal write_attempted
        if event in {'write_attempt','manifest_attempt'}:write_attempted=True
        row={'time_utc':datetime.now(timezone.utc).isoformat(),'event':event,**fields}
        journal.write(json.dumps(row)+'\n');journal.flush()
        if event not in {'status','write_attempt'}:print(json.dumps(row),flush=True)
    try:
        log('start',mode=args.mode,reviewed_dump_sha256=sha(reviewed) if reviewed else None)
        h=WriteHID()
        before=upload_image(h,log,'pre_write')
        (output/'pre_write.upload').write_bytes(before)
        if reviewed is not None and before!=reviewed:raise RuntimeError('Speaker differs from --dump; no write')
        data,report=build(before,args.mode)
        (output/'target.dfu').write_bytes(data)
        log('target_built',**report)
        download(h,data,log)
        after=upload_image(h,log,'post_write');(output/'post_write.upload').write_bytes(after)
        verified=verify_readback(after,data)
        if after!=data:raise RuntimeError('Full canonical readback differs; no reset attempted')
        log('post_write_verified',**verified)
        require_idle(h,log)
        log('reset_attempt',raw='03ff',reference='CSR HidDfu.dll VA 0x10006F10..0x10006F2A')
        h.send(b'\x03\xff')
        log('reset_sent')
        summary.update(result='written_readback_verified_reset_sent',**verified,hardware_playback_tested=False)
        return 0
    except Exception as exc:
        log('error',message=str(exc),write_attempted=write_attempted)
        summary.update(result='failed_or_unconfirmed',error=str(exc),write_attempted=write_attempted,automatic_retry=False)
        if h:
            try:summary['last_status']=get_status(h,log,phase='after_error')
            except Exception as second:summary['status_query_error']=str(second)
        return 1
    finally:
        if h:h.finish()
        summary['write_attempted']=write_attempted
        (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
        journal.close();print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':raise SystemExit(main())
