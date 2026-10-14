import hashlib
import time
import unittest
from pathlib import Path

import test_app
from server import Store
from provider import GenerationError


class DashboardTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown
    request = test_app.AppTests.request
    body = test_app.AppTests.body
    create = test_app.AppTests.create
    wait_job = test_app.AppTests.wait_job

    def owner(self):
        key = 'dashboard-owner-test-key'
        self.store.admin_hash = hashlib.sha256(key.encode()).hexdigest()
        response = self.request('/api/admin/login', 'POST', {'key': key})
        self.assertEqual(response[0], 200)
        return response[2]['Set-Cookie'].split(';')[0]

    def invite(self, owner):
        invite = self.request('/api/admin/invitations', 'POST', {'label': 'Аня'}, cookie=owner)[1]
        self.assertEqual(self.request('/api/invite', 'POST', {'code': invite['token']})[0], 200)
        return invite

    def test_no_consent_required_and_owner_only_archive(self):
        owner = self.owner(); self.invite(owner)
        body = self.body(); body.pop('consent')
        job = self.create(body); self.wait_job(job['id'])
        for path in ('dashboard', 'generations', f"generations/{job['id']}/person", f"generations/{job['id']}/garment", f"generations/{job['id']}/result"):
            self.assertEqual(self.request('/api/admin/'+path)[0], 401)
            self.assertEqual(self.request('/api/admin/'+path, cookie=owner)[0], 200)
        value = self.request('/api/admin/dashboard', cookie=owner)[1]
        self.assertEqual(value['totals'], dict(users=1, generations=1, successful=1, errors=0, pending=0))
        self.assertEqual(value['users'][0]['label'], 'Аня')
        self.assertGreater(value['users'][0]['average_seconds'], 0)
        self.assertGreater(value['users'][0]['last_generation'], 0)
        archive = self.request('/api/admin/generations', cookie=owner)[1]
        self.assertEqual(archive['jobs'][0]['has_result'], 1)
        self.assertNotIn('person', archive['jobs'][0])

    def test_invite_aggregates_browsers_and_errors_without_averaging_failures(self):
        owner = self.owner(); invite = self.invite(owner)
        job = self.create(); self.wait_job(job['id'])
        _, _, headers = self.request('/api/session', cookie='')
        second = headers['Set-Cookie'].split(';')[0]
        self.request('/api/invite', 'POST', {'code': invite['token']}, cookie=second)
        def fail(*args): raise GenerationError('Test failure')
        self.store.provider = fail
        status, failed, _ = self.request('/api/jobs', 'POST', self.body('test_request_second_browser'), cookie=second)
        self.assertEqual(status, 202)
        for _ in range(100):
            if self.request('/api/jobs/'+failed['id'], cookie=second)[1]['status'] == 'error': break
            time.sleep(.025)
        data = self.request('/api/admin/dashboard', cookie=owner)[1]
        user = data['users'][0]
        self.assertEqual((data['totals']['users'], user['generations'], user['successful'], user['errors']), (1, 2, 1, 1))
        success = self.request('/api/admin/generations?status=success&user='+user['user_id'], cookie=owner)[1]
        self.assertEqual(success['total'], 1)
        self.assertAlmostEqual(user['average_seconds'], success['jobs'][0]['wait_elapsed'])
        failure = self.request('/api/admin/generations?status=error', cookie=owner)[1]['jobs'][0]
        self.assertEqual(failure['error'], 'Test failure')
        self.assertEqual(failure['has_person'], 1)
        self.assertEqual(failure['has_result'], 0)
        self.assertEqual(self.request('/api/admin/generations/'+failed['id']+'/result', cookie=owner)[0], 404)

    def test_archive_survives_session_expiry_deletion_and_restart(self):
        owner = self.owner(); self.invite(owner)
        first = self.create(); self.wait_job(first['id'])
        self.request('/api/jobs/'+first['id']+'/feedback', 'POST', {'rating':'helpful'})
        with self.store.db() as db: db.execute('UPDATE jobs SET created=0 WHERE id=?', (first['id'],))
        self.assertEqual(self.request('/api/jobs/'+first['id'])[0], 404)
        second = self.create(self.body('test_request_second_generation')); self.wait_job(second['id'])
        self.request('/api/photos', 'DELETE')
        for job in (first, second):
            self.assertEqual(self.request('/api/jobs/'+job['id'])[0], 404)
            self.assertEqual(self.request('/api/admin/generations/'+job['id']+'/result', cookie=owner)[0], 200)
        self.store.stop.set(); self.store.worker.join(2)
        new = Store(self.store.path, self.store.provider, admin_hash=self.store.admin_hash)
        try:
            self.assertEqual(new.dashboard()['totals']['successful'], 2)
            self.assertEqual(new.archive()['total'], 2)
            self.assertEqual(next(j for j in new.archive()['jobs'] if j['id']==first['id'])['feedback'], 'helpful')
        finally: new.stop.set(); new.worker.join(2)

    def test_delete_inflight_keeps_owner_result(self):
        owner = self.owner(); self.invite(owner)
        self.release.clear(); job = self.create()
        for _ in range(100):
            if self.calls: break
            time.sleep(.01)
        self.request('/api/photos', 'DELETE')
        self.release.set()
        for _ in range(100):
            rows = self.store.archive()['jobs']
            if rows[0]['status']=='success': break
            time.sleep(.025)
        self.assertEqual(rows[0]['status'], 'success')
        self.assertEqual(self.request('/api/admin/generations/'+job['id']+'/result', cookie=owner)[0], 200)
        self.assertEqual(self.request('/api/jobs/'+job['id'])[0], 404)

    def test_old_database_backfill_and_pagination(self):
        job = self.create(); self.wait_job(job['id'])
        self.store.stop.set(); self.store.worker.join(2)
        with self.store.db() as db: db.execute('DROP TABLE generation_archive')
        new = Store(self.store.path)
        try:
            self.assertEqual(new.archive()['total'], 1)
            self.assertEqual(new.archive()['jobs'][0]['has_result'], 1)
            self.assertIsNone(new.dashboard()['users'][0]['average_seconds'])
            with new.db() as db:
                for n in range(25):
                    db.execute('INSERT INTO generation_archive (id,user_id,status,created) VALUES (?,?,?,?)',
                               (f'{n:032x}', 'b'*64, 'error', n))
            self.assertEqual(len(new.archive()['jobs']), 20)
            self.assertEqual(len(new.archive(offset=20)['jobs']), 6)
            self.assertEqual(new.archive(user_id='b'*64, status='error')['total'], 25)
        finally: new.stop.set(); new.worker.join(2)

    def test_admin_on_separate_api_host_and_invalid_filters(self):
        self.server._frontend_origin='https://fitme.lol'
        owner = self.owner()
        self.assertEqual(self.request('/api/admin/dashboard', cookie=owner)[0], 200)
        for query in ('offset=-1', 'offset=no', 'user=wrong', 'status=bad'):
            self.assertEqual(self.request('/api/admin/generations?'+query, cookie=owner)[0], 400)
        with self.store.db() as db: db.execute('UPDATE admin_sessions SET expires=0')
        self.assertEqual(self.request('/api/admin/dashboard', cookie=owner)[0], 401)
        self.assertEqual(self.request('/api/admin/generations', cookie=owner)[0], 401)

    def test_past_attempts_count_without_inventing_lost_statuses(self):
        owner = self.owner(); self.invite(owner)
        with self.store.db() as db:
            sid = db.execute('SELECT id FROM sessions WHERE participant IS NOT NULL').fetchone()[0]
            db.execute('INSERT INTO attempts VALUES (?,?)', (sid, 1000))
        info = self.store.dashboard()
        self.assertEqual(info['totals']['generations'], 1)
        self.assertEqual(info['totals']['successful'], 0)
        self.assertEqual(info['users'][0]['last_generation'], 1000)
        self.assertIsNone(info['users'][0]['average_seconds'])
        self.assertEqual(self.store.archive()['total'], 0)

    def test_cancel_queued_archive_is_not_stuck_pending(self):
        owner = self.owner(); self.invite(owner)
        self.store.stop.set(); self.store.worker.join(2)
        job = self.create()
        self.request('/api/photos', 'DELETE')
        self.assertEqual(self.store.dashboard()['totals']['pending'], 0)
        self.assertEqual(self.store.archive()['jobs'][0]['status'], 'error')
        self.assertEqual(self.request('/api/admin/generations/'+job['id']+'/person', cookie=owner)[0], 200)

    def test_prompt_snapshot_and_no_duplicate_archive_for_same_request(self):
        owner = self.owner(); self.invite(owner)
        self.store.provider.model = 'test-model'
        self.store.provider.prompt = 'Test prompt v1'
        job = self.create(); self.wait_job(job['id']); self.create()
        jobs = self.store.archive()['jobs']
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['model'], 'test-model')
        self.assertEqual(jobs[0]['prompt'], 'Test prompt v1')

    def test_invited_active_time_deduplicates_events_and_overlapping_tabs(self):
        from unittest.mock import patch
        owner = self.owner(); self.invite(owner)
        with self.store.db() as db:
            sid = db.execute('SELECT id FROM sessions WHERE participant IS NOT NULL').fetchone()[0]
        def event(visit, seq, seconds, now):
            with patch('server.time.time', return_value=now):
                return self.store.activity(sid, {'visit_id':visit,'sequence':seq,'seconds':seconds})
        event('a'*32, 1, 10, 1000000000)
        event('a'*32, 1, 10, 1000000000)
        event('b'*32, 1, 10, 1000000000)
        event('a'*32, 2, 30, 1000000030)
        event('b'*32, 2, 30, 1000000030)
        info = self.store.dashboard()
        self.assertEqual(info['engagement']['average_active_seconds'], 40)
        self.assertEqual(info['engagement']['measured_users'], 1)
        self.assertEqual(info['users'][0]['visits'], 2)
        self.assertEqual(info['users'][0]['active_seconds'], 40)

    def test_activity_rejects_invalid_and_ignores_noninvited_or_owner(self):
        owner=self.owner()
        body={'visit_id':'a'*32,'sequence':1,'seconds':10}
        self.assertFalse(self.request('/api/activity','POST',body)[1]['tracked'])
        self.invite(owner)
        for changes in ({'seconds':46},{'seconds':-1},{'seconds':True},{'sequence':0},{'visit_id':'invalid'}):
            self.assertEqual(self.request('/api/activity','POST',{**body,**changes})[0],400)
        self.assertEqual(self.request('/api/activity','POST',body,origin=False)[0],403)
        self.assertTrue(self.request('/api/activity','POST',body)[1]['tracked'])
        self.assertEqual(self.store.dashboard()['engagement']['measured_users'],1)
        with self.store.db() as db:
            db.execute('UPDATE sessions SET participant=?',(self.store.admin_hash,))
        self.assertFalse(self.request('/api/activity','POST',{**body,'sequence':2})[1]['tracked'])

    def test_engagement_denominators_and_feedback_quality(self):
        owner = self.owner(); self.invite(owner)
        info = self.store.dashboard()['engagement']
        self.assertEqual(info['invited_users'], 1)
        self.assertIsNone(info['average_active_seconds'])
        first = self.create(); self.wait_job(first['id'])
        self.request('/api/jobs/'+first['id']+'/feedback','POST',{'rating':'helpful'})
        second = self.create(self.body('test_request_repeat_for_metrics')); self.wait_job(second['id'])
        info=self.store.dashboard()['engagement']
        self.assertEqual(info['activated_users'],1)
        self.assertEqual(info['repeat_users'],1)
        self.assertEqual(info['success_rate'],1)
        self.assertEqual((info['rated'],info['helpful']),(1,1))
