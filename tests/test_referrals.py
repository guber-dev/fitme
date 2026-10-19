"""Referral awards and profile counters: paid quota must never leak or double-credit."""
import hashlib
import unittest
import test_app


class ReferralTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown
    request = test_app.AppTests.request
    body = test_app.AppTests.body
    wait_job = test_app.AppTests.wait_job

    def inviter(self):
        code = 'referral-profile-owner-test'
        self.store.invite_hashes = {hashlib.sha256(code.encode()).hexdigest()}
        self.assertEqual(self.request('/api/invite', 'POST', {'code': code})[0], 200)
        status, link, _ = self.request('/api/referral-link', 'POST', {})
        self.assertEqual(status, 201)
        return link['token']

    def fresh_cookie(self):
        return self.request('/api/session', cookie='')[2]['Set-Cookie'].split(';')[0]

    def test_profile_award_once_and_no_guest_bonus_leak(self):
        token = self.inviter()
        friend = self.fresh_cookie()
        self.assertEqual(self.request('/api/referral', 'POST', {'code': token}, cookie=friend)[0], 200)
        info = self.request('/api/session')[1]
        self.assertEqual((info['referral_invited'], info['referral_activated'], info['referral_bonus']), (1, 0, 0))
        body = self.body()
        status, job, _ = self.request('/api/jobs', 'POST', body, cookie=friend)
        self.assertEqual(status, 202)
        # Repeating both acceptance and the original generation request cannot pay twice.
        self.assertEqual(self.request('/api/referral', 'POST', {'code': token}, cookie=friend)[0], 200)
        self.assertEqual(self.request('/api/jobs', 'POST', body, cookie=friend)[1]['id'], job['id'])
        info = self.request('/api/session')[1]
        self.assertEqual((info['referral_invited'], info['referral_activated'], info['referral_bonus'], info['remaining']), (1, 1, 5, 10))
        guest = self.request('/api/session', cookie='')[1]
        self.assertEqual((guest['referral_bonus'], guest['referral_invited'], guest['remaining']), (0, 0, 0))
        from invitations import listing
        listing_info = listing(self.store)
        self.assertEqual(listing_info['total_limit'], 30)
        self.assertEqual(listing_info['referrals'][0]['activated'], 1)
        self.assertEqual(self.request('/api/admin/referrals')[0], 401)
        self.assertEqual(self.store.dashboard()['engagement']['invited_users'], 2)

    def test_no_reward_for_rejected_generation_or_existing_user(self):
        token = self.inviter()
        self.assertEqual(self.request('/api/referral', 'POST', {'code': token})[0], 400)
        friend = self.fresh_cookie()
        self.request('/api/referral', 'POST', {'code': token}, cookie=friend)
        self.assertEqual(self.request('/api/jobs', 'POST', {'request_id': 'invalid'}, cookie=friend)[0], 400)
        self.assertEqual(self.request('/api/session')[1]['referral_bonus'], 0)
        # Someone who already generated cannot attribute themselves retroactively.
        code = 'existing-referral-user-test'
        self.store.invite_hashes.add(hashlib.sha256(code.encode()).hexdigest())
        existing = self.fresh_cookie()
        self.request('/api/invite', 'POST', {'code': code}, cookie=existing)
        self.assertEqual(self.request('/api/jobs', 'POST', self.body(), cookie=existing)[0], 202)
        self.assertEqual(self.request('/api/referral', 'POST', {'code': token}, cookie=existing)[0], 409)

    def test_open_mode_guest_cannot_spend_other_users_bonus(self):
        token = self.inviter()
        friend = self.fresh_cookie()
        self.request('/api/referral', 'POST', {'code': token}, cookie=friend)
        self.request('/api/jobs', 'POST', self.body(), cookie=friend)
        self.store.invite_hashes.clear()
        self.store.session_limit = 1
        guest = self.fresh_cookie()
        self.assertEqual(self.request('/api/session', cookie=guest)[1]['remaining'], 1)
        job = self.request('/api/jobs', 'POST', self.body(), cookie=guest)[1]
        original_cookie = self.cookie
        self.cookie = guest
        self.wait_job(job['id'])
        self.cookie = original_cookie
        self.assertEqual(self.request('/api/jobs', 'POST', self.body('second_request_0001'), cookie=guest)[0], 429)
