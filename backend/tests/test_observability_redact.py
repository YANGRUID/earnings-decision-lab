from observability.redact import redact


def test_redacts_tiingo_style_token_param():
    text = "Client error '429' for url 'https://api.tiingo.com/tiingo/daily/mu/prices?token=abc123secret&format=json'"
    result = redact(text)
    assert "abc123secret" not in result
    assert "token=REDACTED" in result


def test_redacts_alpha_vantage_style_apikey_param():
    text = "GET https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&apikey=supersecretkey"
    result = redact(text)
    assert "supersecretkey" not in result
    assert "apikey=REDACTED" in result


def test_leaves_non_sensitive_text_unchanged():
    text = "Client error '404 Not Found' for url 'https://api.tiingo.com/tiingo/daily/mu/prices'"
    assert redact(text) == text


def test_redacts_multiple_params_in_one_string():
    text = "token=aaa&other=1 then apikey=bbb"
    result = redact(text)
    assert "aaa" not in result
    assert "bbb" not in result
    assert "other=1" in result


def test_redacts_postgres_dsn_password():
    text = (
        "connection to server failed: could not connect to "
        "postgresql+psycopg://postgres:change_me@db:5432/earnings_decision_lab"
    )
    result = redact(text)
    assert "change_me" not in result
    assert "postgres:REDACTED@db" in result


def test_redacts_dsn_password_alongside_query_param():
    text = "url='https://user:hunter2@example.com/path?token=abc123'"
    result = redact(text)
    assert "hunter2" not in result
    assert "abc123" not in result
