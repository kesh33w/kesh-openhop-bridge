"""Clone the exact public component commits. Does not deploy or use hardware."""
import json
from pathlib import Path
import subprocess
root = Path(__file__).resolve().parents[1]
for name, spec in json.loads((root / 'components.json').read_text()).items():
    path = root / 'sources' / name
    if path.exists():
        raise SystemExit(f'Refusing existing destination: {name}')
    subprocess.run(['git', 'clone', '--no-checkout', spec['url'], str(path)], check=True)
    subprocess.run(['git', '-C', str(path), 'checkout', '--detach', spec['commit']], check=True)
