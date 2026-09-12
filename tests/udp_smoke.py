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

def child(stop, output):
    import listener
    class Sink:
        def insert(self,table,rows,column_names,**kwargs):
            with open(output,'a') as file:
                for row in rows:
                    file.write(json.dumps(dict(table=table,row=dict(zip(column_names,row))),default=str)+'\n')
    listener.get_client=lambda:Sink()
    listener.worker_main(0,stop)

def wait_for(check, seconds=8):
    deadline=time.time()+seconds
    while time.time()<deadline:
        if check():return
        time.sleep(.1)
    raise AssertionError('Timed out waiting for UDP pipeline')

def main():
    with tempfile.TemporaryDirectory() as folder:
        os.environ['CONFIG_DB_PATH']=str(Path(folder)/'devices.db')
        os.environ['DATA_DIR']=str(Path(folder)/'data')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        os.environ['LISTEN_HOST']='127.0.0.1';os.environ['LISTEN_PORT']=str(port)
        import device_store
        from benchmark import payload
        output=Path(folder)/'inserted.jsonl'
        heartbeat=Path(folder)/'data'/'listener-0.json'
        stop=mp.Event();process=mp.Process(target=child,args=(stop,str(output)))
        process.start()
        def applied():
            return heartbeat.exists() and json.loads(heartbeat.read_text()).get('authorization_hash')==device_store.authorization_hash()
        try:
            wait_for(lambda:heartbeat.exists())
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
                sock.sendto(payload(1),('127.0.0.1',port))
                wait_for(lambda:device_store.counts()['pending']==1)
                assert not output.exists(),'Pending router was ingested'
                device_store.set_status('127.0.0.1','approved');wait_for(applied)
                sock.sendto(payload(2),('127.0.0.1',port))
                wait_for(lambda:output.exists() and len(output.read_text().splitlines())==1)
                inserted=json.loads(output.read_text().splitlines()[0])
                assert inserted['table']=='nat_sessions_v2'
                assert inserted['row']['private_port']==1026
                sock.sendto(b'unknown format',('127.0.0.1',port))
                sock.sendto(b'proto ICMP private_ip=10.0.0.1',('127.0.0.1',port))
                wait_for(lambda:len(output.read_text().splitlines())==3)
                device_store.set_status('127.0.0.1','blocked');wait_for(applied)
                sock.sendto(payload(3),('127.0.0.1',port))
                wait_for(lambda:device_store.list_devices()[0]['denied_attempts']==2)
                assert len(output.read_text().splitlines())==3,'Blocked router was ingested'
        finally:
            stop.set();process.join(timeout=40)
            if process.is_alive():
                process.terminate();process.join()
                raise AssertionError('Listener did not shut down')
        assert process.exitcode==0
        stats=json.loads(heartbeat.read_text())
        assert stats['received']==5 and stats['denied']==2 and stats['inserted']==3,stats
        assert stats['dropped_queue']==stats['dropped_spool']==stats['dropped_processing']==0,stats
        assert all(json.loads(line)['table']=='nat_sessions_v2' for line in output.read_text().splitlines())
        print('PASS: real UDP pending -> manual approve -> NAT parse -> durable batch -> insert sink -> block -> graceful shutdown. ClickHouse not exercised.')

if __name__=='__main__':main()
