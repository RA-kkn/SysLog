"""Isolated browser test for receiver/kernel health without changing search fixtures."""
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory() as folder:
        os.environ.update(CONFIG_DB_PATH=str(Path(folder)/'config.db'),DATA_DIR=str(Path(folder)/'data'),COOKIE_SECURE='false')
        import auth
        import search_api
        import uvicorn
        from playwright.sync_api import sync_playwright, expect
        password=secrets.token_urlsafe(20)
        auth.save_user('udp-admin','Test',password,'ADMIN',True,True)
        search_api.query_logs=lambda **kw:dict(results=[],count=0,total_count=0,next_cursor=None)
        search_api.report=lambda:dict(listener='UP',clickhouse='DOWN',api='UP',sqlite='UP',cpu_percent=1,
            ram={'used':1000},storage=None,workers=[dict(pid=12,up=True,consumed=100)],
            receiver=dict(received=101,queued=100,dropped_queue=1,queue_size=70,queue_capacity=100,effective_rcvbuf=67108864,dropped_transport=0),
            kernel_udp=dict(InDatagrams=100,InErrors=73,RcvbufErrors=73,IgnoredMulti=0,MemErrors=0),
            warnings=['Kernel UDP RcvbufErrors increased: packets lost before the listener','Shared packet queue is at least 70% full'])
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(search_api.app,host='127.0.0.1',port=port,log_level='warning'))
        thread=threading.Thread(target=server.run);thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                page=browser.new_page()
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{port}')
                page.fill('#username','udp-admin');page.fill('#password',password)
                page.locator('#loginForm button').click()
                page.locator('[data-page=system]').click()
                expect(page.locator('#kernelStats')).to_contain_text('RcvbufErrors')
                expect(page.locator('#kernelStats')).to_contain_text('73')
                expect(page.locator('#receiverStats')).to_contain_text('70 / 100')
                expect(page.locator('#ingestWarnings')).to_contain_text('70%')
                expect(page.locator('#workers')).to_contain_text('Consumed from shared queue')
                assert not errors,errors
                browser.close()
            print('PASS: Edge receiver counters, kernel RcvbufErrors, congestion warnings, consumer stats; mocked health data')
        finally:
            server.should_exit=True;thread.join(timeout=10)


if __name__=='__main__':main()
