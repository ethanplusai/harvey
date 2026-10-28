"""Credential query values must never reach a log handler.

httpx logs one `HTTP Request: GET <full-url-with-query>` line per request
at INFO; vendors that authenticate with an `api_key` query parameter were
therefore writing their keys into Harvey's log files. See
`harvey/log_redaction.py`.
"""

import logging

from harvey.log_redaction import REDACTED, SecretRedactingFilter, quiet_httpx_loggers, redact_query_secrets


class TestRedactQuerySecrets:
    def test_redacts_api_key_and_keeps_the_rest_of_the_url(self):
        url = "https://api.hunter.example/v2/domain-search?domain=example.com&api_key=hunter-secret-123"
        out = redact_query_secrets(url)
        assert "hunter-secret-123" not in out
        assert out == "https://api.hunter.example/v2/domain-search?domain=example.com&api_key=" + REDACTED

    def test_covers_spelling_variants(self):
        for param in ("api_key", "api-key", "apiKey", "API_KEY", "key", "token", "access_token", "client_secret", "password", "passwd"):
            out = redact_query_secrets(f"https://vendor.example/v1?{param}=s3cr3t-value")
            assert "s3cr3t-value" not in out, param
            assert f"{param}={REDACTED}" in out, param

    def test_leaves_innocent_parameters_alone(self):
        # Whole-word matching: `monkey` and `keyboard` must not be treated
        # as `key`, and ordinary parameters pass through untouched.
        text = "https://site.example/search?monkey=1&keyboard=mech&domain=example.com&q=token"
        assert redact_query_secrets(text) == text

    def test_value_stops_at_the_next_parameter(self):
        out = redact_query_secrets("https://vendor.example/v1?api_key=s3cr3t&domain=example.com")
        assert out == f"https://vendor.example/v1?api_key={REDACTED}&domain=example.com"

    def test_value_stops_at_quotes_and_brackets(self):
        for text in (
            "failed 'https://vendor.example/v1?api_key=s3cr3t'",
            "failed [https://vendor.example/v1?api_key=s3cr3t]",
            'failed "https://vendor.example/v1?api_key=s3cr3t"',
        ):
            out = redact_query_secrets(text)
            assert "s3cr3t" not in out
            assert text[: text.index("api_key=")] in out

    def test_text_without_credentials_is_unchanged(self):
        text = "cycle complete: 3 companies, 1 prospect, no requests failed"
        assert redact_query_secrets(text) == text


class TestSecretRedactingFilter:
    def _record(self, msg, args=None):
        return logging.LogRecord(
            name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
            msg=msg, args=args, exc_info=None,
        )

    def test_redacts_formatted_url(self):
        record = self._record("HTTP Request: GET %s", ("https://api.hunter.example/v2?api_key=s3cr3t",))
        assert SecretRedactingFilter().filter(record) is True
        assert "s3cr3t" not in record.getMessage()
        assert f"api_key={REDACTED}" in record.getMessage()

    def test_collapses_args_into_the_redacted_message(self):
        record = self._record("GET %s failed (%d attempts)", ("https://vendor.example/v1?token=s3cr3t", 3))
        SecretRedactingFilter().filter(record)
        assert record.args is None
        assert record.getMessage() == f"GET https://vendor.example/v1?token={REDACTED} failed (3 attempts)"

    def test_clean_record_passes_through_untouched(self):
        record = self._record("cycle %d done", (7,))
        SecretRedactingFilter().filter(record)
        assert record.msg == "cycle %d done"
        assert record.args == (7,)

    def test_is_idempotent_across_handlers(self):
        record = self._record("HTTP Request: GET https://vendor.example/v1?api_key=s3cr3t", None)
        flt = SecretRedactingFilter()
        flt.filter(record)
        first = record.getMessage()
        flt.filter(record)
        assert record.getMessage() == first

    def test_end_to_end_through_a_real_handler(self):
        # The exact shape that was leaking: an httpx INFO request line
        # reaching a root-level handler.
        import io

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(SecretRedactingFilter())
        test_logger = logging.getLogger("harvey.test_redaction")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.INFO)
        try:
            test_logger.info(
                "HTTP Request: GET https://api.hunter.example/v2/domain-search?domain=example.com&api_key=s3cr3t"
            )
        finally:
            test_logger.removeHandler(handler)
        out = stream.getvalue()
        assert "s3cr3t" not in out
        assert f"api_key={REDACTED}" in out


class TestQuietHttpxLoggers:
    def test_drops_httpx_and_httpcore_to_warning(self):
        quiet_httpx_loggers()
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
