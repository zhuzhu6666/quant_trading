from execution.broker_config import BrokerConnectionConfig


def test_environment_overrides_yaml_for_effective_broker_identity():
    config = BrokerConnectionConfig.from_sources(
        {
            "ctrader": {
                "host": "demo.ctraderapi.com",
                "account_id": 12,
                "port": 5035,
                "symbol": "XAUUSD",
            }
        },
        {
            "CTRADER_HOST": "live.ctraderapi.com",
            "CTRADER_ACCOUNT_ID": "99",
        },
    )

    assert config.host == "live.ctraderapi.com"
    assert config.account_id == 99
    assert config.environment == "live"
    assert config.is_demo is False


def test_config_is_immutable_and_safe_projection_excludes_secrets():
    config = BrokerConnectionConfig.from_sources(
        {"ctrader": {"host": "demo.ctraderapi.com"}},
        {
            "CTRADER_CLIENT_ID": "client",
            "CTRADER_CLIENT_SECRET": "secret",
            "CTRADER_ACCESS_TOKEN": "token",
            "CTRADER_ACCOUNT_ID": "42",
        },
    )

    assert config.is_demo is True
    assert config.credentials_present is True
    assert "client_secret" not in config.to_safe_dict()
    assert "access_token" not in config.to_safe_dict()
    assert len(config.config_hash) == 64


def test_demo_environment_requires_the_canonical_ctrader_demo_host():
    lookalike = BrokerConnectionConfig.from_sources(
        {"ctrader": {"host": "demo.attacker.invalid"}},
        {},
    )
    canonical_with_dns_dot = BrokerConnectionConfig.from_sources(
        {"ctrader": {"host": "DEMO.CTRADERAPI.COM."}},
        {},
    )

    assert lookalike.environment == "live"
    assert lookalike.is_demo is False
    assert canonical_with_dns_dot.environment == "demo"
    assert canonical_with_dns_dot.is_demo is True
