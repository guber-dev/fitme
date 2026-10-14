import base64
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from server import AppServer, Store, Problem, decode_image
from provider import GeminiProvider, GenerationError, usage_record


def fixture(color='green'):
    output = io.BytesIO()
    Image.new('RGB', (300, 400), color).save(output, 'JPEG')
    return output.getvalue()


def data_url(color='green'):
    return 'data:image/jpeg;base64,' + base64.b64encode(fixture(color)).decode()


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []
        self.release = threading.Event()
        self.release.set()

        def fake_provider(person, garment):
            self.calls.append((person, garment))
            self.release.wait(5)
            return fixture('blue'), {'promptTokenCount': 123}

        self.store = Store(Path(self.temp.name)/'test.db', fake_provider)
        self.server = AppServer(('127.0.0.1', 0), self.store, 'http://127.0.0.1')
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.server.origin = self.origin
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _, _, headers = self.request('/api/session')
        self.cookie = headers['Set-Cookie'].split(';')[0]

    def tearDown(self):
        self.release.set()
        self.store.stop.set()
        self.store.worker.join(6)
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, method='GET', body=None, cookie=None, origin=True):
        headers = {'Content-Type': 'application/json', 'X-Fitme-Request': '1'}
        if origin:
            headers['Origin'] = self.origin
        if cookie is not None:
            headers['Cookie'] = cookie
        elif hasattr(self, 'cookie'):
            headers['Cookie'] = self.cookie
        req = urllib.request.Request(self.origin+path, method=method,
              data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            response = urllib.request.urlopen(req)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            value = json.loads(raw) if 'application/json' in response.headers.get('Content-Type', '') else raw
            return response.status, value, response.headers

    def body(self, rid='test_request_00000001'):
        return {'request_id': rid, 'person': data_url(), 'garment': data_url('red'), 'consent': True, 'product_url': ''}

    def create(self, body=None):
        code, job, _ = self.request('/api/jobs', 'POST', body or self.body())
        self.assertEqual(code, 202, job)
        return job

    def wait_job(self, jid):
        for _ in range(100):
            _, job, _ = self.request('/api/jobs/'+jid)
            if job['status'] not in ('queued', 'running'):
                return job
            time.sleep(.025)
        self.fail('Job did not complete')

    def test_invitation_required_and_quota_survives_cookie_reset(self):
        import hashlib
        code = 'private-test-invitation-01'
        self.store.invite_hashes = {hashlib.sha256(code.encode()).hexdigest()}
        self.store.session_limit = 1
        status, _, _ = self.request('/api/jobs', 'POST', self.body())
        self.assertEqual(status, 403)
        status, info, _ = self.request('/api/invite', 'POST', {'code': code})
        self.assertEqual(status, 200)
        self.assertTrue(info['invited'])
        job = self.create()
        self.wait_job(job['id'])
        _, _, fresh = self.request('/api/session', cookie='')
        new_cookie = fresh['Set-Cookie'].split(';')[0]
        status, info, _ = self.request('/api/invite', 'POST', {'code': code}, cookie=new_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(info['remaining'], 0)
        status, _, _ = self.request('/api/jobs/'+job['id']+'/result', cookie=new_cookie)
        self.assertEqual(status, 404)
        status, _, _ = self.request('/api/jobs', 'POST', self.body('test_request_00000002'), cookie=new_cookie)
        self.assertEqual(status, 429)
        self.request('/api/photos', 'DELETE')
        _, info, _ = self.request('/api/session', cookie=new_cookie)
        self.assertEqual(info['remaining'], 0)
        self.assertEqual(len(self.calls), 1)

    def test_invite_rejects_wrong_codes_and_identity_switch(self):
        import hashlib
        codes = ['test-invitation-first', 'test-invitation-second']
        self.store.invite_hashes = {hashlib.sha256(c.encode()).hexdigest() for c in codes}
        status, _, _ = self.request('/api/invite', 'POST', {'code': 'wrong-invitation-code'})
        self.assertEqual(status, 403)
        self.assertEqual(self.request('/api/invite', 'POST', {'code': codes[0]})[0], 200)
        self.assertEqual(self.request('/api/invite', 'POST', {'code': codes[1]})[0], 409)
        self.store.invite_hashes.remove(hashlib.sha256(codes[0].encode()).hexdigest())
        self.assertEqual(self.request('/api/jobs', 'POST', self.body())[0], 403)
        self.assertEqual(len(self.calls), 0)

    def test_rate_limit_blocks_repeated_invite_guesses(self):
        import hashlib
        self.store.invite_hashes = {hashlib.sha256(b'test-invitation-code').hexdigest()}
        for _ in range(12):
            self.assertEqual(self.request('/api/invite', 'POST', {'code': 'bad-test-invitation'})[0], 403)
        self.assertEqual(self.request('/api/invite', 'POST', {'code': 'bad-test-invitation'})[0], 429)
        self.assertEqual(self.request('/api/session')[0], 200)

    def test_global_remaining_and_health_endpoint(self):
        self.store.total_limit = 1
        job = self.create()
        self.wait_job(job['id'])
        _, info, _ = self.request('/api/session', cookie='')
        self.assertEqual(info['remaining'], 0)
        status, value, headers = self.request('/healthz', cookie='')
        self.assertEqual((status, value), (200, {'ok': True}))
        self.assertIsNone(headers.get('Set-Cookie'))

    def test_full_flow_and_feedback(self):
        job = self.create()
        result = self.wait_job(job['id'])
        self.assertEqual(result['status'], 'success')
        code, blob, headers = self.request('/api/jobs/'+job['id']+'/result')
        self.assertEqual(code, 200)
        self.assertEqual(Image.open(io.BytesIO(blob)).size, (300,400))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        code, _, _ = self.request('/api/jobs/'+job['id']+'/feedback', 'POST', {'rating':'helpful'})
        self.assertEqual(code, 200)
        _, info, _ = self.request('/api/session')
        self.assertEqual(info['remaining'], 4)
        self.assertEqual(info['jobs'][0]['feedback'], 'helpful')
        self.assertEqual(len(self.calls), 1)

    def test_idempotent_submission_and_conflict(self):
        first = self.create()
        second = self.create()
        self.assertEqual(first['id'], second['id'])
        self.wait_job(first['id'])
        self.assertEqual(len(self.calls), 1)
        changed = self.body(); changed['garment'] = data_url('yellow')
        code, _, _ = self.request('/api/jobs', 'POST', changed)
        self.assertEqual(code, 409)

    def test_other_session_cannot_read_or_delete_images(self):
        job = self.create(); self.wait_job(job['id'])
        _, _, headers = self.request('/api/session', cookie='fitme_session=another')
        other = headers['Set-Cookie'].split(';')[0]
        for suffix in ['', '/result', '/person', '/garment']:
            code, _, _ = self.request('/api/jobs/'+job['id']+suffix, cookie=other)
            self.assertEqual(code, 404)
        self.request('/api/photos', 'DELETE', cookie=other)
        self.assertEqual(self.request('/api/jobs/'+job['id'])[0], 200)

    def test_mutations_require_same_origin(self):
        self.assertEqual(self.request('/api/jobs', 'POST', self.body(), origin=False)[0], 403)
        self.assertEqual(self.request('/api/photos', 'DELETE', origin=False)[0], 403)
        self.assertEqual(len(self.calls), 0)

    def test_invalid_images_and_link_do_not_charge(self):
        for change in [{'person': 'not an image'}, {'garment': 'data:image/jpeg;base64,bm90YW5pbWFnZQ=='}, {'product_url': 'javascript:alert(1)'}]:
            body = self.body(); body.update(change)
            self.assertEqual(self.request('/api/jobs', 'POST', body)[0], 400)
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(self.request('/api/session')[1]['remaining'], 5)

    def test_disabled_generation_never_calls_provider(self):
        self.store.provider = None
        self.assertEqual(self.request('/api/jobs', 'POST', self.body())[0], 503)
        self.assertEqual(len(self.calls), 0)

    def test_second_job_blocked_during_first(self):
        self.release.clear()
        job = self.create()
        self.assertEqual(self.request('/api/jobs', 'POST', self.body('test_request_00000002'))[0], 409)
        self.release.set(); self.wait_job(job['id'])

    def test_delete_inflight_does_not_resurrect_or_reset_quota(self):
        self.release.clear(); job = self.create()
        for _ in range(100):
            if self.calls: break
            time.sleep(.01)
        self.assertEqual(self.request('/api/photos', 'DELETE')[0], 200)
        self.release.set(); time.sleep(.1)
        self.assertEqual(self.request('/api/jobs/'+job['id'])[0], 404)
        self.assertEqual(self.request('/api/session')[1]['remaining'], 4)

    def test_total_cap_and_session_cap(self):
        self.store.session_limit = 1
        job = self.create(); self.wait_job(job['id'])
        self.assertEqual(self.request('/api/jobs', 'POST', self.body('test_request_00000002'))[0], 429)
        self.store.session_limit = 5; self.store.total_limit = 1
        self.assertEqual(self.request('/api/jobs', 'POST', self.body('test_request_00000003'))[0], 429)

    def test_provider_failure_is_not_retried(self):
        def fail(*args):
            self.calls.append(args)
            raise GenerationError('Provider failed')
        self.store.provider = fail
        job = self.create(); self.assertEqual(self.wait_job(job['id'])['status'], 'error')
        self.create(); time.sleep(.3)
        self.assertEqual(len(self.calls), 1)

    def test_restart_preserves_results_and_does_not_repeat_queued_job(self):
        job = self.create(); self.wait_job(job['id'])
        self.store.stop.set(); self.store.worker.join(2)
        queued = self.create(self.body('test_request_00000002'))
        new = Store(self.store.path, self.store.provider)
        try:
            sid, _ = new.session(self.cookie.split('=',1)[1])
            self.assertEqual(new.get(sid, job['id'])['status'], 'success')
            self.assertEqual(new.get(sid, queued['id'])['status'], 'error')
            self.assertEqual(len(self.calls), 1)
        finally:
            new.stop.set(); new.worker.join(2)

    def test_retention_and_static_allowlist(self):
        job = self.create(); self.wait_job(job['id'])
        with self.store.db() as db:
            db.execute('UPDATE jobs SET created=0')
        self.assertEqual(self.request('/api/jobs/'+job['id'])[0], 404)
        for path in ['/.env', '/server.py', '/data/fitme.sqlite3', '/../../server.py']:
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request('/')[0], 200)


class ProviderTests(unittest.TestCase):
    def test_rest_contract_and_no_person_data_in_prompt(self):
        response = {'candidates':[{'content':{'parts':[{'inlineData':{'mimeType':'image/jpeg','data':base64.b64encode(fixture()).decode()}}]}}], 'usageMetadata':{'promptTokenCount':2}}
        stream = io.BytesIO(json.dumps(response).encode())
        with patch('urllib.request.urlopen', return_value=stream) as request:
            blob, usage = GeminiProvider('test-key')(fixture(), fixture('red'))
        req = request.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(len(payload['contents'][0]['parts']), 3)
        self.assertNotIn('grey', payload['contents'][0]['parts'][0]['text'])
        self.assertEqual(usage['tokens']['promptTokenCount'], 2)
        self.assertEqual(blob, fixture())
        self.assertNotIn('test-key', req.full_url)

    def test_refusal_is_explicit_error(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"candidates":[]}')):
            with self.assertRaises(GenerationError):
                GeminiProvider('test-key')(fixture(), fixture())

    def test_cost_accounts_for_thinking_and_missing_usage(self):
        usage = {'promptTokenCount': 1000, 'candidatesTokenCount': 1200, 'thoughtsTokenCount': 100,
                 'candidatesTokensDetails': [{'modality':'IMAGE', 'tokenCount':1120}]}
        self.assertAlmostEqual(usage_record('gemini-3.1-flash-image', usage)['estimated_usd'], .06824)
        self.assertIsNone(usage_record('gemini-3.1-flash-image', {})['estimated_usd'])


if __name__ == '__main__':
    unittest.main()
