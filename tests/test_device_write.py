"""Mock transport tests; no USB/device access."""
import contextlib
import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
import device_write as w

ROOT=Path(__file__).resolve().parents[1]


def status(state=2,code=0,delay=10):return bytes([2,code])+delay.to_bytes(3,'little')+bytes([state,0])


class Fake:
    def __init__(self,replies):self.replies=iter(replies);self.sent=[]
    def report(self,rid,size):return b'\x03\x02' if rid==3 else next(self.replies)
    def send(self,raw):self.sent.append(raw)


class WriterTests(unittest.TestCase):
    def test_success_busy_and_manifest_sequence(self):
        h=Fake([status(),status(4),status(5),status(5),status(7),status()]);waits=[]
        w.download(h,b'a'*1100,lambda *a,**k:None,waits.append)
        self.assertEqual(len(h.sent),3)
        self.assertEqual([struct.unpack_from('<BBHH',p) for p in h.sent],[(1,1,0,1017),(1,1,1,83),(1,1,2,0)])
        self.assertTrue(all(len(p)==1023 for p in h.sent))
        self.assertEqual(b''.join(p[6:6+struct.unpack_from('<H',p,4)[0]] for p in h.sent),b'a'*1100)
        self.assertEqual(waits,[0.01,0.01,0.01])

    def test_error_stops_without_resend_or_empty_commit(self):
        h=Fake([status(),status(10,2)])
        with self.assertRaisesRegex(RuntimeError,'DFU error'):w.download(h,b'a'*2100,lambda *a,**k:None,lambda _:None)
        self.assertEqual(len(h.sent),1)

    def test_bad_initial_state_never_sends(self):
        h=Fake([status(10)])
        with self.assertRaisesRegex(RuntimeError,'dfuIDLE'):w.download(h,b'abc',lambda *a,**k:None,lambda _:None)
        self.assertEqual(h.sent,[])

    def test_unexpected_idle_after_data_stops(self):
        h=Fake([status(),status()])
        with self.assertRaisesRegex(RuntimeError,'Unexpected download'):w.download(h,b'a'*2100,lambda *a,**k:None,lambda _:None)
        self.assertEqual(len(h.sent),1)

    def test_target_built_from_readback_and_verified(self):
        before=(ROOT/'firmware_patched/flashing/run_20261107T081225Z/pre_write.upload').read_bytes()
        data,_=w.build(before,'patch')
        after=(ROOT/'firmware_patched/flashing/run_20261107T081225Z/post_write.upload').read_bytes()
        self.assertTrue(w.verify_readback(after,data)['exact_image_match'])
        original=(ROOT/'firmware_bin/aura_studio1_original.bin').read_bytes()
        with self.assertRaisesRegex(RuntimeError,'Readback'):w.verify_readback(original,data)

    def apply(self,device_image,*extra):
        h=Fake([status()]*4);h.finish=lambda:None
        saved=w.WriteHID,w.upload_image,sys.argv
        w.WriteHID=lambda:h;w.upload_image=lambda *_:device_image
        try:
            with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
                sys.argv=['w','patch','--apply','--output',str(Path(tmp)/'run'),*extra]
                code=w.main();summary=json.loads((Path(tmp)/'run/summary.json').read_text())
        finally:w.WriteHID,w.upload_image,sys.argv=saved
        return code,summary,h.sent

    def test_apply_refuses_before_any_send(self):
        run='firmware_patched/flashing/run_20261107T081225Z/'
        written=(ROOT/run/'post_write.upload').read_bytes()
        code,summary,sent=self.apply(written)
        self.assertEqual((code,sent,summary['write_attempted']),(1,[],False));self.assertIn('already patched',summary['error'])
        code,summary,sent=self.apply(written,'--dump',str(ROOT/run/'pre_write.upload'))
        self.assertEqual((code,sent),(1,[]));self.assertIn('differs from --dump',summary['error'])


if __name__=='__main__':unittest.main()
