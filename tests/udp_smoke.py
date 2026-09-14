"""Three real UDP packets; isolated SQLite, ClickHouse insert replaced by a file sink."""
import json
import multiprocessing as mp
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def sink_worker(worker_id, packets, run_id):
    import listener
    from unittest.mock import patch
    output=Path(os.environ['UDP_TEST_OUTPUT'])/f'inserted-{worker_id}.jsonl'
    class Sink:
        def insert(self,table,rows,column_names,**kwargs):
            with output.open('a') as file:
                for row in rows:
                    file.write(json.dumps(dict(table=table,row=dict(zip(column_names,row))),default=str)+'\n')
    listener.get_client=lambda:Sink()
    original_parser=listener.route_syslog
    def delayed_parser(*args):
        time.sleep(.005)  # Deterministically leave backlog for the shutdown test.
        return original_parser(*args)
    listener.route_syslog=delayed_parser
    # A parser worker must never create a UDP (or any network) socket in this
    # isolated sink test. Only the parent receiver can bind the listening port.
    with patch('listener.socket.socket',side_effect=AssertionError('Consumer created a socket')):
        listener.worker_main(worker_id,packets,run_id)


def child(stop):
    import listener
    listener.run_listener(stop,worker_target=sink_worker)


def wait_for(check, seconds=8):
    deadline=time.time()+seconds
    while time.time()<deadline:
        if check():return
        time.sleep(.1)
    raise AssertionError('Timed out waiting for UDP pipeline')

def main():
    with tempfile.TemporaryDirectory() as folder:
        os.environ['NUM_WORKERS']='2'
        os.environ['QUEUE_SIZE']='10000'
        os.environ['BATCH_MAX_ROWS']='100'
        os.environ['BATCH_MAX_SECONDS']='0.2'
        os.environ['UDP_TEST_OUTPUT']=folder
        os.environ['CONFIG_DB_PATH']=str(Path(folder)/'devices.db')
        os.environ['DATA_DIR']=str(Path(folder)/'data')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        os.environ['LISTEN_HOST']='127.0.0.1';os.environ['LISTEN_PORT']=str(port)
        import device_store
        from benchmark import payload
        def inserted_rows():
            return [json.loads(line) for file in Path(folder).glob('inserted-*.jsonl') for line in file.read_text().splitlines()]
        def worker_stats():
            return [json.loads(file.read_text()) for file in (Path(folder)/'data').glob('listener-*.json')]
        heartbeat=Path(folder)/'data'/'listener-0.json'
        stop=mp.Event();process=mp.Process(target=child,args=(stop,))
        process.start()
        def applied():
            return len(worker_stats())==2 and all(row.get('authorization_hash')==device_store.authorization_hash() for row in worker_stats())
        try:
            wait_for(lambda:len(worker_stats())==2 and (Path(folder)/'data'/'receiver.json').exists())
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as duplicate:
                try:duplicate.bind(('127.0.0.1',port))
                except OSError:pass
                else:raise AssertionError('A second UDP listener could bind')
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
                sock.sendto(payload(1),('127.0.0.1',port))
                wait_for(lambda:device_store.counts()['pending']==1)
                assert not inserted_rows(),'Pending router was ingested'
                device_store.set_status('127.0.0.1','approved');wait_for(applied)
                sock.sendto(payload(2),('127.0.0.1',port))
                wait_for(lambda:len(inserted_rows())==1)
                inserted=inserted_rows()[0]
                assert inserted['table']=='nat_sessions_v2'
                assert inserted['row']['private_port']==1026
                sock.sendto(b'unknown format',('127.0.0.1',port))
                sock.sendto(b'proto ICMP private_ip=10.0.0.1',('127.0.0.1',port))
                wait_for(lambda:len(inserted_rows())==3)
                device_store.set_status('127.0.0.1','blocked');wait_for(applied)
                sock.sendto(payload(3),('127.0.0.1',port))
                wait_for(lambda:device_store.list_devices()[0]['denied_attempts']==2)
                assert len(inserted_rows())==3,'Blocked router was ingested'
                device_store.set_status('127.0.0.1','approved');wait_for(applied)
                for n in range(1200):
                    sock.sendto(payload(n),('127.0.0.1',port))
                    if n%50==0:time.sleep(.002)
                wait_for(lambda:json.loads((Path(folder)/'data'/'receiver.json').read_text())['received']==1205)
                assert json.loads((Path(folder)/'data'/'receiver.json').read_text())['queue_size']>0
                # Stop with work potentially queued; FIFO markers must drain it.
        finally:
            stop.set();process.join(timeout=40)
            if process.is_alive():
                process.terminate();process.join()
                raise AssertionError('Listener did not shut down')
        assert process.exitcode==0
        stats=json.loads((Path(folder)/'data'/'receiver.json').read_text())
        workers=worker_stats()
        assert stats['received']==1205 and stats['queued']==1205 and stats['dropped_queue']==0,stats
        assert sum(w['denied'] for w in workers)==2
        assert sum(w['parsed'] for w in workers)==1203
        assert sum(w['spooled'] for w in workers)==1203
        assert all(w['consumed']>0 for w in workers),workers
        assert all(w['dropped_spool']==w['dropped_processing']==0 for w in workers),workers
        # Inserted or still durable after graceful shutdown; nothing discarded.
        import sqlite3
        from contextlib import closing
        pending=0
        for spool in (Path(folder)/'data').glob('spool-*.db'):
            with closing(sqlite3.connect(spool)) as c:
                pending+=sum(len(json.loads(row[0])) for row in c.execute('SELECT payload FROM batches'))
        assert len(inserted_rows())+pending==1203
        assert all(row['table']=='nat_sessions_v2' for row in inserted_rows())
        print('PASS: one UDP owner, two socket-free consumers, 1205 received / 2 denied / 1203 parsed and durably spooled, shutdown drain and zero queue/processing/spool drops. ClickHouse insert is a file sink.')

if __name__=='__main__':main()
