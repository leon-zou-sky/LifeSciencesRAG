"""
MySQL 连接（源数据层）

连接 PoC 自己的 MySQL（localhost:3307）。配置可用环境变量覆盖，
默认值为本地 docker-compose 开发环境（非生产凭据）。
"""

import os

import pymysql
from pymysql.cursors import DictCursor

DB_CONFIG = {
    "host": os.environ.get("LS_RAG_MYSQL_HOST", "localhost"),
    "port": int(os.environ.get("LS_RAG_MYSQL_PORT", "3307")),
    "user": os.environ.get("LS_RAG_MYSQL_USER", "root"),
    "password": os.environ.get("LS_RAG_MYSQL_PASSWORD", "root123"),
    "database": os.environ.get("LS_RAG_MYSQL_DB", "ls_rag"),
    "charset": "utf8mb4",
}


def get_conn():
    return pymysql.connect(**DB_CONFIG, cursorclass=DictCursor, autocommit=False)
