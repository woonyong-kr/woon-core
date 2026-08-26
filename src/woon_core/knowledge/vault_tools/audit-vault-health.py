#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

from woon_core.calendar.constants import (
    LINK_CALENDAR_DASHBOARD_CSS_CLASS,
    LINK_CALENDAR_PLUGIN_ID,
    LINK_CALENDAR_PROFILE_ID,
)
from woon_core.errors import WoonError
from woon_core.knowledge.source_boundary import audit_source_boundary
from woon_core.knowledge.wiki_tree import (
    LEGACY_TREE_FIELDS,
    load_wiki_tree,
)

VAULT = Path.cwd().resolve()

CONTENT_ROOTS = [
    "README.md",
    "index.md",
    "head-quarter.md",
    "brain/home.md",
    "brain/log.md",
    "brain/review",
    "wiki",
    "inbox",
]

OPERATING_ROOTS = [
    "AGENTS.md",
    "types",
]

TEMPLATE_ROOTS = [
    "templates",
]

MANAGED_NON_MARKDOWN_ROOTS = [
    "wiki",
    "maps",
    "inbox",
    "types",
    "templates",
]

QUARTZ_SYNC_ROOTS = {
    "README.md",
    "index.md",
    "maps",
    "wiki",
}

SKIP_DIRS = {
    ".git",
    ".obsidian",
    ".legacy-backup",
    ".drawio-backup",
    "quartz",
    "scripts",
    "templates",
    "types",
    "assets",
    "exports",
    "_sources",
}
OPERATING_SKIP_DIRS = SKIP_DIRS - {"types"}
TEMPLATE_SKIP_DIRS = SKIP_DIRS - {"templates"}

WIKILINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
LINK_LIST_SLUG_RE = re.compile(r"\[\[([^\]|#\n]+)(?:#[^\]|]+)?\]\]\s*(?:—|--|-)\s+\S")
ABSOLUTE_LOCAL_RE = re.compile(
    r"""(?<![\w.:/-])(?:/Users|/home)/[^/\s`)\]'\"]+(?:/[^\s`)\]'\"]+)+"""
)
LOCAL_ASSET_FIELDS = {"source_images"}
TRANSIENT_FILE_NAMES = {".DS_Store"}
TRANSIENT_FILE_PREFIXES = (".fuse_hidden",)
TRANSIENT_FILE_SUFFIXES = ("~", ".tmp", ".bak")
SOURCE_LIFECYCLES = {"captured", "compiled", "archived"}
SOURCE_KINDS = {"web", "book", "lecture", "transcript", "clipping"}
TEMPLATE_TYPES = {"Wiki", "키워드", "Source", "Creative", "Daily", "Operations"}
OBSIDIAN_GRAPH_FILTER = "path:wiki tag:#graph/overview -path:wiki/private/_sources"
GLOBAL_GRAPH_ROOT = "wiki/README.md"
OBSIDIAN_FRONT_MATTER_TITLE_PLUGIN = "obsidian-front-matter-title-plugin"
OBSIDIAN_EXPLORER_SNIPPET = "focus-workspace"
MERMAID_PLACEHOLDER_LABELS = {
    "ev",
    "kernel",
    "link",
    "l_run",
    "ok",
    "qemu_cr3",
    "qemu_iret",
    "release",
    "ret",
    "rollback",
    "si",
    "sleep3",
    "sleep5",
    "softmmu",
}
MERMAID_LEGACY_MARKERS = (
    "B&gt;",
    "&lt;/B",
    "&lt;/FONT",
    "&lt;/TABLE",
    "TABLE BORDER=",
    "FONT POINT-SIZE=",
)
MERMAID_NEUTRAL_STYLE_MARKER = "woon-mermaid-neutral-v1"
MERMAID_COLOR_DIRECTIVE_RE = re.compile(
    r"^\s*(?:style\s+\S+|classDef\s+\S+|linkStyle\s+\S+)"
    r"[^\n]*(?:fill|stroke|color)\s*:",
    re.IGNORECASE,
)
MERMAID_THEME_COLOR_RE = re.compile(
    r"%%\{init:[^\n]*(?:themeVariables|themeCSS)",
    re.IGNORECASE,
)
OBSIDIAN_HIDDEN_EXPLORER_PATHS = (
    "assets",
    "catalog",
    "config",
    "docs",
    "evals",
    "exports",
    "quartz",
    "scripts",
    "templates",
    "types",
    "AGENTS.md",
    "CLAUDE.md",
)
OBSIDIAN_IGNORED_PATHS = (
    ".local/",
    "catalog/",
    "config/",
    "docs/",
    "evals/",
    "exports/",
    "wiki/private/_sources/",
    ".json",
)
RETIRED_PRIVATE_NOVEL_ROOT = VAULT / "projects/writing"
RETIRED_WORKSPACE_ROOTS = (
    "ai-reference",
    "_quarantine",
    ".local/woon-brain",
    ".local/codex-write-vault",
    ".legacy-backup",
    "wiki/canonical",
    "brain/wiki",
    "projects",
    "content",
    "users",
)
RETIRED_LEGACY_VIEW_ROOTS = (
    "maps/legacy",
    "maps/samples",
)
RETIRED_LEGACY_VIEW_PATHS = ("inbox/recently-touched.base",)
RETIRED_AI_INSTRUCTION_LOCATORS = (
    "ai-reference/",
    "_quarantine/ai-reference-legacy",
    ".local/woon-brain",
    ".local/codex-write-vault",
    "sync-llm-wiki-pilot",
)
BRAIN_ACTIVITY_KINDS = {"learning", "decision", "task", "meeting", "application"}
BRAIN_COMPLETION_SOURCES = {"user_confirmed", "not-completed"}
PERSON_SCHEMA_PATH = "config/person-schema.json"
DEFAULT_OWNER_ID = "choi-woonyoung"
GENERAL_PERSON_KINDS = {
    "vault-owner",
    "related-person",
    "public-author",
    "organization-representative",
}
VAULT_EXECUTABLE_SUFFIXES = {".py", ".sh", ".js", ".mjs", ".ts"}
CALENDAR_PROJECTION_ROOT = "inbox/calendar/events"
CALENDAR_ICS_PROJECTION_PATH = "inbox/calendar/apple-calendar.ics"
CALENDAR_DASHBOARD_PROJECTION_PATH = "inbox/calendar/apple-calendar.md"
CALENDAR_NOTION_DATABASE_FILENAME = "_database.md"
CALENDAR_ICS_PROJECTION_PRODID = "PRODID:-//Woon//Apple Calendar Read-only Projection//KO"
CALENDAR_PROJECTION_FILE_MODE = 0o400
CALENDAR_PROJECTION_DIRECTORY_MODE = 0o500
NONCANONICAL_MAP_ROOTS = ("maps/legacy/", "maps/samples/")
ALLOWED_WIKI_FACETS = {"개념", "프로젝트", "리소스", "인물", "커리어", "학습", "생활"}
ALLOWED_VISIBLE_ROOT_DIRECTORIES = {
    "assets",
    "brain",
    "catalog",
    "config",
    "docs",
    "evals",
    "exports",
    "inbox",
    "maps",
    "templates",
    "types",
    "wiki",
}
DAILY_DIGEST_EMBED_RE = re.compile(r"!\[\[\.\./daily-digests/(\d{4}-\d{2}-\d{2})\]\]")


def rel(path: Path) -> str:
    return path.relative_to(VAULT).as_posix()


def unexpected_root_directory_issues(vault: Path) -> list[str]:
    """Reject visible top-level folders that bypass the canonical Vault layout."""

    if not vault.is_dir():
        return []
    return [
        f"unexpected visible Vault root directory: {path.name}"
        for path in sorted(vault.iterdir(), key=lambda item: item.name.casefold())
        if path.is_dir()
        and not path.name.startswith(".")
        and path.name not in ALLOWED_VISIBLE_ROOT_DIRECTORIES
    ]


def is_noncanonical_map_archive(path: Path) -> bool:
    """Return whether a preserved map export is outside the active graph corpus.

    Legacy Canvas/Mindmap exports and examples remain local files for reference,
    but they must neither compete with current map titles nor enter the global
    knowledge graph.  They are intentionally still linkable from current notes.
    """

    return rel(path).startswith(NONCANONICAL_MAP_ROOTS)


