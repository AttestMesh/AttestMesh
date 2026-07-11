FROM ghcr.io/attestmesh/telegram-fts-mcp@sha256:52259a2af2d1d61d8b22fe72a89bad72407670217b6879bfc11b352c1cbcfb13

USER root
RUN python -c "from pathlib import Path; p=Path('/app/telegram_fts_mcp/__main__.py'); s=p.read_text(); old='DATABASE_URL, min_size=1, max_size=4, command_timeout=15,'; new='DATABASE_URL, min_size=1, max_size=4, command_timeout=15, timeout=5,'; assert s.count(old) == 1; p.write_text(s.replace(old, new))"
USER mcp
