"""Package an already built SPA, while allowing a clean editable checkout."""

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        resources = Path(self.root) / "src" / "patchouli_lib" / "web_resources"
        if not (resources / "index.html").is_file():
            if self.target_name == "wheel" and version == "editable":
                return
            raise RuntimeError("Build the frontend with npm run build --prefix web first.")
        destination = (
            "patchouli_lib/web_resources"
            if self.target_name == "wheel"
            else "src/patchouli_lib/web_resources"
        )
        build_data.setdefault("force_include", {})[str(resources)] = destination
