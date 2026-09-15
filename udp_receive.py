"""Dependency-free UDP hot path and bounded IPC factory."""
import os
import queue
import socket
import time
from shared_packets import SharedPacketRing


def create_transport(context, capacity):
    mode = os.getenv('RECEIVER_IPC','shared').lower()
    if mode == 'queue':
        return context.Queue(maxsize=capacity)
    if mode != 'shared':
        raise ValueError('RECEIVER_IPC must be shared or queue')
    budget = int(os.getenv('IPC_BUFFER_BYTES','536870912'))
    # Small development queues do not need a 512 MiB allocation.
    size = min(budget,max(65554,capacity*2066))
    return SharedPacketRing(context,capacity,size)


def receive_fallback(sock, packets, stop, metrics):
    buffer = bytearray(65535)
    view = memoryview(buffer)
    shared = isinstance(packets,SharedPacketRing)
    next_flush = time.time_ns()+2_000_000
    received = queued = dropped = 0
    base = {key:metrics[key] for key in ('received','queued','dropped_queue')}
    def publish():
        metrics['received'] = base['received']+received
        metrics['queued'] = base['queued']+queued
        metrics['dropped_queue'] = base['dropped_queue']+dropped
    try:
        while True:
            if received % 64 == 0 and stop.is_set():
                break
            try:
                size,address = sock.recvfrom_into(buffer)
                stamp = time.time_ns()
            except socket.timeout:
                if shared: packets.flush()
                publish()
                if stop.is_set():
                    break
                continue
            received += 1
            try:
                if shared:
                    packets.put_buffer(view,size,address,stamp)
                else:
                    packets.put_nowait((bytes(view[:size]),address[0],address[1],stamp))
                queued += 1
            except queue.Full:
                dropped += 1
            except Exception:
                metrics['dropped_transport'] += 1
                raise
            if shared and stamp >= next_flush:
                packets.flush()
                next_flush = stamp+2_000_000
            if received % 256 == 0:
                publish()
    finally:
        if shared: packets.flush()
        publish()
        view.release()


def receive_loop(sock, packets, stop, metrics):
    import errno
    import logging
    from linux_udp import BatchReceiver
    mode = os.getenv('UDP_RECEIVE_MODE', 'auto').lower()
    batch_size = int(os.getenv('UDP_BATCH_SIZE', '64'))
    if mode not in ('auto', 'recvmmsg', 'recvfrom') or not 1 <= batch_size <= 256:
        raise ValueError('Invalid UDP_RECEIVE_MODE or UDP_BATCH_SIZE (1..256)')
    if mode == 'recvfrom' or not isinstance(sock, socket.socket):
        return receive_fallback(sock, packets, stop, metrics)
    try:
        receiver = BatchReceiver(sock, batch_size)
    except (NotImplementedError, AttributeError):
        if mode == 'recvmmsg':
            raise
        logging.getLogger(__name__).warning('recvmmsg unavailable; using recvfrom_into')
        return receive_fallback(sock, packets, stop, metrics)
    handed_off = False
    metrics['receive_mode'] = 'recvmmsg'
    metrics['receive_batch_size'] = batch_size
    shared = isinstance(packets, SharedPacketRing)
    next_flush = time.monotonic_ns() + 2_000_000
    received = queued = dropped = 0
    base = {key: metrics[key] for key in ('received', 'queued', 'dropped_queue')}
    def publish():
        metrics['received'] = base['received'] + received
        metrics['queued'] = base['queued'] + queued
        metrics['dropped_queue'] = base['dropped_queue'] + dropped
    try:
        while not stop.is_set():
            try:
                count, stamp = receiver.receive()
            except OSError as exc:
                if mode == 'auto' and exc.errno in (errno.ENOSYS, errno.EPERM, errno.EOPNOTSUPP):
                    publish()
                    logging.getLogger(__name__).warning('recvmmsg rejected (%s); using recvfrom_into', exc)
                    metrics['receive_mode'] = 'recvfrom_into'
                    handed_off = True
                    return receive_fallback(sock, packets, stop, metrics)
                raise
            metrics['receive_calls'] += 1
            metrics['receive_max_batch'] = max(metrics['receive_max_batch'], count)
            received += count
            for i in range(count):
                try:
                    if shared:
                        view, size = receiver.encoded(i)
                        packets.put_encoded(view, size)
                    else:
                        view, size, address = receiver.packet(i)
                        packets.put_nowait((bytes(view[:size]), address[0], address[1], stamp))
                    queued += 1
                except queue.Full:
                    dropped += 1
                except Exception:
                    # All remaining datagrams already left the kernel: account
                    # for them before failing rather than silently losing a tail.
                    metrics['dropped_transport'] += count-i
                    raise
            now = time.monotonic_ns()
            if shared and (not count or now >= next_flush):
                packets.flush()
                next_flush = now + 2_000_000
            publish()  # Once per syscall/batch, not per packet.
    finally:
        try:
            if shared:
                packets.flush()
        finally:
            if not handed_off:
                publish()
            receiver.close()
