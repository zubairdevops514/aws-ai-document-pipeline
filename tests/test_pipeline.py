import io
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from processor import MAX_BYTES, process
from handler import handle


def event(version='v1', key='incoming%2Fincident+report.txt'):
    return {'Records': [{'eventSource': 'aws:s3', 'eventName': 'ObjectCreated:Put',
                         's3': {'bucket': {'name': 'input'},
                                'object': {'key': key, 'versionId': version, 'size': 20}}}]}


class PipelineTests(unittest.TestCase):
    def client(self, data=b'First sentence. Second sentence. Third sentence. Fourth sentence.'):
        s3 = Mock()
        s3.get_object.side_effect = lambda **kwargs: {'Body': io.BytesIO(data), 'ContentLength': len(data)}
        return s3

    def test_local_summary_and_digest(self):
        result = process(b'One. Two. Three. Four.')
        self.assertEqual(result['summary'], 'One. Two. Three.')
        self.assertEqual(result['mode'], 'extractive-local')
        self.assertEqual(len(result['sha256']), 64)

    def test_invalid_documents(self):
        for data in (b'', b'   ', b'\xff', b'x' * (MAX_BYTES + 1)):
            with self.assertRaises((ValueError, UnicodeDecodeError)):
                process(data)

    def test_pinned_version_url_decoding_and_result(self):
        s3 = self.client()
        result = handle(event(), s3, None, 'input', 'output')
        self.assertEqual(result['processed'], 1)
        s3.get_object.assert_called_once_with(Bucket='input', Key='incoming/incident report.txt', VersionId='v1')
        stored = json.loads(s3.put_object.call_args.kwargs['Body'])
        self.assertEqual(stored['source']['version_id'], 'v1')
        self.assertEqual(s3.put_object.call_args.kwargs['Bucket'], 'output')

    def test_duplicate_event_has_same_output_key(self):
        s3 = self.client()
        first = handle(event(), s3, None, 'input', 'output')
        self.assertEqual(first, handle(event(), s3, None, 'input', 'output'))
        self.assertNotEqual(first, handle(event('v2'), s3, None, 'input', 'output'))

    def test_rejects_unexpected_bucket_and_missing_version(self):
        for sample, expected_bucket in ((event(), 'other'), (event(None), 'input'),
                                         (event(key='results/x.txt'), 'input')):
            s3 = self.client()
            with self.assertRaises(ValueError):
                handle(sample, s3, None, expected_bucket, 'output')
            s3.get_object.assert_not_called()

    def test_oversized_body_is_bounded(self):
        s3 = self.client(b'x' * (MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            handle(event(), s3, None, 'input', 'output')
        s3.put_object.assert_not_called()

    def test_bedrock_response(self):
        client = Mock()
        client.converse.return_value = {'output': {'message': {'content': [{'text': 'Summary.'}]}}}
        self.assertEqual(process(b'Example.', client, 'model')['mode'], 'bedrock')
        self.assertEqual(client.converse.call_args.kwargs['modelId'], 'model')

    def test_storage_failure_propagates_for_retry(self):
        s3 = self.client()
        s3.put_object.side_effect = RuntimeError('write failed')
        with self.assertRaises(RuntimeError):
            handle(event(), s3, None, 'input', 'output')


if __name__ == '__main__':
    unittest.main()
