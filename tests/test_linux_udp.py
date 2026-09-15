import multiprocessing as mp
import os
import socket
import sys
import time
import unittest
from collections import Counter
from unittest.mock import patch
from linux_udp import BatchReceiver
from shared_packets import SharedPacketRing, BLOCK_BYTES
from udp_receive import receive_loop

@unittest.skipUnless(sys.platform == 'linux', 'Linux syscall integration')
class LinuxBatchTests(unittest.TestCase):
    def test_order_boundaries_empty_and_maximum(self):
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock, socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1048576)
            sock.bind(('127.0.0.1',0))
            receiver=BatchReceiver(sock,8)
            try:
                payloads=[b'',b'one',b'\xff\x00',b'x'*65507]
                before=time.time_ns()
                for raw in payloads: sender.sendto(raw,sock.getsockname())
                count,stamp=receiver.receive()
                self.assertEqual(count,4)
                self.assertTrue(before<=stamp<=time.time_ns())
                for i,raw in enumerate(payloads):
                    view,size,address=receiver.packet(i)
                    self.assertEqual(bytes(view[:size]),raw)
                    self.assertEqual(address, sender.getsockname() if sender.getsockname()[0]!='0.0.0.0' else ('127.0.0.1',sender.getsockname()[1]))
                self.assertEqual(receiver.receive()[0],0)
                sender.sendto(b'reused',sock.getsockname())
                self.assertEqual(receiver.receive()[0],1)
                self.assertEqual(bytes(receiver.packet(0)[0][:6]),b'reused')
            finally:receiver.close()

    def test_saturated_batch_counts_and_drains(self):
        ctx=mp.get_context('spawn');ring=SharedPacketRing(ctx,1,BLOCK_BYTES)
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock, socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
            sock.bind(('127.0.0.1',0));stop=ctx.Event();metrics=Counter()
            for _ in range(5):sender.sendto(b'x',sock.getsockname())
            original=BatchReceiver.receive
            def receive(receiver):
                result=original(receiver);stop.set();return result
            try:
                with patch.object(BatchReceiver,'receive',receive),patch.dict(os.environ,UDP_RECEIVE_MODE='recvmmsg'):
                    receive_loop(sock,ring,stop,metrics)
                self.assertEqual((metrics['received'],metrics['queued'],metrics['dropped_queue']),(5,1,4))
                self.assertEqual(ring.get()[0],b'x')
            finally:ring.close()

    def test_internal_error_accounts_entire_received_batch(self):
        ctx=mp.get_context('spawn');ring=SharedPacketRing(ctx,64,BLOCK_BYTES)
        metrics=Counter()
        try:
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock, patch.object(BatchReceiver,'receive',return_value=(5,time.time_ns())), patch.object(BatchReceiver,'encoded',side_effect=ValueError('truncated')), patch.dict(os.environ,UDP_RECEIVE_MODE='recvmmsg'):
                with self.assertRaises(ValueError):receive_loop(sock,ring,ctx.Event(),metrics)
            self.assertEqual(metrics['received'],5)
            self.assertEqual(metrics['dropped_transport'],5)
        finally:ring.close()

    def test_idle_shutdown(self):
        import threading
        ctx=mp.get_context('spawn');ring=SharedPacketRing(ctx,64,BLOCK_BYTES)
        stop=ctx.Event()
        try:
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock, patch.dict(os.environ,UDP_RECEIVE_MODE='recvmmsg'):
                sock.bind(('127.0.0.1',0))
                errors=[]
                def run():
                    try:receive_loop(sock,ring,stop,Counter())
                    except BaseException as exc:errors.append(exc)
                thread=threading.Thread(target=run)
                thread.start();time.sleep(.03);stop.set();thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors,[])
        finally:ring.close()

    def test_unsupported_syscall_falls_back(self):
        import errno
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            with patch.object(BatchReceiver,'receive',side_effect=OSError(errno.ENOSYS,'unsupported')),patch('udp_receive.receive_fallback') as fallback,patch.dict(os.environ,UDP_RECEIVE_MODE='auto'):
                receive_loop(sock,mp.get_context('spawn').Queue(1),mp.get_context('spawn').Event(),Counter())
                fallback.assert_called_once()

if __name__=='__main__':unittest.main()
