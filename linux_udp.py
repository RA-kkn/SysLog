"""Optional native Linux recvmmsg adapter; no compiler/runtime installation side effects."""
import ctypes as C
import errno
from pathlib import Path
import select
import socket
import sys
from shared_packets import HEADER

STRIDE = 65535 + HEADER.size

class BatchReceiver:
    def __init__(self, sock, batch_size=64):
        if sys.platform != 'linux':
            raise NotImplementedError('recvmmsg requires Linux')
        if not 1 <= batch_size <= 256:
            raise ValueError('UDP_BATCH_SIZE must be between 1 and 256')
        try:
            self.lib = C.CDLL(str(Path(__file__).with_name('_udp_batch.so')), use_errno=True)
        except OSError as exc:
            raise NotImplementedError('Build _udp_batch.so to enable recvmmsg') from exc
        lib=self.lib
        lib.udp_batch_new.argtypes=[C.c_uint];lib.udp_batch_new.restype=C.c_void_p
        lib.udp_batch_free.argtypes=[C.c_void_p];lib.udp_batch_free.restype=None
        lib.udp_batch_data.argtypes=[C.c_void_p];lib.udp_batch_data.restype=C.c_void_p
        lib.udp_batch_lengths.argtypes=[C.c_void_p];lib.udp_batch_lengths.restype=C.POINTER(C.c_uint)
        lib.udp_batch_receive.argtypes=[C.c_void_p,C.c_int];lib.udp_batch_receive.restype=C.c_int
        self.handle=lib.udp_batch_new(batch_size)
        if not self.handle:raise MemoryError('Cannot allocate UDP batch buffers')
        self.sock=sock
        try:
            self.lengths=lib.udp_batch_lengths(self.handle)
            address=lib.udp_batch_data(self.handle)
            self.buffers=[(C.c_ubyte*STRIDE).from_address(address+i*STRIDE) for i in range(batch_size)]
            self.views=[memoryview(b).cast('B') for b in self.buffers]
        except BaseException:
            lib.udp_batch_free(self.handle);self.handle=None;raise

    def receive(self):
        count=self.lib.udp_batch_receive(self.handle,self.sock.fileno())
        if count<0:
            error=C.get_errno()
            if error in (errno.EAGAIN,errno.EWOULDBLOCK,errno.EINTR):
                select.select([self.sock],[],[],.01)
                return 0,0
            raise OSError(error,'recvmmsg failed')
        # Timestamp is already encoded by the native batch reader.
        return count, HEADER.unpack_from(self.views[0])[3] if count and self.lengths[0] else 0

    def encoded(self,i):
        size=self.lengths[i]
        if size<HEADER.size or size>STRIDE:raise ValueError('Invalid/truncated datagram')
        return self.views[i],size

    def packet(self,i,packed=False):
        view,length=self.encoded(i)
        ip,port,size,stamp=HEADER.unpack_from(view)
        return view[HEADER.size:],size,(ip if packed else socket.inet_ntoa(ip),port)

    def close(self):
        for view in self.views:view.release()
        if self.handle:self.lib.udp_batch_free(self.handle);self.handle=None
