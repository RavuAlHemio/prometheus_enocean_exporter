#!/bin/sh

set -e

echo ruff
uv run ruff check "prometheus_enocean_exporter.py"

echo ty
uv run ty check "prometheus_enocean_exporter.py"

echo pylint
uv run pylint \
	--disable=C --disable=R \
	--disable=W0511 \
	--enable=C0301 \
	"prometheus_enocean_exporter.py"

echo mypy
uv run mypy "prometheus_enocean_exporter.py"
