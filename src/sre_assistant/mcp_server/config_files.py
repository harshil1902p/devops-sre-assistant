"""Safe config-file access: allowlisted directory, allowlisted extensions, size cap, no traversal."""

import posixpath

ALLOWED_EXTENSIONS = (".yaml", ".yml", ".json", ".toml", ".ini", ".conf")
MAX_CONFIG_BYTES = 64 * 1024


class ConfigAccessError(ValueError):
    pass


def resolve_config_path(config_dir: str, filepath: str) -> str:
    """Return the absolute in-container path, or raise if it escapes `config_dir` or isn't a config file."""
    if not filepath or "\x00" in filepath:
        raise ConfigAccessError("filepath is empty or contains a NUL byte")
    root = posixpath.normpath(config_dir)
    candidate = posixpath.normpath(posixpath.join(root, filepath))
    if candidate != root and not candidate.startswith(root + "/"):
        raise ConfigAccessError(f"'{filepath}' is outside the allowed config directory {root}")
    if not candidate.lower().endswith(ALLOWED_EXTENSIONS):
        raise ConfigAccessError(f"only config files are readable ({', '.join(ALLOWED_EXTENSIONS)})")
    return candidate
