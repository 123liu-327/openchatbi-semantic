"""Load the project's .env and launch the local semantic workbench."""

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from scripts.run_semantic_demo import prepare


def main() -> int:
    root = Path(__file__).resolve().parent
    env_file = root / ".env"
    load_dotenv(env_file, override=False)
    missing = [name for name in ("OPENAI_API_KEY", "CHATBI_MODEL") if not os.environ.get(name, "").strip()]
    if missing:
        print(f"Fill in {env_file}: {', '.join(missing)}", file=sys.stderr)
        return 2
    _, _, config_file = prepare(root / "data/semantic_demo", model=os.environ["CHATBI_MODEL"])
    child_env = {**os.environ, "CONFIG_FILE": str(config_file), "PYTHONUTF8": "1"}
    print("Starting workbench: http://localhost:8501", flush=True)
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(root / "sample_ui/streamlit_ui.py"),
                "--server.address=localhost",
                "--server.port=8501",
                "--server.headless=true",
                "--server.showEmailPrompt=false",
                "--browser.gatherUsageStats=false",
            ],
            cwd=root,
            env=child_env,
            check=True,
        )
    except KeyboardInterrupt:
        return 0
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
