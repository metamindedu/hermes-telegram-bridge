"""Resolve the selected Hermes dependency environment on every invocation.

The bootstrap interpreter needs only stdlib. No generation paths are cached,
no environment is installed, and a broken PM selection fails closed.
"""
import os
from pathlib import Path
import subprocess
import sys


def main():
    directory = Path(__file__).resolve().parent
    home = directory.parents[1]
    repo = home / 'hermes-agent'
    # The sender intentionally uses the global notification destination.
    os.environ['HERMES_HOME'] = str(home)
    os.environ.pop('HERMES_PROFILE', None)
    sys.path.insert(0, str(repo))
    from pm.environments import project_python
    python = project_python(repo)
    if not python.is_file():
        raise RuntimeError('Selected Hermes interpreter is unavailable')
    env = dict(os.environ)
    env.pop('PYTHONHOME', None)
    env.pop('VIRTUAL_ENV', None)
    env['PYTHONPATH'] = str(repo)
    result = subprocess.run([str(python), str(directory / 'notifier.py'), *sys.argv[1:]], env=env)
    return result.returncode


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # Never expose credential-bearing exception text.
        print('{"ok":false,"reason":"runtime_launcher_failure"}')
        sys.exit(1)
