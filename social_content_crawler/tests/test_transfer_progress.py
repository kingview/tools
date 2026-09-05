import json

import pytest

from social_content_crawler.errors import CrawlerError
from social_content_crawler.transfer_progress import TransferReporter


def test_reports_safe_transfer_state_and_log(tmp_path):
    reporter = TransferReporter(tmp_path, 'execution-123')
    reporter.report(dict(status='finished',filename='/private/source/video.mp4',downloaded_bytes=100,total_bytes=100))
    state = json.loads(reporter.path.read_text())
    assert state['filename'] == 'video.mp4'
    assert state['files_completed'] == 1
    assert '/private/source' not in reporter.log.read_text()


def test_revoked_grant_stops_background_downloader(tmp_path):
    grant = tmp_path/'policy.json'
    grant.write_text(json.dumps({'execution_id':'execution-123'}))
    reporter = TransferReporter(tmp_path, 'execution-123', grant)
    reporter.check_active()
    grant.unlink()
    with pytest.raises(CrawlerError, match='授权已撤销'):
        reporter.check_active()


def test_replaced_grant_cannot_keep_old_transfer_alive(tmp_path):
    grant = tmp_path/'policy.json'
    grant.write_text(json.dumps({'execution_id':'other-execution'}))
    with pytest.raises(CrawlerError):
        TransferReporter(tmp_path,'execution-123',grant).check_active()


def test_transfer_context_flows_into_backend_thread(tmp_path, monkeypatch):
    import asyncio
    from social_content_crawler.diagnostics import diagnostic_context
    from social_content_crawler.transfer_progress import transfer_scope, report_transfer
    monkeypatch.setenv('SOCIAL_AGENT_STATE_ROOT', str(tmp_path))
    monkeypatch.delenv('SOCIAL_AGENT_EXECUTION_POLICY_PATH',raising=False)
    async def work():
        with diagnostic_context(execution_id='execution-123'), transfer_scope():
            await asyncio.to_thread(report_transfer, {'status':'finished','filename':'file.mp4','downloaded_bytes':4,'total_bytes':4})
    asyncio.run(work())
    item = json.loads((tmp_path/'transfer-progress/execution-123.json').read_text())
    assert item['status'] == 'completed' and item['files_completed'] == 1
