import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Callable, TypeVar

from app.config import TASK_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

T = TypeVar("T")

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")


def run_with_timeout(callable_task: Callable[[], T]) -> T:
    future = _executor.submit(callable_task)
    try:
        return future.result(timeout=TASK_TIMEOUT_SECONDS)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise TimeoutError(
            f"Task exceeded {TASK_TIMEOUT_SECONDS}s timeout."
        ) from exc
