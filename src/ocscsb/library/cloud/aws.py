
import boto3

from ocscsb import config

def get_boto_client(service_name: str,
                    *,
                    aws_access_key_id: str | None = None,
                    aws_secret_access_key: str | None = None):
    if aws_access_key_id is None:
        aws_access_key_id = config.get_aws_access_key_id()
    if aws_secret_access_key is None:
        aws_secret_access_key = config.get_aws_secret_access_key()
    return boto3.client( # type: ignore
        service_name,
        endpoint_url=config.get_aws_endpoint_url(),
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        region_name=config.get_aws_region()
    )
