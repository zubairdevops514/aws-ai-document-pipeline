"""S3 event adapter. Inputs and results live in separate private buckets."""
import hashlib
import json
import logging
import os
from urllib.parse import unquote_plus
from processor import MAX_BYTES, process

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


def handle(event, s3, bedrock, input_bucket, output_bucket, model_id=''):
    completed = []
    for record in event.get('Records', []):
        if record.get('eventSource') != 'aws:s3':
            raise ValueError('Expected an S3 event')
        if not record.get('eventName', '').startswith('ObjectCreated:'):
            raise ValueError('Expected an object creation event')
        bucket = record['s3']['bucket']['name']
        obj = record['s3']['object']
        key = unquote_plus(obj['key'])
        if bucket != input_bucket or not key.startswith('incoming/') or not key.endswith('.txt'):
            raise ValueError('Unexpected document location')
        if obj.get('size', 0) > MAX_BYTES:
            raise ValueError('Document too large')
        version = obj.get('versionId')
        if not version:
            raise ValueError('Versioned S3 object required for reproducible processing')
        identity = json.dumps([bucket, key, version], separators=(',', ':')).encode()
        result_key = 'results/' + hashlib.sha256(identity).hexdigest() + '.json'
        request = {'Bucket': bucket, 'Key': key, 'VersionId': version}
        response = s3.get_object(**request)
        body = response['Body']
        try:
            data = body.read(MAX_BYTES + 1)
        finally:
            body.close()
        if response.get('ContentLength', len(data)) > MAX_BYTES:
            raise ValueError('Document too large')
        result = process(data, bedrock if model_id else None, model_id)
        result['source'] = {'bucket': bucket, 'key': key, 'version_id': version}
        s3.put_object(Bucket=output_bucket, Key=result_key,
                      Body=json.dumps(result).encode(), ContentType='application/json',
                      ServerSideEncryption='AES256')
        LOG.info(json.dumps({'event': 'document_processed', 'result_key': result_key,
                             'bytes': result['bytes'], 'mode': result['mode']}))
        completed.append(result_key)
    return {'processed': len(completed), 'results': completed}


def lambda_handler(event, context):
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=3, read_timeout=45,
                    retries={'max_attempts': 2, 'mode': 'standard'})
    model_id = os.environ.get('BEDROCK_MODEL_ID', '')
    return handle(event, boto3.client('s3', config=config),
                  boto3.client('bedrock-runtime', config=config) if model_id else None,
                  os.environ['INPUT_BUCKET'], os.environ['OUTPUT_BUCKET'], model_id)
