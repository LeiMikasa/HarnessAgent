"""Configuration.

Everything that varies between machines, providers, and runs lives here.  The
guiding rule: switching providers is a `.env` edit, never a code change.

    .env  ->  load_settings()  ->  Settings  ->  handed to the runtime
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv is a declared dependency but the harness works without it
    from dotenv import load_dotenv as _dotenv_load
except ImportError:  # pragma: no cover - exercised when dotenv is absent
    _dotenv_load = None


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal `.env` parser: KEY=VALUE, `#` comments, optional quotes.

    Used when python-dotenv is unavailable, so a missing optional dependency
    degrades to "slightly less clever parsing" rather than "no configuration".
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _apply_env_file(path: Path, *, override: bool) -> Path | None:
    """Load a `.env` file into `os.environ`.  Returns the path on success."""
    if not path.is_file():
        return None
    if _dotenv_load is not None:
        try:
            _dotenv_load(path, override=override)
            return path
        except Exception:  # noqa: BLE001 - fall through to the built-in parser
            pass
    for key, value in _parse_env_file(path).items():
        if override or key not in os.environ:
            os.environ[key] = value
    return path


# Provider presets.  A preset only supplies defaults; explicit environment
# variables always win.
PROVIDER_PRESETS: dict[str, dict[str, str | None]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/anthropic",
        "model": "deepseek-v4-flash",
        "key_var": "ANTHROPIC_API_KEY",
    },
    "anthropic": {
        "base_url": None,  # SDK default
        "model": "claude-sonnet-4-6",
        "key_var": "ANTHROPIC_API_KEY",
    },
    "mock": {
        "base_url": None,
        "model": "mock-1",
        "key_var": "",
    },
}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _env_path(name: str) -> Path | None:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return None
    return Path(str(raw).strip()).expanduser()


@dataclass
class Settings:
    """Resolved runtime configuration."""

    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    base_url: str | None = None
    api_key: str | None = None

    workdir: Path = field(default_factory=Path.cwd)
    state_dir: Path = field(default_factory=lambda: Path.cwd() / ".agent")
    skill_dirs: list[Path] = field(default_factory=list)

    max_tokens: int = 8000
    max_turns: int = 50
    subagent_max_turns: int = 30
    goal_evaluator_model: str | None = None
    goal_block_cap: int = 8

    # Populated by `load_settings` so callers can report what was picked up.
    env_file: Path | None = None

    # -- derived paths -------------------------------------------------------

    @property
    def tasks_dir(self) -> Path:
        return self.state_dir / "tasks"

    @property
    def mailbox_dir(self) -> Path:
        return self.state_dir / "mailbox"

    @property
    def memory_dir(self) -> Path:
        return self.state_dir / "memory"

    @property
    def transcripts_dir(self) -> Path:
        return self.state_dir / "transcripts"

    @property
    def worktrees_dir(self) -> Path:
        return self.state_dir / "worktrees"

    @property
    def is_mock(self) -> bool:
        return self.provider == "mock"

    def ensure_dirs(self) -> None:
        for path in (
            self.state_dir,
            self.tasks_dir,
            self.mailbox_dir,
            self.memory_dir,
            self.transcripts_dir,
            self.worktrees_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        base = self.base_url or "(SDK default)"
        key = "set" if self.api_key else "missing"
        return (
            f"provider={self.provider} model={self.model} base_url={base} "
            f"api_key={key} workdir={self.workdir}"
        )


def _split_paths(raw: str) -> list[Path]:
    separator = ";" if os.name == "nt" else ":"
    parts = [chunk.strip() for chunk in raw.split(separator)]
    return [Path(part).expanduser() for part in parts if part]


def load_settings(env_file: str | Path | None = None, **overrides) -> Settings:
    """Load `.env`, resolve the provider, and return a `Settings`.

    Explicit keyword overrides win over environment variables, which win over
    provider presets.  This ordering is what lets a test force `provider="mock"`
    without touching the developer's real `.env`.
    """
    candidate = Path(env_file) if env_file else Path.cwd() / ".env"
    loaded_from = _apply_env_file(candidate, override=True)
    if loaded_from is None:
        # Still pick up a .env sitting next to the package, if any.
        for fallback in (Path(__file__).resolve().parent.parent / ".env",):
            if _apply_env_file(fallback, override=False) is not None:
                loaded_from = fallback
                break

    provider = str(overrides.get("provider") or os.getenv("AGENT_PROVIDER") or "deepseek")
    provider = provider.strip().lower()
    if provider not in PROVIDER_PRESETS:
        provider = "deepseek"
    preset = PROVIDER_PRESETS[provider]

    base_url = overrides.get("base_url") or os.getenv("ANTHROPIC_BASE_URL") or preset["base_url"]
    model = overrides.get("model") or os.getenv("MODEL_ID") or preset["model"]

    key_var = preset.get("key_var") or ""
    api_key = overrides.get("api_key")
    if api_key is None and key_var:
        api_key = os.getenv(key_var)
    if api_key is None and provider == "mock":
        api_key = "mock"

    workdir = overrides.get("workdir") or _env_path("AGENT_WORKDIR") or Path.cwd()
    workdir = Path(workdir).resolve()

    state_dir = overrides.get("state_dir") or _env_path("AGENT_STATE_DIR")
    state_dir = Path(state_dir).resolve() if state_dir else workdir / ".agent"

    raw_skill_dirs = os.getenv("AGENT_SKILL_DIRS", "").strip()
    skill_dirs = overrides.get("skill_dirs")
    if skill_dirs is None:
        skill_dirs = _split_paths(raw_skill_dirs) if raw_skill_dirs else [workdir / "skills"]

    settings = Settings(
        provider=provider,
        model=str(model),
        base_url=str(base_url) if base_url else None,
        api_key=str(api_key) if api_key else None,
        workdir=workdir,
        state_dir=state_dir,
        skill_dirs=[Path(p).resolve() for p in skill_dirs],
        max_tokens=int(overrides.get("max_tokens") or _env_int("AGENT_MAX_TOKENS", 8000)),
        max_turns=int(overrides.get("max_turns") or _env_int("AGENT_MAX_TURNS", 50)),
        subagent_max_turns=int(
            overrides.get("subagent_max_turns") or _env_int("AGENT_SUBAGENT_MAX_TURNS", 30)
        ),
        goal_evaluator_model=overrides.get("goal_evaluator_model") or os.getenv("GOAL_EVALUATOR_MODEL_ID") or None,
        goal_block_cap=int(overrides.get("goal_block_cap") or _env_int("AGENT_GOAL_BLOCK_CAP", 8)),
        env_file=loaded_from,
    )
    return settings
