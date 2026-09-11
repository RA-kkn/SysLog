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
        def fake_search(**kwargs):
            return {'results':[dict(timestamp='2026-09-11T01:00:00Z',router_ip='192.0.2.1',kind='events',
                event_type='system',message='<img src=x onerror=alert(1)>')],
                'count':1,'next_cursor':None,'start':'2026-09-11T00:00:00Z','end':'2026-09-12T00:00:00Z'}
        search_api.query_logs=fake_search
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
                page.locator('[data-page=devices]').click()
                page.fill('#manualIP','192.0.2.1');page.locator('#approveForm button').click()
                page.get_by_text('192.0.2.1',exact=True).wait_for()
                assert page.locator('#devices').inner_text().find('approved')>=0
                page.get_by_role('button',name='Rename',exact=True).click()
                page.fill('#confirmInput','Core Router');page.locator('#confirm button[value=ok]').click()
                page.get_by_text('Core Router',exact=True).wait_for()
                page.screenshot(path=str(artifacts/'devices.png'))
                page.get_by_role('button',name='Block',exact=True).click()
                page.locator('#confirm button[value=ok]').click()
                page.locator('#devices .blocked').wait_for()
                page.locator('[data-page=search]').click();page.locator('#searchForm button').click()
                page.get_by_text('<img src=x onerror=alert(1)>',exact=True).wait_for()
                assert page.locator('#logs img').count()==0
                with page.expect_download() as download:
                    page.locator('#export').click()
                assert download.value.suggested_filename=='syslog-export.csv'
                page.locator('[data-page=settings]').click()
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
            print('PASS: real Edge login, manual approval, rename, block, safe text rendering, bounded CSV, branding, tablet, viewer restrictions; ClickHouse search stubbed.')
        finally:
            server.should_exit=True;thread.join(timeout=10)

if __name__=='__main__':main()
