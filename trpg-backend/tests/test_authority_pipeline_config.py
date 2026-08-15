"""验证 Issue #10 临时权威流水线开关保持显式且默认启用 v2。"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_authority_pipeline_defaults_to_v2() -> None:
    settings = Settings(_env_file=None)  # ty: ignore[unknown-argument]

    assert settings.authority_pipeline_mode == "v2"


def test_authority_pipeline_accepts_only_declared_migration_modes() -> None:
    assert (
        Settings(
            _env_file=None,  # ty: ignore[unknown-argument]
            authority_pipeline_mode="shadow",
        ).authority_pipeline_mode
        == "shadow"
    )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # ty: ignore[unknown-argument]
            authority_pipeline_mode="unsafe",  # ty: ignore[invalid-argument-type]
        )
