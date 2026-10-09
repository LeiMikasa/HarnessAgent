"""Skill loading -- knowledge on demand, not upfront.

Every skill is a directory containing a `SKILL.md` with YAML frontmatter:

    ---
    name: code-review
    description: Review a diff for correctness and style problems.
    ---

    # Code Review
    ...full instructions...

At startup the harness reads only the frontmatter, so a hundred skills cost a
hundred catalog lines.  The body is pulled in only when the model decides the
skill applies:

    system prompt
      |
      +-- Skills available:
      |     - code-review: Review a diff for correctness...
      |     - agent-builder: Scaffold a new agent harness...
      |
      +-- "Use load_skill to read the full instructions when a skill applies."
                    |
                    v
              load_skill("code-review")  ->  the entire SKILL.md
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .tools.registry import ToolContext, ToolRegistry
from .tools.result import ToolResult

MANIFEST_NAME = "SKILL.md"
MAX_SKILL_CHARS = 60_000


@dataclass
class Skill:
    name: str
    description: str
    content: str
    path: Path

    @property
    def directory(self) -> Path:
        return self.path.parent


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a `---`-delimited YAML header from the body.

    Returns `({}, text)` when there is no well-formed header, so a plain
    markdown file is still a usable skill.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}, text

    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.rstrip("\r\n") == "---"),
        None,
    )
    if closing is None:
        return {}, text

    header = "".join(lines[1:closing])
    body = "".join(lines[closing + 1:]).strip()

    metadata: dict = {}
    if yaml is not None:
        try:
            loaded = yaml.safe_load(header)
            if isinstance(loaded, dict):
                metadata = loaded
        except Exception:
            metadata = {}
    if not metadata:
        # Minimal fallback parser: `key: value` pairs, one level deep.
        for line in header.splitlines():
            if ":" not in line or line.lstrip().startswith("#"):
                continue
            key, _, value = line.partition(":")
            metadata[key.strip()] = value.strip().strip("'\"")
    return metadata, body


class SkillLoader:
    """Discovers `*/SKILL.md` under one or more roots."""

    def __init__(self, roots: list[Path] | Path | None = None):
        if roots is None:
            roots = []
        elif isinstance(roots, (str, Path)):
            roots = [Path(roots)]
        self.roots = [Path(root) for root in roots]
        self.skills: dict[str, Skill] = {}
        self.scan()

    # -- discovery ----------------------------------------------------------

    def scan(self) -> dict[str, Skill]:
        self.skills.clear()
        for manifest in self._manifests():
            skill = self._load_manifest(manifest)
            if skill is None:
                continue
            # First root wins, so a project skill can shadow a global one.
            self.skills.setdefault(skill.name, skill)
        return self.skills

    def _manifests(self) -> list[Path]:
        found: list[Path] = []
        seen: set[Path] = set()
        for root in self.roots:
            root = Path(root)
            if not root.is_dir():
                continue
            root_resolved = root.resolve()
            for manifest in sorted(root.glob(f"*/{MANIFEST_NAME}")):
                try:
                    resolved = manifest.resolve()
                except (OSError, RuntimeError):
                    continue
                if not resolved.is_file() or not resolved.is_relative_to(root_resolved):
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                found.append(resolved)
        return found

    def _load_manifest(self, manifest: Path) -> Skill | None:
        try:
            content = manifest.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        metadata, body = parse_frontmatter(content)

        raw_name = metadata.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        name = name or manifest.parent.name

        raw_description = metadata.get("description")
        description = raw_description.strip() if isinstance(raw_description, str) else ""
        if not description:
            first_line = next((line for line in body.splitlines() if line.strip()), "")
            description = first_line
        description = " ".join(str(description).lstrip("# ").split())
        if len(description) > 300:
            description = description[:297] + "..."

        return Skill(name=name, description=description, content=content, path=manifest)

    # -- queries ------------------------------------------------------------

    def catalog(self) -> str:
        if not self.skills:
            return "(no skills found)"
        width = max(len(name) for name in self.skills)
        return "\n".join(
            f"- {skill.name.ljust(width)}: {skill.description}"
            for skill in self.skills.values()
        )

    def names(self) -> list[str]:
        return list(self.skills)

    def get(self, name: str) -> Skill | None:
        if name in self.skills:
            return self.skills[name]
        # Tolerate case and separators: "Code Review" -> "code-review"
        wanted = name.strip().lower().replace("_", "-").replace(" ", "-")
        for key, skill in self.skills.items():
            if key.lower().replace("_", "-").replace(" ", "-") == wanted:
                return skill
        return None

    def load(self, name: str) -> str:
        skill = self.get(name)
        if skill is None:
            available = ", ".join(self.skills) or "none"
            return ToolResult.failure('NOT_FOUND', f"Error: unknown skill {name!r}. Available: {available}", action='inspect', execution_status='not_executed')
        content = skill.content
        if len(content) > MAX_SKILL_CHARS:
            content = content[:MAX_SKILL_CHARS] + "\n... (skill truncated)"
        return content


# --------------------------------------------------------------------------
# Tool
# --------------------------------------------------------------------------


def run_load_skill(args: dict, ctx: ToolContext) -> str:
    name = args.get("name", "")
    if not isinstance(name, str) or not name.strip():
        return ToolResult.failure('INVALID_ARGUMENT', "Error: name is required", action='correct_arguments', execution_status='not_executed')
    loader = _loader(ctx)
    if loader is None:
        return ToolResult.failure('TOOL_UNAVAILABLE', "Error: no skill loader is configured for this session", action='report', execution_status='not_executed')
    return loader.load(name.strip())


def _loader(ctx: ToolContext) -> SkillLoader | None:
    runtime = ctx.runtime
    if runtime is not None and getattr(runtime, "skills", None) is not None:
        return runtime.skills
    return ctx.extra.get("skills")


LOAD_SKILL_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string", "description": "Skill name from the catalog."}},
    "required": ["name"],
}

LOAD_SKILL_DESCRIPTION = (
    "Load the full instructions for a skill listed in the system prompt. "
    "Call this before starting work the skill covers."
)


def register_skill_tools(registry: ToolRegistry) -> ToolRegistry:
    registry.add("load_skill", LOAD_SKILL_DESCRIPTION, LOAD_SKILL_SCHEMA, run_load_skill, read_only=True)
    return registry
