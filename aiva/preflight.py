"""Pre-flight checks: fail fast, with a clear reason, before the real audit runs.

Every failure mode here (no credentials, wrong region, missing IAM permission,
Bedrock model access not granted) previously surfaced as either a raw Python
traceback or, worse, a silently degraded report where every use case comes
back "Task not identifiable" with no indication why. Run this before Step 1
so a self-service customer gets one clear answer instead of a support ticket.
"""

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, EndpointConnectionError


def run_preflight(bucket, prefix, region, model_id):
    """Run all checks and return a list of {name, status, detail, hint}.

    status is "pass" or "fail". hint is only present on failure and names the
    concrete next step (attach a policy, enable model access, fix a flag).
    Does not raise; callers decide whether a failure should stop the run.
    """
    checks = []

    identity = _check_credentials(checks)
    _check_bucket_access(checks, bucket, prefix, region, identity)

    if identity is None:
        checks.append({
            "name": "Bedrock model access",
            "status": "fail",
            "detail": "Skipped: no valid AWS credentials.",
            "hint": "Fix AWS credentials first.",
        })
    else:
        _check_model_access(checks, region, model_id)

    return checks


def _check_credentials(checks):
    """Confirm the caller has usable AWS credentials. Returns identity or None."""
    try:
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        checks.append({
            "name": "AWS credentials",
            "status": "pass",
            "detail": f"Authenticated as {identity['Arn']} (account {identity['Account']}).",
        })
        return identity
    except NoCredentialsError:
        checks.append({
            "name": "AWS credentials",
            "status": "fail",
            "detail": "No AWS credentials found.",
            "hint": "Set AWS_PROFILE or configure credentials (aws configure / aws sso login) "
                    "for the account whose Bedrock logs you want to audit.",
        })
    except ClientError as e:
        checks.append({
            "name": "AWS credentials",
            "status": "fail",
            "detail": f"Credentials rejected: {e.response['Error'].get('Code', 'Unknown')}.",
            "hint": "Your session may be expired. Re-run aws sso login or refresh your credentials.",
        })
    return None


def _check_bucket_access(checks, bucket, prefix, region, identity):
    """Confirm the log bucket exists, is in the given region, and is listable."""
    if identity is None:
        checks.append({
            "name": "S3 log bucket access",
            "status": "fail",
            "detail": "Skipped: no valid AWS credentials.",
            "hint": "Fix AWS credentials first.",
        })
        return

    s3 = boto3.client("s3", region_name=region)
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        code = e.response["Error"].get("Code", "")
        actual_region = e.response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get(
            "x-amz-bucket-region"
        )
        if code in ("301", "PermanentRedirect") or actual_region:
            hint = (f"The bucket is in '{actual_region}', not '{region}'. Re-run with "
                    f"--region {actual_region}." if actual_region else
                    f"The bucket is not in '{region}'. Check the bucket's actual region.")
            checks.append({
                "name": "S3 log bucket access",
                "status": "fail",
                "detail": f"Bucket '{bucket}' exists but is not in region '{region}'.",
                "hint": hint,
            })
        elif code in ("403", "AccessDenied"):
            checks.append({
                "name": "S3 log bucket access",
                "status": "fail",
                "detail": f"Access denied to bucket '{bucket}'.",
                "hint": "Attach docs/iam-policy.json (s3:GetObject, s3:ListBucket) scoped to "
                        "this bucket to your role or user.",
            })
        elif code in ("404", "NoSuchBucket"):
            checks.append({
                "name": "S3 log bucket access",
                "status": "fail",
                "detail": f"Bucket '{bucket}' does not exist in this account/region.",
                "hint": "Check --bucket for typos, or run scripts/enable-model-invocation-logging.sh "
                        "if Model Invocation Logging has not been set up yet.",
            })
        else:
            checks.append({
                "name": "S3 log bucket access",
                "status": "fail",
                "detail": f"Unexpected error accessing bucket '{bucket}': {code}.",
                "hint": "Check the bucket name, region, and your permissions.",
            })
        return
    except EndpointConnectionError:
        checks.append({
            "name": "S3 log bucket access",
            "status": "fail",
            "detail": f"Could not reach S3 in region '{region}'.",
            "hint": "Check --region is a valid AWS region and you have network connectivity.",
        })
        return

    checks.append({
        "name": "S3 log bucket access",
        "status": "pass",
        "detail": f"Bucket '{bucket}' is reachable in region '{region}'.",
    })

    log_prefix = f"{prefix}/AWSLogs/"
    try:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=log_prefix, MaxKeys=1)
        if resp.get("KeyCount", 0) > 0:
            checks.append({
                "name": "Model Invocation Logs present",
                "status": "pass",
                "detail": f"Found log objects under s3://{bucket}/{log_prefix}.",
            })
        else:
            checks.append({
                "name": "Model Invocation Logs present",
                "status": "warn",
                "detail": f"No log objects found yet under s3://{bucket}/{log_prefix}.",
                "hint": "This is expected on a brand-new setup. Logging takes a few minutes to "
                        "start appearing after the first Bedrock call; check --prefix if you "
                        "expected data immediately.",
            })
    except ClientError as e:
        code = e.response["Error"].get("Code", "")
        checks.append({
            "name": "Model Invocation Logs present",
            "status": "fail",
            "detail": f"Could not list objects under '{log_prefix}': {code}.",
            "hint": "Confirm s3:ListBucket is granted (see docs/iam-policy.json).",
        })


def _check_model_access(checks, region, model_id):
    """Confirm the caller can actually invoke the classification model.

    A tiny real Converse call, not just an IAM simulation, because Bedrock
    model access is a separate per-model toggle in the console on top of IAM.
    """
    bedrock = boto3.client("bedrock-runtime", region_name=region)
    try:
        bedrock.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 1, "temperature": 0},
        )
        checks.append({
            "name": "Bedrock model access",
            "status": "pass",
            "detail": f"Successfully invoked '{model_id}' in '{region}'.",
        })
    except ClientError as e:
        code = e.response["Error"].get("Code", "")
        if code == "AccessDeniedException":
            checks.append({
                "name": "Bedrock model access",
                "status": "fail",
                "detail": f"Access denied invoking '{model_id}'.",
                "hint": "Enable model access for this model in the Bedrock console "
                        "(Model access page) in this account/region, and confirm your IAM "
                        "policy grants bedrock:InvokeModel (see docs/iam-policy.json).",
            })
        elif code in ("ValidationException", "ResourceNotFoundException"):
            checks.append({
                "name": "Bedrock model access",
                "status": "fail",
                "detail": f"Model '{model_id}' is not available in region '{region}'.",
                "hint": "Check --model is a valid model ID/inference profile for this region, "
                        "or try a different --region.",
            })
        elif code == "ThrottlingException":
            checks.append({
                "name": "Bedrock model access",
                "status": "warn",
                "detail": "Bedrock throttled the pre-flight check.",
                "hint": "This account/region may be near a Bedrock rate limit. The audit "
                        "may run slowly or hit throttling; consider requesting a quota increase.",
            })
        else:
            checks.append({
                "name": "Bedrock model access",
                "status": "fail",
                "detail": f"Unexpected error invoking '{model_id}': {code}.",
                "hint": "Check the Bedrock service status and your model ID.",
            })
    except EndpointConnectionError:
        checks.append({
            "name": "Bedrock model access",
            "status": "fail",
            "detail": f"Could not reach Bedrock in region '{region}'.",
            "hint": "Check --region is a valid AWS region with Bedrock available, and that "
                    "you have network connectivity.",
        })
