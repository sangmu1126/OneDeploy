"""Classify a Terraform plan without treating provider drift as safe to apply."""

import json
import sys


SERVICE = "aws_ecs_express_gateway_service.demo_web"
PROVIDER_ONLY_FIELDS = {
    "ingress_paths",
    "service_revision_arn",
    "wait_for_steady_state",
}


def classify(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("format_version"), str) or "planned_values" not in plan:
        return 3, "Invalid Terraform plan JSON; inspect the source."
    if plan.get("output_changes"):
        return 3, "Terraform outputs would change; inspect the full plan."

    resources = plan.get("resource_changes", [])
    if not isinstance(resources, list):
        return 3, "Invalid Terraform resource changes; inspect the source."
    changes = [
        resource
        for resource in resources
        if resource["change"]["actions"] != ["no-op"]
    ]
    if not changes:
        return 0, "No resource changes are planned."
    if len(changes) != 1 or changes[0]["address"] != SERVICE:
        return 3, "Unexpected resource changes; inspect the full plan."

    change = changes[0]["change"]
    if change["actions"] != ["update"] or change.get("replace_paths"):
        return 3, "Unexpected ECS service action; inspect the full plan."

    before, after = change["before"], change["after"]
    changed = {
        key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
    }
    unknown = change.get("after_unknown", {})
    if (
        changed == PROVIDER_ONLY_FIELDS
        and before.get("wait_for_steady_state") is None
        and after.get("wait_for_steady_state") is False
        and all(after.get(key) is None and unknown.get(key) is True
                for key in ("ingress_paths", "service_revision_arn"))
        and unknown.get("current_deployment") is True
    ):
        return 2, (
            "Known ECS provider refresh update is still planned. "
            "Do not apply; review provider behavior and the full plan."
        )
    return 3, "Unexpected ECS service field changes; inspect the full plan."


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 audit_plan.py PLAN_JSON", file=sys.stderr)
        return 3
    try:
        with open(sys.argv[1], encoding="utf-8") as source:
            plan = json.load(source)
        status, message = classify(plan)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Cannot audit Terraform plan: {error}", file=sys.stderr)
        return 3
    print(message)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
