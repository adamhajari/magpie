"""Configuration loader for Magpie — reads ~/.magpie/config.toml."""

from __future__ import annotations

import pathlib
import tomllib
from dataclasses import dataclass


class ConfigError(Exception):
    """Configuration file error."""

    pass


@dataclass
class AwsConfig:
    """AWS S3 configuration for cold storage uploads."""

    profile: str
    region: str
    bucket: str


def get_config_path() -> pathlib.Path:
    """Return path to config file: ~/.magpie/config.toml."""
    return pathlib.Path.home() / ".magpie" / "config.toml"


def load_aws_config() -> AwsConfig:
    """Load AWS config from ~/.magpie/config.toml.

    Raises ConfigError if config file missing or keys incomplete.
    """
    config_path = get_config_path()

    if not config_path.exists():
        raise ConfigError(
            f"Config file not found: {config_path}\n"
            "Run: magpie configure"
        )

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        raise ConfigError(f"Failed to parse {config_path}: {e}")

    if "aws" not in data:
        raise ConfigError(
            f"Missing [aws] section in {config_path}\n"
            "Run: magpie configure"
        )

    aws_section = data["aws"]
    required = {"profile", "region", "bucket"}
    missing = required - set(aws_section.keys())

    if missing:
        raise ConfigError(
            f"Missing AWS config keys in {config_path}: {', '.join(missing)}\n"
            "Run: magpie configure"
        )

    return AwsConfig(
        profile=aws_section["profile"],
        region=aws_section["region"],
        bucket=aws_section["bucket"],
    )


def save_aws_config(profile: str, region: str, bucket: str) -> None:
    """Save AWS config to ~/.magpie/config.toml."""
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    content = f"""[aws]
profile = "{profile}"
region  = "{region}"
bucket  = "{bucket}"
"""

    with open(config_path, "w") as f:
        f.write(content)
