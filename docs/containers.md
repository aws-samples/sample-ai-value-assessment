# Run in a container

The [`Dockerfile`](../Dockerfile) builds aiva from the checked-out source, so the
version is whatever you check out. Build it yourself and push it to your own
registry. aiva reads prompt data, so most teams will want their own image scan
and signing rather than a public image.

The commands below are examples. Change names, regions and sizes to fit your account.

## Build

```bash
git clone https://github.com/aws-samples/sample-ai-value-assessment.git
cd sample-ai-value-assessment
docker build -t aiva .            # add --platform linux/amd64 or linux/arm64 to match where it runs
docker run --rm aiva --help
```

The image runs as a non-root user (`aiva`), and `aiva` is the entrypoint.

## Run on any Docker host

Pass credentials as environment variables and mount a folder for the report:

```bash
aws configure export-credentials --format env-no-export > aiva.env   # short-lived credentials
echo "AWS_REGION=ap-southeast-2" >> aiva.env
mkdir -p out
docker run --rm --env-file aiva.env -v "$PWD/out:/home/aiva/out" \
  aiva run --config s3://my-config-bucket/aiva.toml
rm aiva.env
```

With `output = "out/report"` in the config, the report lands in `./out`. With an
`s3://` output, no mount is needed.

## Run on ECS Fargate

### Push the image to ECR

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=ap-southeast-2
REPO=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/aiva

aws ecr create-repository --region $REGION --repository-name aiva --image-scanning-configuration scanOnPush=true
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
docker build --platform linux/arm64 -t $REPO:v0.1.0 .
docker push $REPO:v0.1.0
```

### Two roles

- **Task role:** what aiva itself can do. Start from
  [`iam-policy.json`](iam-policy.json), then add `s3:GetObject` on the config
  object, `s3:PutObject` on the report prefix, `kms:Decrypt` if the log bucket
  uses a customer managed key, and `sts:AssumeRole` for any `role_arn` sources.
  Trusted by `ecs-tasks.amazonaws.com`.
- **Execution role:** what ECS needs to start the task, which is to pull the image and
  write logs. The AWS managed policy `AmazonECSTaskExecutionRolePolicy` covers
  it. It needs no access to the log bucket or Bedrock.

### Task definition

```json
{
  "family": "aiva",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "1024",
  "memory": "2048",
  "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
  "ephemeralStorage": {"sizeInGiB": 21},
  "taskRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/aiva-task",
  "executionRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/aiva-execution",
  "containerDefinitions": [{
    "name": "aiva",
    "image": "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/aiva:v0.1.0",
    "command": ["run", "--config", "s3://my-config-bucket/aiva.toml"],
    "environment": [{"name": "AWS_STS_REGIONAL_ENDPOINTS", "value": "regional"}],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/aiva",
        "awslogs-region": "<REGION>",
        "awslogs-stream-prefix": "aiva",
        "awslogs-create-group": "true"
      }
    }
  }]
}
```

Set `output` in the config to an `s3://` path, since the task's disk is gone
when it stops. For large windows, raise `ephemeralStorage` (up to 200 GiB),
using the disk sizes in the README's
[What it needs](../README.md#what-it-needs). `awslogs-create-group` needs
`logs:CreateLogGroup` on the execution role, or create the log group first.

### Network

Use a private subnet and a security group with no inbound rules and outbound
HTTPS only. Without a NAT gateway, add VPC endpoints for:

- S3 (gateway endpoint)
- `bedrock-runtime`, `sts` (interface endpoints, used by aiva)
- `ecr.api`, `ecr.dkr`, `logs` (interface endpoints, used by ECS to start the task)

### Run it

```bash
aws ecs create-cluster --cluster-name aiva
aws ecs register-task-definition --cli-input-json file://aiva-task.json
aws ecs run-task --cluster aiva --launch-type FARGATE --task-definition aiva \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-aaaa],securityGroups=[sg-bbbb],assignPublicIp=DISABLED}"
```

Follow progress in the `/ecs/aiva` log group. The task stops when the run ends,
and its exit code shows on the stopped task.

### On a schedule

EventBridge Scheduler can start the same task, for example every Monday at
02:00 UTC. The scheduler needs its own role with `ecs:RunTask` on the task
definition and `iam:PassRole` on the task and execution roles.

```bash
aws scheduler create-schedule --name aiva-weekly \
  --schedule-expression "cron(0 2 ? * MON *)" --flexible-time-window Mode=OFF \
  --target '{
    "Arn": "arn:aws:ecs:<REGION>:<ACCOUNT_ID>:cluster/aiva",
    "RoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/aiva-scheduler",
    "EcsParameters": {
      "TaskDefinitionArn": "arn:aws:ecs:<REGION>:<ACCOUNT_ID>:task-definition/aiva",
      "LaunchType": "FARGATE",
      "NetworkConfiguration": {"awsvpcConfiguration": {
        "Subnets": ["subnet-aaaa"], "SecurityGroups": ["sg-bbbb"], "AssignPublicIp": "DISABLED"}}
    }
  }'
```

`from` in the config is a fixed date, so a scheduled `aiva run` reads from the
same start date each time. For a rolling window, use
`["audit", "--bucket", "...", "--days", "7", "--output", "s3://..."]` as the
command instead. It reads one source only.

## Run on EKS

Run it as a Kubernetes Job whose service account maps to the task role above,
through EKS Pod Identity or IRSA, with the same image and arguments:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: aiva
spec:
  backoffLimit: 0
  template:
    spec:
      serviceAccountName: aiva
      restartPolicy: Never
      containers:
        - name: aiva
          image: <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/aiva:v0.1.0
          args: ["run", "--config", "s3://my-config-bucket/aiva.toml"]
          env:
            - {name: AWS_REGION, value: <REGION>}
```

`backoffLimit: 0` stops a failed run from being retried from the start.
