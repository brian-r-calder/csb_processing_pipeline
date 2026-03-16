import re
import subprocess
from pathlib import Path
import tempfile
import shutil

import requests

import pytest

import boto3


def is_responsive(url):
    """Check if the Garage S3 endpoint is responsive.
       Note: 403 Forbidden is expected for an S3 root endpoint without auth,
       meaning the service is up and responding."""
    try:
        response = requests.get(url)
        return response.status_code == 200 or response.status_code == 403
    except ConnectionError:
        return False


@pytest.fixture(scope="session")
def garage_layout(docker_ip, docker_services, docker_compose_file):
    """Wait for Garage to start, create keys, and return the credentials."""
    port = docker_services.port_for("garage", 3900)
    endpoint_url = f"http://{docker_ip}:{port}"

    # Wait until the Garage S3 API is responsive
    docker_services.wait_until_responsive(
        timeout=30.0, pause=0.5, check=lambda: is_responsive(endpoint_url)
    )

    # Base docker compose command
    dc_cmd = ["docker", "exec", "garage"]

    # 1. Get node
    res = subprocess.run([*dc_cmd, "/garage", "status"],
                         text=True, capture_output=True)
    if res.returncode != 0:
        raise Exception((f"Unable to get garage status, return code was {res.returncode}; "
                         f"stdout: {res.stdout}, stderr: {res.stderr}"))
    node_id: str | None = None
    for line in res.stdout.split('\n'):
        if 'NO ROLE ASSIGNED' in line:
            node_id = line.split()[0]
    if node_id is None:
        raise Exception(f"Unable to find garage node from status output: {res.stdout}")

    # 2. Assign storage to node
    res = subprocess.run([*dc_cmd, "/garage", "layout", "assign", "-z", "dc1", "-c", "100G", node_id],
                         capture_output=True)
    if res.returncode != 0:
        raise Exception((f"Unable to assign garage layout, return code was {res.returncode}; "
                         f"stdout: {res.stdout}, stderr: {res.stderr}"))

    # 3. Apply layout
    res = subprocess.run([*dc_cmd, "/garage", "layout", "apply", "--version", "1"],
                         capture_output=True)
    if res.returncode != 0:
        raise Exception((f"Unable to apply garage layout, return code was {res.returncode}; "
                         f"stdout: {res.stdout}, stderr: {res.stderr}"))


@pytest.fixture(scope="session")
def garage_credentials(docker_ip, docker_services, docker_compose_file, garage_layout):
    """Wait for Garage to start, create keys, and return the credentials."""
    port = docker_services.port_for("garage", 3900)
    endpoint_url = f"http://{docker_ip}:{port}"

    # Wait until the Garage S3 API is responsive
    docker_services.wait_until_responsive(
        timeout=30.0, pause=0.5, check=lambda: is_responsive(endpoint_url)
    )

    # Base docker compose command
    dc_cmd = ["docker", "exec", "garage"]

    # 1. Create the key (ignores if it already exists due to your post_start hooks)
    res = subprocess.run([*dc_cmd, "/garage", "key", "create", "csb-key"],
                         text=True, capture_output=True)
    access_key_match = re.search(r"Key ID:\s+(\S+)", res.stdout)
    secret_key_match = re.search(r"Secret key:\s+(\S+)", res.stdout)
    if not access_key_match or not secret_key_match:
        raise RuntimeError(
            f"Failed to extract Garage credentials. CLI Output: {res.stdout}\nErrors: {res.stderr}")

    # 2. Grant bucket creation permissions
    res = subprocess.run([*dc_cmd, "/garage", "key", "allow", "--create-bucket", "csb-key"],
                         text=True, capture_output=True)

    return {
        "aws_access_key_id": access_key_match.group(1),
        "aws_secret_access_key": secret_key_match.group(1),
        "endpoint_url": endpoint_url
    }


@pytest.fixture(scope="session")
def s3_client(garage_credentials):
    """Yields a fully configured boto3 client connected to the local Garage instance."""
    client = boto3.client(
        "s3",
        endpoint_url=garage_credentials["endpoint_url"],
        aws_access_key_id=garage_credentials["aws_access_key_id"],
        aws_secret_access_key=garage_credentials["aws_secret_access_key"],
        region_name="garage"
    )

    # Create a default bucket
    bucket_name = "csb-dest"
    client.create_bucket(Bucket=bucket_name)

    yield client


@pytest.fixture(scope="function")
def temp_path():
    tmp_dir = Path(tempfile.mkdtemp())
    yield tmp_dir
    shutil.rmtree(tmp_dir)
