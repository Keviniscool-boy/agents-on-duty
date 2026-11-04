"""pytest 全局夹具：阻断开发者本机 .env 对测试结果的污染。

api.main 在导入时执行 load_dotenv(PROJECT_ROOT / ".env")，会把
COOKIE_SECURE、APP_ENV 等部署类变量写进 os.environ。这些变量改变
Cookie 的 Secure 行为，导致测试结果随开发者机器配置漂移。

本夹具在每个测试开始前移除相关变量；需要特定部署语义的测试
（如 Secure Cookie 生产默认）必须显式 monkeypatch.setenv 自己的值，
测试结束后由 monkeypatch 自动恢复。
"""

import pytest

# 会改变 HTTP 鉴权/Cookie 行为的部署类变量；测试默认视为"未配置"。
DEPLOYMENT_ENV_VARS = (
    "COOKIE_SECURE",
    "APP_ENV",
)


@pytest.fixture(autouse=True)
def isolate_deployment_env(monkeypatch):
    """默认隔离部署类环境变量，让测试不依赖开发者 .env。

    用空字符串占位而不是删除：load_dotenv(override=False) 只设置
    尚不存在的变量，占位可以阻断 isolated_auth 等夹具重新加载
    api.main 时把 .env 的值再次注入 os.environ。
    """
    for name in DEPLOYMENT_ENV_VARS:
        monkeypatch.setenv(name, "")
    yield
