"""Focused regressions for the Release 6.0.6 direct dependency updates."""

from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from jose import jwt
from python_multipart.multipart import MultipartParseError, MultipartParser, QuerystringParser

from app.core.config import Settings, get_settings
from app.core.security import TokenExpiredError, TokenInvalidError, create_token, decode_token


def test_jwt_rejects_invalid_signature_malformed_and_wrong_algorithm() -> None:
    settings = get_settings()
    valid, _ = create_token(subject="security-regression", token_type="access")
    header, payload, encoded_signature = valid.split(".")
    signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
    corrupted_signature = bytes([signature[0] ^ 1]) + signature[1:]
    assert corrupted_signature != signature
    encoded_corrupted_signature = (
        base64.urlsafe_b64encode(corrupted_signature).rstrip(b"=").decode()
    )
    corrupted = ".".join((header, payload, encoded_corrupted_signature))

    with pytest.raises(TokenInvalidError):
        decode_token(corrupted)
    with pytest.raises(TokenInvalidError):
        decode_token("not-a-jwt")

    wrong_algorithm = jwt.encode(
        {"sub": "security-regression", "typ": "access"},
        settings.jwt_secret_key,
        algorithm="HS384",
    )
    with pytest.raises(TokenInvalidError):
        decode_token(wrong_algorithm)


def test_jwt_rejects_expired_token() -> None:
    expired, _ = create_token(
        subject="security-regression",
        token_type="access",
        expires_delta=timedelta(seconds=-1),
    )
    with pytest.raises(TokenExpiredError):
        decode_token(expired)


def test_urlencoded_semicolon_is_data_not_a_field_separator() -> None:
    fields: list[tuple[bytes, bytes]] = []
    name = bytearray()
    value = bytearray()

    def on_field_start() -> None:
        name.clear()
        value.clear()

    def on_field_name(data: bytes, start: int, end: int) -> None:
        name.extend(data[start:end])

    def on_field_data(data: bytes, start: int, end: int) -> None:
        value.extend(data[start:end])

    def on_field_end() -> None:
        fields.append((bytes(name), bytes(value)))

    parser = QuerystringParser(
        {
            "on_field_start": on_field_start,
            "on_field_name": on_field_name,
            "on_field_data": on_field_data,
            "on_field_end": on_field_end,
        }
    )
    parser.write(b"role=user&metadata=;role=admin")
    parser.finalize()

    assert fields == [(b"role", b"user"), (b"metadata", b";role=admin")]


def test_multipart_parser_enforces_default_header_count_limit() -> None:
    body = (
        b"--security-boundary\r\n"
        + b"".join(f"X-Test-{index}: value\r\n".encode() for index in range(9))
        + b"\r\ndata\r\n--security-boundary--\r\n"
    )
    parser = MultipartParser(b"security-boundary", {})

    with pytest.raises(MultipartParseError, match="Maximum header count exceeded"):
        parser.write(body)


def test_settings_load_values_from_dotenv_file(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PASSWORD_MIN_LENGTH=19\n", encoding="utf-8")

    settings = Settings(_env_file=env_file)

    assert settings.password_min_length == 19
