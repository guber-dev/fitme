import hashlib
import time
import unittest
import test_app


class InvitationTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown
    request = test_app.AppTests.request
    body = test_app.AppTests.body
    create = test_app.AppTests.create
    wait_job = test_app.AppTests.wait_job

    def owner(self):
        key = 'owner-only-secret-key-for-tests'
        self.store.admin_hash = hashlib.sha256(key.encode()).hexdigest()
        status, _, headers = self.request('/api/admin/login', 'POST', {'key': key})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';')[0]

    def test_authorization_and_no_privilege_from_friend_cookie(self):
        self.owner()
        self.assertEqual(self.request('/api/admin/invitations')[0], 401)
        self.assertEqual(self.request('/api/admin/invitations', 'POST', {'label': 'Friend'})[0], 401)
        self.assertEqual(self.request('/api/admin/login', 'POST', {'key':'bad'})[0], 403)
        self.assertEqual(self.request('/api/admin/login', 'POST', {'key':'bad'}, origin=False)[0], 403)

    def test_link_unlock_quota_shared_and_revocation(self):
        owner = self.owner()
        status, invitation, _ = self.request('/api/admin/invitations', 'POST', {'label':'Friend'}, cookie=owner)
        self.assertEqual(status, 201)
        token=invitation['token']
        self.assertEqual(len(token), 43)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT token_hash FROM invitations').fetchone()[0], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(self.request('/api/invite','POST',{'code':token})[0],200)
        self.store.session_limit=1
        job=self.create();self.wait_job(job['id'])
        _,_,headers=self.request('/api/session',cookie='')
        second=headers['Set-Cookie'].split(';')[0]
        self.assertEqual(self.request('/api/invite','POST',{'code':token},cookie=second)[1]['remaining'],0)
        self.assertEqual(self.request('/api/jobs','POST',self.body('another_request_01'),cookie=second)[0],429)
        listing=self.request('/api/admin/invitations',cookie=owner)[1]
        self.assertEqual(listing['invitations'][0]['used'],1)
        self.assertNotIn(token,str(listing));self.assertNotIn('token_hash',str(listing))
        self.assertEqual(self.request('/api/admin/invitations/'+invitation['id']+'/revoke','POST',{},cookie=owner)[0],200)
        self.assertEqual(self.request('/api/jobs','POST',self.body('another_request_02'))[0],403)
        self.assertEqual(self.request('/api/invite','POST',{'code':token},cookie=second)[0],403)

    def test_expiry_logout_and_key_rotation(self):
        owner=self.owner()
        inv=self.request('/api/admin/invitations','POST',{'label':'Friend'},cookie=owner)[1]
        with self.store.db() as db:db.execute('UPDATE invitations SET expires=?',(time.time()-1,))
        self.assertEqual(self.request('/api/invite','POST',{'code':inv['token']})[0],403)
        self.assertEqual(self.request('/api/admin/logout','POST',{},cookie=owner)[0],200)
        self.assertEqual(self.request('/api/admin/invitations',cookie=owner)[0],401)
        owner=self.owner();self.store.admin_hash=hashlib.sha256(b'rotated').hexdigest()
        self.assertEqual(self.request('/api/admin/invitations',cookie=owner)[0],401)

    def test_admin_expiration_static_no_cookie_and_fixed_global_budget(self):
        owner=self.owner()
        for path in ('/','/admin','/admin.js','/app.js'):
            status,_,headers=self.request(path,cookie='')
            self.assertEqual(status,200);self.assertIsNone(headers.get('Set-Cookie'))
        self.store.total_limit=0
        self.assertEqual(self.request('/api/admin/invitations','POST',{'label':'Friend'},cookie=owner)[0],409)
        with self.store.db() as db: db.execute('UPDATE admin_sessions SET expires=0')
        self.assertEqual(self.request('/api/admin/invitations',cookie=owner)[0],401)
