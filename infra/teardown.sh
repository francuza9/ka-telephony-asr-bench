#!/usr/bin/env bash
# Terminate an asr-bench instance and delete its idle alarm.
# Usage: infra/teardown.sh <instance-id>
set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-satli}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_PAGER=""

ID="${1:?usage: infra/teardown.sh <instance-id>}"

aws ec2 terminate-instances --instance-ids "$ID" \
  --query 'TerminatingInstances[0].[InstanceId,CurrentState.Name]' --output text
aws cloudwatch delete-alarms --alarm-names "asr-bench-idle-$ID"
echo "Terminated $ID and deleted alarm asr-bench-idle-$ID"
