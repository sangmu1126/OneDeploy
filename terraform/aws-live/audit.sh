#!/bin/sh
# Read-only AWS plan audit. Saved plans can contain sensitive values.
set -eu
umask 077
cd "$(dirname "$0")"
audit_dir=$(mktemp -d "${TMPDIR:-/tmp}/onedeploy-terraform-audit.XXXXXX")
trap 'rm -rf "$audit_dir"' EXIT HUP INT TERM
terraform plan -input=false -no-color -out="$audit_dir/plan.tfplan" >/dev/null
terraform show -json "$audit_dir/plan.tfplan" > "$audit_dir/plan.json"
python3 audit_plan.py "$audit_dir/plan.json"
