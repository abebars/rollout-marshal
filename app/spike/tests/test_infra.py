"""Static checks on infra/spike/main.tf: the guards of design.md section 2 for task 1.2.

terraform validate and plan prove the file works; these tests pin the guards so an edit
cannot drop one quietly. Whitespace is normalised because terraform fmt aligns the `=`.
"""

from __future__ import annotations

import re
from pathlib import Path

from contracts import names

ROOT = Path(__file__).resolve().parents[3]
MAIN_TF = (ROOT / names.SPIKE_INFRA_DIR / "main.tf").read_text()
ROOT_MAIN_TF = (ROOT / names.INFRA_DIR / "main.tf").read_text()


def code(text: str) -> str:
    """The HCL without comments, every run of whitespace as one space."""
    lines = (line.split("#", 1)[0] for line in text.splitlines())
    return re.sub(r"\s+", " ", " ".join(lines))


TF = code(MAIN_TF)


def test_the_provider_guard_matches_the_root_module():
    root = code(ROOT_MAIN_TF)
    for line in (
        f'account_id = "{names.ACCOUNT_ID}"',
        f'region = "{names.REGION}"',
        "region = local.region",
        "allowed_account_ids = [local.account_id]",
        'project = "rollout-marshal"',
    ):
        assert line in root
        assert line in TF


def test_local_state_only():
    assert "backend" not in TF  # no backend block: state stays local, and *.tfstate is ignored


def test_every_resource_name_is_built_from_the_spike_prefix():
    assert 'function_name = "${local.names.SPIKE_RESOURCE_PREFIX}sdk"' in TF
    assert 'role_name = "${local.function_name}-role"' in TF
    assert 'policy_name = "${local.function_name}-policy"' in TF
    assert "log_group = local.function_name" in TF
    for resource, attribute in [
        ('resource "aws_lambda_function" "spike"', "function_name = local.function_name"),
        ('resource "aws_iam_role" "spike"', "name = local.role_name"),
        ('resource "aws_iam_role_policy" "spike"', "name = local.policy_name"),
        ('resource "aws_cloudwatch_log_group" "spike"', "name = local.log_group"),
    ]:
        assert f"{resource} {{ {attribute}" in TF, resource
    assert len(re.findall(r'resource "', TF)) == 4  # nothing else is created


def test_the_function_gets_the_secret_name_never_its_value():
    assert "FLAGSMITH_KEY_SECRET_NAME = local.names.SECRET_FLAGSMITH_SERVER_KEY" in TF
    assert "aws_secretsmanager_secret_version" not in TF
    assert "secret_string" not in TF
    assert "SECRET_FLAGSMITH_API_KEY" not in TF  # the organisation key is never in reach


def test_runtime_and_architecture_come_from_names():
    assert 'runtime = "python${local.names.PYTHON_VERSION}"' in TF
    assert "architectures = [local.names.LAMBDA_ARCHITECTURE]" in TF
