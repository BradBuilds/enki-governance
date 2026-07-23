# PostGIS + pgvector: spatial queries + vector embeddings
# Base: PostGIS (includes PostgreSQL 16 + PostGIS 3.4)
# Added: pgvector extension for semantic search
FROM postgis/postgis:16-3.4

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*
