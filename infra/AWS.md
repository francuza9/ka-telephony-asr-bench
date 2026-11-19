# AWS setup

Account `100678004861`, region `us-east-1`, CLI profile `satli` (IAM user `satli-admin`).
Scripts default to that profile; override with `AWS_PROFILE`.

## Resources

| Resource | Name / ID |
|---|---|
| Budget | `asr-bench-monthly`, $500/month, credits excluded; emails to g.tskitishvili@satli.dev at actual 20/60/100% and forecasted 100% |
| S3 bucket | `satli-asr-bench-100678004861` (private, SSE-S3, no versioning) |
| IAM role + instance profile | `asr-bench-ec2` (SSM core + inline `asr-bench-bucket-rw`, limited to the bucket) |
| Key pair | `asr-bench` (ed25519), private key at `~/.ssh/asr-bench.pem` |
| Security group | `asr-bench-sg` = `sg-092212ad96856ec13`, default VPC `vpc-0ffcac9df8a31209a`; SSH 22 only from `212.58.103.84/32`, all outbound |
| Launch template | `asr-bench-gpu` = `lt-025cd411d9ce67be1` (v1) |
| AMI | `ami-012ba162b9cd2729c`, Deep Learning OSS Nvidia Driver AMI GPU PyTorch 2.7 (Ubuntu 22.04) 20260427 |
| AMI SSM parameter | `/aws/service/deeplearning/ami/x86_64/oss-nvidia-driver-gpu-pytorch-2.7-ubuntu-22.04/latest/ami-id` |

The template pins the AMI ID resolved from that parameter on 2026-11-19, so runs are reproducible. To move to a newer AMI, re-read the parameter and create a new template version.
Template defaults: g6.xlarge, 200 GB gp3 root (deleted on termination), IMDSv2 required, shutdown behavior `stop`, tag `Project=asr-bench` on instance and volume.

The SSH rule is tied to one IP. If your IP changes, add the new `/32` to `asr-bench-sg`.

## Quota

"Running On-Demand G and VT instances" is 16 vCPU. This covers the G family only (g6.xlarge = 4 vCPU, g6e.xlarge = 4 vCPU), so no A100/H100 (P family).

- Inference: `g6.xlarge` (1x L4)
- Fine-tuning: `g6e.xlarge` (1x L40S)

## Usage

```bash
infra/launch.sh                # g6.xlarge; prints the SSH command
infra/launch.sh g6e.xlarge     # fine-tuning
ssh -i ~/.ssh/asr-bench.pem ubuntu@<public-dns>
infra/teardown.sh <instance-id>
```

`launch.sh` also creates a CloudWatch alarm `asr-bench-idle-<instance-id>` that stops the instance after 30 minutes with CPUUtilization < 5%. GPU-bound jobs with idle CPU can trip it; if a long job is running, check CPU or disable the alarm first. `teardown.sh` deletes the alarm.

## Rule: push, then terminate

Push outputs to GitHub or `s3://satli-asr-bench-100678004861/` (the instance role has access), then terminate. A stopped instance still bills for its 200 GB disk, and the idle alarm only stops, never terminates.

## Usage tracking

```bash
scripts/aws_usage.py             # credits, spend, GPU-hours, burn rate, live instances/volumes
scripts/aws_usage.py --json      # machine-readable
scripts/aws_usage.py --refresh   # bypass the cache
scripts/aws_usage.py --since 2026-11-01
```

Read-only: it only calls `get-`/`list-`/`describe-` APIs (`billing get-credits`, Cost Explorer, `ec2 describe-*`). Cost Explorer costs $0.01 per request, so a run makes 1 CE call (2 if `get-credits` fails and it falls back to estimating credits from CE Credit records) and caches results in `.cache/aws_usage.json` for 6 h. CE data lags up to ~24 h. `infra/teardown.sh` prints a `--brief` summary at the end. Warns on instances running > 12 h, credits < 20% left, and credits expiring within 60 days.
