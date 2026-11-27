"""Tests for scripts/bootstrap_flagsmith.py and the Admin API client it holds (task 3.5).

No call leaves the machine: Flagsmith is app/tests/flagsmith_fake.py, Secrets Manager and
STS are Stubbers or fakes, and the AWS environment holds dummy credentials only.
"""

from __future__ import annotations

import os

import boto3
import pytest
from botocore.stub import Stubber

from app.tests.flagsmith_fake import DEV_ID, DEV_KEY, PROD_KEY, FakeFlagsmith
from contracts import names
from scripts import bootstrap_flagsmith as bootstrap

ORG_KEY = "dummyOrganisationKeyForTests"
KILL, DEFAULT = names.KILL_SWITCH_FEATURE, names.DEFAULT_MODEL_FEATURE


@pytest.fixture(autouse=True)
def _dummy_aws(monkeypatch):
    for name in [name for name in os.environ if name.startswith("AWS_") or name == "BOTO_CONFIG"]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing-secret")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("BOTO_CONFIG", os.devnull)


@pytest.fixture
def flagsmith():
    return FakeFlagsmith()


@pytest.fixture
def admin(flagsmith):
    return bootstrap.Admin(ORG_KEY, transport=flagsmith)


def _run(admin, *argv):
    lines = []
    code = bootstrap.main(list(argv), admin=admin, out=lines.append)
    return code, "\n".join(lines)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------
def test_every_request_carries_the_key_only_in_the_authorization_header(admin, flagsmith):
    admin.development()
    [headers] = flagsmith.headers
    assert headers["Authorization"] == names.api_key_authorization(ORG_KEY) == f"Api-Key {ORG_KEY}"
    assert all(
        ORG_KEY not in str(body) and ORG_KEY not in path for _, path, body in flagsmith.requests
    )


def test_the_client_never_sends_delete_or_patch_or_an_end_path(admin, flagsmith):
    for method in ("DELETE", "PATCH"):
        with pytest.raises(bootstrap.Refused):
            admin.request(method, "/api/v1/environments/x/experiments/1/")
    for path in names.FLAGSMITH_ADMIN_FORBIDDEN_PATHS:
        with pytest.raises(bootstrap.Refused):
            admin.request("POST", f"/api/v1/environments/x/experiments/1/{path}")
    assert flagsmith.requests == []


def test_an_error_names_the_path_without_the_environment_key(admin, flagsmith):
    flagsmith.fail[("GET", "experiments")] = 500
    with pytest.raises(bootstrap.FlagsmithError) as raised:
        admin.request("GET", admin.environment_path("experiments/?status=running"))
    assert DEV_KEY not in str(raised.value)
    assert bootstrap.REDACTED_ENVIRONMENT in str(raised.value) and "500" in str(raised.value)


def test_redact_path_replaces_environment_keys_but_not_environment_ids():
    assert DEV_KEY not in bootstrap.redact_path(f"/api/v1/environments/{DEV_KEY}/experiments/")
    assert bootstrap.redact_path(f"/api/v1/environments/{DEV_ID}/features/1/versions/") == (
        f"/api/v1/environments/{DEV_ID}/features/1/versions/"
    )


def test_development_is_found_by_name_and_read_once(admin, flagsmith):
    assert admin.development() == {"id": DEV_ID, "key": DEV_KEY}
    assert admin.development()["key"] != PROD_KEY
    assert len(flagsmith.requests) == 1


def test_set_state_takes_the_four_v2_steps_and_changes_nothing_until_published(admin, flagsmith):
    """The REST form of the four calls task 1.3 made through MCP, as task 3.5's live run
    (evidence/3.5-app-infra.md) sent them."""
    feature = flagsmith.add_feature("spike_v2", enabled=True)

    admin.set_state(feature, enabled=False)

    base = f"/api/v1/environments/{DEV_ID}/features/{feature['id']}/versions/"
    [create, read, update, publish] = flagsmith.requests[1:]
    assert create == ("POST", base, {})
    uuid = next(iter(flagsmith.versions))
    assert read == ("GET", f"{base}{uuid}/featurestates/", None)
    state = flagsmith.versions[uuid]["states"][0]
    assert update == (
        "PUT",
        f"{base}{uuid}/featurestates/{state['id']}/",
        {
            "feature": feature["id"],
            "enabled": False,
            "feature_state_value": {"type": "unicode", "string_value": None},
        },
    )
    assert publish == ("POST", f"{base}{uuid}/publish/", {})
    assert flagsmith.state("spike_v2") == (False, None)


def test_set_state_sets_a_string_value_as_unicode(admin, flagsmith):
    feature = flagsmith.add_feature(DEFAULT, value=names.VARIANT_CHEAPER)
    admin.set_state(feature, value=names.VARIANT_CURRENT)
    assert flagsmith.state(DEFAULT) == (True, names.VARIANT_CURRENT)
    [(_, _, body)] = [r for r in flagsmith.requests if r[0] == "PUT"]
    assert body["feature_state_value"] == {
        "type": names.API_VERSION_VALUE_TYPE,
        "string_value": names.VARIANT_CURRENT,
    }


