"""Local Queue-vs-ring benchmarks; never binds production :514 or uses a DB."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
import multiprocessing as mp
from pathlib import Path
import queue
import socket
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shared_packets import SharedPacketRing
from udp_receive import receive_loop
from udp_health import read_udp, deltas


def consumer(packets, result):
    result.send('ready')
    start=time.process_time();count=0
    while packets.get() is not None:
        count+=1
    result.send(dict(count=count,cpu_seconds=time.process_time()-start))
    result.close()


def legacy_receive(sock,packets,stop,metrics):
    while not stop.is_set():
        try:
            data,(ip,port)=sock.recvfrom(65535)
            stamp=datetime.now(timezone.utc)
            metrics['received']+=1
            try:packets.put_nowait((data,ip,port,stamp));metrics['queued']+=1
            except queue.Full:metrics['dropped_queue']+=1
        except socket.timeout:pass


def receiver(mode, packets, stop, result):
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,33554432)
    sock.settimeout(.1);sock.bind(('127.0.0.1',0))
    result.send(dict(port=sock.getsockname()[1],rcvbuf=sock.getsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF)))
    metrics=Counter();started=time.process_time()
    if mode=='receiver':
        buffer=bytearray(65535);count=0
        while not stop.is_set():
            try:
                sock.recvfrom_into(buffer);time.time_ns();count+=1
            except socket.timeout:pass
        metrics['received']=count
    elif isinstance(packets,SharedPacketRing):receive_loop(sock,packets,stop,metrics)
    else:legacy_receive(sock,packets,stop,metrics)
    sock.close()
    result.send(dict(metrics,cpu_seconds=time.process_time()-started))
    result.close()
    # This producer owns the Queue feeder. Its process must stay alive until
    # published packets have reached the pipe, even after the socket closes.
    if packets is not None and not isinstance(packets,SharedPacketRing):
        packets.close();packets.join_thread()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--transport',choices=('queue','shared'),default='shared')
    p.add_argument('--mode',choices=('ipc','receiver','udp'),default='ipc')
    p.add_argument('--packets',type=int,default=100000)
    p.add_argument('--bytes',type=int,default=512)
    p.add_argument('--workers',type=int,default=4)
    p.add_argument('--capacity',type=int,default=8192)
    p.add_argument('--rate',type=int,default=25000,help='UDP offered rate; 0 means unpaced')
    p.add_argument('--receiver',choices=('auto','recvfrom','recvmmsg'),default='auto')
    p.add_argument('--burst',type=int,default=32,help='Packets per paced burst')
    p.add_argument('--output',type=Path,help='New JSON result file (never overwrites)')
    args=p.parse_args()
    os.environ['UDP_RECEIVE_MODE']=args.receiver
    if not 1 <= args.burst <= 100000:p.error('burst must be 1..100000')
    if not (1<=args.bytes<=65507 and 1<=args.packets<=10000000 and 1<=args.workers<=32 and 1<=args.capacity<=2000000 and args.rate>=0):
        p.error('Invalid benchmark bounds')
    ctx=mp.get_context('spawn');workers=[];readers=[];rx=None
    packets=None if args.mode=='receiver' else (SharedPacketRing(ctx,args.capacity,max(65554,args.capacity*2066))
            if args.transport=='shared' else ctx.Queue(args.capacity))
    try:
        if packets is not None:
            for _ in range(args.workers):
                read,write=ctx.Pipe(False)
                process=ctx.Process(target=consumer,args=(packets,write));process.start();write.close()
                workers.append(process);readers.append(read)
            for read in readers:assert read.recv()=='ready'
        payload=b'x'*args.bytes
        kernel_before=read_udp()
        started=time.perf_counter();cpu_start=time.process_time()
        if args.mode=='ipc':
            for _ in range(args.packets):
                if args.transport=='shared':
                    packets.put_buffer(memoryview(payload),len(payload),('192.0.2.1',514),time.time_ns(),True)
                else:packets.put((payload,'192.0.2.1',514,datetime.now(timezone.utc)))
            receiver_result=dict(received=args.packets,queued=args.packets,dropped_queue=0)
        else:
            read,write=ctx.Pipe(False);stop=ctx.Event()
            rx=ctx.Process(target=receiver,args=(args.mode,packets,stop,write));rx.start();write.close()
            info=read.recv();started=time.perf_counter()
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
                for n in range(args.packets):
                    sender.sendto(payload,('127.0.0.1',info['port']))
                    if args.rate and (n+1)%args.burst==0:
                        delay=started+(n+1)/args.rate-time.perf_counter()
                        if delay>0:time.sleep(delay)
            offered_seconds=time.perf_counter()-started
            time.sleep(.25);stop.set()
            if not read.poll(30):raise RuntimeError('Receiver did not finish')
            receiver_result=read.recv();receiver_result.update(info)
            rx.join(30)
            if rx.exitcode!=0:raise RuntimeError('Receiver did not shut down cleanly')
        # Producers must finish before publishing markers from this process.
        if packets is not None:
            for _ in workers:packets.put(None)
        results=[]
        for read in readers:
            if not read.poll(30):raise RuntimeError('Consumer did not drain')
            results.append(read.recv())
        for process in workers:process.join(10)
        elapsed=time.perf_counter()-started
        main_cpu=time.process_time()-cpu_start
        receiver_cpu=receiver_result.get('cpu_seconds',main_cpu)
        if args.mode=='ipc' and args.transport=='queue':
            packets.close();packets.join_thread()
            receiver_cpu=time.process_time()-cpu_start
        consumed=sum(row['count'] for row in results)
        result=dict(mode=args.mode,transport=args.transport,receiver=args.receiver,burst=args.burst,offered=args.packets,bytes=args.bytes,workers=args.workers,
            capacity=args.capacity,received=receiver_result['received'],consumed=consumed if packets else None,
            dropped_queue=receiver_result.get('dropped_queue',0),elapsed_seconds=round(elapsed,3),
            drained_eps=round((consumed if packets else receiver_result['received'])/elapsed),
            receiver_cpu_seconds=round(receiver_cpu,3),receiver_cpu_percent=round(receiver_cpu/elapsed*100,1),
            consumer_cpu_seconds=round(sum(row['cpu_seconds'] for row in results),3),
            per_consumer=[row['count'] for row in results],kernel_delta=deltas(read_udp(),kernel_before,elapsed),
            effective_rcvbuf=receiver_result.get('rcvbuf'),receive_calls=receiver_result.get('receive_calls'),receive_max_batch=receiver_result.get('receive_max_batch'))
        if args.mode!='ipc':result.update(offered_eps=round(args.packets/offered_seconds),offered_seconds=round(offered_seconds,3))
        print(json.dumps(result,indent=2))
        if args.output:
            with args.output.open('x',encoding='utf-8') as output:json.dump(result,output,indent=2)
        assert all(process.exitcode==0 for process in workers)
        if packets is not None:assert consumed+result['dropped_queue']==result['received']
    finally:
        for process in workers+([rx] if rx else []):
            if process.is_alive():process.terminate();process.join()
        if packets is not None:packets.close()


if __name__=='__main__':main()
