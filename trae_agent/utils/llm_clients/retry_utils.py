# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

import random
import time
import traceback
from functools import wraps
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def _should_retry_api_error(exc: Exception) -> bool:
    """Retry only on rate limits and transient server/network failures — not on bad requests."""
    try:
        from openai import APIStatusError

        if isinstance(exc, APIStatusError) and exc.status_code is not None:
            code = exc.status_code
            if code == 429:
                return True
            if code >= 500:
                return True
            if 400 <= code < 500:
                return False
    except ImportError:
        pass
    return True


def retry_with(
    func: Callable[..., T],
    provider_name: str = "OpenAI",
    max_retries: int = 3,
) -> Callable[..., T]:
    """
    Decorator that adds retry logic with randomized backoff.

    Args:
        func: The function to decorate
        provider_name: The name of the model provider being called
        max_retries: Maximum number of retry attempts

    Returns:
        Decorated function with retry logic
    """

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        last_exception = None

        for attempt in range(max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exception = e

                if attempt == max_retries:
                    # Last attempt, re-raise the exception
                    raise

                if not _should_retry_api_error(e):
                    raise

                # Exponential backoff with jitter (cap ~60s) — faster recovery than flat 3–30s random
                base = min(60.0, float(2**attempt))
                sleep_time = base + random.uniform(0, min(4.0, base * 0.25))
                this_error_message = str(e)
                print(
                    f"{provider_name} API call failed: {this_error_message}. Will sleep for {sleep_time:.1f} seconds and will retry.\n{traceback.format_exc()}"
                )
                time.sleep(sleep_time)

        # This should never be reached, but just in case
        raise last_exception or Exception("Retry failed for unknown reason")

    return wrapper
