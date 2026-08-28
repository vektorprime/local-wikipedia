#!/bin/bash

# Initial database setup
/app/src/db-init.sh

# Download and store the Wikipedia data
/app/src/download.py

# Start the MCP server
export PYTHONPATH=/app/src:$PYTHONPATH
/app/src/local_wikipedia.py
