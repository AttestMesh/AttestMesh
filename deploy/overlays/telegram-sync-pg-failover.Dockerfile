FROM ghcr.io/attestmesh/telegram-sync@sha256:a5cbf8386a99004ff930578ab16fb10dd594a190985db48147eb8cac8c747ef0

USER root
RUN python -c "from pathlib import Path; p=Path('/srv/app/db.py'); s=p.read_text(); old='await asyncpg.create_pool(database_url, min_size=1, max_size=5)'; new='await asyncpg.create_pool(database_url, min_size=1, max_size=5, timeout=5)'; assert s.count(old) == 1; p.write_text(s.replace(old, new))"
USER tgsync
