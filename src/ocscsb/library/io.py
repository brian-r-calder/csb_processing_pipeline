from abc import ABC
from pathlib import Path
import datetime
import time
from contextlib import contextmanager
from enum import Enum
from typing import cast
import shutil

from smart_open import open as sopen

import boto3
import botocore.exceptions

from ocscsb.library.cloud import aws

# 72-hours TTL
DEFAULT_TTL_SEC = 259_200
ALWAYS_EXISTS_TTL = -1


class StorageProvider(ABC):
    def __init__(self, location: str):
        self.location = location

    def generate_resource_uri(self, object_name: str,
                              *,
                              sub_path: str | None = None) -> str | Path:
        """
        Return the URI for the resource named `object_name` located at `self.location`.

        Parameters
        ----------
        object_name
        sub_path

        Returns
        -------
        For non-local file resources (e.g., S3) returns a str representing the URI of the resource.
        For local file resources (e.g., S3) returns a Path object representing the local file.
        """
        ...

    def object_exists(self, object_name: str,
                      *,
                      ttl_sec: int = ALWAYS_EXISTS_TTL,
                      sub_path: str | None = None) -> bool:
        """
        Test if `object_name` exists and is newer than TTL seconds old
        Parameters
        ----------
        object_name
        ttl_sec : int
            Max age, at which, the object is considered to be stale and therefore non-existent.
            Default ALWAYS_EXISTS_TTL, which forces a strict existence test, regardless of object age.
        sub_path

        Returns
        -------
        True if `object_name` exists and is newer than TTL
        False if `object_name` exists and is older than TTL
        False if `object_name` does not exist
        """
        ...

    def open(self, object_name: str, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        """
        Open `object_name` for reading for writing.
        Parameters
        ----------
        object_name
        mode
        buffering
        encoding
        errors
        newline

        Returns
        -------
        A file-like object that can be read from or written to.
        """
        return sopen(self.generate_resource_uri(object_name),
                     mode=mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline)

    def list_objects(self, prefix: str | None = None, suffix: str | None = None, sub_path: str | None = None) -> list[
        str]:
        """List objects in the storage provider."""
        ...

    def delete_object(self, object_name: str, sub_path: str | None = None) -> bool:
        """Delete an object from the storage provider."""
        ...

    def delete_all(self, sub_path: str | None = None) -> bool:
        """Delete all objects in a sub_path (like rmtree)."""
        ...


class StorageProviderFile(StorageProvider):
    def __init__(self, location: str):
        super().__init__(location)
        self.location_path: Path = Path(self.location).absolute()

    def generate_resource_uri(self, object_name: str,
                              *,
                              sub_path: str | None = None) -> str | Path:
        object_parent = self.location_path
        if sub_path is not None:
            object_parent = object_parent / sub_path
        return object_parent / object_name

    def object_exists(self, object_name: str,
                      *,
                      ttl_sec: int = DEFAULT_TTL_SEC,
                      sub_path: str | None = None) -> bool:
        object_path: Path = cast(Path, self.generate_resource_uri(object_name, sub_path=sub_path))
        if not object_path.exists():
            return False
        if ttl_sec == ALWAYS_EXISTS_TTL:
            return True
        # File exists, check its modification time to see if it is older than TTL, if so, file is
        # considered not to exist.
        curr_time = time.time()
        stat = object_path.stat()
        return stat.st_mtime > (curr_time - ttl_sec)

    def list_objects(self, prefix: str | None = None, suffix: str | None = None, sub_path: str | None = None) -> list[
        str]:
        search_path = self.location_path
        if sub_path is not None:
            search_path = search_path / sub_path
        if not search_path.exists():
            return []

        pattern = "*"
        if prefix:
            pattern = f"{prefix}{pattern}"
        if suffix:
            pattern = f"{pattern}{suffix}"

        return [p.name for p in search_path.glob(pattern) if p.is_file()]

    def delete_object(self, object_name: str, sub_path: str | None = None) -> bool:
        object_path = self.generate_resource_uri(object_name, sub_path=sub_path)
        if object_path.exists():
            object_path.unlink()
            return True
        return False

    def delete_all(self, sub_path: str | None = None) -> bool:
        target_path = self.location_path
        if sub_path is not None:
            target_path = target_path / sub_path
        if target_path.exists() and target_path.is_dir():
            shutil.rmtree(target_path)
            return True
        return False


class StorageProviderS3(StorageProvider):
    def __init__(self, location: str, client: boto3.client):
        super().__init__(location)
        if client is None:
            self._client = aws.get_boto_client('s3')
        else:
            self._client = client

    def generate_resource_uri(self, object_name: str,
                              *,
                              sub_path: str | None = None) -> str | Path:
        if sub_path is not None:
            return f"s3://{self.location}/{sub_path}/{object_name}"
        else:
            return f"s3://{self.location}/{object_name}"

    def object_exists(self, object_name: str,
                      *,
                      ttl_sec: int = DEFAULT_TTL_SEC,
                      sub_path: str | None = None) -> bool:
        object_path = object_name
        if sub_path is not None:
            object_path = f"{sub_path}/{object_name}"
        try:
            response = self._client.head_object(
                Bucket=self.location,
                Key=object_path
            )
            if ttl_sec == ALWAYS_EXISTS_TTL:
                return True
            mtime = response['LastModified']
            curr_time = datetime.datetime.now(mtime.tzinfo)
            dt = datetime.timedelta(seconds=ttl_sec)
            return mtime > (curr_time - dt)
        except botocore.exceptions.ClientError as e:
            if 'An error occurred (404)' in str(e):
                return False
            raise e

    def open(self, object_name: str, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        return sopen(self.generate_resource_uri(object_name),
                     mode=mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline,
                     transport_params={'client': self._client})

    def list_objects(self, prefix: str | None = None, suffix: str | None = None, sub_path: str | None = None) -> list[
        str]:
        bucket = self.location
        full_prefix = ""
        if sub_path:
            full_prefix = f"{sub_path}/"
        if prefix:
            full_prefix = f"{full_prefix}{prefix}"

        paginator = self._client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket, Prefix=full_prefix)

        objects = []
        for page in pages:
            for obj in page.get('Contents', []):
                key = obj['Key']
                # Remove sub_path from the key to get just the object name
                if sub_path and key.startswith(f"{sub_path}/"):
                    name = key[len(sub_path) + 1:]
                else:
                    name = key

                if suffix and not name.endswith(suffix):
                    continue
                if name:  # Avoid empty strings or directory markers
                    objects.append(name)
        return objects

    def delete_object(self, object_name: str, sub_path: str | None = None) -> bool:
        key = object_name
        if sub_path:
            key = f"{sub_path}/{object_name}"
        try:
            self._client.delete_object(Bucket=self.location, Key=key)
            return True
        except Exception:
            return False

    def delete_all(self, sub_path: str | None = None) -> bool:
        if not sub_path:
            # We probably don't want to delete the whole bucket by default
            return False

        bucket = self.location
        prefix = f"{sub_path}/"

        paginator = self._client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

        for page in pages:
            if 'Contents' in page:
                delete_keys = {'Objects': [{'Key': obj['Key']} for obj in page['Contents']]}
                self._client.delete_objects(Bucket=bucket, Delete=delete_keys)
        return True


class StorageProviderType(Enum):
    LOCAL_FILE = 1
    S3 = 2


STORAGE_PROVIDER_TYPES = [e.name.lower() for e in list(StorageProviderType)]
STORAGE_PROVIDER_TYPE_DEFAULT = StorageProviderType.LOCAL_FILE.name.lower()


class File:
    def __init__(self, location: str, object_name: str, storage_provider: StorageProvider):
        self.location = location
        self.object_name = object_name
        self.storage_provider = storage_provider

    @classmethod
    def init(cls, location: str | Path, *,
             object_name: str | None = None, provider: StorageProviderType | str = StorageProviderType.LOCAL_FILE,
             **kwargs) -> 'File':
        location_str: str = str(location)
        if object_name is None:
            path_comp = location_str.split('/')
            if len(path_comp) < 2:
                raise ValueError("location must include object name because object_name was None. "
                                 f"Location was: {location_str}")
            object_name = path_comp[-1]
            location_str = '/'.join(path_comp[:-1])

        if isinstance(provider, str):
            provider = StorageProviderType[provider.upper()]
        match provider:
            case StorageProviderType.LOCAL_FILE:
                storage_provider: StorageProvider = StorageProviderFile(location_str)
            case StorageProviderType.S3:
                storage_provider: StorageProvider = StorageProviderS3(location_str,
                                                                      client=kwargs.get('client', None))
            case _:
                raise ValueError(f"Unable to create IO manager for unknown storage provider type")
        return cls(location_str, object_name, storage_provider)

    @classmethod
    def from_path(cls, path: Path) -> 'File':
        if not path.is_file():
            raise ValueError(f"Path {path} is not a file.")
        path_abs: Path = path.absolute()
        location_str: str = str(path_abs.parent)
        storage_provider: StorageProvider = StorageProviderFile(location_str)
        return cls(location_str, path_abs.name, storage_provider)

    def exists(self, *,
               ttl_sec: int = ALWAYS_EXISTS_TTL):
        return self.storage_provider.object_exists(self.object_name, ttl_sec=ttl_sec)

    @contextmanager
    def open(self, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        f = self.storage_provider.open(self.object_name,
                                       mode=mode, buffering=buffering, encoding=encoding, errors=errors,
                                       newline=newline)
        try:
            yield f
        finally:
            f.close()

    def get_uri(self) -> str:
        return str(self.storage_provider.generate_resource_uri(self.object_name))

    def get_stem(self) -> str:
        return Path(self.object_name).stem

    def delete(self) -> bool:
        return self.storage_provider.delete_object(self.object_name)


class StorageLocation:
    def __init__(self, location: str | Path, provider: StorageProviderType | str,
                 **kwargs):
        self.location = str(location)
        if isinstance(provider, str):
            provider = StorageProviderType[provider.upper()]
        self.provider_type: StorageProviderType = provider
        match provider:
            case StorageProviderType.LOCAL_FILE:
                self.storage_provider: StorageProvider = StorageProviderFile(self.location)
            case StorageProviderType.S3:
                self.storage_provider: StorageProvider = StorageProviderS3(self.location,
                                                                           client=kwargs.get('client',
                                                                                             None))
            case _:
                raise ValueError(f"Unable to create IO manager for unknown storage provider type")

    def new_file(self, object_name: str) -> File:
        return File(self.location, object_name, self.storage_provider)

    def contains(self, object_name: str,
                 *,
                 ttl_sec: int = ALWAYS_EXISTS_TTL,
                 sub_path: str | None = None) -> bool:
        return self.storage_provider.object_exists(object_name, ttl_sec=ttl_sec, sub_path=sub_path)

    def list_files(self, prefix: str | None = None, suffix: str | None = None, sub_path: str | None = None) -> list[
        File]:
        names = self.storage_provider.list_objects(prefix=prefix, suffix=suffix, sub_path=sub_path)
        files = []
        for name in names:
            if sub_path:
                obj_name = f"{sub_path}/{name}"
            else:
                obj_name = name
            files.append(File(self.location, obj_name, self.storage_provider))
        return files

    def delete_file(self, object_name: str, sub_path: str | None = None) -> bool:
        return self.storage_provider.delete_object(object_name, sub_path=sub_path)

    def delete_all(self, sub_path: str | None = None) -> bool:
        return self.storage_provider.delete_all(sub_path=sub_path)

    def sub_location(self, sub_location: str) -> 'StorageLocation':
        return StorageLocation(f"{self.location}/{sub_location}", self.provider_type)

    def get_uri(self, object_name: str | None = None, sub_path: str | None = None) -> str:
        if object_name is None:
            # Return URI of the location itself
            if isinstance(self.storage_provider, StorageProviderFile):
                p = self.storage_provider.location_path
                if sub_path:
                    p = p / sub_path
                return str(p)
            elif isinstance(self.storage_provider, StorageProviderS3):
                uri = f"s3://{self.location}"
                if sub_path:
                    uri = f"{uri}/{sub_path}"
                return uri
        return str(self.storage_provider.generate_resource_uri(object_name, sub_path=sub_path))
