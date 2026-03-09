#!/bin/bash

# Sicherstellen, dass wir im richtigen Verzeichnis sind
cd /opt/blm1

# Wir rufen direkt das Python im venv auf.
# Das erspart das manuelle "source activate".
./venv/bin/python app.py
