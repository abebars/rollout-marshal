"""A DynamoDB stand-in for the scripts' tests (task 3.5): rm-runs as a dict of items in
DynamoDB's JSON, with the conditions the scripts use."""

from __future__ import annotations

import copy

from botocore.exceptions import ClientError

from contracts import names


def pointer(run_id: str, *, feature_key: bool = True) -> dict:
    item = {names.PARTITION_KEY: {"S": names.ACTIVE_POINTER_PK}, "run_id": {"S": run_id}}
    if feature_key:
        item["feature_key"] = {"S": names.experiment_feature_key(run_id)}
    return item


def run_item(run_id: str, state: str) -> dict:
    return {
        names.PARTITION_KEY: {"S": names.run_pk(run_id)},
        "run_id": {"S": run_id},
        "state": {"S": state},
    }


def _failed(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "The condition failed"}},
        operation,
    )


class FakeDynamo:
    def __init__(self, *items: dict) -> None:
        self.items = {item[names.PARTITION_KEY]["S"]: copy.deepcopy(item) for item in items}
        self.calls: list[tuple[str, dict]] = []
        self.race: dict | None = None  # an item that appears just before the next put

    def get_item(self, **params):
        self.calls.append(("get_item", params))
        assert params["TableName"] == names.RUNS_TABLE
        found = self.items.get(params["Key"][names.PARTITION_KEY]["S"])
        return {"Item": copy.deepcopy(found)} if found else {}

    def put_item(self, **params):
        self.calls.append(("put_item", params))
        assert params["TableName"] == names.RUNS_TABLE
        if self.race is not None:
            self.items[self.race[names.PARTITION_KEY]["S"]] = self.race
            self.race = None
        pk = params["Item"][names.PARTITION_KEY]["S"]
        if params.get("ConditionExpression") == names.POINTER_CREATE_CONDITION and pk in self.items:
            raise _failed("PutItem")
        self.items[pk] = copy.deepcopy(params["Item"])
        return {}

    def delete_item(self, **params):
        self.calls.append(("delete_item", params))
        assert params["TableName"] == names.RUNS_TABLE
        pk = params["Key"][names.PARTITION_KEY]["S"]
        found = self.items.get(pk)
        expected = params.get("ExpressionAttributeValues", {}).get(":run_id", {}).get("S")
        if found is not None and expected is not None and found["run_id"]["S"] != expected:
            raise _failed("DeleteItem")
        self.items.pop(pk, None)
        return {}

    def writes(self) -> list[str]:
        return [name for name, _ in self.calls if name != "get_item"]
