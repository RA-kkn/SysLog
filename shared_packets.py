"""Bounded shared-memory packet blocks; synchronize once per block, not per packet."""
import ctypes
import queue
import socket
import struct
from datetime import datetime, timezone

HEADER = struct.Struct('!4sHIQ')  # IPv4, source port, length, UTC nanoseconds
END = 0xffffffff
BLOCK_BYTES = 65536 + HEADER.size


class SharedPacketRing:
    """One producer, multiple consumers. Each consumer owns a block until read.

    Blocks are published FIFO; consumers may process their blocks concurrently.
    A block holds at most 64 packets. Both packet count and allocated bytes are
    bounded. No feeder, pickling, Manager, per-packet lock or named-memory cleanup.
    """
    def __init__(self, context, capacity, byte_capacity):
        if capacity < 1 or byte_capacity < BLOCK_BYTES:
            raise ValueError('Positive capacity and >=65554 IPC bytes required')
        self.capacity = capacity
        self.batch_limit = min(64,capacity)
        self.blocks = min(capacity//self.batch_limit,byte_capacity//BLOCK_BYTES)
        self.byte_capacity = self.blocks*BLOCK_BYTES
        self._data = context.RawArray(ctypes.c_ubyte,self.byte_capacity)
        self._free_ids = context.RawArray(ctypes.c_uint32,range(self.blocks))
        self._ready_ids = context.RawArray(ctypes.c_uint32,self.blocks)
        self._remaining = context.RawArray(ctypes.c_uint32,self.blocks)
        self._free_top = context.RawValue(ctypes.c_uint32,self.blocks)
        self._ready_head = context.RawValue(ctypes.c_uint32,0)
        self._ready_tail = context.RawValue(ctypes.c_uint32,0)
        self._lock = context.Lock()
        self._free = context.Semaphore(self.blocks)
        self._ready = context.Semaphore(0)
        self._reset_local()

    def _reset_local(self):
        self._view = None
        self._write_block = self._read_block = None
        self._write_pos = self._write_count = self._read_pos = self._read_left = 0

    def __getstate__(self):
        state = self.__dict__.copy()
        for key in ('_view','_write_block','_read_block'): state[key] = None
        for key in ('_write_pos','_write_count','_read_pos','_read_left'): state[key] = 0
        return state

    def _buffer(self):
        if self._view is None:
            self._view = memoryview(self._data).cast('B')
        return self._view

    def _start_block(self,block,timeout):
        if not self._free.acquire(block,timeout):
            raise queue.Full
        with self._lock:
            self._free_top.value -= 1
            self._write_block = self._free_ids[self._free_top.value]
        self._write_pos = self._write_count = 0

    def flush(self):
        if self._write_block is None:
            return
        if not self._write_count:
            with self._lock:
                top = self._free_top.value
                self._free_ids[top] = self._write_block
                self._free_top.value = top+1
            self._write_block = None
            self._free.release()
            return
        with self._lock:
            tail = self._ready_tail.value
            self._ready_ids[tail] = self._write_block
            self._ready_tail.value = (tail+1)%self.blocks
        self._write_block = None
        self._ready.release()

    def put_buffer(self,source,size,address,received_ns,block=False,timeout=None):
        if size != END and not 0 <= size <= 65535:
            raise ValueError('Invalid datagram length')
        if size != END and len(source)<size:
            raise ValueError('Buffer is shorter than datagram')
        if not 0 <= address[1] <= 65535 or not 0 <= received_ns < 2**64:
            raise ValueError('Invalid packet metadata')
        packed_ip = socket.inet_aton(address[0])
        needed = HEADER.size+(0 if size==END else size)
        if self._write_block is not None and (self._write_pos+needed>BLOCK_BYTES or size==END):
            self.flush()
        if self._write_block is None:
            self._start_block(block,timeout)
        position = self._write_block*BLOCK_BYTES+self._write_pos
        view = self._buffer()
        HEADER.pack_into(view,position,packed_ip,address[1],size,received_ns)
        if size!=END:
            view[position+HEADER.size:position+needed] = source[:size]
        self._write_pos += needed
        self._write_count += 1
        self._remaining[self._write_block] = self._write_count
        if self._write_count==self.batch_limit or size==END:
            self.flush()

    def put_encoded(self, source, length):
        """Trusted native receiver header + payload; same bounded block format."""
        if not HEADER.size <= length <= HEADER.size+65535 or len(source)<length:
            raise ValueError('Invalid encoded datagram length')
        if self._write_block is not None and self._write_pos+length>BLOCK_BYTES:
            self.flush()
        if self._write_block is None:
            self._start_block(False,None)
        position=self._write_block*BLOCK_BYTES+self._write_pos
        self._buffer()[position:position+length]=source[:length]
        self._write_pos+=length
        self._write_count+=1
        self._remaining[self._write_block]=self._write_count
        if self._write_count==self.batch_limit:
            self.flush()

    def put(self,packet,block=True,timeout=None):
        if packet is None:
            return self.put_buffer(b'',END,('0.0.0.0',0),0,block,timeout)
        raw,ip,port,stamp = packet
        if isinstance(stamp,datetime):
            stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
            delta = stamp-datetime(1970,1,1,tzinfo=timezone.utc)
            stamp = ((delta.days*86400+delta.seconds)*1_000_000+delta.microseconds)*1000
        return self.put_buffer(memoryview(raw),len(raw),(ip,port),stamp,block,timeout)

    def put_nowait(self,packet):
        return self.put(packet,False)

    def get(self,block=True,timeout=None):
        if self._read_block is None:
            # Supports same-process round-trip tests; consumer copies have no
            # producer block. Production publishes on batch/latency/shutdown.
            self.flush()
            if not self._ready.acquire(block,timeout):
                raise queue.Empty
            with self._lock:
                head = self._ready_head.value
                self._read_block = self._ready_ids[head]
                self._ready_head.value = (head+1)%self.blocks
            self._read_pos = 0
            self._read_left = self._remaining[self._read_block]
        position = self._read_block*BLOCK_BYTES+self._read_pos
        view = self._buffer()
        ip,port,size,stamp = HEADER.unpack_from(view,position)
        raw = None if size==END else bytes(view[position+HEADER.size:position+HEADER.size+size])
        self._read_pos += HEADER.size+(0 if size==END else size)
        self._read_left -= 1
        self._remaining[self._read_block] = self._read_left
        if not self._read_left:
            with self._lock:
                top = self._free_top.value
                self._free_ids[top] = self._read_block
                self._free_top.value = top+1
            self._read_block = None
            self._free.release()
        return None if raw is None else (raw,socket.inet_ntoa(ip),port,stamp)

    def qsize(self):
        # Approximate telemetry only. Each aligned counter has one owner:
        # producer until publication, then the consumer which owns that block.
        return sum(self._remaining)

    def used_bytes(self):
        return sum(bool(count) for count in self._remaining)*BLOCK_BYTES

    def close(self):
        if self._view is not None:
            self._view.release()
            self._view = None

    def join_thread(self):
        pass

    def cancel_join_thread(self):
        pass


def received_datetime(nanoseconds):
    if isinstance(nanoseconds,datetime):
        return nanoseconds
    seconds,micros = divmod(nanoseconds//1000,1_000_000)
    return datetime.fromtimestamp(seconds,timezone.utc).replace(microsecond=micros)
