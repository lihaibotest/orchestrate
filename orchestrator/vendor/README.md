# Vendored Runtime Dependency

The distroless Python 3.10 runtime has no package installer. PostgreSQL support
therefore vendors the unpacked `psycopg2-binary` wheel under `vendor/python`.

- Package: `psycopg2-binary 2.9.13`
- Wheel: `cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64`
- SHA-256: `d2fc9342aad969b9a28490a4c3eaba94b35beb2d26e9a39b31d1430378aa71b2`
- Source: Python package index configured for the build environment

The normal development dependency remains declared in `pyproject.toml`. Redis
is configuration-only in P0-02 and is not vendored or imported by the core.
