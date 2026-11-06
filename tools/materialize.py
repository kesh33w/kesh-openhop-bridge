"""Clone pinned public component commits. Does not deploy or use hardware."""
import argparse
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--manifest",
    type=Path,
    default=Path("components.json"),
    help="Manifest path, relative to the hub root unless absolute. Default: components.json.",
)
args = parser.parse_args()
manifest = args.manifest if args.manifest.is_absolute() else root / args.manifest

for name, spec in json.loads(manifest.read_text()).items():
    path = root / "sources" / name
    if path.exists():
        raise SystemExit(f"Refusing existing destination: {name}")
    subprocess.run(["git", "clone", "--no-checkout", spec["url"], str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "checkout", "--detach", spec["commit"]], check=True)
