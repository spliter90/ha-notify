#!/usr/bin/with-contenv bashio
bashio::log.info "Starte Notify …"
exec python3 -u /app/main.py
