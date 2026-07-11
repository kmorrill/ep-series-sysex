# Contributing

Thanks for helping improve EP interoperability.

Please open an issue or pull request for changes. Direct pushes to `main` are
reserved for repository administration; contributions are reviewed by the code
owner before merge.

Do not submit copyrighted sample audio, Teenage Engineering factory/OEM projects,
personal device backups, or raw captures that contain material you cannot legally
redistribute. Prefer synthetic compiler-generated fixtures. If protocol evidence
requires a hardware capture, describe the observation and provide the smallest
non-copyrightable byte excerpt needed to reproduce it.

Run the offline suite before opening a pull request:

```sh
python3 tests/test_roundtrip.py
```
