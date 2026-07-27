#!/bin/bash

# DBの初期設定
/app/src/db-init.sh

# Wikipediaデータのダウンロード・保存
/app/src/download.py

# MCPサーバーの起動
export PYTHONPATH=/app/src:$PYTHONPATH
/app/src/local_wikipedia.py
