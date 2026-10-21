#!/usr/bin/env python3
"""Check configured endpoints without submitting clinical records."""
import argparse
import json
import os
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-retrieval', action='store_true')
    parser.add_argument('--no-grounding', action='store_true')
    args = parser.parse_args()
    checks = [
        ('doctor', os.getenv('DOCTOR_BASE_URL', 'http://127.0.0.1:30000/v1') + '/models', os.getenv('DOCTOR_API_KEY', 'None'), os.getenv('DOCTOR_MODEL', 'doctor_agent')),
        ('patient', os.getenv('RLLM_PATIENT_BASE_URL', 'http://127.0.0.1:30001/v1') + '/models', os.getenv('RLLM_PATIENT_API_KEY', 'None'), os.getenv('RLLM_PATIENT_MODEL', 'patient_agent')),
    ]
    if os.getenv('JUDGE_MODEL'):
        checks.append(('judge', os.getenv('JUDGE_BASE_URL', os.getenv('DOCTOR_BASE_URL', 'http://127.0.0.1:30000/v1')) + '/models', os.getenv('JUDGE_API_KEY', 'None'), os.environ['JUDGE_MODEL']))
    if not args.no_retrieval:
        checks.append(('retrieval', os.getenv('RETRIEVAL_SERVER_URL', 'http://127.0.0.1:8003').rstrip('/') + '/health', '', ''))
    if not args.no_grounding:
        checks.append(('grounding', os.getenv('RLLM_GROUNDING_API_URL', 'http://127.0.0.1:30050/ground').rsplit('/', 1)[0] + '/health', '', ''))
    failed = False
    for name, url, key, model in checks:
        try:
            request = Request(url, headers={'Authorization': f'Bearer {key}'} if key else {})
            with urlopen(request, timeout=5) as response:
                payload = json.load(response)
            if model and model not in [row['id'] for row in payload['data']]:
                raise ValueError('Configured model is not served')
            print(f'{name}: ready')
        except Exception as error:
            print(f'{name}: FAILED ({type(error).__name__})')
            failed = True
    raise SystemExit(failed)


if __name__ == '__main__':
    main()
