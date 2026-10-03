import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "terraform/aws-live/audit_plan.py"
SPEC = importlib.util.spec_from_file_location("audit_plan", SCRIPT)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def plan(changed=None):
    return {"format_version": "1.2", "planned_values": {}, "resource_changes": changed or []}


def known_service_change():
    return {
        "address": AUDIT.SERVICE,
        "change": {
            "actions": ["update"],
            "before": {
                "ingress_paths": [{"endpoint": "existing"}],
                "service_revision_arn": "existing",
                "wait_for_steady_state": None,
                "scaling_target": [{"min_task_count": 0}],
            },
            "after": {
                "ingress_paths": None,
                "service_revision_arn": None,
                "wait_for_steady_state": False,
                "scaling_target": [{"min_task_count": 0}],
            },
            "after_unknown": {
                "current_deployment": True,
                "ingress_paths": True,
                "service_revision_arn": True,
            },
        },
    }


class AuditPlanTests(unittest.TestCase):
    def test_missing_plan_fields(self):
        self.assertEqual(AUDIT.classify({})[0], 3)

    def test_clean_plan(self):
        self.assertEqual(AUDIT.classify(plan())[0], 0)

    def test_known_provider_update_still_blocks_apply(self):
        self.assertEqual(AUDIT.classify(plan([known_service_change()]))[0], 2)

    def test_unexpected_ecs_scaling_change(self):
        resource = known_service_change()
        resource["change"]["after"]["scaling_target"] = [{"min_task_count": 1}]
        self.assertEqual(AUDIT.classify(plan([resource]))[0], 3)

    def test_other_resource_change(self):
        resource = known_service_change()
        resource["address"] = "aws_cloudformation_stack.demo_app_database"
        self.assertEqual(AUDIT.classify(plan([resource]))[0], 3)

    def test_destroy(self):
        resource = known_service_change()
        resource["change"]["actions"] = ["delete"]
        self.assertEqual(AUDIT.classify(plan([resource]))[0], 3)

    def test_output_change(self):
        result = plan()
        result["output_changes"] = {"database_password": {"actions": ["update"]}}
        self.assertEqual(AUDIT.classify(result)[0], 3)


if __name__ == "__main__":
    unittest.main()
