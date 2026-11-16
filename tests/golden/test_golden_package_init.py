"""Contract for the package entry point.

``import openchatbi`` must stay cheap: the patrol job's whole premise is that a
scheduled run needs no LLM, so importing the package must not drag the config and
model stack in. Timing is not asserted (flaky on CI); module presence is, which
is the actual contract.
"""

import subprocess
import sys
import textwrap

import pytest

import openchatbi


def _run_isolated(code: str) -> subprocess.CompletedProcess:
    """Run `code` in a fresh interpreter so import order is not pre-warmed."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestLazyConfigLoading:
    def test_plain_import_does_not_import_the_config_stack(self):
        result = _run_isolated("""
            import sys
            import openchatbi
            print("config_loader" in " ".join(sys.modules))
            print("openchatbi.config_loader" in sys.modules)
            """)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == "False"

    def test_first_attribute_access_loads_the_loader_exactly_once(self):
        result = _run_isolated("""
            import openchatbi
            first = openchatbi._get_config()
            second = openchatbi._get_config()
            print(first is second)
            """)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "True"


class TestBackwardsCompatibility:
    def test_from_package_import_config_still_works(self):
        from openchatbi import config

        assert type(config).__name__ == "ConfigLoader"

    def test_config_classes_remain_reachable_from_the_package(self):
        from openchatbi import Config, ConfigLoader

        assert ConfigLoader is openchatbi._get_config().__class__
        assert Config.__name__ == "Config"

    def test_unknown_attribute_still_raises_attribute_error(self):
        with pytest.raises(AttributeError, match="does_not_exist"):
            _ = openchatbi.does_not_exist
