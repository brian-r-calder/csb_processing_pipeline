from _thread import RLock
from abc import ABC
from pathlib import Path
import datetime
import time
from enum import Enum, Flag, auto
from typing import cast, Sequence, Any, Generator
import shutil
import io
import threading

from smart_open import open as sopen

import boto3
import botocore.exceptions

from ocscsb.library.cloud import aws

# 72-hours TTL
DEFAULT_TTL_SEC = 259_200
ALWAYS_EXISTS_TTL = -1

_OPEN_LOCKS_LOCK = threading.RLock()
_OPEN_LOCKS: dict[str, threading.RLock] = {}

class ObjectType(Flag):
    FILE = auto()
    DIRECTORY = auto()

class StorageProvider(ABC):
    class ObjectStateError(Exception):
        ...

    class IOError(Exception):
        ...

    def __init__(self, location: str):
        self.location = location

    def generate_resource_uri(self, *,
                              object_name: str | None = None,
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

    def generate_gdal_vsi_path(self, object_name: str,
                               *,
                               sub_path: str | None = None,
                               relative: bool = False) -> str:
        """
        Return the GDAL VSI (https://gdal.org/en/stable/user/virtual_file_systems.html) path for the resource
        named `object_name` located at `self.location`.

        Parameters
        ----------
        object_name
        sub_path
        relative
            Return a path relative this provider's location. Note: this may not be applicable to cloud object stores.

        Returns
        -------
        A string beginning with '/vsiPREFIX/...' where PREFIX is a value appropriate to the underlying storage
        provider (which for local file storage may be the same as the URI).
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

    def _prepare_open(self, object_name: str):
        """
        Prepare underlying storage for opening object `object_name`. This could mean,
        for example, making sure intermediate directories between `object_name` and
        `self.location` exist.

        Parameters
        ----------
        object_name

        Raises
        -------
        StorageProvider.ObjectStateError if preparation for opening failed.

        """
        ...

    def open(self, object_name: str, mode='r', buffering=-1, encoding=None, errors=None, newline=None,
             **kwargs):
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
        kwargs

        Returns
        -------
        A file-like object that can be read from or written to.

        Raises
        ------
        StorageProvider.ObjectStateError if the file cannot be opened.
        """
        uri = self.generate_resource_uri(object_name=object_name)
        with _OPEN_LOCKS_LOCK:
            lock = _OPEN_LOCKS.get(str(uri), threading.RLock())
        with lock:
            self._prepare_open(object_name)
            if 'a' in mode:
                # Some smart-open backends (e.g., S3) don't support append operations, so we need to fake it
                existing_data = None
                read_mode = 'rb' if 'b' in mode else 'r'
                if self.object_exists(object_name):
                    try:
                        with sopen(uri, mode=read_mode, buffering=buffering, encoding=encoding, errors=errors,
                                   newline=newline, **kwargs) as f_in:
                            existing_data = f_in.read()
                    except Exception as e:
                        raise StorageProvider.IOError(f"Exception occurred while emulating append-mode operation: {str(e)}")

                write_mode = mode.replace('a', 'w')
                f_out = sopen(uri, mode=write_mode, buffering=buffering, encoding=encoding, errors=errors,
                              newline=newline, **kwargs)
                if existing_data:
                    f_out.write(existing_data)
                return f_out
            else:
                return sopen(uri,
                             mode=mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline,
                             **kwargs)

    def list_objects(self,
                     prefix: str | None = None,
                     suffix: str | None = None,
                     sub_path: str | None = None,
                     object_types: ObjectType = ObjectType.FILE) -> list[str]:
        """List objects in the storage provider."""
        ...

    def list_directory_like(self,
                            pattern: str | None = None) -> list[str]:
        """List directory-like entities along object paths"""
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

    def generate_resource_uri(self, *,
                              object_name: str | None = None,
                              sub_path: str | None = None) -> str | Path:
        object_parent = self.location_path
        if sub_path is not None:
            object_parent = object_parent / sub_path
        if object_name is not None:
            return object_parent / object_name
        return object_parent

    def generate_gdal_vsi_path(self, object_name: str,
                               *,
                               sub_path: str | None = None,
                               relative: bool = False) -> str:
        if relative:
            if sub_path:
                return f"{sub_path}/{object_name}"
            return object_name
        return str(self.generate_resource_uri(object_name=object_name, sub_path=sub_path))

    def object_exists(self, object_name: str,
                      *,
                      ttl_sec: int = DEFAULT_TTL_SEC,
                      sub_path: str | None = None) -> bool:
        object_path: Path = cast(Path, self.generate_resource_uri(object_name=object_name, sub_path=sub_path))
        if not object_path.exists():
            return False
        if ttl_sec == ALWAYS_EXISTS_TTL:
            return True
        # File exists, check its modification time to see if it is older than TTL, if so, file is
        # considered not to exist.
        curr_time = time.time()
        stat = object_path.stat()
        return stat.st_mtime > (curr_time - ttl_sec)

    def _prepare_open(self, object_name: str):
        object_path: Path = cast(Path, self.generate_resource_uri(object_name=object_name))
        object_parent: Path = object_path.parent
        if object_parent.exists():
            if not object_parent.is_dir():
                raise StorageProvider.ObjectStateError(f"Parent {str(object_parent)} "
                                                       f"of object to open {str(object_path)} is not a directory.")
        else:
            object_parent.mkdir(parents=True, exist_ok=True)

    def _object_type_filter(self, object_types: ObjectType, obj) -> bool:
        if not isinstance(obj, Path):
            raise ValueError(f"Parameter obj was expected to be of type Path but was {type(obj)}")
        p = cast(Path, obj)
        passes: bool = False
        if ObjectType.FILE in object_types:
            passes |= p.is_file()
        if ObjectType.DIRECTORY in object_types:
            passes |= p.is_dir()
        return passes

    def list_objects(self, prefix: str | None = None,
                     suffix: str | Sequence[str] | None = None,
                     sub_path: str | None = None, *,
                     object_types: ObjectType = ObjectType.FILE) -> list[str]:
        search_path = self.location_path
        if sub_path is not None:
            search_path = search_path / sub_path
        if not search_path.exists():
            return []

        pattern: str = ''
        if prefix:
            if not prefix.endswith('*'):
                pattern = f"{prefix}*"
            else:
                pattern = prefix
        if suffix:
            pattern = f"{pattern}*{suffix}"
        if pattern == '':
            pattern = '*'

        return [str(p.relative_to(search_path)) for p in search_path.glob(pattern, case_sensitive=False) \
                if self._object_type_filter(object_types, p)]

    def list_directory_like(self,
                            pattern: str | None = None) -> list[str]:
        return self.list_objects(prefix=pattern, object_types=ObjectType.DIRECTORY)

    def delete_object(self, object_name: str, sub_path: str | None = None) -> bool:
        object_path = self.generate_resource_uri(object_name=object_name, sub_path=sub_path)
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

    def generate_resource_uri(self, *,
                              object_name: str | None = None,
                              sub_path: str | None = None) -> str | Path:
        object_parent = self.location
        if sub_path is not None:
            object_parent = f"{object_parent}/{sub_path}"
        if object_name is not None:
            return f"s3://{object_parent}/{object_name}"
        return f"s3://{object_parent}"

    def generate_gdal_vsi_path(self, object_name: str,
                               *,
                               sub_path: str | None = None,
                               relative: bool = False) -> str:
        if sub_path is not None:
            return f"/vsis3/{self.location}/{sub_path}/{object_name}"
        else:
            return f"/vsis3/{self.location}/{object_name}"

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

    def _prepare_open(self, object_name: str):
        try:
            response = self._client.head_bucket(Bucket=self.location)
            resp_meta = response.get('ResponseMetadata', {})
            if 'HTTPStatusCode' not in resp_meta or resp_meta['HTTPStatusCode'] != 200:
                raise StorageProvider.ObjectStateError(f"Bucket {self.location} to store object {object_name} in "
                                                       "does not exist.")
        except botocore.exceptions.ClientError as e:
            raise StorageProvider.ObjectStateError(f"Unable to determine if bucket {self.location} "
                                                   f"to store object {object_name} in exists due to error: {str(e)}")

    def open(self, object_name: str, mode='r', buffering=-1, encoding=None, errors=None, newline=None,
             **kwargs):
        return super().open(object_name, mode=mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline,
                     transport_params={'client': self._client}, **kwargs)

    def list_objects(self,
                     prefix: str | None = None,
                     suffix: str | None = None,
                     sub_path: str | None = None,
                     object_types: ObjectType = ObjectType.FILE) -> list[str]:
        if ObjectType.DIRECTORY in object_types:
            raise ValueError(f"Object type {ObjectType.DIRECTORY.name} not supported for this provider.")
        bucket = self.location
        obj_prefix = ''
        if sub_path:
            obj_prefix = f"{sub_path}/"

        pattern = None
        if suffix:
            pattern = f"*{suffix}"
            if prefix:
                pattern = f"{prefix}{pattern}"
        elif prefix:
            pattern = f"{prefix}*"

        paginator = self._client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket, Prefix=obj_prefix)

        objects = []
        for page in pages:
            for obj in page.get('Contents', []):
                key = obj['Key']
                # Remove sub_path from the key to get just the object name
                if sub_path and key.startswith(f"{sub_path}/"):
                    name = key[len(sub_path) + 1:]
                else:
                    name = key

                if pattern:
                    name_path = Path(name)
                    if not name_path.match(pattern, case_sensitive=False):
                        continue
                if name:
                    # Avoid empty strings or directory markers
                    objects.append(name)
        return objects

    def list_directory_like(self,
                            pattern: str | None = None) -> list[str]:
        if pattern and not pattern.endswith('/') and not '*' in pattern:
            pattern = f"{pattern}*/"
        objects = self.list_objects(prefix=pattern)
        dirs = set()
        for obj in objects:
            comp = obj.split('/')
            if len(comp) > 1:
                dirs.add(comp[0])

        return list(dirs)

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

    def open(self, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        return self.storage_provider.open(self.object_name,
                                          mode=mode, buffering=buffering, encoding=encoding, errors=errors,
                                          newline=newline)

    def write(self, buff: io.BytesIO):
        buff.seek(0)
        with self.open(mode='wb') as f:
            f.write(buff.read())

    def get_uri(self) -> str:
        return str(self.storage_provider.generate_resource_uri(object_name=self.object_name))

    def get_gdal_vsi_path(self, *,
                          relative: bool = False) -> str:
        return self.storage_provider.generate_gdal_vsi_path(self.object_name, relative=relative)

    def get_stem(self) -> str:
        return Path(self.object_name).stem

    def get_suffix(self) -> str:
        return Path(self.object_name).suffix

    def move(self, dest: 'StorageLocation') -> 'File':
        dest_file: File = dest.new_file(self.object_name)
        if self.get_uri() == dest_file.get_uri():
            # dest is self, don't try to move self to self
            return dest_file
        try:
            with self.open(mode='rb') as fread:
                with dest_file.open(mode='wb') as fwrite:
                    fwrite.write(fread.read())
            self.delete()
            return dest_file
        except Exception as e:
            raise StorageProvider.IOError(f"Unable to move {self.get_uri()} to {dest_file.get_uri()} due to error: "
                                          f"{str(e)}")

    def delete(self) -> bool:
        return self.storage_provider.delete_object(self.object_name)

    def __str__(self):
        return self.get_uri()

    def __repr__(self):
        return self.get_uri()


class StorageLocation:
    def __init__(self, location: str | Path, provider: StorageProviderType | str,
                 **kwargs):
        if isinstance(provider, str):
            provider = StorageProviderType[provider.upper()]
        self.provider_type: StorageProviderType | str = provider
        self._sub_path: str | None = None
        self._client = kwargs.get('client',None)
        match provider:
            case StorageProviderType.LOCAL_FILE:
                if 'sub_path' in kwargs:
                    location: str = f"{location}/{kwargs['sub_path']}"
                else:
                    location: str = cast(str, location)
                self.location = location
                self.storage_provider: StorageProvider = StorageProviderFile(location)
            case StorageProviderType.S3:
                location = cast(str, location)
                if location[0] == '/':
                    raise ValueError(f"Location {location} is invalid for S3 storage provider: must not begin with '/'")
                if location[-1] == '/':
                    raise ValueError(f"Location {location} is invalid for S3 storage provider: must not end with '/'")
                loc_end_idx: int = location.find('/')
                if loc_end_idx > 0:
                    self.location = location[:loc_end_idx]
                    self._sub_path = location[loc_end_idx+1:]
                else:
                    self.location = cast(str, location)
                    self._sub_path = kwargs.get('sub_path', None)
                self.storage_provider: StorageProvider = StorageProviderS3(self.location,
                                                                           client=self._client)
            case _:
                raise ValueError(f"Unable to create IO manager for unknown storage provider type")

    @property
    def name(self) -> str:
        """
        Return a string representing the leaf node of the path of this storage location.

        Returns
        -------
        A string representing the leaf node of the path of this storage location.
        """
        if self._sub_path:
            return self._sub_path
        return self.location.split('/')[-1]

    def new_file(self, object_name: str) -> File:
        if self._sub_path:
            object_name = f"{self._sub_path}/{object_name}"
        return File(self.location, object_name, self.storage_provider)

    def contains(self, object_name: str,
                 *,
                 ttl_sec: int = ALWAYS_EXISTS_TTL,
                 sub_path: str | None = None) -> bool:
        if self._sub_path:
            object_name = f"{self._sub_path}/{object_name}"
        return self.storage_provider.object_exists(object_name, ttl_sec=ttl_sec, sub_path=sub_path)

    def list_files(self,
                   prefix: str | None = None,
                   suffix: str | None = None,
                   sub_path: str | None = None) -> list[File]:
        if sub_path is None and self._sub_path:
            sub_path = self._sub_path
        names = self.storage_provider.list_objects(prefix=prefix, suffix=suffix, sub_path=sub_path)
        files = []
        for name in names:
            if sub_path:
                obj_name = f"{sub_path}/{name}"
            else:
                obj_name = name
            files.append(File(self.location, obj_name, self.storage_provider))
        return files

    def list_sub_paths(self,
                       pattern: str | None = None) -> list['StorageLocation']:
        names = self.storage_provider.list_directory_like(pattern=pattern)
        dirs = []
        for name in names:
            dirs.append(self.sub_location(name))
        return dirs

    def delete_file(self, object_name: str, sub_path: str | None = None) -> bool:
        if sub_path is None and self._sub_path:
            sub_path = self._sub_path
        return self.storage_provider.delete_object(object_name, sub_path=sub_path)

    def delete_all(self, sub_path: str | None = None) -> bool:
        if sub_path is None and self._sub_path:
            sub_path = self._sub_path
        return self.storage_provider.delete_all(sub_path=sub_path)

    def sub_location(self, sub_location: str) -> 'StorageLocation':
        if self._sub_path is not None:
            sub_location = f"{self._sub_path}/{sub_location}"
        return StorageLocation(self.location, self.provider_type,
                               sub_path=sub_location,
                               client=self._client)

    def get_uri(self, object_name: str | None = None, sub_path: str | None = None) -> str:
        if sub_path is None and self._sub_path:
            sub_path = self._sub_path
        return str(self.storage_provider.generate_resource_uri(object_name=object_name, sub_path=sub_path))

    def __str__(self):
        return self.get_uri()

    def __repr__(self):
        return self.get_uri()
