"""真实环境验证（审查 §5.1）：SQL 执行期错误 → 降级而非 500。

方法：创建一个只有 SELECT 权限的受限用户，用它在真实 MySQL 上
执行 INSERT——得到确定性的服务端执行期错误（命令被拒绝），
验证 mysql_store 将其转为 MySQLUnavailableError、short_term
落入进程内兜底而不是抛 500。

运行：仓库根目录
DASHSCOPE_API_KEY=dummy MYSQL_HOST=127.0.0.1 MYSQL_PORT=3308 \
MYSQL_USER=root MYSQL_PASSWORD=... MYSQL_DATABASE=smart_cs \
python verify_sql_error_injection.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymysql

from memory.short_term import ShortTermMemory
from storage.mysql_store import MySQLStore, MySQLUnavailableError, mysql_store


def main() -> int:
    mysql_store.self_check()
    print("MySQL 自检通过（真实环境）")

    # 1. 创建受限用户（仅 SELECT），确定性制造"执行期"SQL 错误。
    admin = pymysql.connect(**{
        **{k: v for k, v in mysql_store._config().items() if k != "charset"},
    })
    with admin.cursor() as cursor:
        cursor.execute(
            "CREATE USER IF NOT EXISTS 'limited'@'%' IDENTIFIED BY 'limited_pw'"
        )
        cursor.execute("GRANT SELECT ON smart_cs.* TO 'limited'@'%'")
        cursor.execute("FLUSH PRIVILEGES")
    admin.close()
    print("受限用户 limited 已就绪（仅 SELECT）")

    # 2. 用受限凭据构造存储实例。
    class LimitedStore(MySQLStore):
        @staticmethod
        def _config():
            return {
                "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
                "port": int(os.getenv("MYSQL_PORT", "3306")),
                "user": "limited",
                "password": "limited_pw",
                "database": os.getenv("MYSQL_DATABASE", "smart_cs"),
                "charset": "utf8mb4",
                "autocommit": True,
                "connect_timeout": 5,
            }

    limited = LimitedStore()

    # 3. 执行期错误必须转为 MySQLUnavailableError（而非裸 OperationalError）。
    converted = False
    try:
        limited.add_message("sess-inject", "user", {"role": "user", "content": "注入测试"})
    except MySQLUnavailableError as exc:
        converted = True
        print(f"执行期 SQL 错误已转换: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL 未转换的异常: {type(exc).__name__}: {exc}")

    # 4. short_term 用受限存储时消息应落进程内兜底（调用方降级语义）。
    from memory import short_term as st_mod

    original = st_mod.mysql_store
    st_mod.mysql_store = limited
    try:
        memory = ShortTermMemory(redis_url="redis://127.0.0.1:6399/0")

        async def run():
            await memory.add_message("sess-inject", "user", "注入测试", user_id="u1")
            return await memory.get_history("sess-inject")

        history = asyncio.run(run())
        fell_back = bool(history) and history[-1]["content"] == "注入测试"
    finally:
        st_mod.mysql_store = original

    ok = converted and fell_back
    print(f"{'PASS' if ok else 'FAIL'} SQL 执行期错误 → 转换 + 进程内兜底（非 500）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
