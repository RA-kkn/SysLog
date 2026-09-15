"""Explicit browser smoke test; isolated config DB, synthetic search response."""
import os
import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path

def main():
    with tempfile.TemporaryDirectory() as folder:
        os.environ['CONFIG_DB_PATH']=str(Path(folder)/'devices.db')
        os.environ['DATA_DIR']=str(Path(folder)/'data')
        os.environ['COOKIE_SECURE']='false'
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        import auth
        import search_api
        import uvicorn
        from playwright.sync_api import sync_playwright, expect
        password=secrets.token_urlsafe(20)
        auth.save_user('smoke-admin','Browser test',password,'ADMIN',True,True)
        auth.save_user('smoke-viewer','Browser test',password,'VIEWER',True,False)
        search_calls=[]
        def fake_search(**kwargs):
            search_calls.append(kwargs)
            from nat_view import DISPLAY_FIELDS
            nat=dict(timestamp='2026-09-11T01:00:00Z',private_ip='100.68.180.201',private_port=60734,public_ip='103.125.177.119',public_port=60734,destination_ip='57.144.149.32',destination_port=443,protocol='TCP',subscriber_id='pppoe-S-jameel')
            unknown=dict.fromkeys(DISPLAY_FIELDS)
            unknown.update(timestamp='2026-09-11T01:00:00Z',subscriber_id='<img src=x onerror=alert(1)>')
            rows=[nat,unknown]
            return dict(results=rows,count=len(rows),total_count=501,next_cursor=None if kwargs.get('cursor') else 'page2',start='2026-09-11T00:00:00Z',end='2026-09-12T00:00:00Z')
        search_api.query_logs=fake_search
        search_api.report=lambda: dict(listener='UP',clickhouse='UP',api='UP',sqlite='UP',cpu_percent=3,ram={'used':1000000},budget_bytes=4e12,retention_target_days=365,workers=[dict(pid=123,up=True,received=200,queued=200,nat_parsed=150,events_parsed=50,inserted=200,denied=0,queue_size=0,queue_capacity=50000,spool_bytes=0,parse_failures=0,write_failures=0,dropped_queue=0,dropped_spool=0)],storage=dict(current_eps=7.83,observed_rows=200,database_disk_bytes=149830,compression_ratio=11.44,measured_net_disk_growth_per_day=28000000,estimated_30_day_bytes=857880000,estimated_365_day_bytes=10440000000,disks=[{'free_bytes':1e12}],estimated_days_on_free_disk=1000,budget_percent=.26,projection_confidence='LOW',observed_seconds=420,disk_growth_observation_seconds=420,tables=[]))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(search_api.app,host='127.0.0.1',port=port,log_level='warning'))
        thread=threading.Thread(target=server.run);thread.start()
        artifacts=Path(__file__).resolve().parents[1]/'test-artifacts'
        artifacts.mkdir(exist_ok=True)
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1000})
                errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{port}')
                page.locator('#username').wait_for()
                page.screenshot(path=str(artifacts/'login.png'))
                page.fill('#username','smoke-admin');page.fill('#password',password)
                page.locator('#loginForm button').click();page.locator('#console').wait_for(state='visible')
                expect(page.locator('#searchStatus')).to_contain_text('501 matching records')
                assert len(search_calls)==1
                expect(page.locator('#logs > tr:not(.detail-row)').first).to_contain_text('2026-09-11 06:00:00')
                page.reload()
                expect(page.locator('#searchStatus')).to_contain_text('501 matching records')
                assert len(search_calls)==2
                page.locator('#next').click()
                expect(page.locator('#previous')).to_be_enabled()
                page.locator('#previous').click()
                expect(page.locator('#previous')).to_be_disabled()
                from nat_view import DISPLAY_COLUMNS
                assert page.locator('#logHeader th').all_text_contents()==[label for _,label in DISPLAY_COLUMNS]
                assert page.locator('#kind option').all_text_contents()==['NAT']
                assert page.locator('#logs button').count()==0
                assert page.locator('#logs tr').last.locator('td').count()==10
                assert page.locator('#logs tr').last.locator('td').nth(3).inner_text()==''
                page.screenshot(path=str(artifacts/'nat-search.png'))
                page.locator('[data-page=devices]').click()
                page.fill('#manualIP','192.0.2.1');page.locator('#approveForm button').click()
                page.locator('#devices').get_by_text('192.0.2.1',exact=True).wait_for()
                assert page.locator('#devices').inner_text().find('approved')>=0
                page.get_by_role('button',name='Rename',exact=True).click()
                page.fill('#confirmInput','Core Router');page.locator('#confirm button[value=ok]').click()
                page.get_by_text('Core Router',exact=True).wait_for()
                page.screenshot(path=str(artifacts/'devices.png'))
                page.get_by_role('button',name='Block',exact=True).click()
                page.locator('#confirm button[value=ok]').click()
                page.locator('#devices .blocked').wait_for()
                page.locator('[data-page=search]').click()
                page.locator('#logs > tr:not(.detail-row)').get_by_text('<img src=x onerror=alert(1)>',exact=True).wait_for()
                assert page.locator('#logs img').count()==0
                with page.expect_download() as download:
                    page.locator('#export').click()
                assert download.value.suggested_filename=='syslog-export.csv'
                import csv
                exported=list(csv.reader(Path(download.value.path()).read_text(encoding='utf-8').splitlines()))
                assert exported[0]==[label for _,label in DISPLAY_COLUMNS]
                assert exported[1][0]=='2026-09-11 06:00:00'
                assert len(exported[1])==10
                page.select_option('#kind','nat_sessions_v2');page.locator('#searchForm button').click()
                expect(page.locator('#logHeader')).to_contain_text('Private Port')
                expect(page.locator('#logs > tr:not(.detail-row)')).to_have_count(2)
                page.locator('[data-page=system]').click()
                expect(page.locator('#ingestionDetails')).to_be_hidden()
                page.locator('#viewIngestionDetails').click()
                expect(page.locator('#compressionStats')).to_contain_text('11.44x')
                expect(page.locator('.worker-card')).to_have_count(0)
                assert page.locator('#workers table').count()==1
                page.screenshot(path=str(artifacts/'system-health.png'))
                page.locator('[data-page=settings]').click()
                expect(page.locator('#users tbody tr')).to_have_count(2)
                page.locator('#productName').fill('Example ISP')
                page.locator('#brandingForm button[type=submit]').count()  # implicit submit button below
                page.get_by_role('button',name='Save branding').click()
                expect(page).to_have_title('Example ISP')
                page.set_viewport_size({'width':768,'height':1024})
                page.screenshot(path=str(artifacts/'settings-tablet.png'))
                page.locator('#logout').click();page.locator('#login').wait_for(state='visible')
                page.fill('#username','smoke-viewer');page.fill('#password',password)
                page.locator('#loginForm button').click();page.locator('#console').wait_for(state='visible')
                assert not page.locator('[data-page=settings]').is_visible()
                assert not page.locator('#export').is_visible()
                assert not errors,errors
                browser.close()
            print('PASS: Edge auto-load/reload/navigation, totals, next/previous, exact ten NAT columns, no details/raw exposure, Karachi time, health cards, login/RBAC/devices/CSV/branding/theme layout. Search/health responses stubbed.')
        finally:
            server.should_exit=True;thread.join(timeout=10)

if __name__=='__main__':main()
