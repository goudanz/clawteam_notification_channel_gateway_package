import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:
    yaml = None


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML not installed. Run: pip install pyyaml")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@dataclass
class AppConfig:
    channels: dict[str, Any]
    bindings: dict[str, Any]

    @classmethod
    def load(cls, base_dir: Path) -> "AppConfig":
        channels_file = os.environ.get("CBG_CHANNELS", "./configs/channels.yaml")
        bindings_file = os.environ.get("CBG_BINDINGS", "./configs/bindings.yaml")

        channels_path = (base_dir / channels_file).resolve() if not Path(channels_file).is_absolute() else Path(channels_file)
        bindings_path = (base_dir / bindings_file).resolve() if not Path(bindings_file).is_absolute() else Path(bindings_file)

        if not channels_path.exists():
            raise RuntimeError(f"channels config not found: {channels_path}")
        if not bindings_path.exists():
            raise RuntimeError(f"bindings config not found: {bindings_path}")

        channels = _load_yaml(channels_path)
        bindings = _load_yaml(bindings_path)
        return cls(channels=channels, bindings=bindings)
