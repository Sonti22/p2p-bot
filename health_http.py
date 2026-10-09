"""Optional in-process health HTTP server. Standalone server has no bot heartbeat.

HEALTH_CHECK_PORT=0 disables bot listener. Requests use Authorization: Bearer.
Only explicit successful scans make /health ready; HTTP uptime alone is not enough.
"""
import argparse
import asyncio
import logging
import os
import secrets
import time
from aiohttp import web

logger = logging.getLogger(__name__)
_metrics_store = {}


def start_metrics():
    _metrics_store.clear()
    _metrics_store.update(uptime=time.time(), last_scan=None, last_scan_duration=None,
                          scan_count=0, scan_errors={}, version='unknown', commit_hash=None,
                          is_update_pending=False, paper_cycles=0, paper_balance=None)


def set_metric(key, value):
    _metrics_store[key] = value


def clear_metric(key):
    _metrics_store.pop(key, None)


def update_scan_status(duration, errors):
    _metrics_store.update(last_scan=time.time(), last_scan_duration=duration,
                          scan_count=_metrics_store.get('scan_count',0)+1,
                          scan_errors={str(k):time.time() for k in errors})


def get_status():
    now=time.time()
    start=_metrics_store.get('uptime')
    last=_metrics_store.get('last_scan')
    return {'uptime_seconds':max(0,now-start) if start is not None else None,
            'last_scan':last, 'last_scan_age':now-last if last is not None else None,
            'last_scan_duration':_metrics_store.get('last_scan_duration'),
            'scan_count':_metrics_store.get('scan_count',0),
            'scan_errors':dict(_metrics_store.get('scan_errors',{})),
            'version':_metrics_store.get('version','unknown'),
            'paper_cycles':_metrics_store.get('paper_cycles',0),
            'paper_balance':str(_metrics_store['paper_balance']) if _metrics_store.get('paper_balance') is not None else None}


class MetricsCollector:
    def __init__(self):
        self._counters,self._gauges={},{}
    def increment(self,name,value=1):
        self._counters[name]=self._counters.get(name,0)+value
    def set_gauge(self,name,value):
        self._gauges[name]=value
    def to_prometheus(self):
        return ''.join(f'{key} {value}\n' for values in (self._counters,self._gauges) for key,value in values.items())


collector=MetricsCollector()


@web.middleware
async def protect(request,handler):
    expected=request.app['password']
    supplied=request.headers.get('Authorization','')
    authorized=not expected or secrets.compare_digest(supplied.encode(),('Bearer '+expected).encode())
    if authorized:
        response=await handler(request)
    else:
        response=web.Response(status=401,text='Unauthorized')
    # Query strings and headers may contain secrets; never log them.
    path=request.path if request.path in ('/health','/status','/metrics','/') else '<unknown>'
    logger.info('health request %s %s status=%d',request.method,path,response.status)
    return response


async def handle_health(request):
    s=get_status()
    age=s['last_scan_age']
    ready=s['uptime_seconds'] is not None and age is not None and 0<=age<=300
    return web.json_response({'ready':ready,'last_scan_age':age},status=200 if ready else 503)


async def handle_status(request):
    return web.json_response(get_status())


async def handle_metrics(request):
    if not request.app['prometrics']:
        return web.Response(status=404,text='Disabled')
    s=get_status()
    text=collector.to_prometheus()+f"bot_scan_count {s['scan_count']}\n"
    if s['last_scan_age'] is not None:
        text+=f"bot_last_scan_age_seconds {s['last_scan_age']}\n"
    return web.Response(text=text,content_type='text/plain')


async def handle_root(request):
    return web.Response(text='/health /status /metrics')


def create_app(password=None,prometrics=False):
    app=web.Application(middlewares=[protect])
    app['password']=password
    app['prometrics']=prometrics
    app.router.add_get('/health',handle_health)
    app.router.add_get('/status',handle_status)
    app.router.add_get('/metrics',handle_metrics)
    app.router.add_get('/',handle_root)
    return app


async def init_app(port=None):
    port=int(os.environ.get('HEALTH_CHECK_PORT','8080')) if port is None else port
    if not 0<=port<=65535:
        raise ValueError('Invalid health port')
    host=os.environ.get('HEALTH_CHECK_HOST','127.0.0.1')
    password=os.environ.get('HEALTH_CHECK_PASSWORD') or None
    if host not in ('127.0.0.1','::1') and not password:
        raise ValueError('Public health listener requires password')
    runner=web.AppRunner(create_app(password,os.environ.get('PROMETRICS','0')=='1'),access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner,host,port).start()
    except BaseException:
        await runner.cleanup()
        raise
    logger.info('Health listener started port=%d',port)
    return runner


async def serve(port):
    runner=await init_app(port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=None)
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(serve(args.port))
    except KeyboardInterrupt:
        pass


if __name__=='__main__':
    main()
