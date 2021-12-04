#!/bin/sh
python3 -m pylint \
	--disable=C --disable=R \
	--disable=W0511 \
	--enable=C0301 \
	"prometheus_enocean_exporter.py"
