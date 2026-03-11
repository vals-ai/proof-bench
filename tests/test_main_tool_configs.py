import argparse
from pathlib import Path

from tests.support import install_model_library_stub, reload_module


def _load_main_module():
    install_model_library_stub()
    return reload_module("main")


def _make_args(**overrides):
    args = argparse.Namespace(
        dataset="exported",
        model="openai/gpt-4o",
        k=1,
        temperature=0.7,
        log_file=None,
        problem_id=None,
        domains=None,
        include_nl_proof=False,
        enable_loogle=False,
        loogle_local=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_build_tool_configs_always_returns_run_code_config():
    main_module = _load_main_module()
    args = _make_args(enable_loogle=False)
    loogle_config, run_code_config = main_module.build_tool_configs(args)

    assert loogle_config is None
    assert run_code_config["transport"] == "stdio"
    assert "stdio_command" in run_code_config
    assert "project_path" in run_code_config


def test_main_forwards_run_code_config(monkeypatch, tmp_path):
    main_module = _load_main_module()
    args = _make_args(enable_loogle=False)
    dataset = [{"id": "p1"}]
    captured: dict = {}

    monkeypatch.setattr(main_module.argparse.ArgumentParser, "parse_args", lambda self: args)
    monkeypatch.setattr(main_module, "setup_logging", lambda parsed: ("dummy.log", Path(tmp_path)))
    monkeypatch.setattr(main_module, "load_dataset", lambda parsed: dataset)
    monkeypatch.setattr(main_module, "filter_dataset", lambda data, problem_id, domains: (data, "all"))
    monkeypatch.setattr(main_module, "write_metadata", lambda parsed, log_dir, problem_scope, size: None)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    def _fake_run_proving_pipeline(*_args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(main_module, "run_proving_pipeline", _fake_run_proving_pipeline)

    main_module.main()

    assert captured["run_code_config"] is not None
    assert captured["loogle_config"] is None
