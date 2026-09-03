"""Real yt-dlp/FFmpeg transfer against loopback; no external browser/network."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading

import pytest
from yt_dlp.utils import DownloadError

from social_content_crawler import backend as bm
from social_content_crawler.contracts import DownloadInput


@pytest.fixture
def local_media(tmp_path):
    clip=tmp_path/'clip.mp4'
    subprocess.run([bm._ffmpeg_executable(),'-nostdin','-y','-f','lavfi','-i','color=c=blue:s=32x32:d=0.3',
                    '-f','lavfi','-i','sine=frequency=400:duration=0.3','-c:v','libx264','-c:a','aac','-shortest',str(clip)],
                   check=True,capture_output=True)
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path=='/missing.png':
                self.send_error(404); return
            content=png if self.path.endswith('.png') else clip.read_bytes()
            self.send_response(200)
            self.send_header('Content-Length',str(len(content)))
            self.send_header('Content-Type','image/png' if self.path.endswith('.png') else 'video/mp4')
            self.end_headers();self.wfile.write(content)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('kind',['best','video','audio','images','partial-images','metadata'])
def test_browser_fallback_transfers_actual_media(monkeypatch,tmp_path,local_media,kind):
    monkeypatch.setenv('SOCIAL_AGENT_LOG_DIR',str(tmp_path/'logs'))
    output=tmp_path/'output';output.mkdir()
    info={'id':'123','extractor':'douyin:browser','extractor_key':'DouyinBrowser','title':'机器鸭',
          'description':'完整的帖子文字','webpage_url':'https://www.douyin.com/video/123','duration':0.3,
          'formats':[{'format_id':'0','url':local_media+'/clip.mp4','ext':'mp4'}]}
    if kind in {'images','partial-images'}:
        info.update(formats=[],thumbnails=[{'id':'1','url':local_media+'/a.png'},{'id':'2','url':local_media+'/b.png'}])
        if kind=='partial-images':
            info['thumbnails'][1]['url']=local_media+'/missing.png'
    monkeypatch.setattr(bm,'extract_from_browser',lambda **kw:info)
    backend=bm.YtDlpBackend()
    def fail(*a,**kw): raise DownloadError('force test fallback')
    monkeypatch.setattr(backend,'_extract_with_browser_fallback',fail)
    request=DownloadInput(urls=[info['webpage_url']],session_ref='sess_douyin_abcdefghijklmnopqrstuvwx',
                          media_format=kind if kind in {'best','video','audio'} else 'best',
                          mode='metadata_only' if kind=='metadata' else 'download')
    if kind=='partial-images':
        with pytest.raises(bm.CrawlerError,match='image download incomplete'):
            backend._run(request,output,None,None,'http://127.0.0.1:12345')
        return
    result=backend._run(request,output,None,None,'http://127.0.0.1:12345')
    assert len(result)==1 and result[0]['description']=='完整的帖子文字'
    files=[p for p in output.iterdir() if p.is_file()]
    if kind=='metadata':
        assert files==[]
    elif kind=='images':
        assert len(files)==2 and all(p.read_bytes().startswith(b'\x89PNG') for p in files)
    else:
        assert len(files)==1 and files[0].stat().st_size>100
        assert files[0].suffix==('.m4a' if kind=='audio' else '.mp4')


def test_duration_limit_cannot_silently_produce_empty_success(monkeypatch,tmp_path,local_media):
    monkeypatch.setenv('SOCIAL_AGENT_LOG_DIR',str(tmp_path/'logs'))
    info={'id':'123','extractor_key':'DouyinBrowser','title':'too long','duration':100,
          'formats':[{'url':local_media+'/clip.mp4','ext':'mp4'}]}
    monkeypatch.setattr(bm,'extract_from_browser',lambda **kw:info)
    options={'quiet':True,'logger':bm._QuietLogger(),'match_filter':bm._download_filter(1),
             'outtmpl':str(tmp_path/'test.%(ext)s')}
    with pytest.raises(DownloadError):
        bm.YtDlpBackend()._douyin_browser_fallback('https://www.douyin.com/video/123',
            DownloadInput(urls=['https://www.douyin.com/video/123']),options,'http://127.0.0.1:12345')
