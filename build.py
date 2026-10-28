"""Make sure vendor/pokerbot is on disk before Vercel bundles the function.

The Git submodule is the source of truth. A Vercel build that does not check
the submodule out still needs those modules, so clone the recorded commit.
"""
import shutil
import subprocess
from pathlib import Path

DEST = Path('vendor/pokerbot')
URL = 'https://github.com/YosiefAbraham/pokerbot.git'


def recorded_sha():
    try:
        line = subprocess.check_output(
            ['git', 'ls-tree', 'HEAD', 'vendor/pokerbot'], text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    parts = line.split()
    return parts[2] if len(parts) >= 3 else None


def main():
    if (DEST / 'protocol.py').is_file():
        print('pokerbot already present')
        return
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.parent.mkdir(parents=True, exist_ok=True)
    sha = recorded_sha()
    if sha:
        subprocess.check_call(['git', 'clone', '--depth', '1', URL, str(DEST)])
        # A shallow clone of main may already be this commit. Fetch it if not.
        head = subprocess.check_output(['git', '-C', str(DEST), 'rev-parse', 'HEAD'], text=True).strip()
        if head != sha:
            subprocess.check_call(['git', '-C', str(DEST), 'fetch', '--depth', '1', 'origin', sha])
            subprocess.check_call(['git', '-C', str(DEST), 'checkout', sha])
    else:
        subprocess.check_call(['git', 'clone', '--depth', '1', URL, str(DEST)])
    print('pokerbot ready', recorded_sha() or 'main')


if __name__ == '__main__':
    main()
