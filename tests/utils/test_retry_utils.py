# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

import unittest
from unittest.mock import MagicMock, patch

from trae_agent.utils.llm_clients.retry_utils import _should_retry_api_error, retry_with


class TestShouldRetryApiError(unittest.TestCase):
    def test_rate_limit_429_returns_true(self):
        from openai import APIStatusError

        exc = APIStatusError("rate limited", response=MagicMock(), body=None)
        exc.status_code = 429
        self.assertTrue(_should_retry_api_error(exc))

    def test_server_error_5xx_returns_true(self):
        from openai import APIStatusError

        exc = APIStatusError("server error", response=MagicMock(), body=None)
        exc.status_code = 500
        self.assertTrue(_should_retry_api_error(exc))

    def test_server_error_502_returns_true(self):
        from openai import APIStatusError

        exc = APIStatusError("bad gateway", response=MagicMock(), body=None)
        exc.status_code = 502
        self.assertTrue(_should_retry_api_error(exc))

    def test_client_error_400_returns_false(self):
        from openai import APIStatusError

        exc = APIStatusError("bad request", response=MagicMock(), body=None)
        exc.status_code = 400
        self.assertFalse(_should_retry_api_error(exc))

    def test_client_error_404_returns_false(self):
        from openai import APIStatusError

        exc = APIStatusError("not found", response=MagicMock(), body=None)
        exc.status_code = 404
        self.assertFalse(_should_retry_api_error(exc))

    def test_non_api_error_returns_true(self):
        exc = ValueError("some other error")
        self.assertTrue(_should_retry_api_error(exc))

    def test_status_code_none_returns_true(self):
        from openai import APIStatusError

        exc = APIStatusError("no status", response=MagicMock(), body=None)
        exc.status_code = None
        self.assertTrue(_should_retry_api_error(exc))


class TestRetryWith(unittest.TestCase):
    def test_successful_first_call_returns_value(self):
        @retry_with
        def succeed():
            return "ok"

        result = succeed()
        self.assertEqual(result, "ok")

    @patch("time.sleep")
    def test_retryable_error_eventually_succeeds(self, mock_sleep):
        call_count = [0]

        @retry_with
        def succeeds_after_two():
            call_count[0] += 1
            if call_count[0] < 3:
                from openai import APIStatusError

                exc = APIStatusError("server error", response=MagicMock(), body=None)
                exc.status_code = 503
                raise exc
            return "success"

        result = succeeds_after_two()
        self.assertEqual(result, "success")
        self.assertEqual(call_count[0], 3)

    def test_non_retryable_error_raises_immediately(self):
        call_count = [0]

        @retry_with
        def bad_request():
            call_count[0] += 1
            from openai import APIStatusError

            exc = APIStatusError("bad request", response=MagicMock(), body=None)
            exc.status_code = 400
            raise exc

        from openai import APIStatusError

        with self.assertRaises(APIStatusError):
            bad_request()
        self.assertEqual(call_count[0], 1)

    @patch("time.sleep")
    def test_max_retries_exhausted_raises(self, mock_sleep):
        call_count = [0]

        @retry_with
        def always_fails():
            call_count[0] += 1
            from openai import APIStatusError

            exc = APIStatusError("server error", response=MagicMock(), body=None)
            exc.status_code = 500
            raise exc

        from openai import APIStatusError

        with self.assertRaises(APIStatusError):
            always_fails()
        self.assertEqual(call_count[0], 4)  # 1 initial + 3 retries

    @patch("time.sleep")
    def test_non_api_exception_retries(self, mock_sleep):
        call_count = [0]

        @retry_with
        def network_error():
            call_count[0] += 1
            if call_count[0] < 2:
                raise ConnectionError("timeout")
            return "recovered"

        result = network_error()
        self.assertEqual(result, "recovered")
        self.assertEqual(call_count[0], 2)

    def test_custom_provider_name_passed(self):
        @retry_with
        def check_provider():
            return "done"

        self.assertEqual(check_provider.__name__, "check_provider")

    @patch("time.sleep")
    def test_custom_max_retries(self, mock_sleep):
        call_count = [0]

        wrapped = retry_with(lambda: _increment_and_fail_500(call_count), max_retries=1)
        from openai import APIStatusError

        with self.assertRaises(APIStatusError):
            wrapped()
        self.assertEqual(call_count[0], 2)  # 1 initial + 1 retry


def _increment_and_fail_500(call_count):
    call_count[0] += 1
    from openai import APIStatusError

    exc = APIStatusError("server error", response=MagicMock(), body=None)
    exc.status_code = 500
    raise exc


if __name__ == "__main__":
    unittest.main()
