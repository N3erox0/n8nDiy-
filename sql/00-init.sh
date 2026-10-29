#!/bin/bash
# Postgres 首次初始化时执行：为 worker 建业务库 cf 和测试库 cf_test
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE DATABASE cf;
  CREATE DATABASE cf_test;
EOSQL
