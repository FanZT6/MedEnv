#!/usr/bin/env python3
"""Check Git's public file set; optionally export a data-free GitHub source archive."""
import argparse
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_DATA = {'data/README.md', 'data/dataset_summary.json'}


def public_files():
    raw = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT)
    return sorted(set(name.decode() for name in raw.split(b'\0') if name))


def audit(files):
    problems = []
    secret_pattern = re.compile(r'(?:sk-[A-Za-z0-9_-]{24,}|hf_[A-Za-z0-9]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)')
    for name in files:
        path = ROOT / name
        if name.startswith(('data/bench/', 'data/retrieval/', 'data/private/', 'outputs/', 'models/')):
            problems.append(f'Restricted artifact: {name}')
        if name.startswith('data/') and name not in ALLOWED_DATA and not name.startswith('data/synthetic/'):
            problems.append(f'Unapproved public dataset file: {name}')
        if path.is_symlink():
            problems.append(f'Symlink in release: {name}')
            continue
        if not path.is_file():
            problems.append(f'Missing Git file: {name}')
            continue
        if path.stat().st_size > 10 * 1024 * 1024:
            problems.append(f'Unexpected large release file: {name}')
            continue
        if re.search(r'(^|/)\.env($|\.)', name) and name != '.env.example':
            problems.append(f'Environment credentials file: {name}')
        if re.search(r'(ehr|osce)_\d+\.json$|trajectory_.*\.json$|\.(safetensors|index|pt|pth)$', name):
            problems.append(f'Clinical/model artifact: {name}')
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            if not name.startswith('data/synthetic/'):
                problems.append(f'Unexpected binary file: {name}')
            continue
        if secret_pattern.search(text):
            problems.append(f'Potential credential: {name}')
        if re.search(r'/(?:data|home)/xuxiang/', text):
            problems.append(f'Machine-specific path: {name}')
        if path.suffix in {'.json', '.jsonl', '.csv'} and re.search(r'"(?:subject_id|hadm_id|stay_id)"\s*:\s*\d+', text):
            problems.append(f'Patient identifier payload: {name}')
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    files = public_files()
    problems = audit(files)
    if problems:
        raise SystemExit('\n'.join(problems))
    if args.archive:
        args.archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.archive, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                archive.write(ROOT / name, 'MedEnv/' + name)
    print(f'PASS: {len(files)} public files; no restricted data or detected credentials')
    if args.archive:
        print(f'Public archive: {args.archive}')


if __name__ == '__main__':
    main()
