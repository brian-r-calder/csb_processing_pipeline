import os
import re

_ANSI_ESCAPE_PATTERN = re.compile(r'(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]')

def _strip_ansi(in_str: str) -> str:
    return _ANSI_ESCAPE_PATTERN.sub('', in_str)

def get_aws_access_key_id() -> str:
    return _strip_ansi(os.environ.get('AWS_ACCESS_KEY_ID', None))

def get_aws_secret_access_key() -> str:
    return _strip_ansi(os.environ.get('AWS_SECRET_ACCESS_KEY', None))

def get_aws_region() -> str:
    return os.environ.get('AWS_REGION', 'us-east-1')

def get_aws_endpoint_url() -> str:
    return os.environ.get('AWS_ENDPOINT_URL', None)
