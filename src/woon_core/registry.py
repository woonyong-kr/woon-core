"""Repository registry validation and path resolution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from woon_core.errors import WoonError
from woon_core.io import load_yaml

REGISTRY_RELATIVE_PATH = Path("woon-core/registry/repositories.yaml")


@dataclass(frozen=True, slots=True)
class Repository:
    remote: str
    directory: str
    role: str = ""
    output: bool = False


@dataclass(frozen=True, slots=True)
class SyncResult:
    cloned: int
    existing: int


@dataclass(frozen=True, slots=True)
class Registry:
    version: int
    repositories: dict[str, Repository]

    @classmethod
    def load(cls, root: Path) -> Registry:
        path = root / REGISTRY_RELATIVE_PATH
        raw = load_yaml(path)
        raw_repositories = raw.get("repositories")
        if not isinstance(raw_repositories, dict):
            raise WoonError("registry requires a repositories mapping")
        repositories: dict[str, Repository] = {}
        for identifier, item in raw_repositories.items():
            if not isinstance(identifier, str) or not isinstance(item, dict):
                raise WoonError("registry repository entries must be mappings")
            repositories[identifier] = Repository(
                remote=str(item.get("remote", "")),
                directory=str(item.get("directory", "")),
                role=str(item.get("role", "")),
                output=bool(item.get("output", False)),
            )
        registry = cls(version=int(raw.get("version", 0)), repositories=repositories)
        registry.validate()
        return registry

    def validate(self) -> None:
        if self.version != 1:
            raise WoonError(f"unsupported registry version {self.version}")
        seen_directories: dict[str, str] = {}
        for identifier, repository in self.repositories.items():
            if not identifier or not repository.directory or not repository.remote:
                raise WoonError(f"repository {identifier!r} requires remote and directory")
            directory = PurePosixPath(repository.directory)
            if (
                directory.is_absolute()
                or ".." in directory.parts
                or str(directory) != repository.directory
            ):
                raise WoonError(
                    f"repository {identifier!r} has unsafe directory {repository.directory!r}"
                )
            if previous := seen_directories.get(repository.directory):
                raise WoonError(
                    f"repositories {previous!r} and {identifier!r} share directory "
                    f"{repository.directory!r}"
                )
            seen_directories[repository.directory] = identifier
            parsed = urlparse(repository.remote)
            if parsed.scheme != "https" or parsed.hostname != "github.com":
                raise WoonError(
                    f"repository {identifier!r} has unsupported remote {repository.remote!r}"
                )

    def resolve(self, root: Path, reference: str) -> Path:
        identifier, relative = _parse_reference(reference)
        try:
            repository = self.repositories[identifier]
        except KeyError as error:
            raise WoonError(f"unknown repository {identifier!r}") from error
        base = (root / repository.directory).resolve(strict=False)
        resolved = (base / relative).resolve(strict=False)
        if not resolved.is_relative_to(base):
            raise WoonError(f"reference escapes repository {identifier!r}")
        return resolved

    def missing(self, root: Path) -> list[str]:
        return sorted(
            identifier
            for identifier, repository in self.repositories.items()
            if not (root / repository.directory).exists()
        )

    def sync(self, root: Path) -> SyncResult:
        cloned = 0
        existing = 0
        for identifier in sorted(self.repositories):
            repository = self.repositories[identifier]
            target = root / repository.directory
            if target.exists():
                if not (target / ".git").exists():
                    raise WoonError(f"{target} exists but is not a Git checkout")
                existing += 1
                continue
            try:
                subprocess.run(["git", "clone", "--", repository.remote, str(target)], check=True)
            except subprocess.CalledProcessError as error:
                raise WoonError(f"clone {identifier}: {error}") from error
            cloned += 1
        return SyncResult(cloned=cloned, existing=existing)


def _parse_reference(reference: str) -> tuple[str, Path]:
    if not reference.startswith("repo://"):
        if not reference or "/" in reference:
            raise WoonError(f"invalid repository ID {reference!r}")
        return reference, Path()
    rest = reference.removeprefix("repo://")
    identifier, separator, raw_relative = rest.partition("/")
    if not identifier:
        raise WoonError("repo URI requires an ID")
    relative = PurePosixPath(raw_relative) if separator else PurePosixPath()
    if relative.is_absolute() or ".." in relative.parts:
        raise WoonError("repo URI may not escape its repository")
    return identifier, Path(*relative.parts)