def wiki_and_entity_policy_issues(
    relative: str, text: str, frontmatter: dict[str, object]
) -> tuple[list[str], list[str]]:
    wiki: list[str] = []
    entities: list[str] = []
    if relative.startswith(("brain/wiki/", "projects/", "content/", "users/")):
        entities.append(f"{relative}: retired parallel knowledge root must not exist")
        return wiki, entities
    if not relative.startswith("wiki/"):
        return wiki, entities
    if frontmatter.get("type") != "Wiki":
        wiki.append(f"{relative}: Wiki document type must be Wiki")
    canonical_id = frontmatter.get("canonical_id")
    if (
        not isinstance(canonical_id, str)
        or not canonical_id.strip()
        or len(canonical_id) > 160
        or canonical_id.startswith("/")
        or ".." in Path(canonical_id).parts
        or any(char.isspace() for char in canonical_id)
    ):
        wiki.append(f"{relative}: canonical_id must be a stable path-independent identity")
    facets = frontmatter.get("facets", [])
    if not isinstance(facets, list):
        wiki.append(f"{relative}: facets must be a list when present")
        facets = []
    elif facets and (
        len(facets) != len(set(str(value) for value in facets)) or set(facets) - ALLOWED_WIKI_FACETS
    ):
        wiki.append(f"{relative}: facets must be unique values from the Wiki contract")
    if frontmatter.get("knowledge_state") not in {
        "생각 중",
        "확인 필요",
        "근거 확인됨",
        "오래됨",
        "폐기됨",
    }:
        wiki.append(f"{relative}: knowledge_state is invalid")
    for field in LEGACY_TREE_FIELDS.intersection(frontmatter):
        wiki.append(f"{relative}: legacy Wiki tree field remains: {field}")
    node_kind = frontmatter.get("node_kind")
    if node_kind not in {"root", "hub", "topic", "entity", "detail", "decision"}:
        wiki.append(f"{relative}: node_kind is invalid")
    view_mode = frontmatter.get("view_mode")
    if view_mode not in {"tree", "linear", "project", "topic-timeline", "article"}:
        wiki.append(f"{relative}: view_mode is invalid")
    keywords = frontmatter.get("keywords")
    if (
        not isinstance(keywords, list)
        or not keywords
        or not all(isinstance(value, str) and value.strip() for value in keywords)
    ):
        wiki.append(f"{relative}: keywords must be a non-empty string list")
    if not isinstance(frontmatter.get("aliases"), list):
        wiki.append(f"{relative}: aliases must be a list")
    if not isinstance(frontmatter.get("updated"), (str, date, datetime)):
        wiki.append(f"{relative}: updated is required")
    parent = frontmatter.get("parent")
    if relative == "wiki/README.md" and parent not in {None, ""}:
        wiki.append(f"{relative}: Wiki root must not have a parent")
    elif relative != "wiki/README.md" and not isinstance(parent, str):
        wiki.append(f"{relative}: visible Wiki must have exactly one parent")
    if frontmatter.get("question_kind") == "interview":
        title = frontmatter.get("title")
        filename = Path(relative).stem
        interview_tracks = frontmatter.get("interview_tracks")
        question_topic = frontmatter.get("question_topic")
        if re.match(r"(?i)^(?:q\d{1,3}[-_. ]|archive-)", filename) or (
            isinstance(canonical_id, str)
            and re.search(r"(?i)/(?:q\d{1,3}[-_.]|archive-)", canonical_id)
        ):
            wiki.append(f"{relative}: interview identity must not use sequence or archive prefixes")
        if isinstance(title, str) and re.match(r"(?i)^q\d{1,3}[. ]", title):
            wiki.append(f"{relative}: interview title must be a natural question without numbering")
        if not isinstance(interview_tracks, list) or not interview_tracks:
            wiki.append(f"{relative}: interview_tracks must be a non-empty list")
        if not isinstance(question_topic, str) or not question_topic.strip():
            wiki.append(f"{relative}: question_topic must name a reusable semantic topic")
        if parent == "[[wiki/README|Wiki]]":
            wiki.append(
                f"{relative}: interview question must use a semantic parent below Wiki root"
            )
        if isinstance(parent, str):
            parent_match = re.fullmatch(
                r"\[\[(?P<path>[^\]|#]+)(?:#[^\]|]+)?(?:\|(?P<title>[^\]]+))?\]\]",
                parent.strip(),
            )
            if parent_match is not None:
                parent_path = parent_match.group("path")
                parent_title = parent_match.group("title") or Path(parent_path).name
                if re.fullmatch(r"wiki/personal/interview/[^/]+/README", parent_path):
                    wiki.append(
                        f"{relative}: interview question must not use a job track as its parent"
                    )
                if (
                    isinstance(question_topic, str)
                    and question_topic.strip()
                    and parent_title.strip() != question_topic.strip()
                ):
                    wiki.append(f"{relative}: question_topic must match the semantic parent title")
    if frontmatter.get("entity_kind") == "project" and (
        not isinstance(frontmatter.get("project_id"), str)
        or not isinstance(frontmatter.get("objective"), str)
    ):
        entities.append(f"{relative}: project facet requires project_id and objective")
    if frontmatter.get("entity_kind") == "content" and frontmatter.get(
        "content_kind"
    ) not in {
        "book",
        "film",
        "series",
        "lecture",
        "course",
        "podcast",
        "game",
        "article",
        "exhibition",
        "learning-material-bundle",
        "novel",
    }:
        entities.append(f"{relative}: content facet requires a valid content_kind")
    return wiki, entities


def nonwiki_keyword_policy_issues(relative: str, frontmatter: dict[str, object]) -> list[str]:
    """Keep human/AI keyword knowledge in the one canonical Wiki tree."""

    if not relative.startswith("wiki/") and frontmatter.get("type") == "키워드":
        return [f"{relative}: keyword knowledge belongs only under wiki/; use an operational type"]
    return []


def runtime_permission_issues(vault: Path) -> list[str]:
    """Require user-only permissions for private runtime state and receipts."""

    root = vault / ".local/woon-knowledge"
    if not root.exists():
        return []
    issues: list[str] = []
    for path in sorted((root, *root.rglob("*"))):
        relative = path.relative_to(vault).as_posix()
        if path.is_symlink():
            issues.append(f"{relative}: runtime symlinks are forbidden")
        elif path.is_dir() and path.stat().st_mode & 0o777 != 0o700:
            issues.append(f"{relative}: runtime directory mode must be 0700")
        elif path.is_file() and path.stat().st_mode & 0o777 != 0o600:
            issues.append(f"{relative}: runtime file mode must be 0600")
    return issues


def obsidian_workspace_issues(vault: Path) -> list[str]:
    """Reject saved Obsidian tabs that still point at removed Vault files."""

    issues: list[str] = []
    obsidian = vault / ".obsidian"

    def check_reference(
        referenced: str,
        *,
        source: str,
        allow_directory: bool,
        allow_missing: bool = False,
    ) -> None:
        candidate = Path(referenced)
        if candidate.is_absolute() or ".." in candidate.parts:
            issues.append(f"{source}: unsafe saved file reference: {referenced}")
            return
        target = (vault / candidate).resolve()
        try:
            target.relative_to(vault.resolve())
        except ValueError:
            issues.append(f"{source}: saved file escapes Vault: {referenced}")
            return
        exists = target.exists() if allow_directory else target.is_file()
        if target.is_symlink() or (not exists and not allow_missing):
            issues.append(f"{source}: saved file is missing: {referenced}")

    def visit(value: object, *, source: str) -> None:
        if isinstance(value, dict):
            referenced = value.get("file")
            if isinstance(referenced, str):
                check_reference(referenced, source=source, allow_directory=False)
            for child in value.values():
                visit(child, source=source)
        elif isinstance(value, list):
            for child in value:
                visit(child, source=source)

    for name in ("workspace.json", "workspaces.json"):
        path = obsidian / name
        if not path.exists():
            continue
        if not path.is_file() or path.is_symlink():
            issues.append(f".obsidian/{name}: workspace state path is unsafe")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            issues.append(f".obsidian/{name}: workspace state is invalid JSON")
            continue
        source = f".obsidian/{name}"
        visit(payload, source=source)
        if name == "workspace.json" and isinstance(payload, dict):
            last_open = payload.get("lastOpenFiles", [])
            if isinstance(last_open, list):
                for referenced in last_open:
                    if isinstance(referenced, str):
                        # Obsidian keeps harmless deleted paths in its recent
                        # history while the app is open. Active pane state is
                        # still validated above; a stale recent item must not
                        # fail the knowledge pipeline or be recreated.
                        check_reference(
                            referenced,
                            source=source,
                            allow_directory=True,
                            allow_missing=True,
                        )
    return sorted(set(issues))


def daily_digest_embed_issues(vault: Path) -> list[str]:
    """Reject the retired duplicate-DailyDigest projection shape.

    Conversation conclusions now belong in the Core-owned marker block inside
    the sole daily record.  Keeping an embed or a standalone generated digest
    creates two user-visible histories for one date.
    """

    issues: list[str] = []
    daily_root = vault / "inbox/daily"
    if not daily_root.exists():
        return issues
    for note in sorted(daily_root.glob("????-??-??.md")):
        text = note.read_text(encoding="utf-8")
        for day in DAILY_DIGEST_EMBED_RE.findall(text):
            issues.append(
                f"{note.relative_to(vault).as_posix()}: retired Codex digest embed for {day}"
            )
    legacy_root = vault / "inbox/daily-digests"
    if legacy_root.is_dir():
        for digest in sorted(legacy_root.glob("*.md")):
            issues.append(
                f"{digest.relative_to(vault).as_posix()}: retired duplicate daily digest file"
            )
    return issues


def vault_execution_ownership_issues(vault: Path) -> list[str]:
    """Reject executable maintenance code in the document Vault.

    Raw source material may contain code under ``wiki/private/_sources``. This guard only
    protects runnable top-level files and the retired ``scripts/`` runtime
    folder, both of which must be owned by ``woon-core``.
    """

    issues: list[str] = []
    scripts = vault / "scripts"
    if scripts.exists():
        issues.append("scripts/ must be owned by woon-core, not the Vault")
    for path in sorted(vault.iterdir()):
        if path.is_file() and path.suffix.lower() in VAULT_EXECUTABLE_SUFFIXES:
            issues.append(f"top-level executable source must move to woon-core: {path.name}")
    return issues


def iter_markdown() -> list[Path]:
    files: list[Path] = []
    for root in CONTENT_ROOTS:
        path = VAULT / root
        if path.is_file() and path.suffix == ".md":
            files.append(path)
        elif path.is_dir():
            for item in path.rglob("*.md"):
                parts = set(item.relative_to(VAULT).parts)
                if parts & SKIP_DIRS:
                    continue
                files.append(item)
    return sorted(set(files))


def iter_content_files() -> list[Path]:
    files: list[Path] = []
    for root in CONTENT_ROOTS:
        path = VAULT / root
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            for item in path.rglob("*"):
                if not item.is_file():
                    continue
                parts = set(item.relative_to(VAULT).parts)
                if parts & SKIP_DIRS:
                    continue
                files.append(item)
    return sorted(set(files))


def iter_managed_non_markdown_files() -> list[Path]:
    files: list[Path] = []
    for root in MANAGED_NON_MARKDOWN_ROOTS:
        path = VAULT / root
        if not path.exists():
            continue
        for item in path.rglob("*"):
            if not item.is_file() or item.suffix == ".md":
                continue
            parts = set(item.relative_to(VAULT).parts)
            if parts & {".git", ".obsidian", "quartz", "_sources"}:
                continue
            files.append(item)
    return sorted(set(files))


def iter_operating_markdown() -> list[Path]:
    files: list[Path] = []
    for root in OPERATING_ROOTS:
        path = VAULT / root
        if path.is_file() and path.suffix == ".md":
            files.append(path)
        elif path.is_dir():
            for item in path.rglob("*.md"):
                parts = set(item.relative_to(VAULT).parts)
                if parts & OPERATING_SKIP_DIRS:
                    continue
                files.append(item)
    return sorted(set(files))


