from pathlib import Path

import pytest

from sigsleuth.config import ConfigurationError, load_environment, settings_from_environment


def test_capture_settings_load_from_dotenv(tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "\n".join(
            (
                "SIGSLEUTH_DATABASE_PATH=./data/test.db",
                "SIGSLEUTH_MQTT_HOST=mqtt.example.test",
                "SIGSLEUTH_MQTT_PORT=1883",
                "SIGSLEUTH_MQTT_TOPIC=msh/+/+/+/#",
                "SIGSLEUTH_MQTT_USERNAME=listener",
                "SIGSLEUTH_MQTT_PASSWORD=secret",
            )
        ),
        encoding="utf-8",
    )

    settings = settings_from_environment(load_environment(dotenv_path, environ={}), capture=True)

    assert settings.database_path == Path("./data/test.db")
    assert settings.mqtt_host == "mqtt.example.test"
    assert settings.mqtt_port == 1883
    assert settings.mqtt_topic == "msh/+/+/+/#"
    assert settings.mqtt_username == "listener"
    assert settings.mqtt_password == "secret"


def test_process_environment_overrides_dotenv(tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "SIGSLEUTH_MQTT_HOST=from-file\nSIGSLEUTH_MQTT_TOPIC=file/#\n",
        encoding="utf-8",
    )

    environment = load_environment(
        dotenv_path,
        environ={
            "SIGSLEUTH_MQTT_HOST": "from-process",
            "SIGSLEUTH_MQTT_TOPIC": "process/#",
            "SIGSLEUTH_MQTT_TLS": "true",
        },
    )
    settings = settings_from_environment(environment, capture=True)

    assert settings.mqtt_host == "from-process"
    assert settings.mqtt_topic == "process/#"
    assert settings.mqtt_tls is True


def test_configuration_errors_do_not_include_password() -> None:
    password = "do-not-print-this"

    with pytest.raises(ConfigurationError) as caught:
        settings_from_environment(
            {
                "SIGSLEUTH_MQTT_HOST": "localhost",
                "SIGSLEUTH_MQTT_TOPIC": "msh/#",
                "SIGSLEUTH_MQTT_PASSWORD": password,
            },
            capture=True,
        )

    assert password not in str(caught.value)


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("SIGSLEUTH_RETENTION_DAYS", "-1"),
        ("SIGSLEUTH_RETENTION_DAYS", "many"),
        ("SIGSLEUTH_COVERAGE_RETENTION_DAYS", "-1"),
    ),
)
def test_retention_days_must_be_a_non_negative_integer(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=name):
        settings_from_environment({name: value}, capture=False)


def test_api_port_error_names_the_api_setting() -> None:
    with pytest.raises(ConfigurationError, match="SIGSLEUTH_API_PORT"):
        settings_from_environment({"SIGSLEUTH_API_PORT": "invalid"}, capture=False)


def test_mqtt_host_and_topic_are_required_only_for_capture() -> None:
    assert settings_from_environment({}, capture=False).mqtt_host == ""
    with pytest.raises(ConfigurationError, match="SIGSLEUTH_MQTT_HOST"):
        settings_from_environment({}, capture=True)


@pytest.mark.parametrize("topic", ["msh/#/x", "msh/a#", "msh/a+/b"])
def test_invalid_topic_filter_is_rejected_at_startup(topic: str) -> None:
    environment = {"SIGSLEUTH_MQTT_HOST": "broker", "SIGSLEUTH_MQTT_TOPIC": topic}
    with pytest.raises(ConfigurationError, match="SIGSLEUTH_MQTT_TOPIC"):
        settings_from_environment(environment, capture=True)


def test_fixed_now_needs_an_offset_and_no_capture() -> None:
    settings = settings_from_environment(
        {"SIGSLEUTH_FIXED_NOW": "2026-10-12T16:46:00Z"}, capture=False
    )
    assert settings.fixed_now is not None
    assert settings.fixed_now.isoformat() == "2026-10-12T16:46:00+00:00"
    for value in ("2026-10-12T16:46:00", "yesterday"):
        with pytest.raises(ConfigurationError, match="SIGSLEUTH_FIXED_NOW"):
            settings_from_environment({"SIGSLEUTH_FIXED_NOW": value}, capture=False)
    capture = {"SIGSLEUTH_MQTT_HOST": "broker", "SIGSLEUTH_MQTT_TOPIC": "msh/#"}
    with pytest.raises(ConfigurationError, match="SIGSLEUTH_FIXED_NOW"):
        settings_from_environment(
            capture | {"SIGSLEUTH_FIXED_NOW": "2026-10-12T16:46Z"}, capture=True
        )


def test_hide_opted_out_is_on_unless_turned_off() -> None:
    def hide(value: str | None) -> bool:
        environment = {} if value is None else {"SIGSLEUTH_HIDE_OPTED_OUT": value}
        return settings_from_environment(environment, capture=False).hide_opted_out

    assert hide(None) and hide("") and hide("true")
    assert not hide("false") and not hide("off")
    with pytest.raises(ConfigurationError, match="SIGSLEUTH_HIDE_OPTED_OUT"):
        hide("maybe")
