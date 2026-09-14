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


def receive_loop(sock, packets, stop, metrics):
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
