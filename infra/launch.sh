#!/usr/bin/env bash
# Launch one asr-bench GPU instance from the launch template.
# Usage: infra/launch.sh [instance-type]   (default g6.xlarge; use g6e.xlarge for fine-tuning)
set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-satli}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_PAGER=""

TYPE="${1:-g6.xlarge}"
TEMPLATE="asr-bench-gpu"
KEY="$HOME/.ssh/asr-bench.pem"

ID=$(aws ec2 run-instances \
  --launch-template "LaunchTemplateName=$TEMPLATE,Version=\$Latest" \
  --instance-type "$TYPE" \
  --min-count 1 --max-count 1 \
  --query 'Instances[0].InstanceId' --output text)
echo "Launched $ID ($TYPE), waiting for running..."
aws ec2 wait instance-running --instance-ids "$ID"

# Stop (not terminate) after 30 min of CPUUtilization < 5%: 6 x 5-minute periods.
aws cloudwatch put-metric-alarm \
  --alarm-name "asr-bench-idle-$ID" \
  --alarm-description "Stop $ID after 30 min of CPU < 5%" \
  --namespace AWS/EC2 --metric-name CPUUtilization \
  --dimensions "Name=InstanceId,Value=$ID" \
  --statistic Average --period 300 --evaluation-periods 6 \
  --threshold 5 --comparison-operator LessThanThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "arn:aws:automate:$AWS_REGION:ec2:stop"

DNS=$(aws ec2 describe-instances --instance-ids "$ID" \
  --query 'Reservations[0].Instances[0].PublicDnsName' --output text)
echo "Instance: $ID"
echo "Idle alarm: asr-bench-idle-$ID (stops after 30 min of CPU < 5%)"
echo "Connect:   ssh -i $KEY ubuntu@$DNS"
echo "Tear down: infra/teardown.sh $ID"
