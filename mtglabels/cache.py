import datetime
import functools
import hashlib
import logging
import pickle
import tempfile

from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

DEFAULT_TTL = 24 * 60 * 60
DEFAULT_NAMESPACE = "mtglabels"

def get_cache_dir(namespace: str = DEFAULT_NAMESPACE) -> Path:
    return Path(tempfile.gettempdir()) / namespace

def _get_cache_path(namespace: str, fn: Callable, args: list, kwargs: dict) -> Path:
    """Generates a cache path for the given function and arguments."""
    cache_dir = get_cache_dir(namespace)
    fn_name = fn.__qualname__
    args_hash = hashlib.sha1(
        pickle.dumps((args, kwargs))
    ).hexdigest()
    return cache_dir / (fn_name + "." + args_hash)

def _get_file_age(path: Path) -> float:
    """Returns the file's modification age in seconds."""
    if isinstance(path, str):
        path = Path(path)
    now = datetime.datetime.now()
    last_modified = datetime.datetime.fromtimestamp(path.stat().st_mtime)
    age = now - last_modified
    return age.total_seconds()

def filecache(ttl: int = DEFAULT_TTL, namespace: str = DEFAULT_NAMESPACE):
    def wrapper(fn: Callable):
        @functools.wraps(fn)
        def cached_fn(*args, **kwargs):
            fn_name = fn.__qualname__
            cache_path = _get_cache_path(namespace, fn, args, kwargs)
            try:
                if ttl < 0 or _get_file_age(cache_path) < ttl:
                    with open(cache_path, "rb") as c:
                        return pickle.load(c)
            except FileNotFoundError:
                pass
            except (AttributeError, EOFError, ImportError, IndexError, pickle.UnpicklingError) as ex:
                log.warning(f"Failed to load cache for {fn_name}: {ex}")

            rtn = fn(*args, **kwargs)

            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "wb") as c:
                pickle.dump(rtn, c)

            return rtn
        return cached_fn

    if callable(ttl):
        # @filecache
        fn = ttl
        ttl, namespace = DEFAULT_TTL, DEFAULT_NAMESPACE
        return wrapper(fn)

    # @filecache(...)
    return wrapper