# Data directories

- `synthetic/`: four public software-test cases; images are generated patterns.
- `bench/`: complete local prepared OSCE benchmark, Git-ignored.
- `retrieval/`: local third-party guidelines and FAISS index, Git-ignored.
- `private/`: per-file integrity manifests, Git-ignored.
- `dataset_summary.json`: public aggregate inventory without patient identifiers.

See [data access and provenance](../docs/DATA.md) and use
`python scripts/import_data.py --help` to import an authorized local copy.
The Apache-2.0 code license does not grant rights to redistribute source data.
