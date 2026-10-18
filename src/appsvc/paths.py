"""Where the local application keeps its state: the SAME files the CLI uses (no second database)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def _defaults() -> dict[str, Path]:
    from src.experiments.registry import DEFAULT_DB_PATH as registry
    from src.monitoring.store import DEFAULT_DB_PATH as monitoring
    from src.paper.store import DEFAULT_DB_PATH as paper
    return {"registry": Path(registry), "paper_db": Path(paper), "monitoring_db": Path(monitoring)}


@dataclass(frozen=True)
class AppPaths:
    registry: Path = field(default_factory=lambda: _defaults()["registry"])
    paper_db: Path = field(default_factory=lambda: _defaults()["paper_db"])
    monitoring_db: Path = field(default_factory=lambda: _defaults()["monitoring_db"])
    candidates_dir: Path = Path("data/strategy_candidates")
    profiles_dir: Path = Path("data/app/paper_profiles")
    prefs_file: Path = Path("data/app/preferences.json")
    scanner_db: Path = Path("data/scanner/scanner.db")

    @classmethod
    def under(cls, root: Path | str) -> "AppPaths":
        """All state beneath one directory (tests and disposable validation roots)."""
        r = Path(root)
        return cls(registry=r / "experiments.db", paper_db=r / "paper" / "paper_runtime.db",
                   monitoring_db=r / "monitoring" / "monitoring.db", candidates_dir=r / "strategy_candidates",
                   profiles_dir=r / "app" / "paper_profiles", prefs_file=r / "app" / "preferences.json",
                   scanner_db=r / "scanner" / "scanner.db")

    def registry_exists(self) -> bool:
        return self.registry.is_file()

    def paper_exists(self) -> bool:
        return self.paper_db.is_file()

    def monitoring_exists(self) -> bool:
        return self.monitoring_db.is_file()
