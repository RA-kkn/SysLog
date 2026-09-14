"""Opt-in local IPC benchmark, not a UDP/parser/ClickHouse capacity claim."""
import argparse
import json
import multiprocessing as mp
import queue
import time
from datetime import datetime, timezone


def consumer(packets, result):
    count=0
    while packets.get() is not None:
        count+=1
    result.send(count)
    result.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packets',type=int,default=50000)
    parser.add_argument('--consumers',type=int,default=4)
    parser.add_argument('--capacity',type=int,default=500000)
    parser.add_argument('--bytes',type=int,default=512)
    args=parser.parse_args()
    if not (0<args.packets<=1000000 and 0<args.consumers<=16 and 0<args.capacity<=500000 and 0<args.bytes<=65507):
        parser.error('Invalid benchmark bounds')
    ctx=mp.get_context('spawn');packets=ctx.Queue(args.capacity)
    workers=[];results=[]
    for _ in range(args.consumers):
        read,write=ctx.Pipe(duplex=False)
        process=ctx.Process(target=consumer,args=(packets,write))
        process.start();write.close()
        workers.append(process);results.append(read)
    payload=b'x'*args.bytes;drops=0
    started=time.perf_counter()
    for _ in range(args.packets):
        try:packets.put_nowait((payload,'192.0.2.1',514,datetime.now(timezone.utc)))
        except queue.Full:drops+=1
    enqueue_seconds=time.perf_counter()-started
    for _ in workers:packets.put(None)
    counts=[result.recv() for result in results]
    for process in workers:process.join()
    elapsed=time.perf_counter()-started
    packets.close();packets.join_thread()
    assert sum(counts)+drops==args.packets
    assert all(p.exitcode==0 for p in workers)
    print(json.dumps(dict(packets=args.packets,bytes=args.bytes,consumers=args.consumers,
        queue_capacity=args.capacity,consumed=sum(counts),drops=drops,per_consumer=counts,
        enqueue_seconds=round(enqueue_seconds,3),drained_seconds=round(elapsed,3),
        drained_packets_per_second=round(sum(counts)/elapsed)),indent=2))


if __name__=='__main__':main()