def iter_template_markdown() -> list[Path]:
    files: list[Path] = []
    for root in TEMPLATE_ROOTS:
        path = VAULT / root
        if path.is_file() and path.suffix == ".md":
            files.append(path)
        elif path.is_dir():
            for item in path.rglob("*.md"):
                parts = set(item.relative_to(VAULT).parts)
                if parts & TEMPLATE_SKIP_DIRS:
                    continue
                files.append(item)
    return sorted(set(files))


def is_transient_file(path: Path) -> bool:
    name = path.name
    return (
        name in TRANSIENT_FILE_NAMES
        or name.startswith(TRANSIENT_FILE_PREFIXES)
        or name.endswith(TRANSIENT_FILE_SUFFIXES)
    )


def is_allowed_non_markdown_file(path: Path) -> bool:
    r = rel(path)
    if path.name == ".gitkeep" and r.startswith(("wiki/", "brain/wiki/")):
        return True
    if path.suffix == ".base" and r.startswith(("inbox/", "wiki/private/_sources/knowledge/")):
        return True
    if r == CALENDAR_ICS_PROJECTION_PATH:
        return is_core_calendar_ics_projection(path)
    if path.suffix == ".canvas" and r.startswith("maps/"):
        return is_noncanonical_map_archive(path) or is_valid_markdown_canvas(path)
    if r == "catalog/source-audits/inflearn-java-course-materials.json":
        return True
    if (
        r.startswith("wiki/private/_sources/knowledge/private/career/applications/")
        and path.suffix.casefold() in {".json", ".pdf", ".yaml", ".yml"}
    ):
        return True
    if path.suffix != ".drawio":
        return False
    return r.startswith("wiki/ai/transformer/diagrams/")


def is_core_calendar_ics_projection(path: Path) -> bool:
    """Permit only the minimized, Core-owned legacy ICS compatibility feed."""

    if not path.is_file():
        return False
    try:
        content = path.read_bytes()
    except OSError:
        return False
    return (
        CALENDAR_ICS_PROJECTION_PRODID.encode("utf-8") in content
        and b"BEGIN:VCALENDAR\r\n" in content
        and path.stat().st_mode & 0o777 == CALENDAR_PROJECTION_FILE_MODE
    )


def is_calendar_projection_markdown(path: Path) -> bool:
    """Keep generated local calendar views out of knowledge-title quality checks."""

    relative = rel(path)
    return relative.startswith(f"{CALENDAR_PROJECTION_ROOT}/") or relative == (
        CALENDAR_DASHBOARD_PROJECTION_PATH
    )


def calendar_projection_issues(vault: Path) -> list[str]:
    """Validate Core-owned Markdown and ICS projections without editable calendar state."""

    directory = vault / CALENDAR_PROJECTION_ROOT
    ics_path = vault / CALENDAR_ICS_PROJECTION_PATH
    dashboard_path = vault / CALENDAR_DASHBOARD_PROJECTION_PATH
    directory_present = directory.exists() or directory.is_symlink()
    ics_present = ics_path.exists() or ics_path.is_symlink()
    dashboard_present = dashboard_path.exists() or dashboard_path.is_symlink()
    if not directory_present and not ics_present and not dashboard_present:
        return []

    issues: list[str] = []
    if directory_present:
        relative_directory = directory.relative_to(vault).as_posix()
        scan_directory = True
        if directory.is_symlink() or not _resolves_within(vault, directory):
            issues.append(f"{relative_directory}: Core projection directory must be Vault-local")
            scan_directory = False
        elif not directory.is_dir():
            issues.append(f"{relative_directory}: Core projection path must be a directory")
            scan_directory = False
        if scan_directory:
            if directory.stat().st_mode & 0o777 != CALENDAR_PROJECTION_DIRECTORY_MODE:
                issues.append(f"{relative_directory}: Core projection directory must be read-only")

            for path in sorted(directory.glob("*.md")):
                relative = path.relative_to(vault).as_posix()
                if path.is_symlink() or not path.is_file() or not _resolves_within(vault, path):
                    issues.append(f"{relative}: calendar projection must be a Vault-local file")
                    continue
                if path.name == ".prisma-virtual-events.md" or path.name == "Virtual Events.md":
                    issues.append(f"{relative}: retired Prisma support file must be removed")
                    continue
                metadata = parse_frontmatter(path.read_text(encoding="utf-8"))
                if path.name == CALENDAR_NOTION_DATABASE_FILENAME:
                    issues.append(
                        f"{relative}: retired Notion Bases calendar database must be removed"
                    )
                    continue
                if metadata.get("woon_projection") != "apple-calendar":
                    issues.append(f"{relative}: calendar projection marker is required")
                if metadata.get("source") != "apple-calendar-readonly":
                    issues.append(f"{relative}: calendar projection source must be read-only")
                if metadata.get("type") != "calendar-event":
                    issues.append(f"{relative}: calendar projection type must be calendar-event")
                if not isinstance(metadata.get("title"), str) or not str(metadata["title"]).strip():
                    issues.append(f"{relative}: calendar projection title is required")
                if (
                    not isinstance(metadata.get("calendar"), str)
                    or not str(metadata["calendar"]).strip()
                ):
                    issues.append(f"{relative}: calendar projection calendar is required")
                if not isinstance(metadata.get("Date"), str) or not str(metadata["Date"]).strip():
                    issues.append(f"{relative}: calendar projection requires Date")
                if (
                    not isinstance(metadata.get("Category"), str)
                    or not str(metadata["Category"]).strip()
                ):
                    issues.append(f"{relative}: calendar projection requires Category")
                all_day = metadata.get("All Day")
                if all_day is False:
                    for field in ("Start Date", "End Date"):
                        if (
                            not isinstance(metadata.get(field), str)
                            or not str(metadata[field]).strip()
                        ):
                            issues.append(f"{relative}: timed calendar projection requires {field}")
                elif all_day is not True:
                    issues.append(f"{relative}: calendar projection All Day must be boolean")
                if path.stat().st_mode & 0o777 != CALENDAR_PROJECTION_FILE_MODE:
                    issues.append(f"{relative}: calendar projection must be read-only")

    if ics_present and (
        ics_path.is_symlink()
        or not _resolves_within(vault, ics_path)
        or not is_core_calendar_ics_projection(ics_path)
    ):
        issues.append(
            f"{CALENDAR_ICS_PROJECTION_PATH}: Core ICS projection must be Vault-local and read-only"
        )
    if dashboard_present:
        if (
            dashboard_path.is_symlink()
            or not dashboard_path.is_file()
            or not _resolves_within(vault, dashboard_path)
        ):
            issues.append(
                f"{CALENDAR_DASHBOARD_PROJECTION_PATH}: dashboard must be a Vault-local file"
            )
        else:
            try:
                dashboard_content = dashboard_path.read_text(encoding="utf-8")
            except OSError:
                issues.append(f"{CALENDAR_DASHBOARD_PROJECTION_PATH}: dashboard must be readable")
                return issues
            dashboard = parse_frontmatter(dashboard_content)
            if dashboard.get("woon_projection") != "apple-calendar-dashboard":
                issues.append(
                    f"{CALENDAR_DASHBOARD_PROJECTION_PATH}: Core dashboard marker is required"
                )
            if dashboard.get("source") != "apple-calendar-readonly":
                issues.append(
                    f"{CALENDAR_DASHBOARD_PROJECTION_PATH}: dashboard source must be read-only"
                )
            if dashboard_path.stat().st_mode & 0o777 != CALENDAR_PROJECTION_FILE_MODE:
                issues.append(f"{CALENDAR_DASHBOARD_PROJECTION_PATH}: dashboard must be read-only")
            required_dashboard = (
                f"cssclasses: {LINK_CALENDAR_DASHBOARD_CSS_CLASS}\n"
                "---\n\n"
                f"```{LINK_CALENDAR_PLUGIN_ID}\n"
                f"profile: {LINK_CALENDAR_PROFILE_ID}\n"
                "```"
            )
            if required_dashboard not in dashboard_content:
                issues.append(
                    f"{CALENDAR_DASHBOARD_PROJECTION_PATH}: dashboard must embed Link Calendar"
                )
    elif directory_present or ics_present:
        issues.append(f"{CALENDAR_DASHBOARD_PROJECTION_PATH}: Core dashboard is required")
    return issues


