"""Outfit regression checks without HTTP sockets or paid model calls."""
import base64
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from server import Store, Problem, CATEGORIES
from provider import GeminiProvider, GenerationError


def photo(color='green'):
    output = io.BytesIO()
    Image.new('RGB', (300, 400), color).save(output, 'JPEG')
    return output.getvalue()


def image(color='green'):
    return 'data:image/jpeg;base64,' + base64.b64encode(photo(color)).decode()


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.labels = []

    def __call__(self, person, garments):
        self.calls.append((person, garments))
        return photo('blue'), {}

    def classify(self, blob, categories):
        self.labels.append(blob)
        return 'Джинсы', {}


class OutfitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = FakeProvider()
        self.store = Store(Path(self.temp.name) / 'outfits.db', self.provider)
        self.sid, _ = self.store.session('')

    def tearDown(self):
        self.store.stop.set()
        self.store.worker.join(3)
        self.temp.cleanup()

    def body(self):
        return {'request_id': 'outfit_request_000001', 'person': image(), 'garments': [
            {'image': image('red'), 'category': 'Футболка'},
            {'image': image('blue'), 'category': 'Джинсы'},
            {'image': image('yellow'), 'category': 'Кепка'},
            {'image': image('white'), 'category': 'Кроссовки'}]}

    def finished(self, jid):
        for _ in range(150):
            job = self.store.get(self.sid, jid)
            if job['status'] not in ('queued', 'running'):
                return job
            self.store.stop.wait(.02)
        self.fail('The test worker did not complete')

    def test_all_products_reach_one_generation_and_archive(self):
        body = self.body()
        job = self.store.create(self.sid, body)
        self.assertEqual([x['category'] for x in job['garments']], [x['category'] for x in body['garments']])
        row = self.finished(job['id'])
        self.assertEqual(row['status'], 'success')
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.provider.calls[0][1]), 4)
        self.assertEqual(self.provider.calls[0][1], [base64.b64decode(x['image']) for x in self.store.items(row)])
        with self.store.db() as db:
            archived = db.execute('SELECT * FROM generation_archive WHERE id=?', (job['id'],)).fetchone()
            self.assertEqual(len(self.store.items(archived)), 4)
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], 1)

    def test_idempotency_includes_every_photo_label_and_order(self):
        body = self.body()
        first = self.store.create(self.sid, body)
        self.assertEqual(self.store.create(self.sid, body)['id'], first['id'])
        for mutate in ('label', 'photo', 'order'):
            changed = self.body()
            if mutate == 'label':
                changed['garments'][-1]['category'] = 'Обувь'
            elif mutate == 'photo':
                changed['garments'][-1]['image'] = image('black')
            else:
                changed['garments'].reverse()
            with self.assertRaises(Problem) as error:
                self.store.create(self.sid, changed)
            self.assertEqual(error.exception.status, 409)

    def test_empty_over_limit_and_invalid_products_do_not_charge(self):
        for items in ([], self.body()['garments'] * 2, [{'image': image(), 'category': 'unknown'}], [None]):
            body = self.body(); body['garments'] = items
            with self.assertRaises(Problem):
                self.store.create(self.sid, body)
        self.assertEqual(self.store.config(self.sid)['remaining'], 5)
        self.assertEqual(self.provider.calls, [])

    def test_six_products_supported(self):
        body = self.body(); body['garments'] += body['garments'][:2]
        job = self.store.create(self.sid, body)
        self.assertEqual(self.finished(job['id'])['status'], 'success')
        self.assertEqual(len(self.provider.calls[0][1]), 6)

    def test_legacy_single_product_still_works(self):
        body = self.body(); body['garment'] = body.pop('garments')[0]['image']
        job = self.store.create(self.sid, body)
        row = self.finished(job['id'])
        self.assertEqual(job['garments'], [{'category': 'Вещь', 'index': 0}])
        self.assertIsInstance(self.provider.calls[0][1], bytes)
        with self.store.db() as db:
            db.execute('UPDATE jobs SET garments=NULL WHERE id=?', (row['id'],))
        self.assertEqual(len(self.store.items(self.store.get(self.sid, row['id']))), 1)

    def test_other_session_cannot_access_outfit(self):
        job = self.store.create(self.sid, self.body())
        other, _ = self.store.session('')
        with self.assertRaises(Problem) as error:
            self.store.get(other, job['id'])
        self.assertEqual(error.exception.status, 404)

    def test_recognition_cached_without_using_generation_quota(self):
        self.assertTrue(self.store.config(self.sid)['classification_enabled'])
        for _ in range(2):
            self.assertEqual(self.store.classify(self.sid, {'image': image()}), {'category': 'Джинсы'})
        self.assertEqual(len(self.provider.labels), 1)
        self.assertEqual(self.store.config(self.sid)['remaining'], 5)

    def test_recognition_failure_keeps_manual_fallback(self):
        with patch.object(self.provider, 'classify', side_effect=GenerationError('offline')):
            self.assertEqual(self.store.classify(self.sid, {'image': image()}), {'category': 'Вещь'})
        self.assertEqual(self.store.classify(self.sid, {'image': image()}), {'category': 'Вещь'})
        self.assertEqual(self.provider.labels, [])

    def test_uninvited_recognition_never_calls_provider(self):
        self.store.invite_hashes.add('a' * 64)
        with self.assertRaises(Problem) as error:
            self.store.classify(self.sid, {'image': image()})
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.provider.labels, [])


class OutfitProviderTests(unittest.TestCase):
    def test_multiple_references_in_one_request_person_first(self):
        provider = GeminiProvider('test-key')
        response = {'candidates': [{'content': {'parts': [{'inlineData': {
            'mimeType': 'image/jpeg', 'data': base64.b64encode(photo()).decode()}}]}}]}
        with patch('provider.urllib.request.urlopen') as call:
            call.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
            products = [photo('red'), photo('blue'), photo('white')]
            provider(photo(), products)
            self.assertEqual(call.call_count, 1)
            payload = json.loads(call.call_args.args[0].data)
            blobs = [base64.b64decode(p['inlineData']['data']) for p in payload['contents'][0]['parts'] if 'inlineData' in p]
            self.assertEqual(blobs, [photo(), *products])

    def test_classification_returns_only_allowed_labels(self):
        provider = GeminiProvider('test-key')
        with patch('provider.urllib.request.urlopen') as call:
            for value, expected in [('Кепка', 'Кепка'), ('unknown or injected text', 'Вещь')]:
                call.return_value.__enter__.return_value.read.return_value = json.dumps({
                    'candidates': [{'content': {'parts': [{'text': value}]}}]}).encode()
                self.assertEqual(provider.classify(photo(), CATEGORIES)[0], expected)
                config = json.loads(call.call_args.args[0].data)['generationConfig']
                self.assertEqual(config['responseModalities'], ['TEXT'])


if __name__ == '__main__':
    unittest.main()