def test_find_feature_matches_the_exact_name_only(admin, flagsmith):
    flagsmith.add_feature(f"{KILL}_old")
    assert admin.find_feature(KILL) is None
    found = flagsmith.add_feature(KILL)
    assert admin.find_feature(KILL)["id"] == found["id"]


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def test_bootstrap_creates_both_permanent_features_with_their_defaults(admin, flagsmith):
    code, text = _run(admin)

    assert code == bootstrap.EXIT_OK
    assert flagsmith.feature(KILL)["type"] == names.API_FEATURE_STANDARD
    assert flagsmith.state(KILL)[0] is names.KILL_SWITCH_DEFAULT_ENABLED
    assert flagsmith.feature(DEFAULT)["type"] == names.API_FEATURE_STANDARD
    assert flagsmith.state(DEFAULT) == (True, names.DEFAULT_MODEL_DEFAULT)
    assert f"created {KILL}" in text and f"created {DEFAULT}" in text


def test_bootstrap_finds_existing_features_and_changes_nothing(admin, flagsmith):
    """The defaults apply only on create: a found state is never compared or reset."""
    flagsmith.add_feature(KILL, enabled=False)  # rolled back
    flagsmith.add_feature(DEFAULT, value=names.VARIANT_CHEAPER)  # promoted

    code, _ = _run(admin)

    assert code == bootstrap.EXIT_OK
    assert flagsmith.writes() == []
    assert flagsmith.state(KILL)[0] is False
    assert flagsmith.state(DEFAULT)[1] == names.VARIANT_CHEAPER


@pytest.mark.parametrize(
    ("setup", "problem"),
    [
        (lambda f: f.add_feature(KILL, "MULTIVARIATE"), "MULTIVARIATE"),
        (lambda f: (f.add_feature(KILL), f.add_feature(DEFAULT, "MULTIVARIATE")), "MULTIVARIATE"),
        (lambda f: (f.add_feature(KILL), f.add_feature(DEFAULT, value="fable")), "fable"),
        (lambda f: (f.add_feature(KILL), f.add_feature(DEFAULT, value=None)), "None"),
    ],
    ids=["a multivariate kill switch", "a multivariate default", "a wrong value", "no value"],
)
def test_bootstrap_stops_on_a_feature_that_does_not_match(admin, flagsmith, setup, problem):
    setup(flagsmith)
    code, text = _run(admin)
    assert code == bootstrap.EXIT_FAILED
    assert problem in text and "dashboard" in text
    assert flagsmith.writes() == []


def test_v2_probe_runs_only_on_a_spike_feature(admin, flagsmith):
    flagsmith.add_feature(KILL)
    code, text = _run(admin, "--v2-probe", KILL)
    assert code == bootstrap.EXIT_REFUSED and "spike_" in text
    assert flagsmith.writes() == []


def test_v2_probe_turns_the_feature_off_then_on_and_reads_each_back(admin, flagsmith):
    flagsmith.add_feature("spike_kill_switch", enabled=True)

    readings = bootstrap.v2_probe(admin, "spike_kill_switch", out=lambda line: None)

    assert readings == [("before", True), ("set False", False), ("set True", True)]
    assert [m for m, _ in flagsmith.writes()] == ["POST", "PUT", "POST"] * 2


# ---------------------------------------------------------------------------
# The key and the account
# ---------------------------------------------------------------------------
def test_the_organisation_key_is_read_from_its_secret():
    secrets = boto3.client("secretsmanager", region_name=names.REGION)
    with Stubber(secrets) as stubber:
        stubber.add_response(
            "get_secret_value",
            {"Name": names.SECRET_FLAGSMITH_API_KEY, "SecretString": ORG_KEY},
            expected_params={"SecretId": names.SECRET_FLAGSMITH_API_KEY},
        )
        assert bootstrap.organisation_key(secrets) == ORG_KEY


@pytest.mark.parametrize("stored", [" padded ", '{"key": "x"}', "Api-Key x"])
def test_a_secret_that_is_not_a_bare_key_is_refused_without_repeating_it(stored):
    secrets = boto3.client("secretsmanager", region_name=names.REGION)
    with Stubber(secrets) as stubber:
        stubber.add_response("get_secret_value", {"SecretString": stored})
        with pytest.raises(bootstrap.Refused) as raised:
            bootstrap.organisation_key(secrets)
    assert stored.strip() == "" or stored not in str(raised.value)


def test_another_account_is_refused():
    sts = boto3.client("sts", region_name=names.REGION)
    with Stubber(sts) as stubber:
        stubber.add_response("get_caller_identity", {"Account": "123456789012"})
        with pytest.raises(bootstrap.Refused, match="123456789012"):
            bootstrap.check_account(sts)
        stubber.add_response("get_caller_identity", {"Account": names.ACCOUNT_ID})
        bootstrap.check_account(sts)


def test_nothing_printed_holds_a_key(admin, flagsmith):
    flagsmith.add_feature("spike_kill_switch")
    lines = []
    logged = bootstrap.Admin(ORG_KEY, transport=flagsmith, log=lambda m, p, s: lines.append(p))
    bootstrap.main([], admin=logged, out=lines.append)
    bootstrap.main(["--v2-probe", "spike_kill_switch"], admin=logged, out=lines.append)
    text = "\n".join(lines)
    assert ORG_KEY not in text and DEV_KEY not in text and PROD_KEY not in text