def _resolves_within(vault: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(vault.resolve())
    except ValueError:
        return False
    return True


def is_valid_markdown_canvas(path: Path) -> bool:
    """Allow only a JSON Canvas whose cards are existing Markdown files.

    The detailed navigation contract is enforced by the knowledge-navigation
    skill. The Vault-wide audit keeps the filesystem boundary narrow so that a
    malformed canvas cannot silently become a general asset exception.
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return False
    vault = VAULT.resolve()
    node_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "file":
            return False
        node_id = node.get("id")
        target = node.get("file")
        if not isinstance(node_id, str) or not node_id or node_id in node_ids:
            return False
        if not isinstance(target, str) or Path(target).suffix not in {".md", ".base"}:
            return False
        candidate = (vault / target).resolve()
        try:
            candidate.relative_to(vault)
        except ValueError:
            return False
        if not candidate.is_file():
            return False
        node_ids.add(node_id)
    for edge in edges:
        if not isinstance(edge, dict) or "label" in edge:
            return False
        if edge.get("fromNode") not in node_ids or edge.get("toNode") not in node_ids:
            return False
    return True


def parse_frontmatter(text: str) -> dict[str, object]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    raw = text[4:end].splitlines()
    data: dict[str, object] = {}
    current_key: str | None = None
    for line in raw:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        list_item = re.match(r"^\s*-\s+(.*)$", line)
        if list_item and current_key:
            current_value = data.get(current_key)
            if isinstance(current_value, list):
                values = current_value
            else:
                values = []
                data[current_key] = values
            values.append(clean_value(list_item.group(1)))
            continue
        match = re.match(r"^([A-Za-z0-9가-힣 _-]+):\s*(.*)$", line)
        if match:
            current_key = match.group(1).strip()
            value = match.group(2).strip()
            if value == "":
                data[current_key] = ""
            elif value == "[]":
                data[current_key] = []
            else:
                data[current_key] = clean_value(value)
    return data


def clean_value(value: str) -> object:
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith(("[", "{")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        # YAML escapes a literal apostrophe in a single-quoted scalar by doubling it.
        return value[1:-1].replace("''", "'")
    return value


def retired_external_video_boundary_issues(vault: Path) -> list[str]:
    """Reject a retired video archive instead of silently preserving new captures."""

    if (vault / "wiki/private/_sources/knowledge/external-video").exists():
        return [
            "wiki/private/_sources/knowledge/external-video is retired; "
            "keep changing video links out of the canonical Vault"
        ]
    return []


def retired_ai_instruction_boundary_issues(vault: Path) -> list[str]:
    """Reject deleted AI instruction roots and references instead of hiding them."""

    issues: list[str] = []
    for root in RETIRED_WORKSPACE_ROOTS:
        if (vault / root).exists():
            issues.append(f"{root} is a retired workspace root and must be absent")
    for nested_git in vault.rglob(".git"):
        if nested_git.parent != vault:
            issues.append(
                f"{nested_git.relative_to(vault).as_posix()} is a nested Git repository "
                "and must be absent"
            )

    excluded_roots = {
        ".git",
        ".local",
        ".legacy-backup",
        ".obsidian",
        "_sources",
        "scripts",
        "quartz",
    }
    for path in sorted(vault.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(vault)
        if excluded_roots.intersection(relative.parts):
            continue
        if path.suffix not in {".md", ".yaml", ".yml", ".json", ".toml"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rendered_path = relative.as_posix()
        if path.suffix == ".md" and parse_frontmatter(text).get("type") == "AI Reference":
            issues.append(f"{rendered_path}: legacy AI Reference documents are not permitted")
        for locator in RETIRED_AI_INSTRUCTION_LOCATORS:
            if locator in text:
                issues.append(f"{rendered_path}: retired AI instruction locator {locator!r}")
    return issues


def retired_legacy_view_issues(vault: Path) -> list[str]:
    """Reject obsolete visualization copies and sample notes from the live Vault."""

    issues = [
        f"{root} is a retired visualization copy/sample root and must be absent"
        for root in RETIRED_LEGACY_VIEW_ROOTS
        if (vault / root).exists()
    ]
    issues.extend(
        f"{path} is a retired duplicate Base view and must be absent"
        for path in RETIRED_LEGACY_VIEW_PATHS
        if (vault / path).exists()
    )
    return issues


def brain_activity_log_issues(log: Path) -> list[str]:
    """Validate appended events while ignoring the documentation example fence."""

    if not log.is_file():
        return ["brain/log.md is required"]
    records: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for line in strip_fenced_blocks(log.read_text(encoding="utf-8")).splitlines():
        event_match = re.match(r"^- event_id:\s*(.*)$", line)
        if event_match:
            current = {"event_id": event_match.group(1).strip(), "external_id_keys": set()}
            records.append(current)
            continue
        if current is None:
            continue
        field_match = re.match(r"^  ([a-z_]+):\s*(.*)$", line)
        if field_match:
            current[field_match.group(1)] = field_match.group(2).strip()
            continue
        nested_match = re.match(r"^    (task|calendar):\s*(.*)$", line)
        if nested_match and current.get("external_ids") == "":
            keys = current["external_id_keys"]
            assert isinstance(keys, set)
            keys.add(nested_match.group(1))

    issues: list[str] = []
    seen_ids: set[str] = set()
    required = {
        "event_id",
        "occurred_at",
        "kind",
        "source_id",
        "facts",
        "external_ids",
        "privacy",
        "completion_source",
    }
    for record in records:
        event_id = str(record.get("event_id", "")).strip()
        label = event_id or "<missing-event-id>"
        for field in sorted(required):
            if field == "external_ids":
                if field not in record:
                    issues.append(f"brain/log.md {label}: missing {field}")
            elif not str(record.get(field, "")).strip():
                issues.append(f"brain/log.md {label}: missing {field}")
        if event_id:
            if event_id in seen_ids:
                issues.append(f"brain/log.md {label}: duplicate event_id")
            seen_ids.add(event_id)

        occurred_at = str(record.get("occurred_at", "")).strip()
        if occurred_at:
            try:
                parsed = datetime.fromisoformat(occurred_at)
            except ValueError:
                issues.append(f"brain/log.md {label}: invalid occurred_at")
            else:
                if parsed.tzinfo is None:
                    issues.append(f"brain/log.md {label}: occurred_at must be timezone-aware")
        if record.get("kind") not in BRAIN_ACTIVITY_KINDS:
            issues.append(f"brain/log.md {label}: unsupported kind")
        if record.get("privacy") != "local-only":
            issues.append(f"brain/log.md {label}: privacy must be local-only")
        if record.get("completion_source") not in BRAIN_COMPLETION_SOURCES:
            issues.append(f"brain/log.md {label}: unsupported completion_source")
        external_keys = record.get("external_id_keys")
        if isinstance(external_keys, set) and external_keys != {"task", "calendar"}:
            issues.append(f"brain/log.md {label}: external_ids requires task and calendar keys")
    return issues


def person_schema_issues(vault: Path) -> list[str]:
    """Keep person cards discoverable without leaking Novel identities into the general map."""

    issues: list[str] = []
    schema_path = vault / PERSON_SCHEMA_PATH
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"{PERSON_SCHEMA_PATH}: cannot load schema: {error}"]
    if not isinstance(schema, dict) or schema.get("version") != 1:
        return [f"{PERSON_SCHEMA_PATH}: version must be 1"]
    owner = schema.get("default_record_owner")
    if not isinstance(owner, dict) or owner != {
        "person_id": "choi-woonyoung",
        "representation": "person-id",
        "mode": "implicit-if-omitted",
    }:
        issues.append(f"{PERSON_SCHEMA_PATH}: default_record_owner must identify 최우녕")
    privacy = schema.get("privacy")
    if not isinstance(privacy, dict) or privacy.get("automation_identity_inference") != "forbidden":
        issues.append(f"{PERSON_SCHEMA_PATH}: automation identity inference must be forbidden")

    templates_root = vault / "templates"
    for template in sorted(templates_root.rglob("*.md")) if templates_root.exists() else []:
        metadata = parse_frontmatter(template.read_text(encoding="utf-8"))
        relative = template.relative_to(vault).as_posix()
        if metadata.get("record_owner") != DEFAULT_OWNER_ID:
            issues.append(f"{relative}: record_owner must default to 최우녕")
        for field in ("people", "person_roles", "attributions"):
            if metadata.get(field) != []:
                issues.append(f"{relative}: {field} must start as an empty list")

    registered_cards: list[str] = []
    person_paths = tuple(sorted((vault / "wiki/personal").glob("*.md"))) + tuple(
        sorted((vault / "wiki/private").glob("*.md"))
    )
    for card_path in person_paths:
        metadata = parse_frontmatter(card_path.read_text(encoding="utf-8"))
        if metadata.get("entity_type") != "person":
            continue
        relative = card_path.relative_to(vault).as_posix()
        registered_cards.append(relative)
        for field in ("person_id", "person_kind", "person_scope", "relationship_to_owner"):
            if not isinstance(metadata.get(field), str) or not str(metadata[field]).strip():
                issues.append(f"{relative}: person card must define {field}")
        scope = metadata.get("person_scope")
        kind = metadata.get("person_kind")
        if scope not in {"general", "novel-local-only"}:
            issues.append(f"{relative}: unsupported person_scope")
        if kind not in GENERAL_PERSON_KINDS:
            issues.append(f"{relative}: unsupported person_kind")
        parent = metadata.get("parent")
        if scope == "general" and not (
            isinstance(parent, str) and parent.startswith("[[wiki/people/README")
        ):
            issues.append(f"{relative}: general person card must be a child of wiki/people/README")
        if (
            scope == "novel-local-only"
            and isinstance(parent, str)
            and parent.startswith("[[wiki/people/README")
        ):
            issues.append(
                f"{relative}: novel-local-only card must not enter the general people tree"
            )

    people_hub = vault / "wiki/people/README.md"
    if not people_hub.is_file():
        issues.append("wiki/people/README.md is required")
    elif "[[wiki/personal/최우녕|최우녕]]" not in people_hub.read_text(encoding="utf-8"):
        issues.append("wiki/people/README.md: people tree must include the Vault owner")
    return issues


def h1(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def quartz_root(path: Path) -> str:
    r = rel(path)
    if r in {"README.md", "index.md"}:
        return r
    return r.split("/", 1)[0]


def strip_fenced_blocks(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def strip_inline_code(text: str) -> str:
    return re.sub(r"`[^`]*`", "", text)


def as_list(value: object) -> list[object]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    return [value]


def is_external_ref(value: str) -> bool:
    return "://" in value or value.startswith(("mailto:", "#")) or value.startswith("data:")


def local_ref_exists(owner: Path, value: str) -> bool:
    ref = value.split("#", 1)[0].split("?", 1)[0].strip()
    if not ref or is_external_ref(ref):
        return True
    candidate_paths: list[Path] = []
    ref_path = Path(ref)
    if ref_path.is_absolute():
        candidate_paths.append(ref_path)
    else:
        candidate_paths.append((owner.parent / ref_path).resolve())
        candidate_paths.append((VAULT / ref_path).resolve())
    return any(path.exists() for path in candidate_paths)


def target_index(files: list[Path]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in files:
        r = rel(path)
        no_ext = r[:-3]
        keys = {
            no_ext,
            path.stem,
            r,
        }
        if path.name in {"README.md", "index.md"}:
            if path.parent == VAULT:
                keys.update({"README", "index"})
            else:
                keys.add(no_ext.removesuffix("/README").removesuffix("/index"))
                keys.add(path.parent.name)
        for key in keys:
            index.setdefault(key, []).append(path)
    return index


def target_index_any(files: list[Path]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in files:
        r = rel(path)
        no_ext = str(Path(r).with_suffix(""))
        keys = {
            no_ext,
            path.stem,
            r,
        }
        if path.name in {"README.md", "index.md"}:
            if path.parent == VAULT:
                keys.update({"README", "index"})
            else:
                keys.add(no_ext.removesuffix("/README").removesuffix("/index"))
                keys.add(path.parent.name)
        for key in keys:
            bucket = index.setdefault(key, [])
            if path not in bucket:
                bucket.append(path)
    return index


def resolve_link(target: str, index: dict[str, list[Path]]) -> list[Path]:
    target = target.strip()
    if not target or "://" in target:
        return []
    target = target.removesuffix(".md")
    return index.get(target, [])


def global_graph_root_issues(
    files: list[Path],
    texts: dict[Path, str],
    metadata: dict[Path, dict[str, object]],
    index: dict[str, list[Path]],
) -> list[str]:
    """Require every visible Wiki or map node to reach the single Wiki root."""

    def is_visible(path: Path) -> bool:
        relative = rel(path)
        return relative.startswith(("wiki/", "maps/")) and not relative.startswith(
            ("wiki/private/", *NONCANONICAL_MAP_ROOTS)
        )

    by_relative = {rel(path): path for path in files}
    issues: list[str] = []
    anchor = by_relative.get(GLOBAL_GRAPH_ROOT)
    if anchor is None:
        return [f"missing Wiki graph root: {GLOBAL_GRAPH_ROOT}"]

    visible = {path for path in files if is_visible(path)}
    traversable = visible | {anchor}
    graph: dict[Path, set[Path]] = {path: set() for path in traversable}
    resolved_paths = {path.resolve(): path for path in traversable}

    def add_graph_edge(source: Path, target: Path) -> None:
        target = resolved_paths.get(target.resolve(), target)
        if target in traversable:
            graph[source].add(target)
            # Obsidian's global graph is directional in storage but visualizes
            # an undirected relationship.  Keep that visual connectivity
            # invariant explicit here as well.
            graph[target].add(source)

    for path in traversable:
        link_text = strip_inline_code(strip_fenced_blocks(texts[path])).replace("\\|", "|")
        for target in WIKILINK_RE.findall(link_text):
            matches = set(resolve_link(target, index))
            relative_target = target.strip().removesuffix(".md")
            if relative_target and "://" not in relative_target:
                candidate = (path.parent / relative_target).with_suffix(".md").resolve()
                try:
                    candidate.relative_to(VAULT)
                except ValueError:
                    pass
                else:
                    if candidate in traversable:
                        matches.add(candidate)
            for match in matches:
                add_graph_edge(path, match)

        # Obsidian also renders ordinary relative Markdown links in the graph.
        # Accept only local paths and resolve a directory target to its README.
        for href in MARKDOWN_LINK_RE.findall(link_text):
            target = href.strip("<>").split("#", 1)[0].split("?", 1)[0]
            if not target or is_external_ref(target):
                continue
            candidate = (path.parent / target).resolve()
            try:
                candidate.relative_to(VAULT.resolve())
            except ValueError:
                continue
            candidates = [candidate]
            if candidate.is_dir():
                candidates.append(candidate / "README.md")
            elif candidate.suffix == "":
                candidates.append(candidate.with_suffix(".md"))
            for match in candidates:
                add_graph_edge(path, match)

    def connected_from(start: Path) -> set[Path]:
        reachable: set[Path] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            stack.extend(sorted(graph[current] - reachable))
        return reachable

    reachable = connected_from(anchor)
    for path in sorted(visible):
        if path not in reachable:
            issues.append(f"Wiki root cannot reach {rel(path)}")
    return issues


def mermaid_quality_issues(relative: str, text: str) -> tuple[list[str], list[str]]:
    """Report converter debris and duplicate stand-in nodes inside Mermaid blocks."""

    shape_issues: list[str] = []
    placeholder_issues: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if not lines[index].strip().startswith("```mermaid"):
            index += 1
            continue
        block_start = index + 1
        index += 1
        block: list[tuple[int, str]] = []
        while index < len(lines) and not lines[index].strip().startswith("```"):
            block.append((index + 1, lines[index]))
            index += 1

        block_text = "\n".join(line for _, line in block)
        for no, line in block:
            unsafe_edge_label = any(
                not match.group("label").lstrip().startswith('"')
                and re.search(r"[()\[\]]", match.group("label"))
                for match in re.finditer(r"(?:-->|<-->|-.->|==>)\|(?P<label>[^|\n]+)\|", line)
            )
            unsafe_node = bool(re.search(r"\b(?:call|end)\[", line))
            if (
                re.search(r'\["[^"\]]*<br/>\([^"\)]*"\]', line)
                or any(marker in line for marker in MERMAID_LEGACY_MARKERS)
                or unsafe_edge_label
                or unsafe_node
            ):
                shape_issues.append(f"{relative}: line {no}")
            for label in re.findall(r'\["([^"\]]+)"\]', line):
                if label.strip() in MERMAID_PLACEHOLDER_LABELS:
                    placeholder_issues.append(f"{relative}: line {no}: {label.strip()}")

            duplicate = re.match(
                r'^\s*([A-Za-z][A-Za-z0-9_-]*)_2\["([A-Za-z][A-Za-z0-9_-]*)"\]\s*$',
                line,
            )
            if duplicate is None or duplicate.group(1) != duplicate.group(2):
                continue
            base = duplicate.group(1)
            if re.search(rf"^\s*{re.escape(base)}(?:\[|\(|\{{)", block_text, re.MULTILINE):
                placeholder_issues.append(f"{relative}: line {no}: {base}_2")

        if index >= len(lines):
            shape_issues.append(f"{relative}: line {block_start}: unclosed Mermaid block")
        index += 1
    return shape_issues, placeholder_issues


def mermaid_color_issues(relative: str, text: str) -> list[str]:
    """Report Mermaid-local colors that break the shared light/dark visual contract."""

    issues: list[str] = []
    in_mermaid = False
    for no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not in_mermaid:
            in_mermaid = stripped.startswith("```mermaid")
            continue
        if stripped.startswith("```"):
            in_mermaid = False
            continue
        if MERMAID_COLOR_DIRECTIVE_RE.search(line) or MERMAID_THEME_COLOR_RE.search(line):
            issues.append(f"{relative}: line {no}")
    return issues


def wiki_base_contract_issues(vault: Path) -> list[str]:
    """Validate the native Base semantically instead of matching YAML formatting."""

    wiki_base = vault / "inbox/wiki/wiki.base"
    if not wiki_base.is_file():
        return ["inbox/wiki/wiki.base is missing"]
    try:
        base_data = yaml.safe_load(wiki_base.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        return [f"inbox/wiki/wiki.base is invalid YAML: {error}"]
    if not isinstance(base_data, dict):
        return ["inbox/wiki/wiki.base must contain a YAML object"]

    issues: list[str] = []
    formulas = base_data.get("formulas")
    title_formula = formulas.get("title_link") if isinstance(formulas, dict) else None
    if title_formula != "file.asLink(title)":
        issues.append("inbox/wiki/wiki.base must use file.asLink(title) for the human title")
    views = base_data.get("views")
    view_names = (
        {
            view.get("name")
            for view in views
            if isinstance(view, dict) and isinstance(view.get("name"), str)
        }
        if isinstance(views, list)
        else set()
    )
    for required_name in (
        "전체",
        "프로젝트",
        "책",
        "리소스",
        "개념",
        "학습",
        "커리어",
        "생활",
        "인물",
    ):
        if required_name not in view_names:
            issues.append(f"inbox/wiki/wiki.base is missing view {required_name!r}")
    return issues


def main() -> int:
    files = [path for path in iter_markdown() if not is_calendar_projection_markdown(path)]
    index = target_index(files)
    issues: dict[str, list[str]] = {
        "missing_frontmatter": [],
        "missing_required_metadata": [],
        "title_h1_mismatch": [],
        "published_outside_quartz_scope": [],
        "local_operational_published": [],
        "published_links_to_unpublished": [],
        "published_links_to_local_only": [],
        "broken_wikilinks": [],
        "ambiguous_wikilinks": [],
        "duplicate_titles": [],
        "duplicate_content": [],
        "mermaid_placeholder_nodes": [],
        "operational_link_violations": [],
        "unreachable_published_from_home": [],
        "zero_incoming_published": [],
        "published_wiki_without_map_link": [],
        "thin_published_wiki_docs": [],
        "manual_toc_duplicates_headings": [],
        "missing_asset_refs": [],
        "table_in_map_docs": [],
        "link_list_slug_exposure": [],
        "absolute_local_paths": [],
        "transient_files": [],
        "unexpected_non_markdown_files": [],
        "unexpected_root_directories": [],
        "operational_metadata_violations": [],
        "template_policy_violations": [],
        "inbox_policy_violations": [],
        "source_policy_violations": [],
        "source_boundary_violations": [],
        "book_source_link_violations": [],
        "obsidian_graph_policy_violations": [],
        "source_asset_policy_violations": [],
        "private_novel_boundary_violations": [],
        "retired_external_video_boundary_violations": [],
        "retired_ai_instruction_boundary_violations": [],
        "retired_legacy_view_violations": [],
        "brain_activity_log_violations": [],
        "wiki_pipeline_policy_violations": [],
        "entity_policy_violations": [],
        "person_schema_violations": [],
        "user_visible_review_violations": [],
        "vault_execution_ownership_violations": [],
        "calendar_projection_violations": [],
        "daily_digest_projection_violations": [],
        "runtime_permission_violations": [],
        "obsidian_workspace_violations": [],
        "global_graph_root_violations": [],
        "wiki_display_contract_violations": [],
        "strict_relative_link_violations": [],
        "mermaid_shape_violations": [],
        "mermaid_color_violations": [],
    }

    if RETIRED_PRIVATE_NOVEL_ROOT.exists():
        issues["private_novel_boundary_violations"].append(
            "projects/writing is retired; Novel sources belong in "
            "wiki/private/_sources/novel and navigation belongs in wiki/private/novel"
        )
    issues["source_boundary_violations"].extend(audit_source_boundary(VAULT))

    issues["retired_external_video_boundary_violations"].extend(
        retired_external_video_boundary_issues(VAULT)
    )
    issues["retired_ai_instruction_boundary_violations"].extend(
        retired_ai_instruction_boundary_issues(VAULT)
    )
    issues["retired_legacy_view_violations"].extend(retired_legacy_view_issues(VAULT))
    issues["unexpected_root_directories"].extend(unexpected_root_directory_issues(VAULT))
    issues["brain_activity_log_violations"].extend(
        brain_activity_log_issues(VAULT / "brain/log.md")
    )
    issues["person_schema_violations"].extend(person_schema_issues(VAULT))
    issues["vault_execution_ownership_violations"].extend(vault_execution_ownership_issues(VAULT))
    issues["calendar_projection_violations"].extend(calendar_projection_issues(VAULT))
    issues["daily_digest_projection_violations"].extend(daily_digest_embed_issues(VAULT))
    issues["runtime_permission_violations"].extend(runtime_permission_issues(VAULT))
    issues["obsidian_workspace_violations"].extend(obsidian_workspace_issues(VAULT))
    try:
        tree_nodes, tree_texts, tree_issues = load_wiki_tree(VAULT)
    except WoonError as error:
        issues["wiki_display_contract_violations"].append(str(error))
    else:
        issues["wiki_display_contract_violations"].extend(tree_issues)
        for node in tree_nodes:
            text = tree_texts[node.relative_path]
            if (
                "<!-- woon-wiki-overview:start -->" in text
                or "<!-- woon-wiki-overview:end -->" in text
            ):
                issues["wiki_display_contract_violations"].append(
                    f"{node.relative_path}: generated metadata overview is retired"
                )
            expected_mode = {
                "book": "linear",
                "project": "project",
                "person": "topic-timeline",
            }.get(str(parse_frontmatter(text).get("entity_kind", "")))
            if expected_mode is not None and node.view_mode != expected_mode:
                issues["entity_policy_violations"].append(
                    f"{node.relative_path}: entity view_mode must be {expected_mode}"
                )
    for map_markdown in sorted((VAULT / "maps").rglob("*.md")):
        issues["wiki_display_contract_violations"].append(
            f"parallel Markdown knowledge map must be removed: {rel(map_markdown)}"
        )

    wiki_view_root = VAULT / "inbox/wiki"
    retired_facet_pages = sorted(wiki_view_root.glob("*.md")) if wiki_view_root.is_dir() else []
    if retired_facet_pages:
        issues["wiki_display_contract_violations"].extend(
            f"duplicate Wiki dashboard must be removed: {rel(path)}" for path in retired_facet_pages
        )
    issues["wiki_display_contract_violations"].extend(wiki_base_contract_issues(VAULT))

    for retired_map in ("maps/README.md", "maps/developer-wiki-map.md", "maps/vault-moc.md"):
        if (VAULT / retired_map).exists():
            issues["wiki_display_contract_violations"].append(
                f"retired parallel navigation must be removed: {retired_map}"
            )

    home_path = VAULT / "brain/home.md"
    if home_path.is_file():
        home_text = home_path.read_text(encoding="utf-8")
        for required_embed in (
            "![[../inbox/inbox-review.base#검토 대기]]",
            "![[../inbox/daily/daily.base#일일 이력]]",
        ):
            if required_embed not in home_text:
                issues["wiki_display_contract_violations"].append(
                    f"brain/home.md must reuse {required_embed} instead of a duplicate table"
                )
        for required_link in (
            "[[../wiki/README|Wiki]]",
            "[[../wiki/personal/projects/README|프로젝트]]",
            "[[../wiki/people/README|인물·관계]]",
        ):
            if required_link not in home_text:
                issues["wiki_display_contract_violations"].append(
                    f"brain/home.md must expose the human navigation link {required_link}"
                )

    if not (VAULT / "wiki/README.md").is_file():
        issues["wiki_display_contract_violations"].append(
            "wiki/README.md is required as the sole human Wiki entry"
        )

    source_asset_audit = Path(__file__).with_name("audit-source-assets.py")
    source_asset_result = subprocess.run(
        [sys.executable, str(source_asset_audit)],
        cwd=VAULT,
        capture_output=True,
        text=True,
        check=False,
    )
    if source_asset_result.returncode != 0:
        try:
            asset_report = json.loads(source_asset_result.stdout)
        except json.JSONDecodeError:
            issues["source_asset_policy_violations"].append(
                source_asset_result.stderr.strip() or "source asset audit failed"
            )
        else:
            issues["source_asset_policy_violations"].extend(asset_report["issues"])

    graph_config_path = VAULT / ".obsidian" / "graph.json"
    try:
        graph_config = json.loads(graph_config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        issues["obsidian_graph_policy_violations"].append(str(exc))
    else:
        if graph_config.get("search") != OBSIDIAN_GRAPH_FILTER:
            issues["obsidian_graph_policy_violations"].append(
                f"search must be {OBSIDIAN_GRAPH_FILTER!r}"
            )
        if graph_config.get("showAttachments") is not False:
            issues["obsidian_graph_policy_violations"].append("attachments must be hidden")
        if graph_config.get("hideUnresolved") is not True:
            issues["obsidian_graph_policy_violations"].append("unresolved nodes must be hidden")
        if graph_config.get("showTags") is not False:
            issues["obsidian_graph_policy_violations"].append("tags must be hidden")
        if graph_config.get("showOrphans") is not False:
            issues["obsidian_graph_policy_violations"].append("orphan nodes must be hidden")

        excluded_group_roots = (
            "path:inbox",
            "path:sources",
            "path:projects/writing",
            "path:brain/wiki",
            "path:projects",
            "path:content",
            "path:users",
            "path:assets",
        )
        allowed_group_roots = ("path:wiki",)
        colors: dict[int, str] = {}
        for group in graph_config.get("colorGroups", []):
            query = group.get("query", "") if isinstance(group, dict) else ""
            if any(root in query for root in excluded_group_roots):
                issues["obsidian_graph_policy_violations"].append(
                    f"excluded color group remains: {query}"
                )
            if not any(root in query for root in allowed_group_roots):
                issues["obsidian_graph_policy_violations"].append(
                    f"color group is outside graph scope: {query}"
                )
            color = group.get("color") if isinstance(group, dict) else None
            rgb = color.get("rgb") if isinstance(color, dict) else None
            if not isinstance(rgb, int):
                issues["obsidian_graph_policy_violations"].append(
                    f"color group has no RGB color: {query}"
                )
            elif rgb in colors:
                issues["obsidian_graph_policy_violations"].append(
                    f"color is reused by {colors[rgb]!r} and {query!r}"
                )
            else:
                colors[rgb] = query

    explorer_css_path = VAULT / ".obsidian" / "snippets" / f"{OBSIDIAN_EXPLORER_SNIPPET}.css"
    try:
        explorer_css = explorer_css_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        issues["obsidian_graph_policy_violations"].append(str(exc))
    else:
        if MERMAID_NEUTRAL_STYLE_MARKER not in explorer_css:
            issues["obsidian_graph_policy_violations"].append(
                "enabled CSS snippet must apply the neutral Mermaid display contract"
            )
        for hidden_path in OBSIDIAN_HIDDEN_EXPLORER_PATHS:
            selector = f'data-path="{hidden_path}"'
            if selector not in explorer_css:
                issues["obsidian_graph_policy_violations"].append(
                    f"file explorer must hide {hidden_path!r}"
                )

    app_config_path = VAULT / ".obsidian" / "app.json"
    try:
        app_config = json.loads(app_config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        issues["obsidian_graph_policy_violations"].append(str(exc))
    else:
        if app_config.get("showUnsupportedFiles") is not False:
            issues["obsidian_graph_policy_violations"].append(
                "unsupported files must be hidden from the file explorer"
            )
        ignore_filters = app_config.get("userIgnoreFilters", [])
        if not isinstance(ignore_filters, list):
            issues["obsidian_graph_policy_violations"].append("userIgnoreFilters must be a list")
        else:
            for ignored_path in OBSIDIAN_IGNORED_PATHS:
                if ignored_path not in ignore_filters:
                    issues["obsidian_graph_policy_violations"].append(
                        f"Obsidian must ignore {ignored_path!r}"
                    )

    appearance_config_path = VAULT / ".obsidian" / "appearance.json"
    try:
        appearance_config = json.loads(appearance_config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        issues["obsidian_graph_policy_violations"].append(str(exc))
    else:
        enabled_snippets = appearance_config.get("enabledCssSnippets", [])
        if (
            not isinstance(enabled_snippets, list)
            or OBSIDIAN_EXPLORER_SNIPPET not in enabled_snippets
        ):
            issues["obsidian_graph_policy_violations"].append(
                f"CSS snippet {OBSIDIAN_EXPLORER_SNIPPET!r} must be enabled"
            )

    community_plugins_path = VAULT / ".obsidian" / "community-plugins.json"
    try:
        community_plugins = json.loads(community_plugins_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        issues["obsidian_graph_policy_violations"].append(str(exc))
    else:
        if (
            not isinstance(community_plugins, list)
            or OBSIDIAN_FRONT_MATTER_TITLE_PLUGIN not in community_plugins
        ):
            issues["obsidian_graph_policy_violations"].append(
                f"community plugin {OBSIDIAN_FRONT_MATTER_TITLE_PLUGIN!r} must be enabled"
            )

    title_plugin_config_path = (
        VAULT / ".obsidian" / "plugins" / OBSIDIAN_FRONT_MATTER_TITLE_PLUGIN / "data.json"
    )
    try:
        title_plugin_config = json.loads(title_plugin_config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        issues["obsidian_graph_policy_violations"].append(str(exc))
    else:
        if not isinstance(title_plugin_config, dict):
            issues["obsidian_graph_policy_violations"].append(
                "Front Matter Title config must be an object"
            )
        else:
            templates = title_plugin_config.get("templates", {})
            common_templates = templates.get("common", {}) if isinstance(templates, dict) else {}
            if not isinstance(common_templates, dict) or common_templates.get("main") != "title":
                issues["obsidian_graph_policy_violations"].append(
                    "Front Matter Title common template must use 'title'"
                )

            features = title_plugin_config.get("features", {})
            graph_feature = features.get("graph", {}) if isinstance(features, dict) else {}
            if not isinstance(graph_feature, dict) or graph_feature.get("enabled") is not True:
                issues["obsidian_graph_policy_violations"].append(
                    "Front Matter Title graph feature must be enabled"
                )
            else:
                graph_templates = graph_feature.get("templates", {})
                if not isinstance(graph_templates, dict) or graph_templates.get("main") not in {
                    None,
                    "",
                    "title",
                }:
                    issues["obsidian_graph_policy_violations"].append(
                        "Front Matter Title graph template must inherit or use 'title'"
                    )

    for path in iter_content_files():
        if is_transient_file(path):
            issues["transient_files"].append(rel(path))

    for path in iter_managed_non_markdown_files():
        if not is_allowed_non_markdown_file(path):
            issues["unexpected_non_markdown_files"].append(rel(path))

    operating_files = iter_operating_markdown()
    required = ["type", "title", "publish", "access", "status"]
    for path in operating_files:
        text = path.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        r = rel(path)
        if r == "AGENTS.md" and text.startswith("<!-- Code generated by woon. DO NOT EDIT. -->"):
            continue
        for no, line in enumerate(text.splitlines(), start=1):
            if ABSOLUTE_LOCAL_RE.search(line):
                issues["absolute_local_paths"].append(f"{r}: line {no}")
        if not fm:
            issues["operational_metadata_violations"].append(f"{r}: missing frontmatter")
            continue

        missing = [key for key in required if key not in fm]
        if missing:
            issues["operational_metadata_violations"].append(f"{r}: missing {', '.join(missing)}")

        title = fm.get("title")
        first_h1 = h1(text)
        if isinstance(title, str) and title and first_h1 and title != first_h1:
            issues["operational_metadata_violations"].append(
                f"{r}: title={title!r}, h1={first_h1!r}"
            )

        if fm.get("publish") is not False or fm.get("access") != "local-only":
            issues["operational_metadata_violations"].append(
                f"{r}: operational docs must be publish:false and access:local-only"
            )

        doc_type = fm.get("type")
        if doc_type == "AI Reference":
            issues["operational_metadata_violations"].append(
                f"{r}: legacy AI Reference documents are not permitted"
            )
        elif doc_type == "Type" and not r.startswith("types/"):
            issues["operational_metadata_violations"].append(f"{r}: Type must live in types/")

    template_files = iter_template_markdown()
    template_required = ["type", "title", "publish", "access", "status"]
    for path in template_files:
        text = path.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        r = rel(path)
        if not fm:
            issues["template_policy_violations"].append(f"{r}: missing frontmatter")
            continue

        missing = [key for key in template_required if key not in fm]
        if missing:
            issues["template_policy_violations"].append(f"{r}: missing {', '.join(missing)}")

        doc_type = fm.get("type")
        if doc_type not in TEMPLATE_TYPES:
            issues["template_policy_violations"].append(
                f"{r}: template target type must be one of {sorted(TEMPLATE_TYPES)}"
            )

        if doc_type == "Source":
            if fm.get("publish") is not False or fm.get("access") != "local-only":
                issues["template_policy_violations"].append(
                    f"{r}: Source templates must be publish:false and access:local-only"
                )
            if fm.get("source_kind") not in SOURCE_KINDS:
                issues["template_policy_violations"].append(
                    f"{r}: source_kind must be one of {sorted(SOURCE_KINDS)}"
                )
            if fm.get("lifecycle") not in SOURCE_LIFECYCLES:
                issues["template_policy_violations"].append(
                    f"{r}: lifecycle must be one of {sorted(SOURCE_LIFECYCLES)}"
                )
        elif doc_type == "Daily":
            if fm.get("publish") is not False or fm.get("access") != "local-only":
                issues["template_policy_violations"].append(
                    f"{r}: Daily templates must be publish:false and access:local-only"
                )

    operational_target_files = sorted(
        set(files) | set(iter_content_files()) | set(operating_files) | set(template_files)
    )
    operational_index = target_index_any(operational_target_files)
    for path in operating_files:
        r = rel(path)
        link_text = strip_inline_code(strip_fenced_blocks(path.read_text(encoding="utf-8")))
        for target in WIKILINK_RE.findall(link_text):
            matches = resolve_link(target, operational_index)
            if not matches:
                issues["operational_link_violations"].append(f"{r} -> {target}: missing")
                continue
            unique_matches = sorted({rel(match) for match in matches})
            if len(unique_matches) > 1:
                issues["operational_link_violations"].append(
                    f"{r} -> {target}: ambiguous {unique_matches}"
                )

    metadata: dict[Path, dict[str, object]] = {}
    texts: dict[Path, str] = {}
    book_sources: list[tuple[Path, list[str]]] = []
    for book_map in sorted((VAULT / "maps").glob("book-*-map.md")):
        book_metadata = parse_frontmatter(book_map.read_text(encoding="utf-8"))
        book_labels = [book_metadata.get("title"), *as_list(book_metadata.get("aliases"))]
        book_sources.append(
            (book_map, [label for label in book_labels if isinstance(label, str) and label])
        )

    for path in files:
        text = path.read_text(encoding="utf-8")
        texts[path] = text
        fm = parse_frontmatter(text)
        metadata[path] = fm
        r = rel(path)
        if "Woon Wiki" in text:
            issues["wiki_display_contract_violations"].append(
                f"{r}: use Wiki as the only human-facing name"
            )
        if "<!-- recent-docs:start -->" in text:
            issues["wiki_display_contract_violations"].append(
                f"{r}: generated recent-docs lists duplicate Base views"
            )
        if r.startswith("wiki/") and r != "wiki/README.md":
            for target in WIKILINK_RE.findall(text):
                raw_target = target.strip().split("#", 1)[0]
                if not raw_target.startswith("."):
                    continue
                candidate = (path.parent / raw_target).resolve()
                candidates = [candidate]
                if candidate.suffix == "":
                    candidates.extend((candidate.with_suffix(".md"), candidate / "README.md"))
                if not any(item.is_file() for item in candidates):
                    issues["strict_relative_link_violations"].append(f"{r} -> {target}")
        mermaid_shapes, mermaid_placeholders = mermaid_quality_issues(r, text)
        issues["mermaid_shape_violations"].extend(mermaid_shapes)
        issues["mermaid_placeholder_nodes"].extend(mermaid_placeholders)
        issues["mermaid_color_violations"].extend(mermaid_color_issues(r, text))
        for no, line in enumerate(text.splitlines(), start=1):
            if ABSOLUTE_LOCAL_RE.search(line):
                issues["absolute_local_paths"].append(f"{r}: line {no}")
        if is_noncanonical_map_archive(path):
            # Preserved exports remain resolvable targets but are deliberately
            # outside the canonical metadata/title corpus.
            continue

        if not fm:
            issues["missing_frontmatter"].append(r)
            continue

        issues["wiki_display_contract_violations"].extend(nonwiki_keyword_policy_issues(r, fm))

        if r.startswith("wiki/") and fm.get("type") == "Wiki":
            summary = fm.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                issues["wiki_display_contract_violations"].append(
                    f"{r}: summary is required for the shared Wiki Base"
                )
            if "기존 문서를 단일 Wiki 정본 계약으로 전환" in text:
                issues["wiki_display_contract_violations"].append(
                    f"{r}: migration bookkeeping must not appear as growth history"
                )

        if r.startswith("maps/") and fm.get("type") == "키워드" and fm.get("status") != "Archived":
            summary = fm.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                issues["wiki_display_contract_violations"].append(
                    f"{r}: summary is required for the map Base"
                )

        required = ["type", "title", "publish", "access", "status"]
        missing = [key for key in required if key not in fm]
        if missing:
            issues["missing_required_metadata"].append(f"{r}: {', '.join(missing)}")

        title = fm.get("title")
        first_h1 = h1(text)
        if isinstance(title, str) and title and first_h1 and title != first_h1:
            issues["title_h1_mismatch"].append(f"{r}: title={title!r}, h1={first_h1!r}")

        if r.startswith("brain/review/") and r != "brain/review/README.md":
            if not isinstance(title, str) or not re.search(r"[가-힣]", title):
                issues["user_visible_review_violations"].append(
                    f"{r}: review title must be Korean and human-readable"
                )
            elif re.search(r"(?:[a-f0-9]{6,}|candidate|preflight)", title, flags=re.IGNORECASE):
                issues["user_visible_review_violations"].append(
                    f"{r}: review title must not expose an internal identifier"
                )

        wiki_issues, entity_issues = wiki_and_entity_policy_issues(r, text, fm)
        issues["wiki_pipeline_policy_violations"].extend(wiki_issues)
        issues["entity_policy_violations"].extend(entity_issues)
        if r.startswith("wiki/") and fm.get("type") == "Wiki" and (
            "<!-- woon-wiki-overview:start -->" in text
            or "<!-- woon-wiki-overview:end -->" in text
        ):
            issues["wiki_display_contract_violations"].append(
                f"{r}: generated metadata overview is retired"
            )

        if r.startswith("maps/"):
            table_lines = [
                str(no)
                for no, line in enumerate(text.splitlines(), start=1)
                if line.startswith("| ")
            ]
            if table_lines:
                issues["table_in_map_docs"].append(f"{r}: lines={', '.join(table_lines[:10])}")

        if r.startswith("wiki/") and fm.get("type") == "Wiki" and fm.get("content_kind") == "book":
            wikilink_targets = set(WIKILINK_RE.findall(text))
            for book_map, labels in book_sources:
                if not any(label in text for label in labels):
                    continue
                book_targets = {
                    book_map.stem,
                    book_map.relative_to(VAULT).with_suffix("").as_posix(),
                }
                if wikilink_targets.isdisjoint(book_targets):
                    message = f"{r}: missing [[{book_map.stem}]]"
                    if message not in issues["book_source_link_violations"]:
                        issues["book_source_link_violations"].append(message)

        visible_text = strip_fenced_blocks(text)
        for no, line in enumerate(visible_text.splitlines(), start=1):
            if not line.lstrip().startswith(">") and LINK_LIST_SLUG_RE.search(line):
                issues["link_list_slug_exposure"].append(f"{r}: line {no}: {line.strip()}")

        for field in LOCAL_ASSET_FIELDS:
            for value in as_list(fm.get(field)):
                if not isinstance(value, str):
                    issues["missing_asset_refs"].append(
                        f"{r}: {field} contains non-string value {value!r}"
                    )
                    continue
                if not local_ref_exists(path, value):
                    issues["missing_asset_refs"].append(f"{r}: {field} -> {value}")

        published = fm.get("publish") is True
        root = quartz_root(path)
        if published and root not in QUARTZ_SYNC_ROOTS:
            issues["published_outside_quartz_scope"].append(r)

        if (
            r.startswith(("inbox/", "wiki/private/_sources/knowledge/"))
            or r == "head-quarter.md"
        ) and (published or fm.get("access") != "local-only"):
            issues["local_operational_published"].append(r)

        if r.startswith("inbox/"):
            if fm.get("publish") is not False or fm.get("access") != "local-only":
                issues["inbox_policy_violations"].append(
                    f"{r}: inbox docs must be publish:false and access:local-only"
                )
            if r in {"inbox/README.md", "inbox/capture/README.md", "inbox/daily/README.md"}:
                if fm.get("type") not in {"Dashboard", "Operations"}:
                    issues["inbox_policy_violations"].append(
                        f"{r}: inbox README must be Dashboard or Operations"
                    )
            elif r.startswith("inbox/daily/") and fm.get("type") != "Daily":
                issues["inbox_policy_violations"].append(f"{r}: inbox/daily notes must be Daily")

        if r.startswith("brain/"):
            if fm.get("publish") is not False or fm.get("access") != "local-only":
                issues["operational_metadata_violations"].append(
                    f"{r}: brain must be publish:false and access:local-only"
                )
            if r.startswith(
                ("brain/review/mail/", "brain/review/codex/", "brain/review/activity/")
            ):
                allowed_statuses = {"Review"}
                if r.startswith("brain/review/mail/"):
                    allowed_statuses.add("Scheduled")
                if fm.get("type") != "Candidate" or fm.get("status") not in allowed_statuses:
                    issues["operational_metadata_violations"].append(
                        f"{r}: review candidate must be type Candidate and a permitted status"
                    )
                # Review cards are a human UI. Opaque IDs and connector
                # locators belong only to the ignored local runtime state.
                for field in ("summary",):
                    value = fm.get(field)
                    if not isinstance(value, str) or not value.strip():
                        issues["operational_metadata_violations"].append(
                            f"{r}: review candidate missing {field}"
                        )
                for field in (
                    "candidate_id",
                    "kind",
                    "source_locator",
                    "time_precision",
                    "activity_id",
                    "idempotency_key",
                    "external_ids",
                ):
                    if field in fm:
                        issues["user_visible_review_violations"].append(
                            f"{r}: review candidate must not expose {field}"
                        )
                if fm.get("review_kind") == "인물 정리":
                    for field in ("people", "person_roles", "attributions", "person_id"):
                        if field in fm:
                            issues["person_schema_violations"].append(
                                f"{r}: person-memory review must not resolve or link a person"
                            )

        if r.startswith("wiki/private/_sources/knowledge/private/"):
            # Private originals are byte-preserved evidence, not compiler input.
            # Their historical frontmatter must not be rewritten merely to fit a
            # current Source schema; private paths are excluded from LLM search.
            if fm.get("publish") is not False or fm.get("access") != "local-only":
                issues["source_policy_violations"].append(
                    f"{r}: private original must be publish:false and access:local-only"
                )
        elif (
            r.startswith("wiki/private/_sources/knowledge/")
            and r != "wiki/private/_sources/knowledge/README.md"
        ):
            if fm.get("type") != "Source":
                issues["source_policy_violations"].append(f"{r}: type must be Source")
            if fm.get("publish") is not False or fm.get("access") != "local-only":
                issues["source_policy_violations"].append(
                    f"{r}: Source must be publish:false and access:local-only"
                )
            if fm.get("source_kind") not in SOURCE_KINDS:
                issues["source_policy_violations"].append(
                    f"{r}: source_kind must be one of {sorted(SOURCE_KINDS)}"
                )
            if fm.get("lifecycle") not in SOURCE_LIFECYCLES:
                issues["source_policy_violations"].append(
                    f"{r}: lifecycle must be one of {sorted(SOURCE_LIFECYCLES)}"
                )

    for book_map, _ in book_sources:
        book_targets = {
            book_map.stem,
            book_map.relative_to(VAULT).with_suffix("").as_posix(),
        }
        book_text = book_map.read_text(encoding="utf-8")
        for target in WIKILINK_RE.findall(book_text):
            for linked_path in resolve_link(target, index):
                linked_rel = rel(linked_path)
                if not linked_rel.startswith("wiki/") or linked_path.name in {
                    "README.md",
                    "index.md",
                }:
                    continue
                linked_targets = set(WIKILINK_RE.findall(texts[linked_path]))
                if linked_targets.isdisjoint(book_targets):
                    message = f"{linked_rel}: missing [[{book_map.stem}]]"
                    if message not in issues["book_source_link_violations"]:
                        issues["book_source_link_violations"].append(message)

    title_index: dict[str, list[Path]] = {}
    for path, fm in metadata.items():
        if is_noncanonical_map_archive(path):
            continue
        r = rel(path)
        title = fm.get("title")
        if isinstance(title, str) and title.strip():
            title_index.setdefault(title.strip(), []).append(path)
    for path in operating_files:
        text = path.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        title = fm.get("title")
        if isinstance(title, str) and title.strip():
            title_index.setdefault(title.strip(), []).append(path)
    for title, paths in sorted(title_index.items()):
        unique_paths = sorted(set(paths))
        if len(unique_paths) > 1:
            issues["duplicate_titles"].append(f"{title!r}: {[rel(path) for path in unique_paths]}")

    canonical_id_index: dict[str, list[Path]] = {}
    for path, fm in metadata.items():
        if not rel(path).startswith("wiki/"):
            continue
        canonical_id = fm.get("canonical_id")
        if isinstance(canonical_id, str) and canonical_id.strip():
            canonical_id_index.setdefault(canonical_id.casefold(), []).append(path)
    for canonical_id, paths in sorted(canonical_id_index.items()):
        if len(paths) > 1:
            issues["wiki_pipeline_policy_violations"].append(
                f"canonical_id {canonical_id!r} is duplicated: {[rel(path) for path in paths]}"
            )

    content_hashes: dict[str, list[str]] = {}
    for path, text in texts.items():
        r = rel(path)
        body = text
        if body.startswith("---\n"):
            parts = body.split("---\n", 2)
            if len(parts) == 3:
                body = parts[2]
        body = re.sub(
            r"<!-- breadcrumb:start -->.*?<!-- breadcrumb:end -->",
            "",
            body,
            flags=re.DOTALL,
        )
        normalized = re.sub(r"\s+", " ", body).strip().casefold()
        if len(normalized) < 80:
            continue
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        content_hashes.setdefault(digest, []).append(r)
    for paths in content_hashes.values():
        if len(paths) > 1:
            issues["duplicate_content"].append(str(sorted(paths)))

    issues["global_graph_root_violations"].extend(
        global_graph_root_issues(files, texts, metadata, index)
    )

    graph: dict[Path, set[Path]] = {path: set() for path in files}
    incoming: dict[Path, set[Path]] = {path: set() for path in files}

    for path, fm in metadata.items():
        if fm.get("publish") is not True:
            continue
        r = rel(path)
        link_text = strip_fenced_blocks(texts[path]).replace("\\|", "|")
        for target in WIKILINK_RE.findall(link_text):
            matches = resolve_link(target, index)
            if not matches:
                issues["broken_wikilinks"].append(f"{r} -> {target}")
                continue
            if len(matches) > 1:
                issues["ambiguous_wikilinks"].append(
                    f"{r} -> {target}: {[rel(p) for p in matches]}"
                )
            for match in matches:
                graph[path].add(match)
                incoming[match].add(path)
                target_rel = rel(match)
                target_fm = metadata.get(match, {})
                intra_wiki = r.startswith("wiki/") and target_rel.startswith("wiki/")
                if (
                    not intra_wiki
                    and fm.get("access") == "public"
                    and target_fm.get("publish") is False
                ):
                    issues["published_links_to_unpublished"].append(f"{r} -> {target_rel}")
                if (
                    not intra_wiki
                    and fm.get("access") == "public"
                    and (
                        target_fm.get("access") != "public"
                        or target_rel.startswith(("wiki/private/_sources/knowledge/", "inbox/"))
                    )
                ):
                    issues["published_links_to_local_only"].append(f"{r} -> {target_rel}")

        # 마크다운 이미지 임베드(예: UML SVG)가 실제 파일을 가리키는지 검증한다.
        # 렌더 UML 정책: 깨진 이미지 링크를 만들지 않기 위해 임베드 대상 파일 존재를 강제한다.
        for im in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", texts[path]):
            href = im.group(1).strip().split()[0]
            if href.startswith(("http://", "https://", "data:", "#", "mailto:")):
                continue
            href_clean = href.split("#")[0].split("?")[0].strip()
            if not href_clean:
                continue
            target_asset = (path.parent / href_clean).resolve()
            if not target_asset.exists():
                issues["missing_asset_refs"].append(f"{r} -> {href}")

    home = VAULT / "README.md"
    reachable: set[Path] = set()
    stack = [home] if home.exists() else []
    while stack:
        current = stack.pop()
        if current in reachable:
            continue
        reachable.add(current)
        stack.extend(sorted(graph.get(current, set()) - reachable))

    for path, fm in metadata.items():
        if fm.get("publish") is not True:
            continue

        r = rel(path)
        if quartz_root(path) not in QUARTZ_SYNC_ROOTS:
            continue

        if path != home and path not in reachable and not r.startswith("wiki/"):
            issues["unreachable_published_from_home"].append(r)

        if path != home and not incoming[path] and not r.startswith("wiki/"):
            issues["zero_incoming_published"].append(r)

        if (
            r.startswith("wiki/")
            and path.name not in {"README.md", "index.md"}
            and fm.get("type") == "Wiki"
            and "<!-- woon-wiki-children:start -->" not in texts[path]
        ):
            body = texts[path].split("\n---", 1)[-1]
            h2_count = sum(1 for line in body.splitlines() if line.startswith("## "))
            visible_body = strip_fenced_blocks(body)
            plain_chars = len(re.sub(r"\s+", "", visible_body))
            if re.search(r"(?m)^## 목차\s*$", visible_body):
                issues["manual_toc_duplicates_headings"].append(r)
            if plain_chars < 500 or h2_count < 2:
                issues["thin_published_wiki_docs"].append(
                    f"{r}: chars={plain_chars}, h2={h2_count}"
                )

    compact = {key: value for key, value in issues.items() if value}
    report = {
        "markdown_files_scanned": len(files),
        "operational_files_scanned": len(operating_files),
        "template_files_scanned": len(template_files),
        "published_files": sum(1 for fm in metadata.values() if fm.get("publish") is True),
        "issue_counts": {key: len(value) for key, value in issues.items()},
        "issues": compact,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if compact else 0


if __name__ == "__main__":
    sys.exit(main())
