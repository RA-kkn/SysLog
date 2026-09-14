import multiprocessing as mp
import queue
import socket
import struct
import time
import unittest
from collections import Counter
from unittest.mock import Mock
from shared_packets import SharedPacketRing, BLOCK_BYTES, received_datetime
from udp_receive import receive_loop


def consume(ring,pipe):
    ids=[]
    while True:
        packet=ring.get(timeout=10)
        if packet is None:break
        raw,ip,port,stamp=packet
        assert ip=='192.0.2.1' and port==514 and stamp==1700000000123456789
        ids.append(struct.unpack('!I',raw[:4])[0])
    pipe.send(ids);pipe.close()


class SharedTests(unittest.TestCase):
    def ring(self,capacity=128,blocks=2):
        ring=SharedPacketRing(mp.get_context('spawn'),capacity,blocks*BLOCK_BYTES)
        self.addCleanup(ring.close)
        return ring

    def test_copy_before_buffer_reuse(self):
        ring=self.ring();buffer=bytearray(b'first')
        ring.put_buffer(memoryview(buffer),5,('192.0.2.1',514),1234)
        buffer[:]=b'other'
        self.assertEqual(ring.get(timeout=1),(b'first','192.0.2.1',514,1234))

    def test_packet_and_byte_limits(self):
        ring=self.ring(2,1)
        for _ in range(2):ring.put_buffer(b'x',1,('127.0.0.1',1),1)
        with self.assertRaises(queue.Full):ring.put_buffer(b'x',1,('127.0.0.1',1),1)
        self.assertEqual(ring.qsize(),2)
        ring.get();ring.get()
        self.assertEqual(ring.qsize(),0)
        ring=self.ring(128,1)
        large=b'x'*65507
        ring.put_buffer(large,len(large),('127.0.0.1',1),1)
        with self.assertRaises(queue.Full):ring.put_buffer(large,len(large),('127.0.0.1',1),1)
        self.assertEqual(ring.get()[0],large)

    def test_all_sizes_empty_binary_and_recycling(self):
        ring=self.ring()
        for n in (0,1,512,2048,65507,65535)*10:
            raw=(b'\xff\x00ABC'*(n//5+1))[:n]
            ring.put_buffer(raw,n,('203.0.113.8',65535),1700000000123456789)
            ring.flush()
            self.assertEqual(ring.get(),(raw,'203.0.113.8',65535,1700000000123456789))
        ring.put(None)
        self.assertIsNone(ring.get())

    def test_exact_timestamp_without_float_rounding(self):
        stamp=received_datetime(1700000000999999999)
        self.assertEqual(stamp.microsecond,999999)
        self.assertEqual(stamp.utcoffset().total_seconds(),0)
        self.assertEqual(received_datetime(1700000001000000000).microsecond,0)

    def test_multiple_consumers_no_overwrite_or_missing_ids(self):
        ctx=mp.get_context('spawn');ring=self.ring(512,8)
        children=[];pipes=[]
        try:
            for _ in range(4):
                read,write=ctx.Pipe(False)
                child=ctx.Process(target=consume,args=(ring,write));child.start();write.close()
                children.append(child);pipes.append(read)
            for n in range(4000):
                raw=struct.pack('!I',n)+b'x'*508
                ring.put_buffer(raw,len(raw),('192.0.2.1',514),1700000000123456789,True,10)
            for _ in children:ring.put(None,timeout=10)
            values=[]
            for read in pipes:
                self.assertTrue(read.poll(15));values.extend(read.recv())
            for child in children:child.join(5);self.assertEqual(child.exitcode,0)
            self.assertEqual(sorted(values),list(range(4000)))
            self.assertEqual(ring.qsize(),0)
        finally:
            for child in children:
                if child.is_alive():child.terminate();child.join()

    def test_receive_saturation_and_final_partial_metrics(self):
        ring=self.ring(1,1);sock=Mock();stop=Mock()
        stop.is_set.side_effect=[False,True]
        sequence=iter((b'one',b'two',None))
        def read(buffer):
            raw=next(sequence)
            if raw is None:raise socket.timeout()
            buffer[:len(raw)]=raw
            return len(raw),('192.0.2.1',514)
        sock.recvfrom_into.side_effect=read
        counters=Counter();receive_loop(sock,ring,stop,counters)
        self.assertEqual(dict(counters),dict(received=2,queued=1,dropped_queue=1))
        self.assertEqual(ring.get()[0],b'one')

    def test_partial_block_is_published_on_shutdown(self):
        ring=self.ring();sock=Mock();stop=Mock()
        stop.is_set.side_effect=[False,True]
        def read(buffer):
            buffer[:1]=b'x'
            sock.recvfrom_into.side_effect=socket.timeout()
            return 1,('192.0.2.1',514)
        sock.recvfrom_into.side_effect=read
        counters=Counter();receive_loop(sock,ring,stop,counters)
        self.assertIsNone(ring._write_block)
        self.assertEqual(ring.get()[0],b'x')


if __name__=='__main__':unittest.main()
